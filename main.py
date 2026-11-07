# -*- coding: utf-8 -*-
"""astrbot_plugin_memory_diary —— 记忆日记（AstrBot 入口）。

分层（详见 ARCHITECTURE.md）：
  memory_diary/  领域核心，不 import astrbot（存储 / 写作流程 / 插图 / 批注 …）
  adapter/       AstrBot 适配层实现（指令、事件钩子、Web 日记本 API），按功能分模块
  main.py        只剩插件注册 + 组装 + 带 ``@filter.*`` 装饰器的入口薄壳

为什么入口必须留在这里：AstrBot 按 ``handler.__module__`` 把 handler 绑定到插件
实例（见 StarHandlerMetadata.handler_module_path），装饰器入口挪到别的模块就找不到。
所以每个入口只做一件事——转调 ``self._handle_<入口名>()``，实现在 adapter/ 对应模块。

新增指令 / 钩子的约定：
  1. 实现写在 adapter/ 的对应 mixin 里，命名为 ``_handle_<入口名>``；
  2. 这里只加装饰器 + 同签名薄壳 + 面向用户的 docstring（AstrBot 用它做指令描述）。
"""

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star, register

from .adapter import AdapterMixins
from .adapter.compat import resolve_data_dir
from .memory_diary.core import MemoryDiaryCore
from .memory_diary.log import logger


@register(
    "astrbot_plugin_memory_diary",
    "mrzhsz3210",
    "指令/定时自动触发写日记（用户画像 / 对用户的评价 / 给未来自己的叮嘱），新上下文自动读取日记，上下文将满时自动提醒，支持每天定时自动写完整日记。",
    "v0.7.0",
)
class MemoryDiaryPlugin(*AdapterMixins, MemoryDiaryCore, Star):
    """AstrBot 插件入口：只保留注册与 handler 薄壳，实现见 adapter/ 与 memory_diary/。"""

    def __init__(self, context, config=None):
        super().__init__(context, config, data_dir=resolve_data_dir())
        # 可视化视图（日记本页面）后端 API；不支持的 AstrBot 版本自动跳过
        try:
            self._register_web_apis()
        except Exception as e:
            logger.warning(f"[memory_diary] 注册 Web API 失败，日记本页面不可用: {e}")

    # ------------------------- 指令：帮助 / 形象档案 -------------------------
    # 实现：adapter/cmd_help_persona.py

    @filter.command("日记帮助", alias={"diary_help"})
    async def diary_help(self, event: AstrMessageEvent):
        """查看记忆日记插件的全部指令与用法。"""
        async for result in self._handle_diary_help(event):
            yield result

    @filter.command("形象", alias={"日记形象"})
    async def persona_image_cmd(self, event: AstrMessageEvent):
        """查看/确认/修改插图里的形象档案（仅私聊）。"""
        async for result in self._handle_persona_image_cmd(event):
            yield result

    # ------------------------- 指令：备忘 -------------------------
    # 实现：adapter/cmd_memo.py

    @filter.command("memo", alias={"记一下", "记住"})
    async def memo_cmd(self, event: AstrMessageEvent):
        """把一件重要的事记下来，下次写日记时重点写进画像。用法：/记一下 要记住的事"""
        async for result in self._handle_memo_cmd(event):
            yield result

    @filter.command("查看备忘", alias={"memo_view"})
    async def memo_view(self, event: AstrMessageEvent):
        """查看待写入日记的备忘（仅私聊）：列出全部备忘及其场景。"""
        async for result in self._handle_memo_view(event):
            yield result

    @filter.command("删除备忘", alias={"memo_delete"})
    async def memo_delete(self, event: AstrMessageEvent):
        """删除备忘（仅私聊）：先列出，再回复序号删一条或「全部」清空，60 秒内有效。"""
        async for result in self._handle_memo_delete(event):
            yield result

    # ------------------------- 指令：写日记 -------------------------
    # 实现：adapter/cmd_write.py

    @filter.command("diary", alias={"日记", "写日记"})
    async def write_diary_cmd(self, event: AstrMessageEvent):
        """写日记：默认简记；/日记 完整 写完整篇；/日记 简记 强制简记。"""
        async for result in self._handle_write_diary_cmd(event):
            yield result

    # ------------------------- 按需搜索 -------------------------
    # 实现：adapter/cmd_search.py

    @filter.command("回忆", alias={"recall"})
    async def recall(self, event: AstrMessageEvent):
        """查找历史证据。用法：/回忆 [全部|私人随笔] 关键词 [更多关键词]"""
        async for result in self._handle_recall(event):
            yield result

    @filter.llm_tool(name="memory_diary_search")
    async def memory_diary_search(
        self,
        event: AstrMessageEvent,
        query: str,
        day: str = "",
        max_results: int = 5,
        evidence_type: str = "user",
    ) -> str:
        """搜索当前用户与作用域的日记证据。evidence_type 可选 user/self/all/mixed/external/unknown。"""
        return await self._handle_memory_diary_search(
            event, query, day, max_results, evidence_type
        )

    # ------------------------- 指令：查看日记 -------------------------
    # 实现：adapter/cmd_view.py

    @filter.command("查看日记", alias={"diary_view"})
    async def view_diary(self, event: AstrMessageEvent):
        """查看日记（仅本人、仅私聊）：先列出时间，再按选择发送对应内容。"""
        async for result in self._handle_view_diary(event):
            yield result

    # ------------------------- 指令：批注（v0.6，仅私聊） -------------------------
    # 实现：adapter/cmd_annotation.py

    @filter.command("查看批注", alias={"annotations", "diary_annotations"})
    async def view_annotations(self, event: AstrMessageEvent):
        """查看页边批注（仅私聊）：本轮新写的、悬置的、已写回的都在这里。"""
        async for result in self._handle_view_annotations(event):
            yield result

    @filter.command("写回批注", alias={"diary_writeback"})
    async def writeback_annotation(self, event: AstrMessageEvent):
        """把讨论收敛后的批注写回（仅私聊，需用户签字确认，可悬置）。"""
        async for result in self._handle_writeback_annotation(event):
            yield result

    # ------------------------- 指令：删除日记 -------------------------
    # 实现：adapter/cmd_delete.py

    @filter.command("删除日记", alias={"diary_delete"})
    async def delete_diary(self, event: AstrMessageEvent):
        """删除日记（仅本人、仅私聊）：可选全部 / 某天 / 时间段。"""
        async for result in self._handle_delete_diary(event):
            yield result

    # ------------------------- 指令：导出 / 导入日记（迁移包） -------------------------
    # 实现：adapter/cmd_transfer.py

    @filter.command("导出日记", alias={"diary_export"})
    async def export_diary(self, event: AstrMessageEvent):
        """导出自己的迁移包（仅私聊）：日记 zip + 本人的派生签名钥匙，发 QQ 文件。"""
        async for result in self._handle_export_diary(event):
            yield result

    @filter.command("导入日记", alias={"diary_import"})
    async def import_diary(self, event: AstrMessageEvent):
        """导入自己的迁移包（仅私聊）：直接把 /导出日记 收到的迁移包发给我。"""
        async for result in self._handle_import_diary(event):
            yield result

    # ------------------------- 指令：密码 / 状态 / 注入范围 / 追加注入 -------------------------
    # 实现：adapter/cmd_settings.py

    @filter.command("日记密码", alias={"diary_password"})
    async def diary_password(self, event: AstrMessageEvent):
        """设置/修改/取消 Web 日记本页面的查看密码（仅私聊、仅本人）。

        用法：/日记密码 设置 <密码> ｜ /日记密码 修改 <旧> <新> ｜ /日记密码 取消 <旧>
        """
        async for result in self._handle_diary_password(event):
            yield result

    @filter.command("日记凭证", alias={"diary_token"})
    async def diary_token(self, event: AstrMessageEvent):
        """管理外部写入凭证（仅私聊、仅本人）。

        用法：/日记凭证 新办 <来源标识> ｜ /日记凭证 列表 ｜ /日记凭证 吊销 <序号>
        """
        async for result in self._handle_diary_token(event):
            yield result

    @filter.command("日记状态", alias={"diary_status"})
    async def diary_status(self, event: AstrMessageEvent):
        """查看当前上下文占用、日记更新时间和哨兵状态。"""
        async for result in self._handle_diary_status(event):
            yield result

    @filter.command("日记范围", alias={"diary_scope"})
    async def diary_scope(self, event: AstrMessageEvent, scope: str = ""):
        """设置新上下文注入哪些日记：最新 / 全部 / 最近N天 / 时间段。

        ``scope`` 是给 AstrBot CommandFilter 的占位参数：它按处理函数签名收集
        参数（前两个参数视为 self/event）。参数实际由 _command_arg() 从原始消息
        解析，这里不使用 scope 的值；保留它只为让“带参数的调用”与框架的指令参数
        语义对齐（当前去掉也能工作，但保留成本更低、对未来框架收紧更稳）。
        """
        async for result in self._handle_diary_scope(event, scope):
            yield result

    @filter.command("注入日记", alias={"diary_inject"})
    async def diary_inject(self, event: AstrMessageEvent, scope: str = ""):
        """主动把指定日记注入接下来的对话（本会话去重，下一句对话时一次性生效）。"""
        async for result in self._handle_diary_inject(event, scope):
            yield result

    # ------------------------- 事件钩子：/reset 识别、新会话注入日记 + 哨兵、token 校准 -------------------------
    # 实现：adapter/hooks.py

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """reset 双保险（P0-1 补强）：监听到 /reset 指令文本就置标记。

        只置标记、不拦截、不影响 AstrBot 内置 reset 的本身逻辑；
        标记在下一次 LLM 请求（on_llm_request）时消费，按新代际重置状态。
        """
        return await self._handle_on_any_message(event)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """每次 LLM 请求前：新会话注入日记；同时执行上下文哨兵。"""
        return await self._handle_on_llm_request(event, req)

    async def on_llm_response(self, event: AstrMessageEvent, resp):
        """拿到本次请求的真实 token 用量，对估算系数做 EWMA 自适应校准。

        仅在「改进版token估算」+「自动校准」同时开启时生效；
        on_llm_request 里暂存的静态估算 E 在这里被消费。

        P2-5：方法体保持普通 async 方法，装饰器在类定义完成后按 AstrBot 是否
        提供 on_llm_response 钩子动态应用（见文件末尾）——老 4.x 缺该钩子时
        插件仍能正常加载，只是不做校准。
        """
        return await self._handle_on_llm_response(event, resp)


# P2-5 兼容注册：老 AstrBot 4.x 可能没有 on_llm_response 钩子。模块导入期硬用
# @filter.on_llm_response() 会让整个插件在 import 阶段 AttributeError、直接加载
# 失败；这里在类定义完成后按钩子是否存在动态应用装饰器（保持方法名与直接调用
# 不变）——缺钩子时插件照常加载，只是不做 token 校准（降级而非崩溃）。
# 注：metadata.yaml 的 astrbot_version 下限不在这里维护（由发布配置/文档负责）。
if hasattr(filter, "on_llm_response"):
    MemoryDiaryPlugin.on_llm_response = filter.on_llm_response()(
        MemoryDiaryPlugin.on_llm_response
    )
else:  # pragma: no cover - 只有缺少该钩子的老版本 AstrBot 会走到
    logger.warning(
        "[memory_diary] 当前 AstrBot 版本没有 on_llm_response 钩子，"
        "token 估算自动校准不可用（插件其余功能正常）"
    )
