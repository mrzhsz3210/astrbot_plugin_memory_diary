# -*- coding: utf-8 -*-
"""共享小工具：文件名 key、不可见字符剥离。"""

import hashlib
import re

# 零宽与方向控制字符：其他插件常在消息末尾追加不可见标记，
# 不剥掉会把指令参数/白名单判成"不认识"（日志里用 repr 才能看到它们）。
# 只影响解析判断，不回写消息原文。
INVISIBLE_CHARS_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")


def strip_invisible(text: str) -> str:
    """剥离零宽与方向控制字符。"""
    return INVISIBLE_CHARS_RE.sub("", text or "")


def safe_key(raw: str) -> str:
    """把 sender_id 转成安全的文件名 key（旧版命名，仅兼容迁移）。"""
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


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
