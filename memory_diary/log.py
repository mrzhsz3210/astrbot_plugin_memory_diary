# -*- coding: utf-8 -*-
"""日志兼容层：有 AstrBot 时使用其插件日志，无 AstrBot 时退回标准库。

这样 memory_diary 核心包可以脱离 AstrBot 直接被测试/复用。
"""

try:  # pragma: no cover - 取决于运行环境
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_memory_diary")
