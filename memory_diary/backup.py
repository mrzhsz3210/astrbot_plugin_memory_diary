# -*- coding: utf-8 -*-
"""外部删除/篡改韧性（v0.7.7，Bug C）：滚动自动备份、破坏告警、官方恢复路径。

与回收站（trash.py）的分工：回收站管"插件内的删除"（/删除日记、Web 批量
删除），本模块管"插件外的破坏"——AI 用别的插件的 shell/python 工具直接删
或改写 plugin_data 里的日记包。插件拦不住别人删，但可以：

  1. 滚动备份：每次写日记覆盖 zip 之前，把当前 zip 复制一份到 ``backups/``
     子目录，每个日记本保留最近 N 份（``backup_copies``，默认 5，0 关闭）。
     复制的是原始字节（加密状态原样保留），恢复时逐条验签，绝不绕过签名体系。
  2. 登记册 ``diary_registry.json``：记录"插件知道应该存在的日记包"。
     启动时与 /日记状态 里对照磁盘——登记在册却凭空消失 = 疑似外部删除，
     ERROR 日志 + 状态页/WebUI 明示，并列出可用备份。
  3. 官方恢复路径：从 backups/ 恢复前对备份包**全量验签**（正文、插图、
     批注、来源侧车），任何一项不过都拒绝；通过才把现有（可能已被外部
     捏造的）文件隔离到 ``.corrupt-*`` 副本并放回备份。

登记册只记"插件自己写过/恢复过的包"。外部凭空捏造的无签名假包不在册，
它由"全量条目验签失败"这条告警兜底（fail-closed 口径不变：不展示、
不重签、拒绝写入，但恢复入口可以把好备份放回去解锁）。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import time
from pathlib import Path

from .common import split_diary_identity
from .log import logger


class BackupMixin:
    """滚动备份与外部破坏告警；由 MemoryDiaryCore 组合（不 import AstrBot）。"""

    BACKUP_DIR_NAME = "backups"
    REGISTRY_FILE = "diary_registry.json"

    # ------------------------- 配置 -------------------------

    def _backup_copies(self) -> int:
        """每个日记本保留几份滚动备份；0 = 关闭。非法值回退默认 5。"""
        try:
            n = int(self.config.get("backup_copies", 5))
        except (TypeError, ValueError):
            n = 5
        return min(max(n, 0), 50)

    def _backup_dir(self) -> Path:
        return self.data_dir / self.BACKUP_DIR_NAME

    # ------------------------- 滚动备份 -------------------------

    def _roll_backup(self, sender_id: str, path: Path) -> None:
        """写日记覆盖前，把当前 zip 复制进 backups/ 并按份数裁剪。

        任何失败都只记 warning——备份是保险丝，绝不能阻塞写日记主流程。
        """
        try:
            keep = self._backup_copies()
            if keep <= 0 or not path.exists():
                return
            backup_dir = self._backup_dir()
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = self._now().strftime("%Y%m%d_%H%M%S")
            dest = backup_dir / f"{path.stem}_{ts}.zip"
            if dest.exists():
                # 同一秒多次写：加随机后缀避免互相覆盖
                dest = backup_dir / f"{path.stem}_{ts}_{secrets.token_hex(2)}.zip"
            shutil.copy2(path, dest)
            copies = sorted(backup_dir.glob(f"{path.stem}_*.zip"))
            for old in copies[:-keep] if len(copies) > keep else []:
                old.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"[memory_diary] 滚动备份失败（不阻塞写日记）: {e}")

    def list_diary_backups(self, sender_id: str) -> list:
        """某身份名下的滚动备份清单（新到旧）：[{name, label, days}]。"""
        stem = self._diary_path(sender_id).stem
        backup_dir = self._backup_dir()
        items = []
        if not backup_dir.is_dir():
            return items
        for path in sorted(
            backup_dir.glob(f"{stem}_*.zip"), reverse=True
        ):
            ts = path.stem[len(stem) + 1:]
            label = ts
            if len(ts) >= 15 and ts[8] == "_":
                label = (
                    f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}"
                )
            days = []
            try:
                with self._diary_zip(path) as z:
                    days = sorted(
                        self._entry_day(n)
                        for n in z.namelist()
                        if self.ENTRY_NAME_RE.match(n)
                    )
            except Exception:
                pass
            items.append({"name": path.name, "label": label, "days": days})
        return items

    # ------------------------- 登记册与破坏告警 -------------------------

    def _registry_path(self) -> Path:
        return self.data_dir / self.REGISTRY_FILE

    def _load_registry(self) -> dict:
        try:
            data = json.loads(self._registry_path().read_text(encoding="utf-8"))
            files = data.get("files") if isinstance(data, dict) else None
            return files if isinstance(files, dict) else {}
        except Exception:
            return {}

    def _save_registry(self, files: dict) -> None:
        try:
            p = self._registry_path()
            tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
            tmp.write_text(
                json.dumps({"version": 1, "files": files}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, p)
        except Exception as e:
            logger.warning(f"[memory_diary] 日记登记册写入失败: {e}")

    def _registry_mark(self, sender_id: str) -> None:
        """写入/恢复成功后登记：这个日记包应该存在。"""
        try:
            path = self._diary_path(sender_id)
            files = self._load_registry()
            files[path.name] = {
                "owner": str(sender_id),
                "updated": self._now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save_registry(files)
        except Exception as e:
            logger.warning(f"[memory_diary] 日记登记册更新失败: {e}")

    def _registry_unmark(self, sender_id: str) -> None:
        """插件内整包删除后销记：这个包不再应该存在（区别于外部删除）。"""
        try:
            files = self._load_registry()
            if files.pop(self._diary_path(sender_id).name, None) is not None:
                self._save_registry(files)
        except Exception as e:
            logger.warning(f"[memory_diary] 日记登记册更新失败: {e}")

    def _vanished_diary_alerts(self, base_sender: str = "") -> list:
        """登记在册但磁盘上已消失的日记包（疑似外部删除）。

        base_sender 非空时只报该用户的（/日记状态 是用户视角）；
        空串时全量（启动扫描/管理视角）。
        """
        alerts = []
        for name, rec in sorted(self._load_registry().items()):
            if not isinstance(rec, dict):
                continue
            owner = str(rec.get("owner") or "")
            if base_sender and split_diary_identity(owner)[0] != str(base_sender):
                continue
            if not (self.data_dir / name).exists():
                backups = self.list_diary_backups(owner) if owner else []
                alerts.append(
                    {
                        "file": name,
                        "owner": owner,
                        "updated": str(rec.get("updated") or ""),
                        "backups": backups,
                    }
                )
        return alerts

    def _all_entries_tampered(self, sender_id: str) -> bool:
        """日记包存在且含有日记条目、但**全部**条目验签失败（疑似外部篡改/捏造）。

        条目为空（无日记）或 zip 读不出来都不算——前者无据可判，
        后者走既有的 corrupt 隔离口径。
        """
        path = self._diary_path(sender_id)
        if not path.exists():
            return False
        try:
            with self._diary_zip(path) as z:
                names = set(z.namelist())
                entries = sorted(n for n in names if self.ENTRY_NAME_RE.match(n))
                if not entries:
                    return False
                for n in entries:
                    sig_name = self._sig_entry(n)
                    actual = (
                        z.read(sig_name).decode("utf-8", "ignore").strip()
                        if sig_name in names
                        else ""
                    )
                    if self._entry_sig_matches(
                        sender_id, n, z.read(n).decode("utf-8"), actual
                    ):
                        return False
                return True
        except Exception:
            return False

    def _external_damage_scan(self) -> list:
        """启动时的外部破坏扫描：消失的登记包 + 全量验签失败的包，ERROR 日志。

        返回告警文本列表（供 /日记状态 与 WebUI 复用）；扫描本身任何异常
        降级为 warning，绝不阻塞启动。
        """
        alerts = []
        try:
            for item in self._vanished_diary_alerts():
                msg = (
                    f"日记包 {item['file']} 登记在册但已凭空消失"
                    f"（归属 {item['owner'] or '未知'}），疑似被插件之外的操作删除；"
                    f"可用备份 {len(item['backups'])} 份，"
                    "用户可发 /恢复日记 查看并恢复"
                )
                logger.error(f"[memory_diary] {msg}")
                alerts.append(msg)
            for path in sorted(self.data_dir.glob("diary_*.zip")):
                owner, _nick = self._zip_owner_info(path)
                if not owner:
                    continue
                if self._all_entries_tampered(owner):
                    backups = self.list_diary_backups(owner)
                    msg = (
                        f"日记包 {path.name} 的全部条目验签失败（归属 {owner}），"
                        "疑似被插件之外的操作改写/捏造；已按防篡改规则锁死读写，"
                        f"可用备份 {len(backups)} 份，用户可发 /恢复日记 从备份恢复"
                    )
                    logger.error(f"[memory_diary] {msg}")
                    alerts.append(msg)
        except Exception as e:
            logger.warning(f"[memory_diary] 外部破坏扫描异常: {e}")
        return alerts

    # ------------------------- 从备份恢复 -------------------------

    def restore_diary_backup(self, sender_id: str, backup_name: str):
        """把一份滚动备份放回为当前日记包。返回 (恢复的天数列表, 错误信息)。

        恢复前对备份包**全量验签**：正文逐篇、插图逐张、批注/来源侧车整包，
        任何一项不过都拒绝（fail-closed，与读取口径一致）；
        当前已存在的（可能被外部捏造的）日记包先隔离成 .corrupt-* 副本。
        """
        sender_id = str(sender_id)
        backup_dir = self._backup_dir()
        name = str(backup_name or "")
        # 防目录穿越：只认本目录下 "<该身份文件stem>_*.zip" 的真实文件
        stem = self._diary_path(sender_id).stem
        path = backup_dir / name
        if (
            not name.startswith(f"{stem}_")
            or not name.endswith(".zip")
            or "/" in name
            or "\\" in name
            or not path.exists()
        ):
            return [], "找不到这份备份"
        try:
            with self._diary_zip(path) as z:
                data = {n: z.read(n) for n in z.namelist()}
        except Exception as e:
            return [], f"备份包读取失败（{e}）"
        names = set(data)
        entries = sorted(n for n in names if self.ENTRY_NAME_RE.match(n))
        if not entries:
            return [], "这份备份里没有日记"
        try:
            for n in entries:
                sig_name = self._sig_entry(n)
                if sig_name not in names:
                    return [], f"备份缺少 {self._entry_label(n)} 的签名，已拒绝恢复"
                text = data[n].decode("utf-8")
                actual = data[sig_name].decode("utf-8", "ignore").strip()
                if not self._entry_sig_matches(sender_id, n, text, actual):
                    return [], f"备份里 {self._entry_label(n)} 验签失败，已拒绝恢复"
            for n in sorted(names):
                if self.IMAGE_NAME_RE.match(n):
                    sig_name = self._image_sig_entry(n)
                    actual = (
                        data[sig_name].decode("utf-8", "ignore").strip()
                        if sig_name in names
                        else ""
                    )
                    if not actual or not self._image_sig_matches(
                        sender_id, n, data[n], actual
                    ):
                        return [], f"备份里的插图 {n} 验签失败，已拒绝恢复"
            for entry, sig_entry, checker in (
                (
                    self.ANNOTATIONS_ENTRY,
                    self.ANNOTATIONS_SIG_ENTRY,
                    self._annotations_sig_matches,
                ),
                (
                    self.PROVENANCE_ENTRY,
                    self.PROVENANCE_SIG_ENTRY,
                    self._provenance_sig_matches,
                ),
            ):
                if entry in names:
                    actual = (
                        data.get(sig_entry, b"").decode("utf-8", "ignore").strip()
                    )
                    if not checker(sender_id, data[entry], actual):
                        return [], "备份的附属文件验签失败，已拒绝恢复"
            # 交接信 / 素材队列 / 锚点归档：与各自读取路径同口径的 compare_digest
            for entry, sig_entry, signer in (
                (
                    self.CURRENT_SELF_ENTRY,
                    self.CURRENT_SELF_SIG_ENTRY,
                    self._current_self_sig,
                ),
                (
                    self.MATERIALS_ENTRY,
                    self.MATERIALS_SIG_ENTRY,
                    self._materials_sig,
                ),
                (
                    self.CURRENT_SELF_ARCHIVE_ENTRY,
                    self.CURRENT_SELF_ARCHIVE_SIG_ENTRY,
                    self._current_self_archive_sig,
                ),
            ):
                if entry in names:
                    actual = (
                        data.get(sig_entry, b"").decode("utf-8", "ignore").strip()
                    )
                    expected = signer(sender_id, data[entry])
                    if not actual or not hmac.compare_digest(expected, actual):
                        return [], "备份的附属文件验签失败，已拒绝恢复"
        except Exception as e:
            return [], f"备份验签过程出错（{e}），已拒绝恢复"

        live = self._diary_path(sender_id)
        try:
            if live.exists():
                # 现有文件（可能是外部捏造/改写的）先隔离，绝不静默覆盖
                self._quarantine_corrupt(live)
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, live)
        except Exception as e:
            return [], f"放回备份失败（{e}）"
        self._invalidate_search_index(sender_id)
        self._registry_mark(sender_id)
        days = sorted(self._entry_day(n) for n in entries)
        logger.info(
            f"[memory_diary] 已从备份恢复用户 {sender_id} 的日记："
            f"{len(days)} 天（备份 {name}）"
        )
        return days, ""
