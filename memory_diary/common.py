# -*- coding: utf-8 -*-
"""共享小工具：文件名 key。"""

import hashlib


def safe_key(raw: str) -> str:
    """把 sender_id 转成安全的文件名 key（旧版命名，仅兼容迁移）。"""
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
