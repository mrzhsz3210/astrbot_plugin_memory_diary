# -*- coding: utf-8 -*-
from __future__ import annotations

"""批注（v0.6 设计方案第二部分）：阅读的副产品，不是对话的开场白。

设计要点（见《批注功能接口契约》与设计方案）：
- 批注只产生于"读"的时刻：reset 后首次注入日记时同步生成一批，与日记一起注入；
- 批注与日记同等待遇注入上下文，用户不翻牌就当它不存在（不主动推进）；
- 写回必须用户签字：`/写回批注` 蒸馏出草稿，用户确认后才落盘；
- 纯文字玩法：任何环节都不触发插图、不调用插图模块。

本模块不 import AstrBot：LLM / 会话历史都通过 duck-typed 的 context 使用。
"""

import asyncio
import json
import re
import secrets
from datetime import datetime

from .common import strip_invisible
from .constants import (
    ANNOTATION_ANCHOR_MAX,
    ANNOTATION_DEFAULT_REPLY_TITLE,
    ANNOTATION_DEFAULT_TITLE,
    ANNOTATION_DISTILL_PROMPT,
    ANNOTATION_DISTILL_SYSTEM_PROMPT,
    ANNOTATION_EXISTING_BLOCK,
    ANNOTATION_GEN_PROMPT,
    ANNOTATION_GEN_SYSTEM_PROMPT,
    ANNOTATION_INJECT_RULES,
    ANNOTATION_NOTE_MAX,
    ANNOTATION_QUESTION_HINT_OFF,
    ANNOTATION_QUESTION_HINT_ON,
    ANNOTATION_REPLY_TITLE_MAX,
    ANNOTATION_SELF_PLACEHOLDER,
    ANNOTATION_SUMMARY_MAX,
    ANNOTATION_TITLE_MAX,
    ANNOTATION_VIEW_MAX,
)
from .log import logger


class AnnotationMixin:
    """批注领域编排：生成、注入、命令文案、写回蒸馏、Web 数据。"""

    # 注入块字数上限（批注和日记吃同一份上下文预算；超出时从最旧的一条开始丢）
    _ANNOTATION_INJECT_CHAR_CAP = 1500
    # 写回蒸馏最多带多少字对话（超出只取最近部分）
    _ANNOTATION_DISTILL_CONVO_CHARS = 4000
    # prompt 里最多列多少条已有未解批注（防止文件积压后 prompt 过长）
    _ANNOTATION_EXISTING_PROMPT_MAX = 10

    # ------------------------- 配置与基础 -------------------------

    def _annotations_enabled(self) -> bool:
        """批注总开关（默认关闭；关闭时全链路零额外调用、零行为变化）。"""
        return bool(self.config.get("annotation_enabled", False))

    def _annotation_cfg(self) -> dict:
        """把配置归一化成批注配置 dict（非法值回退默认，绝不抛）。"""
        try:
            max_per_round = int(self.config.get("annotation_max_per_round", 5))
        except (TypeError, ValueError):
            max_per_round = 5
        max_per_round = max(1, min(5, max_per_round))
        try:
            timeout = int(self.config.get("annotation_timeout", 30))
        except (TypeError, ValueError):
            timeout = 30
        timeout = max(5, min(300, timeout))
        return {
            "annotation_enabled": self._annotations_enabled(),
            "annotation_max_per_round": max_per_round,
            "annotation_question_hint": bool(
                self.config.get("annotation_question_hint", True)
            ),
            "annotation_timeout": timeout,
        }

    def _now_iso(self) -> str:
        try:
            return self._now().strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:  # pragma: no cover - _now 理论上不会失败
            return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def _sanitize_annotation_text(raw, limit: int) -> str:
        """批注类文本消毒：去不可见/控制字符、折空白、去闭合注入标签、截断。"""
        text = strip_invisible(str(raw or ""))
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(
            r"</\s*memory_diary\s*>", "< /memory_diary>", text, flags=re.IGNORECASE
        )
        if limit > 0:
            text = text[:limit].strip()
        return text

    @staticmethod
    def _extract_json_payload(raw):
        """从模型输出里抠出 JSON 对象/数组（容忍 ```json 围栏与前后废话）。

        P1-1 修复：模型直接输出纯数组 `[{...}]` 时，不能先找 `{`——那样会截到
        数组内对象、解析成 dict，上层按 `payload["annotations"]` 取不到数组。
        顺序是：整段先试 → 按"最先出现的括号"决定外层 → 两种括号各退一步。
        """
        text = str(raw or "").strip()
        if not text:
            return None
        fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
        if fenced:
            text = fenced.group(1).strip()
        # 1) 整段就是合法 JSON（纯数组 / 纯对象 / 带围栏）
        try:
            return json.loads(text)
        except Exception:
            pass
        # 2) 前后有废话：以最先出现的 { 或 [ 为外层，配对到最近的同类闭合符
        first_obj = text.find("{")
        first_arr = text.find("[")
        if first_arr >= 0 and (first_obj < 0 or first_arr < first_obj):
            start, opener, closer = first_arr, "[", "]"
        elif first_obj >= 0:
            start, opener, closer = first_obj, "{", "}"
        else:
            return None
        end = text.rfind(closer)
        if end > start:
            try:
                return json.loads(text[start: end + 1])
            except Exception:
                pass
        # 3) 兜底：两种括号组合各试一次
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start: end + 1])
                except Exception:
                    continue
        return None

    @staticmethod
    def _as_bool(value) -> bool:
        """模型可能把 true/false 写成字符串；"false"/"0"/"否" 必须解析为 False。"""
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "y", "是", "真")
        return bool(value)

    @staticmethod
    def _entry_day_of(entry) -> str:
        """annotations.json 的 entry 字段（"20261031.md"）→ "20261031"。"""
        return str(entry or "").strip()[:8]

    def _annotation_quota(self, diaries_text: str) -> int:
        """按正文字数给额度：每约 300 字一条，至少 1 条、至多配置上限。"""
        cfg = self._annotation_cfg()
        text = re.sub(r"\s+", "", str(diaries_text or ""))
        if not text:
            return 0
        quota = max(1, len(text) // 300)
        return max(1, min(cfg["annotation_max_per_round"], quota))

    # ------------------------- 生成（reset 注入时同步调用） -------------------------

    def _build_annotation_prompt(self, diaries_text: str, quota: int, persona: str,
                                 existing: list) -> str:
        cfg = self._annotation_cfg()
        try:
            days = int(self.config.get("inject_default_days", 7) or 0)
        except (TypeError, ValueError):
            days = 0
        days_text = str(days) if days > 0 else "这些"
        # 范围短语：避免 days<=0（注入全部日记）时渲染出"这 这些 天"这样的病句
        days_scope = f"这 {days} 天" if days > 0 else "这些天"
        if cfg["annotation_question_hint"]:
            question_hint = ANNOTATION_QUESTION_HINT_ON.replace(
                "{{days_scope}}", days_scope
            )
        else:
            question_hint = ANNOTATION_QUESTION_HINT_OFF
        existing_block = ""
        if existing:
            lines = []
            for item in existing[-self._ANNOTATION_EXISTING_PROMPT_MAX:]:
                anchor = str(item.get("anchor") or "").strip()
                note = str(item.get("note") or "").strip()
                if not anchor or not note:
                    continue
                created = str(item.get("created_at") or "")[:10]
                lines.append(f'- {created} 读，锚于"{anchor}"："{note}"')
            if lines:
                existing_block = ANNOTATION_EXISTING_BLOCK.replace(
                    "{{lines}}", "\n".join(lines)
                )
        prompt = ANNOTATION_GEN_PROMPT
        replacements = (
            ("{{days}}", days_text),
            ("{{days_scope}}", days_scope),
            ("{{persona}}", persona.strip() or "（管理员未配置人设，按你自己的性格来）"),
            ("{{quota}}", str(quota)),
            ("{{question_hint}}", question_hint),
            ("{{existing}}", existing_block),
            ("{{diaries}}", diaries_text),
        )
        for key, value in replacements:
            prompt = prompt.replace(key, value)
        return prompt

    def _anchorable_entry_text(self, sender_id: str, entry_name: str,
                               prov_entries: dict) -> str:
        """批注"可锚文本"：原文里 her 自编的内容替换为统一占位符。

        两类内容不可锚：
        - 分隔条可识别的"无新对话·她的日常随笔"片段（整段都是 AI 编的）；
        - 写作时被 <self>…</self> 标出、记入来源侧车的自发挥原文子串。
        替换（而非删除）让文本保持可读，批注模型看到占位符就知道那里不批。
        """
        content, tampered = self._load_entry(sender_id, entry_name)
        if tampered or not content:
            return ""
        rec = (prov_entries or {}).get(str(entry_name).split("/")[-1])
        self_list = rec.get("self") if isinstance(rec, dict) else None
        if not isinstance(self_list, list):
            self_list = []
        parts = []
        for seg in self._segments(content):
            header, body = seg["header"], seg["text"]
            if header:
                parts.append(header)
            if self._segment_info(header)["mode"] == "life":
                if body.strip():
                    parts.append(ANNOTATION_SELF_PLACEHOLDER)
                continue
            for sub in self_list:
                sub = str(sub or "")
                if sub and sub in body:
                    body = body.replace(sub, ANNOTATION_SELF_PLACEHOLDER)
            parts.append(body)
        return "\n".join(p for p in parts if p)

    def _annotation_day_texts(self, sender_id: str, used_names: list) -> list:
        """构造 [(entry_name, day, 可锚文本, 检索文本)]。

        可锚文本 = life 片段与 <self> 子串已替换占位符的版本，用于批注 prompt；
        检索文本 = 可锚文本 + 一份图片占位化副本，用于锚点校验
        （锚点引用图片占位写法时也能命中）。
        """
        try:
            prov, status = self.load_provenance(sender_id)
        except Exception:
            prov, status = {"entries": {}}, "missing"
        prov_entries = prov.get("entries") if status == "ok" else {}
        out = []
        for name in used_names or []:
            try:
                anchorable = self._anchorable_entry_text(
                    sender_id, name, prov_entries
                )
            except Exception:
                continue
            if not anchorable:
                continue
            try:
                placeholder = self._image_macro_to_placeholder(anchorable)
            except Exception:
                placeholder = anchorable
            out.append(
                (name, self._entry_day(name), anchorable, anchorable + "\n" + placeholder)
            )
        return out

    @staticmethod
    def _next_annotation_seq(data: dict, day: str) -> int:
        used = set()
        for item in (data or {}).get("annotations") or []:
            m = re.match(r"^ann_(\d{8})_(\d+)$", str(item.get("id") or ""))
            if m and m.group(1) == day:
                used.add(int(m.group(2)))
        return (max(used) + 1) if used else 1

    @staticmethod
    def _parse_annotation_payload(raw):
        """解析生成结果。返回 (title, [候选 item])；兼容对象与纯数组两种输出。"""
        payload = AnnotationMixin._extract_json_payload(raw)
        title, items = "", []
        if isinstance(payload, dict):
            title = str(payload.get("title") or "").strip()
            arr = payload.get("annotations")
            if isinstance(arr, list):
                items = arr
        elif isinstance(payload, list):
            items = payload
        cleaned = [item for item in items if isinstance(item, dict)]
        return title, cleaned

    async def _generate_annotations(self, sender_id: str, umo: str,
                                    diaries_text: str, used_names: list,
                                    is_group: bool = False) -> dict:
        """读日记同步生成一批批注并落盘。**永不抛异常**。

        返回 {"created": int, "title": str, "round": str, "reason": str}；
        失败 reason ∈ disabled / group / no_diary / no_provider / timeout /
        llm_error / parse_error / no_valid / save_failed / error。
        """
        result = {"created": 0, "title": "", "round": "", "reason": ""}
        try:
            if not self._annotations_enabled():
                return {**result, "reason": "disabled"}
            # 群聊不生成批注（管理员拍板：批注仅私聊，避免私人批注进群上下文）
            if is_group:
                return {**result, "reason": "group"}
            diaries_text = str(diaries_text or "").strip()
            used_names = [str(n) for n in (used_names or []) if str(n).strip()]
            quota = self._annotation_quota(diaries_text)
            if not diaries_text or quota <= 0 or not used_names:
                return {**result, "reason": "no_diary"}
            # 批注读的是"可锚版本"：无新对话随笔与 <self> 自发挥已替换成占位符，
            # 模型从源头就不会把批注落在她自编的内容上；老日记没有来源记录时
            # 退化为只按随笔模式过滤，行为与旧版一致。
            day_texts = self._annotation_day_texts(sender_id, used_names)
            prompt_blocks = [
                f"【{self._entry_label(name)} 的日记】\n{anchorable}"
                for name, _day, anchorable, _search in day_texts
            ]
            budget = max(len(diaries_text), 200)
            while (
                len(prompt_blocks) > 1
                and sum(len(b) for b in prompt_blocks) > budget
            ):
                prompt_blocks.pop(0)  # 超预算从最旧的开始丢，与注入截断同口径
            prompt_diaries = "\n\n".join(prompt_blocks)
            if not prompt_diaries.strip():
                return {**result, "reason": "no_diary"}
            quota = self._annotation_quota(prompt_diaries)
            if quota <= 0:
                return {**result, "reason": "no_diary"}
            cfg = self._annotation_cfg()
            try:
                provider = await self.context.get_using_provider_async(umo=umo)
            except Exception as e:
                logger.warning(f"[memory_diary] 获取批注生成模型失败: {e}")
                return {**result, "reason": "no_provider"}
            if provider is None:
                return {**result, "reason": "no_provider"}
            persona = ""
            if bool(self.config.get("use_persona", True)):
                try:
                    persona = await self._get_persona_prompt(umo)
                except Exception:
                    persona = ""
            data, status = self.load_annotations(sender_id)
            if status not in ("ok", "missing"):
                logger.warning(
                    f"[memory_diary] 批注文件状态异常（{status}），本轮不生成批注"
                )
                return {**result, "reason": "save_failed"}
            existing = [
                x
                for x in data.get("annotations") or []
                if x.get("status") in ("open", "deferred")
            ]
            prompt = self._build_annotation_prompt(
                prompt_diaries, quota, persona, existing
            )
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        system_prompt=ANNOTATION_GEN_SYSTEM_PROMPT,
                    ),
                    timeout=cfg["annotation_timeout"],
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[memory_diary] 批注生成超时（{cfg['annotation_timeout']}s），本轮跳过"
                )
                return {**result, "reason": "timeout"}
            except Exception as e:
                logger.warning(f"[memory_diary] 批注生成调用失败，本轮跳过: {e}")
                return {**result, "reason": "llm_error"}

            title, candidates = self._parse_annotation_payload(
                getattr(resp, "completion_text", "")
            )
            if not candidates:
                logger.warning("[memory_diary] 批注生成结果不是合法 JSON 数组，本轮跳过")
                return {**result, "reason": "parse_error"}

            now = self._now()
            day = now.strftime("%Y%m%d")
            now_iso = self._now_iso()
            round_id = f"r_{now.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"
            seq = self._next_annotation_seq(data, day)
            seen = {
                (
                    str(x.get("anchor") or "").strip(),
                    str(x.get("note") or "").strip(),
                )
                for x in data.get("annotations") or []
            }
            accepted = []
            for item in candidates:
                if len(accepted) >= quota:
                    break
                anchor = self._sanitize_annotation_text(
                    item.get("anchor"), ANNOTATION_ANCHOR_MAX
                )
                note = self._sanitize_annotation_text(
                    item.get("note"), ANNOTATION_NOTE_MAX
                )
                if not anchor or not note:
                    continue
                # 锚点不允许落在/引用"她的私人随笔"占位符（模型照抄占位符时挡住）
                if ANNOTATION_SELF_PLACEHOLDER in anchor:
                    continue
                # 锚点必须真的出现在本轮读过的日记可锚文本里（防模型编造锚点；
                # 也用于确定 entry；落在占位符/随笔里的锚点在这里被硬过滤）
                entry_day = ""
                for _name, day, _anchorable, search in day_texts:
                    if anchor in search:
                        entry_day = day
                        break
                if not entry_day:
                    logger.warning(
                        f"[memory_diary] 批注锚点在日记原文里找不到，已丢弃：{anchor!r}"
                    )
                    continue
                key = (anchor, note)
                if key in seen:
                    continue
                seen.add(key)
                accepted.append(
                    {
                        "id": f"ann_{day}_{seq}",
                        "round": round_id,
                        "anchor": anchor,
                        "entry": f"{entry_day}.md",
                        "created_at": now_iso,
                        "note": note,
                        "is_question": self._as_bool(item.get("is_question")),
                        "status": "open",
                        "reply": None,
                    }
                )
                seq += 1
            if not accepted:
                return {**result, "reason": "no_valid"}

            clean_title = (
                self._sanitize_annotation_text(title, ANNOTATION_TITLE_MAX)
                or ANNOTATION_DEFAULT_TITLE
            )

            def _mut(ann_data: dict):
                ann_data.setdefault("annotations", [])
                ann_data["annotations"].extend(accepted)
                ann_data["last_round"] = round_id
                ann_data["last_title"] = clean_title
                ann_data["updated_at"] = now_iso
                return ann_data

            ok, _ = self.update_annotations(sender_id, _mut)
            if not ok:
                logger.warning("[memory_diary] 批注落盘失败（readonly?），本轮作废")
                return {**result, "reason": "save_failed"}
            logger.info(
                f"[memory_diary] 已为用户 {sender_id} 生成 {len(accepted)} 条页边批注"
                f"（轮次 {round_id}）"
            )
            return {
                "created": len(accepted),
                "title": clean_title,
                "round": round_id,
                "reason": "",
            }
        except Exception as e:  # noqa: BLE001 - 批注失败绝不能影响日记注入
            logger.warning(f"[memory_diary] 批注生成异常，本轮跳过: {e}")
            return {**result, "reason": "error"}

    # ------------------------- 注入 -------------------------

    @staticmethod
    def _annotation_status_label(status: str) -> str:
        if status == "resolved":
            return "已解"
        if status == "deferred":
            return "悬置"
        return "未解"

    @staticmethod
    def _strip_speculation_prefix(text: str) -> str:
        """展示时去掉模型自带的"推测："前缀，避免出现"其中推测：推测：…"。"""
        return re.sub(r"^(?:AI\s*)?推测\s*[:：]\s*", "", str(text or "").strip()).strip()

    @staticmethod
    def _annotation_item_lines(idx: int, item: dict) -> list:
        """一条批注的展示行（/查看批注 与 QQ 第五部分共用同一格式）。"""
        created = str(item.get("created_at") or "")[:10]
        status = AnnotationMixin._annotation_status_label(
            str(item.get("status") or "open")
        )
        lines = [
            f"[{idx}] {created} 读 · {status}",
            f'锚于："{item.get("anchor") or ""}"',
            f'批注：{item.get("note") or ""}',
        ]
        reply = item.get("reply") if isinstance(item.get("reply"), dict) else None
        if reply:
            resolved = str(reply.get("resolved_at") or "")[:10]
            resolved_label = resolved[5:] if len(resolved) == 10 else created[5:]
            summary = str(reply.get("summary") or "").strip()
            if summary:
                lines.append(f"（{resolved_label} 聊明：{summary}）")
            speculation = AnnotationMixin._strip_speculation_prefix(
                str(reply.get("speculation_marked") or "")
            )
            if speculation:
                lines.append(f"（其中推测：{speculation}）")
        return lines

    def _select_inject_annotations(self, data: dict) -> list:
        """注入候选：只带 open/deferred，按"最新疑问句优先"取上限，再按时间升序渲染。

        P1-2 修复：先把疑问句稳定排到尾部，再取尾部——超限时优先保住最新的疑问句；
        随后恢复时间升序供渲染。
        """
        cfg = self._annotation_cfg()
        candidates = [
            x
            for x in data.get("annotations") or []
            if x.get("status") in ("open", "deferred")
        ]
        if not candidates:
            return []
        ordered = sorted(candidates, key=lambda x: str(x.get("created_at") or ""))
        # 陈述句排前、疑问句排后（同组内仍按时间升序）→ 尾部优先是疑问句
        ordered.sort(key=lambda x: 1 if x.get("is_question") else 0)
        selected = ordered[-cfg["annotation_max_per_round"]:]
        selected.sort(key=lambda x: str(x.get("created_at") or ""))
        return selected

    @staticmethod
    def _safe_inject_text(raw) -> str:
        """注入前的最后一道消毒：批注文本会进 <memory_diary> 块，闭合标签必须拆掉。"""
        return re.sub(
            r"</\s*memory_diary\s*>",
            "< /memory_diary>",
            str(raw or ""),
            flags=re.IGNORECASE,
        )

    def _render_inject_block(self, title: str, selected: list) -> str:
        title = self._safe_inject_text(title)
        lines = [f"【页边批注 · {title}】（你上一轮读这些日记时在页边留下的字）", ""]
        for item in selected:
            day_label = self._entry_label(str(item.get("entry") or "")) or "某天"
            created = str(item.get("created_at") or "")[:10]
            status = self._annotation_status_label(
                str(item.get("status") or "open")
            )
            anchor = self._safe_inject_text(item.get("anchor"))
            note = self._safe_inject_text(item.get("note"))
            lines.append(
                f'〔页边批注 {created} 读〕锚于 {day_label} 日记"{anchor}"'
            )
            lines.append(f'    "{note}（{status}）"')
            lines.append("")
        return "\n".join(lines).rstrip()

    def _annotation_inject_block(self, sender_id: str,
                                 char_cap: int = None) -> str:
        """随日记一起注入的批注块；无批注/关闭/验签失败 → ""（日记照常注入）。

        char_cap：本轮还能给批注块的字数（由调用方按"日记已占多少注入预算"算）。
        None = 用类默认上限 1500；< 100 时返回 ""（预算不足就不注入，避免把
        日记挤出 inject_max_chars；批注仍可用 /查看批注 翻看）。
        """
        try:
            if not self._annotations_enabled():
                return ""
            cap = (
                self._ANNOTATION_INJECT_CHAR_CAP
                if char_cap is None
                else max(0, int(char_cap))
            )
            if char_cap is not None and cap < 100:
                # 预算已被日记占满：本轮不注入批注（只记日志，不打扰用户）
                logger.info("[memory_diary] 注入预算不足，本轮跳过页边批注块")
                return ""
            data, status = self.load_annotations(sender_id)
            if status == "missing":
                return ""
            if status != "ok":
                logger.warning(
                    f"[memory_diary] 批注文件状态异常（{status}），本轮不注入批注"
                )
                return ""
            selected = self._select_inject_annotations(data)
            if not selected:
                return ""
            title = (
                self._sanitize_annotation_text(
                    data.get("last_title"), ANNOTATION_TITLE_MAX
                )
                or ANNOTATION_DEFAULT_TITLE
            )
            # 预算保险丝：超出字数上限时从最旧的一条开始丢，至少留一条
            while selected:
                block = self._render_inject_block(title, selected)
                if len(block) <= cap or len(selected) == 1:
                    break
                selected = selected[1:]
            block = self._render_inject_block(title, selected)
            if len(block) > cap:
                block = block[:cap].rstrip()
            return block + "\n\n" + ANNOTATION_INJECT_RULES
        except Exception as e:  # noqa: BLE001 - 注入绝不能因批注失败
            logger.warning(f"[memory_diary] 组装批注注入块失败，本轮不注入: {e}")
            return ""

    # ------------------------- /查看批注 -------------------------

    def _select_view_annotations(self, data: dict) -> list:
        """`/查看批注` 的展示集合：本轮注入时产生的 + 所有未解决/悬置的。"""
        last_round = str(data.get("last_round") or "")
        records = [
            x
            for x in data.get("annotations") or []
            if str(x.get("round") or "") == last_round
            or x.get("status") in ("open", "deferred")
        ]
        records.sort(key=lambda x: str(x.get("created_at") or ""))
        deduped, seen = [], set()
        for item in records:
            item_id = str(item.get("id") or "")
            if item_id and item_id in seen:
                continue
            seen.add(item_id)
            deduped.append(item)
        return deduped[-ANNOTATION_VIEW_MAX:]

    def annotations_view_has_any(self, sender_id: str) -> bool:
        data, status = self.load_annotations(sender_id)
        if status != "ok":
            return False
        return bool(self._select_view_annotations(data))

    def annotations_view_text(self, sender_id: str) -> str:
        """`/查看批注` 的完整回复文本（固定文案，不额外调用模型）。"""
        if not self._annotations_enabled():
            return "📖 批注功能未开启（管理员可在配置页开启）。"
        data, status = self.load_annotations(sender_id)
        if status == "no_key":
            return (
                "📖 签名密钥（.diary_key）不可读，我没法验签批注——"
                "请管理员检查插件数据目录里的密钥文件。"
            )
        if status == "tampered":
            return (
                "📖 页边批注验签失败，疑似在插件之外被改过，已拒绝展示。"
                "如果这不是你改的，请管理员从备份恢复。"
            )
        items = self._select_view_annotations(data)
        if not items:
            return "📖 这轮读日记，我还没有在页边留下批注。"
        try:
            days = int(self.config.get("inject_default_days", 7) or 0)
        except (TypeError, ValueError):
            days = 0
        if days > 0:
            head = f"📖 这 {days} 天日记里，我留下了 {len(items)} 条批注："
        else:
            head = f"📖 这些日记里，我留下了 {len(items)} 条批注："
        blocks = [
            "\n".join(self._annotation_item_lines(idx, item))
            for idx, item in enumerate(items, 1)
        ]
        return head + "\n\n" + "\n\n".join(blocks)

    # ------------------------- QQ 第五部分 -------------------------

    def qq_annotation_section_text(self, sender_id: str, day: str) -> str:
        """某天日记的第五部分（标题 + 条目，条间空行）；无批注/关闭 → ""。"""
        if not self._annotations_enabled():
            return ""
        try:
            items, status = self.annotations_for_day(sender_id, day)
            if status == "tampered":
                return "（页边批注验签失败，已略去）"
            if status == "no_key":
                return "（签名密钥不可用，页边批注无法验签，已略去）"
            if not items:
                return ""
            data, _ = self.load_annotations(sender_id)
            title = (
                self._sanitize_annotation_text(
                    data.get("last_title"), ANNOTATION_TITLE_MAX
                )
                or ANNOTATION_DEFAULT_TITLE
            )
            blocks = [title]
            for idx, item in enumerate(items, 1):
                blocks.append("\n".join(self._annotation_item_lines(idx, item)))
            return "\n\n".join(blocks)
        except Exception as e:  # noqa: BLE001 - 呈现失败不连累日记正文
            logger.warning(f"[memory_diary] 组装 QQ 批注第五部分失败: {e}")
            return ""

    # ------------------------- WebUI -------------------------

    def annotations_web_payload(self, sender_id: str, day: str) -> tuple:
        """返回 (该天批注列表, tampered)。关闭时静默隐藏。"""
        if not self._annotations_enabled():
            return [], False
        try:
            items, status = self.annotations_for_day(sender_id, day)
            if status == "tampered":
                return [], True
            if status != "ok":
                return [], False
            payload = []
            for item in items:
                reply = (
                    item.get("reply")
                    if isinstance(item.get("reply"), dict)
                    else None
                )
                if reply is not None:
                    # 展示层（前端）会自己加"推测："前缀，这里先去掉模型自带的前缀，
                    # 避免出现"推测：推测：…"；同时只暴露展示需要的字段。
                    reply = {
                        "resolved_at": str(reply.get("resolved_at") or ""),
                        "reply_title": str(reply.get("reply_title") or ""),
                        "summary": str(reply.get("summary") or ""),
                        "speculation_marked": self._strip_speculation_prefix(
                            str(reply.get("speculation_marked") or "")
                        ),
                    }
                payload.append(
                    {
                        "id": str(item.get("id") or ""),
                        "anchor": str(item.get("anchor") or ""),
                        "note": str(item.get("note") or ""),
                        "is_question": bool(item.get("is_question")),
                        "status": str(item.get("status") or "open"),
                        "created_at": str(item.get("created_at") or ""),
                        "reply": reply,
                    }
                )
            return payload, False
        except Exception as e:  # noqa: BLE001 - Web 读取不能让日记本页面 500
            logger.warning(f"[memory_diary] Web 批注数据组装失败: {e}")
            return [], False

    # ------------------------- /写回批注 -------------------------

    def _select_writeback_annotations(self, sender_id: str):
        """可写回批注（open/deferred，按时间升序）。返回 (列表, status)。"""
        data, status = self.load_annotations(sender_id)
        if status != "ok":
            return [], status
        items = [
            x
            for x in data.get("annotations") or []
            if x.get("status") in ("open", "deferred")
        ]
        items.sort(key=lambda x: str(x.get("created_at") or ""))
        return items[-ANNOTATION_VIEW_MAX:], status

    def open_annotation_list_text(self, sender_id: str) -> str:
        if not self._annotations_enabled():
            return "📖 批注功能未开启（管理员可在配置页开启）。"
        items, status = self._select_writeback_annotations(sender_id)
        if status == "tampered":
            return "📖 页边批注验签失败，疑似被插件之外改过，已拒绝操作。"
        if status == "no_key":
            return (
                "📖 签名密钥（.diary_key）不可读，我没法验签批注——"
                "请管理员检查插件数据目录里的密钥文件。"
            )
        if not items:
            return "📖 现在没有可以写回的批注。"
        lines = [f"📖 现在可以写回的批注有 {len(items)} 条：", ""]
        for idx, item in enumerate(items, 1):
            created = str(item.get("created_at") or "")[:10]
            kind = "疑问" if item.get("is_question") else "陈述"
            lines.append(
                f'[{idx}] {created} 读 · {kind} · 锚于"{item.get("anchor")}"'
            )
            lines.append(f'    {item.get("note")}')
            lines.append("")
        lines.append("回复序号选择要写回的那条（60 秒内有效），回复「取消」退出。")
        return "\n".join(lines).rstrip()

    def annotation_by_index(self, sender_id: str, idx: int):
        """按 open_annotation_list_text 的编号取批注 record（含 id）；越界返回 None。"""
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return None
        items, status = self._select_writeback_annotations(sender_id)
        if status != "ok" or idx < 1 or idx > len(items):
            return None
        return dict(items[idx - 1])

    async def _distill_annotation_reply(self, sender_id: str, umo: str,
                                        ann: dict):
        """用当前会话讨论内容蒸馏写回草稿；失败返回 None（不写坏数据）。"""
        try:
            cfg = self._annotation_cfg()
            try:
                provider = await self.context.get_using_provider_async(umo=umo)
            except Exception as e:
                logger.warning(f"[memory_diary] 写回蒸馏获取模型失败: {e}")
                return None
            if provider is None:
                logger.warning("[memory_diary] 写回蒸馏没有可用模型")
                return None
            try:
                history = await self._get_history(umo)
            except Exception:
                history = []
            convo = ""
            if history:
                try:
                    convo = self._filter_diary_ops(self._flatten_history(history))
                except Exception:
                    convo = self._flatten_history(history)
            convo = (convo or "").strip()[-self._ANNOTATION_DISTILL_CONVO_CHARS:]
            if not convo:
                logger.info("[memory_diary] 会话里还没有可蒸馏的讨论，写回中止")
                return None
            persona = ""
            if bool(self.config.get("use_persona", True)):
                try:
                    persona = await self._get_persona_prompt(umo)
                except Exception:
                    persona = ""
            prompt = ANNOTATION_DISTILL_PROMPT
            for key, value in (
                ("{{anchor}}", str(ann.get("anchor") or "")),
                ("{{note}}", str(ann.get("note") or "")),
                ("{{conversation}}", convo),
            ):
                prompt = prompt.replace(key, value)
            system_prompt = ANNOTATION_DISTILL_SYSTEM_PROMPT
            if persona:
                system_prompt += "\n\n【你的人设】\n" + persona
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt, system_prompt=system_prompt
                    ),
                    timeout=cfg["annotation_timeout"],
                )
            except asyncio.TimeoutError:
                logger.warning("[memory_diary] 写回蒸馏超时")
                return None
            except Exception as e:
                logger.warning(f"[memory_diary] 写回蒸馏调用失败: {e}")
                return None
            payload = self._extract_json_payload(
                getattr(resp, "completion_text", "")
            )
            if not isinstance(payload, dict):
                logger.warning("[memory_diary] 写回蒸馏结果不是合法 JSON 对象")
                return None
            summary = self._sanitize_annotation_text(
                payload.get("summary"), ANNOTATION_SUMMARY_MAX
            )
            if not summary:
                return None
            return {
                "reply_title": self._sanitize_annotation_text(
                    payload.get("reply_title"), ANNOTATION_REPLY_TITLE_MAX
                )
                or ANNOTATION_DEFAULT_REPLY_TITLE,
                "summary": summary,
                "speculation_marked": self._sanitize_annotation_text(
                    payload.get("speculation_marked"), ANNOTATION_SUMMARY_MAX
                ),
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory_diary] 写回蒸馏异常: {e}")
            return None

    def annotation_draft_text(self, ann: dict, draft: dict) -> str:
        """给用户签字的草稿（批注原文 / 回复标题 / 摘要 / 推测分开呈现）。"""
        lines = [
            "📖 我拟好了要写回这条批注的内容：",
            "",
            f'· 锚于："{ann.get("anchor") or ""}"',
            f'· 批注：{ann.get("note") or ""}',
            f"· 回复标题：{draft.get('reply_title') or ANNOTATION_DEFAULT_REPLY_TITLE}",
            f"· 摘要：{draft.get('summary') or ''}",
        ]
        speculation = self._strip_speculation_prefix(
            str(draft.get("speculation_marked") or "")
        )
        if speculation:
            lines.append(f"· 其中推测：{speculation}")
        lines += ["", "回复「确认」写回；回复「悬置」先放着；回复「取消」不写。"]
        return "\n".join(lines)

    def apply_annotation_reply(self, sender_id: str, ann_id: str,
                               draft: dict) -> bool:
        """签字写回：status=resolved，reply 落盘（confirmed_by_user 只在这里为 True）。"""
        ann_id = str(ann_id or "").strip()
        if not ann_id:
            return False
        reply = {
            "resolved_at": self._now_iso(),
            "reply_title": self._sanitize_annotation_text(
                draft.get("reply_title"), ANNOTATION_REPLY_TITLE_MAX
            )
            or ANNOTATION_DEFAULT_REPLY_TITLE,
            "summary": self._sanitize_annotation_text(
                draft.get("summary"), ANNOTATION_SUMMARY_MAX
            ),
            "confirmed_by_user": True,
            "speculation_marked": self._sanitize_annotation_text(
                draft.get("speculation_marked"), ANNOTATION_SUMMARY_MAX
            ),
        }
        if not reply["summary"]:
            return False

        hit = {"found": False}

        def _mut(data: dict):
            for item in data.get("annotations") or []:
                if str(item.get("id") or "") == ann_id:
                    item["status"] = "resolved"
                    item["reply"] = reply
                    hit["found"] = True
            data["updated_at"] = self._now_iso()
            return data

        ok, _ = self.update_annotations(sender_id, _mut)
        # P2-2：id 不存在时不能报"成功"（否则用户以为写回了、实际什么都没写）
        return bool(ok and hit["found"])

    def defer_annotation(self, sender_id: str, ann_id: str) -> bool:
        """悬置：status=deferred（下一轮 reset 仍会作为路标带回来）。"""
        ann_id = str(ann_id or "").strip()
        if not ann_id:
            return False

        hit = {"found": False}

        def _mut(data: dict):
            for item in data.get("annotations") or []:
                if str(item.get("id") or "") == ann_id:
                    item["status"] = "deferred"
                    hit["found"] = True
            data["updated_at"] = self._now_iso()
            return data

        ok, _ = self.update_annotations(sender_id, _mut)
        return bool(ok and hit["found"])
