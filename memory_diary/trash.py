# -*- coding: utf-8 -*-
"""删除回收站（v0.7.7）：软删除、限期保留、验签恢复。

/删除日记 不再物理抹掉数据：被删的条目（正文 + 签名 + 插图 + 批注/来源切片）
整体移进数据目录下 ``trash/`` 的回收站 zip，保留 ``trash_retention_days``
天（默认 30，0 = 关闭回收站、退回 v0.7.5 的硬删除），逾期由启动清理与
删除/恢复操作前的例行清理物理删除。

防篡改口径与日记 zip 同级：
  - 回收站 zip 里每篇正文/插图沿用**原始签名**（签名域含存储身份，
    恢复时重新验签，等于"原样放回"，不存在替回收站重签）；
  - manifest.json 记录归属身份、删除时间、条目清单，并带一份用该身份
    派生钥匙算的 HMAC（覆盖 manifest 本体 + 全部 payload 字节），
    被改过的回收站条目不列出、不恢复；
  - 回收站文件本身走 ``_write_zip_atomic``：整包加密开启时同样加密落盘。

恢复 = 把回收站里的条目合并回当前日记 zip：已存在同一天的条目不覆盖
（删除后又写了新日记的场景，新内容优先），批注/来源切片按 id/条目名去重合并。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from .errors import DiaryKeyUnavailable
from .log import logger


class TrashMixin:
    """回收站关注点；由 MemoryDiaryCore 组合（不 import AstrBot）。"""

    TRASH_DIR_NAME = "trash"
    TRASH_MANIFEST = "manifest.json"
    TRASH_PAYLOAD_PREFIX = "payload/"
    TRASH_FILE_RE_PREFIX = "trash_"

    # ------------------------- 基础 -------------------------

    def _trash_dir(self) -> Path:
        return self.data_dir / self.TRASH_DIR_NAME

    def _trash_retention_days(self) -> int:
        """回收站保留天数；0 = 关闭回收站（硬删除）。非法值回退默认 30。"""
        try:
            days = int(self.config.get("trash_retention_days", 30))
        except (TypeError, ValueError):
            days = 30
        return min(max(days, 0), 3650)

    def _trash_sig(self, sender_id: str, meta: dict, payload: dict) -> str:
        """manifest 签名：身份派生钥匙覆盖 manifest 本体 + 全部 payload 字节。"""
        uk = self._user_sign_key(sender_id)
        if uk is None:
            raise DiaryKeyUnavailable("签名密钥不可用")
        h = hmac.new(uk, digestmod=hashlib.sha256)
        h.update(
            json.dumps(meta, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        for name in sorted(payload):
            h.update(b"\x00")
            h.update(name.encode("utf-8"))
            h.update(b"\x00")
            h.update(hashlib.sha256(payload[name]).digest())
        return h.hexdigest()

    # ------------------------- 写入（软删除） -------------------------

    def _trash_write(
        self,
        sender_id: str,
        *,
        kind: str,
        payload: dict,
        annotations_slice: list | None = None,
        provenance_slice: dict | None = None,
    ) -> str | None:
        """把删除下来的条目写进回收站，返回回收站条目 id；失败返回 None。

        payload 的键是日记 zip 内的原始条目名（diaries/…、sigs/…、images/…），
        写入回收站时统一加 payload/ 前缀；正文与插图签名原样随行。
        """
        if self._trash_retention_days() <= 0:
            return ""  # 回收站已关闭：调用方按硬删除继续，不算失败
        if not payload:
            return ""
        days = sorted(
            {
                self._entry_day(n)
                for n in payload
                if self.ENTRY_NAME_RE.match(n)
            }
        )
        meta = {
            "version": 1,
            "owner": str(sender_id),
            "kind": str(kind),
            "days": days,
            "deleted_at": self._now().strftime("%Y-%m-%d %H:%M:%S"),
            "ts": self._now().timestamp(),
        }
        # 切片文件同样属于 payload：一并纳入 manifest 签名域（否则回收站里
        # 带批注的条目会在读取时验签失败，且切片本身也不受篡改保护）
        signed_payload = dict(payload)
        if annotations_slice:
            signed_payload["trash_annotations_slice.json"] = json.dumps(
                {"annotations": annotations_slice}, ensure_ascii=False, indent=2
            ).encode("utf-8")
        if provenance_slice:
            signed_payload["trash_provenance_slice.json"] = json.dumps(
                {"entries": provenance_slice}, ensure_ascii=False, indent=2
            ).encode("utf-8")
        entries = {}
        try:
            meta["sig"] = self._trash_sig(sender_id, meta, signed_payload)
        except DiaryKeyUnavailable:
            logger.warning("[memory_diary] 签名密钥不可用，回收站写入被拒绝")
            return None
        entries[self.TRASH_MANIFEST] = json.dumps(
            meta, ensure_ascii=False, indent=2
        ).encode("utf-8")
        for name, blob in signed_payload.items():
            entries[self.TRASH_PAYLOAD_PREFIX + name] = blob
        trash_id = secrets.token_hex(8)
        path = self._trash_dir() / f"{self.TRASH_FILE_RE_PREFIX}{trash_id}.zip"
        try:
            self._trash_dir().mkdir(parents=True, exist_ok=True)
            self._write_zip_atomic(path, entries)
        except Exception as e:
            logger.warning(f"[memory_diary] 回收站写入失败: {e}")
            return None
        logger.info(
            f"[memory_diary] 已把 {len(days)} 天的日记移入回收站"
            f"（条目 {trash_id}，{self._trash_retention_days()} 天内可恢复）"
        )
        return trash_id

    # ------------------------- 读取 / 清单 -------------------------

    def _read_trash_item(self, path: Path):
        """读回收站条目并验签，返回 (meta, payload)；失败返回 (None, None)。"""
        try:
            with self._diary_zip(path) as z:
                data = {n: z.read(n) for n in z.namelist()}
        except Exception as e:
            logger.warning(f"[memory_diary] 回收站条目无法读取（{path.name}）: {e}")
            return None, None
        raw_manifest = data.get(self.TRASH_MANIFEST)
        if raw_manifest is None:
            return None, None
        try:
            meta = json.loads(raw_manifest.decode("utf-8"))
        except Exception:
            return None, None
        if not isinstance(meta, dict) or not meta.get("owner"):
            return None, None
        payload = {
            n[len(self.TRASH_PAYLOAD_PREFIX):]: b
            for n, b in data.items()
            if n.startswith(self.TRASH_PAYLOAD_PREFIX)
        }
        sig = str(meta.pop("sig", "") or "")
        try:
            expected = self._trash_sig(meta["owner"], meta, payload)
        except DiaryKeyUnavailable:
            return None, None
        if not sig or not hmac.compare_digest(expected, sig):
            logger.warning(
                f"[memory_diary] 回收站条目验签失败（{path.name}），已忽略"
            )
            return None, None
        return meta, payload

    def list_trash(self, sender_id: str) -> list:
        """当前身份名下的回收站清单（按删除时间升序）；验签失败的不列出。"""
        items = []
        trash_dir = self._trash_dir()
        if not trash_dir.is_dir():
            return items
        for path in sorted(trash_dir.glob(f"{self.TRASH_FILE_RE_PREFIX}*.zip")):
            meta, _payload = self._read_trash_item(path)
            if not meta or str(meta.get("owner")) != str(sender_id):
                continue
            items.append(
                {
                    "id": path.stem[len(self.TRASH_FILE_RE_PREFIX):],
                    "kind": str(meta.get("kind") or "entries"),
                    "days": list(meta.get("days") or []),
                    "deleted_at": str(meta.get("deleted_at") or ""),
                    "ts": float(meta.get("ts") or 0),
                }
            )
        items.sort(key=lambda x: x["ts"])
        return items

    # ------------------------- 恢复 -------------------------

    def restore_trash(self, sender_id: str, trash_id: str):
        """把回收站条目合并回当前日记 zip。

        返回 (restored_days, skipped_days, error)：
        - restored_days：实际放回的天（YYYYMMDD 列表）；
        - skipped_days：回收站里有、但当前日记已存在同一天（不覆盖新内容）；
        - error：整体失败原因（非空时前两者的语义为空）。
        恢复成功后回收站条目删除；部分恢复（有 skipped）也删除——
        跳过的天是用户主动放弃的旧版本，不应再留第二份。
        """
        sender_id = str(sender_id)
        path = (
            self._trash_dir() / f"{self.TRASH_FILE_RE_PREFIX}{trash_id}.zip"
        )
        if not path.exists():
            return [], [], "回收站里找不到这条记录（可能已过保留期被清理）"
        meta, payload = self._read_trash_item(path)
        if not meta:
            return [], [], "这条回收站记录验签失败，已拒绝恢复"
        if str(meta.get("owner")) != sender_id:
            return [], [], "这条回收站记录不属于当前人格名下的日记"

        # 逐篇验签（签名是删除时的原签，域含身份）：不过验的绝不放回
        diary_payload = {}
        images_payload = {}
        tampered = []
        for name, blob in payload.items():
            if self.ENTRY_NAME_RE.match(name):
                text = blob.decode("utf-8")
                actual = (
                    payload.get(self._sig_entry(name), b"")
                    .decode("utf-8", "ignore")
                    .strip()
                )
                if not self._entry_sig_matches(sender_id, name, text, actual):
                    tampered.append(name)
                    continue
                diary_payload[name] = text
            elif self.IMAGE_NAME_RE.match(name):
                sig = payload.get(self._image_sig_entry(name), b"").decode(
                    "utf-8", "ignore"
                ).strip()
                if not sig or not self._image_sig_matches(
                    sender_id, name, blob, sig
                ):
                    tampered.append(name)
                    continue
                images_payload[name] = blob
        if tampered:
            logger.warning(
                f"[memory_diary] 回收站条目 {trash_id} 有 {len(tampered)} 个"
                "条目验签失败，已跳过不恢复"
            )

        # 读当前日记 zip（存在则整体合并；不存在则按空本子建）
        live_path = self._diary_path(sender_id)
        live = {}
        if live_path.exists():
            try:
                with self._diary_zip(live_path) as z:
                    live = {n: z.read(n) for n in z.namelist()}
            except Exception as e:
                return [], [], f"当前日记文件读取失败（{e}），恢复未进行"

        restored, skipped = [], []
        out = dict(live)
        for name, text in diary_payload.items():
            if name in live:
                skipped.append(self._entry_day(name))
                continue
            # 上面的验签循环已保证签名存在且通过（缺失/不符都进了 tampered），
            # 这里只搬原签，绝不替任何条目重签（与保存路径同一条红线）
            out[name] = text.encode("utf-8")
            out[self._sig_entry(name)] = payload[self._sig_entry(name)]
            restored.append(self._entry_day(name))
        for name, blob in images_payload.items():
            if name in live:
                continue
            out[name] = blob
            sig_name = self._image_sig_entry(name)
            if sig_name in payload:
                out[sig_name] = payload[sig_name]

        # 批注切片：按 id 去重合并回 annotations.json（整体重签）
        ann_note = ""
        slice_raw = payload.get("trash_annotations_slice.json")
        if slice_raw:
            try:
                slice_items = json.loads(slice_raw.decode("utf-8")).get(
                    "annotations"
                ) or []
            except Exception:
                slice_items = []
            restored_days_set = set(restored)
            slice_items = [
                x
                for x in slice_items
                if isinstance(x, dict)
                and str(x.get("entry") or "")[:8] in restored_days_set
            ]
            if slice_items:
                ann_ok = self._merge_annotations_slice(
                    sender_id, out, slice_items
                )
                if not ann_ok:
                    ann_note = "（批注因现有批注文件验签异常未合并）"

        # 来源切片：按条目名合并回 provenance.json（整体重签）
        prov_raw = payload.get("trash_provenance_slice.json")
        if prov_raw:
            try:
                slice_entries = json.loads(prov_raw.decode("utf-8")).get(
                    "entries"
                ) or {}
            except Exception:
                slice_entries = {}
            slice_entries = {
                k: v
                for k, v in slice_entries.items()
                if str(k)[:8] in set(restored)
            }
            if slice_entries:
                self._merge_provenance_slice(sender_id, out, slice_entries)

        if not restored:
            if skipped:
                return [], sorted(set(skipped)), (
                    "这些天在当前日记里已经有更新的内容，没有覆盖放回"
                )
            return [], [], "回收站条目里没有可恢复的内容"

        if self.README_ZIP_ENTRY not in out:
            out[self.README_ZIP_ENTRY] = self._readme_text(sender_id, "").encode(
                "utf-8"
            )
        try:
            self._write_zip_atomic(live_path, out)
        except Exception as e:
            return [], [], f"写回日记文件失败（{e}），恢复未进行"
        self._invalidate_search_index(sender_id)
        self._registry_mark(sender_id)  # v0.7.7：恢复成功，登记册同步
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"[memory_diary] 回收站条目清理失败（{path.name}）: {e}")
        logger.info(
            f"[memory_diary] 已从回收站恢复 {len(restored)} 天日记"
            f"（条目 {trash_id}）{ann_note}"
        )
        return sorted(set(restored)), sorted(set(skipped)), ""

    def _merge_annotations_slice(self, sender_id: str, out: dict, items: list) -> bool:
        """把批注切片合并进待写出的 zip 条目 dict；现有批注验签异常时返回 False。"""
        raw = out.get(self.ANNOTATIONS_ENTRY)
        if raw is not None:
            actual = (
                out.get(self.ANNOTATIONS_SIG_ENTRY, b"")
                .decode("utf-8", "ignore")
                .strip()
            )
            if not self._annotations_sig_matches(sender_id, raw, actual):
                logger.warning("[memory_diary] 现有批注验签失败，回收站批注未合并")
                return False
            try:
                data = self._normalize_annotations(json.loads(raw.decode("utf-8")))
            except Exception:
                return False
        else:
            data = self._annotations_empty()
        have = {str(x.get("id") or "") for x in data["annotations"]}
        for item in items:
            if str(item.get("id") or "") not in have:
                data["annotations"].append(item)
        data["annotations"] = self._trim_annotations(data["annotations"])
        data["updated_at"] = self._now().strftime("%Y-%m-%dT%H:%M:%S")
        raw_out = self._dump_annotations(data)
        out[self.ANNOTATIONS_ENTRY] = raw_out
        out[self.ANNOTATIONS_SIG_ENTRY] = self._annotations_sig(
            sender_id, raw_out
        ).encode("utf-8")
        return True

    def _merge_provenance_slice(self, sender_id: str, out: dict, entries: dict) -> bool:
        """把来源切片合并进待写出的 zip 条目 dict；现有侧车验签异常时返回 False。"""
        raw = out.get(self.PROVENANCE_ENTRY)
        if raw is not None:
            actual = (
                out.get(self.PROVENANCE_SIG_ENTRY, b"")
                .decode("utf-8", "ignore")
                .strip()
            )
            if not self._provenance_sig_matches(sender_id, raw, actual):
                logger.warning("[memory_diary] 现有来源侧车验签失败，回收站来源未合并")
                return False
            try:
                data = self._normalize_provenance(json.loads(raw.decode("utf-8")))
            except Exception:
                return False
        else:
            data = self._provenance_empty()
        for name, rec in entries.items():
            if name not in data["entries"] and isinstance(rec, dict):
                data["entries"][name] = rec
        data = self._normalize_provenance(data)
        raw_out = self._dump_provenance(data)
        out[self.PROVENANCE_ENTRY] = raw_out
        out[self.PROVENANCE_SIG_ENTRY] = self._provenance_sig(
            sender_id, raw_out
        ).encode("utf-8")
        return True

    # ------------------------- 到期清理 -------------------------

    def _purge_trash(self) -> int:
        """物理删除超过保留期的回收站条目；返回清理数量。"""
        days = self._trash_retention_days()
        trash_dir = self._trash_dir()
        if not trash_dir.is_dir():
            return 0
        now_ts = self._now().timestamp()
        real_now_ts = time.time()
        removed = 0
        for path in sorted(trash_dir.glob(f"{self.TRASH_FILE_RE_PREFIX}*.zip")):
            try:
                expired = False
                # manifest 读不出来（损坏/验签失败）时按文件 mtime 判定，
                # 不能让一个坏文件永久赖在回收站里
                # （mtime 是真实时钟，不能与可能带时区配置的 _now() 混算）
                meta = None
                try:
                    meta, _ = self._read_trash_item(path)
                except Exception:
                    meta = None
                if meta and meta.get("ts"):
                    expired = (now_ts - float(meta["ts"])) > days * 86400
                else:
                    expired = (
                        real_now_ts - path.stat().st_mtime
                    ) > max(days, 1) * 86400
                if expired:
                    path.unlink(missing_ok=True)
                    removed += 1
            except Exception as e:
                logger.warning(f"[memory_diary] 回收站清理失败（{path.name}）: {e}")
        if removed:
            logger.info(f"[memory_diary] 回收站例行清理：已删除 {removed} 条过期记录")
        return removed
