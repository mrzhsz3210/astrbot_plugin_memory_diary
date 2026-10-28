# -*- coding: utf-8 -*-
"""日志兼容层：有 AstrBot 时使用其插件 logger；脱离 AstrBot 时静默丢弃。

AstrBot 插件规范要求统一使用 ``astrbot.api.logger``，**禁止使用标准库 logging**，
因此本模块不导入 logging。只有在无 AstrBot 的环境里直接 import
``memory_diary.core`` 时才会走到静默兜底（为了保留核心包可独立测试/复用的能力），
AstrBot 运行时不会进入该分支。
"""

try:  # pragma: no cover - 取决于运行环境
    from astrbot.api import logger
except ImportError:  # pragma: no cover

    class _SilentLogger:
        """无 AstrBot 环境下的空 logger，只实现插件实际用到的几个方法。"""

        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

        def exception(self, *args, **kwargs):
            pass

    logger = _SilentLogger()
