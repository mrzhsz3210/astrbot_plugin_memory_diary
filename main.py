# -*- coding: utf-8 -*-
"""astrbot_plugin_memory_diary —— 记忆日记（AstrBot 适配层）。

业务逻辑已拆分到 memory_diary 包：
  constants / errors / common / log / storage / service / core

本文件只保留 AstrBot 相关的装饰器、指令处理、事件钩子与可视化视图 API。
这样主类仍在 main.py 中，AstrBot 按模块路径注册 handler 的行为不变。
"""

import asyncio
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import zipfile
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star, register

from .memory_diary.constants import CHARS_PER_TOKEN, INJECT_TEMPLATE, PLUGIN_NAME
from .memory_diary.core import MemoryDiaryCore
from .memory_diary.errors import DiaryKeyUnavailable
from .memory_diary.log import logger

try:
    from astrbot.api.message_components import At
except ImportError:  # pragma: no cover
    At = None

try:
    from astrbot.api.star import StarTools  # v3.5+
except ImportError:  # pragma: no cover
    StarTools = None

try:
    from astrbot.core.agent.message import TextPart  # v4.x
except ImportError:  # pragma: no cover
    TextPart = None

try:
    from astrbot.core.utils.session_waiter import SessionController, session_waiter

    _SESSION_WAITER_OK = True
except ImportError:  # pragma: no cover
    SessionController = None
    session_waiter = None
    _SESSION_WAITER_OK = False

try:
    # 插件可视化视图后端（AstrBot 较新版本提供；旧版本自动降级为无页面）
    from astrbot.api.web import (
        PluginUploadFile as _PluginUploadFile,
    )
    from astrbot.api.web import (
        error_response as _web_error,
    )
    from astrbot.api.web import (
        file_response as _web_file,
    )
    from astrbot.api.web import (
        json_response as _web_json,
    )
    from astrbot.api.web import (
        request as _web_request,
    )

    _WEB_API_OK = True
except ImportError:  # pragma: no cover
    _PluginUploadFile = None
    _web_error = _web_file = _web_json = _web_request = None
    _WEB_API_OK = False


def resolve_data_dir() -> Path:
    """数据目录：<AstrBot>/data/plugin_data/astrbot_plugin_memory_diary/。"""
    try:
        if StarTools is not None:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:
        pass
    return Path("data/plugin_data") / PLUGIN_NAME


@register(
    "astrbot_plugin_memory_diary",
    "mrzhsz3210",
    "指令/定时自动触发写日记（用户画像 / 对用户的评价 / 给未来自己的叮嘱），新上下文自动读取日记，上下文将满时自动提醒，支持每天定时自动写完整日记。",
    "v0.5.4",
)

class MemoryDiaryPlugin(MemoryDiaryCore, Star):
    """AstrBot 插件入口：只保留指令/事件/Web 适配，业务逻辑在 memory_diary 包。"""

    def __init__(self, context, config=None):
        super().__init__(context, config, data_dir=resolve_data_dir())
        # 可视化视图（日记本页面）后端 API；不支持的 AstrBot 版本自动跳过
        try:
            self._register_web_apis()
        except Exception as e:
            logger.warning(f"[memory_diary] 注册 Web API 失败，日记本页面不可用: {e}")

    # ------------------------- 群聊多 AI 防冲突 -------------------------

    def _bot_names(self) -> list:
        """配置的"我的称呼"列表（用于文本形式 @名字 的比对）。"""
        raw = str(self.config.get("bot_call_names", "") or "")
        return [n.strip().lstrip("@") for n in re.split(r"[,，、]", raw) if n.strip()]

    def _should_respond(self, event: AstrMessageEvent) -> bool:
        """判断这条指令是不是发给我的（群里多个 AI 都装了本插件时防冲突）。

        - 私聊：永远响应；
        - 群里 @了我（QQ 的 At 组件指向我，或文本 @名字 命中配置的"我的称呼"）：响应；
        - 群里明确 @了别人（At 组件不是我、或文本 @名字 不在我的称呼里）：沉默，
          让被点名的那个 AI 响应；
        - 群里没有任何 @：响应（广义兼容模式），除非配置开启"群里只响应@我的指令"。
        """
        if not event.get_group_id():
            return True
        self_id = ""
        try:
            self_id = str(event.get_self_id())
        except Exception:
            pass
        at_me, at_other = False, False
        if At is not None:
            try:
                for comp in event.get_messages():
                    if isinstance(comp, At):
                        if self_id and str(comp.qq) == self_id:
                            at_me = True
                        else:
                            at_other = True
            except Exception:
                pass
        if at_me:
            return True
        # 文本形式的点名只看"指令本体"（第一个空白前的 token，如 "/写日记@小深"）：
        # 内容里出现的 @（例如 /记一下 记得@张三 他下周考试）不算点名，避免误判沉默
        full_text = (event.message_str or "").strip()
        cmd_token = full_text.split(None, 1)[0] if full_text else ""
        m = re.search(r"@([^\s@，,]+)", cmd_token)
        if m:
            target = m.group(1)
            names = self._bot_names()
            # 文本 @命中我的称呼才响应；@的是别人（或无法确认是我）一律沉默
            return any(target.lower() == n.lower() for n in names)
        if at_other:
            return False
        return not bool(self.config.get("group_require_at", False))

    def _command_arg(self, event: AstrMessageEvent) -> str:
        """从原始消息取指令后的完整参数，并剥掉 @提及。

        P2-3 修复：AstrBot 的 CommandFilter 按空格拆参数，只把第一个 token 传给
        处理函数的参数——"/日记范围 2026-10-01 到 2026-10-23" 会被截成单日。
        这里直接解析 event.message_str，保留带空格的完整参数。
        """
        text = (event.message_str or "").strip()
        parts = text.split(maxsplit=1)
        arg = parts[1] if len(parts) > 1 else ""
        if not arg and "　" in text:  # 兼容全角空格
            arg = text.split("　", 1)[1]
        return re.sub(r"@[^\s@，,]+", "", arg).strip()
    # ------------------------- 指令：帮助 -------------------------

    @filter.command("日记帮助", alias={"diary_help"})
    async def diary_help(self, event: AstrMessageEvent):
        """查看记忆日记插件的全部指令与用法。"""
        if not self._should_respond(event):
            return
        yield event.plain_result(
            "📖 记忆日记 · 指令一览\n"
            "━━━━━━━━━━━━━━\n"
            "· /写日记 —— 默认写简记（第一次写日记会写完整）\n"
            "· /写日记 完整 —— 写完整日记，并合并同场景之前的简记\n"
            "· /写日记 简记 —— 强制只写一段简记\n"
            "· /记一下 内容 —— 把重要的事存为备忘，下次写日记重点记进去（指令和内容空格分开）\n"
            "· /查看备忘 —— 私聊专用：查看待写入的备忘（带场景标注）\n"
            "· /删除备忘 —— 私聊专用：按序号删一条，或回复「全部」清空\n"
            "· /查看日记 —— 私聊专用：列出日期后按提示查看某天的日记\n"
            "· /删除日记 —— 私聊专用：删除全部 / 某天 / 某时间段的日记\n"
            "· /导出日记 —— 私聊：把自己的日记和签名钥匙打包发给你，用于换设备迁移\n"
            "· /导入日记 —— 私聊：把迁移包发给我，验签通过后自动安放日记\n"
            "· /日记状态 —— 查看上下文占用、日记篇数、哨兵状态\n"
            "· /日记范围 —— 设置新上下文注入哪些日记：最新 / 全部 / 最近N天 / 时间段 / 默认\n"
            "· /注入日记 范围 —— 主动让她现在就读指定日记（下一句对话生效，本会话去重）\n"
            "· /日记帮助 —— 就是你现在看到的这个\n"
            "━━━━━━━━━━━━━━\n"
            "小提示：上下文快满时我会主动提醒并自动写好日记；\n"
            "写完发 /reset，我会先读一遍日记再继续陪你。\n"
            + (
                "本群多 AI 防冲突已生效：在指令后 @我 才是叫我哦。"
                if event.get_group_id()
                else ""
            )
        )

    # ------------------------- 指令：记一下（备忘） -------------------------

    @filter.command("memo", alias={"记一下", "记住"})
    async def memo_cmd(self, event: AstrMessageEvent):
        """把一件重要的事记下来，下次写日记时重点写进画像。用法：/记一下 要记住的事"""
        if not self._should_respond(event):
            return
        sender_id = event.get_sender_id()
        text = (event.message_str or "").strip()
        # 指令与内容在同一句话内、用空格分开：去掉第一个词（指令本体）
        parts = text.split(maxsplit=1)
        note = parts[1].strip() if len(parts) > 1 else ""
        # 兼容用户把指令和内容用全角空格分开的情况
        if not note and "　" in text:
            note = text.split("　", 1)[1].strip()
        # 群里点名写法（/记一下 @我 内容）：把 @我的部分从内容里剥掉
        for n in self._bot_names():
            if note.startswith("@" + n):
                note = note[len(n) + 1 :].strip(" ，,")
                break

        if not note:
            yield event.plain_result(
                "在指令后面空一格，写上要我记住的事就可以啦，例如：\n"
                "/记一下 我下周三要考试\n"
                "下次写日记时我会把它重点记进去。"
            )
            return

        is_group = bool(event.get_group_id())
        total, saved = self._append_memo(
            sender_id, note, scene="group" if is_group else "private"
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

    @filter.command("查看备忘", alias={"memo_view"})
    async def memo_view(self, event: AstrMessageEvent):
        """查看待写入日记的备忘（仅私聊）：列出全部备忘及其场景。"""
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
        sender_id = event.get_sender_id()
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

    @filter.command("删除备忘", alias={"memo_delete"})
    async def memo_delete(self, event: AstrMessageEvent):
        """删除备忘（仅私聊）：先列出，再回复序号删一条或「全部」清空，60 秒内有效。"""
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
        sender_id = event.get_sender_id()
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
            text = (ev.message_str or "").strip()
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
                idx = int(text)
                if 1 <= idx <= len(cur):
                    target = cur[idx - 1]
                    # 按内容+时间定位删除，避免序号在两次读取间漂移
                    rest = [
                        m
                        for m in cur
                        if not (
                            m.get("text") == target.get("text")
                            and m.get("time") == target.get("time")
                            and self._memo_scene(m) == self._memo_scene(target)
                        )
                    ]
                    data = self._load_pref_data(sender_id)
                    if rest:
                        data["memos"] = rest
                    else:
                        data.pop("memos", None)
                    if not self._save_pref_data(sender_id, data):
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
                else:
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

    # ------------------------- 指令：写日记 -------------------------

    @filter.command("diary", alias={"日记", "写日记"})
    async def write_diary_cmd(self, event: AstrMessageEvent):
        """写日记：默认简记；/日记 完整 写完整篇；/日记 简记 强制简记。"""
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

        # 阈值门：上下文占用未达配置比例时不允许手动写日记（0 为不限制）
        min_ratio = float(self.config.get("write_min_ratio", 0) or 0)
        if min_ratio > 0:
            limit = int(self.config.get("context_token_limit", 60000) or 60000)
            history = await self._get_history(umo)
            chars = 0
            for msg in history:
                if isinstance(msg, dict):
                    chars += len(self._content_to_text(msg.get("content", "")))
                else:
                    chars += len(str(msg))
            ratio = (chars / CHARS_PER_TOKEN) / limit if limit > 0 else 0
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

        diary_content, err = await self._write_diary(
            umo,
            sender_id,
            sender_name,
            is_group=bool(event.get_group_id()),
            requested_mode=requested_mode,
        )
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

        if (
            bool(self.config.get("show_diary_after_write", False))
            and not event.get_group_id()
        ):
            # 日记全文只在私聊展示，群聊不展示，避免泄露给其他成员
            notice += f"\n\n—— 日记全文 ——\n{diary_content}"

        yield event.plain_result(notice)

    # ------------------------- 指令：查看 / 删除 -------------------------

    @filter.command("查看日记", alias={"diary_view"})
    async def view_diary(self, event: AstrMessageEvent):
        """查看日记（仅本人、仅私聊）：先列出时间，再按选择发送对应内容。"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 日记是只给本人看的哦，请私聊我查看～",
                    "用户在群里想看日记，你要委婉地拒绝：日记是只给本人看的私密内容，请他来私聊再看。",
                )
            )
            return

        sender_id = event.get_sender_id()
        # 复审 12：密钥不可用时不要显示成"被篡改"——验签根本做不了，先说明
        if self._secret_key() is None:
            yield event.plain_result(
                "📖 签名密钥（.diary_key）不可读，我没法验签，"
                "也就不能确定日记内容是不是真的——请管理员检查插件数据目录里的密钥文件。"
            )
            return
        # 先做全量 zip 健康检查：中央目录还在、单篇 CRC 坏掉时，
        # is_zipfile() 仍会返回 True，必须先 testzip() 拦下来。
        corrupt = self._corrupt_notice(sender_id)
        if corrupt:
            yield event.plain_result(corrupt)
            return
        latest_content, latest_tampered, latest_name = self._load_latest(sender_id)
        if latest_tampered and latest_name is None:
            # 验签失败属于安全结论，保持固定文案（与同一指令其它验签提示一致）
            yield event.plain_result(
                "📖 日记在插件之外被修改过，已被判定失效、不再使用。"
                "发送 /日记 让我重新写一篇吧。"
            )
            return
        entries = self._list_entries(sender_id)
        if not entries:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 还没有写过日记。发送 /日记 让我现在写一篇吧。",
                    "用户想看日记，但还一篇都没写过。用你自己的口吻告诉他，"
                    "并邀请他发 /日记 让你写第一篇。",
                )
            )
            return

        lines = []
        for i, name in enumerate(entries, 1):
            tag = "（最新）" if name == entries[-1] else ""
            lines.append(f"{i}. {self._entry_label(name)}{tag}")
        yield event.plain_result(
            f"📖 你一共有 {len(entries)} 天的日记：\n"
            + "\n".join(lines)
            + "\n\n回复「最新」读最新一天的；回复序号读对应那天的；"
            "回复日期（如 2026-10-23）读那天的日记。60 秒内有效，回复「取消」退出。"
        )

        if not _SESSION_WAITER_OK:
            # 环境不支持会话等待：直接发最新一篇，并提示可用序号/日期参数
            if latest_tampered:
                yield event.plain_result(
                    "📖 最新一篇验签失败，疑似被篡改，已拒绝读取。"
                )
            else:
                yield event.plain_result(
                    f"📖 {self._entry_label(entries[-1])} 的日记：\n\n{latest_content}"
                )
            return

        @session_waiter(timeout=60, record_history_chains=False)
        async def diary_view_waiter(
            controller: SessionController, ev: AstrMessageEvent
        ):
            text = (ev.message_str or "").strip()
            low = text.lower()
            if self._is_cancel(text):
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 好，不看啦～想再看随时发 /查看日记。",
                            "用户取消了查看日记。用你自己的口吻轻松回应一下，"
                            "告诉他想看随时再来。",
                        )
                    )
                )
                controller.stop()
                return
            targets = None
            if low in ("最新", "latest", "new", "newest"):
                targets = [entries[-1]]
            elif text.isdigit():
                idx = int(text)
                if 1 <= idx <= len(entries):
                    targets = [entries[idx - 1]]
            else:
                m = re.match(
                    r"^(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?$", text
                )
                if m:
                    y, mo, dd = (int(g) for g in m.groups())
                    name = self._md_entry(f"{y:04d}{mo:02d}{dd:02d}")
                    targets = [name] if name in entries else []

            if targets is None:
                await ev.send(
                    ev.plain_result(
                        "📖 没看懂～回复「最新」、序号，或日期（如 2026-10-23）。"
                    )
                )
                controller.keep(timeout=60, reset_timeout=True)
                return
            if not targets:
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 那一天没有写日记哦。",
                            "用户指定的那一天没有写过日记，用你自己的口吻如实告诉他。",
                        )
                    )
                )
                controller.stop()
                return
            for name in targets:
                content, tampered = self._load_entry(sender_id, name)
                if tampered:
                    await ev.send(
                        ev.plain_result(
                            f"📖 {self._entry_label(name)} 这篇日记验签失败，疑似被篡改，已拒绝读取。"
                        )
                    )
                else:
                    await ev.send(
                        ev.plain_result(
                            f"📖 {self._entry_label(name)} 的日记：\n\n{content}"
                        )
                    )
            controller.stop()

        try:
            await diary_view_waiter(event)
        except TimeoutError:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 查看超时啦，想再看随时发 /查看日记。",
                    "查看日记的互动超时了。用你自己的口吻告诉他，想看的话随时再发 /查看日记。",
                )
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 查看日记交互异常: {e}")
        finally:
            event.stop_event()

    @filter.command("删除日记", alias={"diary_delete"})
    async def delete_diary(self, event: AstrMessageEvent):
        """删除日记（仅本人、仅私聊）：可选全部 / 某天 / 时间段。"""
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

        sender_id = event.get_sender_id()
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
        yield event.plain_result(
            f"📖 你目前有 {len(entries)} 天的日记：\n"
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
            text = (ev.message_str or "").strip()
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
                        await ev.send(
                            ev.plain_result(
                                await self._say(
                                    ev.unified_msg_origin,
                                    "📖 全部日记已删除。",
                                    "用户确认后，他的全部日记已经删掉了。用你自己的口吻确认这件事，"
                                    "语气体贴一点（日记是回忆），一句话即可。",
                                )
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
                await ev.send(
                    ev.plain_result(
                        f"⚠️ 将删除你所有 {len(entries)} 天的日记，此操作不可恢复。"
                        "回复「确认删除」执行，回复其他任何内容取消。"
                    )
                )
                controller.keep(timeout=60, reset_timeout=True)
                return
            if text.isdigit():
                idx = int(text)
                if 1 <= idx <= len(entries):
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
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            canned,
                            f"用户刚删掉了 {deleted} 天的日记，具体是：{labels}。"
                            f"请用你自己的口吻确认删除完成（必须原样保留日记日期：{labels}），"
                            "语气体贴一点（日记是回忆），一两句话即可。",
                        )
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

    # ------------------------- 指令：导出日记（迁移包） -------------------------
    #
    # 目的：云端服务器的用户不用 SSH，私聊发 /导出日记 即可拿到迁移包。
    # 签名体系：主密钥 .diary_key 只在服务器上，每个用户的签名钥匙由它按
    # QQ 号派生（user-key:<QQ>）。迁移包只带当事人这把派生钥匙——
    # 泄露也仅能动这一份日记，因此普通用户也可以安全地自助导出。
    # 安全约束：
    #   · 仅私聊——钥匙按 QQ 绑定，群聊不开放；
    #   · 临时包发完即删，不在数据目录留第二份钥匙副本；
    #   · 日常存储的日记 zip 仍然不含任何钥匙（"绝不入包"的设计不变）。

    async def _send_export_file(self, event: AstrMessageEvent, path: Path) -> bool:
        """把文件发到当前私聊。优先 NapCat 原生上传 API，回退标准 File 组件。"""
        # 路径 1：aiocqhttp / NapCat 原生接口（新版 NapCat 不支持消息内嵌 file 段，
        # 会报 file segment without file_id is unsupported）
        client = getattr(event, "bot", None)
        if client is not None:
            try:
                await client.api.call_action(
                    "upload_private_file",
                    user_id=int(event.get_sender_id()),
                    file=str(path.resolve()),
                    name=path.name,
                )
                return True
            except Exception as e:
                logger.warning(
                    f"[memory_diary] upload_private_file 失败，回退 File 组件: {e}"
                )
        # 路径 2：AstrBot 标准 File 组件（其他平台兜底）
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import File

            await event.send(
                MessageChain(chain=[File(file=str(path), name=path.name)])
            )
            return True
        except Exception as e:
            logger.error(f"[memory_diary] File 组件发送也失败: {e}")
            return False

    @filter.command("导出日记", alias={"diary_export"})
    async def export_diary(self, event: AstrMessageEvent):
        """导出自己的迁移包（仅私聊）：日记 zip + 本人的派生签名钥匙，发 QQ 文件。"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 导出日记只能私聊操作哦。",
                    "用户在群里想导出日记，你要委婉地拒绝：导出包含私密内容，请他来私聊操作。",
                )
            )
            return

        sender_id = event.get_sender_id()
        if self._secret_key() is None:
            yield event.plain_result(
                "📖 签名密钥（.diary_key）不可读，没法导出——"
                "请管理员检查插件数据目录里的密钥文件。"
            )
            return
        corrupt = self._corrupt_notice(sender_id)
        if corrupt:
            yield event.plain_result(corrupt)
            return
        diary_path = self._diary_path(sender_id)
        entries = self._list_entries(sender_id)
        if not diary_path.exists() or not entries:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 还没有写过日记，没什么可导出的。发送 /日记 让我先写一篇吧。",
                    "用户想导出日记，但还一篇都没写过、没什么可导的。"
                    "用你自己的口吻告诉他，并邀请他先发 /日记 让你写一篇。",
                )
            )
            return

        export_path = None
        try:
            export_path = self._build_export_bundle(sender_id, diary_path, entries)

            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    f"📖 打包好啦：{len(entries)} 天的日记 + 只属于你的签名钥匙，现在发给你。\n"
                    "这把钥匙动得了你这份日记（所以别转发给别人），但碰不了别人的日记。",
                    f"用户要导出日记，迁移包已经打好了：{len(entries)} 天的日记 + 只属于他的签名钥匙。"
                    f"请用你自己的口吻告诉他马上把包发过去（必须保留 {len(entries)} 天这个数字），"
                    "并提醒他：这把钥匙只动得了自己这份日记，但也别转发给别人。两三句话以内。",
                )
            )
            if await self._send_export_file(event, export_path):
                yield event.plain_result(
                    await self._say(
                        event.unified_msg_origin,
                        "📖 迁移包已发出～到新设备后按包里的「导入说明.txt」操作，"
                        "签名会原样通过，不用重新签。",
                        "迁移包已经通过私聊文件发给用户了。用你自己的口吻告诉他："
                        "到新设备后按包里的「导入说明.txt」操作就行，签名会原样通过。",
                    )
                )
            else:
                yield event.plain_result(
                    "📖 包打好了但发送失败（协议端可能读不到这个文件路径，"
                    "比如 AstrBot 和 NapCat 分属两个没有共享卷的容器）。"
                    "详情请见后台日志。"
                )
        except Exception as e:
            logger.error(f"[memory_diary] 导出日记失败: {e}")
            yield event.plain_result("📖 导出失败了，具体原因请管理员看后台日志。")
        finally:
            # 导出包含签名钥匙，绝不在数据目录留第二份副本
            if export_path is not None:
                try:
                    export_path.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(
                        f"[memory_diary] 导出临时包删除失败，请手动清理 {export_path}: {e}"
                    )
        event.stop_event()

    # ------------------------- 指令：导入日记（迁移包） -------------------------
    #
    # 与 /导出日记 配套的接收端：用户把迁移包当 QQ 文件发回来即可，全程不碰文件夹。
    # 安全顺序：先解开包做全量校验（manifest 格式 + zip CRC + 包内密钥逐篇验签），
    # 全部通过才允许接触本机的密钥与日记文件——不信任的包一个字节都不落盘。

    @filter.command("导入日记", alias={"diary_import"})
    async def import_diary(self, event: AstrMessageEvent):
        """导入自己的迁移包（仅私聊）：直接把 /导出日记 收到的迁移包发给我。"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 导入日记只能私聊操作哦。",
                    "用户在群里想导入日记迁移包。请用你自己的口吻委婉拒绝："
                    "导入涉及签名钥匙和私密日记，请他来私聊操作。",
                )
            )
            return
        if not _SESSION_WAITER_OK:
            yield event.plain_result(
                "📖 当前环境不支持交互操作——请管理员在管理面板的「日记本」"
                "页面用「导入日记」上传迁移包。"
            )
            return

        yield event.plain_result(
            await self._say(
                event.unified_msg_origin,
                "📖 请把迁移包（memory_diary_export_*.zip）作为文件发给我，"
                "120 秒内有效，回复「取消」退出。\n"
                "我会先验签确认它没被改过，再安放日记。",
                "用户要导入日记迁移包。请用你自己的口吻请他把迁移包 zip 作为"
                "「文件」（不是文字或图片）发给你，告诉他 120 秒内有效、回复「取消」退出；"
                "你会先验签确认没被改过再安放日记。"
                "必须保留「文件」「120 秒」「取消」这几个信息，两三句话以内。",
            )
        )

        @session_waiter(timeout=120, record_history_chains=False)
        async def diary_import_waiter(
            controller: SessionController, ev: AstrMessageEvent
        ):
            if self._is_cancel(ev.message_str or ""):
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 已取消导入，什么也没改动。",
                            "用户取消了导入迁移包的操作，什么都没改动。"
                            "用你自己的口吻确认一下就好。",
                        )
                    )
                )
                controller.stop()
                return
            try:
                from astrbot.api.message_components import File as _FileComp
            except ImportError:  # pragma: no cover
                _FileComp = None
            file_comp = None
            if _FileComp is not None:
                for comp in ev.get_messages():
                    if isinstance(comp, _FileComp):
                        file_comp = comp
                        break
            if file_comp is None:
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 没收到文件哦——请把迁移包 zip 作为「文件」发给我。",
                            "用户说要导入日记，但没有发文件过来。请用你自己的口吻提醒他："
                            "要把迁移包 zip 作为「文件」（不是文字或图片）发给你。",
                        )
                    )
                )
                controller.keep(timeout=120, reset_timeout=True)
                return

            downloaded = ""
            try:
                downloaded = await file_comp.get_file()
            except Exception as e:
                logger.warning(f"[memory_diary] 迁移包下载失败: {e}")
            if not downloaded:
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 文件下载失败了，请重发一次迁移包。",
                            "迁移包下载失败了。请用你自己的口吻让他重发一次迁移包，"
                            "语气温柔一点。",
                        )
                    )
                )
                controller.keep(timeout=120, reset_timeout=True)
                return

            try:
                owner_id, key, entries = self._verify_bundle(Path(downloaded))
                sender_name = ""
                try:
                    sender_name = ev.get_sender_name() or ""
                except Exception:
                    pass
                mode = self._install_bundle(
                    ev.get_sender_id(), owner_id, key, entries, sender_name
                )
                n_days = len(entries)
                if mode == "same_key":
                    msg = (
                        f"📖 导入完成！{n_days} 天的日记已安放"
                        "（签名钥匙与本机一致，原样有效）。\n"
                        "发 /查看日记 就能看到她写的日记啦。"
                    )
                    key_fact = "签名钥匙与本机一致，原签名原样有效"
                else:
                    msg = (
                        f"📖 导入完成！{n_days} 天的日记已用本机的签名钥匙"
                        "重新签名后安放，本机原有的日记不受影响。\n"
                        "发 /查看日记 就能看到她写的日记啦。"
                    )
                    key_fact = (
                        "已用本机的签名钥匙重新签名后安放，"
                        "本机原有的日记不受影响"
                    )
                logger.info(
                    f"[memory_diary] 用户 {ev.get_sender_id()} 导入迁移包成功："
                    f"{n_days} 天日记，模式 {mode}"
                )
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            msg,
                            f"用户的迁移包导入成功了：{n_days} 天的日记已经安放"
                            f"（{key_fact}）。请用你自己的口吻告诉他导入完成"
                            f"（必须保留 {n_days} 天这个数字与签名相关的说明），"
                            "并邀请他发 /查看日记 看日记。两三句话以内。",
                        )
                    )
                )
            except ValueError as e:
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            f"📖 导入被拒绝：{e}",
                            f"用户导入的迁移包被拒绝了，原因是：{e}。"
                            "请用你自己的口吻把拒绝原因如实告诉他"
                            "（必须原样保留原因里的关键信息，不要自己补充判断），"
                            "语气认真一点，两三句话以内。",
                        )
                    )
                )
            except Exception as e:
                logger.error(f"[memory_diary] 导入日记失败: {e}")
                await ev.send(
                    ev.plain_result("📖 导入失败了，具体原因请管理员看后台日志。")
                )
            finally:
                # 下载的迁移包含签名钥匙，用完即删
                try:
                    Path(downloaded).unlink(missing_ok=True)
                except Exception:
                    pass
            controller.stop()

        try:
            await diary_import_waiter(event)
        except TimeoutError:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 导入超时啦，想导入随时再发 /导入日记。",
                    "导入日记的互动超时了，什么也没改动。用你自己的口吻告诉他，"
                    "想导入的话随时再发 /导入日记。",
                )
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 导入日记交互异常: {e}")
        finally:
            event.stop_event()
    # ------------------------- 可视化视图：日记本 Web 页面 -------------------------
    #
    # AstrBot 插件可视化视图（views/diary-book/）的后端 API。
    # 页面运行在 Dashboard 受限 iframe 中、经 bridge 调用本组接口，
    # 天然继承面板登录鉴权——等价于"管理员在服务器上读文件"，不再另做口令。
    # 与 QQ 指令侧的关键差异：Web 上没有 QQ 会话上下文，而验签必须有主人 QQ
    # （文件名是 HMAC 派生、反推不出）。三层兜底：
    #   1. 新 zip 的 说明.txt 含"用户ID"行，直接可用；
    #   2. 旧 zip 没有该行时，页面请管理员输入一次主人 QQ，全量验签通过才接受，
    #      并记入 web_owner_map.json，以后免输；
    #   3. QQ 输错 → 签名对不上，明确拒绝。

    def _register_web_apis(self):
        if not _WEB_API_OK:
            return
        reg = getattr(self.context, "register_web_api", None)
        if reg is None:
            logger.warning(
                "[memory_diary] 当前 AstrBot 版本不支持插件可视化视图，日记本页面不可用"
            )
            return
        reg(f"/{PLUGIN_NAME}/users", self.web_list_users, ["GET"],
            "日记本：列出有日记的用户")
        reg(f"/{PLUGIN_NAME}/days/<file_key>", self.web_list_days, ["GET"],
            "日记本：列出某用户的日记日期")
        reg(f"/{PLUGIN_NAME}/day/<file_key>/<day>", self.web_read_day, ["GET"],
            "日记本：读取某天的日记")
        reg(f"/{PLUGIN_NAME}/export/<file_key>", self.web_export, ["GET"],
            "日记本：导出迁移包（浏览器下载）")
        reg(f"/{PLUGIN_NAME}/export_local/<file_key>", self.web_export_local, ["GET"],
            "日记本：导出迁移包到本机下载文件夹并打开位置")
        reg(f"/{PLUGIN_NAME}/import", self.web_import, ["POST"],
            "日记本：导入迁移包")
        reg(f"/{PLUGIN_NAME}/prefs", self.web_get_prefs, ["GET"],
            "日记本：读取界面偏好")
        reg(f"/{PLUGIN_NAME}/prefs/save", self.web_save_prefs, ["POST"],
            "日记本：保存界面偏好")
        reg(f"/{PLUGIN_NAME}/paper", self.web_get_paper, ["GET"],
            "日记本：读取自定义纸张")
        reg(f"/{PLUGIN_NAME}/paper/upload", self.web_upload_paper, ["POST"],
            "日记本：上传自定义纸张")
        reg(f"/{PLUGIN_NAME}/backdrop", self.web_get_backdrop, ["GET"],
            "日记本：读取自定义背景板")
        reg(f"/{PLUGIN_NAME}/backdrop/upload", self.web_upload_backdrop, ["POST"],
            "日记本：上传自定义背景板")

    @staticmethod
    def _valid_file_key(file_key: str) -> bool:
        return bool(re.fullmatch(r"[0-9a-f]{16}", file_key or ""))
    def _check_web_password(self, owner_id: str, for_download: bool = False):
        """Web 翻阅/导出的密码闸。返回 None 放行；否则返回给前端的响应。
        for_download=True 时（bridge.download 期望文件响应）用错误而非 JSON 表达。"""
        rec = self._view_password_rec(owner_id)
        if rec is None:
            return None
        pw = (_web_request.query.get("pw", "") or "").strip()
        if not pw:
            if for_download:
                return _web_error("PASSWORD_REQUIRED", status_code=403)
            return _web_json({"need_password": True})
        if not self._verify_view_password(rec, pw):
            return _web_error("PASSWORD_WRONG", status_code=403)
        return None
    async def web_list_users(self):
        users = []
        owner_map = None
        for path in sorted(self.data_dir.glob("diary_*.zip")):
            file_key = path.stem[len("diary_"):]
            if not self._valid_file_key(file_key):
                continue
            owner, nick = self._zip_owner_info(path)
            if not owner:
                if owner_map is None:
                    owner_map = self._load_owner_map()
                owner = owner_map.get(file_key, "")
            day_count, latest = 0, ""
            try:
                with zipfile.ZipFile(path) as z:
                    days = sorted(
                        n for n in z.namelist() if self.ENTRY_NAME_RE.match(n)
                    )
                day_count = len(days)
                latest = self._entry_label(days[-1]) if days else ""
            except Exception:
                pass
            users.append({
                "file_key": file_key,
                "nickname": nick or "（未知昵称）",
                "owner_known": bool(owner),
                "protected": bool(owner and self._view_password_rec(owner)),
                "day_count": day_count,
                "latest": latest,
                "size": path.stat().st_size,
            })
        return _web_json({"users": users})

    async def web_list_days(self, file_key: str):
        if not self._valid_file_key(file_key):
            return _web_error("无效的用户标识", status_code=400)
        path = self.data_dir / f"diary_{file_key}.zip"
        if not path.exists():
            return _web_error("日记文件不存在", status_code=404)
        try:
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                if bad is not None:
                    return _web_error(f"日记文件损坏（{bad} CRC 校验失败）",
                                      status_code=500)
                days = sorted(n for n in z.namelist() if self.ENTRY_NAME_RE.match(n))
        except Exception as e:
            return _web_error(f"读取日记文件失败：{e}", status_code=500)
        return _web_json({
            "days": [
                {"day": self._entry_day(n), "label": self._entry_label(n)}
                for n in days
            ]
        })

    async def web_read_day(self, file_key: str, day: str):
        if not self._valid_file_key(file_key) or not re.fullmatch(r"\d{8}", day or ""):
            return _web_error("无效的参数", status_code=400)
        path = self.data_dir / f"diary_{file_key}.zip"
        if not path.exists():
            return _web_error("日记文件不存在", status_code=404)
        qq_hint = (_web_request.query.get("qq", "") or "").strip()
        owner, err = self._resolve_zip_owner(path, file_key, qq_hint)
        if not owner:
            if err == "OWNER_UNKNOWN":
                return _web_json({"need_owner": True})
            return _web_error(f"无法确认日记主人：{err}", status_code=400)
        gate = self._check_web_password(owner)
        if gate is not None:
            return gate
        entry = self._md_entry(day)
        content, tampered = self._load_entry(owner, entry)
        if tampered:
            return _web_json({
                "tampered": True,
                "label": self._entry_label(entry),
                "content": "",
            })
        return _web_json({
            "tampered": False,
            "label": self._entry_label(entry),
            "content": content,
        })

    def _web_prepare_export(self, file_key: str):
        """web_export / web_export_local 共用的前半段：
        校验标识、解析主人、过密码闸、打包。成功返回 (临时包路径, None)，
        失败返回 (None, 错误响应)。"""
        if not self._valid_file_key(file_key):
            return None, _web_error("无效的用户标识", status_code=400)
        path = self.data_dir / f"diary_{file_key}.zip"
        if not path.exists():
            return None, _web_error("日记文件不存在", status_code=404)
        qq_hint = (_web_request.query.get("qq", "") or "").strip()
        owner, err = self._resolve_zip_owner(path, file_key, qq_hint)
        if not owner:
            if err == "OWNER_UNKNOWN":
                return None, _web_error("OWNER_UNKNOWN", status_code=400)
            return None, _web_error(f"无法确认日记主人：{err}", status_code=400)
        gate = self._check_web_password(owner, for_download=True)
        if gate is not None:
            return None, gate
        try:
            with zipfile.ZipFile(path) as z:
                entries = [n for n in z.namelist() if self.ENTRY_NAME_RE.match(n)]
            if not entries:
                return None, _web_error("这个用户还没有日记", status_code=400)
            bundle = self._build_export_bundle(owner, path, entries)
        except DiaryKeyUnavailable:
            return None, _web_error("签名密钥不可用，无法导出", status_code=500)
        except Exception as e:
            logger.error(f"[memory_diary] Web 导出失败: {e}")
            return None, _web_error("导出打包失败，详见后台日志", status_code=500)
        return bundle, None

    async def web_export(self, file_key: str):
        bundle, err_resp = self._web_prepare_export(file_key)
        if err_resp is not None:
            return err_resp
        # file_response 在 handler 返回后才读文件，立即删除会竞态；
        # 延迟 120 秒清理——迁移包含签名钥匙，不能长期留在磁盘上
        try:
            asyncio.get_running_loop().call_later(
                120, lambda: bundle.unlink(missing_ok=True)
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 迁移包延迟清理注册失败: {e}")
        return _web_file(bundle, filename=bundle.name,
                         content_type="application/zip")

    # 回环地址集合（AstrBot 4.x 的插件 Web 请求暴露的是 ``client_host``，
    # 不是 ``remote_addr``；见 astrbot.api.web.PluginRequest）。
    _WEB_LOOPBACKS = ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")

    def _web_is_local_panel(self) -> bool:
        """面板请求是否真的来自服务器本机。

        「存到本机并打开位置」会写管理员主目录并启动文件管理器，必须只在
        本机操作时可用。这里用 ``client_host`` 判断回环地址，并对反向代理
        转发头做二次校验：只要第一跳不是回环地址，就按远程处理。
        """
        try:
            host = str(getattr(_web_request, "client_host", "") or "").strip().lower()
        except Exception:
            return False
        if host not in self._WEB_LOOPBACKS:
            return False
        try:
            headers = getattr(_web_request, "headers", None)
            forwarded = ""
            if headers is not None:
                forwarded = str(
                    headers.get("x-forwarded-for") or headers.get("x-real-ip") or ""
                ).strip()
        except Exception:
            forwarded = ""
        first = forwarded.split(",")[0].strip().lower()
        return not first or first in self._WEB_LOOPBACKS

    async def web_export_local(self, file_key: str):
        """存到本机「下载」文件夹并打开文件位置。

        浏览器没有权限打开用户的文件管理器，但面板就开在服务器本机
        （localhost、且没有反向代理转发）时，后端可以代劳。远程访问面板时
        拒绝并提示改用「下载到浏览器」。
        """
        if not self._web_is_local_panel():
            return _web_error(
                "REMOTE_PANEL：管理面板不在服务器本机，请改用「下载到浏览器」",
                status_code=400,
            )
        bundle, err_resp = self._web_prepare_export(file_key)
        if err_resp is not None:
            return err_resp
        downloads = Path.home() / "Downloads"
        if not downloads.is_dir():
            downloads = Path.home()
        target = downloads / bundle.name
        n = 1
        while target.exists():
            target = downloads / f"{bundle.stem}_{n}{bundle.suffix}"
            n += 1
        try:
            shutil.move(str(bundle), str(target))
        except Exception as e:
            bundle.unlink(missing_ok=True)
            logger.error(f"[memory_diary] 迁移包保存到下载目录失败: {e}")
            return _web_error("保存到下载文件夹失败，详见后台日志", status_code=500)
        # 打开文件管理器并选中文件（失败也不回滚，文件已就位）
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(target)])
            elif os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
                subprocess.Popen(["open", "-R", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target.parent)])
        except Exception as e:
            logger.warning(f"[memory_diary] 打开文件位置失败（文件已保存）: {e}")
        return _web_json({"saved_to": str(target), "filename": target.name})

    async def web_import(self):
        files = await _web_request.files()
        upload = files.get("file") if files else None
        if _PluginUploadFile is None or not isinstance(upload, _PluginUploadFile):
            return _web_error("没有收到文件", status_code=400)
        tmp = self.data_dir / f"import_tmp_{secrets.token_hex(4)}.zip"
        try:
            await upload.save(tmp)
            owner_id, key, entries = self._verify_bundle(tmp)
            mode = self._install_bundle(
                owner_id, owner_id, key, entries, check_owner=False
            )
            logger.info(
                f"[memory_diary] Web 页面导入迁移包成功："
                f"{len(entries)} 天日记，主人 {owner_id}，模式 {mode}"
            )
            return _web_json({
                "imported": len(entries), "mode": mode, "owner": owner_id
            })
        except ValueError as e:
            return _web_error(f"导入被拒绝：{e}", status_code=400)
        except Exception as e:
            logger.error(f"[memory_diary] Web 导入失败: {e}")
            return _web_error("导入失败，详见后台日志", status_code=500)
        finally:
            # 上传的迁移包含签名钥匙，用完即删
            tmp.unlink(missing_ok=True)

    # 界面偏好（字号/字体）：iframe 沙箱没有 localStorage，
    # 存到数据目录、按管理面板账号分开；视图加载时读取，改动即保存。
    _VIEW_FONT_FAMILIES = ("kai", "song", "sans")

    def _view_prefs_path(self) -> Path:
        return self.data_dir / "web_view_prefs.json"

    def _load_view_prefs(self) -> dict:
        try:
            d = json.loads(self._view_prefs_path().read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    async def web_get_prefs(self):
        username = getattr(_web_request, "username", None) or "_default"
        prefs = self._load_view_prefs().get(username)
        return _web_json({
            "prefs": prefs if isinstance(prefs, dict) else {},
            "brand": self._web_brand(),
        })

    async def web_save_prefs(self):
        payload = await _web_request.json(default={})
        prefs = payload.get("prefs") if isinstance(payload, dict) else None
        if not isinstance(prefs, dict):
            return _web_error("无效的偏好内容", status_code=400)
        clean = {}
        # 白名单 + 范围钳制：不信任视图传来的任何值
        try:
            size = int(prefs.get("font_size", 16))
            clean["font_size"] = min(max(size, 13), 22)
        except (TypeError, ValueError):
            pass
        family = str(prefs.get("font_family", ""))
        if family in self._VIEW_FONT_FAMILIES:
            clean["font_family"] = family
        paper = str(prefs.get("paper", ""))
        if paper in ("default", "custom"):
            clean["paper"] = paper
        backdrop = str(prefs.get("backdrop", ""))
        if backdrop in ("default", "custom"):
            clean["backdrop"] = backdrop
        username = getattr(_web_request, "username", None) or "_default"
        try:
            all_prefs = self._load_view_prefs()
            all_prefs[username] = clean
            p = self._view_prefs_path()
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(all_prefs, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception as e:
            logger.warning(f"[memory_diary] 保存界面偏好失败: {e}")
            return _web_error("保存失败，详见后台日志", status_code=500)
        return _web_json({"saved": True, "prefs": clean})

    # ---- 自定义纸张 / 背景板：图片存数据目录，读取时以 base64 JSON 返回 ----
    # （iframe 沙箱不能直接带鉴权拉二进制，bridge 只能传 JSON，故用 data URL）
    # kind 取值 "paper"（书页纸纹）或 "backdrop"（书本背后的背景板），互不影响。
    _PAPER_MIMES = {"image/png", "image/jpeg", "image/webp"}
    _PAPER_MAX_BYTES = 3 * 1024 * 1024
    _WEB_IMAGE_KINDS = ("paper", "backdrop")

    def _web_brand(self) -> str:
        """日记本页面左上角称呼（配置项 web_diary_brand），默认 TA，最长 12 字符。"""
        name = str(self.config.get("web_diary_brand", "") or "").strip()
        name = re.sub(r"\s+", "", name)[:12]
        return name or "TA"

    def _web_image_paths(self, kind: str):
        return (
            self.data_dir / f"web_{kind}.img",
            self.data_dir / f"web_{kind}.json",
        )

    async def _web_get_image(self, kind: str):
        img_path, meta_path = self._web_image_paths(kind)
        if not img_path.exists():
            return _web_json({"found": False})
        try:
            mime = "image/png"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("mime") in self._PAPER_MIMES:
                    mime = meta["mime"]
            data = base64.b64encode(img_path.read_bytes()).decode("ascii")
            # 字段名不能叫 data：AstrBot 插件视图的 bridge 会把响应体顶层的 data
            # 当成信封解包（PluginViewPage.vue: response.data?.data ?? response.data），
            # 一旦撞名，前端只拿到 base64 字符串，r.found / r.mime 全丢了，
            # 表现就是「上传成功但页面永远不换图」（同款坑见 tests/test_web.py）。
            return _web_json({"found": True, "mime": mime, "image": data})
        except Exception as e:
            logger.warning(f"[memory_diary] 读取自定义{kind}图片失败: {e}")
            return _web_error("读取图片失败，详见后台日志", status_code=500)

    async def _web_upload_image(self, kind: str):
        files = await _web_request.files()
        upload = files.get("file") if files else None
        if _PluginUploadFile is None or not isinstance(upload, _PluginUploadFile):
            return _web_error("没有收到文件", status_code=400)
        mime = (getattr(upload, "content_type", "") or "").lower().strip()
        if mime not in self._PAPER_MIMES:
            return _web_error("只支持 PNG / JPG / WebP 图片", status_code=400)
        img_path, meta_path = self._web_image_paths(kind)
        tmp = self.data_dir / f"{kind}_tmp_{secrets.token_hex(4)}"
        try:
            await upload.save(tmp)
            if tmp.stat().st_size > self._PAPER_MAX_BYTES:
                return _web_error("图片不能超过 3MB", status_code=400)
            os.replace(tmp, img_path)
            meta_path.write_text(json.dumps({"mime": mime}), encoding="utf-8")
            return _web_json({"saved": True})
        except Exception as e:
            logger.error(f"[memory_diary] 上传自定义{kind}图片失败: {e}")
            return _web_error("保存失败，详见后台日志", status_code=500)
        finally:
            tmp.unlink(missing_ok=True)

    async def web_get_paper(self):
        return await self._web_get_image("paper")

    async def web_upload_paper(self):
        return await self._web_upload_image("paper")

    async def web_get_backdrop(self):
        return await self._web_get_image("backdrop")

    async def web_upload_backdrop(self):
        return await self._web_upload_image("backdrop")

    @filter.command("日记密码", alias={"diary_password"})
    async def diary_password(self, event: AstrMessageEvent):
        """设置/修改/取消 Web 日记本页面的查看密码（仅私聊、仅本人）。

        用法：/日记密码 设置 <密码> ｜ /日记密码 修改 <旧> <新> ｜ /日记密码 取消 <旧>
        """
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
        # 不用 _command_arg：它会剥 @提及，可能误伤含 @ 的密码。
        tokens = (event.message_str or "").strip().split()
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

        yield event.plain_result(usage)

    @filter.command("日记状态", alias={"diary_status"})
    async def diary_status(self, event: AstrMessageEvent):
        """查看当前上下文占用、日记更新时间和哨兵状态。"""
        if not self._should_respond(event):
            return
        sender_id = event.get_sender_id()
        umo = event.unified_msg_origin

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

        # 上下文占用估算（基于会话历史）
        limit = int(self.config.get("context_token_limit", 60000) or 60000)
        remind_ratio, auto_ratio = self._watch_ratios()
        history = await self._get_history(umo)
        chars = 0
        for msg in history:
            if isinstance(msg, dict):
                chars += len(self._content_to_text(msg.get("content", "")))
            else:
                chars += len(str(msg))
        used = int(chars / CHARS_PER_TOKEN)
        pct = int(used / limit * 100) if limit > 0 else 0

        # 哨兵状态
        cid = await self._curr_cid(umo)
        state = self._watch_state.get(umo) or {}
        watch_ok = bool(self.config.get("enable_context_watch", True))
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

        yield event.plain_result(
            "📖 日记状态：\n"
            f"· 日记：{diary_info}\n"
            f"· 当前上下文：约 {used}/{limit} tokens（{pct}%，估算值）\n"
            f"· 阈值：提醒 {int(remind_ratio * 100)}% / 自动写日记 {int(auto_ratio * 100)}%\n"
            f"· 本会话哨兵：{watch_info}\n"
            f"· 本会话日记注入：{inject_info}"
        )

    # ------------------------- 指令：注入范围 -------------------------

    @filter.command("日记范围", alias={"diary_scope"})
    async def diary_scope(self, event: AstrMessageEvent, scope: str = ""):
        """设置新上下文注入哪些日记：最新 / 全部 / 最近N天 / 时间段。"""
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

    @filter.command("注入日记", alias={"diary_inject"})
    async def diary_inject(self, event: AstrMessageEvent, scope: str = ""):
        """主动把指定日记注入接下来的对话（本会话去重，下一句对话时一次性生效）。"""
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

    # ------------------------- 新会话注入日记 + 哨兵 -------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """reset 双保险（P0-1 补强）：监听到 /reset 指令文本就置标记。

        只置标记、不拦截、不影响 AstrBot 内置 reset 的本身逻辑；
        标记在下一次 LLM 请求（on_llm_request）时消费，按新代际重置状态。
        """
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return  # 自己发的消息不算
        except Exception:
            pass
        # 登记私聊会话（供每天定时自动写日记主动通知），并惰性启动后台任务
        try:
            self._remember_auto_user(event)
        except Exception as e:
            logger.warning(f"[memory_diary] 处理自动写日记用户登记异常: {e}")
        task = getattr(self, "_auto_task", None)
        if not getattr(self, "_terminated", False) and (task is None or task.done()):
            self._start_auto_diary_task()
        text = (event.message_str or "").strip().lower()
        if text in ("/reset", "reset", "/重置", "重置"):
            self._reset_pending.add(event.unified_msg_origin)
            self._cap_state(self._reset_pending)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """每次 LLM 请求前：新会话注入日记；同时执行上下文哨兵。"""
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
                prev_watch = self._watch_state.get(umo0)
                if reset_flagged or (
                    cid0
                    and (
                        self._injected_cid.get(umo0) == cid0
                        or (prev_watch and prev_watch.get("cid") == cid0)
                    )
                ):
                    self._injected_cid.pop(umo0, None)
                    self._injected_entries.pop(umo0, None)
                    self._pending_inject.pop(umo0, None)
                    self._watch_state.pop(umo0, None)
                    logger.info(
                        "[memory_diary] 检测到 /reset（指令标记或上下文已清空），"
                        "会话代际状态已重置，本轮将重新注入日记"
                    )
        except Exception as e:
            logger.warning(f"[memory_diary] reset 代际识别异常: {e}")

        # 1) 上下文哨兵
        try:
            await self._watch_context(event, req)
        except Exception as e:
            logger.warning(f"[memory_diary] 上下文哨兵异常: {e}")

        # 2) 注入日记（自动 + 手动追加）
        try:
            sender_id = event.get_sender_id()
            in_group = bool(event.get_group_id())
            umo = event.unified_msg_origin
            cid = await self._curr_cid(umo)

            # 本会话已注入过的篇目记录（用于手动追加注入时去重）
            rec = self._injected_entries.get(umo)
            if not rec or rec.get("cid") != cid:
                rec = {"cid": cid, "names": set()}
                self._injected_entries[umo] = rec
                self._cap_state(self._injected_entries)

            # a) 用户用 /注入日记 主动要求的追加注入：下一轮请求一次性带入。
            #    P2-12：手动注入是用户明确指令，不受"新会话自动读取"开关影响。
            pending = self._pending_inject.pop(umo, None)
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
                    sender_id, in_group=in_group, exclude_names=pending_names
                )
                if diary:
                    req.system_prompt = (
                        (req.system_prompt or "")
                        + "\n\n"
                        + INJECT_TEMPLATE.format(diary=diary)
                    )
                    self._injected_cid[umo] = cid
                    self._cap_state(self._injected_cid)
                    rec["names"].update(names)
                elif tampered_any:
                    logger.warning(
                        f"[memory_diary] 用户 {sender_id} 的日记均被篡改，跳过注入"
                    )
                return

            # 临时模式（默认）：每个新会话的第一次 LLM 请求注入一次。
            # 不再要求 contexts 为空——/reset 后如果用户先发了 /查看日记 等命令，
            # 历史可能已经不是空的，硬性跳过会导致本会话永远注入不上。
            if self._injected_cid.get(umo) == cid:
                return
            diary, tampered_any, names = self._assemble_inject(
                sender_id, in_group=in_group
            )
            if not diary:
                if tampered_any:
                    logger.warning(
                        f"[memory_diary] 用户 {sender_id} 的日记均被篡改，跳过注入"
                    )
                return
            self._injected_cid[umo] = cid
            self._cap_state(self._injected_cid)
            rec["names"].update(names)
            self._deliver_inject(req, INJECT_TEMPLATE.format(diary=diary))
            logger.info(f"[memory_diary] 已在新会话为用户 {sender_id} 注入日记")
        except Exception as e:
            logger.warning(f"[memory_diary] 注入日记异常: {e}")

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

