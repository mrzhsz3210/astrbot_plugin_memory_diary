# -*- coding: utf-8 -*-
"""指令实现：/写日记。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

from astrbot.api.event import AstrMessageEvent

from ..memory_diary.log import logger


class WriteCommandsMixin:
    """指令实现：/写日记。"""

    # ------------------------- 指令：写日记 -------------------------

    async def _writing_busy_text(self, umo: str) -> str:
        """写日记互斥时手动入口的婉拒文案（两处检查共用）。"""
        return await self._say(
            umo,
            "⏳ 我正在写日记呢，等我写完这半篇再来找我好不好～",
            "用户让你写日记，但你正在为他写另一篇日记（自动触发、定时任务或刚启动的）。"
            "请用你自己的口吻让他稍等片刻（不要说技术细节），"
            "告诉他你写完手头的这半篇再来找他。两三句话以内。",
        )

    async def _handle_write_diary_cmd(self, event: AstrMessageEvent):
        """写日记：默认简记；/日记 完整 写完整篇；/日记 简记 强制简记。（``main.write_diary_cmd`` 的实现）"""
        if not self._should_respond(event):
            return
        umo = event.unified_msg_origin
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name() or sender_id

        arg = self._command_arg(event).lower()
        if arg in ("完整", "正式", "full"):
            requested_mode = "full"
        elif arg in ("简记", "简略", "brief"):
            requested_mode = "brief"
        elif arg in ("", "自动", "auto"):
            requested_mode = "auto"
        else:
            # 参数不认识时记录 repr：不可见字符（其他插件追加的标记）肉眼看不见
            logger.warning(
                f"[memory_diary] 无法识别的写日记参数，原始消息："
                f"{(event.message_str or '')!r}"
            )
            yield event.plain_result(
                "📖 用法：\n"
                "· /日记 —— 默认简记（第一次写日记会写完整）\n"
                "· /日记 完整 —— 写一篇完整日记，并合并同场景之前的简记\n"
                "· /日记 简记 —— 强制只写一段简记"
            )
            return

        remaining = self._op_cooldown_remaining(sender_id)
        if remaining > 0:
            canned = f"⏳ 日记操作太频繁啦，请 {remaining} 分钟后再让我写。"
            yield event.plain_result(
                await self._say(
                    umo,
                    canned,
                    f"用户又想操作日记，但距离上次操作还没过冷却期，还需要等 {remaining} 分钟。"
                    f"请用你自己的口吻婉拒ta（必须保留 {remaining} 分钟这个等待时间），"
                    "可以撒撒娇，让ta过会儿再来。",
                )
            )
            return

        # 与哨兵/定时任务的自动写日记互斥：三者都走 _save_diary 整体重写同一 zip。
        # 锁的粒度是 sender_id（保护的是 diary_<sender_id>.zip），不是会话 umo：
        # 同一群里不同用户各写各的日记，不能互相阻塞；同一用户的私聊/群聊写同
        # 一个 zip，必须互斥。写的时候还要占位（见下面 _write_diary 处的 try/finally）。
        if sender_id in self._writing:
            yield event.plain_result(await self._writing_busy_text(umo))
            return

        # 阈值门：上下文占用未达配置比例时不允许手动写日记（0 为不限制）
        # 估算与哨兵/状态页同口径（v2 下复用哨兵最近一次请求的数字）
        # 窗口 0/负数 = 比例体系停用：百分比算不出来，门槛一并跳过（否则会永远拦人）
        min_ratio = float(self.config.get("write_min_ratio", 0) or 0)
        limit = self._context_limit()
        if min_ratio > 0 and limit > 0:
            used = await self._context_used(umo)
            ratio = used / limit
            if ratio < min_ratio:
                canned = (
                    f"⏳ 上下文才用了约 {int(ratio * 100)}%，还没到该写日记的时候"
                    f"（阈值 {int(min_ratio * 100)}%）。再陪我聊聊，或者到快满时我会自动写～"
                )
                yield event.plain_result(
                    await self._say(
                        umo,
                        canned,
                        f"用户想让你现在写日记，但管理员设了门槛：上下文占用要达到 {int(min_ratio * 100)}%"
                        f" 才可以，现在才约 {int(ratio * 100)}%。请用你自己的口吻告诉ta还早"
                        "（保留两个百分比数字），邀请ta再聊聊，或者说到快满时你会自己写好。",
                    )
                )
                return

        full_before = self._full_diary_count_today(
            sender_id, bool(event.get_group_id())
        )
        # 双向互斥：手动写期间也要占住 _writing。只做开头的“检查”是不够的——
        # 手动写要等 LLM 生成（数秒到数十秒），这期间哨兵/每天定时的自动写仍会
        # 启动，两边都整体重写同一个 zip，照样丢一篇。占位与自动写同一把锁。
        # 另外：上面的门槛检查里有 await（_context_used），check 与 add 之间可能
        # 被别的写者插进来，所以占位前同步复查一次，紧接着 add（中间没有 await）。
        if sender_id in self._writing:
            yield event.plain_result(await self._writing_busy_text(umo))
            return
        self._writing.add(sender_id)
        try:
            diary_content, err = await self._write_diary(
                umo,
                sender_id,
                sender_name,
                is_group=bool(event.get_group_id()),
                requested_mode=requested_mode,
            )
        finally:
            self._writing.discard(sender_id)
        if err:
            # 对话式的婉拒（没新内容/今天随笔已写过）走人设口吻；技术性报错保持固定文案，保证信息准确
            conversational = err.startswith(("距离上次", "今天的随笔"))
            if conversational:
                yield event.plain_result(
                    await self._say(
                        umo,
                        f"📖 {err}",
                        f"用户让你写日记，但你要婉拒：{err}。"
                        "请用你自己的口吻把这件事自然地告诉ta，两三句话以内。",
                    )
                )
            else:
                yield event.plain_result(f"📖 {err}")
            return
        self._op_mark(sender_id)  # 写成功才计入冷却

        # 让她像平时说话一样告知一声，而不是把日记全文贴出来
        # 关闭「按人设口吻」时不额外调用 LLM，直接用固定文案
        notice = ""
        if bool(self.config.get("use_persona", True)):
            notice = await self._in_character_notice(
                umo,
                "你刚刚悄悄写完并保存了给未来自己的日记。请告诉用户：日记写好了"
                "（不要复述日记内容，可以一两句轻轻带过记了些什么），"
                "并温柔提醒 ta：发送 /reset 开启新上下文后，你会先读一遍日记再来见 ta。",
            )
        if not notice:
            notice = (
                "📖 日记写好啦，已经存好。发送 /reset 后，我会先读一遍日记再来找你。"
            )
        notice += (
            f"\n\n（本篇约 {len(diary_content)} 字，/reset 后的第一轮会一次性读入，"
            "不会每轮重复消耗 token）"
        )

        # 完整篇每日名额提示（仅当本次确实新增了完整篇且配置了上限；0=不限）
        try:
            max_full = int(self.config.get("max_full_diaries_per_day", 1))
        except (TypeError, ValueError):
            max_full = 1
        if max_full < 0:
            max_full = 1
        if requested_mode == "full" and max_full > 0:
            full_after = self._full_diary_count_today(
                sender_id, bool(event.get_group_id())
            )
            if full_after > full_before:
                remaining = max(0, max_full - full_after)
                if remaining > 0:
                    notice += (
                        f"\n\n（今天这个场景的完整日记已写 {full_after}/{max_full} 篇，"
                        f"还剩 {remaining} 篇；次日 00:00 重置）"
                    )
                else:
                    notice += (
                        f"\n\n（今天这个场景的完整日记已写 {full_after}/{max_full} 篇，"
                        "名额已用完；可以继续写简记，或次日 00:00 重置后再写完整篇）"
                    )

        # v0.6 插图提示行（有图在画 / 有形象提案待确认），由 _write_diary 写入
        # _image_last_notice；读一次即清空，避免串到下一次通知里。
        # 只在手动 /写日记 的通知处追加——自动写日记的通知路径保持 v0.5.5 原样。
        image_notice = getattr(self, "_image_last_notice", "") or ""
        if image_notice:
            notice += image_notice
            self._image_last_notice = ""

        if (
            bool(self.config.get("show_diary_after_write", False))
            and not event.get_group_id()
        ):
            # 日记全文只在私聊展示，群聊不展示，避免泄露给其他成员。
            # v0.6：QQ 纯文本里不露 markdown 图片语法——插图引用显示成
            # [插图：描述] / [用户分享的照片：描述] 占位（图文穿插只走 /查看日记）。
            notice += (
                "\n\n—— 日记全文 ——\n"
                f"{self._image_macro_to_placeholder(diary_content)}"
            )

        yield event.plain_result(notice)
