# -*- coding: utf-8 -*-
"""指令实现：/删除日记。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

import re

from astrbot.api.event import AstrMessageEvent

from ..memory_diary.log import logger
from .compat import SessionController, session_waiter, _SESSION_WAITER_OK


class DeleteCommandsMixin:
    """指令实现：/删除日记。"""

    async def _handle_delete_diary(self, event: AstrMessageEvent):
        """删除日记（仅本人、仅私聊）：可选全部 / 某天 / 时间段。（``main.delete_diary`` 的实现）"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 删除日记请私聊我操作～",
                    "用户在群里想删日记，你要委婉地拒绝：删日记涉及隐私，请他来私聊操作。",
                )
            )
            return

        # v0.7.7 人格隔离：删除只作用于当前人格名下的日记
        sender_id, persona_name = await self._diary_identity_info(
            event.unified_msg_origin, event.get_sender_id()
        )
        remaining = self._op_cooldown_remaining(sender_id)
        if remaining > 0:
            canned = f"⏳ 日记操作太频繁啦，请 {remaining} 分钟后再操作。"
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    canned,
                    f"用户又想操作日记，但距离上次操作还没过冷却期，还需要等 {remaining} 分钟。"
                    f"请用你自己的口吻婉拒ta（必须保留 {remaining} 分钟这个等待时间），"
                    "可以撒撒娇，让ta过会儿再来。",
                )
            )
            return

        # v0.7.7：删除前列行清理过期回收站条目（保留期承诺的例行维护）
        try:
            self._purge_trash()
        except Exception:
            pass

        entries = self._list_entries(sender_id)
        if not entries:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 没有可删除的日记。",
                    "用户想删日记，但还一篇都没写过，用你自己的口吻如实告诉他。",
                )
            )
            return

        lines = [f"{i}. {self._entry_label(n)}" for i, n in enumerate(entries, 1)]
        # v0.7.7 人格隔离：多人格用户先看清"现在删的是哪个人格的本子"
        scope_note = f"（当前人格「{persona_name}」名下）" if persona_name else ""
        yield event.plain_result(
            f"📖 你目前有 {len(entries)} 天的日记{scope_note}：\n"
            + "\n".join(lines)
            + "\n\n回复「全部」删除所有日记；回复序号删除那天；回复日期（如 2026-10-23）删除那天；"
            "回复时间段（如 2026-10-20 到 2026-10-23）删除区间内所有天。"
            "60 秒内有效，回复「取消」退出。"
        )

        if not _SESSION_WAITER_OK:
            yield event.plain_result(
                "📖 当前环境不支持交互式删除。为避免误删，未做任何改动。"
            )
            return

        pending_all = {"v": False}
        date_pat = r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?"

        @session_waiter(timeout=60, record_history_chains=False)
        async def diary_delete_waiter(
            controller: SessionController, ev: AstrMessageEvent
        ):
            # P3-14：不可见字符只影响「取消/全部/序号/日期/时间段」判断，不回写消息
            text = self._strip_invisible(ev.message_str or "").strip()
            low = text.lower()

            # 任意阶段都可以取消（包括「全部删除」的二次确认阶段）
            if self._is_cancel(text):
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 已取消，日记原样保留。",
                            "用户取消了删除日记的操作，什么都没改动。用你自己的口吻确认一下就好。",
                        )
                    )
                )
                controller.stop()
                return

            # 第二步：全部删除的确认
            if pending_all["v"]:
                if low in ("确认", "确认删除", "是", "yes", "y"):
                    # P3-9 修复：删除失败（权限/IO）必须明说，不能谎报"已删除"
                    if self._delete_diary(sender_id):
                        self._op_mark(sender_id)
                        # v0.7.7 回收站：提示恢复途径（suffix 不过 _say，防转述丢失）
                        trash_days = self._trash_retention_days()
                        hint = (
                            f"\n（{trash_days} 天内发 /恢复日记 还能找回。）"
                            if trash_days > 0
                            else ""
                        )
                        await ev.send(
                            ev.plain_result(
                                await self._say(
                                    ev.unified_msg_origin,
                                    "📖 全部日记已删除。",
                                    "用户确认后，他的全部日记已经删掉了。用你自己的口吻确认这件事，"
                                    "语气体贴一点（日记是回忆），一句话即可。",
                                )
                                + hint
                            )
                        )
                    else:
                        await ev.send(
                            ev.plain_result(
                                "📖 删除失败（文件不可写或无权限），日记未改动——"
                                "请让管理员检查数据目录后重试。"
                            )
                        )
                else:
                    await ev.send(
                        ev.plain_result(
                            await self._say(
                                ev.unified_msg_origin,
                                "📖 已取消，日记原样保留。",
                                "用户在全部删除的二次确认时放弃了，日记没动。"
                                "用你自己的口吻确认一下就好。",
                            )
                        )
                    )
                controller.stop()
                return

            targets = None
            if low in ("全部", "all"):
                pending_all["v"] = True
                # v0.7.7：二次确认说清「删的是哪个人格名下的日记、删完能去哪找回」，
                # 避免多人格用户"以为在删别人格的，结果删了自己的"
                trash_days = self._trash_retention_days()
                if persona_name:
                    scope_text = f"你在人格「{persona_name}」名下的全部 {len(entries)} 天日记"
                else:
                    scope_text = f"你所有 {len(entries)} 天的日记"
                if trash_days > 0:
                    warn_text = (
                        f"⚠️ 将删除{scope_text}。删除的内容会先放进回收站，"
                        f"{trash_days} 天内可发 /恢复日记 找回（逾期清理后才不可恢复）。"
                    )
                else:
                    warn_text = f"⚠️ 将删除{scope_text}，此操作不可恢复。"
                await ev.send(
                    ev.plain_result(
                        warn_text + "回复「确认删除」执行，回复其他任何内容取消。"
                    )
                )
                controller.keep(timeout=60, reset_timeout=True)
                return
            if text.isdigit():
                # P3-2：isdigit() 会放行 ² 等上标数字（int() 转不了），
                # 解析失败统一走"没看懂"分支，不能让 ValueError 打断交互。
                try:
                    idx = int(text)
                except (TypeError, ValueError):
                    idx = None
                if idx is not None and 1 <= idx <= len(entries):
                    targets = [entries[idx - 1]]
            else:
                m_range = re.match(
                    rf"^{date_pat}\s*(?:到|至|~|--?|—)\s*{date_pat}$", text
                )
                if m_range:
                    g = [int(x) for x in m_range.groups()]
                    start = f"{g[0]:04d}{g[1]:02d}{g[2]:02d}"
                    end = f"{g[3]:04d}{g[4]:02d}{g[5]:02d}"
                    if start > end:
                        start, end = end, start
                    targets = [n for n in entries if start <= self._entry_day(n) <= end]
                else:
                    m_day = re.match(rf"^{date_pat}$", text)
                    if m_day:
                        y, mo, dd = (int(x) for x in m_day.groups())
                        # 注意：entries 里的条目名带 diaries/ 前缀，这里必须同样带上，
                        # 否则永远匹配不上（按日期删除会误报"那一天没有写日记"）
                        name = self._md_entry(f"{y:04d}{mo:02d}{dd:02d}")
                        targets = [name] if name in entries else []

            if targets is None:
                # P3-15：用法提示分支记 repr，便于看到不可见字符
                logger.warning(
                    f"[memory_diary] 删除日记没看懂用户回复，原始消息："
                    f"{(ev.message_str or '')!r}"
                )
                await ev.send(
                    ev.plain_result(
                        "📖 没看懂～回复「全部」、序号、日期，或时间段（如 2026-10-20 到 2026-10-23）。"
                    )
                )
                controller.keep(timeout=60, reset_timeout=True)
                return
            if not targets:
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 指定的日期里没有日记哦。",
                            "用户指定的日期范围里没有日记，用你自己的口吻如实告诉他。",
                        )
                    )
                )
                controller.stop()
                return

            deleted = self._delete_entries(sender_id, targets)
            if deleted > 0:
                self._op_mark(sender_id)
                labels = "、".join(self._entry_label(n) for n in targets)
                canned = f"📖 已删除 {deleted} 天的日记：{labels}"
                # v0.7.7 回收站：提示恢复途径（suffix 不过 _say，防转述丢失）
                trash_days = self._trash_retention_days()
                hint = (
                    f"\n（{trash_days} 天内发 /恢复日记 还能找回。）"
                    if trash_days > 0
                    else ""
                )
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            canned,
                            f"用户刚删掉了 {deleted} 天的日记，具体是：{labels}。"
                            f"请用你自己的口吻确认删除完成（必须原样保留日记日期：{labels}），"
                            "语气体贴一点（日记是回忆），一两句话即可。",
                        )
                        + hint
                    )
                )
            else:
                await ev.send(ev.plain_result("📖 删除失败，日记未改动。"))
            controller.stop()

        try:
            await diary_delete_waiter(event)
        except TimeoutError:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 删除操作超时，日记未改动。",
                    "删除日记的互动超时了，什么都没删。用你自己的口吻告诉他，"
                    "想删的话再发一次指令。",
                )
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 删除日记交互异常: {e}")
        finally:
            event.stop_event()
