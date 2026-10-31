# -*- coding: utf-8 -*-
from __future__ import annotations

"""日记服务：会话历史 -> 素材 -> LLM 写作 -> 保存，以及哨兵/定时任务。

只依赖 duck-typed 的 AstrBot context（conversation_manager /
persona_manager / get_using_provider_async / send_message），不 import
AstrBot。真正的消息平台适配留在 main.py。
"""

import asyncio
import inspect
import json
import os
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅为类型标注；运行时不依赖 AstrBot
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import ProviderRequest

from .constants import (
    AUTO_DIARY_DEFAULT_TIME,
    AUTO_DIARY_GRACE_SECONDS,
    AUTO_DIARY_STATE_FILE,
    AUTO_DIARY_USERS_FILE,
    BRIEF_SYSTEM_PROMPT,
    CALIB_EWMA_ALPHA,
    CALIB_FACTOR_MAX,
    CALIB_FACTOR_MIN,
    CALIB_RATIO_SKIP,
    CHARS_PER_TOKEN,
    DIARY_SYSTEM_PROMPT,
    ERR_NO_NEW_MATERIAL,
    LIFE_SYSTEM_PROMPT,
    _STATE_CAP,
)
from .token_estimator import estimate_messages, estimate_request
from .errors import (
    DiaryCorruptError,
    DiaryKeyUnavailable,
    DiaryTamperedError,
)
from .log import logger


class ServiceMixin:
    """写作编排、上下文哨兵与定时任务；由 MemoryDiaryCore 组合。"""

    # ------------------------- 会话历史 -------------------------

    async def _get_history(self, umo: str) -> list:
        """从 AstrBot 会话管理器读取当前会话的历史消息。"""
        try:
            conv_mgr = self.context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                return []
            conv = await conv_mgr.get_conversation(umo, cid)
            if not conv or not conv.history:
                return []
            history = json.loads(conv.history)
            return history if isinstance(history, list) else []
        except Exception as e:
            logger.warning(f"[memory_diary] 读取会话历史失败: {e}")
            return []

    async def _curr_cid(self, umo: str) -> str:
        try:
            return (
                await self.context.conversation_manager.get_curr_conversation_id(umo)
                or ""
            )
        except Exception:
            return ""

    @staticmethod
    async def _maybe_await(value):
        if inspect.isawaitable(value):
            return await value
        return value

    async def _get_persona_prompt(self, umo: str) -> str:
        """获取当前会话使用的人设 system_prompt；取不到时返回空串。"""
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        try:
            # 1) 会话上指定了 persona_id 时优先用它（/persona 切换的人设）。
            #    优先走 v3 人格库接口（不同版本方法名不同，逐个探测），再回退旧接口。
            conv_mgr = self.context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(umo)
            conv = await conv_mgr.get_conversation(umo, cid) if cid else None
            persona_id = getattr(conv, "persona_id", "") or ""
            if persona_id and persona_id != "[%None]":
                for getter_name in ("get_persona_v3_by_id", "get_persona"):
                    getter = getattr(pm, getter_name, None)
                    if getter is None:
                        continue
                    try:
                        persona = await self._maybe_await(getter(persona_id))
                    except Exception:
                        continue
                    if isinstance(persona, dict):
                        prompt = (
                            persona.get("system_prompt") or persona.get("prompt") or ""
                        )
                    else:
                        prompt = (
                            getattr(persona, "system_prompt", "")
                            or getattr(persona, "prompt", "")
                            or ""
                        )
                    if prompt.strip():
                        return prompt.strip()
            # 2) 回退到默认人格（同样按版本探测接口名）
            for getter_name in ("get_default_persona_v3", "get_default_persona"):
                getter = getattr(pm, getter_name, None)
                if getter is None:
                    continue
                try:
                    personality = await self._maybe_await(getter(umo))
                except Exception:
                    continue
                if isinstance(personality, dict):
                    prompt = personality.get("prompt", "") or ""
                else:
                    prompt = getattr(personality, "prompt", "") or ""
                if prompt.strip():
                    return prompt.strip()
            return ""
        except Exception as e:
            logger.warning(f"[memory_diary] 获取人设失败: {e}")
            return ""

    @staticmethod
    def _content_to_text(content) -> str:
        """兼容字符串与多模态 content 列表。"""
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
            return "".join(parts)
        return str(content or "")

    def _flatten_history(self, history: list) -> str:
        """把历史消息转成可读文本。"""
        lines = []
        for msg in history:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = self._content_to_text(msg.get("content", "")).strip()
            if not content:
                continue
            speaker = "用户" if role == "user" else "我"
            lines.append(f"{speaker}: {content}")
        return "\n".join(lines)

    # 需要从写作素材中剔除的日记操作：用户侧的指令、AI 侧的插件固定通知前缀
    _OP_USER_COMMANDS = (
        "日记",
        "写日记",
        "查看日记",
        "删除日记",
        "日记状态",
        "日记范围",
        "日记帮助",
        "注入日记",
        "记一下",
        "记住",
        "查看备忘",
        "删除备忘",
        "diary",
        "diary_view",
        "diary_delete",
        "diary_status",
        "diary_scope",
        "diary_help",
        "diary_inject",
        "memo",
        "memo_view",
        "memo_delete",
        "形象",
        "日记形象",
        # v0.6 批注指令：翻批注/写回批注都是操作，不进写作素材，也不进写回蒸馏素材
        "查看批注",
        "写回批注",
        "annotations",
        "diary_annotations",
        "diary_writeback",
        "reset",
        "重置",
    )
    _OP_ASSISTANT_PREFIXES = ("📖", "⚠️ 上下文已用", "📝 小提醒", "📌 已记下")

    @classmethod
    def _is_op_command(cls, raw: str) -> bool:
        """判断一条用户消息是否为日记/会话操作指令。

        规则：带指令前缀（/ ! ！ . 。 #）的消息，前缀后按指令词开头匹配；
        不带前缀的消息，只有整条与指令词完全相等才算指令——
        避免误伤"我写了一篇日记""日记写完了"这类自然语言。
        """
        s = raw.strip()
        if not s:
            return False
        if s[0] in "/!！.。#":
            body = s.lstrip("/!！.。#").lower()
            return any(body.startswith(cmd) for cmd in cls._OP_USER_COMMANDS)
        return s.lower() in cls._OP_USER_COMMANDS

    @classmethod
    def _filter_diary_ops(cls, convo_text: str) -> str:
        """剔除日记操作相关的行：不进入写作素材，省 token 也避免污染画像。"""
        kept = []
        for line in convo_text.split("\n"):
            if line.startswith("用户: "):
                if cls._is_op_command(line[len("用户: ") :]):
                    continue
            elif line.startswith("我: "):
                body = line[len("我: ") :].strip()
                if body.startswith(cls._OP_ASSISTANT_PREFIXES):
                    continue
                if "—— 日记全文 ——" in body:
                    continue
            kept.append(line)
        return "\n".join(kept)

    # ------------------------- 写日记（指令与自动共用） -------------------------

    # 写作模型标出的"自发挥"片段：<self>…</self>。保存前剥离，原文记入来源侧车，
    # 供批注锚定过滤（批注不允许落在 AI 自编内容上）。
    _SELF_MARK_RE = re.compile(r"<self>(.*?)</self>", re.IGNORECASE | re.DOTALL)

    @classmethod
    def _extract_self_marks(cls, content: str):
        """剥离 <self>…</self> 标签。返回（干净正文, [自发挥原文子串]）。

        模型不合规（未闭合/落单标签）时兜底剥掉裸标签，只留文字——
        绝不让标签原样漏进落盘正文或用户眼睛。
        """
        spans: list = []

        def _grab(m):
            inner = m.group(1).strip()
            if inner:
                spans.append(inner)
            return inner

        text = cls._SELF_MARK_RE.sub(_grab, str(content or ""))
        text = re.sub(r"</?self\s*>", "", text, flags=re.IGNORECASE)
        return text, spans

    async def _write_diary(
        self,
        umo: str,
        sender_id: str,
        sender_name: str,
        is_group: bool = False,
        allow_life: bool = True,
        extra_material: str = "",
        requested_mode: str = "auto",
        from_auto: bool = False,
    ):
        """调用 LLM 写日记并保存。返回 (diary_content, error_msg)。

        三种写作模式：
        - full：完整日记，四段式质量锚点；
        - brief：1~3 句简记，默认模式；
        - life：距上次写日记没有任何新对话，写一段她自己的拟人日常
          （allow_life=False 时不写，返回 ERR_NO_NEW_MATERIAL，供自动写场景静默跳过）。

        requested_mode：auto（默认命令，第一次完整、之后简记）/ brief / full。
        from_auto：是否由 90% 哨兵触发；完整名额满了时自动降级为简记。
        extra_material：触发本轮的那条当前消息；自动触发时它还没进会话历史。
        """
        history = await self._get_history(umo)
        cid = await self._curr_cid(umo)
        cursor = self._load_material_cursor(sender_id, umo)
        cursor_count = cursor.get("count")
        cursor_valid = (
            cursor.get("cid") == cid
            and isinstance(cursor_count, int)
            and 0 <= cursor_count <= len(history)
            and (
                cursor_count == 0
                or self._message_hash(history[cursor_count - 1])
                == cursor.get("last_hash")
            )
        )
        if cursor_valid:
            new_history = history[cursor_count:]
            # 上一次自动写日记时，当前消息还没进 history；下一轮它会出现在游标后，
            # 用 last_extra_hash 把这条已经写过的消息跳掉。
            last_extra_hash = str(cursor.get("last_extra_hash") or "")
            if (
                last_extra_hash
                and new_history
                and self._message_hash(new_history[0]) == last_extra_hash
            ):
                new_history = new_history[1:]
        else:
            new_history = history

        # 按消息条数截断只用于“游标失效”的回退；有游标时读全部新增，避免漏素材。
        max_msgs = int(self.config.get("max_history_messages", 60) or 60)
        if is_group:
            # 群聊历史混有其他成员发言，按倍率放大窗口，提高覆盖到画像对象发言的概率
            mult = int(self.config.get("group_history_multiplier", 3) or 1)
            max_msgs *= max(1, mult)
        if not cursor_valid and len(new_history) > max_msgs:
            new_history = new_history[-max_msgs:]

        convo_text = self._flatten_history(new_history)
        # 剔除日记操作相关内容：不进入写作素材，省 token 也避免污染画像
        if convo_text:
            convo_text = self._filter_diary_ops(convo_text)

        # P1-5：并入当前消息（与 history 末条重复时不重复添加；本身是指令时不进素材）
        extra = (extra_material or "").strip()
        extra_hash = (
            self._message_hash({"role": "user", "content": extra}) if extra else ""
        )
        extra_in_history = bool(
            extra_hash
            and history
            and self._message_hash(history[-1]) == extra_hash
        )
        extra_pending = bool(
            extra and not self._is_op_command(extra) and not extra_in_history
        )
        if extra_pending:
            extra_line = f"用户: {extra}"
            lines = convo_text.split("\n") if convo_text else []
            if not lines or lines[-1] != extra_line:
                convo_text = (
                    (convo_text + "\n" + extra_line) if convo_text else extra_line
                )

        entries = self._list_entries(sender_id)
        prev_label = self._entry_label(entries[-1]) if entries else ""
        today_entry = self._md_entry(self._now().strftime("%Y%m%d"))
        today_content = ""
        if today_entry in entries:
            today_content, today_tampered = self._load_entry(sender_id, today_entry)
            if today_tampered:
                return None, (
                    "今天的日记验签失败，疑似被手动修改过；"
                    "请先用 /删除日记 处理掉这篇，或从备份恢复后再写。"
                )

        scene = "group" if is_group else "private"
        full_count = self._full_count_for_scene(today_content, scene)
        briefs = self._briefs_for_scene(today_content, scene)

        try:
            max_full = int(self.config.get("max_full_diaries_per_day", 1))
        except Exception:
            max_full = 1
        if max_full < 0:
            max_full = 1

        requested = requested_mode if requested_mode in ("auto", "brief", "full") else "auto"
        # 群聊写日记只吸收群聊场景的备忘（私聊备忘继续等待下次私聊写日记），
        # 否则私聊备忘会被写进"写于群聊"的日记片段、随群聊注入泄露
        memos = self._load_memos(sender_id, scene="group" if is_group else "")
        # 完整篇可以把同场景的旧简记作为素材；简记不回头合并旧简记。
        has_merge_material = requested == "full" and bool(briefs)
        new_material = bool(convo_text or memos)
        no_new_material = not new_material and not has_merge_material

        if no_new_material:
            if not allow_life:
                return None, ERR_NO_NEW_MATERIAL
            if not bool(self.config.get("empty_diary_as_life", True)):
                return None, "距离上次写日记还没有新的聊天内容，今天没有什么可记的啦"
            # 同一天最多写一篇"她的日常随笔"：无新对话时素材游标不会前进，
            # 每过一遍操作冷却又会触发一次，当天文件会被随笔刷屏
            if self._load_pref_data(sender_id).get(
                "last_life_day"
            ) == self._now().strftime("%Y%m%d"):
                return None, "今天的随笔已经写过啦，等我们有新聊天时再叫我写日记吧"
            mode = "life"
        else:
            if requested == "brief":
                mode = "brief"
            elif requested == "full":
                mode = "full"
            else:
                # 默认命令：第一次写日记作为初始锚点写完整，之后默认简记
                mode = "full" if not entries else "brief"

            if mode == "full" and max_full > 0 and full_count >= max_full:
                if from_auto:
                    # 哨兵在 90% 必须留下记忆；完整名额满了就降级为简记
                    mode = "brief"
                else:
                    return None, (
                        f"今天这个场景已经写过 {full_count} 篇完整日记了"
                        f"（上限 {max_full} 篇）。可以继续写简记，"
                        "或把配置 max_full_diaries_per_day 调大/设为 0。"
                    )
            if mode == "brief" and not new_material:
                return None, ERR_NO_NEW_MATERIAL

        now_str = self._now().strftime("%Y-%m-%d %H:%M:%S")
        extra_instruction = str(self.config.get("extra_instruction", "") or "").strip()

        prompt_parts = [f"当前时间：{now_str}"]
        if mode == "life":
            if prev_label:
                prompt_parts.append(
                    f"上一篇日记写于 {prev_label}，从那以后你们没有再聊过天。"
                )
            prompt_parts.append("请写下这段时间你自己的日常。")
        else:
            prompt_parts.append(f"用户当前昵称：{sender_name}")
            if prev_label:
                prompt_parts.append(
                    f"上一篇日记写于 {prev_label}。本篇请涵盖从那之后到现在这段时间的内容。"
                )
                if not cursor_valid:
                    prompt_parts.append(
                        "注意：对话记录是回退读取的最近内容，可能仍包含上次日记已经写过的旧内容，"
                        "请识别并跳过，只记录新发生的事。"
                    )
            else:
                prompt_parts.append(
                    "这是第一篇日记，请涵盖你从对话记录中能了解到的全部。"
                )
            if mode == "brief":
                prompt_parts.append(
                    "这次只写一段简略的当天简记，不要重复之前写过的内容。"
                )
            if mode == "full" and briefs:
                brief_lines = "\n".join(
                    f"- {b['header']}\n{b['text']}" for b in briefs
                )
                prompt_parts += [
                    "",
                    "今天同一场景下已经写过这些简记（请把它们作为素材合并进完整日记，"
                    "去重后用自己的话重写，不要逐句照抄）：",
                    brief_lines,
                ]
            if is_group:
                prompt_parts.append(
                    f"注意：以下对话记录来自群聊，可能包含其他成员的发言。"
                    f"请只围绕昵称为「{sender_name}」的这位用户来写画像与评价，"
                    "不要把其他成员说的话当成是这位用户说的。"
                )
            if memos:
                memo_lines = "\n".join(f"- [{m['time']}] {m['text']}" for m in memos)
                prompt_parts.append(
                    "用户特别叮嘱要记住的事（用户亲口要求记下，务必吸收进画像"
                    + ("，简记里也要简要提及" if mode == "brief" else "")
                    + f"）：\n{memo_lines}"
                )
            if convo_text:
                prompt_parts += [
                    "",
                    "这段时间的对话记录：",
                    convo_text,
                ]
            if extra_instruction:
                prompt_parts += [
                    "",
                    f"额外写作要求（来自用户设置）：{extra_instruction}",
                ]

        # ---- 插图（v0.6）：扫描用户照片 + 追加插图决策提示词 ----
        # 图标关闭时 image_addon 为空串：不额外调用模型、不改变原有 prompt（零感知）。
        image_photos: list = []
        image_notice_extra = ""
        self._image_last_notice = ""
        try:
            if self._image_enabled():
                image_cfg = self._image_cfg()
                photo_limit = (
                    image_cfg["user_image_limit"]
                    if image_cfg["user_image_enabled"]
                    else 0
                )
                # 群聊写日记不收集照片（审查 P1-3）：会话历史里所有成员的消息都是
                # role=user，无法区分"这张照片是谁发的"——把别人的照片收进写作者
                # 的日记 zip 是跨用户隐私事故，宁可少收。
                if is_group:
                    photo_limit = 0
                if photo_limit > 0:
                    image_photos = self._collect_user_photos(
                        new_history, photo_limit
                    )
                    # 可选视觉接口：配置了才补"真实画面描述"；否则保持内容未识别
                    image_photos = await self._caption_user_photos(image_photos)
                # 最近图回顾只在真的可能画插图时才读 zip（省 IO）
                ai_slots = (
                    image_cfg["image_limit_full"]
                    if mode == "full"
                    else image_cfg["image_limit_memo"]
                )
                recent_images = (
                    self._image_style_summary(sender_id) if ai_slots > 0 else []
                )
                image_addon = self._image_prompt_addon(
                    mode,
                    image_photos,
                    image_cfg["persona_image_desc"],
                    recent_images,
                )
                if image_addon:
                    prompt_parts += ["", image_addon]
        except Exception as e:
            logger.warning(f"[memory_diary] 组装插图提示词失败（本次不插图）: {e}")
        prompt = "\n".join(prompt_parts)

        provider = await self.context.get_using_provider_async(umo=umo)
        if provider is None:
            return None, "当前没有可用的 LLM 提供商"

        # 按当前人设的口吻写日记（可在配置中关闭）
        system_prompt = {
            "full": DIARY_SYSTEM_PROMPT,
            "brief": BRIEF_SYSTEM_PROMPT,
            "life": LIFE_SYSTEM_PROMPT,
        }[mode]
        if bool(self.config.get("use_persona", True)):
            persona_prompt = await self._get_persona_prompt(umo)
            if persona_prompt:
                keep_req = (
                    "但日记的四个部分与事实性要求不变。"
                    if mode == "full"
                    else "但上文的事实性要求不变（不编造、不重复）。"
                )
                system_prompt += (
                    "\n\n【你的人设】\n"
                    f"{persona_prompt}\n\n"
                    '写日记时请保持这个人设的性格与口吻：以人设中的"我"来写，'
                    f"像人设本人写给未来的自己，{keep_req}"
                )

        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
            )
            diary_content = (resp.completion_text or "").strip()
        except Exception as e:
            logger.error(f"[memory_diary] 调用 LLM 写日记失败: {e}")
            return None, f"调用 LLM 写日记失败：{e}"

        if not diary_content:
            return None, "模型没有返回日记内容"

        # 来源标记（批注防"批在 AI 自编内容上"）：剥掉 <self> 标签，原文子串
        # 暂存到 self_spans，等下面的保存成功后再写入来源侧车。
        diary_content, self_spans = self._extract_self_marks(diary_content)

        # ---- 插图（v0.6）：AI 意愿标记 → 闸门 → PENDING 占位 / 用户照片落盘 ----
        # 日记正文先落盘（图后到），所以这里只做"决定与占位"，真正的生图在
        # 保存成功之后由 _schedule_illustrations 起后台任务（设计第 5 节）。
        image_decisions: list = []
        image_day = self._now().strftime("%Y%m%d")
        try:
            if self._image_enabled():
                wanted = self._count_ai_image_markers(diary_content)
                allowed = self._image_gate(sender_id, is_group, mode, wanted)
                diary_content, image_decisions = self._parse_image_markers(
                    diary_content, allowed
                )
                # 即使本次没扫到照片也走一遍：清掉模型偶发写出的照片标记，
                # 不让 〔用户照片：N｜…〕 原样留在落盘正文里。
                # 起始序号接当天已有插图（审查 P2-4：同一天多次写日记不留重复序号）
                photo_start_seq = self._next_image_seq(sender_id, image_day)
                diary_content, photo_next_seq = self._apply_user_photo_markers(
                    sender_id, diary_content, image_photos, image_day,
                    photo_start_seq,
                )
                photo_count = max(
                    0, int(photo_next_seq) - int(photo_start_seq)
                )
                if photo_count > 0:
                    image_notice_extra += (
                        f"\n\n（这篇日记收了你发来的 {photo_count} 张照片，"
                        "她给照片写了注脚。）"
                    )
        except Exception as e:
            logger.warning(f"[memory_diary] 插图决策/标记处理失败（按纯文字保存）: {e}")

        try:
            separator_note = "（无新对话·她的日常随笔）" if mode == "life" else ""
            absorb_headers = (
                [b["header"] for b in briefs] if mode == "full" and briefs else None
            )
            self._save_diary(
                sender_id,
                diary_content,
                sender_name=sender_name,
                entry_name_hint=image_day,
                separator_note=separator_note,
                origin="group" if is_group else "private",
                mode=mode,
                absorb_headers=absorb_headers,
            )
            # 插图调度：创建后台任务（不阻塞本次返回）；形象档案为空时先走确认仪式
            try:
                await self._schedule_illustrations(
                    sender_id,
                    sender_name,
                    umo,
                    image_day,
                    mode,
                    image_decisions,
                    self._image_cfg(),
                )
            except Exception as e:
                logger.warning(f"[memory_diary] 插图调度失败（日记已保存）: {e}")
            if image_notice_extra:
                self._image_last_notice = (
                    (self._image_last_notice or "") + image_notice_extra
                )
            # 记录本次读到的历史边界，下一次只取游标之后的新消息。
            # 自动触发时当前消息可能还没进 history，记下它的哈希，下一轮跳过。
            last_msg_hash = (
                self._message_hash(history[-1]) if history else ""
            )
            self._save_material_cursor(
                sender_id,
                umo,
                {
                    "cid": cid,
                    "count": len(history),
                    "last_hash": last_msg_hash,
                    "last_extra_hash": extra_hash if extra_pending else "",
                },
            )
            # 只清已吸收进日记的场景：群聊写只清群聊备忘，私聊写全清
            self._clear_memos(
                sender_id, scenes=("group",) if is_group else ("private", "group")
            )
            # 来源侧车：这篇日记的自发挥子串落盘（失败只记日志——日记本身
            # 已写好，绝不能因为来源标记的存取失败把整次写作报成失败）。
            if self_spans:
                try:
                    if not self.record_self_provenance(
                        sender_id, image_day, self_spans
                    ):
                        logger.warning(
                            "[memory_diary] 来源侧车写入失败（批注过滤将退化为"
                            "仅按随笔模式过滤）"
                        )
                except Exception as e:
                    logger.warning(f"[memory_diary] 记录来源标记失败（不影响日记）: {e}")
            if mode == "life":
                # 记录今天已写过随笔，同日不再重复触发（防刷屏）
                pref = self._load_pref_data(sender_id)
                pref["last_life_day"] = self._now().strftime("%Y%m%d")
                self._save_pref_data(sender_id, pref)
        except DiaryKeyUnavailable as e:
            logger.error(f"[memory_diary] 密钥不可用，日记未写入: {e}")
            return None, (
                "日记写好了但没能保存：签名密钥（.diary_key）不可读或无法生成。"
                "为了不留下以后无法验签的日记，这次没有写入——"
                "请检查插件数据目录的权限后让我再试一次。"
            )
        except DiaryTamperedError as e:
            logger.error(f"[memory_diary] 保存前验签失败，已拒绝写入: {e}")
            return None, (
                f"日记写好了但没能保存：{e} 的日记验签失败（疑似被手动改过）。"
                "我不会给被改过的内容重新签名，所以这次没有写入——"
                "请先用 /删除日记 删掉那天的日记，或从备份恢复后再让我写。"
            )
        except DiaryCorruptError as e:
            logger.error(f"[memory_diary] 日记文件损坏: {e}")
            return None, (
                f"日记写好了但没能保存：{e}。"
                "我没有写入新内容；请管理员检查并处理好损坏文件（恢复或删除）后，再让我写。"
            )
        except Exception as e:
            logger.error(f"[memory_diary] 日记写入文件失败: {e}")
            return None, (
                "日记写好了但保存到文件失败——请检查插件数据目录"
                f"（{self.data_dir}）的写入权限和磁盘空间。错误：{e}"
            )
        logger.info(
            f"[memory_diary] 已为用户 {sender_id} 写入日记（{mode} 模式，{len(diary_content)} 字）"
        )
        return diary_content, None

    async def _in_character_notice(self, umo: str, situation: str) -> str:
        """让她以人设口吻生成一句给用户的口头通知；失败时返回空串。"""
        try:
            provider = await self.context.get_using_provider_async(umo=umo)
            if provider is None:
                return ""
            persona_prompt = ""
            if bool(self.config.get("use_persona", True)):
                persona_prompt = await self._get_persona_prompt(umo)
            sys_parts = []
            if persona_prompt:
                sys_parts.append(f"【你的人设】\n{persona_prompt}")
            sys_parts.append(
                "现在你需要给用户一句简短的口头通知。要求：保持人设的口吻与性格，"
                "像平时说话一样自然，两三句话以内，不要用列表或小标题。"
            )
            resp = await provider.text_chat(
                prompt=situation,
                system_prompt="\n\n".join(sys_parts),
            )
            return (resp.completion_text or "").strip()
        except Exception as e:
            logger.warning(f"[memory_diary] 生成口头通知失败: {e}")
            return ""

    async def _say(self, umo: str, canned: str, situation: str) -> str:
        """命令反馈："按人设口吻"开启时用她的话转述，关闭或生成失败时用固定文案。

        canned 是兜底文案；situation 描述要传达的事实（含必须保留的数字/名称）。
        只用于对话式反馈；列表/菜单/日记正文/日期清单/技术性报错仍用固定文案，
        避免数字与操作指引被转述错。
        """
        if bool(self.config.get("use_persona", True)):
            notice = await self._in_character_notice(umo, situation)
            if notice:
                return notice
        return canned
    # ------------------------- 上下文哨兵 -------------------------

    def _token_estimation_v2(self) -> bool:
        """改进版 token 估算开关（实验性功能，默认关闭）；关闭回退 v0.5.4 旧公式。"""
        return bool(self.config.get("token_estimation_v2", False))

    def _context_limit(self) -> int:
        """模型上下文窗口（tokens）；0 或负数表示哨兵与比例门槛停用。

        不能用 ``config.get(...) or 60000`` 读取——那样 0 会被静默当成默认
        60000，管理员想用 0 停用哨兵时会看起来"配置没生效"。这里只在缺省/
        非法值时回退默认 60000，0/负数原样返回，由调用方按"停用"处理：
        哨兵不判定、`/日记状态` 不显示百分比、手动写日记的比例门槛跳过。
        """
        try:
            return int(self.config.get("context_token_limit", 60000))
        except (TypeError, ValueError):
            return 60000

    def _calibration_enabled(self) -> bool:
        """EWMA 自适应校准开关（实验性功能，默认关闭；仅改进版估算开启时生效）。"""
        return self._token_estimation_v2() and bool(
            self.config.get("token_calibration_enabled", False)
        )

    def _estimate_tokens(self, req: ProviderRequest) -> int:
        """估算本次请求携带的上下文 token 数（静态估算，不含校准系数）。

        token_estimation_v2 开启：按字符类别加权 + 消息/工具/多模态开销；
        关闭：旧公式（字符数 ÷ 1.8），行为与 v0.5.4 完全一致。
        """
        if self._token_estimation_v2():
            return estimate_request(req)
        total_chars = len(req.system_prompt or "") + len(req.prompt or "")
        for msg in getattr(req, "contexts", None) or []:
            if isinstance(msg, dict):
                total_chars += len(self._content_to_text(msg.get("content", "")))
            else:
                total_chars += len(str(msg))
        return int(total_chars / CHARS_PER_TOKEN)

    async def _estimate_history_tokens(self, umo: str) -> int:
        """手动写日记门槛与 /日记状态 共用：按会话历史估算占用（含校准系数）。

        v2 走与哨兵同一个 estimator（同输入必同输出）；关闭时用旧公式。
        """
        history = await self._get_history(umo)
        if not self._token_estimation_v2():
            chars = 0
            for msg in history:
                if isinstance(msg, dict):
                    chars += len(self._content_to_text(msg.get("content", "")))
                else:
                    chars += len(str(msg))
            return int(chars / CHARS_PER_TOKEN)
        base = estimate_messages(history)
        key = await self._provider_key(umo)
        return int(base * self._calib_factor(key))

    async def _context_used(self, umo: str) -> int:
        """当前上下文占用：v2 下与哨兵同口径，供状态页和手动门槛复用。

        优先取最近一次请求时哨兵算出的 used（同一会话、同一窗口大小）——
        这样 /日记状态、手动写日记门槛看到的百分比和哨兵判定完全一致，
        不会再出现"哨兵说该写了、状态页说没满"。没有可用记录（还没发过
        请求 / 哨兵停用 / 窗口大小刚改过）时回退按历史估算。
        旧公式路径保持 v0.5.4 的历史口径不变。
        """
        if self._token_estimation_v2():
            info = self._last_context_used.get(umo)
            limit = self._context_limit()
            if limit > 0 and info and int(info.get("limit", -1)) == limit:
                return int(info.get("used", 0) or 0)
        return await self._estimate_history_tokens(umo)

    async def _context_calib_key(self, umo: str) -> str:
        """观测行用的 provider::model：优先复用哨兵最近一次请求记下的 key。"""
        if self._token_estimation_v2():
            info = self._last_context_used.get(umo)
            limit = self._context_limit()
            if limit > 0 and info and int(info.get("limit", -1)) == limit and info.get("key"):
                return str(info["key"])
        return await self._provider_key(umo)

    async def _stage_pending_estimate(self, umo: str, req) -> None:
        """P2-1：暂存本次静态估算 E，供 on_llm_response 拿到真实 usage 后校准。

        与“上下文哨兵是否启用”解耦：用户把提醒/自动写两档阈值都设 0（合法配置）
        或 context_token_limit<=0 时，_watch_context 会提前返回；过去暂存写在
        _watch_context 里，导致这些用户的 EWMA 永远不更新。现在由 on_llm_request
        统一暂存，_watch_context 只负责阈值判定。

        v2 或校准关闭时不暂存，保持 v0.5.4 行为不变（差分套件默认两者都关闭）。
        """
        if not self._calibration_enabled():
            return
        try:
            key = await self._provider_key(umo, req)
            if not key:
                return
            self._pending_estimates[umo] = {
                "key": key,
                "estimate": self._estimate_tokens(req),
            }
            self._cap_state(self._pending_estimates)
        except Exception as e:
            logger.debug(f"[memory_diary] 暂存校准估算失败: {e}")

    def _refresh_pending_estimate(self, umo: str, req) -> None:
        """日记注入完成后重算本次静态估算，让校准样本 E 与真实请求同口径。

        on_llm_request 里哨兵先于注入执行（触发判定必须尽早），若拿注入前的
        E 去对注入后的 actual 做 EWMA，样本会被系统性抬高（persist_inject
        每轮都会发生）。注入完成后重算一次即可消除这部分偏差。
        """
        pending = self._pending_estimates.get(umo)
        if not pending or not self._token_estimation_v2():
            return
        try:
            pending["estimate"] = self._estimate_tokens(req)
        except Exception as e:
            logger.debug(f"[memory_diary] 重算校准估算失败: {e}")

    # ---- token 估算自适应校准（EWMA，按 provider::model 持久化） ----

    async def _provider_key(self, umo: str, req=None) -> str:
        """校准系数的键 provider::model；取不到返回空串（该次不参与校准）。

        provider 段优先用配置里的 provider id（``provider_config["id"]``）：
        标准 AstrBot Provider 实例没有 ``provider_id`` 属性，且 ``meta`` 是方法
        不是属性，所以先把 ``meta()`` 调出来再兜底取 id/name。
        """
        try:
            provider = await self.context.get_using_provider_async(umo=umo)
        except Exception:
            return ""
        if provider is None:
            return ""
        cfg = getattr(provider, "provider_config", None)
        cfg_id = str(cfg.get("id", "") or "") if isinstance(cfg, dict) else ""
        meta = getattr(provider, "meta", None)
        if callable(meta):
            try:
                meta = meta()
            except Exception:
                meta = None
        name = str(
            getattr(provider, "provider_id", "")
            or cfg_id
            or getattr(meta, "id", "")
            or getattr(meta, "name", "")
            or type(provider).__name__
        )
        model = ""
        if req is not None:
            model = str(getattr(req, "model", "") or "")
        if not model:
            model = str(
                getattr(provider, "model", "")
                or getattr(provider, "model_name", "")
                or ""
            )
        return f"{name}::{model}" if model else name

    def _calib_table(self) -> dict:
        if self._token_calib is None:
            self._token_calib = self._load_token_calibration()
        return self._token_calib

    def _calib_factor(self, key: str) -> float:
        """某 provider::model 的当前校准系数；无样本/未开启时 1.0（纯静态估算）。"""
        if not key or not self._calibration_enabled():
            return 1.0
        rec = self._calib_table().get(key)
        if not rec:
            return 1.0
        factor = float(rec.get("factor", 1.0))
        return min(max(factor, CALIB_FACTOR_MIN), CALIB_FACTOR_MAX)

    def _calib_status_line(self, key: str) -> str:
        """/日记状态 的校准观测行；无样本或未开启时返回空串。"""
        if not key or not self._calibration_enabled():
            return ""
        rec = self._calib_table().get(key)
        if not rec or not rec.get("samples"):
            return ""
        # P3-8：显示也按读取口径钳制，避免手改/坏文件出现“系数 99.00”
        # 但实际按 5.00 计算的观感不一致。
        factor = min(
            max(float(rec.get("factor", 1.0)), CALIB_FACTOR_MIN), CALIB_FACTOR_MAX
        )
        return (
            f"· 校准：上次实际 {int(rec.get('last_actual', 0)):,}"
            f"（系数 {factor:.2f}，"
            f"样本 {int(rec.get('samples', 0))}，{key}）"
        )

    def _update_calibration(self, key: str, estimate: int, actual: int):
        """EWMA 自适应：factor = factor×0.8 + (actual/E)×0.2，钳制 [0.3, 5.0]。

        actual 或 E 为 0、比例异常（>10 倍或 <1/10）时跳过本次更新。
        """
        if not key or estimate <= 0 or actual <= 0:
            return
        ratio = actual / estimate
        if ratio > CALIB_RATIO_SKIP or ratio < 1 / CALIB_RATIO_SKIP:
            logger.debug(
                f"[memory_diary] token 校准样本异常（E={estimate}, actual={actual}），跳过"
            )
            return
        table = self._calib_table()
        rec = table.get(key) or {"factor": 1.0, "samples": 0}
        old = min(
            max(float(rec.get("factor", 1.0)), CALIB_FACTOR_MIN), CALIB_FACTOR_MAX
        )
        new = old * (1 - CALIB_EWMA_ALPHA) + ratio * CALIB_EWMA_ALPHA
        new = min(max(new, CALIB_FACTOR_MIN), CALIB_FACTOR_MAX)
        table[key] = {
            "factor": round(new, 4),
            "samples": int(rec.get("samples", 0) or 0) + 1,
            "last_actual": int(actual),
            "updated": self._now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self._save_token_calibration(table):
            logger.debug(
                f"[memory_diary] token 校准更新（{key}）：系数 {old:.3f} → {new:.3f}"
                f"（E={estimate}, actual={actual}, 样本 {table[key]['samples']}）"
            )

    def _watch(self, umo: str, cid: str) -> dict:
        """取/重置某会话的哨兵状态。conversation_id 变化时视为新会话。"""
        state = self._watch_state.get(umo)
        if state is None or state.get("cid") != cid:
            state = {"cid": cid, "reminded": False, "auto_written": False}
            self._watch_state[umo] = state
            self._cap_state(self._watch_state)
        return state

    def _watch_ratios(self):
        """读取提醒/自动写日记阈值（0 = 该档关闭），并强制 remind <= auto。

        哨兵启用开关已并入滑条：两档都为 0 即哨兵整体停用。
        存量兼容：老配置 enable_context_watch=false 仍存在时视为两档都关，
        并记一次迁移日志（只读旧键、永不写入）。
        _conf_schema.json 无法表达跨字段校验，只能在读取时兜底：
        auto>0 且 remind>auto 时把提醒阈值截断到自动阈值并警告一次——否则"提醒"
        会晚于"自动写日记"甚至永不触发，哨兵行为变得不可解释。
        越界值钳制到 [0, 1]。
        """

        def _ratio(name, default):
            try:
                v = float(self.config.get(name, default))
            except (TypeError, ValueError):
                v = default
            return min(max(v, 0.0), 1.0)

        remind = _ratio("remind_ratio", 0.75)
        auto = _ratio("auto_ratio", 0.9)
        legacy = self.config.get("enable_context_watch", None)
        if legacy is not None and not bool(legacy):
            if not self._watch_migrate_warned:
                self._watch_migrate_warned = True
                logger.warning(
                    "[memory_diary] 检测到旧配置 enable_context_watch=false：哨兵已停用。"
                    "该开关已并入阈值滑条（0 = 不提醒 / 不自动写），"
                    "请到插件配置页确认两档阈值后可忽略此提示"
                )
            return 0.0, 0.0
        if auto > 0 and remind > auto:
            if not self._ratio_warned:
                self._ratio_warned = True
                logger.warning(
                    f"[memory_diary] 提醒阈值（{remind}）大于自动写日记阈值（{auto}），"
                    "已按自动写日记阈值截断提醒阈值——请到插件配置页修正"
                )
            remind = auto
        return remind, auto

    async def _watch_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """估算上下文占用，触发提醒 / 自动写日记。"""
        limit = self._context_limit()
        remind_ratio, auto_ratio = self._watch_ratios()
        if limit <= 0 or (remind_ratio <= 0 and auto_ratio <= 0):
            return

        umo = event.unified_msg_origin
        base = self._estimate_tokens(req)
        key = ""
        if self._token_estimation_v2():
            # P2-1：校准样本暂存已上移到 on_llm_request（与哨兵启用解耦）；
            # 这里复用那一份 provider key，保证判定与样本用同一个 key。
            staged = self._pending_estimates.get(umo)
            key = str((staged or {}).get("key") or "")
            if not key:
                key = await self._provider_key(umo, req)
        used = int(base * self._calib_factor(key))
        if self._token_estimation_v2():
            # 记下本次哨兵口径的占用，供 /日记状态 与手动写日记门槛复用：
            # 三处看到同一个数，避免"哨兵说该写了、状态页说没满"。
            self._last_context_used[umo] = {
                "used": used,
                "limit": limit,
                "key": key,
            }
            self._cap_state(self._last_context_used)
        ratio = used / limit
        if ratio < remind_ratio:
            return

        cid = await self._curr_cid(umo)
        state = self._watch(umo, cid)
        pct = int(ratio * 100)

        if auto_ratio > 0 and ratio >= auto_ratio and not state["auto_written"]:
            sender_id = event.get_sender_id()
            # 互斥锁的粒度是 sender_id，不是会话 umo：写的是 diary_<sender_id>.zip。
            # 同一群里不同用户各写各的日记，不能互相阻塞；同一用户的私聊/群聊写
            # 同一个 zip，必须互斥（手动、哨兵、每天定时共用这把锁）。
            if sender_id in self._writing:
                return  # 该用户已有写日记流程在进行
            self._writing.add(sender_id)
            try:
                sender_name = event.get_sender_name() or sender_id
                diary, err = await self._write_diary(
                    umo,
                    sender_id,
                    sender_name,
                    is_group=bool(event.get_group_id()),
                    allow_life=False,  # 自动写不编造"她的日常"
                    extra_material=req.prompt
                    or "",  # P1-5：当前消息还没进 history，手动并入
                    requested_mode="full",
                    from_auto=True,
                )
            finally:
                self._writing.discard(sender_id)

            # 每代际（每个 conversation_id）只自动尝试一次：_write_diary 返回 err 时
            # 也置位，本代际不再重试——否则 LLM 网络抖动时每轮请求都会重试、连续刷
            # 失败提示并反复打 provider；用户已经从失败提示里拿到 /日记（手动重试）
            # 和 /reset 两个出口。其中 ERR_NO_NEW_MATERIAL 是确定性结果（确实没有
            # 新内容），重试无意义。（_write_diary 内部已捕获各类异常并转成 err；
            # 若它抛出未捕获异常，则走不到这里，下一次请求会重新尝试。）
            state["auto_written"] = True
            state["reminded"] = True
            if err == ERR_NO_NEW_MATERIAL:
                # P1-6：不能静默——上下文还在继续涨，用户需要知道现在该 /reset 了
                canned = (
                    f"⚠️ 上下文已用约 {pct}%（估算 {used}/{limit} tokens），"
                    "不过这轮没有新的聊天内容可写进日记，所以这次没写。\n"
                    "建议现在发送 /reset 开启新上下文，我会先读之前的日记再继续陪你。"
                )
                await event.send(
                    event.plain_result(
                        await self._say(
                            umo,
                            canned,
                            f"上下文已经用到约 {pct}%（估算 {used}/{limit} tokens），"
                            "但这轮没有新的聊天内容可以写进日记，所以这次没写。"
                            "请用你自己的口吻提醒用户：必须保留两个数字"
                            f"（{pct}%、{used}/{limit} tokens）和「/reset」这条指令，"
                            "建议他发 /reset 开新上下文，你会先读之前的日记再继续陪他。"
                            "两三句话以内。",
                        )
                    )
                )
                return
            if diary:
                # 关闭「按人设口吻」时不额外调用 LLM，直接用固定文案
                notice = ""
                if bool(self.config.get("use_persona", True)):
                    notice = await self._in_character_notice(
                        umo,
                        "上下文快满了，你刚刚悄悄把给未来自己的日记写好存起来了。"
                        "请用你自己的口吻提醒用户：日记已经写好，请 ta 发送 /reset 开启新的上下文，"
                        "你会先读一遍日记再继续陪 ta。不要复述日记内容。",
                    )
                if not notice:
                    notice = "我已经把日记写好存起来啦 📖 发送 /reset 后，我会先读一遍日记，再继续陪你。"
                notice += (
                    f"\n\n（本篇约 {len(diary)} 字，/reset 后的第一轮会一次性读入，"
                    "不会每轮重复消耗 token）"
                )
                if (
                    bool(self.config.get("show_diary_after_write", False))
                    and not event.get_group_id()
                ):
                    # 日记全文只在私聊展示，群聊不展示
                    notice += f"\n\n—— 日记全文 ——\n{diary}"
                await event.send(
                    event.plain_result(
                        f"⚠️ 上下文已用约 {pct}%（估算 {used}/{limit} tokens），快到极限啦。\n\n{notice}"
                    )
                )
            else:
                await event.send(
                    event.plain_result(
                        f"⚠️ 上下文大约已用 {pct}%（估算 {used}/{limit} tokens），但我自动写日记失败了（{err}）。"
                        "你可以发 /日记 让我再试一次，或尽快 /reset。"
                    )
                )

        elif remind_ratio > 0 and ratio >= remind_ratio and not state["reminded"]:
            state["reminded"] = True
            canned = (
                f"📝 小提醒：我们的上下文已用约 {pct}%（估算 {used}/{limit} tokens）。"
                "快到写日记的时候啦——你可以现在发 /日记 让我记录，"
                "或者用到更满时我会自动写好。"
            )
            await event.send(
                event.plain_result(
                    await self._say(
                        umo,
                        canned,
                        f"上下文已用约 {pct}%（估算 {used}/{limit} tokens），"
                        "快到写日记的时候了。请用你自己的口吻提醒用户："
                        f"必须保留两个数字（{pct}%、{used}/{limit} tokens）"
                        "和「/日记」这条指令；可以现在发 /日记 让你记录，"
                        "或者用到更满时你会自动写好。两三句话以内。",
                    )
                )
            )
    # ------------------------- 每天定时自动写完整日记 -------------------------
    #
    # 管理员在配置页开启「每天定时自动写完整日记」并指定时间后，后台任务到点给
    # 「有过私聊会话的用户」自动写一篇完整日记（等同 /写日记 完整）。约束：
    #   · 当天已经写过任意完整日记（私聊/群聊场景都算）就跳过；
    #   · 没有新素材（也没有当天简记可合并）就静默跳过，不写“日常随笔”；
    #   · 每个用户每天只尝试一次，失败不重试；
    #   · 写完后用人设口吻主动私聊告知；失败只发一次固定技术说明（保证信息准确）。
    # 私聊会话来自 on_any_message 的登记（auto_diary_users.json）；没登记过私聊
    # 会话的用户无法主动通知，也不参与自动写。配置变更会由 AstrBot 重载插件生效。

    def _auto_diary_users_path(self) -> Path:
        return self.data_dir / AUTO_DIARY_USERS_FILE

    def _auto_diary_state_path(self) -> Path:
        return self.data_dir / AUTO_DIARY_STATE_FILE

    def _load_auto_users(self):
        """载入私聊用户登记表：{sender_id: {"umo":..., "name":..., "updated":...}}。"""
        try:
            raw = self._auto_diary_users_path().read_text(encoding="utf-8")
            data = json.loads(raw)
            users = data.get("users") if isinstance(data, dict) else None
            if isinstance(users, dict):
                self._auto_users = {
                    str(k): v
                    for k, v in users.items()
                    if isinstance(v, dict) and str(v.get("umo") or "").strip()
                }
                return
            self._auto_users = {}
        except FileNotFoundError:
            self._auto_users = {}
        except Exception as e:
            logger.warning(f"[memory_diary] 读取自动写日记用户表失败: {e}")
            self._auto_users = {}

    def _save_auto_users(self) -> bool:
        try:
            path = self._auto_diary_users_path()
            tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
            tmp.write_text(
                json.dumps({"users": self._auto_users}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
            return True
        except Exception as e:
            logger.warning(f"[memory_diary] 保存自动写日记用户表失败: {e}")
            return False

    def _remember_auto_user(self, event: AstrMessageEvent):
        """登记私聊会话，供定时任务写完后主动通知；群聊不登记。"""
        if getattr(self, "_terminated", False):
            return
        try:
            if event.get_group_id():
                return
            sender_id = str(event.get_sender_id() or "").strip()
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if not sender_id or not umo:
                return
            if not self._is_allowed(sender_id):
                return  # 白名单外的用户不登记进定时自动写日记名单
            try:
                name = event.get_sender_name() or ""
            except Exception:
                name = ""
            rec = self._auto_users.get(sender_id)
            if rec and rec.get("umo") == umo and rec.get("name") == name:
                return
            self._auto_users[sender_id] = {
                "umo": umo,
                "name": name,
                "updated": self._now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            while len(self._auto_users) > _STATE_CAP:
                self._auto_users.pop(next(iter(self._auto_users)))
            self._save_auto_users()
        except Exception as e:
            logger.warning(f"[memory_diary] 登记自动写日记私聊用户失败: {e}")

    async def _seed_auto_users_from_conversations(self):
        """从 AstrBot 会话库补齐私聊用户：umo 形如 platform:FriendMessage:<QQ>。

        这样升级前已有私聊会话的用户也会被自动写日记覆盖，不必等他们先发一条消息。
        只取私聊会话；群聊不登记。取不到时静默跳过（依赖 on_any_message 登记）。
        """
        mgr = getattr(self.context, "conversation_manager", None)
        getter = getattr(mgr, "get_conversations", None)
        if getter is None:
            return
        try:
            convs = await self._maybe_await(getter())
        except Exception as e:
            logger.debug(f"[memory_diary] 读取会话列表补齐私聊用户失败: {e}")
            return
        changed = False
        now_str = self._now().strftime("%Y-%m-%d %H:%M:%S")
        for conv in convs or []:
            umo = str(getattr(conv, "user_id", "") or "")
            parts = umo.split(":")
            # AstrBot 4.x 的 UMO 消息类型是 FriendMessage；兼容测试/旧格式的 private
            if len(parts) < 3 or parts[1].lower() not in (
                "friendmessage",
                "privatemessage",
                "private",
            ):
                continue
            sender_id = parts[-1].strip()
            if not sender_id:
                continue
            if not self._is_allowed(sender_id):
                continue  # 白名单外的用户不补进定时自动写日记名单
            rec = self._auto_users.get(sender_id)
            name = str(rec.get("name") or "") if rec else ""
            if not name:
                # 已有日记的用户可以从 zip 内说明.txt 取昵称；取不到用 QQ 号
                try:
                    _, nick = self._zip_owner_info(self._diary_path(sender_id))
                    name = nick or sender_id
                except Exception:
                    name = sender_id
            if rec and rec.get("umo") == umo and rec.get("name") == name:
                continue
            self._auto_users[sender_id] = {
                "umo": umo,
                "name": name,
                "updated": now_str,
            }
            changed = True
        if changed:
            while len(self._auto_users) > _STATE_CAP:
                self._auto_users.pop(next(iter(self._auto_users)))
            self._save_auto_users()
            logger.info(
                f"[memory_diary] 已从会话库补齐自动写日记私聊用户，共 {len(self._auto_users)} 个"
            )

    def _load_auto_state(self) -> dict:
        try:
            data = json.loads(self._auto_diary_state_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.debug(f"[memory_diary] 读取自动写日记触发状态失败: {e}")
            return {}

    def _save_auto_state(self, state: dict) -> bool:
        try:
            path = self._auto_diary_state_path()
            tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except Exception as e:
            logger.warning(f"[memory_diary] 保存自动写日记触发状态失败: {e}")
            return False

    def _parse_auto_diary_time(self):
        """解析配置里的 HH:MM；无效时返回 None 并只警告一次。"""
        raw = str(
            self.config.get("auto_diary_time", AUTO_DIARY_DEFAULT_TIME)
            or AUTO_DIARY_DEFAULT_TIME
        ).strip()
        m = re.fullmatch(r"([01]?\d|2[0-3])\s*[:：]\s*([0-5]?\d)", raw)
        if m is None:
            if raw != self._auto_time_warned:
                self._auto_time_warned = raw
                logger.warning(
                    f"[memory_diary] 自动写日记时间格式无效: {raw!r}（应为 HH:MM，例如 22:00）"
                )
            return None
        return int(m.group(1)), int(m.group(2))

    def _start_auto_diary_task(self):
        """启动后台定时任务；已启动、已终止或环境没有事件循环时安全跳过。"""
        if getattr(self, "_terminated", False):
            return
        task = getattr(self, "_auto_task", None)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[memory_diary] 当前没有运行中的事件循环，自动写日记任务暂未启动")
            return
        try:
            self._auto_task = loop.create_task(self._auto_diary_loop())
            logger.info("[memory_diary] 每天定时自动写日记后台任务已启动")
        except Exception as e:
            logger.warning(f"[memory_diary] 启动自动写日记任务失败: {e}")

    async def _auto_diary_loop(self):
        """每 20 秒检查一次是否到点；到点当天只触发一次。"""
        try:
            await asyncio.sleep(5)  # 等插件与配置加载稳定
            while True:
                try:
                    await self._auto_diary_tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[memory_diary] 自动写日记检查异常: {e}")
                await asyncio.sleep(20)
        except asyncio.CancelledError:
            raise

    async def _auto_diary_tick(self):
        if not bool(self.config.get("enable_auto_diary", False)):
            return
        parsed = self._parse_auto_diary_time()
        if parsed is None:
            return
        now = self._now()
        today = now.strftime("%Y%m%d")
        state = self._auto_state or self._load_auto_state()
        self._auto_state = state
        if str(state.get("last_trigger_day") or "") == today:
            return
        target = now.replace(
            hour=parsed[0], minute=parsed[1], second=0, microsecond=0
        )
        if now < target:
            return
        # 启动/重启比设定时间晚太多（例如服务器休眠后开机）不补写，等明天；
        # 只给一个小窗口，避免“到点前已过”却突然补写。
        if (now - target).total_seconds() > AUTO_DIARY_GRACE_SECONDS:
            return
        # 先落盘“今天已触发”：之后无论成功失败都不再重试（进程重载也不会）
        self._auto_state = {
            "last_trigger_day": today,
            "last_trigger_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_auto_state(self._auto_state)
        logger.info(
            f"[memory_diary] 到点触发每天自动写日记（{today} "
            f"{parsed[0]:02d}:{parsed[1]:02d}，登记用户 {len(self._auto_users)} 个）"
        )
        await self._run_auto_diaries(today)

    def _has_full_diary_today(self, sender_id: str, day: str) -> bool:
        """今天是否已经写过完整日记（私聊或群聊场景都算）。"""
        entry = self._md_entry(day)
        entries = self._list_entries(sender_id)
        if entry not in entries:
            return False
        content, tampered = self._load_entry(sender_id, entry)
        if tampered:
            # 验签失败时无法确认内容，直接跳过自动写，避免再触发保存失败
            return True
        return bool(
            self._full_count_for_scene(content, "private")
            + self._full_count_for_scene(content, "group")
        )

    def _full_diary_count_today(self, sender_id: str, is_group: bool) -> int:
        """今天当前场景已写的完整日记篇数（用于 /日记 完整 后的名额提示）。"""
        entry = self._md_entry(self._now().strftime("%Y%m%d"))
        entries = self._list_entries(sender_id)
        if entry not in entries:
            return 0
        content, tampered = self._load_entry(sender_id, entry)
        if tampered:
            return 0
        return self._full_count_for_scene(
            content, "group" if is_group else "private"
        )

    async def _run_auto_diaries(self, day: str):
        """给登记过的私聊用户逐个写完整日记；单个失败继续下一个，不重试。"""
        users = list(self._auto_users.items())
        if not users:
            logger.info("[memory_diary] 自动写日记：没有登记过私聊会话的用户，跳过")
            return
        wrote = skipped = failed = 0
        for sender_id, rec in users:
            sender_id = str(sender_id)
            rec = rec if isinstance(rec, dict) else {}
            if not self._is_allowed(sender_id):
                # 真正触发前再过一遍白名单：管理员收紧名单后无需重启，
                # 已登记的用户也不再被写
                skipped += 1
                continue
            umo = str(rec.get("umo") or "").strip()
            if not umo:
                skipped += 1
                continue
            if sender_id in self._writing:
                # 同一用户已有写日记流程（手动/哨兵/定时）在进行，避免并发重写 zip
                skipped += 1
                continue
            try:
                if self._has_full_diary_today(sender_id, day):
                    skipped += 1
                    continue
            except Exception as e:
                logger.warning(
                    f"[memory_diary] 检查用户 {sender_id} 今日完整日记失败，跳过: {e}"
                )
                skipped += 1
                continue
            self._writing.add(sender_id)
            diary, err = None, None
            try:
                diary, err = await self._write_diary(
                    umo,
                    sender_id,
                    str(rec.get("name") or sender_id),
                    is_group=False,
                    allow_life=False,
                    requested_mode="full",
                )
            except Exception as e:
                err = f"自动写日记异常：{e}"
            finally:
                self._writing.discard(sender_id)
            if diary:
                wrote += 1
                await self._notify_auto_diary_success(umo, diary)
            elif err == ERR_NO_NEW_MATERIAL:
                # 没有新素材不是失败：静默跳过，不写日常随笔，也不打扰用户
                skipped += 1
            elif err:
                failed += 1
                logger.warning(
                    f"[memory_diary] 用户 {sender_id} 自动写日记失败（不重试）: {err}"
                )
                await self._notify_auto_diary_failure(umo, err)
            else:
                skipped += 1
        logger.info(
            f"[memory_diary] 每天定时自动写日记完成：成功 {wrote}，跳过 {skipped}，失败 {failed}"
        )

    async def _notify_auto_diary_success(self, umo: str, diary: str):
        """写完后主动私聊告知；语气走人设，失败回退固定文案。"""
        notice = ""
        if bool(self.config.get("use_persona", True)):
            try:
                notice = await self._in_character_notice(
                    umo,
                    "你按每天和用户约定的时间，悄悄写好了今天的完整日记。"
                    "请用你自己的口吻告诉用户：今天的日记已经写好（不要复述日记内容），"
                    "这是按约定时间自动写的，不需要 /reset。两三句话以内。",
                )
            except Exception as e:
                logger.warning(f"[memory_diary] 生成自动写日记通知失败: {e}")
        if not notice:
            notice = "📖 今天约定的完整日记写好啦，已经存好。"
        if (
            bool(self.config.get("show_diary_after_write", False))
            and diary
        ):
            # 纯文本通知里不直接露出 markdown 图片语法，改成占位符（v0.6）
            notice += (
                "\n\n—— 日记全文 ——\n"
                + self._image_macro_to_placeholder(diary)
            )
        await self._send_proactive(umo, notice)

    async def _notify_auto_diary_failure(self, umo: str, err: str):
        """失败只提示一次（不重试）；先用人设口吻道个歉，技术原因固定附后。"""
        notice = ""
        if bool(self.config.get("use_persona", True)):
            try:
                notice = await self._in_character_notice(
                    umo,
                    "今天到点自动写日记没有成功。请用你自己的口吻简短地告诉用户这件事、"
                    "道个歉，不要编造原因，也不要给技术细节；准确原因会附在后面。"
                    "两三句话以内。",
                )
            except Exception as e:
                logger.warning(f"[memory_diary] 生成自动写日记失败通知失败: {e}")
        detail = (
            f"📖 今天的自动日记没能写成：{err}\n"
            "这次不会自动重试；需要的话可以私聊发 /日记 让我手动写。"
        )
        text = f"{notice}\n\n{detail}" if notice else detail
        await self._send_proactive(umo, text)

    async def _send_proactive(self, umo: str, text: str) -> bool:
        """主动向私聊会话发送一条文本（定时任务没有 event，用 context.send_message）。"""
        try:
            try:
                from astrbot.api.event import MessageChain
            except Exception:
                from astrbot.core.message.message_event_result import MessageChain

            send = getattr(self.context, "send_message", None)
            if send is None:
                logger.warning(
                    "[memory_diary] 当前 AstrBot 版本不支持主动发送消息，自动日记通知未发出"
                )
                return False
            ok = await send(umo, MessageChain().message(text))
            if not ok:
                logger.warning(f"[memory_diary] 自动日记通知未发送成功（会话 {umo}）")
            return bool(ok)
        except Exception as e:
            logger.warning(f"[memory_diary] 主动发送自动日记通知失败: {e}")
            return False

