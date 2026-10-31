# -*- coding: utf-8 -*-
"""日记插图编排（v0.6 D3/D4/D5 的领域部分）：闸门、标记、用户照片、异步落图。

设计第 5 节"异步管线（日记先行，图后到）"的落点：

    写日记 → AI 在正文里留插图标记 → 程序算闸门（AI 意愿 × 概率 × 限额）
    → 允许的标记换成 PENDING 占位引用，正文立即落盘
    → 后台任务生图 → 成功：图片入 zip + 占位替换 + 推送"插图到了"
                     → 失败：删掉占位行（静默，不阻塞日记与对话）

解耦纪律（设计第 12 节）：插图逻辑全部在本模块；zip/签名等公共能力在
storage.py；HTTP 通道在 imaging.py；QQ/WebUI 适配在 main.py / views。
本模块不 import AstrBot（发送消息时才做函数内延迟 import，沿用 service.py 的
既有写法）。

形象一致性（设计第 4 节）：每次生成都原样嵌入 persona_image_desc；为空时
先走初始化仪式（提炼 → 用户 /形象 确认 → 落盘），确认前不生图；
AI 可用〔形象提议：…〕提议进化，但必须用户签字才改写配置。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import random
import re
import secrets
from pathlib import Path

from .common import sniff_image_mime, strip_invisible
from .constants import (
    IMAGE_DECISION_PROMPT,
    IMAGE_GEN_PROMPT_NO_PERSONA,
    IMAGE_GEN_PROMPT_TEMPLATE,
    IMAGE_MAX_BYTES,
    IMAGE_ORIENTATION_HINTS,
    IMAGE_PHOTO_ONLY_PROMPT,
    PERSONA_IMAGE_DESC_PROMPT,
)
from .log import logger


class IllustrationMixin:
    """插图决策与异步管线；由 MemoryDiaryCore 组合。"""

    # ---- 日记正文里的机器标记（解析后必须从正文移除）----
    _AI_MARKER_RE = re.compile(r"〔\s*插图\s*[:：]\s*([^〕]+?)〕")
    _PHOTO_MARKER_RE = re.compile(r"〔\s*用户照片\s*[:：]\s*([^〕]+?)〕")
    _PERSONA_MARKER_RE = re.compile(r"〔\s*形象提议\s*[:：]\s*([^〕]+?)〕")
    # 正文里已落盘的图片语法（QQ/Web 呈现与注入占位都按这个解析）
    _IMAGE_MD_RE = re.compile(
        r"!\[(?P<alt>[^\]]*)\]\((?P<name>images/[^)\s]+)\)"
    )
    _USER_PHOTO_ALT_PREFIX = "用户分享的照片："
    _PENDING_NAME_RE = re.compile(r"images/PENDING-[0-9a-f]{6,16}\.png$")

    # ------------------------- 开关 / 闸门 -------------------------

    def _image_enabled(self) -> bool:
        """插图功能总开关（image_enabled，默认关）。"""
        try:
            return bool((self.config or {}).get("image_enabled", False))
        except Exception:
            return False

    def _image_gate(self, sender_id: str, is_group: bool, mode: str,
                    wanted: int) -> int:
        """闸门：返回本次真正允许生成的插图张数（0 ~ wanted）。

        触发公式（设计第 2 节）：
            AI 意愿（wanted）× 概率闸门（image_prob_*）× 限额（image_limit_*）
        群聊先过 image_limit_group（默认 0 = 群聊不插图），最后按
        image_limit_daily 的今日余量截断。这里只算数、不占额度；
        真正建任务时由 _image_usage_consume() 记账。
        """
        if not self._image_enabled():
            return 0
        try:
            wanted = max(0, int(wanted))
        except (TypeError, ValueError):
            return 0
        if wanted <= 0:
            return 0
        cfg = self._image_cfg()
        is_full = mode == "full"
        prob = cfg["image_prob_full"] if is_full else cfg["image_prob_memo"]
        limit = cfg["image_limit_full"] if is_full else cfg["image_limit_memo"]
        if is_group:
            group_limit = cfg["image_limit_group"]
            if group_limit <= 0:
                return 0  # 群聊不插图（默认）
            limit = min(limit, group_limit)
        if limit <= 0:
            return 0
        allowed = min(wanted, limit)
        # 概率闸门逐张掷：调高概率更容易"想画就画"，但 AI 意愿仍是前提
        passed = 0
        for _ in range(allowed):
            if random.random() < prob:
                passed += 1
        if passed <= 0:
            return 0
        return min(passed, self._image_usage_remaining())

    def _count_ai_image_markers(self, content: str) -> int:
        """数一数 AI 在正文里表达了几次插图意愿（闸门输入）。"""
        try:
            return len(self._AI_MARKER_RE.findall(content or ""))
        except Exception:
            return 0

    # ------------------------- 写日记 prompt 增强 -------------------------

    def _image_prompt_addon(self, mode: str, user_photos: list,
                            persona_desc: str,
                            recent_images: list) -> str:
        """写日记时追加的插图决策提示词；开关关闭返回空串（零额外消耗）。"""
        if not self._image_enabled():
            return ""
        cfg = self._image_cfg()
        max_ai = cfg["image_limit_full"] if mode == "full" else cfg["image_limit_memo"]
        if max_ai <= 0:
            max_ai = 0
        photo_limit = cfg["user_image_limit"] if cfg["user_image_enabled"] else 0
        if photo_limit <= 0 or not user_photos:
            photo_list = "（这段对话里没有可收的用户照片）"
            photo_limit = 0
        else:
            lines = []
            for p in user_photos:
                caption = str(p.get("caption") or "").strip() or "（没有配文）"
                vision_desc = str(p.get("vision_desc") or "").strip()
                if vision_desc:
                    lines.append(
                        f"{p.get('index')}. 配文：{caption}；"
                        f"画面描述（视觉接口）：{vision_desc}"
                    )
                else:
                    lines.append(
                        f"{p.get('index')}. 配文：{caption}；"
                        "画面内容未识别（只写你知道的，别猜）"
                    )
            photo_list = "\n".join(lines)
        # 简记/随笔默认既不画也不收照片：直接不追加任何内容（零 token 开销）
        if max_ai <= 0 and photo_limit <= 0:
            return ""
        if max_ai <= 0:
            # 只收用户照片：不塞插图标记语法，省 token 也避免 AI 写了又被删
            return IMAGE_PHOTO_ONLY_PROMPT.format(
                limit_photo=photo_limit, photo_list=photo_list
            )
        persona = str(persona_desc or "").strip()
        if not persona:
            persona = (
                "（还没有形象档案。想画的时候照常写〔插图〕标记，"
                "程序会先请你和用户一起确认一版形象，这次先不画；"
                "不要自己临时编一个长相。）"
            )
        addon = IMAGE_DECISION_PROMPT.format(
            max_ai=max_ai, limit_photo=photo_limit,
            photo_list=photo_list, persona=persona,
        )
        style = str(cfg["image_style_prompt"] or "").strip()
        if style:
            addon += f"\n\n画风锚（所有插图都保持这个画风）：{style}"
        recent = [str(x).strip() for x in (recent_images or []) if str(x).strip()]
        if recent:
            addon += "\n\n她最近在图里长什么样（保持一致）：\n" + "\n".join(
                f"- {line}" for line in recent[-5:]
            )
        return addon

    # ------------------------- 标记解析 -------------------------

    @staticmethod
    def _sanitize_alt(raw: str, limit: int = 30) -> str:
        """画面描述 → 安全 alt 文本（去方括号/换行/控制字符，截断）。"""
        text = strip_invisible(str(raw or ""))
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
        text = text.replace("[", "").replace("]", "").replace("(", "").replace(")", "")
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit] or "一幅画"

    def _parse_image_markers(self, content: str, allowed_ai: int):
        """解析日记正文里的机器标记。返回 (content, decisions)。

        - 〔插图：描述｜方向〕：前 allowed_ai 个替换成 PENDING 占位引用
          （`![描述](images/PENDING-<token>.png)`），其余整条移除；
        - 〔形象提议：描述〕：移除并作为 decision 交给调度层存"待确认提案"；
        - 〔用户照片：N｜描述〕：**保留在正文里**，由 _apply_user_photo_markers
          解析（它要照序号取真实图片字节）。
        decisions 元素：
          {"kind": "ai", "alt": str, "orientation": "portrait|landscape|square",
           "pending_ref": str, "marker": str}
          {"kind": "persona_proposal", "desc": str}
        """
        try:
            allowed = max(0, int(allowed_ai))
        except (TypeError, ValueError):
            allowed = 0
        decisions: list = []
        used = {"n": 0}
        content = str(content or "")

        def _ai_repl(match):
            raw = match.group(1) or ""
            if used["n"] >= allowed:
                return ""  # 超出限额/AI 意愿未通过：整条删掉
            used["n"] += 1
            alt_raw, orientation_raw = self._split_marker_payload(raw)
            alt = self._sanitize_alt(alt_raw)
            orientation = self._normalize_orientation(orientation_raw)
            token = secrets.token_hex(4)
            pending_ref = f"![{alt}](images/PENDING-{token}.png)"
            decisions.append(
                {
                    "kind": "ai",
                    "alt": alt,
                    "orientation": orientation,
                    "pending_ref": pending_ref,
                    "marker": match.group(0),
                }
            )
            return pending_ref

        content = self._AI_MARKER_RE.sub(_ai_repl, content)

        def _persona_repl(match):
            desc = self._sanitize_alt(match.group(1) or "", limit=60)
            if desc and desc != "一幅画":
                decisions.append({"kind": "persona_proposal", "desc": desc})
            return ""

        content = self._PERSONA_MARKER_RE.sub(_persona_repl, content)

        # 形如 "〔插图：xxx〕/〔形象提议：xxx〕" 的行被整条删掉后会留下空行，折掉
        content = re.sub(r"[ \t]+\n", "\n", content)
        content = re.sub(r"\n{3,}", "\n\n", content).strip("\n")
        return content, decisions

    @staticmethod
    def _split_marker_payload(raw: str):
        """把"描述｜横"拆成 (描述, 方向)；没有分隔符时方向为空。"""
        text = str(raw or "").strip()
        for sep in ("｜", "|"):
            if sep in text:
                alt, _, tail = text.partition(sep)
                return alt.strip(), tail.strip()
        return text, ""

    def _apply_user_photo_markers(self, sender_id: str, content: str,
                                  photos: list, day: str, seq: int):
        """把〔用户照片：N｜描述〕替换成已写进 zip 的图片引用。

        照片在写日记时同步落盘（复用 D1 的 save_image：同一套 zip、
        同一套图片签名、同一套呈现管线）。返回 (content, next_seq)。
        """
        cfg = self._image_cfg()
        limit = cfg["user_image_limit"] if cfg["user_image_enabled"] else 0
        by_index = {
            int(p.get("index")): p for p in (photos or []) if p.get("index")
        }
        used = {"n": 0}
        seq_box = {"v": int(seq or 1)}

        def _repl(match):
            raw = match.group(1) or ""
            idx_text, _, desc_raw = raw.partition("｜")
            if "|" in idx_text:  # 兼容半角分隔
                idx_text, _, desc_raw = raw.partition("|")
            try:
                index = int(re.sub(r"\D", "", idx_text))
            except (TypeError, ValueError):
                return ""
            photo = by_index.get(index)
            if photo is None or used["n"] >= limit:
                return ""  # 没有这张/超上限：标记删掉，不让它进正文
            used["n"] += 1
            desc = self._sanitize_alt(desc_raw, limit=20)
            if desc == "一幅画":
                desc = "内容未识别"
            alt = self._sanitize_alt(
                f"{self._USER_PHOTO_ALT_PREFIX}{desc}", limit=30
            )
            try:
                name = self.save_image(
                    sender_id,
                    photo["data"],
                    day,
                    seq_box["v"],
                    f"照片_{desc}",
                    ext="",
                )
            except Exception as e:
                logger.warning(f"[memory_diary] 用户照片写入日记失败: {e}")
                return ""
            seq_box["v"] += 1
            return f"![{alt}]({name})"

        content = self._PHOTO_MARKER_RE.sub(_repl, content or "")
        content = re.sub(r"[ \t]+\n", "\n", content)
        content = re.sub(r"\n{3,}", "\n\n", content).strip("\n")
        return content, seq_box["v"]

    # ------------------------- 用户照片扫描 -------------------------

    async def _caption_user_photos(self, photos: list) -> list:
        """可选视觉接口：给每张用户照片补一句真实画面描述（设计 7.4）。

        只有管理员配置了 vision_api_* 才会调用；未配置/失败保持空串，
        正文按"内容未识别"诚实降级——绝不臆断画面。隐私：照片只发往
        管理员显式配置的视觉接口，与生图通道相互独立。
        """
        cfg = self._image_cfg()
        if not cfg.get("vision_api_base") or not cfg.get("vision_api_model"):
            return photos
        for photo in photos or []:
            if photo.get("vision_desc"):
                continue
            try:
                desc = await self.caption_image(photo.get("data") or b"")
            except Exception as e:
                logger.warning(f"[memory_diary] 识别用户照片失败（跳过）: {e}")
                desc = ""
            if desc:
                photo["vision_desc"] = desc
        return photos

    def _collect_user_photos(self, history: list, max_n: int = 1) -> list:
        """扫描会话历史里的用户图片组件，取出可入日记的图片字节。

        AstrBot 的历史里用户图片是 content 列表中的 image_url part（通常是
        data URL，也可能是本地路径 / file:// URI）。只收用户消息里的图片
        （设计隐私红线 6：用户照片入日记仅接受用户消息中的图片组件）。
        配文取同一条消息里的文本，供 AI 判断"值不值得进日记"。
        """
        try:
            limit = max(0, int(max_n))
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return []
        out = []
        seen = set()
        for msg in history or []:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            caption = self._content_to_text(content).strip()
            caption = re.sub(r"^\[图片\]\s*", "", caption).strip()
            caption = caption[:120]
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") not in ("image_url", "image", "input_image"):
                    continue
                data, declared_mime = self._read_history_image(part)
                if not data or len(data) < 16:
                    continue
                digest = hashlib.sha256(data).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                out.append(
                    {
                        "index": len(out) + 1,
                        "data": data,
                        "mime": declared_mime or sniff_image_mime(data),
                        "caption": caption,
                        "time": "",
                    }
                )
                if len(out) >= limit:
                    return out
        return out

    @staticmethod
    def _read_history_image(part: dict):
        """从 image_url part 里读出 (图片字节, 声明的 mime)；读不到返回 (b"", "")。

        本地路径/file:// 读文件；data URL 解 base64（mime 取 data URL 声明值，
        与实际字节嗅探不一致时以声明值优先——AstrBot 里 gif 会被转成 jpeg data URL）。
        http(s) 与超过 IMAGE_MAX_BYTES 的一律拒绝（不下载、不进 zip）。
        """
        payload = part.get("image_url")
        if isinstance(payload, dict):
            payload = payload.get("url") or payload.get("path") or ""
        if not isinstance(payload, str):
            payload = str(payload or "")
        url = payload.strip()
        if not url:
            return b"", ""
        try:
            if url.startswith("data:"):
                comma = url.find(",")
                if comma < 0:
                    return b"", ""
                header = url[len("data:"):comma]
                mime = header.split(";", 1)[0].strip().lower()
                data = base64.b64decode(url[comma + 1:], validate=False)
                if not data or len(data) > IMAGE_MAX_BYTES:
                    return b"", ""
                return data, (mime if mime.startswith("image/") else "")
            if url.startswith("base64://"):
                data = base64.b64decode(url[len("base64://"):], validate=False)
                if not data or len(data) > IMAGE_MAX_BYTES:
                    return b"", ""
                return data, ""
            path = url
            if path.startswith("file://"):
                from urllib.parse import unquote

                path = unquote(path[len("file://"):])
                if re.match(r"^/[A-Za-z]:", path):  # Windows: /C:/...
                    path = path[1:]
            if path.startswith("http://") or path.startswith("https://"):
                # 不主动下载外部 URL：避免 SSRF，也不把网络不确定性带进写日记流程
                return b"", ""
            p = Path(path)
            if not p.is_file():
                return b"", ""
            data = p.read_bytes()
            if not data or len(data) > IMAGE_MAX_BYTES:
                return b"", ""
            return data, ""
        except Exception:
            return b"", ""

    # ------------------------- 生图 prompt 组装 -------------------------

    def _build_image_prompt(self, alt: str, orientation: str,
                            sender_id: str = "") -> str:
        """组装单张插图的生图 prompt：形象档案原样嵌入 + 画风锚 + 最近图回顾。"""
        cfg = self._image_cfg()
        persona = str(cfg["persona_image_desc"] or "").strip()
        style = str(cfg["image_style_prompt"] or "").strip()
        if not style:
            style = "手绘、淡彩、柔和光线，像画在日记本页边的小插画"
        recent_lines = (
            self._image_style_summary(sender_id) if sender_id else []
        )
        recent = ""
        if recent_lines:
            recent = "【她最近在图里长什么样（画风与气质保持一致）】\n" + "\n".join(
                f"- {line}" for line in recent_lines
            ) + "\n"
        hint = IMAGE_ORIENTATION_HINTS.get(
            self._normalize_orientation(orientation), IMAGE_ORIENTATION_HINTS["square"]
        )
        template = (
            IMAGE_GEN_PROMPT_TEMPLATE if persona else IMAGE_GEN_PROMPT_NO_PERSONA
        )
        if persona:
            return template.format(
                alt=alt, persona=persona, style=style,
                recent=recent, orientation_hint=hint,
            )
        return template.format(alt=alt, style=style, recent=recent, orientation_hint=hint)

    def _image_style_summary(self, sender_id: str, limit: int = 3) -> list:
        """最近几篇日记里的插图描述（"最近图回顾"，喂给生图 prompt）。"""
        try:
            entries = self._list_entries(sender_id)
        except Exception:
            return []
        alts = []
        for name in entries[-max(1, limit):]:
            try:
                content, tampered = self._load_entry(sender_id, name)
            except Exception:
                continue
            if tampered or not content:
                continue
            for m in self._IMAGE_MD_RE.finditer(content):
                alt = (m.group("alt") or "").strip()
                if alt and not alt.startswith(self._USER_PHOTO_ALT_PREFIX):
                    alts.append(alt)
        # 去重保序，取最后 5 条
        seen, out = set(), []
        for alt in alts:
            if alt in seen:
                continue
            seen.add(alt)
            out.append(alt)
        return out[-5:]

    # ------------------------- 异步调度 -------------------------

    async def _schedule_illustrations(self, sender_id: str, sender_name: str,
                                     umo: str, day: str, mode: str,
                                     decisions: list, cfg: dict) -> None:
        """保存后调度：形象档案把关 + 每日额度 + 后台任务（设计第 5 节）。"""
        ai = [d for d in decisions or [] if d.get("kind") == "ai"]
        proposals = [
            d for d in decisions or [] if d.get("kind") == "persona_proposal"
        ]
        entry_name = self._md_entry(day)
        notice_parts = []

        if proposals:
            desc = proposals[-1].get("desc") or ""
            if desc and self._save_pending_persona_desc(sender_id, desc, "evolve"):
                notice_parts.append(
                    "（她还想给形象做一点小变化，需要你确认：\n"
                    f"「{desc}」\n回复 /形象 确认 就按这个改，/形象 可查看当前档案）"
                )

        if not ai:
            if notice_parts:
                self._image_last_notice = "\n\n" + "\n\n".join(notice_parts)
            return

        # 形象档案为空：初始化仪式——先提炼一版提案给用户确认，这次不画
        if not str(cfg.get("persona_image_desc") or "").strip():
            desc = await self._propose_persona_image_desc(umo)
            if desc and self._save_pending_persona_desc(sender_id, desc, "init"):
                notice_parts.append(
                    "（她本想给这篇日记配一幅画，但你们还没一起定下她长什么样。"
                    "她从人设里先提炼了一版：\n"
                    f"「{desc}」\n这次先不画；回复 /形象 确认 之后，"
                    "以后的日记就能照这份档案画了。也可以 /形象 修改 <你的描述> 直接改。）"
                )
            else:
                notice_parts.append(
                    "（她本想给这篇日记配一幅画，但还没确认过形象档案。"
                    "先用 /形象 定一版她的样子，之后就能画了。）"
                )
            self._remove_pending(sender_id, entry_name, [d["pending_ref"] for d in ai])
            if notice_parts:
                self._image_last_notice = "\n\n" + "\n\n".join(notice_parts)
            return

        # 正常调度：额度不够的占位要清掉，不能留 PENDING 引用在正文里
        skipped = []
        quota_skipped = 0
        for decision in ai:
            if not self._image_usage_consume():
                skipped.append(decision["pending_ref"])
                quota_skipped += 1
                continue
            prompt = self._build_image_prompt(
                decision["alt"], decision["orientation"], sender_id
            )
            try:
                sem = self._get_image_semaphore(cfg["image_concurrency"])
                task = asyncio.create_task(
                    self._run_image_job(
                        sender_id=sender_id,
                        sender_name=sender_name,
                        umo=umo,
                        day=day,
                        pending_ref=decision["pending_ref"],
                        alt=decision["alt"],
                        orientation=decision["orientation"],
                        prompt=prompt,
                        cfg=cfg,
                        sem=sem,
                    )
                )
            except RuntimeError:
                # 没有运行中的事件循环（同步测试/极端场景）：退还额度 + 删除占位
                self._image_usage_refund()
                skipped.append(decision["pending_ref"])
                logger.warning("[memory_diary] 无事件循环，插图任务未创建")
                break
            tasks = getattr(self, "_image_tasks", None)
            if not isinstance(tasks, set):
                tasks = set()
                self._image_tasks = tasks
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if skipped:
            self._remove_pending(sender_id, entry_name, skipped)
        if quota_skipped:
            notice_parts.append(
                f"（今天的插图额度已经用完，{quota_skipped} 幅没有画。）"
            )
        if notice_parts:
            self._image_last_notice = "\n\n" + "\n\n".join(notice_parts)

    def _remove_pending(self, sender_id: str, entry_name: str, refs: list) -> None:
        """把还没开始画的 PENDING 占位从正文里删掉（失败只记日志）。"""
        refs = [r for r in (refs or []) if r]
        if not refs:
            return
        try:
            self.replace_pending_images(sender_id, entry_name, {}, refs)
        except Exception as e:
            logger.warning(f"[memory_diary] 清理插图占位失败: {e}")

    def _get_image_semaphore(self, concurrency: int) -> asyncio.Semaphore:
        """全局并发生图上限（设计第 5 节：image_concurrency 防显存/速率崩溃）。"""
        try:
            cap = max(1, int(concurrency))
        except (TypeError, ValueError):
            cap = 2
        sem = getattr(self, "_image_semaphore", None)
        current = getattr(self, "_image_semaphore_cap", 0)
        if not isinstance(sem, asyncio.Semaphore) or current != cap:
            sem = asyncio.Semaphore(cap)
            try:
                self._image_semaphore = sem
                self._image_semaphore_cap = cap
            except Exception:
                pass
        return sem

    async def _run_image_job(self, sender_id: str, sender_name: str, umo: str,
                            day: str, pending_ref: str, alt: str,
                            orientation: str, prompt: str, cfg: dict = None,
                            sem=None) -> None:
        """单张图的后台任务：生成 → 入 zip → 替换占位 → 推送；失败静默清占位。

        cfg 是调度时的配置快照（第 9 个位置参数，契约固定）；sem 是共享的
        并发生图信号量（调度层传入，缺省时按当前配置自建）。
        """
        entry_name = self._md_entry(day)
        cfg = cfg if isinstance(cfg, dict) else self._image_cfg()
        lock = sem or self._get_image_semaphore(
            cfg.get("image_concurrency") or 2
        )
        try:
            async with lock:
                data = await self.generate_image(
                    prompt,
                    self._image_size(orientation),
                    ref_path=str(cfg.get("user_ref_photo") or ""),
                )
            if not data:
                # 配置页承诺"生成失败不计数"：失败退还今日额度（设计 D2/D3）
                self._image_usage_refund()
                self._remove_pending(sender_id, entry_name, [pending_ref])
                return
            seq = self._next_image_seq(sender_id, day)
            name = self.save_image(sender_id, data, day, seq, alt)
            replacement = f"![{self._sanitize_alt(alt)}]({name})"
            ok = self.replace_pending_images(
                sender_id, entry_name, {pending_ref: replacement}, []
            )
            if not ok:
                self._remove_pending(sender_id, entry_name, [pending_ref])
                return
            logger.info(
                f"[memory_diary] 用户 {sender_id} 的插图已完成并落盘：{name}"
            )
            await self._notify_image_arrived(umo, alt, name, data, sender_name)
        except asyncio.CancelledError:
            # 插件卸载/终止：不再等网络，清掉占位后退出（额度未产出图，退还）
            self._image_usage_refund()
            self._remove_pending(sender_id, entry_name, [pending_ref])
            raise
        except Exception as e:
            logger.warning(f"[memory_diary] 插图任务异常（已清理占位）: {e}")
            self._image_usage_refund()
            self._remove_pending(sender_id, entry_name, [pending_ref])

    def _next_image_seq(self, sender_id: str, day: str) -> int:
        """当天该用户第几张图（文件名里的当日序号，用于稳定排序/去重）。"""
        try:
            names = self.list_images(sender_id)
        except Exception:
            names = []
        seq = 0
        for name in names:
            base = name.split("/")[-1]
            if base.startswith(f"{day}_"):
                try:
                    seq = max(seq, int(base[len(day) + 1:].split("_")[0]))
                except (TypeError, ValueError):
                    seq += 1
        return seq + 1

    # ------------------------- 推送 -------------------------

    async def _notify_image_arrived(self, umo: str, alt: str, name: str,
                                    data: bytes, sender_name: str = "") -> None:
        """图到了 → 私聊/群聊推送"插图到了" + 图片（失败静默）。"""
        text = f"📖 插图到了：{alt}"
        send = getattr(self.context, "send_message", None)
        if send is None:
            return
        tmp_path = None
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Image as _Image
            from astrbot.api.message_components import Plain as _Plain
        except Exception:
            try:
                chain = MessageChain().message(text)
                await send(umo, chain)
            except Exception as e:
                logger.debug(f"[memory_diary] 插图通知发送失败（静默）: {e}")
            return
        try:
            ext = self.sniff_image_ext(data)
            tmp_dir = self.data_dir / "temp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = tmp_dir / f"img_push_{secrets.token_hex(6)}.{ext}"
            tmp_path.write_bytes(data)
            chain = MessageChain(
                chain=[
                    _Plain(text=text),
                    _Image.fromFileSystem(str(tmp_path)),
                ]
            )
            ok = await send(umo, chain)
            if not ok:
                logger.debug("[memory_diary] 插图通知未发送成功（静默）")
        except Exception as e:
            logger.debug(f"[memory_diary] 插图通知发送异常（静默）: {e}")
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

    # ------------------------- QQ / WebUI 呈现辅助 -------------------------

    def _split_images_for_send(self, sender_id: str, content: str) -> list:
        """按图片引用把正文切段，供 QQ 端图文穿插发送（设计 7.1）。

        返回元素：
          {"type": "text", "text": str}
          {"type": "image", "name": "images/xxx.png", "alt": str}
          {"type": "placeholder", "text": "[插图：alt]"}     缺图优雅降级
          {"type": "warning", "text": "⚠ ..."}               验签失败按篡改告警
        """
        content = str(content or "")
        parts: list = []
        pos = 0
        for m in self._IMAGE_MD_RE.finditer(content):
            before = content[pos:m.start()]
            if before:
                parts.append({"type": "text", "text": before})
            alt = (m.group("alt") or "").strip()
            name = m.group("name")
            is_photo = alt.startswith(self._USER_PHOTO_ALT_PREFIX)
            if self._PENDING_NAME_RE.match(name):
                parts.append(
                    {
                        "type": "placeholder",
                        "text": f"（{alt}——还在画）",
                    }
                )
            else:
                data, status = self.load_image(sender_id, name)
                if status == "ok" and data:
                    parts.append({"type": "image", "name": name, "alt": alt})
                elif status == "tampered":
                    parts.append(
                        {
                            "type": "warning",
                            "text": (
                                f"⚠ 这一天的日记里有一{'张照片' if is_photo else '幅插图'}"
                                f"（{alt}）验签失败，疑似被换过，已拒绝显示。"
                            ),
                        }
                    )
                elif is_photo:
                    parts.append(
                        {"type": "placeholder", "text": f"[用户分享的照片：{alt.split('：', 1)[-1]}]"}
                    )
                else:
                    parts.append({"type": "placeholder", "text": f"[插图：{alt}]"})
            pos = m.end()
        tail = content[pos:]
        if tail:
            parts.append({"type": "text", "text": tail})
        if not parts:
            parts.append({"type": "text", "text": content})
        return parts

    # ------------------------- 形象档案（D6） -------------------------

    def _persona_image_desc(self) -> str:
        """形象唯一事实源（配置项 persona_image_desc）。"""
        return str((self.config or {}).get("persona_image_desc", "") or "").strip()

    def _set_config_value(self, key: str, value) -> bool:
        """写配置项并落盘（AstrBotConfig 有 save_config；纯 dict 只改内存）。"""
        try:
            self.config[key] = value
        except Exception as e:
            logger.warning(f"[memory_diary] 写配置项 {key} 失败: {e}")
            return False
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as e:
                logger.warning(f"[memory_diary] 保存配置项 {key} 失败: {e}")
                return False
        return True

    def _pending_persona_key(self) -> str:
        return "pending_persona_image_desc"

    def _load_pending_persona_desc(self, sender_id: str) -> dict:
        """读取"待确认的形象提案"；没有返回 {}。"""
        try:
            rec = self._load_pref_data(sender_id).get(self._pending_persona_key())
        except Exception:
            return {}
        if not isinstance(rec, dict):
            return {}
        desc = str(rec.get("desc") or "").strip()
        if not desc:
            return {}
        return {
            "desc": desc,
            "created_at": str(rec.get("created_at") or ""),
            "kind": str(rec.get("kind") or "init"),
        }

    def _save_pending_persona_desc(self, sender_id: str, desc: str,
                                   kind: str = "init") -> bool:
        """存一版待确认提案（不直接改配置——签字前不生图/不生效）。"""
        desc = re.sub(r"\s+", " ", str(desc or "")).strip()[:200]
        if not desc:
            return False
        try:
            data = self._load_pref_data(sender_id)
            data[self._pending_persona_key()] = {
                "desc": desc,
                "created_at": self._now().strftime("%Y-%m-%d %H:%M"),
                "kind": kind if kind in ("init", "evolve") else "init",
            }
            return bool(self._save_pref_data(sender_id, data))
        except Exception as e:
            logger.warning(f"[memory_diary] 保存形象提案失败: {e}")
            return False

    def _clear_pending_persona_desc(self, sender_id: str) -> bool:
        try:
            data = self._load_pref_data(sender_id)
            if self._pending_persona_key() not in data:
                return True
            data.pop(self._pending_persona_key(), None)
            return bool(self._save_pref_data(sender_id, data))
        except Exception as e:
            logger.warning(f"[memory_diary] 清除形象提案失败: {e}")
            return False

    def _apply_pending_persona_desc(self, sender_id: str):
        """用户签字：把待确认提案写进配置字段。返回 (ok, desc 或错误文案)。"""
        pending = self._load_pending_persona_desc(sender_id)
        if not pending:
            return False, "没有待确认的形象提案"
        ok = self._set_config_value("persona_image_desc", pending["desc"])
        if not ok:
            return False, "写入配置失败，请管理员检查配置权限"
        self._clear_pending_persona_desc(sender_id)
        logger.info(f"[memory_diary] 用户 {sender_id} 已确认形象档案（签字生效）")
        return True, pending["desc"]

    async def _propose_persona_image_desc(self, umo: str, hint: str = "") -> str:
        """从当前人设提炼一版"画图专用"形象描述（初始化仪式 / 重拟共用）。"""
        try:
            provider = await self.context.get_using_provider_async(umo=umo)
        except Exception:
            provider = None
        if provider is None:
            return ""
        persona = ""
        try:
            if bool((self.config or {}).get("use_persona", True)):
                persona = await self._get_persona_prompt(umo)
        except Exception:
            persona = ""
        persona = persona or str(hint or "").strip()
        if not persona:
            return ""
        prompt = PERSONA_IMAGE_DESC_PROMPT.format(persona=persona[:2000])
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=(
                    "你是一个只输出一行中文形象描述的工具，"
                    "不要解释、不要列表、不要引号。"
                ),
            )
            text = str(getattr(resp, "completion_text", "") or "")
        except Exception as e:
            logger.warning(f"[memory_diary] 提炼形象描述失败: {e}")
            return ""
        text = re.sub(r"\s+", " ", strip_invisible(text)).strip().strip("\"'“”「」")
        return text[:160]
