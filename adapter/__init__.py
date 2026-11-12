# -*- coding: utf-8 -*-
"""AstrBot 适配层：指令 / 事件钩子 / Web 日记本 API 的实现。

和 ``memory_diary``（领域核心，不 import astrbot）不同，这一层可以依赖 AstrBot；
每个模块提供一个 mixin，由 ``main.MemoryDiaryPlugin`` 组合。带 ``@filter.*``
装饰器的入口薄壳留在 main.py，实现以 ``_handle_<入口名>`` 的形式放在这里。
"""

from .cmd_annotation import AnnotationCommandsMixin
from .cmd_delete import DeleteCommandsMixin
from .cmd_help_persona import HelpPersonaCommandsMixin
from .cmd_memo import MemoCommandsMixin
from .cmd_restore import RestoreCommandsMixin
from .cmd_search import SearchCommandsMixin
from .cmd_settings import SettingsCommandsMixin
from .cmd_transfer import TransferCommandsMixin
from .cmd_view import ViewCommandsMixin
from .cmd_write import WriteCommandsMixin
from .gate import GateMixin
from .hooks import EventHooksMixin
from .lifecycle import LifecycleMixin
from .web_diary import WebDiaryApiMixin
from .web_export import WebExportApiMixin
from .web_note import WebNoteApiMixin
from .web_material import WebMaterialApiMixin
from .web_search import WebSearchApiMixin
from .web_prefs import WebPrefsApiMixin

# 组合顺序：都排在 MemoryDiaryCore 之前（与拆分前"方法定义在主类上"的覆盖关系一致）。
# LifecycleMixin.initialize 通过 super() 继续调用 MemoryDiaryCore.initialize。
AdapterMixins = (
    LifecycleMixin,
    GateMixin,
    HelpPersonaCommandsMixin,
    MemoCommandsMixin,
    SearchCommandsMixin,
    WriteCommandsMixin,
    ViewCommandsMixin,
    AnnotationCommandsMixin,
    DeleteCommandsMixin,
    RestoreCommandsMixin,
    TransferCommandsMixin,
    SettingsCommandsMixin,
    EventHooksMixin,
    WebDiaryApiMixin,
    WebExportApiMixin,
    WebPrefsApiMixin,
    WebNoteApiMixin,
    WebMaterialApiMixin,
    WebSearchApiMixin,
)

__all__ = ["AdapterMixins"]
