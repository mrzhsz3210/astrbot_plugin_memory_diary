# -*- coding: utf-8 -*-
"""指令实现：/日记帮助、/形象（形象档案）。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

import re

from astrbot.api.event import AstrMessageEvent

from ..memory_diary.log import logger


class HelpPersonaCommandsMixin:
    """指令实现：/日记帮助、/形象（形象档案）。"""

    # ------------------------- 指令：帮助 -------------------------

    async def _handle_diary_help(self, event: AstrMessageEvent):
        """查看记忆日记插件的全部指令与用法。（``main.diary_help`` 的实现）"""
        if not self._should_respond(event):
            return
        # P3-6：群聊提示语必须与实际闸门口径一致——只有配置明确要求 @ 时才提示 @；
        # 默认（group_require_at=false）直接发指令就能用，不能固定提示"必须@"来误导用户。
        group_tip = ""
        if event.get_group_id():
            if self.config.get("group_require_at", False):
                group_tip = "本群多 AI 防冲突已生效：在指令后 @我 才是叫我哦。"
            else:
                group_tip = (
                    "本群多 AI 防冲突已生效：直接发指令即可，"
                    "想明确叫我也能在指令后 @我。"
                )
        yield event.plain_result(
            "📖 记忆日记 · 指令一览\n"
            "━━━━━━━━━━━━━━\n"
            "· /写日记 —— 默认写简记（第一次写日记会写完整）\n"
            "· /写日记 完整 —— 写完整日记，并合并同场景之前的简记\n"
            "· /写日记 简记 —— 强制只写一段简记\n"
            "· /记一下 内容 —— 把重要的事存为备忘，下次写日记重点记进去（指令和内容空格分开）\n"
            "· /查看备忘 —— 私聊专用：查看待写入的备忘（带场景标注）\n"
            "· /删除备忘 —— 私聊专用：按序号删一条，或回复「全部」清空\n"
            "· /回忆 [全部|私人随笔] 关键词 —— 默认查找用户相关证据，可显式回查全部类型或 AI 私人随笔\n"
            "· /查看日记 —— 私聊专用：列出日期后按提示查看某天的日记\n"
            "· /查看批注 —— 私聊专用：查看本轮读日记时留下的批注与所有未解/悬置的批注\n"
            "· /写回批注 —— 私聊专用：把聊明白的批注写回日记（需确认，可悬置）\n"
            "· /删除日记 —— 私聊专用：删除全部 / 某天 / 某时间段的日记\n"
            "· /导出日记 —— 私聊：把自己的日记和签名钥匙打包发给你，用于换设备迁移\n"
            "· /导入日记 —— 私聊：把迁移包发给我，验签通过后自动安放日记\n"
            "· /日记状态 —— 查看上下文占用、日记篇数、哨兵状态\n"
            "· /日记范围 —— 设置新上下文注入哪些日记：最新 / 全部 / 最近N天 / 时间段 / 默认\n"
            "· /注入日记 范围 —— 主动让她现在就读指定日记（下一句对话生效，本会话去重）\n"
            "· /形象 —— 私聊专用：查看/确认/修改/重拟插画里「她」长什么样的形象档案\n"
            "· /日记帮助 —— 就是你现在看到的这个\n"
            "━━━━━━━━━━━━━━\n"
            "小提示：上下文快满时我会主动提醒并自动写好日记；\n"
            "写完发 /reset，我会先读一遍日记再继续陪你。\n"
            + group_tip
        )

    # ------------------------- 指令：形象档案（插图一致性，D6） -------------------------
    #
    # 形象档案 persona_image_desc 是"她"在插图里长什么样的唯一事实源（设计第 4 节）：
    # AI 不能自己改，只能提议；用户确认后才写进配置。进化需签字。
    # 这里的文案全部用固定文案，不走 _say 的人设转述——形象描述一个字都不能被
    # 改写（转述可能把"及腰黑发"说成"长发"），而且 200 字的描述也不适合复述。

    _PERSONA_DESC_MAX = 200

    # P2-19：形象档案是插件实例级设置（影响所有用户看到的插画），写操作只有
    # AstrBot 管理员能做；只读查看与「重拟」提案保持白名单用户可用。
    _PERSONA_ADMIN_REQUIRED_TEXT = (
        "📖 形象档案是插件实例级设置，只有 AstrBot 管理员能改。\n"
        "请让管理员发 /形象 修改 <描述> 或 /形象 确认。"
    )

    @staticmethod
    def _is_admin_event(event: AstrMessageEvent) -> bool:
        """判断事件发送者是不是 AstrBot 管理员（P2-19）。

        优先用 AstrBot 的 ``event.is_admin()``（4.29.3 实现为 role == "admin"）；
        老版本/测试假事件没有该方法时回退读 ``event.role``；两者都取不到按
        非管理员处理（fail closed），不把实例级配置交给普通用户。
        """
        try:
            is_admin = getattr(event, "is_admin", None)
            if callable(is_admin):
                return bool(is_admin())
        except Exception:
            pass
        try:
            return str(getattr(event, "role", "") or "").strip().lower() == "admin"
        except Exception:
            return False

    def _persona_image_usage_text(self) -> str:
        return (
            "📖 形象档案用法：\n"
            "· /形象 —— 查看当前形象档案与待确认的提案\n"
            "· /形象 确认 —— 采纳待确认的提案并写入配置（需 AstrBot 管理员）\n"
            "· /形象 修改 <描述> —— 直接修改形象描述（最多 200 字，需 AstrBot 管理员）\n"
            "· /形象 重拟 —— 让我按当前人设重新提炼一版提案（先给你确认）"
        )

    async def _handle_persona_image_cmd(self, event: AstrMessageEvent):
        """查看/确认/修改插图里的形象档案（仅私聊）。（``main.persona_image_cmd`` 的实现）"""
        if not self._should_respond(event):
            return
        umo = event.unified_msg_origin
        if event.get_group_id():
            yield event.plain_result(
                "📖 形象档案只能私聊查看和修改哦——插画里的「她」是只给你看的样子。"
            )
            return
        sender_id = event.get_sender_id()
        arg = self._strip_invisible(self._command_arg(event)).strip()
        parts = arg.split(None, 1)
        action = parts[0] if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if not arg:
            cur = str(self.config.get("persona_image_desc", "") or "").strip()
            pending = self._load_pending_persona_desc(sender_id)
            pending_desc = ""
            if isinstance(pending, dict):
                pending_desc = str(pending.get("desc") or "").strip()
            lines = [
                "📖 插画里的「她」·形象档案",
                f"· 当前形象：{cur or '（还没设置）'}",
                f"· 待确认提案：{pending_desc or '（没有）'}",
                "",
                self._persona_image_usage_text(),
            ]
            yield event.plain_result("\n".join(lines))
            return

        if action in ("确认", "确认提案", "采纳"):
            if not self._is_admin_event(event):
                yield event.plain_result(self._PERSONA_ADMIN_REQUIRED_TEXT)
                return
            ok, payload = self._apply_pending_persona_desc(sender_id)
            if ok:
                yield event.plain_result(
                    "📖 形象档案已确认并写入配置，之后画插图都会照这版画：\n"
                    f"{payload}"
                )
            else:
                yield event.plain_result(
                    f"📖 没有确认成功：{payload}\n"
                    "可以先发 /形象 重拟 生成一版提案，或 /形象 修改 <描述> 直接写。"
                )
            return

        if action in ("修改", "改"):
            if not self._is_admin_event(event):
                yield event.plain_result(self._PERSONA_ADMIN_REQUIRED_TEXT)
                return
            desc = re.sub(r"\s+", " ", rest).strip()
            desc = self._strip_invisible(desc).strip()
            if not desc:
                yield event.plain_result(
                    "📖 用法：/形象 修改 <描述>\n"
                    "例如：/形象 修改 黑色及腰长发，浅灰眼睛，常穿白色长裙"
                )
                return
            truncated = len(desc) > self._PERSONA_DESC_MAX
            if truncated:
                desc = desc[: self._PERSONA_DESC_MAX].strip()
            if self._set_config_value("persona_image_desc", desc):
                if truncated:
                    suffix = f"（{len(desc)} 字，超出 200 字的部分已截断）"
                else:
                    suffix = f"（{len(desc)} 字）"
                yield event.plain_result(
                    f"📖 形象档案已更新{suffix}，之后画插图都会照这版画：\n{desc}"
                )
            else:
                yield event.plain_result(
                    "📖 保存失败：形象档案没能写入配置（具体原因见后台日志）。\n"
                    "发 /形象 可以看到当前实际生效的档案。"
                )
            return

        if action in ("重拟", "重新拟", "生成"):
            desc = ""
            try:
                desc = str(await self._propose_persona_image_desc(umo) or "").strip()
            except Exception as e:
                logger.warning(f"[memory_diary] 提炼形象提案失败: {e}")
            if not desc:
                yield event.plain_result(
                    "📖 这次没能提炼出新的形象提案（可能是模型暂时不可用）。\n"
                    "可以稍后再发 /形象 重拟，或者用 /形象 修改 <描述> 直接写。"
                )
                return
            cur = str(self.config.get("persona_image_desc", "") or "").strip()
            saved = self._save_pending_persona_desc(
                sender_id, desc, kind="evolve" if cur else "init"
            )
            tail = (
                "确认的话发 /形象 确认 写入配置；想微调就发 /形象 修改 <描述>。"
                if saved
                else "（提醒：这份提案没能存成待确认状态，发 /形象 确认 可能无效；"
                "可以重试 /形象 重拟，或用 /形象 修改 <描述> 直接写。）"
            )
            yield event.plain_result(
                f"📖 我想到一版新的形象提案（{len(desc)} 字）：\n{desc}\n\n{tail}"
            )
            return

        # 不认识的参数：记录 repr 便于排查不可见字符，然后给用法
        logger.warning(
            f"[memory_diary] 无法识别的形象参数，原始消息："
            f"{(event.message_str or '')!r}"
        )
        yield event.plain_result(self._persona_image_usage_text())
