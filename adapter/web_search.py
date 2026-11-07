# -*- coding: utf-8 -*-
"""日记本时间轴、筛选与本地证据搜索 API。"""

from __future__ import annotations

from ..memory_diary.search import EVIDENCE_TYPES, search_diaries
from .compat import _web_error, _web_json, _web_request


class WebSearchApiMixin:
    async def web_search_diaries(self, file_key: str):
        if not self._valid_file_key(file_key):
            return _web_error("无效的用户标识", status_code=400)
        if not bool(self.config.get("local_search_enabled", True)):
            return _web_error("本地搜索已关闭", status_code=403)
        path = self.data_dir / f"diary_{file_key}.zip"
        owner, _nick = self._zip_owner_info(path)
        if not owner:
            owner = self._load_owner_map().get(file_key, "")
        denied = self._check_web_password(owner, file_key)
        if denied is not None:
            return denied
        query = _web_request.query
        evidence_type = str(query.get("evidence_type") or "user").strip().lower()
        if evidence_type not in EVIDENCE_TYPES:
            return _web_error("无效的证据类型", status_code=400)
        tri_state = {}
        for key in ("has_annotations", "has_images"):
            value = str(query.get(key) or "").lower()
            if value in ("true", "1"):
                tri_state[key] = True
            elif value in ("false", "0"):
                tri_state[key] = False
        try:
            offset = max(0, int(query.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        result = search_diaries(
            self,
            owner,
            str(query.get("q") or ""),
            scope=str(query.get("scope") or ""),
            day=str(query.get("day") or ""),
            mode=str(query.get("mode") or ""),
            origin=str(query.get("origin") or ""),
            offset=offset,
            limit=20,
            evidence_type=evidence_type,
            **tri_state,
        )
        result["page_size"] = 20
        result["next_offset"] = offset + 20 if offset + 20 < result["total"] else None
        return _web_json(result)
