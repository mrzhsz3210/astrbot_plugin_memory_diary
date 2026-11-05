# -*- coding: utf-8 -*-
"""外部写入 API（v0.6.2，issue #3）：本地程序凭写入凭证给主人的日记追加段落。

与本目录其它 Web 接口的关键差异：日记本页面接口继承面板登录鉴权（管理员
在服务器上操作）；本接口面向**没有面板会话的本地程序**，经
``/api/v1/plugins/extensions/...`` 进入（AstrBot API key，scope=plugin，
是平台侧的第一道门），插件自己的写入凭证是第二道门。

四道安全闸（缺一不进包；与 README「外部写入 API」章节一一对应）：

1. 鉴权：token → owner 映射存于数据目录（只存 SHA-256 哈希，不落日志、
   不随导出包外流）；凭证与 source 绑定，白名单制；未配置凭证的主人
   默认拒绝写入（fail-closed）。
2. 幂等：ext_id 强制携带，去重索引进 provenance.json 的 external 记录——
   随包 HMAC 签名、重启不丢、与日记段落在**同一次原子重写**里落盘；
   重放返回首次结果，绝不二次追加。删除日记时 external 记录转存独立墓碑
   侧车（P2-4），删除后重放同一 ext_id 返回 replayed 且不复活已删内容。
3. 只追加：外部写入只追加为新段落，绝不覆盖、绝不删除既有内容；
   单条 8 KB 上限；日期校验复用 WebDiaryApiMixin._web_normalize_day。
4. fail-closed：任何一项校验失败 → 本轮整体不写；异常不吞，返回明确
   错误码；zip 重写沿用 storage 层现有原子写。

「external」只作为 provenance 元数据里的来源标记存在，**不是**第二信任
等级——条目仍是插件亲笔签名（这是与 issue #3 方案 2 的本质区别）。
"""

import asyncio
import re
import time

from ..memory_diary.errors import (
    DiaryCorruptError,
    DiaryKeyUnavailable,
    DiaryTamperedError,
)
from ..memory_diary.log import logger
from .compat import _web_json, _web_request


class WebNoteApiMixin:
    """外部写入 API：POST /astrbot_plugin_memory_diary/note。"""

    # 限流：每 token 每分钟 20 次（防脚本失控）。内存滑动窗口，重启清零——
    # 防的是失控不是蓄意，蓄意者本来就有凭证，限流挡不住，吊销才能。
    _EXT_RATE_LIMIT = 20
    _EXT_RATE_WINDOW = 60.0

    # source 标识：小写字母/数字/下划线/连字符，1~32 字符
    _EXT_SOURCE_RE = re.compile(r"^[a-z0-9_\-]{1,32}$")
    # ext_id：可打印 ASCII，1~64 字符
    _EXT_ID_RE = re.compile(r"^[!-~]{1,64}$")

    # ------------------------- 基础设施 -------------------------

    @staticmethod
    def _note_err(code: str, message: str, status: int):
        """统一错误体：{"ok": false, "code", "message"}（HTTP 状态码见文档表）。"""
        return _web_json(
            {"ok": False, "code": code, "message": message}, status_code=status
        )

    def _ext_locks(self) -> dict:
        locks = self.__dict__.get("_external_locks")
        if locks is None:
            locks = self.__dict__["_external_locks"] = {}
        return locks

    def _ext_rate_buckets(self) -> dict:
        buckets = self.__dict__.get("_external_rate")
        if buckets is None:
            buckets = self.__dict__["_external_rate"] = {}
        return buckets

    def _ext_rate_ok(self, token_hash: str) -> bool:
        """滑动窗口限流：每 token 每分钟 _EXT_RATE_LIMIT 次。"""
        now = time.monotonic()
        bucket = self._ext_rate_buckets().setdefault(token_hash, [])
        cutoff = now - self._EXT_RATE_WINDOW
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= self._EXT_RATE_LIMIT:
            return False
        bucket.append(now)
        return True

    def _ext_display_name(self, source: str) -> str:
        """来源显示名：读配置 `external_source_display`（`来源标识=显示名`，多条英文分号分隔）。

        只影响分隔条的展示文字；provenance 里记录的仍是机器来源标识，验签、
        幂等、审计全不受影响。配置缺项、格式错误或查不到时回退原始 source。
        """
        raw = str(self.config.get("external_source_display", "") or "")
        mapping: dict[str, str] = {}
        for part in raw.split(";"):
            if "=" not in part:
                continue
            key, _, value = part.partition("=")
            key, value = key.strip(), value.strip()
            # 显示名会写进日记分隔条（审查 P3-1）：剔除换行与分隔条语法字符，
            # 防止配置值伪造「—— …… ——」行或拆断注入结构；截断到 16 字符。
            value = re.sub(r"[\r\n—─–‒()（）【】]", "", value)[:16]
            if key and value:
                mapping[key] = value
        return mapping.get(source, source)

    @staticmethod
    def _break_separator_lines(text: str) -> str:
        """拆断外部正文里形似日记分隔条的行（P2-5），使解析侧不再认它。

        存储层的分隔条语法是「行首 `—— `、行尾 `——`」，被
        `_segments` / `_segment_info` / `_filter_segments_for_group` 当成插件
        写入的场景/模式元数据。外部 text 若含 `—— 21:00 写于群聊 ——` 就能让
        私聊来源的记忆混进群聊注入，`—— … 完整 ——` 还能占掉当天完整篇名额。
        在第一个「——」中间插一个零宽字符：展示上无差别，正则不再命中；
        只动行首（stripped 后以「——」开头）的行，不改动正文其余内容。
        """
        lines = []
        for line in text.split("\n"):
            if line.strip().startswith("——"):
                line = line.replace("——", "—\u200b—", 1)
            lines.append(line)
        return "\n".join(lines)

    # ------------------------- 端点 -------------------------

    async def web_external_note(self):
        """POST /note：凭写入凭证给主人某天的日记追加一个段落。

        校验顺序：形状 → 鉴权（401/403）→ 限流（429）→ 字段（400/413）→
        幂等重放 → 写入。鉴权排在字段校验之前：未通过鉴权的调用方无权知道
        其它字段错在哪。
        """
        payload = await _web_request.json(default=None)
        if not isinstance(payload, dict):
            return self._note_err("BAD_REQUEST", "请求体必须是 JSON 对象", 400)

        # ---- 闸一：鉴权 ----
        token = str(payload.get("token") or "").strip()
        rec = self.resolve_external_token(token)
        if rec is None:
            return self._note_err("UNAUTHORIZED", "凭证缺失或无效", 401)
        owner = str(rec.get("owner") or "").strip()
        source = str(payload.get("source") or "").strip()
        if not source or source != str(rec.get("source") or ""):
            return self._note_err(
                "SOURCE_NOT_ALLOWED", "source 不在该凭证的白名单内", 403
            )

        # ---- 限流 ----
        if not self._ext_rate_ok(self._external_token_hash(token)):
            return self._note_err(
                "RATE_LIMITED", "请求过于频繁，请稍后再试", 429
            )

        # ---- 字段校验 ----
        day = self._web_normalize_day(payload.get("day"))
        if not day:
            return self._note_err(
                "BAD_REQUEST", "day 非法：接受 YYYYMMDD 或 YYYY-MM-DD", 400
            )
        # 不允许未来日期（fail-closed）：日历上不存在的那天本来也不该有日记
        if day > self._now().strftime("%Y%m%d"):
            return self._note_err("BAD_REQUEST", "不允许写入未来日期", 400)
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return self._note_err("BAD_REQUEST", "text 缺失或为空", 400)
        # P2-5：写入前拆断外部正文里形似日记分隔条的行，避免外部内容伪造
        # 「写于群聊 / 完整」等插件元数据；长度上限按最终正文计算。
        text = self._break_separator_lines(text.strip())
        if len(text.encode("utf-8")) > self.EXTERNAL_NOTE_MAX_BYTES:
            return self._note_err(
                "PAYLOAD_TOO_LARGE",
                f"text 超过 {self.EXTERNAL_NOTE_MAX_BYTES} 字节上限",
                413,
            )
        ext_id = payload.get("ext_id")
        if isinstance(ext_id, str):
            ext_id = ext_id.strip()
        if not isinstance(ext_id, str) or not self._EXT_ID_RE.match(ext_id):
            return self._note_err(
                "BAD_REQUEST",
                "ext_id 缺失或非法（1~64 字符可打印 ASCII，客户端幂等键）",
                400,
            )

        # ---- 闸二/三/四：幂等 + 只追加 + fail-closed（同 owner 串行） ----
        lock = self._ext_locks().setdefault(owner, asyncio.Lock())
        async with lock:
            # 幂等重放：同 (source, ext_id) 已写过 → 返回首次结果，绝不二次追加
            prov, prov_status = self.load_provenance(owner)
            if prov_status in ("ok", "missing"):
                for name, entry in (prov.get("entries") or {}).items():
                    for r in entry.get("external") or []:
                        if r.get("source") == source and r.get("ext_id") == ext_id:
                            return _web_json(
                                {
                                    "ok": True,
                                    "day": str(name)[:8],
                                    "appended": False,
                                    "replayed": True,
                                }
                            )
            elif prov_status == "tampered":
                # 来源侧车被篡改：fail-closed，绝不替被改过的内容重签
                logger.error(
                    f"[memory_diary] 外部写入被拒绝：主人 {owner} 的来源侧车验签失败"
                )
                return self._note_err(
                    "WRITE_FAILED", "日记完整性校验未通过，请检查日记文件", 500
                )

            # P2-4：删除墓碑——命中说明这段曾写过、且所属日记已被删除。返回与既有
            # 重放同形的响应且绝不写盘，否则桥超时重试同一 ext_id 会把已删段落复活。
            tomb_rec, tomb_status = self.has_tombstone(owner, source, ext_id)
            if tomb_status == "tampered":
                logger.error(
                    f"[memory_diary] 外部写入被拒绝：主人 {owner} 的删除墓碑验签失败"
                )
                return self._note_err(
                    "WRITE_FAILED", "日记完整性校验未通过，请检查日记文件", 500
                )
            if tomb_rec is not None:
                return _web_json(
                    {
                        "ok": True,
                        "day": str(tomb_rec.get("day") or day)[:8],
                        "appended": False,
                        "replayed": True,
                    }
                )

            def _mut(data: dict):
                rec_day = data["entries"].setdefault(f"{day}.md", {})
                ext = rec_day.setdefault("external", [])
                ext.append(
                    {
                        "source": source,
                        "ext_id": ext_id,
                        "at": self._now().strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                )
                data["updated_at"] = self._now().strftime("%Y-%m-%dT%H:%M:%S")
                return data

            # 沿用 QQ 指令侧的同一条落盘路径（_save_diary）：同签名、同
            # provenance、同记忆吸收；分隔条带来源标记，正文里一眼可辨。
            # 昵称从既有 zip 的说明.txt 取出原样保留，不被外部写入冲掉。
            _, nick = self._zip_owner_info(self._diary_path(owner))
            try:
                self._save_diary(
                    owner,
                    text,
                    sender_name=nick or "",
                    entry_name_hint=day,
                    separator_note=f"（外部·{self._ext_display_name(source)}）",
                    origin="",
                    mode="",
                    provenance_mutator=_mut,
                )
            except (
                DiaryTamperedError,
                DiaryCorruptError,
                DiaryKeyUnavailable,
            ) as e:
                logger.error(
                    f"[memory_diary] 外部写入被拒（fail-closed）：主人 {owner}，{e}"
                )
                return self._note_err(
                    "WRITE_FAILED", f"写入失败：{e}", 500
                )
            except Exception as e:
                logger.error(f"[memory_diary] 外部写入异常：主人 {owner}，{e}")
                return self._note_err("WRITE_FAILED", "写入失败，详见后台日志", 500)

        logger.info(
            f"[memory_diary] 外部写入成功：主人 {owner}，来源 {source}，日期 {day}"
        )
        return _web_json({"ok": True, "day": day, "appended": True})
