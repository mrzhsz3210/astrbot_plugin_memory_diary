# -*- coding: utf-8 -*-
"""日记模型输出的解析契约与显式记忆识别；不依赖 AstrBot。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .common import strip_prompt_scaffold


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
_QUOTED_RE = re.compile(r"「[^」]*」|\"[^\"]*\"", re.DOTALL)


def has_diary_anchor(text: str) -> bool:
    return bool(_ANCHOR_RE.search(str(text or "")))


def is_explicit_message(text: str) -> bool:
    """偏召回：只按引号外的常见交代或纠正表述进入显式通道。"""
    unquoted = _QUOTED_RE.sub("", str(text or ""))
    return bool(_EXPLICIT_TRIGGER_RE.search(unquoted))


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
    cleaned = []
    for item in required:
        item = strip_prompt_scaffold(str(item)).strip()
        if item:
            cleaned.append(item)
    required = cleaned
    if not required:
        return result
    if result.status == "no_diary":
        result = DiaryResult(status="ok", text="")
    notes = list(result.explicit_notes)
    text = result.text
    for item in required:
        if any(item in note or note in item for note in notes):
            continue
        text += ("\n\n" if text else "") + "【显式交代】\n" + item
        notes.append(item)
    result.text = text
    result.explicit_notes = notes
    if result.status in ("retry", "degraded"):
        result.status = "ok"
    return result


# ---------------------------------------------------------------------------
# 思维链泄漏兜底（v0.7.6，Bug A）
#
# 推理模型（Intern-S2 等）的产出可能把英文思维链混进 content——端点把
# reasoning 拼进正文、或模型干脆把"思考过程"当日记写。上游（AstrBot 非
# 流式路径）只剥 <think> 标签，挡不住无标签的拼接，插件侧必须有
# 不依赖上游的兜底：先剥标签块，再做特征识别，命中则重写/降级。
# ---------------------------------------------------------------------------

# 闭合的 <think>/<thinking> 块；未闭合的从标签处吃到文末（模型被截断的场景）
_REASONING_TAG_BLOCK_RE = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?\s*>", re.IGNORECASE | re.DOTALL
)
_REASONING_TAG_TAIL_RE = re.compile(
    r"<think(?:ing)?>.*$", re.IGNORECASE | re.DOTALL
)

# 英文推理句式的行首特征（保守清单，宁可漏判不要误伤正常日记）
_REASONING_LINE_RE = re.compile(
    r"^\s*(?:"
    r"Okay(?:,| so| let)|Ok(?:,| so)|Let me|Let's|I need to|I should|I will|I'll|"
    r"I’m|I'm |The user|Wait[, ]|Hmm|Alright|Thinking|Step \d|First[, ]|"
    r"So (?:the|I|let)|Now (?:I|the|let)|Actually[, ]|Looking at|Based on"
    r")",
    re.IGNORECASE,
)

_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")


def strip_reasoning_blocks(text: str) -> str:
    """剥离 <think>/<thinking> 标签块（含未闭合尾巴）；无标签时原文返回。"""
    cur = str(text or "")
    if "<think" not in cur.lower():
        return cur
    cur = _REASONING_TAG_BLOCK_RE.sub("", cur)
    return _REASONING_TAG_TAIL_RE.sub("", cur)


def _cjk_ratio(text: str) -> float:
    chars = [c for c in str(text or "") if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if _CJK_RE.match(c)) / len(chars)


def looks_like_reasoning_leak(text: str) -> bool:
    """文本是否疑似混入英文思维链：推理句式行占比超阈值且达到一定行数。"""
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    hits = sum(1 for ln in lines if _REASONING_LINE_RE.match(ln))
    return hits >= 3 and hits / len(lines) >= 0.25


def language_mismatch(text: str, reference: str) -> bool:
    """语料为中文而产出主体为英文（思维链泄漏的典型伴随特征）。

    reference 为空或本身也不含中文时不判（没有可对齐的基准）；
    产出很短时不判（简记本来就可能很短，误判代价高）。
    """
    text = str(text or "")
    if len(text) < 120 or _cjk_ratio(reference) < 0.2:
        return False
    return _cjk_ratio(text) < 0.1
