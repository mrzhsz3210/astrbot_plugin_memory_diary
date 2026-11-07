# -*- coding: utf-8 -*-
"""事件钩子实现：/reset 识别、新会话注入日记 + 上下文哨兵、token 校准。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

from astrbot.api.event import AstrMessageEvent, filter as event_filter
from astrbot.api.provider import ProviderRequest

from ..memory_diary.common import strip_trailing_decorations
from ..memory_diary.constants import INJECT_TEMPLATE
from ..memory_diary.log import logger
from ..memory_diary.scope import scope_from_event
from ..memory_diary.token_estimator import estimate_text
from .compat import TextPart

# P2-12：老版本 AstrBot 4.x 没有 on_llm_response 钩子（main.py 末尾按 hasattr
# 动态注册）。没有它就无法确认"这一轮请求真的成功了"，此时退回旧行为——交付即
# 落位，避免"已注入"永远无法确认、每轮重复注入。
_HAS_RESPONSE_HOOK = hasattr(event_filter, "on_llm_response")


class EventHooksMixin:
    """事件钩子实现：/reset 识别、新会话注入日记 + 上下文哨兵、token 校准。"""

    # ------------------------- 新会话注入日记 + 哨兵 -------------------------

    async def _handle_on_any_message(self, event: AstrMessageEvent):
        """reset 双保险（P0-1 补强）：监听到 /reset 指令文本就置标记。（``main.on_any_message`` 的实现）"""
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return  # 自己发的消息不算
        except Exception:
            pass
        # 附录 A 修复：白名单外用户在私聊发指令时给一次性提示（群聊/普通闲聊静默）
        await self._maybe_hint_whitelist_denied(event)
        sender_id = str(event.get_sender_id() or "")
        if not self._is_allowed(sender_id):
            self._auto_users.pop(sender_id, None)
            for task in list(getattr(self, "_diary_tasks", set())):
                if self._diary_task_owners.get(task) == sender_id:
                    task.cancel()
            for task in list(getattr(self, "_image_tasks", set())):
                if self._image_task_owners.get(task) == sender_id:
                    task.cancel()
            return
        # 登记私聊会话（供每天定时自动写日记主动通知），并惰性启动后台任务
        try:
            self._remember_auto_user(event)
        except Exception as e:
            logger.warning(f"[memory_diary] 处理自动写日记用户登记异常: {e}")
        task = getattr(self, "_auto_task", None)
        if not getattr(self, "_terminated", False) and (task is None or task.done()):
            self._start_auto_diary_task()
        # P3-14：/reset 识别前先剥不可见字符（只用于判断，不回写消息内容）
        # v0.6.1：部分插件（qq_tools 的 show_message_id）会往消息尾部追加
        # [MSG_ID:...] 机器标记，重置标记会认不出来 → 这里再剥一层尾部装饰，
        # 同样只用于判断、不回写 event.message_str。
        text = self._strip_invisible(event.message_str or "")
        text = strip_trailing_decorations(text).strip().lower()
        if text in ("/reset", "reset", "/重置", "重置"):
            self._reset_pending.add(event.unified_msg_origin)
            self._cap_state(self._reset_pending)

    async def _annotation_block_for_inject(
        self, sender_id, umo, in_group, diary: str, names: list,
        is_new_generation: bool,
    ) -> str:
        """批注注入块：reset 后首次注入时先同步生成，再返回注入块。

        - 生成条件：annotation_enabled 且非群聊且日记非空且本代际还没生成过
          （is_new_generation 由调用方按代际状态判定）；
        - 关闭 / 群聊 / 无日记 / 已生成过 → 只返回（可能为空的）注入块；
        - 批注层任何异常都只 warning 降级，日记注入照常，绝不阻塞对话。
        """
        # 群聊：不生成、不注入（批注仅私聊，隐私红线）
        if in_group:
            return ""
        try:
            if not self._annotations_enabled():
                return ""
        except Exception as e:
            logger.warning(f"[memory_diary] 批注开关判定异常，本轮不注入批注: {e}")
            return ""
        if is_new_generation and diary:
            try:
                result = await self._generate_annotations(
                    sender_id, umo, diary, list(names or []), is_group=in_group
                )
                if isinstance(result, dict):
                    created = int(result.get("created") or 0)
                    reason = str(result.get("reason") or "")
                    if created > 0:
                        logger.info(
                            f"[memory_diary] 已生成 {created} 条页边批注"
                            f"（round={result.get('round') or ''}）"
                        )
                    elif reason not in ("", "disabled", "group", "no_diary"):
                        logger.warning(
                            f"[memory_diary] 批注生成降级（{reason}），"
                            "本轮日记照常注入"
                        )
            except Exception as e:
                logger.warning(
                    f"[memory_diary] 批注生成异常，本轮日记照常注入: {e}"
                )
        try:
            # 批注和日记吃同一份注入预算（设计第二部分第 5 节）：按日记已占字数
            # 算出还能给批注块的字数（预留 ~400 字给行为纪律），再交给批注层裁剪。
            # 日记已把预算占满时 char_cap < 100，批注层本轮直接不注入（/查看批注 仍可看）。
            try:
                budget = int(self.config.get("inject_max_chars", 8000) or 8000)
            except (TypeError, ValueError):
                budget = 8000
            if budget <= 0:
                budget = 8000
            char_cap = min(1500, max(0, budget - len(diary or "") - 400))
            try:
                token_budget = int(self.config.get("inject_token_limit", 2400) or 2400)
            except (TypeError, ValueError):
                token_budget = 2400
            if token_budget <= 0:
                token_budget = 2400
            template_overhead = estimate_text(INJECT_TEMPLATE.format(diary="")) + 2
            token_cap = max(
                0, token_budget - estimate_text(diary or "") - template_overhead
            )
            return self._annotation_inject_block(
                sender_id, char_cap=char_cap, token_cap=token_cap
            ) or ""
        except Exception as e:
            logger.warning(f"[memory_diary] 批注注入块读取失败，已跳过: {e}")
            return ""

    async def _handle_on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """每次 LLM 请求前：新会话注入日记；同时执行上下文哨兵。（``main.on_llm_request`` 的实现）"""
        # 白名单外的用户：不注入日记、不跑上下文哨兵（"不给任何人写日记"的核心拦截点）
        # P3-11：判定异常必须 fail-closed（与 _is_allowed 的原则一致）——宁可这一轮
        # 不注入，也不要在身份不明时把日记塞进请求里。
        try:
            if not self._is_allowed(event.get_sender_id()):
                return
        except Exception as e:
            logger.warning(
                f"[memory_diary] 白名单判定异常，按拒绝处理（fail-closed，本轮跳过）: {e}"
            )
            return
        # 0) /reset 代际识别（P0-1 修复 + 双保险）：
        # AstrBot 4.x 的 /reset 只清空 history、不换 conversation_id，
        # 所以"新会话"不能只认 cid。两条识别路径，任一命中即视为新的一代：
        #   a) on_any_message 监听到 /reset 指令文本，置了 _reset_pending 标记；
        #   b) cid 没变但 req.contexts 已为空（上下文被清空）。
        # 命中后重置注入/去重/哨兵/待注入全部状态，让日记在本轮重新注入。
        try:
            umo0 = event.unified_msg_origin
            reset_flagged = umo0 in self._reset_pending
            self._reset_pending.discard(umo0)
            if reset_flagged or not req.contexts:
                cid0 = await self._curr_cid(umo0)
                # P1-4：状态按 (umo, sender) 命名空间化，命中判定用当前发送者的键
                key0 = self._inject_state_key(
                    umo0, event.get_sender_id(), bool(event.get_group_id())
                )
                prev_watch = self._watch_state.get(key0)
                if reset_flagged or (
                    cid0
                    and (
                        self._injected_cid.get(key0) == cid0
                        or (prev_watch and prev_watch.get("cid") == cid0)
                    )
                ):
                    # /reset 清的是整个会话的上下文：非会话隔离的群聊里所有发送者
                    # 都属于新一代，一并失效（其它会话的状态不受影响）。
                    for state in (
                        self._injected_cid,
                        self._injected_entries,
                        self._inject_confirm,
                        self._pending_inject,
                        self._watch_state,
                    ):
                        self._pop_session_state(state, umo0)
                    # 新上下文：上一代的占用估算已失效，状态页回退历史估算
                    self._last_context_used.pop(umo0, None)
                    logger.info(
                        "[memory_diary] 检测到 /reset（指令标记或上下文已清空），"
                        "会话代际状态已重置，本轮将重新注入日记"
                    )
        except Exception as e:
            logger.warning(f"[memory_diary] reset 代际识别异常: {e}")

        # P2-1：校准样本 E 与哨兵是否启用解耦——哨兵停用（两档阈值都 0 或
        # context_token_limit<=0）时 _watch_context 会提前返回，样本必须在它之前暂存。
        # 注入完成后由 finally 里的 _refresh_pending_estimate 按注入后的请求重算。
        await self._stage_pending_estimate(event.unified_msg_origin, req)

        # 1) 上下文哨兵
        try:
            await self._watch_context(event, req)
        except Exception as e:
            logger.warning(f"[memory_diary] 上下文哨兵异常: {e}")

        # 2) 注入日记（自动 + 手动追加）
        try:
            sender_id = event.get_sender_id()
            current_scope = str(scope_from_event(event))
            in_group = scope_from_event(event).is_group
            umo = event.unified_msg_origin
            cid = await self._curr_cid(umo)
            # P1-4：注入去重/代际状态按 (umo, sender) 命名空间化；私聊键就是 umo
            state_key = self._inject_state_key(umo, sender_id, in_group)

            # 本命名空间已注入过的篇目记录（用于手动追加注入时去重）
            rec = self._injected_entries.get(state_key)
            if not rec or rec.get("cid") != cid:
                rec = {"cid": cid, "names": set()}
                self._injected_entries[state_key] = rec
                self._cap_state(self._injected_entries)

            # a) 用户用 /注入日记 主动要求的追加注入：下一轮请求一次性带入。
            #    P2-12：手动注入是用户明确指令，不受"新会话自动读取"开关影响。
            pending = self._pending_inject.pop(state_key, None)
            pending_names: set = set()
            if pending is not None:
                scope = pending
                new_names = {
                    n
                    for n in self._select_entries(self._list_entries(sender_id), scope)
                } - rec["names"]
                if new_names:
                    extra, _, extra_names = self._assemble_inject(
                        sender_id,
                        in_group=in_group,
                        memory_scope_value=current_scope,
                        scope_override=scope,
                        only_names=new_names,
                    )
                    if extra:
                        self._deliver_inject(req, INJECT_TEMPLATE.format(diary=extra))
                        rec["names"].update(extra_names)
                        pending_names = set(extra_names)
                        logger.info(
                            f"[memory_diary] 已按用户要求追加注入 {len(extra_names)} 天日记"
                        )

            if not bool(self.config.get("enable_auto_inject", True)):
                return

            # b) 常规注入
            persist = bool(self.config.get("persist_inject", False))
            if persist:
                # 持久模式：每一轮请求都把日记放进 system_prompt，全程可回忆，
                # 代价是每轮都消耗日记的 token。
                # P2-12：本轮已被手动追加注入过的篇目不再重复塞进 system_prompt。
                diary, tampered_any, names = self._assemble_inject(
                    sender_id,
                    in_group=in_group,
                    memory_scope_value=current_scope,
                    exclude_names=pending_names,
                    lightweight=True,
                )
                if diary:
                    # 持久模式每轮都注入日记：批注只在"本代际首次注入"时同步生成一次，
                    # 之后每轮只附带已有的 open/deferred 批注块。代际状态挂在 rec 上，
                    # /reset 的代际识别会重置 rec，从而允许新代际重新生成。
                    seeded = bool(rec.get("annotation_generated"))
                    if not seeded:
                        # 先占位再 await：生成要等模型最多 annotation_timeout 秒，
                        # 期间并发第二个请求不应再触发一次生成。
                        rec["annotation_generated"] = True
                    ann_block = await self._annotation_block_for_inject(
                        sender_id, umo, in_group, diary, names,
                        is_new_generation=not seeded,
                    )
                    inject_diary = (
                        diary + "\n\n" + ann_block if ann_block else diary
                    )
                    req.system_prompt = (
                        (req.system_prompt or "")
                        + "\n\n"
                        + INJECT_TEMPLATE.format(diary=inject_diary)
                    )
                    self._mark_inject_delivered(state_key, cid)
                    rec["names"].update(names)
                elif tampered_any:
                    logger.warning(
                        f"[memory_diary] 用户 {sender_id} 的日记均被篡改，跳过注入"
                    )
                return

            # 临时模式（默认）：每个新会话的第一次 LLM 请求注入一次。
            # 不再要求 contexts 为空——/reset 后如果用户先发了 /查看日记 等命令，
            # 历史可能已经不是空的，硬性跳过会导致本会话永远注入不上。
            if self._injected_cid.get(state_key) == cid:
                return
            diary, tampered_any, names = self._assemble_inject(
                sender_id,
                in_group=in_group,
                memory_scope_value=current_scope,
                exclude_names=pending_names,
                lightweight=True,
            )
            if not diary:
                if tampered_any:
                    logger.warning(
                        f"[memory_diary] 用户 {sender_id} 的日记均被篡改，跳过注入"
                    )
                # P2-10：本轮已经用 /注入日记 的 pending 把日记塞进请求了，虽然自动
                # 注入被排除空了，也要算作"本代际已注入"，否则下一轮又会整篇重注入。
                if pending_names:
                    self._mark_inject_delivered(state_key, cid)
                return
            self._mark_inject_delivered(state_key, cid)
            rec["names"].update(names)
            # 本分支就是"本代际第一次注入日记"：此刻同步生成批注（失败静默降级），
            # 再把批注块拼在日记后一起注入；关闭态返回空串，注入文本逐字节不变。
            # 与持久模式共用 rec["annotation_generated"] 标记，保证两条路径语义一致。
            seeded = bool(rec.get("annotation_generated"))
            if not seeded:
                # 先占位再 await：生成最多等 annotation_timeout 秒，期间并发请求
                # 不应重复生成（代际状态挂在 rec 上，reset 时会重建）。
                rec["annotation_generated"] = True
            ann_block = await self._annotation_block_for_inject(
                sender_id, umo, in_group, diary, names,
                is_new_generation=not seeded,
            )
            inject_diary = diary + ("\n\n" + ann_block if ann_block else "")
            self._deliver_inject(req, INJECT_TEMPLATE.format(diary=inject_diary))
            logger.info(f"[memory_diary] 已在新会话为用户 {sender_id} 注入日记")
        except Exception as e:
            logger.warning(f"[memory_diary] 注入日记异常: {e}")
        finally:
            # 注入完成后重算本次静态估算：校准样本 E 要与真正发出去的请求同口径
            self._refresh_pending_estimate(event.unified_msg_origin, req)

    def _mark_inject_delivered(self, key: str, cid: str) -> None:
        """记录"本轮已把日记塞进请求"：等 on_llm_response 确认后才算成功（P2-12）。

        AstrBot 的 on_llm_request 在真正调用 provider 之前触发；provider 失败
        （超时/网络错误/被别的插件 stop_event 终止）时不会触发 on_llm_response，
        这一轮也进不了会话历史。所以"已注入"不能在交付时就落位——否则首轮失败
        后 `_injected_cid` 命中，整个代际再也不注入（又回到"reset 后失忆"）。
        这里只记一条"已交付待确认"；on_llm_response 成功时才写入 `_injected_cid`。
        老版本 AstrBot 没有 on_llm_response 钩子时退回旧行为（交付即确认）。
        """
        if not _HAS_RESPONSE_HOOK:
            self._injected_cid[key] = cid
            self._cap_state(self._injected_cid)
            return
        self._inject_confirm[key] = cid
        self._cap_state(self._inject_confirm)

    async def _handle_on_llm_response(self, event: AstrMessageEvent, resp):
        """拿到本次请求的真实 token 用量，对估算系数做 EWMA 自适应校准。（``main.on_llm_response`` 的实现）"""
        try:
            # P2-12：拿到响应说明这一轮请求真的成功了，此刻才确认"本代际已注入"。
            # 这一步与校准开关无关，必须在 _calibration_enabled 判断之前执行。
            umo = event.unified_msg_origin
            state_key = self._inject_state_key(
                umo, event.get_sender_id(), bool(event.get_group_id())
            )
            confirmed_cid = self._inject_confirm.pop(state_key, None)
            if confirmed_cid is not None:
                self._injected_cid[state_key] = confirmed_cid
                self._cap_state(self._injected_cid)
            if not self._calibration_enabled():
                return
            pending = self._pending_estimates.pop(umo, None)
            if not pending:
                return
            usage = getattr(resp, "usage", None)
            if usage is None:
                return
            actual = int(getattr(usage, "input_other", 0) or 0) + int(
                getattr(usage, "input_cached", 0) or 0
            )
            # P3-13：多步工具 agent 的样本口径不一致——E 是首个请求的静态估算，
            # actual 是 agent 最后一步（含累计工具结果）的真实用量，两者配对会把
            # 系数系统性往高处推。AstrBot 在每次工具执行后往 ProviderRequest 的
            # tool_calls_result 里追加结果（同一次请求的同一个 req 对象），因此
            # on_llm_response 时能据此判定"这一轮跑过多步工具"，直接跳过采样。
            req_ref = pending.get("req_ref")
            req = req_ref() if callable(req_ref) else None
            if req is not None and getattr(req, "tool_calls_result", None):
                logger.debug(
                    "[memory_diary] 多步工具 agent：本轮 usage 与首步静态估算"
                    "口径不一致，跳过本次校准采样"
                )
                return
            self._update_calibration(
                str(pending.get("key") or ""),
                int(pending.get("estimate", 0) or 0),
                actual,
            )
        except Exception as e:
            logger.warning(f"[memory_diary] token 估算校准异常: {e}")

    @staticmethod
    def _deliver_inject(req: ProviderRequest, inject_text: str):
        """把注入文本塞进本次请求：优先临时 extra_user_content_parts，失败退回 system_prompt。"""
        try:
            if TextPart is None:
                raise RuntimeError("TextPart 不可用")
            part = TextPart(text=inject_text)
            try:
                part = part.mark_as_temp()  # 不持久化进会话历史
            except Exception:
                pass
            req.extra_user_content_parts.append(part)
        except Exception:
            req.system_prompt = (req.system_prompt or "") + "\n\n" + inject_text
