# -*- coding: utf-8 -*-
"""日记、备忘与搜索共用的作用域规则。"""

from __future__ import annotations

import re
from dataclasses import dataclass


_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class MemoryScope:
    value: str = "private"

    @property
    def is_group(self) -> bool:
        return self.value.startswith("group:")

    @property
    def group_id(self) -> str:
        return self.value[6:] if self.is_group else ""

    def __str__(self) -> str:
        return self.value


def normalize_group_id(group_id) -> str:
    value = str(group_id or "").strip()
    return value if _GROUP_ID_RE.fullmatch(value) else ""


def memory_scope(origin: str = "private", group_id: str = "") -> MemoryScope:
    if str(origin or "") == "group" or str(origin or "").startswith("group:"):
        raw_id = str(origin or "")[6:] if str(origin or "").startswith("group:") else group_id
        if raw_id in ("unknown", "legacy-unknown"):
            return MemoryScope(f"group:{raw_id}")
        normalized = normalize_group_id(raw_id)
        return MemoryScope(f"group:{normalized or 'unknown'}")
    return MemoryScope("private")


def parse_scope(value) -> MemoryScope:
    text = str(value or "private").strip()
    if text == "private":
        return MemoryScope("private")
    if text in ("group", "group:unknown", "group:legacy-unknown"):
        return MemoryScope("group:legacy-unknown" if text == "group" else text)
    if text.startswith("group:"):
        return memory_scope("group", text[6:])
    return MemoryScope("private")


def scope_from_event(event) -> MemoryScope:
    group_id = str(event.get_group_id() or "").strip()
    return memory_scope("group", group_id) if group_id or _event_is_group(event) else MemoryScope("private")


def _event_is_group(event) -> bool:
    try:
        return "GroupMessage" in str(event.unified_msg_origin or "")
    except Exception:
        return False


def parse_group_share_map(raw) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for rule in str(raw or "").split(";"):
        target, separator, sources = rule.partition("=")
        target_id = normalize_group_id(target.strip())
        if not separator or not target_id:
            continue
        allowed = []
        for source in sources.split(","):
            source_id = normalize_group_id(source.strip())
            if source_id and source_id != target_id and source_id not in allowed:
                allowed.append(source_id)
        if allowed:
            result[target_id] = tuple(allowed)
    return result


def readable_scopes(scope, share_map="") -> tuple[str, ...]:
    current = parse_scope(scope).value
    if not current.startswith("group:") or current in (
        "group:unknown", "group:legacy-unknown"
    ):
        return (current,)
    sources = parse_group_share_map(share_map).get(parse_scope(scope).group_id, ())
    return (current,) + tuple(f"group:{source}" for source in sources)
