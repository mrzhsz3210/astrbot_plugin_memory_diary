# -*- coding: utf-8 -*-
"""日记模型输出的解析契约与显式记忆识别；不依赖 AstrBot。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class DiaryResult:
    status: str
    text: str
    explicit_notes: list[str] = field(default_factory=list)
    reason: str = ""
    rewrite_self: bool = False


_EXPLICIT_RE = re.compile(r"<explicit\b[^>]*>(.*?)</explicit\s*>", re.IGNORECASE | re.DOTALL)
_ANCHOR_RE = re.compile(
    r"<explicit\b|「[^」]+」|“[^”]+”|\"[^\"]+\"|‘[^’]+’|"
    r"\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}|"
    r"\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}\s*(?:点|时)[:：半]?\d*|"
    r"今天|昨天|明天|上周|本周|周[一二三四五六日天]|"
    r"参加|去了|完成|发布|考试|会议|讨论|决定|约定|遇到|发生"
)
_EXPLICIT_TRIGGER_RE = re.compile(
    r"记住|记得|别忘|不要忘记|以后注意|你记错了|"
    r"不对\s*[，,：:]?\s*是|其实不是|应该是|纠正"
)


def has_diary_anchor(text: str) -> bool:
    return bool(_ANCHOR_RE.search(str(text or "")))


def is_explicit_message(text: str) -> bool:
    """偏召回：命中常见交代或纠正表述即进入显式通道。"""
    return bool(_EXPLICIT_TRIGGER_RE.search(str(text or "")))


def parse_diary_result(
    content: str, *, require_anchor: bool = False, second_attempt: bool = False
) -> DiaryResult:
    text = str(content or "").strip()
    rewrite_match = re.search(
        r"<rewrite_self>\s*(true|false)\s*</rewrite_self\s*>",
        text,
        re.IGNORECASE,
    )
    rewrite_self = bool(
        rewrite_match and rewrite_match.group(1).lower() == "true"
    )
    text = re.sub(
        r"<rewrite_self>\s*(?:true|false)\s*</rewrite_self\s*>",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    no_diary = re.fullmatch(r"【无日记】[ \t]*([^\n]{0,30})", text)
    if no_diary and not re.search(r"</?self\s*>", text, re.IGNORECASE):
        return DiaryResult(
            status="no_diary", text="", reason=no_diary.group(1).strip()
        )

    explicit_notes = []

    def _extract(match):
        explicit_notes.append(match.group(1))
        return match.group(1)

    clean = _EXPLICIT_RE.sub(_extract, text)
    clean = re.sub(r"</?explicit\b[^>]*>", "", clean, flags=re.IGNORECASE)
    if require_anchor and not has_diary_anchor(text):
        return DiaryResult(
            status="degraded" if second_attempt else "retry",
            text=clean,
            explicit_notes=explicit_notes,
            reason="missing_anchor",
            rewrite_self=rewrite_self,
        )
    return DiaryResult(
        status="ok",
        text=clean,
        explicit_notes=explicit_notes,
        rewrite_self=rewrite_self,
    )


def ensure_explicit_notes(result: DiaryResult, required: list[str]) -> DiaryResult:
    """模型漏包或误判“不写”时，由确定性通道补齐用户原话。"""
    required = [str(item) for item in required if str(item)]
    if not required:
        return result
    if result.status == "no_diary":
        result = DiaryResult(status="ok", text="")
    notes = list(result.explicit_notes)
    text = result.text
    for item in required:
        if any(item in note for note in notes):
            continue
        text += ("\n\n" if text else "") + "【显式交代】\n" + item
        notes.append(item)
    result.text = text
    result.explicit_notes = notes
    if result.status in ("retry", "degraded"):
        result.status = "ok"
    return result
