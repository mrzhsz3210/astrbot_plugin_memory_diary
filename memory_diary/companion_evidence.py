# -*- coding: utf-8 -*-
"""陪伴插件记录的角色级 HMAC 存证副本；不参与召回或写作。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from .log import logger


class CompanionEvidenceMixin:
    COMPANION_EVIDENCE_RETENTION_DAYS = 90
    _COMPANION_EVIDENCE_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    @staticmethod
    def _companion_context_field(value, key, default=None):
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    def _companion_role_key(self, context):
        scope = self._companion_context_field(context, "scope")
        values = [
            self._companion_context_field(scope, "installation_id"),
            self._companion_context_field(scope, "bot_id"),
            self._companion_context_field(context, "role"),
            self._companion_context_field(context, "role_binding_revision", ""),
        ]
        normalized = [str(value or "").strip() for value in values]
        if not all(normalized[:3]):
            return None
        return hashlib.sha256("\0".join(normalized).encode("utf-8")).hexdigest()[:32]

    def _companion_evidence_root(self) -> Path:
        return self.data_dir / "companion_evidence"

    def _companion_evidence_sig(self, role_key: str, filename: str, raw: bytes) -> str:
        key = self._secret_key()
        if key is None:
            return ""
        return hmac.new(
            key,
            b"companion_evidence\n"
            + role_key.encode("ascii")
            + b"\n"
            + filename.encode("ascii")
            + b"\n"
            + raw,
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _companion_evidence_sig_path(path: Path) -> Path:
        return path.with_name(path.name + ".sig")

    def _load_companion_evidence(self, path: Path, role_key: str):
        sig_path = self._companion_evidence_sig_path(path)
        try:
            raw = path.read_bytes()
            actual = sig_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.warning(
                f"[memory_diary] 陪伴存证读取失败 {path.name}: {type(exc).__name__}"
            )
            return None
        expected = self._companion_evidence_sig(role_key, path.name, raw)
        if not expected or not hmac.compare_digest(expected, actual):
            logger.warning(f"[memory_diary] 陪伴存证验签失败，已拒绝读取 {path}")
            return None
        records = []
        try:
            for line in raw.decode("utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("存证行不是对象")
                records.append(item)
            return records
        except Exception as exc:
            logger.warning(
                f"[memory_diary] 陪伴存证格式无效，已拒绝读取 {path.name}: "
                f"{type(exc).__name__}"
            )
            return None

    def _companion_quarantine_invalid_evidence(self, path: Path, role_key: str) -> bool:
        sig_path = self._companion_evidence_sig_path(path)
        try:
            raw = path.read_bytes()
            expected = self._companion_evidence_sig(role_key, path.name, raw)
            if not expected:
                return False
            try:
                actual = sig_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                actual = ""
            if actual and hmac.compare_digest(expected, actual):
                return False
            quarantine = path.with_name(
                f"{path.name}.corrupt-{self._now().strftime('%Y%m%dT%H%M%S')}-"
                f"{secrets.token_hex(3)}"
            )
            if sig_path.exists():
                sig_path.replace(quarantine.with_name(quarantine.name + ".sig"))
            path.replace(quarantine)
            logger.warning(f"[memory_diary] 陪伴存证验签冲突，已隔离保留 {quarantine.name}")
            return True
        except Exception as exc:
            logger.warning(
                f"[memory_diary] 陪伴存证隔离失败 {path.name}: {type(exc).__name__}"
            )
            return False

    @staticmethod
    def _companion_atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
        try:
            with tmp.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if tmp.read_bytes() != payload:
                raise OSError("陪伴存证写入后字节自检失败")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _companion_occurred_day(value) -> str:
        text = str(value or "").strip()
        if len(text) < 10:
            return ""
        day = text[:10]
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return ""
        return day

    def _companion_store_evidence(self, record):
        memory_id = str(record.get("memory_id") or "").strip()
        source_plugin = str(record.get("source_plugin") or "").strip()
        role = str(self._companion_context_field(record.get("context"), "role", "") or "")
        try:
            if not memory_id or not source_plugin:
                return None
            if record.get("kind") not in {"persona_life", "proactive_message"}:
                return None
            role_key = self._companion_role_key(record.get("context"))
            if role_key is None:
                return None
            day = self._companion_occurred_day(record.get("occurred_at"))
            if not day:
                return None
            content = record.get("content")
            if not isinstance(content, str):
                return None
            path = self._companion_evidence_root() / role_key / f"{day}.jsonl"
            sig_path = self._companion_evidence_sig_path(path)
            if path.exists():
                records = self._load_companion_evidence(path, role_key)
                if records is None:
                    if not self._companion_quarantine_invalid_evidence(path, role_key):
                        return None
                    records = []
            else:
                if sig_path.exists():
                    logger.warning(f"[memory_diary] 陪伴存证正文缺失但签名残留，已拒绝写入 {path}")
                    return None
                records = []
            item = {
                "memory_id": memory_id,
                "source_plugin": source_plugin,
                "kind": record.get("kind"),
                "content": content,
                "occurred_at": str(record.get("occurred_at") or ""),
                "role": role,
                "scope": record.get("scope"),
                "session_id": record.get("session_id"),
                "platform": record.get("platform"),
                "tags": record.get("tags"),
                "metadata": record.get("metadata"),
                "confidence": record.get("confidence"),
                "importance": record.get("importance"),
                "received_at": self._now().isoformat(),
            }
            replaced = False
            for index, old in enumerate(records):
                if (
                    str(old.get("source_plugin") or "") == source_plugin
                    and str(old.get("memory_id") or "") == memory_id
                ):
                    records[index] = item
                    replaced = True
                    break
            if not replaced:
                records.append(item)
            raw = "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in records
            ).encode("utf-8")
            signature = self._companion_evidence_sig(role_key, path.name, raw)
            if not signature:
                return None
            self._companion_atomic_write(path, raw)
            self._companion_atomic_write(sig_path, signature.encode("ascii"))
            return memory_id
        except Exception as exc:
            logger.warning(
                "[memory_diary] 陪伴记录存证写入失败："
                f"kind={record.get('kind') or ''}，role={role}，"
                f"memory_id={memory_id}，{type(exc).__name__}"
            )
            return None

    def _companion_evidence_expire(self) -> int:
        root = self._companion_evidence_root()
        if not root.is_dir():
            return 0
        cutoff = self._now().date() - timedelta(days=self.COMPANION_EVIDENCE_RETENTION_DAYS)
        removed = 0
        for path in root.glob("*/*.jsonl"):
            day = path.name[:-6]
            if not self._COMPANION_EVIDENCE_DAY.fullmatch(day):
                continue
            try:
                if datetime.strptime(day, "%Y-%m-%d").date() >= cutoff:
                    continue
                path.unlink(missing_ok=True)
                self._companion_evidence_sig_path(path).unlink(missing_ok=True)
                removed += 1
            except Exception as exc:
                logger.warning(
                    f"[memory_diary] 清理过期陪伴存证失败 {path.name}: "
                    f"{type(exc).__name__}"
                )
        for directory in root.iterdir():
            if directory.is_dir():
                try:
                    directory.rmdir()
                except OSError:
                    pass
        if removed:
            logger.info(f"[memory_diary] 启动清理：已删除 {removed} 份过期陪伴存证")
        return removed

    def _companion_evidence_stats(self) -> tuple[int, int]:
        root = self._companion_evidence_root()
        if not root.is_dir():
            return 0, 0
        cutoff = self._now().date() - timedelta(days=self.COMPANION_EVIDENCE_RETENTION_DAYS)
        total = 0
        roles = 0
        for directory in root.iterdir():
            if not directory.is_dir():
                continue
            role_count = 0
            for path in directory.glob("*.jsonl"):
                day = path.name[:-6]
                if not self._COMPANION_EVIDENCE_DAY.fullmatch(day):
                    continue
                try:
                    if datetime.strptime(day, "%Y-%m-%d").date() < cutoff:
                        continue
                except ValueError:
                    continue
                records = self._load_companion_evidence(path, directory.name)
                if records:
                    role_count += len(records)
            if role_count:
                roles += 1
                total += role_count
        return total, roles
