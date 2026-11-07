# -*- coding: utf-8 -*-
"""验签原文的按需本地关键词搜索；不创建持久化索引。"""

from __future__ import annotations

import re
import unicodedata

from .constants import ANNOTATION_SELF_PLACEHOLDER
from .scope import readable_scopes

EVIDENCE_LABELS = {
    "user": "用户相关记录",
    "self": "AI 私人随笔",
    "mixed": "混合段落",
    "external": "外部素材",
    "unknown": "未分级（旧记录）",
}
EVIDENCE_TYPES = frozenset((*EVIDENCE_LABELS, "all"))
USER_EVIDENCE_TYPES = frozenset(("user", "mixed", "external", "unknown"))


def normalize_evidence_type(value) -> str:
    evidence_type = str(value or "user").strip().lower()
    return evidence_type if evidence_type in EVIDENCE_TYPES else "user"


def _normalized_with_map(text: str):
    chars = []
    indexes = []
    for index, char in enumerate(str(text or "")):
        normalized = unicodedata.normalize("NFKC", char).casefold()
        chars.append(normalized)
        indexes.extend([index] * len(normalized))
    return "".join(chars), indexes


def normalize_search_text(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def evidence_snippet(text: str, keywords: list[str], limit: int = 240) -> str:
    normalized, indexes = _normalized_with_map(text)
    positions = [normalized.find(keyword) for keyword in keywords if keyword]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return str(text or "")[:limit]
    start_normalized = min(positions)
    start = indexes[start_normalized] if start_normalized < len(indexes) else 0
    half = max(20, limit // 3)
    left = max(0, start - half)
    right = min(len(text), left + limit)
    left = max(0, right - limit)
    return ("…" if left else "") + text[left:right].strip() + ("…" if right < len(text) else "")


def classify_search_segment(
    raw_text: str,
    *,
    mode: str = "",
    header: str = "",
    provenance: dict | None = None,
    source_status: str = "ok",
    evidence_type: str = "user",
) -> dict:
    raw_text = str(raw_text or "")
    requested = normalize_evidence_type(evidence_type)
    status = str(source_status or "missing")
    record = provenance if isinstance(provenance, dict) else {}
    spans = record.get("self") if status in ("ok", "partial") else []
    if not isinstance(spans, list):
        spans = []
    matched = [str(span) for span in spans if str(span or "") and str(span) in raw_text]
    display_text = raw_text
    for span in matched:
        display_text = display_text.replace(span, ANNOTATION_SELF_PLACEHOLDER)
    self_masked = display_text != raw_text

    if status in ("tampered", "no_key"):
        classified, status = "unknown", "tampered"
    elif status == "partial":
        classified = "unknown"
    elif mode == "life":
        classified = "self"
    elif spans and not matched:
        classified, status = "unknown", "partial"
    elif "外部" in str(header or "") or mode == "external":
        classified = "external"
    elif matched:
        classified = "mixed"
    elif mode == "legacy":
        classified, status = "unknown", "missing"
    elif status == "ok":
        classified = "user"
    else:
        classified, status = "unknown", "missing"

    search_text = (
        raw_text
        if requested in ("self", "all") and classified in ("self", "mixed")
        else display_text
    )
    explicit = record.get("explicit") if status in ("ok", "partial") else []
    has_explicit = isinstance(explicit, list) and any(
        str(note or "") and str(note) in raw_text for note in explicit
    )
    return {
        "raw_text": raw_text,
        "search_text": search_text,
        "display_text": display_text,
        "evidence_type": classified,
        "source_status": status,
        "has_explicit": has_explicit,
        "self_masked": self_masked,
    }


def _evidence_selected(classified: str, requested: str) -> bool:
    if requested == "all":
        return True
    if requested == "user":
        return classified in USER_EVIDENCE_TYPES
    if requested == "self":
        return classified in ("self", "mixed")
    return classified == requested


def search_diaries(
    plugin,
    sender_id: str,
    query: str = "",
    *,
    scope: str = "",
    day: str = "",
    mode: str = "",
    origin: str = "",
    has_annotations=None,
    has_images=None,
    offset: int = 0,
    limit: int = 20,
    evidence_type: str = "user",
):
    """返回统一证据结果；scope 为空仅供已鉴权的管理端跨场景检索。"""
    if not plugin._is_allowed(sender_id) or not bool(plugin.config.get("local_search_enabled", True)):
        return {"results": [], "total": 0, "tampered": False, "disabled": True}
    requested = normalize_evidence_type(evidence_type)
    keywords = [normalize_search_text(part) for part in str(query or "").split() if part]
    allowed = set(readable_scopes(scope, plugin.config.get("group_share_map", ""))) if scope else None
    annotations, ann_status = plugin.load_annotations(sender_id)
    annotated_days = {
        str(item.get("entry") or "")[:8]
        for item in (annotations.get("annotations") or [])
        if isinstance(item, dict)
    } if ann_status in ("ok", "missing") else set()
    try:
        provenance, provenance_status = plugin.load_provenance(sender_id)
    except Exception:
        provenance, provenance_status = {"entries": {}}, "tampered"
    provenance_entries = (
        provenance.get("entries") if provenance_status == "ok" else {}
    )
    results = []
    tampered_any = ann_status == "tampered" or provenance_status == "tampered"
    for entry in reversed(plugin._list_entries(sender_id)):
        entry_day = plugin._entry_day(entry)
        if day and entry_day != str(day).replace("-", ""):
            continue
        content, tampered = plugin._load_entry(sender_id, entry)
        if tampered:
            tampered_any = True
            continue
        record = provenance_entries.get(str(entry).split("/")[-1], {})
        day_spans = record.get("self") or []
        segments = plugin._segments(content)
        segment_spans = [
            [span for span in day_spans if str(span or "") and span in segment["text"]]
            for segment in segments
        ]
        matched_spans = {span for spans in segment_spans for span in spans}
        unmatched_spans = [span for span in day_spans if span not in matched_spans]
        for segment, local_spans in zip(segments, segment_spans):
            info = plugin._segment_info(segment["header"])
            if allowed is not None and info["scope"] not in allowed:
                continue
            segment_mode = info["mode"]
            header = segment["header"] or ""
            if "外部" in header:
                segment_mode = "external"
            elif not header:
                segment_mode = "legacy"
            local_record = dict(record)
            local_record["self"] = local_spans if matched_spans else day_spans
            source_status = provenance_status
            if unmatched_spans and (not matched_spans or local_spans):
                source_status = "partial"
            prepared = classify_search_segment(
                segment["text"],
                mode=segment_mode,
                header=header,
                provenance=local_record,
                source_status=source_status,
                evidence_type=requested,
            )
            if not _evidence_selected(prepared["evidence_type"], requested):
                continue
            if mode and segment_mode != mode:
                continue
            if origin and info["origin"] != origin and info["scope"] != origin:
                continue
            images = bool(re.search(r"!\[[^\]]*\]\(images/[^)]+\)", segment["text"]))
            annotated = entry_day in annotated_days
            if has_annotations is not None and annotated != bool(has_annotations):
                continue
            if has_images is not None and images != bool(has_images):
                continue
            normalized = normalize_search_text(prepared["search_text"])
            if not all(keyword in normalized for keyword in keywords):
                continue
            source = "私聊" if info["origin"] == "private" else info["scope"]
            external = re.search(r"外部·([^）]+)", header)
            if external:
                source = external.group(1)
            snippet_keywords = keywords
            if prepared["self_masked"]:
                display_normalized = normalize_search_text(prepared["display_text"])
                if any(keyword not in display_normalized for keyword in keywords):
                    snippet_keywords = [normalize_search_text(ANNOTATION_SELF_PLACEHOLDER)]
            results.append({
                "entry": entry,
                "day": entry_day,
                "segment_header": header,
                "scope": info["scope"],
                "mode": segment_mode,
                "source": source,
                "snippet": evidence_snippet(prepared["display_text"], snippet_keywords),
                "has_annotations": annotated,
                "has_images": images,
                "evidence_type": prepared["evidence_type"],
                "evidence_label": EVIDENCE_LABELS[prepared["evidence_type"]],
                "has_explicit": prepared["has_explicit"],
                "self_masked": prepared["self_masked"],
                "source_status": prepared["source_status"],
            })
    total = len(results)
    start = max(0, int(offset or 0))
    size = min(100, max(1, int(limit or 20)))
    return {
        "results": results[start:start + size],
        "total": total,
        "tampered": tampered_any,
        "disabled": False,
    }
