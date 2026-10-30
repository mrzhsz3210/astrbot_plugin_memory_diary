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
