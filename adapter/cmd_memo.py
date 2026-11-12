# -*- coding: utf-8 -*-
"""指令实现：/记一下、/查看备忘、/删除备忘。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

from astrbot.api.event import AstrMessageEvent

from ..memory_diary.log import logger
from ..memory_diary.scope import scope_from_event
from .compat import SessionController, session_waiter, _SESSION_WAITER_OK


class MemoCommandsMixin:
    """指令实现：/记一下、/查看备忘、/删除备忘。"""

    # ------------------------- 指令：记一下（备忘） -------------------------

    async def _handle_memo_cmd(self, event: AstrMessageEvent):
        """把一件重要的事记下来，下次写日记时重点写进画像。用法：/记一下 要记住的事（``main.memo_cmd`` 的实现）"""
        if not self._should_respond(event):
            return
        # v0.7.6 人格隔离：备忘跟随当前人格的日记本
        sender_id = await self._diary_identity(
            event.unified_msg_origin, event.get_sender_id()
        )
        raw_text = event.message_str or ""
        # P3-13：零宽/方向控制字符只用于「判定指令与内容」，备忘内容一律保存原文——
        # 对整条消息剥离会破坏 ZWJ emoji（👨\u200d👩\u200d👧 会被拆成三个独立人像）
        # 和阿拉伯/波斯文正字法必需的 ZWNJ（影响字形连写）。
        # 指令与内容在同一句话内、用空格分开。剥离只删非空白字符，不改变空白
        # 分隔位置，所以内容可以从原文按同一分割位置取回，保留不可见字符原样。
        # 全角空格（U+3000）由 str.split() 当 Unicode 空白一并处理，无需兜底分支。
        raw_parts = raw_text.strip().split(maxsplit=1)
        note = raw_parts[1].strip() if len(raw_parts) > 1 else ""

        if not note:
            # P3-15：用法提示分支记 repr，日志里能看到其他插件追加的不可见字符
            logger.warning(
                f"[memory_diary] 记一下 缺少内容，原始消息：{raw_text!r}"
            )
            yield event.plain_result(
                "在指令后面空一格，写上要我记住的事就可以啦，例如：\n"
                "/记一下 我下周三要考试\n"
                "下次写日记时我会把它重点记进去。"
            )
            return

        is_group = scope_from_event(event).is_group
        total, saved = self._append_memo(
            sender_id,
            note,
            scene="group" if is_group else "private",
            scope=str(scope_from_event(event)),
        )
        if not saved:
            # P2-5 修复：保存失败必须明说，不能谎报"已记下"
            # 技术性写入失败保持固定文案：必须逐字保留数据目录/磁盘/权限等排查指引
            yield event.plain_result(
                "📌 保存失败了（数据目录不可写或磁盘已满），这条备忘其实没有记下——"
                "请让管理员检查插件数据目录的权限后再试。"
            )
            return
        scene_tip = (
            "（这条是群聊备忘，只会在群聊写日记时吸收；群聊/私聊备忘各存 20 条）"
            if is_group
            else "（这条是私聊备忘，不会出现在群聊的日记里；群聊/私聊备忘各存 20 条）"
        )
        # P3-1 修复：scene_tip 只在最后追加一次，canned 里不再重复包含
        canned = f"📌 已记下：「{note}」\n下次写日记时我会重点把它写进画像里" + (
            f"（当前这个场景还有 {total} 条备忘待写入）。" if total > 1 else "。"
        )
        yield event.plain_result(
            await self._say(
                event.unified_msg_origin,
                canned,
                f"用户刚让你记住一件事：「{note}」。请用你自己的口吻确认你记住了（保留原话引用），"
                "并告诉ta下次写日记时会重点记进去。两三句话以内。",
            )
            + f"\n{scene_tip}"
        )

    # ------------------------- 指令：查看 / 删除备忘 -------------------------

    async def _handle_memo_view(self, event: AstrMessageEvent):
        """查看待写入日记的备忘（仅私聊）：列出全部备忘及其场景。（``main.memo_view`` 的实现）"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📌 备忘是你的小秘密哦，请私聊我查看～",
                    "用户在群里想看备忘，你要委婉地拒绝：备忘是私密的，请他来私聊再看。",
                )
            )
            return
        sender_id = await self._diary_identity(
            event.unified_msg_origin, event.get_sender_id()
        )
        memos = self._load_memos(sender_id)
        if not memos:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📌 目前没有待写入日记的备忘。用 /记一下 内容 就能存一条。",
                    "用户查看备忘，但现在一条备忘都没有。告诉他：没有待写的备忘，"
                    "想存的话发 /记一下 加内容就行。",
                )
            )
            return
        lines = [
            f"{i}. [{m['time']} · {'群聊' if self._memo_scene(m) == 'group' else '私聊'}] {m['text']}"
            for i, m in enumerate(memos, 1)
        ]
        yield event.plain_result(
            f"📌 你目前有 {len(memos)} 条备忘待写入日记：\n"
            + "\n".join(lines)
            + "\n\n（私聊写日记会吸收全部备忘；群聊写日记只吸收群聊备忘。"
            "想删的话用 /删除备忘）"
        )

    async def _handle_memo_delete(self, event: AstrMessageEvent):
        """删除备忘（仅私聊）：先列出，再回复序号删一条或「全部」清空，60 秒内有效。（``main.memo_delete`` 的实现）"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📌 删除备忘请私聊我操作～",
                    "用户在群里想删备忘，你要委婉地拒绝：删备忘涉及隐私，请他来私聊操作。",
                )
            )
            return
        sender_id = await self._diary_identity(
            event.unified_msg_origin, event.get_sender_id()
        )
        memos = self._load_memos(sender_id)
        if not memos:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📌 没有可删除的备忘。",
                    "用户想删备忘，但现在一条备忘都没有，如实告诉他就好。",
                )
            )
            return

        lines = [
            f"{i}. [{m['time']} · {'群聊' if self._memo_scene(m) == 'group' else '私聊'}] {m['text']}"
            for i, m in enumerate(memos, 1)
        ]
        yield event.plain_result(
            f"📌 你目前有 {len(memos)} 条备忘：\n"
            + "\n".join(lines)
            + "\n\n回复序号删除那条；回复「全部」清空所有备忘（备忘可以再记，不需要二次确认）。"
            "60 秒内有效，回复「取消」退出。"
        )

        if not _SESSION_WAITER_OK:
            yield event.plain_result("📌 当前环境不支持交互式操作，未做任何改动。")
            return

        @session_waiter(timeout=60, record_history_chains=False)
        async def memo_delete_waiter(
            controller: SessionController, ev: AstrMessageEvent
        ):
            # P3-14：不可见字符只影响「取消/全部/序号」判断，剥离后不回写消息
            text = self._strip_invisible(ev.message_str or "").strip()
            if self._is_cancel(text):
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📌 已取消，备忘原样保留。",
                            "用户取消了删除备忘的操作，什么都没改动。用你自己的口吻确认一下就好。",
                        )
                    )
                )
                controller.stop()
                return
            cur = self._load_memos(sender_id)
            if text.lower() in ("全部", "all"):
                if not self._clear_memos(sender_id):
                    await ev.send(
                        ev.plain_result(
                            "📌 保存失败（数据目录不可写或磁盘已满），备忘未改动。"
                        )
                    )
                    controller.stop()
                    return
                canned = f"📌 已清空全部 {len(cur)} 条备忘。"
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            canned,
                            f"用户刚刚清空了全部 {len(cur)} 条备忘。请用你自己的口吻确认备忘已清空"
                            f"（保留 {len(cur)} 这个数字），可以轻松一点，告诉ta随时可以再来记。",
                        )
                    )
                )
                controller.stop()
                return
            if text.isdigit():
                # P3-2：isdigit() 会放行 ² 等上标数字（int() 转不了），
                # 解析失败统一走"没看懂"分支，不能让 ValueError 打断交互。
                try:
                    idx = int(text)
                except (TypeError, ValueError):
                    idx = None
                if idx is not None and 1 <= idx <= len(cur):
                    target = cur[idx - 1]
                    # P3-4：按下标只删这一个元素。旧实现按"内容+时间"过滤，同文本
                    # 同分钟的两条备忘会被一次删掉两条却只提示删一条；cur 是刚重新
                    # 读取的列表，下标此刻有效，不存在"序号漂移"问题。
                    rest = cur[: idx - 1] + cur[idx:]
                    # v0.7.6：备忘按人格分槽存储，回写必须走存储层的分槽口径
                    if not self._save_memos_list(sender_id, rest):
                        await ev.send(
                            ev.plain_result(
                                "📌 保存失败（数据目录不可写或磁盘已满），备忘未改动。"
                            )
                        )
                        controller.stop()
                        return
                    canned = f"📌 已删除备忘：「{target['text']}」"
                    await ev.send(
                        ev.plain_result(
                            await self._say(
                                ev.unified_msg_origin,
                                canned,
                                f"用户刚刚删掉了一条备忘，内容是「{target['text']}」。"
                                "请用你自己的口吻确认这条备忘已经删掉了（保留原内容引用），一句话即可。",
                            )
                        )
                    )
                    controller.stop()
                    return
                if idx is not None:
                    canned = f"📌 没有这个序号（当前共 {len(cur)} 条），已取消。"
                    await ev.send(
                        ev.plain_result(
                            await self._say(
                                ev.unified_msg_origin,
                                canned,
                                f"用户在删备忘时回复了序号 {idx}，但没有这个序号"
                                f"（当前共 {len(cur)} 条）。删除已取消、备忘没动，"
                                f"请用你自己的口吻告诉ta（必须保留 {len(cur)} 这个数字）。",
                            )
                        )
                    )
                    controller.stop()
                    return
            # 没看懂（含 P3-2 里 isdigit() 但 int() 解析失败的输入，如 ²）
            # P3-15：用法提示分支记 repr，便于看到不可见字符
            logger.warning(
                f"[memory_diary] 删除备忘没看懂用户回复，原始消息："
                f"{(ev.message_str or '')!r}"
            )
            await ev.send(
                ev.plain_result(
                    await self._say(
                        ev.unified_msg_origin,
                        "📌 没看懂，已取消，备忘原样保留。",
                        "用户的回复看不懂（既不是序号也不是「全部」），删除操作已取消、"
                        "备忘没动。用你自己的口吻告诉他就好。",
                    )
                )
            )
            controller.stop()

        try:
            await memo_delete_waiter(event)
        except Exception as e:
            logger.warning(f"[memory_diary] 删除备忘交互异常: {e}")
        finally:
            event.stop_event()
