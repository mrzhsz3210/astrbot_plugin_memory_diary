# -*- coding: utf-8 -*-
"""上下文 token 估算器（Phase 1 静态估算）。

纯函数、零依赖：不 import AstrBot，ProviderRequest 按 duck-typing 取属性，
两个入口共用同一个核心，保证"同输入必同输出"：

- ``estimate_request(req)``：接受 ProviderRequest 形状——计入 system_prompt、
  prompt、contexts、func_tool、image_urls、audio_urls、
  extra_user_content_parts、tool_calls_result；
- ``estimate_messages(messages, system_prompt="")``：接受裸消息列表
  （供 /日记状态 与手动写日记门槛复用，解决多处估算不一致的问题）。

估算公式：

    tokens ≈ CJK字符 × 0.8
           + ASCII字符 × 0.25
           + 其他字符 × 0.33
           + 消息条数 × 3（role 包装与特殊 token 开销）
           + 工具schema字符数 ÷ 4
           + 图片数 × IMAGE_TOKEN_ESTIMATE（保守值）
           + 音频数 × AUDIO_TOKEN_ESTIMATE

权重集中在 constants.py；旧公式（字符数 ÷ CHARS_PER_TOKEN）保留在调用方做回退。
"""

from .constants import (
    ASCII_WEIGHT,
    AUDIO_TOKEN_ESTIMATE,
    CJK_WEIGHT,
    IMAGE_TOKEN_ESTIMATE,
    MSG_OVERHEAD_TOKENS,
    OTHER_WEIGHT,
    TOOL_SCHEMA_CHARS_PER_TOKEN,
)


def _is_cjk(o: int) -> bool:
    return (
        0x4E00 <= o <= 0x9FFF  # CJK 统一表意文字
        or 0x3400 <= o <= 0x4DBF  # 扩展 A
        or 0x20000 <= o <= 0x2A6DF  # 扩展 B
        or 0x3000 <= o <= 0x303F  # CJK 标点
        or 0xFF00 <= o <= 0xFFEF  # 全角字符
        or 0x3040 <= o <= 0x30FF  # 日文假名
        or 0xAC00 <= o <= 0xD7AF  # 韩文音节
    )


def estimate_text(text) -> int:
    """按字符类别加权估算一段文本的 token 数。"""
    if not text:
        return 0
    cjk = ascii_ = other = 0
    for ch in str(text):
        o = ord(ch)
        if o < 128:
            ascii_ += 1
        elif _is_cjk(o):
            cjk += 1
        else:
            other += 1
    return int(cjk * CJK_WEIGHT + ascii_ * ASCII_WEIGHT + other * OTHER_WEIGHT)


def _content_text(content) -> str:
    """兼容字符串与多模态 content 列表（与 service._content_to_text 同规则）。"""
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return str(content or "")


def _content_image_count(content) -> int:
    """多模态 content 列表里内嵌的图片数。"""
    if not isinstance(content, list):
        return 0
    return sum(
        1
        for part in content
        if isinstance(part, dict)
        and part.get("type") in ("image_url", "image", "input_image")
    )


def _part_image_count(part) -> int:
    """``extra_user_content_parts`` 里单个 part 携带的图片数（dict / 对象都兼容）。

    P3-9：过去这里只统计文本，内嵌图片（ImagePart 或多模态 content 列表）
    完全没算，会让带图请求被系统性低估。与 ``_content_image_count`` 同口径：
    图片列表里的每一项 + 图片类型的 part 本身各算一张。
    """
    if isinstance(part, dict):
        content = part.get("content")
        part_type = part.get("type")
    else:
        content = getattr(part, "content", None)
        part_type = getattr(part, "type", None)
    count = _content_image_count(content)
    if part_type in ("image_url", "image", "input_image"):
        count += 1
    return count


def estimate_messages(messages, system_prompt: str = "") -> int:
    """估算裸消息列表（OpenAI 格式 dict 列表）的 token 数。"""
    total = estimate_text(system_prompt)
    images = 0
    for msg in messages or []:
        if isinstance(msg, dict):
            content = msg.get("content", "")
            total += estimate_text(_content_text(content))
            images += _content_image_count(content)
        else:
            total += estimate_text(msg)
        total += MSG_OVERHEAD_TOKENS
    return total + images * IMAGE_TOKEN_ESTIMATE


def _tool_schema_chars(func_tool) -> int:
    """工具集合的 schema 字符数。duck-typed：优先 pydantic dump，回退 str()。"""
    try:
        tools = getattr(func_tool, "tools", None)
        if isinstance(tools, list):
            total = 0
            for tool in tools:
                dump = getattr(tool, "model_dump", None)
                if callable(dump):
                    total += len(str(dump()))
                else:
                    total += len(str(tool))
            return total
        return len(str(func_tool))
    except Exception:
        return 0


def estimate_request(req) -> int:
    """估算一次 ProviderRequest 携带的上下文 token 数。"""
    total = estimate_text(getattr(req, "system_prompt", "") or "")
    total += estimate_text(getattr(req, "prompt", "") or "")
    total += estimate_messages(getattr(req, "contexts", None) or [])

    images = len(getattr(req, "image_urls", None) or [])
    audios = len(getattr(req, "audio_urls", None) or [])

    for part in getattr(req, "extra_user_content_parts", None) or []:
        if isinstance(part, dict):
            total += estimate_text(part.get("text", ""))
        else:
            total += estimate_text(getattr(part, "text", "") or "")
        images += _part_image_count(part)

    func_tool = getattr(req, "func_tool", None)
    if func_tool is not None:
        total += _tool_schema_chars(func_tool) // TOOL_SCHEMA_CHARS_PER_TOKEN

    tool_calls_result = getattr(req, "tool_calls_result", None)
    if tool_calls_result:
        total += estimate_text(str(tool_calls_result))

    return total + images * IMAGE_TOKEN_ESTIMATE + audios * AUDIO_TOKEN_ESTIMATE
