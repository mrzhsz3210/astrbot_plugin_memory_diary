# -*- coding: utf-8 -*-
"""指令实现：/恢复日记（回收站找回，v0.7.6）。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
交互模式复用 /删除日记 的既有约定：仅本人、仅私聊、操作冷却、session_waiter。
"""

from astrbot.api.event import AstrMessageEvent

from ..memory_diary.log import logger
from .compat import SessionController, session_waiter, _SESSION_WAITER_OK


class RestoreCommandsMixin:
    """指令实现：/恢复日记。"""

    async def _handle_restore_diary(self, event: AstrMessageEvent):
        """从回收站恢复最近删除的日记（仅本人、仅私聊）。（``main.restore_diary`` 的实现）"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 恢复日记请私聊我操作～",
                    "用户在群里想恢复日记，你要委婉地拒绝：恢复日记涉及隐私，请他来私聊操作。",
                )
            )
            return

        # v0.7.6 人格隔离：只能看到并恢复当前人格名下的回收站条目
        sender_id, persona_name = await self._diary_identity_info(
            event.unified_msg_origin, event.get_sender_id()
        )
        retention = self._trash_retention_days()

        # v0.7.6（Bug C）：回收站关闭不等于"没有恢复途径"——自动备份是
        # 独立通道（防外部删除），只要日记本异常且有备份，备份恢复仍可用
        live_path = self._diary_path(sender_id)
        live_broken = live_path.exists() and self._all_entries_tampered(sender_id)
        backups = []
        if not live_path.exists() or live_broken:
            try:
                backups = self.list_diary_backups(sender_id)
            except Exception as e:
                logger.warning(f"[memory_diary] 读取备份列表失败: {e}")

        if retention <= 0 and not backups:
            yield event.plain_result(
                "📖 回收站已被管理员关闭（trash_retention_days=0），"
                "删除的日记会立即清理，无法恢复。"
            )
            return

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

        # 列清单前例行清理过期条目，保证"还在保留期内"的承诺成立
        try:
            self._purge_trash()
        except Exception as e:
            logger.warning(f"[memory_diary] 回收站例行清理失败: {e}")

        items = self.list_trash(sender_id) if retention > 0 else []

        if not items and not backups:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 回收站是空的，没有可以找回的日记。",
                    "用户想找回删除的日记，但回收站里一条记录都没有。"
                    "用你自己的口吻如实告诉他。",
                )
            )
            return

        def _item_line(i: int, item: dict) -> str:
            labels = "、".join(
                f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in item.get("days") or []
            )
            return (
                f"{i}. {item.get('deleted_at') or '未知时间'} 删除的 "
                f"{len(item.get('days') or [])} 天：{labels}"
            )

        def _backup_line(i: int, item: dict) -> str:
            labels = "、".join(
                f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in item.get("days") or []
            )
            return f"B{i}. {item['label']} 的自动备份（{len(item['days'])} 天：{labels}）"

        scope_note = f"（人格「{persona_name}」名下）" if persona_name else ""
        sections = []
        if items:
            sections.append(
                f"📖 回收站里{scope_note}有 {len(items)} 条删除记录：\n"
                + "\n".join(_item_line(i, item) for i, item in enumerate(items, 1))
            )
        if backups:
            state = "已丢失" if not live_path.exists() else "全部条目验签失败"
            sections.append(
                f"💾 当前日记本{state}，可从自动备份恢复：\n"
                + "\n".join(
                    _backup_line(i, item) for i, item in enumerate(backups, 1)
                )
            )
        reply_guide = "回复序号恢复回收站条目"
        if backups:
            reply_guide += "，回复 B 加序号（如 B1）恢复自动备份"
        yield event.plain_result(
            "\n\n".join(sections)
            + f"\n\n{reply_guide}；60 秒内有效，回复「取消」退出。"
        )

        if not _SESSION_WAITER_OK:
            yield event.plain_result(
                "📖 当前环境不支持交互式恢复。为避免误操作，未做任何改动。"
            )
            return

        @session_waiter(timeout=60, record_history_chains=False)
        async def diary_restore_waiter(
            controller: SessionController, ev: AstrMessageEvent
        ):
            text = self._strip_invisible(ev.message_str or "").strip()
            if self._is_cancel(text):
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 已取消，回收站原样保留。",
                            "用户取消了恢复日记的操作，什么都没改动。用你自己的口吻确认一下就好。",
                        )
                    )
                )
                controller.stop()
                return
            idx = None
            backup_idx = None
            if text.isdigit():
                try:
                    idx = int(text)
                except (TypeError, ValueError):
                    idx = None
            elif backups and text.lower().startswith("b") and text[1:].isdigit():
                # 自动备份通道（Bug C）：B1/B2… 对应备份清单序号
                try:
                    backup_idx = int(text[1:])
                except (TypeError, ValueError):
                    backup_idx = None

            if backup_idx is not None and 1 <= backup_idx <= len(backups):
                backup = backups[backup_idx - 1]
                days, err = self.restore_diary_backup(sender_id, backup["name"])
                if err:
                    await ev.send(ev.plain_result(f"📖 恢复没有完成：{err}"))
                    controller.stop()
                    return
                labels = "、".join(
                    f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in days
                )
                self._op_mark(sender_id)
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            f"📖 已从自动备份恢复 {len(days)} 天的日记：{labels}",
                            f"用户从自动备份找回了 {len(days)} 天的日记，具体是：{labels}。"
                            f"请用你自己的口吻确认恢复完成（必须原样保留日记日期：{labels}），"
                            "语气体贴一点（失而复得的回忆），一两句话即可。",
                        )
                    )
                )
                controller.stop()
                return

            if idx is None or not (1 <= idx <= len(items)):
                logger.warning(
                    f"[memory_diary] 恢复日记没看懂用户回复，原始消息："
                    f"{(ev.message_str or '')!r}"
                )
                await ev.send(
                    ev.plain_result(
                        "📖 没看懂～回复序号恢复对应那条"
                        + ("，或 B 加序号恢复备份" if backups else "")
                        + "，或回复「取消」退出。"
                    )
                )
                controller.keep(timeout=60, reset_timeout=True)
                return

            item = items[idx - 1]
            restored, skipped, err = self.restore_trash(sender_id, item["id"])
            if err:
                await ev.send(
                    ev.plain_result(f"📖 恢复没有完成：{err}")
                )
                controller.stop()
                return
            labels = "、".join(
                f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in restored
            )
            canned = f"📖 已恢复 {len(restored)} 天的日记：{labels}"
            if skipped:
                skip_labels = "、".join(
                    f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in skipped
                )
                canned += (
                    f"\n（{skip_labels} 在当前日记里已有更新的内容，"
                    "没有覆盖放回）"
                )
            self._op_mark(sender_id)
            await ev.send(
                ev.plain_result(
                    await self._say(
                        ev.unified_msg_origin,
                        canned,
                        f"用户刚从回收站找回了 {len(restored)} 天的日记，具体是：{labels}。"
                        f"请用你自己的口吻确认恢复完成（必须原样保留日记日期：{labels}），"
                        "语气体贴一点（失而复得的回忆），一两句话即可。",
                    )
                    + (
                        f"\n（{'、'.join(f'{d[:4]}-{d[4:6]}-{d[6:]}' for d in skipped)} "
                        "已有更新内容，未覆盖）"
                        if skipped
                        else ""
                    )
                )
            )
            controller.stop()

        try:
            await diary_restore_waiter(event)
        except TimeoutError:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 恢复操作超时，回收站未改动。",
                    "恢复日记的互动超时了，什么都没改动。用你自己的口吻告诉他，"
                    "想恢复的话再发一次指令。",
                )
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 恢复日记交互异常: {e}")
        finally:
            event.stop_event()
