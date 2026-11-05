# -*- coding: utf-8 -*-
"""日记本 Web 页面后端：路由注册、鉴权辅助、读日记/插图、导入、批量删除。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

import base64
import re
import secrets
import zipfile

from ..memory_diary.common import sniff_image_mime
from ..memory_diary.constants import PLUGIN_NAME
from ..memory_diary.log import logger
from .compat import _PluginUploadFile, _web_error, _web_json, _web_request, _WEB_API_OK


class WebDiaryApiMixin:
    """日记本 Web 页面后端：路由注册、鉴权辅助、读日记/插图、导入、批量删除。"""

    # ------------------------- 可视化视图：日记本 Web 页面 -------------------------
    #
    # AstrBot 插件可视化视图（views/diary-book/）的后端 API。
    # 页面运行在 Dashboard 受限 iframe 中、经 bridge 调用本组接口，
    # 天然继承面板登录鉴权——等价于"管理员在服务器上读文件"，不再另做口令。
    # 与 QQ 指令侧的关键差异：Web 上没有 QQ 会话上下文，而验签必须有主人 QQ
    # （文件名是 HMAC 派生、反推不出）。三层兜底：
    #   1. 新 zip 的 说明.txt 含"用户ID"行，直接可用；
    #   2. 旧 zip 没有该行时，页面请管理员输入一次主人 QQ，全量验签通过才接受，
    #      并记入 web_owner_map.json，以后免输；
    #   3. QQ 输错 → 签名对不上，明确拒绝。

    def _register_web_apis(self):
        if not _WEB_API_OK:
            return
        reg = getattr(self.context, "register_web_api", None)
        if reg is None:
            logger.warning(
                "[memory_diary] 当前 AstrBot 版本不支持插件可视化视图，日记本页面不可用"
            )
            return
        reg(f"/{PLUGIN_NAME}/users", self.web_list_users, ["GET"],
            "日记本：列出有日记的用户")
        reg(f"/{PLUGIN_NAME}/days/<file_key>", self.web_list_days, ["GET"],
            "日记本：列出某用户的日记日期")
        reg(f"/{PLUGIN_NAME}/day/<file_key>/<day>", self.web_read_day, ["GET"],
            "日记本：读取某天的日记")
        reg(f"/{PLUGIN_NAME}/export/<file_key>", self.web_export, ["GET"],
            "日记本：导出迁移包（浏览器下载）")
        reg(f"/{PLUGIN_NAME}/export_all", self.web_export_all, ["GET"],
            "日记本：一键备份全部用户日记（不含签名钥匙）")
        reg(f"/{PLUGIN_NAME}/export_all_local", self.web_export_all_local, ["GET"],
            "日记本：一键备份全部用户日记到本机并打开位置")
        reg(f"/{PLUGIN_NAME}/export_local/<file_key>", self.web_export_local, ["GET"],
            "日记本：导出迁移包到本机下载文件夹并打开位置")
        reg(f"/{PLUGIN_NAME}/import", self.web_import, ["POST"],
            "日记本：导入迁移包")
        reg(f"/{PLUGIN_NAME}/delete_days", self.web_delete_days, ["POST"],
            "日记本：批量删除某用户若干天的日记（不可恢复）")
        reg(f"/{PLUGIN_NAME}/prefs", self.web_get_prefs, ["GET"],
            "日记本：读取界面偏好")
        reg(f"/{PLUGIN_NAME}/prefs/save", self.web_save_prefs, ["POST"],
            "日记本：保存界面偏好")
        reg(f"/{PLUGIN_NAME}/paper", self.web_get_paper, ["GET"],
            "日记本：读取自定义纸张")
        reg(f"/{PLUGIN_NAME}/paper/upload", self.web_upload_paper, ["POST"],
            "日记本：上传自定义纸张")
        reg(f"/{PLUGIN_NAME}/backdrop", self.web_get_backdrop, ["GET"],
            "日记本：读取自定义背景板")
        reg(f"/{PLUGIN_NAME}/backdrop/upload", self.web_upload_backdrop, ["POST"],
            "日记本：上传自定义背景板")
        reg(f"/{PLUGIN_NAME}/image/<file_key>/<name>", self.web_get_diary_image,
            ["GET"], "日记本：读取日记插图")
        # 外部写入（v0.6.2，issue #3）：面向无面板会话的本地程序，走 AstrBot
        # v1 API key + 插件写入凭证双闸，实现见 adapter/web_note.py
        reg(f"/{PLUGIN_NAME}/note", self.web_external_note, ["POST"],
            "外部写入：凭写入凭证给日记追加段落（幂等、只追加、fail-closed）")

    @staticmethod
    def _valid_file_key(file_key: str) -> bool:
        return bool(re.fullmatch(r"[0-9a-f]{16}", file_key or ""))
    def _check_web_password(self, owner_id: str, for_download: bool = False,
                            pw: str = None):
        """Web 翻阅 / 导出 / 删除的密码闸。返回 None 放行；否则返回给前端的响应。

        for_download=True 时（bridge.download 期望文件响应）用错误而非 JSON 表达。
        GET 接口从 query 取 pw；POST 接口（批量删除）把密码放在请求体里，由调用方
        通过 pw 参数传入（pw=None 时沿用 query，保证原有 GET 行为逐字节不变）。
        """
        rec = self._view_password_rec(owner_id)
        if rec is None:
            return None
        if pw is None:
            pw = _web_request.query.get("pw", "") or ""
        pw = str(pw).strip()
        if not pw:
            if for_download:
                return _web_error("PASSWORD_REQUIRED", status_code=403)
            return _web_json({"need_password": True})
        if not self._verify_view_password(rec, pw):
            return _web_error("PASSWORD_WRONG", status_code=403)
        return None
    async def web_list_users(self):
        users = []
        owner_map = None
        for path in sorted(self.data_dir.glob("diary_*.zip")):
            file_key = path.stem[len("diary_"):]
            if not self._valid_file_key(file_key):
                continue
            owner, nick = self._zip_owner_info(path)
            if not owner:
                if owner_map is None:
                    owner_map = self._load_owner_map()
                owner = owner_map.get(file_key, "")
            day_count, latest = 0, ""
            try:
                with zipfile.ZipFile(path) as z:
                    days = sorted(
                        n for n in z.namelist() if self.ENTRY_NAME_RE.match(n)
                    )
                day_count = len(days)
                latest = self._entry_label(days[-1]) if days else ""
            except Exception:
                pass
            # glob 与 stat 之间文件可能被并发删除/替换（例如同一时刻正在导入覆盖
            # 该用户的 zip）。stat 失败只影响列表里的“大小”一列，不能让它把整个
            # 用户列表接口打成 500（那样页面连已存在的用户都列不出来）。
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            users.append({
                "file_key": file_key,
                "nickname": nick or "（未知昵称）",
                # 主人 QQ 给界面显示用（v0.6.1）：昵称可以是纯不可见字符，
                # 只显示昵称时用户在下拉框里分不清谁是谁；日记本是管理面板
                # 页面，QQ 号本就出现在导出/验签流程里，不构成额外暴露。
                "owner": owner,
                "owner_known": bool(owner),
                "protected": bool(owner and self._view_password_rec(owner)),
                "day_count": day_count,
                "latest": latest,
                "size": size,
            })
        return _web_json({"users": users})

    async def web_list_days(self, file_key: str):
        if not self._valid_file_key(file_key):
            return _web_error("无效的用户标识", status_code=400)
        path = self.data_dir / f"diary_{file_key}.zip"
        if not path.exists():
            return _web_error("日记文件不存在", status_code=404)
        try:
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                if bad is not None:
                    return _web_error(f"日记文件损坏（{bad} CRC 校验失败）",
                                      status_code=500)
                days = sorted(n for n in z.namelist() if self.ENTRY_NAME_RE.match(n))
        except Exception as e:
            return _web_error(f"读取日记文件失败：{e}", status_code=500)
        return _web_json({
            "days": [
                {"day": self._entry_day(n), "label": self._entry_label(n)}
                for n in days
            ]
        })

    async def web_read_day(self, file_key: str, day: str):
        if not self._valid_file_key(file_key) or not re.fullmatch(r"\d{8}", day or ""):
            return _web_error("无效的参数", status_code=400)
        path = self.data_dir / f"diary_{file_key}.zip"
        if not path.exists():
            return _web_error("日记文件不存在", status_code=404)
        qq_hint = (_web_request.query.get("qq", "") or "").strip()
        owner, err = self._resolve_zip_owner(path, file_key, qq_hint)
        if not owner:
            if err == "OWNER_UNKNOWN":
                return _web_json({"need_owner": True})
            return _web_error(f"无法确认日记主人：{err}", status_code=400)
        gate = self._check_web_password(owner)
        if gate is not None:
            return gate
        entry = self._md_entry(day)
        content, tampered = self._load_entry(owner, entry)
        if tampered:
            return _web_json({
                "tampered": True,
                "label": self._entry_label(entry),
                "content": "",
            })
        resp = {
            "tampered": False,
            "label": self._entry_label(entry),
            "content": content,
        }
        # 页边批注：按条件附加字段（零批注时响应与 v0.5.5 逐字节一致）。
        # 日记本身验签失败的早退分支不加批注字段，避免泄露任何在途信息。
        try:
            annotations, ann_tampered = self.annotations_web_payload(owner, day)
        except Exception as e:
            logger.warning(f"[memory_diary] 读取 Web 批注失败，已跳过: {e}")
            annotations, ann_tampered = [], False
        if annotations:
            resp["annotations"] = annotations
        if ann_tampered:
            resp["annotations_tampered"] = True
        return _web_json(resp)

    async def web_get_diary_image(self, file_key: str, name: str):
        """日记插图服务路由：从用户日记 zip 读图，验签后以 base64 JSON 返回。

        为什么不是二进制 file_response：日记本页面运行在 Dashboard 的 sandbox
        iframe 里，``<img src>`` 不会带上面板 Cookie（请求 401），而 bridge 只能
        传 JSON（契约附录 A）。所以这里把图片 base64 化后放在 ``image`` 字段——
        不能叫 ``data``：bridge 会把响应体顶层的 data 当信封解包
        （同款坑见 ``_web_get_image`` 与 tests/test_web.py）。

        ``name`` 可以是 ``images/xxx.png`` 也可以是纯文件名 ``xxx.png``：
        AstrBot 路由的 ``<name>`` 不匹配斜杠，页面实际用纯文件名；两种写法都
        支持，且只认 zip 里真实存在的条目（防目录穿越）。
        """
        if not self._valid_file_key(file_key):
            return _web_error("无效的用户标识", status_code=400)
        path = self.data_dir / f"diary_{file_key}.zip"
        if not path.exists():
            return _web_error("日记文件不存在", status_code=404)
        qq_hint = (_web_request.query.get("qq", "") or "").strip()
        owner, err = self._resolve_zip_owner(path, file_key, qq_hint)
        if not owner:
            if err == "OWNER_UNKNOWN":
                return _web_json({"need_owner": True})
            return _web_error(f"无法确认日记主人：{err}", status_code=400)
        gate = self._check_web_password(owner)
        if gate is not None:
            return gate
        raw_name = str(name or "").strip()
        entry = raw_name if raw_name.startswith("images/") else f"images/{raw_name}"
        # 只服务确实存在于 zip 里的条目：穿越路径/拼错的名字一律按缺失处理，
        # 不把 storage 的路径规则当作唯一防线。
        try:
            known = set(self.list_images(owner))
        except Exception as e:
            logger.warning(f"[memory_diary] Web 读取插图列表失败: {e}")
            known = set()
        if entry not in known:
            return _web_json({"found": False, "status": "missing"})
        data, status = self.load_image(owner, entry)
        if status in ("tampered", "no_key"):
            # no_key 与正文不可验签同口径：验不了就当篡改，宁可不显示
            return _web_json({"found": False, "status": "tampered"})
        if not data:
            return _web_json({"found": False, "status": "missing"})
        return _web_json({
            "found": True,
            "mime": sniff_image_mime(data),
            "image": base64.b64encode(data).decode("ascii"),
        })

    async def web_import(self):
        files = await _web_request.files()
        upload = files.get("file") if files else None
        if _PluginUploadFile is None or not isinstance(upload, _PluginUploadFile):
            return _web_error("没有收到文件", status_code=400)
        tmp = self.data_dir / f"import_tmp_{secrets.token_hex(4)}.zip"
        try:
            await upload.save(tmp)
            owner_id, key, entries, images, annotations = self._verify_bundle(tmp)
            mode = self._install_bundle(
                owner_id, owner_id, key, entries, check_owner=False,
                images=images, annotations=annotations,
            )
            logger.info(
                f"[memory_diary] Web 页面导入迁移包成功："
                f"{len(entries)} 天日记，主人 {owner_id}，模式 {mode}"
            )
            return _web_json({
                "imported": len(entries), "mode": mode, "owner": owner_id
            })
        except ValueError as e:
            return _web_error(f"导入被拒绝：{e}", status_code=400)
        except Exception as e:
            logger.error(f"[memory_diary] Web 导入失败: {e}")
            return _web_error("导入失败，详见后台日志", status_code=500)
        finally:
            # 上传的迁移包含签名钥匙，用完即删
            tmp.unlink(missing_ok=True)

    # ---- 日记本：批量删除（v0.6.1，用户建议） ----
    #
    # 复用 storage._delete_entries：签名、插图、批注、来源的级联清理与原子重写
    # 都在那一层，本接口只做参数校验、主人解析、密码闸与结果统计。
    # Web 侧是管理员在服务器上的操作，**不走** diary_op_interval 冷却（那是 QQ
    # 用户防刷用的）；但删除不可逆，前端必须二次确认并在文案里提示先导出备份。

    # 一次最多删多少天：保险丝，防止构造超大请求把事件循环占住（一年 366 天）。
    _WEB_BATCH_DELETE_MAX_DAYS = 366

    @staticmethod
    def _web_normalize_day(value) -> str:
        """把前端传来的日期归一化成内部 YYYYMMDD；不合法返回空串。

        页面日期列表给的是 ``web_list_days`` 的 ``day``（YYYYMMDD），也接受
        YYYY-MM-DD（脚本/手写调用更自然）。只校验格式与月日范围，不校验闰年——
        日历上不存在的那天本来就不会有日记条目。
        """
        raw = str(value or "").strip()
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if m:
            raw = "".join(m.groups())
        if not re.fullmatch(r"\d{8}", raw):
            return ""
        month, day = int(raw[4:6]), int(raw[6:8])
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return ""
        return raw

    async def web_delete_days(self):
        """批量删除某用户若干天的日记（不可恢复）。返回 deleted/remaining/missing。"""
        payload = await _web_request.json(default={})
        if not isinstance(payload, dict):
            return _web_error("无效的请求内容", status_code=400)
        file_key = str(payload.get("file_key") or "").strip()
        if not self._valid_file_key(file_key):
            return _web_error("无效的用户标识", status_code=400)
        path = self.data_dir / f"diary_{file_key}.zip"
        if not path.exists():
            return _web_error("日记文件不存在", status_code=404)

        raw_days = payload.get("days")
        if not isinstance(raw_days, list) or not raw_days:
            return _web_error("没有选择要删除的日期", status_code=400)
        if len(raw_days) > self._WEB_BATCH_DELETE_MAX_DAYS:
            return _web_error(
                f"一次最多删除 {self._WEB_BATCH_DELETE_MAX_DAYS} 天",
                status_code=400,
            )
        days = []
        for item in raw_days:
            day = self._web_normalize_day(item)
            if not day:
                return _web_error(f"无效的日期：{item}", status_code=400)
            if day not in days:  # 去重：重复日期只删一次，也不虚报 deleted
                days.append(day)

        # 主人解析与读/导出口径一致：旧 zip 的说明.txt 没有「用户ID」行时，
        # 要求管理员输入一次主人 QQ（全量验签通过才接受，并记入 owner_map）。
        qq_hint = str(payload.get("qq") or "").strip()
        if not qq_hint:
            qq_hint = (_web_request.query.get("qq", "") or "").strip()
        owner, err = self._resolve_zip_owner(path, file_key, qq_hint)
        if not owner:
            if err == "OWNER_UNKNOWN":
                return _web_json({"need_owner": True})
            return _web_error(f"无法确认日记主人：{err}", status_code=400)
        # 删除比翻阅更敏感：设了查看密码的日记，删除也必须先过密码闸。
        # 密码在 POST 请求体里（GET 接口才用 query），见 _check_web_password 的 pw 参数。
        gate = self._check_web_password(owner, pw=payload.get("pw"))
        if gate is not None:
            return gate

        deleted = self._delete_entries(owner, [self._md_entry(d) for d in days])
        remaining = len(self._list_entries(owner))
        logger.info(
            f"[memory_diary] Web 批量删除日记：主人 {owner} 删除 {deleted} 天，"
            f"剩余 {remaining} 天"
        )
        return _web_json({
            "deleted": deleted,
            "remaining": remaining,
            # 选了但 zip 里不存在的天数（前端用于提示「部分日期没有日记」）
            "missing": max(0, len(days) - deleted),
        })
