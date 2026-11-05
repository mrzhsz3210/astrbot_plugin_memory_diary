# -*- coding: utf-8 -*-
"""AstrBot 可选能力的兼容导入（不同 AstrBot 版本缺哪个就降级哪个）。

适配层各 mixin 统一从这里取这些名字，避免每个模块各写一遍 try/except。
"""

from pathlib import Path

from ..memory_diary.constants import PLUGIN_NAME

try:
    from astrbot.api.message_components import At
except ImportError:  # pragma: no cover
    At = None

try:
    # v0.6 插图：QQ 端图文穿插发送（老版本 AstrBot 没有 Image 时降级为占位文本）
    from astrbot.api.message_components import Image as _ImageComponent
except ImportError:  # pragma: no cover
    _ImageComponent = None

try:
    from astrbot.api.star import StarTools  # v3.5+
except ImportError:  # pragma: no cover
    StarTools = None

try:
    from astrbot.core.agent.message import TextPart  # v4.x
except ImportError:  # pragma: no cover
    TextPart = None

try:
    from astrbot.core.utils.session_waiter import SessionController, session_waiter

    _SESSION_WAITER_OK = True
except ImportError:  # pragma: no cover
    SessionController = None
    session_waiter = None
    _SESSION_WAITER_OK = False

try:
    # 插件可视化视图后端（AstrBot 较新版本提供；旧版本自动降级为无页面）
    from astrbot.api.web import (
        PluginUploadFile as _PluginUploadFile,
    )
    from astrbot.api.web import (
        error_response as _web_error,
    )
    from astrbot.api.web import (
        file_response as _web_file,
    )
    from astrbot.api.web import (
        json_response as _web_json,
    )
    from astrbot.api.web import (
        request as _web_request,
    )

    _WEB_API_OK = True
except ImportError:  # pragma: no cover
    _PluginUploadFile = None
    _web_error = _web_file = _web_json = _web_request = None
    _WEB_API_OK = False


def resolve_data_dir() -> Path:
    """数据目录：<AstrBot>/data/plugin_data/astrbot_plugin_memory_diary/。"""
    try:
        if StarTools is not None:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:
        pass
    return Path("data/plugin_data") / PLUGIN_NAME
