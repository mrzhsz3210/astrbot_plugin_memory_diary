# -*- coding: utf-8 -*-
"""指令实现：/日记密码、/日记状态、/日记范围、/注入日记。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

from astrbot.api.event import AstrMessageEvent

from ..memory_diary.log import logger


class SettingsCommandsMixin:
    """指令实现：/日记密码、/日记状态、/日记范围、/注入日记。"""

    async def _handle_diary_password(self, event: AstrMessageEvent):
        """设置/修改/取消 Web 日记本页面的查看密码（仅私聊、仅本人）。（``main.diary_password`` 的实现）"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 密码设置只能私聊操作哦。",
                    "用户在群里想设置日记本查看密码。请用你自己的口吻委婉拒绝："
                    "密码是私密设置，请他来私聊操作。",
                )
            )
            return
        sender_id = event.get_sender_id()
        # message_str 含指令名本身（"/日记密码 设置 xxx"），先剥掉第一个 token。
        # 这里不用 _command_arg：它会剥 @提及，可能误伤含 @ 的密码；
        # 但零宽/方向控制字符必须先剥掉，否则密码会带着看不见的字符存进哈希。
        tokens = self._strip_invisible(event.message_str or "").strip().split()
        parts = tokens[1:]
        data = self._load_pref_data(sender_id)
        cur = data.get("view_password")
        cur = cur if isinstance(cur, dict) and cur.get("hash") else None

        usage = (
            "📖 查看密码用法：\n"
            "· /日记密码 设置 新密码 —— 给 Web 日记本页面加锁\n"
            "· /日记密码 修改 旧密码 新密码\n"
            "· /日记密码 取消 旧密码 —— 去掉锁\n"
            "（密码 4~32 位、不含空格；只防管理面板里的随手翻看，"
            "防不了有服务器文件权限的人哦）"
        )
        if not parts:
            state = "已设置" if cur else "未设置"
            yield event.plain_result(f"📖 你的日记查看密码：{state}。\n\n{usage}")
            return

        action = parts[0]

        def _valid(pw: str) -> bool:
            return 4 <= len(pw) <= 32 and not any(c.isspace() for c in pw)

        if action == "设置" and len(parts) == 2:
            if cur:
                yield event.plain_result(
                    await self._say(
                        event.unified_msg_origin,
                        "📖 你已经设置过密码啦，想换用 /日记密码 修改 旧密码 新密码。",
                        "用户想设置查看密码，但之前已经设置过了。请用你自己的口吻告诉他，"
                        "想换的话用 /日记密码 修改 旧密码 新密码（必须保留这条指令），"
                        "两三句话以内。",
                    )
                )
                return
            if not _valid(parts[1]):
                yield event.plain_result(
                    await self._say(
                        event.unified_msg_origin,
                        "📖 密码要 4~32 位、不含空格哦。",
                        "用户设置的查看密码不符合要求。请用你自己的口吻提醒他："
                        "密码必须 4~32 位、不含空格（必须保留这两个要求）。",
                    )
                )
                return
            data["view_password"] = self._make_view_password(parts[1])
            if not self._save_pref_data(sender_id, data):
                yield event.plain_result("📖 保存失败（数据目录不可写），密码未设置。")
                return
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 设置好啦。以后在 Web 日记本页面翻看或导出你的日记，都要先输入这个密码。",
                    "用户刚设置好 Web 日记本页面的查看密码。请用你自己的口吻确认设置成功，"
                    "并提醒他：以后在面板里翻看或导出日记都要先输入这个密码。两三句话以内。",
                )
            )
            return

        if action == "修改" and len(parts) == 3:
            if not cur:
                yield event.plain_result(
                    await self._say(
                        event.unified_msg_origin,
                        "📖 你还没设置过密码，直接用 /日记密码 设置 新密码。",
                        "用户想修改查看密码，但还没设置过。请用你自己的口吻告诉他，"
                        "先用 /日记密码 设置 新密码（必须保留这条指令）。",
                    )
                )
                return
            if not self._verify_view_password(cur, parts[1]):
                yield event.plain_result(
                    await self._say(
                        event.unified_msg_origin,
                        "📖 旧密码不对哦。",
                        "用户修改查看密码时旧密码输错了。请用你自己的口吻如实告诉他"
                        "旧密码不对、这次没有修改。",
                    )
                )
                return
            if not _valid(parts[2]):
                yield event.plain_result(
                    await self._say(
                        event.unified_msg_origin,
                        "📖 新密码要 4~32 位、不含空格哦。",
                        "用户设置的新密码不符合要求。请用你自己的口吻提醒他："
                        "新密码必须 4~32 位、不含空格（必须保留这两个要求）。",
                    )
                )
                return
            data["view_password"] = self._make_view_password(parts[2])
            if not self._save_pref_data(sender_id, data):
                yield event.plain_result("📖 保存失败（数据目录不可写），密码未改动。")
                return
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 密码改好啦。",
                    "用户的查看密码已经改好了。请用你自己的口吻确认一下，一两句话即可。",
                )
            )
            return

        if action == "取消" and len(parts) == 2:
            if not cur:
                yield event.plain_result(
                    await self._say(
                        event.unified_msg_origin,
                        "📖 你本来就没有设置密码哦。",
                        "用户想取消查看密码，但他本来就没设置过密码。"
                        "请用你自己的口吻如实告诉他。",
                    )
                )
                return
            if not self._verify_view_password(cur, parts[1]):
                yield event.plain_result(
                    await self._say(
                        event.unified_msg_origin,
                        "📖 密码不对，没有取消。",
                        "用户取消查看密码时密码输错了。请用你自己的口吻告诉他"
                        "密码不对、密码没有被取消。",
                    )
                )
                return
            data.pop("view_password", None)
            if not self._save_pref_data(sender_id, data):
                yield event.plain_result("📖 保存失败（数据目录不可写），密码未改动。")
                return
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 查看密码已取消。",
                    "用户的查看密码已经取消，Web 日记本页面不再需要密码。"
                    "请用你自己的口吻确认一下，一两句话即可。",
                )
            )
            return

        # 参数没看懂：记 repr 方便定位零宽/方向控制字符（密码指令不在 _command_arg 链路里）
        logger.warning(
            f"[memory_diary] 无法识别的日记密码参数，原始消息："
            f"{(event.message_str or '')!r}"
        )
        yield event.plain_result(usage)

    async def _handle_diary_status(self, event: AstrMessageEvent):
        """查看当前上下文占用、日记更新时间和哨兵状态。（``main.diary_status`` 的实现）"""
        if not self._should_respond(event):
            return
        sender_id = event.get_sender_id()
        umo = event.unified_msg_origin
        # 群聊里不展示个人日记统计与实例级校准观测（同群可见，属元数据暴露）；
        # 上下文占用/哨兵状态是群会话本身的信息，保留展示。
        in_group = bool(event.get_group_id())

        # 日记信息：先做全量 zip 健康检查，损坏时不能显示“0 字，校验通过”
        corrupt = self._corrupt_notice(sender_id)
        if corrupt:
            latest_content, latest_tampered, latest_name = "", False, None
            entries = []
        else:
            latest_content, latest_tampered, latest_name = self._load_latest(sender_id)
            entries = self._list_entries(sender_id)
        if corrupt:
            diary_info = (
                "⚠️ 日记文件已损坏（CRC 校验失败或无法打开；"
                "写入时会自动隔离，请管理员检查数据目录）"
            )
        elif entries:
            if latest_tampered:
                diary_info = (
                    f"共 {len(entries)} 天的日记，最新 {self._entry_label(latest_name)} "
                    "—— ⚠️ 验签失败，疑似被篡改，已失效"
                )
            else:
                diary_info = (
                    f"共 {len(entries)} 天的日记，最新为 {self._entry_label(latest_name)}"
                    f"（{len(latest_content)} 字，校验通过）"
                )
        elif latest_tampered:
            diary_info = "⚠️ 验签失败，疑似被篡改，已失效"
        else:
            diary_info = "还没有写过日记"

        if in_group:
            # 日记篇数/最新日期属于个人元数据，群聊里同群可见——只提示去私聊看
            diary_info = "群聊不展示个人日记统计（私聊发送 /日记状态 查看）"

        # 上下文占用估算（v2 下与哨兵同口径：复用最近一次请求的估算）
        # 窗口 0/负数 = 哨兵与比例门槛停用（P?: 原 `or 60000` 会把 0 吞成默认值）
        limit = self._context_limit()
        remind_ratio, auto_ratio = self._watch_ratios()
        used = await self._context_used(umo)
        pct = int(used / limit * 100) if limit > 0 else 0
        # 阈值显示用原始配置值（哨兵关闭时也如实展示配置），生效与否看"本会话哨兵"行
        def _cfg_ratio(name, default):
            try:
                v = float(self.config.get(name, default))
            except (TypeError, ValueError):
                v = default
            return int(min(max(v, 0.0), 1.0) * 100)

        remind_pct = _cfg_ratio("remind_ratio", 0.75)
        auto_pct = _cfg_ratio("auto_ratio", 0.9)

        # 哨兵状态
        cid = await self._curr_cid(umo)
        state = self._watch_state.get(umo) or {}
        watch_ok = (remind_ratio > 0 or auto_ratio > 0) and limit > 0
        if watch_ok and state.get("cid") == cid:
            flags = []
            if state.get("reminded"):
                flags.append("已提醒")
            if state.get("auto_written"):
                flags.append("已自动写日记")
            watch_info = "、".join(flags) if flags else "未触发"
        elif watch_ok:
            watch_info = "未触发"
        else:
            watch_info = "已关闭"

        # 注入状态：本会话有没有实际把日记塞进她的请求里
        if not entries:
            inject_info = "无日记可注入"
        elif not bool(self.config.get("enable_auto_inject", True)):
            inject_info = "已关闭（配置面板关了自动读取）"
        elif self._injected_cid.get(umo) == cid:
            inject_info = "本会话已注入 ✅"
        else:
            inject_info = "本会话未注入（你下一句正常对话时注入；命令消息不触发）"

        # 上下文占用行：v2 显示"估算 X / Y tokens"并附校准观测行；旧公式保持原格式
        # 窗口 0/负数时无法换算百分比：明确提示比例体系已停用，不显示会误导的 0%
        # 群聊里不显示校准观测行（provider::model / 样本数 / 上次实际是实例级元数据）
        if limit <= 0:
            context_line = (
                "· 当前上下文：模型上下文窗口配置为 0/负数，"
                f"哨兵与比例门槛已停用（估算 {used:,} tokens）"
            )
            # 校准与哨兵启停解耦（P2-1）：窗口停用但校准仍照常采样，观测行保留（群聊除外）
            calib_line = (
                self._calib_status_line(await self._context_calib_key(umo))
                if self._token_estimation_v2() and not in_group
                else ""
            )
        elif self._token_estimation_v2():
            context_line = f"· 当前上下文：估算 {used:,} / {limit:,} tokens（{pct}%）"
            calib_line = (
                ""
                if in_group
                else self._calib_status_line(await self._context_calib_key(umo))
            )
        else:
            context_line = f"· 当前上下文：约 {used}/{limit} tokens（{pct}%，估算值）"
            calib_line = ""

        lines = [
            "📖 日记状态：",
            f"· 日记：{diary_info}",
            context_line,
        ]
        if calib_line:
            lines.append(calib_line)
        lines += [
            f"· 阈值：提醒 {remind_pct}% / 自动写日记 {auto_pct}%",
            f"· 本会话哨兵：{watch_info}",
            f"· 本会话日记注入：{inject_info}",
        ]
        yield event.plain_result("\n".join(lines))

    # ------------------------- 指令：注入范围 -------------------------

    async def _handle_diary_scope(self, event: AstrMessageEvent, scope: str = ""):
        """设置新上下文注入哪些日记：最新 / 全部 / 最近N天 / 时间段。（``main.diary_scope`` 的实现）"""
        if not self._should_respond(event):
            return
        sender_id = event.get_sender_id()
        # P2-3：不使用被空格截断的 scope 参数，直接解析原始消息（支持带空格的时间段写法）
        arg = self._command_arg(event)
        low = arg.lower()

        if not arg:
            cur = self._load_pref(sender_id)
            if cur:
                desc = self._scope_desc(cur)
            else:
                default_days = int(self.config.get("inject_default_days", 7) or 0)
                desc = (
                    "全部日记（管理员默认）"
                    if default_days <= 0
                    else f"最近 {default_days} 天（默认）"
                )
            yield event.plain_result(
                f"📖 当前设置：新上下文注入【{desc}】。\n\n"
                "修改方式：\n"
                "· /日记范围 最新 —— 只读最新一天\n"
                "· /日记范围 全部 —— 读取所有日记\n"
                "· /日记范围 最近7天 —— 读取最近 N 天\n"
                "· /日记范围 2026-10-01到2026-10-23 —— 读取指定时间段\n"
                "· /日记范围 默认 —— 恢复管理员配置的默认范围\n\n"
                "注入总量有上限保护，超出会优先保留最近的日记。"
            )
            return

        parsed = "KEEP"
        if low in ("默认", "default"):
            parsed = None  # 清除偏好，回到管理员默认
        else:
            p = self._parse_scope(arg)
            if p is not None:
                parsed = p

        if parsed == "KEEP":
            logger.warning(
                f"[memory_diary] 无法识别的日记范围参数，原始消息："
                f"{(event.message_str or '')!r}"
            )
            yield event.plain_result(
                "📖 没看懂～可选：最新 / 全部 / 最近N天（如 最近7天）/ 日期（如 2026-10-23）/ 时间段（如 2026-10-01到2026-10-23）。"
            )
            return

        # P2-5b：保存失败必须明说，不能谎报"已设置"
        if not self._save_pref(sender_id, parsed):
            yield event.plain_result(
                "📖 保存失败（数据目录不可写或磁盘已满），注入范围没有改动——"
                "请让管理员检查插件数据目录后重试。"
            )
            return
        if parsed is None:
            default_days = int(self.config.get("inject_default_days", 7) or 0)
            desc = (
                "全部日记（管理员默认）"
                if default_days <= 0
                else f"最近 {default_days} 天（管理员默认）"
            )
        else:
            desc = self._scope_desc(parsed)
        canned = (
            f"✅ 已设置：新上下文将注入【{desc}】。"
            "下次 /reset 后生效；注入总量有上限保护，超出会优先保留最近的日记。"
        )
        yield event.plain_result(
            await self._say(
                event.unified_msg_origin,
                canned,
                f"用户刚设置了新上下文要注入的日记范围：【{desc}】。"
                f"请用你自己的口吻确认设置成功（必须原样保留范围「{desc}」），"
                "并提醒ta下次 /reset 后生效。两三句话以内。",
            )
        )

    # ------------------------- 指令：追加注入 -------------------------

    async def _handle_diary_inject(self, event: AstrMessageEvent, scope: str = ""):
        """主动把指定日记注入接下来的对话（本会话去重，下一句对话时一次性生效）。（``main.diary_inject`` 的实现）"""
        if not self._should_respond(event):
            return
        sender_id = event.get_sender_id()
        # P2-3：直接解析原始消息，支持带空格的时间段写法
        arg = self._command_arg(event)

        corrupt = self._corrupt_notice(sender_id)
        if corrupt:
            yield event.plain_result(corrupt)
            return
        entries = self._list_entries(sender_id)
        if not entries:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 还没有日记可以注入，先让我写一篇吧（/日记）。",
                    "用户想让你主动读日记，但还一篇都没写过。请用你自己的口吻告诉他"
                    "现在还没有日记，并邀请他先发 /日记 让你写一篇"
                    "（必须保留 /日记 这条指令）。",
                )
            )
            return
        if not arg:
            yield event.plain_result(
                "📖 主动注入用法：/注入日记 最新 / 全部 / 最近7天 / 2026-10-23 / 2026-10-20到2026-10-23\n"
                "注入会在你下一句正常对话时生效（临时一次性带入，不长期占用上下文）；\n"
                "本会话已经注入过的篇目不会重复注入。"
            )
            return

        parsed = self._parse_scope(arg)
        if parsed is None:
            logger.warning(
                f"[memory_diary] 无法识别的注入范围参数，原始消息："
                f"{(event.message_str or '')!r}"
            )
            yield event.plain_result(
                "📖 没看懂～可选：最新 / 全部 / 最近N天 / 日期 / 时间段。"
            )
            return

        targets = self._select_entries(entries, parsed)
        if not targets:
            # P2-2：显式日期范围没有日记时明说，不静默替换成最新一篇
            scope_desc = self._scope_desc(parsed)
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    f"📖 【{scope_desc}】里没有日记哦，换个范围试试。",
                    f"用户想注入【{scope_desc}】的日记，但这个范围里一篇日记都没有。"
                    f"请用你自己的口吻告诉他这个范围里没有日记"
                    f"（必须原样保留范围名称：{scope_desc}），可以建议换个范围。",
                )
            )
            return
        umo = event.unified_msg_origin
        cid = await self._curr_cid(umo)
        rec = self._injected_entries.get(umo)
        if not rec or rec.get("cid") != cid:
            rec = {"cid": cid, "names": set()}
            self._injected_entries[umo] = rec
        already = rec["names"]
        new_targets = [t for t in targets if t not in already]
        if not new_targets:
            canned = f"📖 【{self._scope_desc(parsed)}】的日记在本会话都已经读过了，没有新篇目需要注入。"
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    canned,
                    f"用户想让你再读【{self._scope_desc(parsed)}】的日记，但这些你在本会话都已经读过了。"
                    "请用你自己的口吻告诉ta：这些我都记得啦，不用重复读。两三句话以内。",
                )
            )
            return

        self._pending_inject[umo] = parsed
        self._cap_state(self._pending_inject)
        labels = "、".join(self._entry_label(t) for t in new_targets)
        dup_note = (
            f"（{len(targets) - len(new_targets)} 天本会话已读过，自动跳过）"
            if len(new_targets) < len(targets)
            else ""
        )
        said = await self._say(
            event.unified_msg_origin,
            "",
            f"用户要求你在下一句对话前先读这些天的日记：{labels}。"
            f"请用你自己的口吻答应下来（必须原样报出日期：{labels}），"
            "告诉ta下一句聊天时你就会先读这些日记。两三句话以内。",
        )
        if not said:
            said = (
                f"📖 好的，你下一句正常对话时，我会先读【{self._scope_desc(parsed)}】：{labels}。\n"
                "注入是临时一次性的：只带入紧接着的那一轮回复，不长期占用上下文；\n"
                "想让她在整个会话里一直记得，可在配置里开「注入持久化」。"
            )
        yield event.plain_result(said + (f"\n{dup_note}" if dup_note else ""))
