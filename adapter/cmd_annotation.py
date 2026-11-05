# -*- coding: utf-8 -*-
"""指令实现：/查看批注、/写回批注（v0.6，仅私聊）。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

from astrbot.api.event import AstrMessageEvent

from ..memory_diary.log import logger
from .compat import SessionController, session_waiter, _SESSION_WAITER_OK


class AnnotationCommandsMixin:
    """指令实现：/查看批注、/写回批注（v0.6，仅私聊）。"""

    # ------------------------- 指令：批注（v0.6，仅私聊） -------------------------
    #
    # 批注是"她"读完日记后在页边留下的字，属于私人内容：只在私聊展示与写回。
    # 展示文案全部由 memory_diary/annotation.py 生成（固定文案，不额外调模型）；
    # 写回必须走 session_waiter 由用户签字确认，AI 不能自行改写日记/批注。

    async def _handle_view_annotations(self, event: AstrMessageEvent):
        """查看页边批注（仅私聊）：本轮新写的、悬置的、已写回的都在这里。（``main.view_annotations`` 的实现）"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result("📖 批注是只给本人看的哦，请私聊我查看～")
            return
        if not self._annotations_enabled():
            yield event.plain_result("📖 批注功能未开启（管理员可在配置页开启）。")
            return
        yield event.plain_result(
            self.annotations_view_text(event.get_sender_id())
        )

    async def _handle_writeback_annotation(self, event: AstrMessageEvent):
        """把讨论收敛后的批注写回（仅私聊，需用户签字确认，可悬置）。（``main.writeback_annotation`` 的实现）"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result("📖 批注是只给本人看的哦，请私聊我查看～")
            return
        if not self._annotations_enabled():
            yield event.plain_result("📖 批注功能未开启（管理员可在配置页开启）。")
            return

        sender_id = event.get_sender_id()
        umo = event.unified_msg_origin
        # 列之前先用同一个编号口径探一下有没有可写回的批注（无则直接给固定文案）
        try:
            has_any = self.annotation_by_index(sender_id, 1) is not None
        except Exception as e:
            logger.warning(f"[memory_diary] 读取待写回批注失败: {e}")
            has_any = False
        try:
            listing = self.open_annotation_list_text(sender_id)
        except Exception as e:
            logger.warning(f"[memory_diary] 生成待写回批注列表失败: {e}")
            listing = "📖 暂时读不到批注，请稍后再试。"
        if not has_any:
            yield event.plain_result(listing)
            return

        if not _SESSION_WAITER_OK:
            # 非交互环境明确拒绝：写回必须用户签字，绝不静默改数据
            yield event.plain_result(
                "📖 当前环境不支持交互确认，写回没有进行，批注原样保留。"
            )
            return

        yield event.plain_result(listing)

        pending = {"ann": None, "draft": None}

        @session_waiter(timeout=60, record_history_chains=False)
        async def annotation_writeback_waiter(
            controller: SessionController, ev: AstrMessageEvent
        ):
            text = self._strip_invisible(ev.message_str or "").strip()
            if self._is_cancel(text):
                await ev.send(
                    ev.plain_result("📖 已取消，批注原样保留。")
                )
                controller.stop()
                return
            # 第一步：选号
            if pending["ann"] is None:
                if not text.isdigit():
                    await ev.send(
                        ev.plain_result(
                            "📖 请回复序号选择要写回哪一条（回复「取消」退出）。"
                        )
                    )
                    controller.keep(timeout=60, reset_timeout=True)
                    return
                ann = self.annotation_by_index(sender_id, int(text))
                if ann is None:
                    await ev.send(
                        ev.plain_result(
                            "📖 没有这个序号，请重新回复序号（回复「取消」退出）。"
                        )
                    )
                    controller.keep(timeout=60, reset_timeout=True)
                    return
                try:
                    draft = await self._distill_annotation_reply(
                        sender_id, umo, ann
                    )
                except Exception as e:
                    logger.warning(f"[memory_diary] 批注写回蒸馏异常: {e}")
                    draft = None
                if not draft:
                    await ev.send(
                        ev.plain_result(
                            "📖 这次没聊出能写回的内容，批注原样保留。"
                        )
                    )
                    controller.stop()
                    return
                pending["ann"] = ann
                pending["draft"] = draft
                await ev.send(
                    ev.plain_result(self.annotation_draft_text(ann, draft))
                )
                controller.keep(timeout=60, reset_timeout=True)
                return
            # 第二步：签字确认 / 悬置 / 取消（取消已在上方统一处理）
            ann_id = str((pending["ann"] or {}).get("id") or "")
            low = text.lower()
            if low in ("确认", "确认写回", "是", "yes", "y", "ok"):
                if self.apply_annotation_reply(sender_id, ann_id, pending["draft"]):
                    await ev.send(
                        ev.plain_result("📖 已确认写回，这条批注标记为已解。")
                    )
                else:
                    await ev.send(
                        ev.plain_result(
                            "📖 保存失败（数据目录不可写或磁盘已满），批注没有改动。"
                        )
                    )
                controller.stop()
                return
            if low in ("悬置", "暂缓", "先放着", "defer"):
                if self.defer_annotation(sender_id, ann_id):
                    await ev.send(
                        ev.plain_result(
                            "📖 已悬置，这条批注先放着；"
                            "之后想写回再发 /写回批注 就行。"
                        )
                    )
                else:
                    await ev.send(
                        ev.plain_result(
                            "📖 保存失败（数据目录不可写或磁盘已满），批注没有改动。"
                        )
                    )
                controller.stop()
                return
            await ev.send(
                ev.plain_result(
                    "📖 没看懂～回复「确认」写回、「悬置」先放着，或「取消」退出。"
                )
            )
            controller.keep(timeout=60, reset_timeout=True)

        try:
            await annotation_writeback_waiter(event)
        except TimeoutError:
            yield event.plain_result(
                "📖 交互超时啦，批注原样保留，想写回随时再发 /写回批注。"
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 写回批注交互异常: {e}")
        finally:
            event.stop_event()
