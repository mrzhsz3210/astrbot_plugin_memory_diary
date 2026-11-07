# -*- coding: utf-8 -*-
"""待吸收外部素材：签名队列、保留期与严格处理决定。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import zipfile
from datetime import datetime, timedelta

from .errors import DiaryTamperedError
from .log import logger
from .scope import parse_scope
from .token_estimator import estimate_text


class ExternalMaterialMixin:
    MATERIALS_ENTRY = "external_materials.json"
    MATERIALS_SIG_ENTRY = "sigs/external_materials.json.sig"
    MATERIAL_PENDING_MAX = 100
    MATERIAL_BATCH_MAX = 8
    MATERIAL_BATCH_TOKENS = 2000
    MATERIAL_SINGLE_MAX_TOKENS = 2600
    MATERIAL_STATUSES = ("pending", "absorbed", "deferred", "discarded", "expired")

    @staticmethod
    def _materials_empty() -> dict:
        return {"version": 1, "items": []}

    @classmethod
    def _normalize_materials(cls, data) -> dict:
        out = cls._materials_empty()
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return out
        seen = set()
        for rec in data["items"]:
            if not isinstance(rec, dict):
                continue
            source = str(rec.get("source") or "")[:32]
            ext_id = str(rec.get("ext_id") or "")[:64]
            material_id = str(rec.get("id") or "") or hashlib.sha256(
                (source + "\0" + ext_id).encode("utf-8")
            ).hexdigest()
            if not source or not ext_id or material_id in seen:
                continue
            status = str(rec.get("status") or "pending")
            if status not in cls.MATERIAL_STATUSES:
                status = "pending"
            item = {
                "id": material_id,
                "source": source,
                "ext_id": ext_id,
                "scope": parse_scope(rec.get("scope") or "private").value,
                "text": str(rec.get("text") or "") if status != "expired" else "",
                "submitted_at": str(rec.get("submitted_at") or "")[:30],
                "status": status,
                "status_at": str(rec.get("status_at") or "")[:30],
                "reason": str(rec.get("reason") or "")[:200],
                "absorbed_entry": str(rec.get("absorbed_entry") or "")[:40] or None,
            }
            out["items"].append(item)
            seen.add(material_id)
        return out

    def _materials_sig(self, sender_id: str, raw: bytes) -> str:
        key = self._user_sign_key(sender_id)
        if not self._is_allowed(sender_id) or key is None:
            return ""
        return hmac.new(
            key,
            sender_id.encode() + b"\n" + self.MATERIALS_ENTRY.encode() + b"\n" + raw,
            hashlib.sha256,
        ).hexdigest()

    def load_external_materials(self, sender_id: str):
        empty = self._materials_empty()
        if not self._is_allowed(sender_id):
            return empty, "denied"
        path = self._diary_path(sender_id)
        if not path.exists():
            return empty, "missing"
        try:
            with self._diary_zip(path) as z:
                names = set(z.namelist())
                if self.MATERIALS_ENTRY not in names:
                    return empty, "missing"
                raw = z.read(self.MATERIALS_ENTRY)
                actual = z.read(self.MATERIALS_SIG_ENTRY).decode().strip() if self.MATERIALS_SIG_ENTRY in names else ""
        except Exception as exc:
            logger.warning(f"[memory_diary] 读取外部素材队列失败: {exc}")
            return empty, "tampered"
        expected = self._materials_sig(sender_id, raw)
        if not expected or not hmac.compare_digest(expected, actual):
            return empty, "tampered"
        try:
            return self._normalize_materials(json.loads(raw.decode("utf-8"))), "ok"
        except Exception:
            return empty, "tampered"

    def _write_external_materials(self, sender_id: str, data: dict) -> bool:
        if not self._is_allowed(sender_id):
            return False
        path = self._diary_path(sender_id)
        try:
            entries = {}
            if path.exists():
                with self._diary_zip(path) as z:
                    entries = {name: z.read(name) for name in z.namelist()}
                raw = entries.get(self.MATERIALS_ENTRY)
                if raw is not None:
                    actual = entries.get(self.MATERIALS_SIG_ENTRY, b"").decode("utf-8", "ignore").strip()
                    if not hmac.compare_digest(self._materials_sig(sender_id, raw), actual):
                        raise DiaryTamperedError("外部素材队列")
            normalized = self._normalize_materials(data)
            raw = json.dumps(normalized, ensure_ascii=False, indent=2).encode("utf-8")
            entries[self.MATERIALS_ENTRY] = raw
            entries[self.MATERIALS_SIG_ENTRY] = self._materials_sig(sender_id, raw).encode()
            self._write_zip_atomic(path, entries)
            return True
        except Exception as exc:
            logger.warning(f"[memory_diary] 外部素材队列写入失败，保留上一版本: {exc}")
            return False

    def enqueue_external_material(
        self, sender_id: str, source: str, ext_id: str, text: str, scope: str = "private"
    ):
        data, status = self.load_external_materials(sender_id)
        if status not in ("ok", "missing"):
            return None, status
        material_id = hashlib.sha256((source + "\0" + ext_id).encode()).hexdigest()
        for item in data["items"]:
            if item["id"] == material_id:
                return dict(item), "replayed"
        tombstone, tomb_status = self.has_tombstone(sender_id, source, ext_id)
        if tomb_status == "tampered":
            return None, "tampered"
        if tombstone is not None:
            return {
                "id": material_id,
                "status": "discarded",
                "reason": "所属日记已删除",
                "absorbed_entry": None,
            }, "tombstone"
        if estimate_text(text) + 30 > self.MATERIAL_SINGLE_MAX_TOKENS:
            return None, "too_large"
        active = [item for item in data["items"] if item["status"] in ("pending", "deferred")]
        if len(active) >= self.MATERIAL_PENDING_MAX:
            return None, "full"
        now = self._now().strftime("%Y-%m-%dT%H:%M:%S")
        item = {
            "id": material_id,
            "source": source,
            "ext_id": ext_id,
            "scope": parse_scope(scope).value,
            "text": text,
            "submitted_at": now,
            "status": "pending",
            "status_at": now,
            "reason": "",
            "absorbed_entry": None,
        }
        data["items"].append(item)
        return (dict(item), "queued") if self._write_external_materials(sender_id, data) else (None, "write_failed")

    def expire_external_materials(self, sender_id: str) -> int:
        try:
            days = int(self.config.get("external_material_retention_days", 30))
        except (TypeError, ValueError):
            days = 30
        if days < 0:
            days = 30
        if days == 0:
            return 0
        data, status = self.load_external_materials(sender_id)
        if status not in ("ok", "missing"):
            return 0
        cutoff = self._now() - timedelta(days=days)
        changed = 0
        now = self._now().strftime("%Y-%m-%dT%H:%M:%S")
        for item in data["items"]:
            if item["status"] not in ("pending", "deferred"):
                continue
            try:
                submitted = datetime.fromisoformat(item["submitted_at"])
                if submitted.tzinfo is not None:
                    submitted = submitted.replace(tzinfo=None)
                now_naive = self._now().replace(tzinfo=None)
                expired = submitted <= now_naive - timedelta(days=days)
            except (TypeError, ValueError):
                expired = False
            if expired:
                item.update(status="expired", status_at=now, reason="超过保留期", text="")
                changed += 1
        if changed and not self._write_external_materials(sender_id, data):
            return 0
        return changed

    def external_material_batch(self, sender_id: str, scope: str) -> list:
        self.expire_external_materials(sender_id)
        data, status = self.load_external_materials(sender_id)
        if status != "ok":
            return []
        wanted = parse_scope(scope).value
        batch = []
        used = 0
        for item in data["items"]:
            if item["scope"] != wanted or item["status"] not in ("pending", "deferred"):
                continue
            cost = estimate_text(item["text"]) + 30
            if cost > self.MATERIAL_SINGLE_MAX_TOKENS:
                logger.warning(
                    f"[memory_diary] 外部素材 {item['id']} 单条估算 {cost} token，"
                    f"超过可处理上限 {self.MATERIAL_SINGLE_MAX_TOKENS}，请管理员拆分或明确丢弃"
                )
                continue
            if not batch and cost > self.MATERIAL_BATCH_TOKENS:
                batch.append(item)
                break
            if batch and (
                len(batch) >= self.MATERIAL_BATCH_MAX
                or used + cost > self.MATERIAL_BATCH_TOKENS
            ):
                break
            batch.append(item)
            used += cost
        return batch

    @staticmethod
    def parse_material_decisions(text: str):
        raw = str(text or "")
        match = re.search(r"<material_decisions>\s*(.*?)\s*</material_decisions\s*>", raw, re.I | re.S)
        if not match:
            return raw, {}
        clean = (raw[:match.start()] + raw[match.end():]).strip()
        try:
            payload = json.loads(match.group(1))
            records = payload.get("decisions") if isinstance(payload, dict) else payload
            if not isinstance(records, list):
                return clean, {}
            decisions = {}
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                material_id = str(rec.get("material_id") or "")
                action = str(rec.get("action") or "")
                if material_id and action in ("absorb", "defer", "discard"):
                    decisions[material_id] = {"action": action, "reason": str(rec.get("reason") or "")[:200]}
            return clean, decisions
        except Exception:
            return clean, {}

    @staticmethod
    def apply_material_decisions(data: dict, batch: list, decisions: dict, entry: str, now: str) -> dict:
        by_id = {item["id"]: item for item in data.get("items", [])}
        for queued in batch:
            decision = decisions.get(queued["id"])
            if decision is None:
                continue
            item = by_id.get(queued["id"])
            if item is None or item["status"] not in ("pending", "deferred"):
                continue
            action = decision["action"]
            item["status"] = {"absorb": "absorbed", "defer": "deferred", "discard": "discarded"}[action]
            item["status_at"] = now
            item["reason"] = decision.get("reason", "")
            item["absorbed_entry"] = entry if action == "absorb" else None
        return data

    def set_external_material_status(self, sender_id: str, material_id: str, action: str) -> bool:
        if action not in ("retry", "discard"):
            return False
        data, status = self.load_external_materials(sender_id)
        if status != "ok":
            return False
        for item in data["items"]:
            if item["id"] == material_id and item["status"] in ("deferred", "pending", "discarded"):
                if action == "retry" and item["status"] == "discarded":
                    return False
                item["status"] = "pending" if action == "retry" else "discarded"
                item["status_at"] = self._now().strftime("%Y-%m-%dT%H:%M:%S")
                item["reason"] = "管理员重试" if action == "retry" else "管理员明确丢弃"
                return self._write_external_materials(sender_id, data)
        return False
