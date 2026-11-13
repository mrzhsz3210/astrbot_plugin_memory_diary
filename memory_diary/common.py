# -*- coding: utf-8 -*-
"""共享小工具：文件名 key、不可见字符、提示脚手架与尾部机器标记剥离。"""

import hashlib
import re

# 零宽与方向控制字符：其他插件常在消息末尾追加不可见标记，
# 不剥掉会把指令参数/白名单判成"不认识"（日志里用 repr 才能看到它们）。
# 只影响解析判断，不回写消息原文。
INVISIBLE_CHARS_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")


def strip_invisible(text: str) -> str:
    """剥离零宽与方向控制字符。"""
    return INVISIBLE_CHARS_RE.sub("", text or "")


_SCAFFOLD_HINT_RE = re.compile(
    r"<system[_-]reminder\b|\[你上一条回复是念成语音发出去的|"
    r"User\s+ID\s*:|Current\s+datetime\s*:",
    re.IGNORECASE,
)
_SYSTEM_REMINDER_RE = re.compile(
    r"<system[_-]reminder\b[^>]*>.*?</system[_-]reminder\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SYSTEM_REMINDER_UNCLOSED_RE = re.compile(
    r"<system[_-]reminder\b[^\r\n]*", re.IGNORECASE
)
_VOICE_RECEIPT_RE = re.compile(
    r"\[你上一条回复是念成语音发出去的.*?\]", re.DOTALL
)
_VOICE_RECEIPT_UNCLOSED_RE = re.compile(
    r"\[你上一条回复是念成语音发出去的[^\]\r\n]*"
)
_USER_ID_FRAGMENT_RE = re.compile(
    r"User\s+ID\s*:\s*\d+(?:\s*,\s*Nickname\s*:[^,\r\n<]*)?",
    re.IGNORECASE,
)
_CURRENT_DATETIME_FRAGMENT_RE = re.compile(
    r"Current\s+datetime\s*:[^\r\n<]*", re.IGNORECASE
)


def strip_prompt_scaffold(text: str) -> str:
    """剥离固定形状的提示脚手架，保留用户真实发言。"""
    cur = text or ""
    if not _SCAFFOLD_HINT_RE.search(cur):
        return cur
    cur = _SYSTEM_REMINDER_RE.sub("", cur)
    cur = _SYSTEM_REMINDER_UNCLOSED_RE.sub("", cur)
    cur = _VOICE_RECEIPT_RE.sub("", cur)
    cur = _VOICE_RECEIPT_UNCLOSED_RE.sub("", cur)
    cur = _USER_ID_FRAGMENT_RE.sub("", cur)
    return _CURRENT_DATETIME_FRAGMENT_RE.sub("", cur)


# 尾部装饰剥离：其他插件（如 astrbot_plugin_qq_tools）会往消息文本尾部追加
# "[MSG_ID:123]" 之类机器标记；/reset 是精确匹配，带上这类后缀就认不出来
# （issue #2：reset 标记没置位 → 新一轮跳过自动注入）。
# 只用于判断，绝不回写消息原文（与 strip_invisible 同一条纪律）。
_TRAILING_DECOR_RE = re.compile(r"\s*\[[A-Za-z_][A-Za-z0-9_]*:[^\[\]]*\]\s*$")


def strip_trailing_decorations(text: str) -> str:
    """反复剥掉尾部 [XXX:...] 机器标记（如 [MSG_ID:-2037476869]）。

    可能连着追加多个标记，所以循环剥到不再变化为止；中间的标记不动
    （只处理"尾部"，避免误伤正文里合法出现的方括号内容）。
    """
    prev = None
    cur = text or ""
    while cur != prev:
        prev = cur
        cur = _TRAILING_DECOR_RE.sub("", cur)
    return cur


def safe_key(raw: str) -> str:
    """把 sender_id 转成安全的文件名 key（旧版命名，仅兼容迁移）。"""
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


# ---- 日记身份（v0.7.7 人格隔离）----
# 存储维度的「身份」= 原始 sender_id，或在人格隔离生效时为
# ``<sender_id>::p::<persona_id>``。身份字符串贯穿文件 key、派生签名钥匙与
# 条目签名域，因此同一人格的数据天然聚在同一本日记里、不同人格互不可见。
# 默认人格与取不到人格的兜底都映射回原始 sender_id（即 v0.7.5 的旧文件），
# 老用户升级零迁移、签名不炸。
PERSONA_ID_SEP = "::p::"
# 默认人格的稳定标识：不进入身份字符串（默认人格直接用原始 sender_id），
# 只作为「这是默认人格」的语义标记在解析结果里传递。
DEFAULT_PERSONA_ID = "__default__"

# persona_id 只来自 AstrBot 会话/人格库，但仍按"要进 README 单行与日志"的
# 口径消毒：去掉空白与控制字符（防 README 的「用户ID：」行被换行撑破）、
# 限长 64。人格改名在 AstrBot 里等于新人格（v3 按名字解析），与这里的
# 身份口径一致——改名后旧人格的日记留在旧身份名下，不会串数据。
_PERSONA_ID_STRIP_RE = re.compile(r"[\s\x00-\x1f\x7f]+")


def sanitize_persona_id(raw) -> str:
    """把 AstrBot 的 persona_id/人格名消毒成可进身份字符串的形态；空串表示不可用。"""
    text = _PERSONA_ID_STRIP_RE.sub("", str(raw or ""))
    return text[:64]


def make_diary_identity(sender_id, persona_id) -> str:
    """合成存储身份；persona_id 为空（含默认人格）时就是原始 sender_id。"""
    base = str(sender_id or "")
    pid = sanitize_persona_id(persona_id)
    if not base or not pid or pid == DEFAULT_PERSONA_ID:
        return base
    return f"{base}{PERSONA_ID_SEP}{pid}"


def split_diary_identity(identity) -> tuple:
    """拆存储身份 → (原始 sender_id, persona_id)；无人格维度时 persona_id 为 ''。"""
    text = str(identity or "")
    base, sep, pid = text.partition(PERSONA_ID_SEP)
    if not sep:
        return text, ""
    return base, pid


# ---- 图片字节嗅探（纯函数，storage / imaging 共用，避免两套判断口径）----
# 只看魔数，不信扩展名与 Content-Type：生图服务返回的格式经常与请求的不一致。
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)


def sniff_image_ext(data: bytes) -> str:
    """按魔数判断图片扩展名（png/jpg/webp/gif）；认不出时回退 "png"。"""
    data = bytes(data or b"")
    for magic, ext, _mime in _IMAGE_SIGNATURES:
        if data.startswith(magic):
            return ext
    # WebP: "RIFF"+4 字节长度+"WEBP"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "png"


def sniff_image_mime(data: bytes) -> str:
    """按魔数判断图片 MIME；认不出时回退 image/png。"""
    data = bytes(data or b"")
    for _magic, _ext, mime in _IMAGE_SIGNATURES:
        if data.startswith(_magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"
