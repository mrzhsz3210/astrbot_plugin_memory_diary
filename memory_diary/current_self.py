# -*- coding: utf-8 -*-
"""「当前的我」交接信：签名存储、滚段/整篇重写与轻量注入。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import zipfile

from .log import logger
from .token_estimator import estimate_text
from .writing import looks_like_reasoning_leak, strip_reasoning_blocks


class CurrentSelfMixin:
    CURRENT_SELF_ENTRY = "current_self.json"
    CURRENT_SELF_SIG_ENTRY = "sigs/current_self.json.sig"
    CURRENT_SELF_ARCHIVE_ENTRY = "current_self_anchor_archive.json"
    CURRENT_SELF_ARCHIVE_SIG_ENTRY = "sigs/current_self_anchor_archive.json.sig"
    CURRENT_SELF_HISTORY_MAX = 5
    CURRENT_SELF_ACTIVE_ANCHORS_MAX = 200

    @staticmethod
    def _current_self_empty() -> dict:
        return {
            "version": 1,
            "current": None,
            "history": [],
            "since_rewrite": 0,
        }

    @classmethod
    def _normalize_current_self(cls, data) -> dict:
        out = cls._current_self_empty()
        if not isinstance(data, dict):
            return out
        try:
            out["since_rewrite"] = max(0, int(data.get("since_rewrite") or 0))
        except (TypeError, ValueError):
            pass

        def _version(rec):
            if not isinstance(rec, dict):
                return None
            text = str(rec.get("text") or "").strip()
            if not text:
                return None
            try:
                revision = max(1, int(rec.get("revision") or 1))
            except (TypeError, ValueError):
                revision = 1
            return {
                "revision": revision,
                "text": text[:900],
                "updated_at": str(rec.get("updated_at") or "")[:30],
            }

        out["current"] = _version(data.get("current"))
        history = data.get("history")
        if isinstance(history, list):
            out["history"] = [
                version for rec in history[-cls.CURRENT_SELF_HISTORY_MAX:]
                if (version := _version(rec)) is not None
            ]

        current = data.get("current")
        raw_anchors = current.get("anchors") if isinstance(current, dict) else None
        anchors = []
        seen = set()
        if isinstance(raw_anchors, list):
            for rec in raw_anchors:
                if not isinstance(rec, dict):
                    continue
                text = str(rec.get("text") or "").strip()[:500]
                source = rec.get("source")
                if not text or not isinstance(source, dict):
                    continue
                entry = str(source.get("entry") or "").strip()
                if not re.fullmatch(r"diaries/\d{8}\.md", entry):
                    continue
                anchor_id = str(rec.get("id") or "").strip() or hashlib.sha256(
                    text.encode("utf-8")
                ).hexdigest()[:16]
                if anchor_id in seen:
                    continue
                normalized = {
                    "id": anchor_id,
                    "text": text,
                    "source": {
                        "model": str(source.get("model") or "unknown")[:120],
                        "persona": str(source.get("persona") or "")[:64],
                        "entry": entry,
                    },
                }
                superseded = rec.get("superseded_by")
                if isinstance(superseded, dict):
                    normalized["superseded_by"] = {
                        "by": str(superseded.get("by") or "")[:80],
                        "reason": str(superseded.get("reason") or "")[:200],
                        "at": str(superseded.get("at") or "")[:30],
                    }
                anchors.append(normalized)
                seen.add(anchor_id)
        if out["current"] is not None:
            out["current"]["anchors"] = anchors
            pending_items = []
            raw_pending = current.get("pending_items")
            if isinstance(raw_pending, list):
                seen_pending = set()
                for rec in raw_pending:
                    if not isinstance(rec, dict):
                        continue
                    text = str(rec.get("text") or "").strip()[:300]
                    source = str(rec.get("source") or "").strip()
                    if not text or text in seen_pending:
                        continue
                    pending_items.append(
                        {
                            "text": text,
                            "source": source,
                            "confirmed_at": str(rec.get("confirmed_at") or "")[:30],
                        }
                    )
                    seen_pending.add(text)
            out["current"]["pending_items"] = pending_items
        return out

    def _current_self_sig(self, sender_id: str, raw: bytes) -> str:
        if not self._is_allowed(sender_id):
            return ""
        key = self._user_sign_key(sender_id)
        if key is None:
            return ""
        return hmac.new(
            key,
            sender_id.encode("utf-8")
            + b"\n"
            + self.CURRENT_SELF_ENTRY.encode("utf-8")
            + b"\n"
            + raw,
            hashlib.sha256,
        ).hexdigest()

    def load_current_self(self, sender_id: str):
        path = self._diary_path(sender_id)
        if not path.exists():
            return self._current_self_empty(), "missing"
        try:
            with self._diary_zip(path) as z:
                names = set(z.namelist())
                if self.CURRENT_SELF_ENTRY not in names:
                    return self._current_self_empty(), "missing"
                raw = z.read(self.CURRENT_SELF_ENTRY)
                actual = (
                    z.read(self.CURRENT_SELF_SIG_ENTRY).decode("utf-8", "ignore").strip()
                    if self.CURRENT_SELF_SIG_ENTRY in names
                    else ""
                )
        except Exception as e:
            logger.warning(f"[memory_diary] 读取交接信失败: {e}")
            return self._current_self_empty(), "tampered"
        expected = self._current_self_sig(sender_id, raw)
        if not expected or not hmac.compare_digest(expected, actual):
            logger.warning(f"[memory_diary] 用户 {sender_id} 的交接信验签失败")
            return self._current_self_empty(), "tampered"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            logger.warning(f"[memory_diary] 用户 {sender_id} 的交接信 JSON 损坏")
            return self._current_self_empty(), "tampered"
        return self._normalize_current_self(data), "ok"

    def _current_self_archive_sig(self, sender_id: str, raw: bytes) -> str:
        key = self._user_sign_key(sender_id)
        if not self._is_allowed(sender_id) or key is None:
            return ""
        return hmac.new(
            key,
            sender_id.encode() + b"\n" + self.CURRENT_SELF_ARCHIVE_ENTRY.encode() + b"\n" + raw,
            hashlib.sha256,
        ).hexdigest()

    @classmethod
    def _normalize_anchor_archive(cls, data) -> dict:
        items = data.get("items") if isinstance(data, dict) else []
        normalized = []
        for rec in items if isinstance(items, list) else []:
            if not isinstance(rec, dict):
                continue
            anchor = cls._normalize_current_self({"current": {
                "text": "归档", "anchors": [rec]
            }})["current"]["anchors"]
            if not anchor:
                continue
            normalized.append({
                **anchor[0],
                "archived_at": str(rec.get("archived_at") or "")[:30],
                "batch": str(rec.get("batch") or "")[:80],
            })
        return {"version": 1, "items": normalized}

    def load_current_self_anchor_archive(self, sender_id: str):
        empty = {"version": 1, "items": []}
        if not self._is_allowed(sender_id):
            return empty, "denied"
        path = self._diary_path(sender_id)
        if not path.exists():
            return empty, "missing"
        try:
            with self._diary_zip(path) as z:
                names = set(z.namelist())
                if self.CURRENT_SELF_ARCHIVE_ENTRY not in names:
                    return empty, "missing"
                raw = z.read(self.CURRENT_SELF_ARCHIVE_ENTRY)
                actual = z.read(self.CURRENT_SELF_ARCHIVE_SIG_ENTRY).decode().strip() if self.CURRENT_SELF_ARCHIVE_SIG_ENTRY in names else ""
        except Exception:
            return empty, "tampered"
        expected = self._current_self_archive_sig(sender_id, raw)
        if not expected or not hmac.compare_digest(expected, actual):
            return empty, "tampered"
        try:
            return self._normalize_anchor_archive(json.loads(raw.decode())), "ok"
        except Exception:
            return empty, "tampered"

    def save_current_self(
        self, sender_id: str, data: dict, *, record_history: bool = True
    ) -> bool:
        path = self._diary_path(sender_id)
        try:
            entries = {}
            if path.exists():
                with self._diary_zip(path) as z:
                    entries = {name: z.read(name) for name in z.namelist()}
            old_raw = entries.get(self.CURRENT_SELF_ENTRY)
            old_actual = entries.get(self.CURRENT_SELF_SIG_ENTRY, b"").decode(
                "utf-8", "ignore"
            ).strip()
            if old_raw is not None:
                expected = self._current_self_sig(sender_id, old_raw)
                if not expected or not hmac.compare_digest(expected, old_actual):
                    logger.warning("[memory_diary] 旧交接信验签失败，拒绝覆盖")
                    return False
                json.loads(old_raw.decode("utf-8"))
            normalized = self._normalize_current_self(data)
            if normalized["current"] is None:
                return False
            if old_raw is not None and record_history:
                old, status = self.load_current_self(sender_id)
                if status != "ok":
                    return False
                if old["current"] is not None:
                    old_version = {
                        key: old["current"][key]
                        for key in ("revision", "text", "updated_at")
                    }
                    normalized["history"] = (old["history"] + [old_version])[
                        -self.CURRENT_SELF_HISTORY_MAX:
                    ]
            anchors = normalized["current"]["anchors"]
            if len(anchors) > self.CURRENT_SELF_ACTIVE_ANCHORS_MAX:
                overflow = anchors[:-self.CURRENT_SELF_ACTIVE_ANCHORS_MAX]
                normalized["current"]["anchors"] = anchors[-self.CURRENT_SELF_ACTIVE_ANCHORS_MAX:]
                archive_raw = entries.get(self.CURRENT_SELF_ARCHIVE_ENTRY)
                if archive_raw is None:
                    archive = {"version": 1, "items": []}
                else:
                    actual = entries.get(self.CURRENT_SELF_ARCHIVE_SIG_ENTRY, b"").decode(
                        "utf-8", "ignore"
                    ).strip()
                    expected = self._current_self_archive_sig(sender_id, archive_raw)
                    if not expected or not hmac.compare_digest(expected, actual):
                        logger.warning("[memory_diary] 锚点归档验签失败，拒绝覆盖")
                        return False
                    archive = self._normalize_anchor_archive(json.loads(archive_raw.decode()))
                archived_at = self._now().strftime("%Y-%m-%dT%H:%M:%S")
                batch = f"archive_{archived_at}_{normalized['current']['revision']}"
                archive["items"].extend({
                    **anchor, "archived_at": archived_at, "batch": batch
                } for anchor in overflow)
                archive_raw = json.dumps(archive, ensure_ascii=False, indent=2).encode()
                entries[self.CURRENT_SELF_ARCHIVE_ENTRY] = archive_raw
                entries[self.CURRENT_SELF_ARCHIVE_SIG_ENTRY] = self._current_self_archive_sig(
                    sender_id, archive_raw
                ).encode()
            raw = json.dumps(
                normalized, ensure_ascii=False, indent=2
            ).encode("utf-8")
            entries[self.CURRENT_SELF_ENTRY] = raw
            entries[self.CURRENT_SELF_SIG_ENTRY] = self._current_self_sig(
                sender_id, raw
            ).encode("utf-8")
            self._write_zip_atomic(path, entries)
            return True
        except Exception as e:
            logger.warning(f"[memory_diary] 交接信写入失败，保留上一有效版本: {e}")
            return False

    def supersede_current_self_anchor(
        self, sender_id: str, anchor_id: str, by: str, reason: str
    ) -> bool:
        data, status = self.load_current_self(sender_id)
        if status != "ok" or data["current"] is None:
            return False
        found = False
        for anchor in data["current"]["anchors"]:
            if anchor["id"] == anchor_id:
                anchor["superseded_by"] = {
                    "by": str(by or "")[:80],
                    "reason": str(reason or "")[:200],
                    "at": self._now().strftime("%Y-%m-%dT%H:%M:%S"),
                }
                found = True
                break
        if not found:
            return False
        return self.save_current_self(sender_id, data, record_history=False)

    def append_current_self_pending(
        self, sender_id: str, text: str, source: str
    ) -> bool:
        """追加用户已确认的新认识；不改正文、锚点或历史版本。"""
        if not bool(self.config.get("current_self_enabled", True)):
            return False
        text = str(text or "").strip()[:300]
        if not text:
            return False
        data, status = self.load_current_self(sender_id)
        if status not in ("ok", "missing"):
            return False
        if data["current"] is None:
            data["current"] = {
                "revision": 1,
                "text": "尚未形成完整交接信。",
                "updated_at": self._now().strftime("%Y-%m-%dT%H:%M:%S"),
                "anchors": [],
                "pending_items": [],
            }
        items = data["current"].setdefault("pending_items", [])
        if any(item.get("text") == text for item in items):
            return True
        items.append(
            {
                "text": text,
                "source": str(source or ""),
                "confirmed_at": self._now().strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
        return self.save_current_self(sender_id, data, record_history=False)

    @staticmethod
    def _fit_current_self_text(text: str, token_cap: int) -> str:
        if estimate_text(text) <= token_cap:
            return text
        best = ""
        low, high = 0, len(text)
        while low <= high:
            size = (low + high) // 2
            candidate = text[:size].rstrip()
            if estimate_text(candidate) <= token_cap:
                best = candidate
                low = size + 1
            else:
                high = size - 1
        return best or "…"

    def _current_self_inject_block(self, sender_id: str, token_cap: int = 1200) -> str:
        if not bool(self.config.get("current_self_enabled", True)):
            return ""
        data, status = self.load_current_self(sender_id)
        if status != "ok" or data["current"] is None:
            return ""
        current = data["current"]
        block = self._fit_current_self_text(
            "【当前的我】\n" + current["text"], token_cap
        )
        if estimate_text(block) > token_cap:
            return ""

        pending = current.get("pending_items") or []

        def _pending_block(items: list) -> str:
            if not items:
                return block
            text = block + "\n\n【用户已确认的新认识】\n" + "\n".join(
                f"- {item['text']}" for item in items
            )
            if len(items) < len(pending):
                text += "\n（仅注入部分近期确认事项，其余仍完整保存在交接信中。）"
            return text

        kept_pending = []
        for item in reversed(pending):
            candidate_items = [item] + kept_pending
            if estimate_text(_pending_block(candidate_items)) > token_cap:
                break
            kept_pending = candidate_items
        block = _pending_block(kept_pending)

        active = [
            anchor for anchor in current.get("anchors", [])
            if not anchor.get("superseded_by")
        ]
        anchor_cap = min(400, max(0, token_cap - estimate_text(block)))
        kept_anchors = []
        for anchor in reversed(active):
            line = f"- {anchor['text']}（出自 {anchor['source']['entry']}）"
            candidate = [line] + kept_anchors
            section = "\n\n【仍有效的锚点】\n" + "\n".join(candidate)
            if (
                estimate_text(section) <= anchor_cap
                and estimate_text(block + section) <= token_cap
            ):
                kept_anchors = candidate
            else:
                break
        if kept_anchors:
            block += "\n\n【仍有效的锚点】\n" + "\n".join(kept_anchors)
        return block

    async def _current_self_source(self, umo: str, entry: str) -> dict:
        provider = await self.context.get_using_provider_async(umo=umo)
        model = str(
            getattr(provider, "model", "")
            or getattr(provider, "model_name", "")
            or "unknown"
        )
        persona = await self._get_persona_prompt(umo)
        persona_version = hashlib.sha256(persona.encode("utf-8")).hexdigest()[:16] if persona else ""
        return {"model": model, "persona": persona_version, "entry": entry}

    def _recent_diaries_for_self(self, sender_id: str) -> str:
        blocks = []
        for name in reversed(self._list_entries(sender_id)):
            content, tampered = self._load_entry(sender_id, name)
            if tampered:
                continue
            block = f"【{self._entry_label(name)} / {name}】\n{content}"
            if estimate_text("\n\n".join(blocks + [block])) > 4000:
                break
            blocks.append(block)
        return "\n\n".join(blocks)

    @staticmethod
    def _parse_current_self_output(raw: str) -> dict | None:
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        try:
            data = json.loads(text)
        except Exception:
            return None
        if not isinstance(data, dict) or not str(data.get("text") or "").strip():
            return None
        anchors = data.get("anchors", [])
        if isinstance(anchors, str):
            anchors = [anchors]
        elif not isinstance(anchors, list):
            return None
        if any(not isinstance(anchor, str) for anchor in anchors):
            return None
        data["anchors"] = anchors
        return data

    @staticmethod
    def _anchors_for_rewrite_prompt(anchors: list, token_cap: int = 1200) -> list:
        kept = []
        for anchor in reversed(anchors):
            candidate = [anchor] + kept
            if estimate_text("\n".join(item["text"] for item in candidate)) > token_cap:
                break
            kept = candidate
        return kept

    async def _update_current_self(
        self, sender_id: str, umo: str, diary: str, entry: str, trigger: bool = False
    ) -> bool:
        if not bool(self.config.get("current_self_enabled", True)):
            return False
        data, status = self.load_current_self(sender_id)
        if status not in ("ok", "missing"):
            return False
        try:
            every = max(1, int(self.config.get("current_self_rewrite_every", 4) or 4))
        except (TypeError, ValueError):
            every = 4
        next_count = int(data.get("since_rewrite") or 0) + 1
        full = trigger or data["current"] is None or next_count >= every
        source = await self._current_self_source(umo, entry)
        provider = await self.context.get_using_provider_async(umo=umo)
        if provider is None:
            return False
        previous = (data["current"] or {}).get("text", "")
        if full:
            prompt = (
                "请整篇重写“当前的我”交接信，不是在旧文后追加。正文 600~900 字，"
                "包含当前关系状态、正在进行的事、近期判断倾向与待确认项。"
                "只输出 JSON：{\"text\":\"交接信正文\",\"anchors\":[\"本次新增的具体原话/时间/事件\"]}。"
                "anchors 只放本次日记中值得长期保留的新锚点，不要复述旧锚点。\n\n"
                f"上一版可替换段：\n{previous}\n\n已有锚点（只读，不要重写）：\n"
                + "\n".join(a["text"] for a in self._anchors_for_rewrite_prompt(
                    (data["current"] or {}).get("anchors", [])
                ))
                + "\n\n近期日记（新到旧）：\n"
                + self._recent_diaries_for_self(sender_id)
            )
        else:
            prompt = (
                "请根据上一版可替换段和本次新日记，重写交接信的可替换段。"
                "不要追加旧文，不要生成或修改锚点。只输出 JSON：{\"text\":\"新的可替换段\"}。\n\n"
                f"上一版：\n{previous}\n\n本次日记（{entry}）：\n{diary}"
            )
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt="你在维护写给未来自己的当前交接信。过去可重新理解，但历史不可篡改。",
                max_tokens=1200 if full else 300,
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 交接信模型调用失败: {e}")
            return False
        # v0.7.6（Bug A）：交接信同样吃思维链泄漏兜底——先剥标签块，
        # 疑似泄漏时按"保留上一版"处理（与 JSON 解析失败同一保护口径）
        raw_output = strip_reasoning_blocks(resp.completion_text or "")
        if looks_like_reasoning_leak(raw_output):
            logger.warning("[memory_diary] 交接信输出疑似混入思维链，保留上一版")
            return False
        parsed = self._parse_current_self_output(raw_output)
        if parsed is None:
            logger.warning("[memory_diary] 交接信输出不是合法 JSON，保留上一版")
            return False
        text = str(parsed["text"] or "").strip()[:900]
        anchors = list((data["current"] or {}).get("anchors", []))
        if full:
            existing_ids = {a["id"] for a in anchors}
            for anchor_text in parsed.get("anchors") or []:
                anchor_text = str(anchor_text or "").strip()[:500]
                if not anchor_text:
                    continue
                anchor_id = hashlib.sha256(anchor_text.encode("utf-8")).hexdigest()[:16]
                if anchor_id in existing_ids:
                    continue
                anchors.append(
                    {"id": anchor_id, "text": anchor_text, "source": dict(source)}
                )
                existing_ids.add(anchor_id)
        previous_current = data["current"] or {}
        data["current"] = {
            "revision": int(previous_current.get("revision") or 0) + 1,
            "text": text,
            "updated_at": self._now().strftime("%Y-%m-%dT%H:%M:%S"),
            "anchors": anchors,
            "pending_items": list(previous_current.get("pending_items") or []),
        }
        data["since_rewrite"] = 0 if full else next_count
        return self.save_current_self(sender_id, data)
