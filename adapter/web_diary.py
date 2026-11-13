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
        reg(f"/{PLUGIN_NAME}/search/<file_key>", self.web_search_diaries, ["GET"],
            "日记本：时间轴、筛选与本地关键词证据搜索")
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
        reg(f"/{PLUGIN_NAME}/unlock/<file_key>", self.web_unlock, ["POST"],
            "日记本：用查看密码换取短期访问令牌（后续读接口带 query tk）")
        # 外部写入（v0.6.2，issue #3）：面向无面板会话的本地程序，走 AstrBot
        # v1 API key + 插件写入凭证双闸，实现见 adapter/web_note.py
        reg(f"/{PLUGIN_NAME}/note", self.web_external_note, ["POST"],
            "旧外部直写：仅兼容没有 API 版本字段的旧凭证")
        reg(f"/{PLUGIN_NAME}/material", self.web_external_material, ["POST"],
            "外部素材 v2：写入签名待吸收队列")
        reg(f"/{PLUGIN_NAME}/materials/<file_key>", self.web_list_materials, ["GET"],
            "日记本：查看待吸收素材")
        reg(f"/{PLUGIN_NAME}/materials/<file_key>/action", self.web_update_material, ["POST"],
            "日记本：重试或明确丢弃素材")

    @staticmethod
    def _valid_file_key(file_key: str) -> bool:
        return bool(re.fullmatch(r"[0-9a-f]{16}", file_key or ""))

    # ---- 查看密码 → 短期会话令牌（P3-7） ----
    #
    # 查看密码原先经 GET query 传输，会进访问日志 / 浏览器历史 / Referer。
    # 宿主 bridge 的 api:get 与 files:download 只能带 query 参数、不能带自定义
    # header（已核对 AstrBot v4.29.3 PluginViewPage.vue），所以令牌走 query 参数
    # 名 `tk`：前端先 POST /unlock/<file_key> 用密码换令牌，之后读接口带 tk。
    # 令牌只放进程内存：不落盘、不进日志，插件重启即失效。
    _WEB_TOKEN_TTL = 1800   # 令牌有效期（秒），与前端 expires_in 一致
    _WEB_TOKEN_MAX = 256    # 同时在用的令牌上限，超出丢最旧的

    def _web_token_store(self) -> dict:
        """内存令牌表（懒初始化）：token -> {file_key, owner, exp}。"""
        tokens = getattr(self, "_web_tokens", None)
        if not isinstance(tokens, dict):
            tokens = {}
            self._web_tokens = tokens
        return tokens

    def _web_token_now(self) -> float:
        try:
            return float(self._now().timestamp())
        except Exception:
            return 0.0

    def _web_prune_tokens(self, now_ts: float) -> None:
        """清掉已过期（或损坏）的令牌。"""
        tokens = self._web_token_store()
        for token in [
            t for t, rec in tokens.items()
            if not isinstance(rec, dict) or float(rec.get("exp") or 0) <= now_ts
        ]:
            tokens.pop(token, None)

    def _web_issue_token(self, owner_id: str, file_key: str) -> str:
        """签发一枚绑定 (file_key, owner) 的短期令牌，返回 <hex>。"""
        now_ts = self._web_token_now()
        tokens = self._web_token_store()
        self._web_prune_tokens(now_ts)
        while len(tokens) >= self._WEB_TOKEN_MAX:
            tokens.pop(next(iter(tokens)), None)
        token = secrets.token_hex(24)
        tokens[token] = {
            "file_key": str(file_key),
            "owner": str(owner_id),
            "exp": now_ts + self._WEB_TOKEN_TTL,
        }
        return token

    def _web_token_valid(self, owner_id: str, file_key: str, token=None) -> bool:
        """query `tk` 是否命中该 (file_key, owner) 且未过期。

        token=None 时从 query 取 `tk`（GET 读接口）；POST 批量删除由调用方
        把请求体里的 tk 传进来（宿主 bridge 的 api:post 不能带 query）。
        """
        if token is None:
            try:
                token = _web_request.query.get("tk", "") or ""
            except Exception:
                token = ""
        token = str(token or "").strip()
        if not token or not file_key:
            return False
        tokens = self._web_token_store()
        rec = tokens.get(token)
        if not isinstance(rec, dict):
            return False
        if float(rec.get("exp") or 0) <= self._web_token_now():
            tokens.pop(token, None)
            return False
        return (
            str(rec.get("file_key") or "") == str(file_key)
            and str(rec.get("owner") or "") == str(owner_id)
        )

    def _check_web_password(self, owner_id: str, file_key: str = "",
                            for_download: bool = False, pw: str = None,
                            tk: str = None):
        """Web 翻阅 / 导出 / 删除的密码闸。返回 None 放行；否则返回给前端的响应。

        校验顺序（P3-7）：先认 query `tk` 令牌（对该 file_key + owner 有效且未
        过期）；否则回退旧密码通道——GET 从 query 取 pw，POST（批量删除）由调用
        方把请求体里的 pw 传进来（pw=None 时沿用 query，保证既有行为逐字节不变）。
        for_download=True 时（bridge.download 期望文件响应）用错误而非 JSON 表达。
        """
        if not self._is_allowed(owner_id):
            return _web_error("该用户不在插件白名单内", status_code=403)
        rec = self._view_password_rec(owner_id)
        if rec is None:
            return None
        if self._web_token_valid(owner_id, file_key, tk):
            return None
        if pw is None:
            pw = _web_request.query.get("pw", "") or ""
        pw = str(pw).strip()
        if not pw:
            if for_download:
                return _web_error("PASSWORD_REQUIRED", status_code=403)
            return _web_json({"need_password": True})
        # 兼容保留旧 pw 通道（前端迁移到 tk 后可以删掉）；只记弃用、不记密码本体
        logger.debug(
            "[memory_diary] Web 密码闸仍在用已弃用的 pw 参数，建议改用 /unlock 令牌"
        )
        if not self._verify_view_password(rec, pw):
            return _web_error("PASSWORD_WRONG", status_code=403)
        return None

    async def web_unlock(self, file_key: str):
        """用查看密码换短期访问令牌（P3-7）。

        POST body: ``{"pw": "密码", "qq": "可选，旧 zip 确认主人用"}``。
        成功 200 ``{"unlocked": true, "token": "<hex>", "expires_in": 1800}``；
        没设查看密码的日记也发令牌（前端统一带 tk，后端行为不变）；
        密码错 403 ``PASSWORD_WRONG``；旧 zip 未解析出主人 200 ``{"need_owner": true}``。
        """
        if not self._valid_file_key(file_key):
            return _web_error("无效的用户标识", status_code=400)
        path = self.data_dir / f"diary_{file_key}.zip"
        if not path.exists():
            return _web_error("日记文件不存在", status_code=404)
        payload = await _web_request.json(default={})
        if not isinstance(payload, dict):
            return _web_error("无效的请求内容", status_code=400)
        qq_hint = str(payload.get("qq") or "").strip()
        if not qq_hint:
            qq_hint = (_web_request.query.get("qq", "") or "").strip()
        owner, err = self._resolve_zip_owner(path, file_key, qq_hint)
        if not owner:
            if err == "OWNER_UNKNOWN":
                return _web_json({"need_owner": True})
            return _web_error(f"无法确认日记主人：{err}", status_code=400)
        if not self._is_allowed(owner):
            return _web_error("该用户不在插件白名单内", status_code=403)
        rec = self._view_password_rec(owner)
        if rec is not None:
            pw = str(payload.get("pw") or "").strip()
            if not pw:
                return _web_error("PASSWORD_REQUIRED", status_code=403)
            if not self._verify_view_password(rec, pw):
                return _web_error("PASSWORD_WRONG", status_code=403)
        return _web_json({
            "unlocked": True,
            "token": self._web_issue_token(owner, file_key),
            "expires_in": self._WEB_TOKEN_TTL,
        })

    async def web_list_users(self):
        users = []
        owner_map = None
        # v0.7.7（Bug C）：外部删除告警——登记在册但文件凭空消失的日记包
        alerts = []
        try:
            for item in self._vanished_diary_alerts():
                alerts.append(
                    f"日记包 {item['file']}（归属 {item['owner'] or '未知'}）"
                    "登记在册但已凭空消失，疑似被外部删除；"
                    f"可用备份 {len(item['backups'])} 份"
                )
        except Exception as e:
            logger.warning(f"[memory_diary] Web 外部删除告警汇总失败: {e}")
        for path in sorted(self.data_dir.glob("diary_*.zip")):
            file_key = path.stem[len("diary_"):]
            if not self._valid_file_key(file_key):
                continue
            owner, nick = self._zip_owner_info(path)
            if not owner:
                if owner_map is None:
                    owner_map = self._load_owner_map()
                owner = owner_map.get(file_key, "")
            if owner and not self._is_allowed(owner):
                continue
            day_count, latest = 0, ""
            try:
                with self._diary_zip(path) as z:
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
            # v0.7.7（Bug C）：全量条目验签失败 = 疑似被外部改写/捏造
            tampered_all = False
            if owner:
                try:
                    tampered_all = self._all_entries_tampered(owner)
                except Exception:
                    tampered_all = False
            if tampered_all:
                alerts.append(
                    f"日记包 {path.name}（归属 {owner}）全部条目验签失败，"
                    "疑似被外部改写；已锁死读写，可用 /恢复日记 从备份恢复"
                )
            item = {
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
            }
            # v0.7.7（Bug C）：告警字段只在真的异常时才出现（保持常态响应形状不变）
            if tampered_all:
                item["alert"] = "tampered"
            users.append(item)
        resp = {"users": users}
        if alerts:
            resp["alerts"] = alerts
        return _web_json(resp)

    async def web_list_days(self, file_key: str):
        if not self._valid_file_key(file_key):
            return _web_error("无效的用户标识", status_code=400)
        path = self.data_dir / f"diary_{file_key}.zip"
        if not path.exists():
            return _web_error("日记文件不存在", status_code=404)
        owner, _nick = self._zip_owner_info(path)
        owner = owner or self._load_owner_map().get(file_key, "")
        if not self._is_allowed(owner):
            return _web_error("该用户不在插件白名单内", status_code=403)
        try:
            with self._diary_zip(path) as z:
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
        gate = self._check_web_password(owner, file_key)
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
        gate = self._check_web_password(owner, file_key)
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
            # P2-1：上传本体限 32MB（压缩态）；解压后总量/压缩比双闸在
            # _verify_bundle 里，zip bomb 在校验前不会整体进内存
            await upload.save(tmp, max_bytes=32 * 1024 * 1024)
            owner_id, key, entries, images, annotations, provenance, current_self, materials, anchor_archive = (
                self._verify_bundle(tmp)
            )
            mode = self._install_bundle(
                owner_id, owner_id, key, entries, check_owner=False,
                images=images, annotations=annotations, provenance=provenance,
                current_self=current_self, materials=materials, anchor_archive=anchor_archive,
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
        # 宿主 bridge 的 api:post 不能带 query，所以批量删除的令牌/密码都在
        # 请求体里（tk/pw）；GET 接口才用 query。见 _check_web_password 的参数。
        gate = self._check_web_password(
            owner, file_key, pw=payload.get("pw"), tk=payload.get("tk")
        )
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
