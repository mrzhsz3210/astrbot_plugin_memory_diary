# -*- coding: utf-8 -*-
"""外部素材 v2 入队接口与管理员工具。"""

from __future__ import annotations

from ..memory_diary.log import logger
from .compat import _web_error, _web_json, _web_request


class WebMaterialApiMixin:
    async def web_external_material(self):
        payload = await _web_request.json(default=None)
        if not isinstance(payload, dict):
            return self._note_err("BAD_REQUEST", "请求体必须是 JSON 对象", 400)
        token = str(payload.get("token") or "").strip()
        rec = self.resolve_external_token(token)
        if rec is None:
            owner = self.external_token_owner(token)
            if owner and not self._is_allowed(owner):
                return self._note_err("OWNER_NOT_ALLOWED", "该用户不在插件白名单内", 403)
            return self._note_err("UNAUTHORIZED", "凭证缺失或无效", 401)
        owner = str(rec.get("owner") or "")
        source = str(payload.get("source") or "").strip()
        if not source or source != str(rec.get("source") or ""):
            return self._note_err("SOURCE_NOT_ALLOWED", "source 不在该凭证的白名单内", 403)
        if not self._ext_rate_ok(self._external_token_hash(token)):
            return self._note_err("RATE_LIMITED", "请求过于频繁，请稍后再试", 429)
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return self._note_err("BAD_REQUEST", "text 缺失或为空", 400)
        text = self._break_separator_lines(text.strip())
        if len(text.encode("utf-8")) > self.EXTERNAL_NOTE_MAX_BYTES:
            return self._note_err("PAYLOAD_TOO_LARGE", "text 超过 8 KB 上限", 413)
        ext_id = payload.get("ext_id")
        ext_id = ext_id.strip() if isinstance(ext_id, str) else ""
        if not self._EXT_ID_RE.fullmatch(ext_id):
            return self._note_err("BAD_REQUEST", "ext_id 缺失或非法", 400)
        scope = str(rec.get("scope") or "private")
        lock = self._ext_locks().setdefault(owner, self._new_ext_lock())
        async with lock:
            item, status = self.enqueue_external_material(owner, source, ext_id, text, scope)
        if status == "full":
            return self._note_err("QUEUE_FULL", "待处理素材已达 100 条上限", 429)
        if status == "too_large":
            return self._note_err(
                "PAYLOAD_TOO_LARGE", "text 超过单条素材 2600 token 的可处理上限", 413
            )
        if item is None:
            logger.error(f"[memory_diary] 外部素材入队失败：主人 {owner}，状态 {status}")
            return self._note_err("WRITE_FAILED", "素材完整性校验或写入失败", 500)
        return _web_json({
            "ok": True,
            "queued": item["status"] in ("pending", "deferred"),
            "status": item["status"],
            "id": item["id"],
            "replayed": status in ("replayed", "tombstone"),
        })

    @staticmethod
    def _new_ext_lock():
        import asyncio
        return asyncio.Lock()

    async def web_list_materials(self, file_key: str):
        if not self._valid_file_key(file_key):
            return _web_error("无效的用户标识", status_code=400)
        path = self.data_dir / f"diary_{file_key}.zip"
        owner, _nick = self._zip_owner_info(path)
        if not owner:
            owner = self._load_owner_map().get(file_key, "")
        denied = self._check_web_password(owner, file_key)
        if denied is not None:
            return denied
        data, status = self.load_external_materials(owner)
        if status not in ("ok", "missing"):
            return _web_error("素材队列验签失败", status_code=500)
        selected = str(_web_request.query.get("status") or "")
        items = [item for item in data["items"] if not selected or item["status"] == selected]
        return _web_json({"items": list(reversed(items)), "status": status})

    async def web_update_material(self, file_key: str):
        if not self._valid_file_key(file_key):
            return _web_error("无效的用户标识", status_code=400)
        path = self.data_dir / f"diary_{file_key}.zip"
        owner, _nick = self._zip_owner_info(path)
        if not owner:
            owner = self._load_owner_map().get(file_key, "")
        payload = await _web_request.json(default={})
        if not isinstance(payload, dict):
            return _web_error("无效的请求内容", status_code=400)
        denied = self._check_web_password(owner, file_key, tk=payload.get("tk"))
        if denied is not None:
            return denied
        ok = self.set_external_material_status(
            owner, str(payload.get("id") or ""), str(payload.get("action") or "")
        )
        if not ok:
            return _web_error("素材不存在或该状态不允许此操作", status_code=400)
        return _web_json({"ok": True})
