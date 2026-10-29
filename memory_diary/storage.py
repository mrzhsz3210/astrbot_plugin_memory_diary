# -*- coding: utf-8 -*-
from __future__ import annotations

"""日记存储：文件布局、HMAC 验签、偏好/备忘、迁移包与 Web 数据。

这一层不 import AstrBot：所有 AstrBot 交互都通过调用方传入的 context /
event 完成。因此它可以在普通 Python 进程里单独导入和测试。
"""

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from .common import safe_key
from .errors import (
    DiaryCorruptError,
    DiaryKeyUnavailable,
    DiaryTamperedError,
)
from .log import logger


class StorageMixin:
    """存储与校验关注点；由 MemoryDiaryCore 组合。"""


    # ------------------------- 存储 -------------------------
    #
    # 日记本体按用户存成一个 zip：diary_<用户ID哈希>.zip（按天归档、分文件夹）
    #   ├── diaries/20261023.md    某一天的日记（当天写多篇会追加在同一文件，带时间分隔）
    #   ├── sigs/20261023.sig      当天文件的 HMAC-SHA256 防篡改签名
    #   └── 说明.txt               日记结构与使用说明
    # 主密钥 .diary_key 在 zip 外（同目录），绝不入包——否则篡改者可自行重签。
    # 新上下文默认自动读最近 7 天的文件（可配）；更早的日期可按需查询。
    # 管理员可直接把对应用户的 zip 发给本人；插件读写 zip 对模型完全透明。

    README_ZIP_ENTRY = "说明.txt"
    ENTRY_NAME_RE = re.compile(r"^diaries/\d{8}\.md$")  # 当前格式
    _LEGACY_DAY_RE = re.compile(r"^\d{8}\.md$")  # v3.1~3.3 平铺按天，仅迁移用
    _V3_ENTRY_RE = re.compile(r"^\d{8}_\d{6}(_\d+)?\.md$")  # v3.0 按篇命名，仅迁移用

    @staticmethod
    def _md_entry(day: str) -> str:
        return f"diaries/{day}.md"

    @staticmethod
    def _sig_entry(md_name: str) -> str:
        """diaries/20261023.md -> sigs/20261023.sig"""
        return f"sigs/{md_name.split('/')[-1][:-3]}.sig"

    @staticmethod
    def _entry_day(entry_name: str) -> str:
        """diaries/20261023.md -> 20261023"""
        return entry_name.split("/")[-1][:8]

    # 日记片段模式：完整 / 简记 / 随笔。旧格式没有模式标记：
    # 含"补记"视为简记，含"无新对话"视为随笔，其余视为完整。
    _MODE_LABELS = {"full": "完整", "brief": "简记", "life": "随笔"}

    @classmethod
    def _segments(cls, content: str) -> list:
        """把一天的文件按分隔条拆成片段（header 可能为 None，兼容旧格式）。"""
        result = []
        header, lines = None, []
        for line in content.split("\n"):
            stripped = line.strip()
            if cls._SEG_HEADER_RE.match(stripped):
                result.append(
                    {"header": header, "text": "\n".join(lines).strip("\n")}
                )
                header, lines = stripped, []
            else:
                lines.append(line)
        result.append({"header": header, "text": "\n".join(lines).strip("\n")})
        return [s for s in result if s["header"] or s["text"].strip()]

    @classmethod
    def _segment_info(cls, header: str) -> dict:
        """从分隔条推断模式/场景/是否已被完整篇吸收。"""
        h = header or ""
        if "随笔" in h or "无新对话" in h:
            mode = "life"
        elif "简记" in h or "补记" in h:
            mode = "brief"
        else:
            mode = "full"
        if "写于群聊" in h:
            scene = "group"
        else:
            scene = "private"  # 旧格式无场景标记，按私聊处理
        return {"mode": mode, "scene": scene, "absorbed": "已吸收" in h}

    def _briefs_for_scene(self, content: str, scene: str) -> list:
        """返回当前场景下尚未被完整篇吸收的简记片段。"""
        out = []
        for seg in self._segments(content):
            info = self._segment_info(seg["header"])
            if (
                info["mode"] == "brief"
                and not info["absorbed"]
                and info["scene"] == scene
            ):
                out.append(seg)
        return out

    def _full_count_for_scene(self, content: str, scene: str) -> int:
        """统计当天当前场景已写了几篇完整日记（旧格式首篇也算完整）。"""
        count = 0
        for seg in self._segments(content):
            info = self._segment_info(seg["header"])
            if info["mode"] == "full" and info["scene"] == scene:
                count += 1
        return count

    @staticmethod
    def _mark_absorbed(content: str, headers: list) -> str:
        """把被完整篇吸收的简记分隔条标记为（已吸收），不删除原文。"""
        wanted = set(headers or [])
        if not wanted:
            return content
        lines = []
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped in wanted and stripped.endswith(" ——"):
                lines.append(stripped[:-3] + "（已吸收）" + stripped[-3:])
            else:
                lines.append(line)
        return "\n".join(lines)

    @classmethod
    def _filter_absorbed_segments(cls, content: str) -> str:
        """注入时跳过已被完整篇吸收的简记，原文仍保留在文件里可查看。"""
        kept = []
        for seg in cls._segments(content):
            header = seg["header"]
            if header and "已吸收" in header:
                continue
            body = seg["text"].strip("\n")
            if header:
                kept.append(f"{header}\n\n{body}" if body.strip() else header)
            elif body.strip():
                kept.append(body)
        return "\n\n".join(kept)

    def _diary_file_key(self, sender_id: str) -> str:
        """日记文件名 key：用签名密钥做 HMAC 派生，替代无盐 MD5(QQ号)——
        防止拿到数据目录的人离线枚举 QQ 号定位某位用户的日记文件。

        密钥不可用时回退旧的 MD5 命名，保证已有日记仍然可读写（不迁移）。
        """
        key = self._secret_key()
        if key is None:
            return safe_key(sender_id)
        return hmac.new(
            key, f"file:{sender_id}".encode("utf-8"), hashlib.sha256
        ).hexdigest()[:16]

    def _diary_path(self, sender_id: str) -> Path:
        new_path = self.data_dir / f"diary_{self._diary_file_key(sender_id)}.zip"
        old_path = self.data_dir / f"diary_{safe_key(sender_id)}.zip"
        if new_path != old_path and old_path.exists() and not new_path.exists():
            # 旧版（MD5 命名）文件首次访问时自动改名迁移
            try:
                old_path.rename(new_path)
                logger.info(
                    f"[memory_diary] 用户 {sender_id} 的日记文件已迁移为 HMAC 派生文件名"
                )
            except Exception as e:
                logger.warning(f"[memory_diary] 日记文件名迁移失败，沿用旧文件名: {e}")
                return old_path
        return new_path

    def _legacy_diary_paths(self, sender_id: str):
        """v1.x 时代的明文存储（.md + .sig），用于自动迁移。"""
        base = self.data_dir / f"diary_{safe_key(sender_id)}"
        return base.with_suffix(".md"), base.with_suffix(".sig")

    def _secret_key(self):
        """签名密钥：首次使用时随机生成（权限 0o600），缓存在内存，不落日志。

        fail-closed（P2-6 修复）：读不到/写不了密钥文件时返回 None，
        由调用方拒绝签名与写入——绝不退回源码里写死的公开常量，
        否则任何人都能算出"密钥"，防篡改形同虚设。
        """
        if self._key_cache:
            return self._key_cache
        key_path = self.data_dir / ".diary_key"
        try:
            if key_path.exists():
                key = key_path.read_bytes().strip()
                if key:
                    self._key_cache = key
                    return key
                logger.error("[memory_diary] 签名密钥文件为空，已拒绝签名相关操作")
                return None
            # 迁移收养（仅兼容 v1 旧迁移包的手动拖放流程）：数据目录里有旧版
            # 迁移包解压出的 diary_key.txt、且本机还没有 .diary_key 时，
            # 收养它作为主密钥。v2 包带的是 user_key.txt（仅单人派生钥匙），
            # 不会被这里收养，应走 /导入日记 或 Web 页面导入。
            # 已有 .diary_key 时绝不收养（上面的分支已返回），避免顶掉在用的密钥。
            adopt_path = self.data_dir / "diary_key.txt"
            if adopt_path.exists():
                adopted = adopt_path.read_bytes().strip()
                if adopted:
                    adopt_path.rename(key_path)
                    try:
                        os.chmod(key_path, 0o600)
                    except Exception:
                        pass
                    self._key_cache = adopted
                    logger.info(
                        "[memory_diary] 已收养迁移包中的 diary_key.txt 作为签名主密钥"
                    )
                    return adopted
                logger.error("[memory_diary] diary_key.txt 为空，无法收养为签名密钥")
                return None
            key = secrets.token_hex(32).encode("utf-8")
            # 先落盘再启用：写不进去就无法保证下次启动还能验签，宁可拒绝
            fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
            try:
                os.chmod(key_path, 0o600)  # Windows 上作用有限，POSIX 上有效
            except Exception:
                pass
            self._key_cache = key
            return key
        except Exception as e:
            logger.error(
                f"[memory_diary] 读取/生成签名密钥失败: {e}——已拒绝签名相关操作"
            )
            return None

    def _signature(self, sender_id: str, content: str) -> str:
        """旧版（v1.x/v2.0 单篇）签名，仅用于迁移校验。"""
        key = self._secret_key()
        if key is None:
            raise DiaryKeyUnavailable("签名密钥不可用")
        return hmac.new(
            key,
            (sender_id + "\n" + content).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _user_sign_key(self, sender_id: str):
        """某个用户的日记签名钥匙：由主密钥按 QQ 号派生，一人一把。

        导出迁移包只带这把钥匙——它泄露了也只能验/改这一份日记，
        主密钥永远不出服务器。这样普通用户也能安全地自助导出自己的日记。
        """
        key = self._secret_key()
        if key is None:
            return None
        return hmac.new(
            key, f"user-key:{sender_id}".encode("utf-8"), hashlib.sha256
        ).digest()

    def _entry_sig(self, sender_id: str, entry_name: str, content: str) -> str:
        """条目签名：绑定用户 + 条目名（防改名）+ 内容。用该用户的派生钥匙。"""
        uk = self._user_sign_key(sender_id)
        if uk is None:
            raise DiaryKeyUnavailable("签名密钥不可用")
        return hmac.new(
            uk,
            (sender_id + "\n" + entry_name + "\n" + content).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _entry_sig_matches(self, sender_id: str, entry_name: str,
                           content: str, actual: str) -> bool:
        """验签：先按现行派生钥匙验，再回退旧版主密钥直签（v0.4.0 及以前）。

        旧签名的条目在下次 _save_diary 整体重写时会被自动升级为派生钥匙签名。
        """
        if not actual:
            return False
        try:
            if hmac.compare_digest(
                self._entry_sig(sender_id, entry_name, content), actual
            ):
                return True
        except DiaryKeyUnavailable:
            return False
        key = self._secret_key()
        if key is None:
            return False
        legacy = hmac.new(
            key,
            (sender_id + "\n" + entry_name + "\n" + content).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(legacy, actual)

    def _readme_text(self, sender_id: str, sender_name: str) -> str:
        return (
            "记忆日记\n"
            "========\n\n"
            f"用户昵称：{sender_name or '（未知）'}\n"
            # Web 日记本页面验签需要主人 QQ（文件名是 HMAC 派生、反推不出）；
            # 日记 zip 本就只发给本人，写入本人 QQ 不构成额外泄露
            f"用户ID：{sender_id}\n"
            f"说明生成时间：{self._now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "【这是什么】\n"
            '这个 zip 保存着一位 AI 写给"下一个自己"的日记。日记按天归档：\n'
            "diaries/ 文件夹里每天一个 <日期>.md 文件（当天写多篇会追加在同一文件里，带时间分隔），\n"
            "sigs/ 文件夹里是对应的防篡改签名（仅供插件验签，可忽略）。完整日记包含四个部分：\n"
            "1. 涵盖时间  2. 用户画像  3. AI 对用户的看法与评价  4. 给未来的 AI 的叮嘱\n"
            '“完整”是四段式质量锚点；“简记”是对新增内容的 1~3 句概括；\n'
            '“简记（已吸收）”表示它已经被后面的完整篇合并，插件注入时会跳过；\n'
            '标注“无新对话·她的日常随笔”的是没有新聊天时 AI 写的自己的拟人日常，不含用户画像。\n'
            '分隔条上的"写于私聊 / 写于群聊"是场景标注：开启群聊注入过滤后，\n'
            "写于私聊的片段不会被注入到群聊的新上下文中（反向不限）。\n\n"
            "【如何使用】\n"
            "解压后把需要的某篇（通常是时间最新的那篇）.md 内容粘贴给任何设备上的任何 AI，\n"
            '并告诉她："这是上一个你写给未来自己的日记，请先阅读并吸收其中的用户画像与叮嘱，\n'
            '然后自然地继续与用户相处。"\n\n'
            "【存储位置】\n"
            f"本文件在 AstrBot 服务器上的位置：{self._diary_path(sender_id).resolve()}\n\n"
            "【其他说明】\n"
            "每个 .sig 文件是插件的防篡改签名，仅供 AstrBot 内的记忆日记插件验签使用，可忽略。\n"
        )

    @staticmethod
    def _entry_label(entry_name: str) -> str:
        """diaries/20261023.md -> 2026-10-23；兼容旧命名。"""
        name = entry_name.split("/")[-1]
        try:
            if "_" in name:
                dt = datetime.strptime(name[:15], "%Y%m%d_%H%M%S")
                return dt.strftime("%Y-%m-%d %H:%M")
            dt = datetime.strptime(name[:8], "%Y%m%d")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return entry_name

    def _list_entries(self, sender_id: str) -> list:
        """列出该用户全部日记条目（按时间升序）。会自动做旧格式迁移。"""
        self._migrate_legacy(sender_id)
        path = self._diary_path(sender_id)
        entries = []
        if path.exists():
            try:
                with zipfile.ZipFile(path) as z:
                    names = set(z.namelist())
                    for n in names:
                        if self.ENTRY_NAME_RE.match(n) and self._sig_entry(n) in names:
                            entries.append(n)
            except zipfile.BadZipFile:
                # 读路径不把损坏吞成"没有日记"：error 级日志 + 指令层用 _corrupt_notice 区分
                logger.error(
                    f"[memory_diary] 日记 zip 已损坏（{path}），读取按空处理；"
                    "下次写入时会自动隔离备份"
                )
            except Exception as e:
                logger.warning(f"[memory_diary] 列出日记条目失败: {e}")
        return sorted(entries)

    def _corrupt_notice(self, sender_id: str) -> str:
        """日记文件损坏时给用户的提示文案；未损坏返回空串（P1-3 读路径）。

        不能只看 is_zipfile：中央目录还在、但某个条目 CRC 坏掉的 zip 仍然
        会被 is_zipfile 判为 True。必须真正 testzip() 读一遍所有条目。
        """
        path = self._diary_path(sender_id)
        if not path.exists():
            return ""
        try:
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                if bad is not None:
                    logger.error(
                        f"[memory_diary] 日记 zip 条目 CRC 校验失败：{path} / {bad}"
                    )
                    return (
                        "📖 你的日记文件有损坏（有一天的内容 CRC 校验失败），"
                        "读不出来——我没有动它。\n"
                        "请管理员到插件数据目录检查这个文件（可以尝试用压缩软件修复，"
                        "或确认损坏后直接删掉，我下次会重新建一个）。"
                        "处理好之前，我不会往里写新日记，避免把残存的内容冲掉。"
                    )
        except Exception as e:
            logger.error(f"[memory_diary] 日记 zip 无法读取：{path} / {e}")
            return (
                "📖 你的日记文件已经损坏，读不出来——我没有动它。\n"
                "请管理员到插件数据目录检查这个文件（可以尝试用压缩软件修复，"
                "或确认损坏后直接删掉，我下次会重新建一个）。"
                "处理好之前，我不会往里写新日记，避免把残存的内容冲掉。"
            )
        return ""

    def _load_entry(self, sender_id: str, entry_name: str):
        """读取一篇日记。返回 (content, tampered)。"""
        path = self._diary_path(sender_id)
        try:
            with zipfile.ZipFile(path) as z:
                content = z.read(entry_name).decode("utf-8").strip()
                try:
                    actual = z.read(self._sig_entry(entry_name)).decode("utf-8").strip()
                except KeyError:
                    actual = ""  # 签名缺失视为篡改
        except Exception as e:
            # 读取失败（CRC、缺条目、临时 IO 错误）绝不能伪装成“空日记”：返回
            # tampered=True 让调用方跳过并提示，避免出现“0 字，校验通过”。
            logger.error(f"[memory_diary] 读取日记条目失败: {e}")
            return "", True
        if self._secret_key() is None:
            logger.error(
                f"[memory_diary] 签名密钥不可用，无法验签，已拒绝读取 {entry_name}"
            )
            return "", True
        if not self._entry_sig_matches(sender_id, entry_name, content, actual):
            logger.warning(
                f"[memory_diary] 用户 {sender_id} 的日记 {entry_name} 验签失败，已拒绝读取"
            )
            return "", True
        return content, False

    def _load_latest(self, sender_id: str):
        """读取最新一篇日记。返回 (content, tampered, entry_name 或 None)。"""
        migration = self._migrate_legacy(sender_id)
        entries = self._list_entries(sender_id)
        if not entries:
            return "", migration == "tampered", None
        content, tampered = self._load_entry(sender_id, entries[-1])
        return content, tampered, entries[-1]

    # ------------------------- 注入范围偏好 -------------------------

    def _pref_path(self, sender_id: str) -> Path:
        return self.data_dir / f"pref_{safe_key(sender_id)}.json"

    def _load_pref_data(self, sender_id: str) -> dict:
        """读取用户偏好文件（含注入范围、上次写作素材哈希等）。"""
        try:
            data = json.loads(self._pref_path(sender_id).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_pref_data(self, sender_id: str, data: dict) -> bool:
        """保存偏好数据；返回是否成功——失败时调用方必须告知用户，不能谎报（P2-5）。

        原子写（P1-1）：先写临时文件再 os.replace，避免 write_text 原地截断
        导致一次崩溃丢掉范围设置/备忘/素材哈希。
        """
        try:
            p = self._pref_path(sender_id)
            if data:
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, p)
            else:
                p.unlink(missing_ok=True)
            return True
        except Exception as e:
            logger.warning(f"[memory_diary] 保存用户偏好失败: {e}")
            return False

    def _load_pref(self, sender_id: str):
        """读取用户注入范围偏好；返回 scope 字符串或 None（用默认）。"""
        scope = self._load_pref_data(sender_id).get("scope")
        return scope if isinstance(scope, str) else None

    def _save_pref(self, sender_id: str, scope) -> bool:
        """保存注入范围偏好；返回是否成功（P2-5b：失败时指令层必须明说）。"""
        data = self._load_pref_data(sender_id)
        if scope is None:
            data.pop("scope", None)
        else:
            data["scope"] = scope
        return self._save_pref_data(sender_id, data)

    def _load_material_hash(self, sender_id: str) -> str:
        """上次写日记时使用的素材哈希，用于判断"有没有说过新的话"。"""
        h = self._load_pref_data(sender_id).get("last_material_hash")
        return h if isinstance(h, str) else ""

    def _save_material_hash(self, sender_id: str, h: str):
        data = self._load_pref_data(sender_id)
        data["last_material_hash"] = h
        self._save_pref_data(sender_id, data)

    # ------------------------- 素材游标（避免多篇日记重复读同一段） -------------------------
    # 每个 umo（私聊/每个群）一个游标：记录上次写日记时读到了历史中的第几条、
    # 最后一条消息的哈希。下次只取游标之后的新消息；找不到游标时回退最近 N 条。
    # 游标只存结构和哈希，不做语义检索，也不引入 embedding。

    @staticmethod
    def _cursor_key(umo: str) -> str:
        return "cursor_" + hashlib.md5(umo.encode("utf-8")).hexdigest()[:16]

    def _load_material_cursor(self, sender_id: str, umo: str) -> dict:
        cursors = self._load_pref_data(sender_id).get("material_cursors")
        if not isinstance(cursors, dict):
            return {}
        rec = cursors.get(self._cursor_key(umo))
        return rec if isinstance(rec, dict) else {}

    def _save_material_cursor(self, sender_id: str, umo: str, rec: dict):
        data = self._load_pref_data(sender_id)
        cursors = data.get("material_cursors")
        if not isinstance(cursors, dict):
            cursors = {}
        cursors[self._cursor_key(umo)] = rec
        # 防止长期运行无限增长；只保留最近 64 个场景
        while len(cursors) > 64:
            cursors.pop(next(iter(cursors)))
        data["material_cursors"] = cursors
        self._save_pref_data(sender_id, data)

    def _message_hash(self, msg) -> str:
        """历史消息的轻量指纹：role + 文本。只用于游标边界，不做语义。"""
        if not isinstance(msg, dict):
            return ""
        text = self._content_to_text(msg.get("content", ""))
        return hashlib.md5(
            (str(msg.get("role", "")) + "\n" + text).encode("utf-8")
        ).hexdigest()

    # ------------------------- 用户备忘（/记一下） -------------------------
    # 用户点名要记住的事，暂存在偏好文件里，下次写日记时作为重点素材注入，
    # 成功写入日记后清除。每条备忘带场景标签（私聊/群聊）：
    # - 私聊写日记吸收全部备忘；群聊写日记只吸收群聊备忘（私聊备忘继续等待），
    #   与日记注入的"私聊不进群聊"方向保持一致；
    # - 两个场景各存最多 20 条、互不占额度；无场景标记的旧备忘视为私聊来源。

    _MEMO_LIMIT = 20  # 每个场景的上限

    @staticmethod
    def _memo_scene(memo: dict) -> str:
        """备忘的场景：无标记的旧数据视为私聊（宁严勿宽）。"""
        s = memo.get("scene")
        return s if s in ("private", "group") else "private"

    def _load_memos(self, sender_id: str, scene: str = "") -> list:
        """读取备忘；scene 为 "private"/"group" 时按场景过滤（"" 表示全部）。"""
        memos = self._load_pref_data(sender_id).get("memos")
        if not isinstance(memos, list):
            return []
        memos = [m for m in memos if isinstance(m, dict) and m.get("text")]
        if scene in ("private", "group"):
            memos = [m for m in memos if self._memo_scene(m) == scene]
        return memos

    @staticmethod
    def _sanitize_closing_tags(text: str) -> str:
        """消毒闭合标签：防止日记/备忘文本里的 </memory_diary> 截断注入用 XML 块。"""
        return re.sub(
            r"</\s*memory_diary\s*>", "< /memory_diary>", text, flags=re.IGNORECASE
        )

    def _append_memo(self, sender_id: str, text: str, scene: str = "private") -> tuple:
        """追加一条备忘，返回 (该场景当前待写入条数, 是否保存成功)。"""
        data = self._load_pref_data(sender_id)
        memos = self._load_memos(sender_id)
        memos.append(
            {
                "time": self._now().strftime("%Y-%m-%d %H:%M"),
                "text": self._sanitize_closing_tags(text.strip()),
                "scene": scene if scene in ("private", "group") else "private",
            }
        )
        # 每个场景各保留最新 20 条，挤掉该场景最旧的，另一场景不受影响
        capped = []
        for sc in ("private", "group"):
            capped += [m for m in memos if self._memo_scene(m) == sc][
                -self._MEMO_LIMIT :
            ]
        data["memos"] = capped
        ok = self._save_pref_data(sender_id, data)
        total = len(
            [m for m in capped if self._memo_scene(m) == self._memo_scene(memos[-1])]
        )
        return total, ok

    def _clear_memos(
        self, sender_id: str, scenes: tuple = ("private", "group")
    ) -> bool:
        """清除指定场景的备忘（默认全部）。写日记只清已吸收进日记的场景。返回是否保存成功。"""
        data = self._load_pref_data(sender_id)
        memos = self._load_memos(sender_id)
        kept = [m for m in memos if self._memo_scene(m) not in scenes]
        if kept:
            data["memos"] = kept
        else:
            data.pop("memos", None)
        return self._save_pref_data(sender_id, data)

    def _select_entries(self, entries: list, scope: str) -> list:
        """按 scope 选择要注入的日期文件。"""
        if scope == "all":
            return list(entries)
        if scope == "latest":
            return [entries[-1]]
        if scope.startswith("days:"):
            try:
                n = max(1, int(scope[5:]))
            except ValueError:
                return [entries[-1]]
            cutoff = (self._now() - timedelta(days=n - 1)).strftime("%Y%m%d")
            sel = [e for e in entries if self._entry_day(e) >= cutoff]
            return sel or [entries[-1]]
        if scope.startswith("range:") and len(scope) == 6 + 8 + 1 + 8:
            start, end = scope[6:14], scope[15:23]
            if start > end:
                start, end = end, start
            return [e for e in entries if start <= self._entry_day(e) <= end]
        return [entries[-1]]

    _DATE_PAT = r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?"

    @staticmethod
    def _valid_day(y: int, m: int, d: int) -> bool:
        """真实日期校验：2026-02-31 这类不存在的日期直接判非法。"""
        try:
            datetime(y, m, d)
            return True
        except ValueError:
            return False

    @classmethod
    def _parse_scope(cls, arg: str):
        """把"最新/全部/最近N天/单日/时间段"解析成 scope 字符串；不合法返回 None。

        供 /日记范围 与 /注入日记 共用。（"默认"只用于 /日记范围，不在此解析。）
        """
        low = arg.lower().strip()
        if low in ("最新", "latest"):
            return "latest"
        if low in ("全部", "all"):
            return "all"
        m = re.match(r"^最近\s*(\d{1,3})\s*天$", arg.strip())
        if m:
            return f"days:{max(1, int(m.group(1)))}"
        m2 = re.match(
            rf"^{cls._DATE_PAT}\s*(?:到|至|~|--?|—)\s*{cls._DATE_PAT}$", arg.strip()
        )
        if m2:
            g = [int(x) for x in m2.groups()]
            if not (
                cls._valid_day(g[0], g[1], g[2]) and cls._valid_day(g[3], g[4], g[5])
            ):
                return None
            start = f"{g[0]:04d}{g[1]:02d}{g[2]:02d}"
            end = f"{g[3]:04d}{g[4]:02d}{g[5]:02d}"
            if start > end:
                start, end = end, start
            return f"range:{start}-{end}"
        m3 = re.match(rf"^{cls._DATE_PAT}$", arg.strip())
        if m3:
            y, mo, dd = (int(x) for x in m3.groups())
            if not cls._valid_day(y, mo, dd):
                return None
            day = f"{y:04d}{mo:02d}{dd:02d}"
            return f"range:{day}-{day}"  # 单日日期 = 起止同一天
        return None

    # 日记片段分隔条（如 "—— 当天 20:30 补记 · 写于群聊 ——"、"—— 14:05 · 写于私聊 ——"）
    _SEG_HEADER_RE = re.compile(r"^—— .+ ——$")

    def _filter_segments_for_group(self, content: str) -> str:
        """群聊注入过滤：只保留标注"写于群聊"的片段（含其分隔条）。

        没有场景标记的片段（旧版本写的）一律视为私聊来源，不注入群聊——宁可少注入，不泄露。
        """
        segments: list = []  # (header 或 None, [正文行])
        cur_header, cur_lines = None, []
        for line in content.split("\n"):
            if self._SEG_HEADER_RE.match(line.strip()):
                segments.append((cur_header, cur_lines))
                cur_header, cur_lines = line.strip(), []
            else:
                cur_lines.append(line)
        segments.append((cur_header, cur_lines))

        kept = []
        for header, lines in segments:
            if header and "写于群聊" in header:
                body = "\n".join(lines).strip("\n")
                kept.append(f"{header}\n\n{body}" if body.strip() else header)
        return "\n\n".join(kept)

    def _assemble_inject(
        self,
        sender_id: str,
        in_group: bool = False,
        scope_override: str = "",
        only_names: set = None,
        exclude_names: set = None,
    ):
        """按用户偏好/配置组装要注入的日记文本（带总量保险丝）。

        in_group=True 且开启"群聊注入过滤"时，只注入群聊场景写下的片段
        （私聊写的片段不进群聊；群聊写的片段在私聊中不受限）。
        scope_override：/注入日记 主动指定的范围，优先于用户偏好与默认。
        only_names：只从这些篇目中选（用于会话内去重）。
        exclude_names：从这些篇目中排除（持久模式下排除本轮已临时注入的篇目）。
        返回 (text, tampered_any, used_names)；text 为空表示没有可注入内容。
        """
        entries = self._list_entries(sender_id)
        if not entries:
            return "", False, []
        default_days = int(self.config.get("inject_default_days", 7) or 0)
        default_scope = "all" if default_days <= 0 else f"days:{default_days}"
        scope = scope_override or self._load_pref(sender_id) or default_scope
        targets = self._select_entries(entries, scope)
        if not targets:
            # P2-2：显式日期范围选不中任何篇目时严格为空、绝不回退最新；
            # 只有 days:N（默认/偏好）找不到时才回退最新一篇（刻意设计）
            if scope.startswith("range:"):
                return "", False, []
            targets = [entries[-1]]
        if only_names is not None:
            targets = [t for t in targets if t in only_names]
            if not targets:
                return "", False, []
        if exclude_names:
            targets = [t for t in targets if t not in exclude_names]
            if not targets:
                return "", False, []

        budget = int(self.config.get("inject_max_chars", 8000) or 8000)
        if budget <= 0:
            budget = 8000
        filter_group = in_group and bool(self.config.get("group_inject_filter", True))

        parts, used, truncated, tampered_any, filtered_any = [], 0, False, False, False
        used_names = []
        for name in reversed(targets):  # 新的优先占预算
            content, tampered = self._load_entry(sender_id, name)
            if tampered:
                tampered_any = True
                continue
            # 已被完整篇吸收的简记不再注入；原文仍保留在 zip 里可查看
            content = self._filter_absorbed_segments(content)
            if not content.strip():
                continue
            if filter_group:
                content = self._filter_segments_for_group(content)
                if not content.strip():
                    filtered_any = True  # 这一天全是私聊片段，整天跳过
                    continue
            block = f"【{self._entry_label(name)} 的日记】\n{content}"
            if used + len(block) > budget:
                marker = "【……该日开头因长度限制被截断】\n"
                remain = budget - used - len(marker)
                if remain > 200:  # 预算还够放一部分：截断这篇的头部，保住结尾的"叮嘱"
                    parts.append(marker + block[-remain:])
                    used_names.append(name)
                truncated = True
                break
            parts.append(block)
            used_names.append(name)
            used += len(block)
        parts.reverse()  # 恢复时间顺序（旧→新）
        used_names.reverse()

        if not parts:
            # P2-1：只有说明、没有正文时返回空串——避免占用"本会话已注入"名额，
            # 也避免群聊里只看到一句没头没尾的附注
            return "", tampered_any, []
        text = "\n\n".join(parts)
        if truncated:
            text += "\n\n（注：因注入长度上限，更早的日记未全部注入；可用 /日记范围 调整或 /查看日记 单独查阅。）"
        if tampered_any:
            text += "\n\n（注：部分日期的日记验签失败，已跳过。）"
        if filtered_any:
            text += "\n\n（注：私聊场景中写下的日记片段按规则未注入本次群聊。）"
        return text, tampered_any, used_names

    def _quarantine_corrupt(self, path: Path):
        """把损坏的日记 zip 改名隔离（不删除、不覆盖），返回备份路径或 None。"""
        ts = self._now().strftime("%Y%m%d_%H%M%S")
        backup = path.with_name(f"{path.stem}.corrupt-{ts}{path.suffix}")
        try:
            path.rename(backup)
            logger.error(
                f"[memory_diary] 日记文件损坏，已隔离为 {backup.name}，"
                "请管理员人工检查后决定恢复或删除"
            )
            return backup
        except Exception as e:
            logger.error(f"[memory_diary] 日记文件损坏且隔离失败（{path}）：{e}")
            return None

    @staticmethod
    def _write_zip_atomic(path: Path, entries: dict):
        """原子写 zip（P1-1 修复）：先写同目录临时文件并自验，再 os.replace 落位。

        "w" 模式会直接截断原文件——进程在写中央目录前被杀、磁盘满时，
        旧日记会整个损坏。原子写保证任何时刻磁盘上要么是完整的旧文件，
        要么是完整的新文件。
        """
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for n, b in entries.items():
                    z.writestr(n, b)
            # 落位前自验：读回中央目录并校验每个条目的 CRC
            with zipfile.ZipFile(tmp) as z:
                bad = z.testzip()
                if bad is not None:
                    raise zipfile.BadZipFile(f"写入后自检失败: {bad}")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)  # 已 os.replace 则不存在；失败时清理残骸

    def _save_diary(
        self,
        sender_id: str,
        content: str,
        sender_name: str = "",
        entry_name_hint: str = "",
        separator_note: str = "",
        origin: str = "",
        mode: str = "",
        absorb_headers: list = None,
    ):
        """把一篇日记写入"当天"的文件；当天已有内容则追加（带时间分隔）。返回文件名。

        origin 为 "private" / "group"（写这篇日记的会话场景），会标注在分隔条上，
        供"群聊注入过滤"识别；旧格式迁移时不传，保持无标记。
        mode 为 "full" / "brief" / "life"，写入分隔条供后续判断模式；
        absorb_headers 是本次完整篇吸收的简记分隔条，原文保留但标记（已吸收）。
        """
        path = self._diary_path(sender_id)
        day = entry_name_hint or self._now().strftime("%Y%m%d")
        entry_name = self._md_entry(day)
        scene = {"private": "私聊", "group": "群聊"}.get(origin, "")
        # 防止日记正文闭合注入用 XML 标签
        content = self._sanitize_closing_tags(content)

        # fail-closed（P2-6）：密钥不可用就不写，不写就无法验签的日记
        if self._secret_key() is None:
            raise DiaryKeyUnavailable("签名密钥不可用，已拒绝写入日记")

        # zip 不支持原地修改，整体重写（日记体量小，开销可忽略）
        existing: dict = {}
        if path.exists():
            try:
                with zipfile.ZipFile(path) as z:
                    names = set(z.namelist())
                    invalid = []
                    for n in sorted(names):
                        if not self.ENTRY_NAME_RE.match(n):
                            continue
                        text = z.read(n).decode("utf-8")
                        sig_name = self._sig_entry(n)
                        actual = (
                            z.read(sig_name).decode("utf-8", "ignore").strip()
                            if sig_name in names
                            else ""
                        )
                        # P1-2 修复：保存前逐条验签——被篡改/签名缺失的条目
                        # 绝不重新签名，整次保存拒绝，让用户先处理（恢复或删除）。
                        # 旧版主密钥直签的条目可过验，随后随整体重写自动升级为派生钥匙签名。
                        if not self._entry_sig_matches(sender_id, n, text, actual):
                            invalid.append(n)
                        else:
                            existing[n] = text
                    if invalid:
                        labels = "、".join(self._entry_label(n) for n in invalid)
                        raise DiaryTamperedError(labels)
            except (DiaryTamperedError, DiaryKeyUnavailable):
                raise
            except Exception as e:
                # P1-3 修复：zip 损坏（含 BadZipFile/CRC 错误）绝不静默覆盖——
                # 先改名隔离备份，再拒绝本次写入，等管理员人工确认
                backup = self._quarantine_corrupt(path)
                if backup:
                    raise DiaryCorruptError(
                        f"原文件已损坏（{e}），已隔离备份为 {backup.name}"
                    )
                raise DiaryCorruptError(
                    f"原文件已损坏（{e}），且隔离备份失败，原文件仍在原位，请先手动备份"
                )

        # 完整篇吸收同场景简记：先给旧简记的分隔条打上（已吸收），原文不删。
        if absorb_headers and entry_name in existing:
            existing[entry_name] = self._mark_absorbed(
                existing[entry_name], absorb_headers
            )

        mode_label = self._MODE_LABELS.get(mode, "")
        scene_tag = f" · 写于{scene}" if scene else ""
        if entry_name in existing:
            now_hm = self._now().strftime("%H:%M")
            if mode_label:
                header = (
                    f"—— 当天 {now_hm} {mode_label}{separator_note}{scene_tag} ——"
                )
            else:
                # 旧格式/迁移：保持原来的“补记”写法
                header = f"—— 当天 {now_hm} 补记{separator_note}{scene_tag} ——"
            existing[entry_name] = (
                existing[entry_name].rstrip()
                + f"\n\n{header}\n\n"
                + content
            )
        else:
            if scene:
                # 首篇也带模式/备注，便于后续判断完整篇与简记
                now_hm = self._now().strftime("%H:%M")
                if mode_label:
                    header = f"—— {now_hm} · {mode_label}{separator_note}{scene_tag} ——"
                else:
                    header = f"—— {now_hm}{separator_note}{scene_tag} ——"
                existing[entry_name] = f"{header}\n\n{content}"
            else:
                existing[entry_name] = content

        out: dict = {}
        for n in sorted(existing):
            out[n] = existing[n].encode("utf-8")
            out[self._sig_entry(n)] = self._entry_sig(sender_id, n, existing[n]).encode(
                "utf-8"
            )
        out[self.README_ZIP_ENTRY] = self._readme_text(sender_id, sender_name).encode(
            "utf-8"
        )
        self._write_zip_atomic(path, out)
        return entry_name

    def _migrate_legacy(self, sender_id: str):
        """把 v1.x 明文 / v2.0 单篇 zip / v3.0 按篇条目 迁移为按天归档。

        返回 "migrated" / "tampered" / None。
        """
        if self._secret_key() is None:
            logger.error("[memory_diary] 签名密钥不可用，无法验签，跳过旧格式迁移")
            return None
        path = self._diary_path(sender_id)

        if path.exists():
            try:
                with zipfile.ZipFile(path) as z:
                    data = {n: z.read(n) for n in z.namelist()}
            except zipfile.BadZipFile:
                return None
            except Exception as e:
                logger.warning(f"[memory_diary] 迁移读取失败: {e}")
                return None
            names = set(data)

            # v3.0 按篇 / v3.1~3.3 平铺按天 → 合并迁入 diaries/ + sigs/
            v3_entries = sorted(n for n in names if self._V3_ENTRY_RE.match(n))
            legacy_days = sorted(n for n in names if self._LEGACY_DAY_RE.match(n))
            if v3_entries or legacy_days:
                day_map: dict = {}  # day -> [(label, text)]
                for n in v3_entries:
                    text = data[n].decode("utf-8").strip()
                    actual = (
                        data.get(n[:-3] + ".sig", b"").decode("utf-8", "ignore").strip()
                    )
                    # 与读取路径一致：v0.4.0 之前的条目是主密钥直签，
                    # 验签必须走带回退的 _entry_sig_matches，否则旧日记会被误判成篡改
                    if not self._entry_sig_matches(sender_id, n, text, actual):
                        logger.warning(f"[memory_diary] 篇目 {n} 验签失败，迁移时跳过")
                        continue
                    day_map.setdefault(n[:8], []).append(
                        (f"{n[9:11]}:{n[11:13]}", text)
                    )
                for n in legacy_days:
                    text = data[n].decode("utf-8").strip()
                    actual = (
                        data.get(n[:-3] + ".sig", b"").decode("utf-8", "ignore").strip()
                    )
                    if not self._entry_sig_matches(sender_id, n, text, actual):
                        logger.warning(f"[memory_diary] 日记 {n} 验签失败，迁移时跳过")
                        continue
                    day_map.setdefault(n[:8], []).append(("", text))
                if not day_map:
                    return "tampered"
                # 已是新格式的内容（diaries/、sigs/）原样保留
                keep = {
                    n2: b
                    for n2, b in data.items()
                    if n2.startswith(("diaries/", "sigs/"))
                }
                try:
                    out = dict(keep)
                    for day in sorted(day_map):
                        items = day_map[day]
                        if len(items) == 1 and not items[0][0]:
                            merged = items[0][1]
                        else:
                            merged = "\n\n".join(
                                (f"—— 当天 {label} 记 ——\n\n{text}" if label else text)
                                for label, text in items
                            )
                        md_name = self._md_entry(day)
                        out[md_name] = merged.encode("utf-8")
                        out[self._sig_entry(md_name)] = self._entry_sig(
                            sender_id, md_name, merged
                        ).encode("utf-8")
                    out[self.README_ZIP_ENTRY] = self._readme_text(
                        sender_id, ""
                    ).encode("utf-8")
                    self._write_zip_atomic(path, out)
                except Exception as e:
                    logger.warning(f"[memory_diary] 迁移写入失败: {e}")
                    return None
                logger.info(
                    f"[memory_diary] 已将用户 {sender_id} 的旧版日记迁移为分文件夹按天归档"
                )
                return "migrated"

            # v2.0：zip 内是单个 diary.md + diary.sig
            if "diary.md" in names:
                content = data["diary.md"].decode("utf-8").strip()
                actual = data.get("diary.sig", b"").decode("utf-8", "ignore").strip()
                if not actual or not hmac.compare_digest(
                    self._signature(sender_id, content), actual
                ):
                    logger.warning(
                        f"[memory_diary] 用户 {sender_id} 的 v2 日记验签失败，拒绝迁移"
                    )
                    return "tampered"
                keep = {
                    n: b
                    for n, b in data.items()
                    if n not in ("diary.md", "diary.sig", self.README_ZIP_ENTRY)
                }
                day = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d")
                entry_name = self._md_entry(day)
                try:
                    out = dict(keep)
                    out[entry_name] = content.encode("utf-8")
                    out[self._sig_entry(entry_name)] = self._entry_sig(
                        sender_id, entry_name, content
                    ).encode("utf-8")
                    out[self.README_ZIP_ENTRY] = self._readme_text(
                        sender_id, ""
                    ).encode("utf-8")
                    self._write_zip_atomic(path, out)
                except Exception as e:
                    logger.warning(f"[memory_diary] 迁移写入失败: {e}")
                    return None
                logger.info(
                    f"[memory_diary] 已将用户 {sender_id} 的 v2 日记迁移为分文件夹按天归档"
                )
                return "migrated"

            return None  # 已是当前格式

        # v1.x：明文 .md + .sig
        md_path, sig_path = self._legacy_diary_paths(sender_id)
        if md_path.exists():
            try:
                content = md_path.read_text(encoding="utf-8").strip()
                actual = (
                    sig_path.read_text(encoding="utf-8").strip()
                    if sig_path.exists()
                    else ""
                )
            except Exception as e:
                logger.warning(f"[memory_diary] 读取旧版日记失败: {e}")
                return None
            if not actual or not hmac.compare_digest(
                self._signature(sender_id, content), actual
            ):
                logger.warning(
                    f"[memory_diary] 用户 {sender_id} 的 v1 日记验签失败，拒绝迁移"
                )
                return "tampered"
            try:
                day = datetime.fromtimestamp(md_path.stat().st_mtime).strftime("%Y%m%d")
                self._save_diary(sender_id, content, entry_name_hint=day)
                md_path.unlink(missing_ok=True)
                sig_path.unlink(missing_ok=True)
                logger.info(
                    f"[memory_diary] 已将用户 {sender_id} 的 v1 日记迁移为按天 zip"
                )
                return "migrated"
            except Exception as e:
                logger.warning(f"[memory_diary] 迁移旧版日记失败: {e}")
        return None

    def _delete_diary(self, sender_id: str) -> bool:
        existed = False
        for p in (self._diary_path(sender_id), *self._legacy_diary_paths(sender_id)):
            try:
                if p.exists():
                    p.unlink()
                    existed = True
            except Exception as e:
                logger.warning(f"[memory_diary] 删除文件失败（{p}）: {e}")
        return existed

    def _delete_entries(self, sender_id: str, names: list) -> int:
        """删除指定日期文件（含签名）。全删光时移除整个 zip。返回实际删除的天数。"""
        path = self._diary_path(sender_id)
        if not path.exists():
            return 0
        try:
            with zipfile.ZipFile(path) as z:
                data = {n: z.read(n) for n in z.namelist()}
        except Exception as e:
            logger.warning(f"[memory_diary] 读取日记 zip 失败: {e}")
            return 0

        remove = set()
        for n in names:
            if n in data:
                remove.add(n)
                remove.add(self._sig_entry(n))
        if not remove:
            return 0

        remaining_days = [
            n for n in data if self.ENTRY_NAME_RE.match(n) and n not in remove
        ]
        try:
            if not remaining_days:
                path.unlink(missing_ok=True)  # 一天都不剩 → 整个 zip 删掉
            else:
                out = {n: b for n, b in data.items() if n not in remove}
                self._write_zip_atomic(path, out)
        except Exception as e:
            logger.warning(f"[memory_diary] 删除日记条目失败: {e}")
            return 0
        return len([n for n in remove if n.endswith(".md")])

    # ------------------------- 操作冷却 -------------------------

    def _op_cooldown_remaining(self, sender_id: str) -> int:
        """返回还需等待的分钟数；0 表示可以操作。间隔为 0 时不限制。"""
        interval = int(self.config.get("diary_op_interval", 5) or 0)
        if interval <= 0:
            return 0
        interval = min(interval, 60)
        last = self._op_last.get(sender_id)
        if last is None:
            return 0
        elapsed = (self._now() - last).total_seconds()
        remaining_sec = interval * 60 - elapsed
        if remaining_sec <= 0:
            return 0
        return int((remaining_sec + 59) // 60)  # 向上取整，提示更直观

    def _op_mark(self, sender_id: str):
        self._op_last[sender_id] = self._now()
        self._cap_state(self._op_last)
    def _import_readme_text(self, diary_name: str) -> str:
        return (
            "记忆日记 · 迁移包导入说明\n"
            "========================\n\n"
            "【包里有什么】\n"
            f"1. {diary_name} —— 你的日记本体（含每篇的防篡改签名）\n"
            "2. user_key.txt —— 只属于你这份日记的签名钥匙。它由服务器主密钥按你的\n"
            "   QQ 号派生，只能验/改你这一份日记，动不了别人的，也反推不出主密钥。\n"
            "3. manifest.json —— 导出信息清单，仅供核对。\n\n"
            "【如何导入到新设备（推荐）】\n"
            "1. 在新设备的 AstrBot 上装好本插件并启动一次。\n"
            "2. 用同一个 QQ 号和 bot 私聊：先发 /导入日记，再把这个 zip 整个\n"
            "   （不用解压）作为文件发给它。插件会逐篇验签，确认没被动过才安放。\n"
            "3. 发 /查看日记 验证。\n"
            "也可以在 AstrBot 管理面板的「日记本」页面点「导入日记」上传本包。\n\n"
            "【注意】\n"
            "· 不要把本包解压后拖进插件数据目录——v2 包不按那种方式收养。\n"
            "· 本包仍含你的签名钥匙，拿到它的人能伪造你这份日记的签名，请勿转发。\n"
            "· 必须在两台设备上使用同一个 QQ 号与 bot 对话——签名绑定了你的 QQ 号。\n"
        )
    def _verify_bundle(self, bundle_path: Path):
        """解析并校验迁移包。返回 (主人QQ, 该用户的派生钥匙bytes, {条目名: 内容})；
        不可信即抛 ValueError。

        两种格式：
        - memory-diary-export/2（现行）：user_key.txt 是主人这把派生钥匙（hex），
          日记签名直接用它验；
        - memory-diary-export/1（旧版）：diary_key.txt 是源设备的主密钥，
          日记签名用它直签验，导入时换算成主人的派生钥匙再继续。
        """
        with zipfile.ZipFile(bundle_path) as z:
            bad = z.testzip()
            if bad is not None:
                raise ValueError(f"迁移包有损坏（条目 {bad} CRC 校验失败）")
            names = set(z.namelist())
            if "manifest.json" not in names:
                raise ValueError("这不是本插件的迁移包（缺少 manifest.json）")
            try:
                manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            except Exception:
                raise ValueError("迁移包的 manifest.json 无法解析")
            fmt = manifest.get("format")
            if fmt not in ("memory-diary-export/1", "memory-diary-export/2"):
                raise ValueError("迁移包格式版本不受支持")
            owner_id = str(manifest.get("sender_id") or "").strip()
            diary_name = str(manifest.get("diary_file") or "").strip()
            if not owner_id or not diary_name or diary_name not in names:
                raise ValueError("迁移包清单不完整，或包内缺少日记文件")
            key_entry = "user_key.txt" if fmt.endswith("/2") else "diary_key.txt"
            if key_entry not in names:
                raise ValueError(f"迁移包缺少 {key_entry}，可能被改动过")
            key_text = z.read(key_entry).decode("utf-8").strip()
            if not key_text:
                raise ValueError(f"迁移包中的 {key_entry} 为空")
            diary_bytes = z.read(diary_name)

        if fmt.endswith("/2"):
            try:
                sign_key = bytes.fromhex(key_text)
            except ValueError:
                raise ValueError("迁移包中的 user_key.txt 格式不对，可能被改动过")
            if not sign_key:
                raise ValueError("迁移包中的 user_key.txt 为空")
            user_key = sign_key
        else:
            sign_key = key_text.encode("utf-8")
            # 旧版包带的是源设备主密钥：换算成主人的派生钥匙供安放阶段比较
            user_key = hmac.new(
                sign_key, f"user-key:{owner_id}".encode("utf-8"), hashlib.sha256
            ).digest()

        entries = {}
        with zipfile.ZipFile(io.BytesIO(diary_bytes)) as dz:
            bad = dz.testzip()
            if bad is not None:
                raise ValueError(f"迁移包里的日记文件有损坏（条目 {bad} CRC 校验失败）")
            dnames = set(dz.namelist())
            for n in sorted(dnames):
                if not self.ENTRY_NAME_RE.match(n):
                    continue
                sig_name = self._sig_entry(n)
                if sig_name not in dnames:
                    raise ValueError(f"日记 {self._entry_label(n)} 缺少签名，迁移包可能被改动过")
                content = dz.read(n).decode("utf-8").strip()
                actual = dz.read(sig_name).decode("utf-8").strip()
                expected = hmac.new(
                    sign_key,
                    (owner_id + "\n" + n + "\n" + content).encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                if not hmac.compare_digest(expected, actual):
                    raise ValueError(
                        f"日记 {self._entry_label(n)} 验签失败，迁移包可能被改动过"
                    )
                entries[n] = content
        if not entries:
            raise ValueError("迁移包里没有任何日记")
        return owner_id, user_key, entries

    def _install_bundle(self, sender_id: str, owner_id: str, user_key: bytes,
                        entries: dict, sender_name: str = "",
                        check_owner: bool = True) -> str:
        """把验签通过的迁移包安放到位。返回模式：same_key / resign（仅作提示用）。

        现行机制不再收养主密钥：日记一律用本机派生的主人钥匙重新签名。
        包内钥匙与本机派生一致（same_key）时重签结果与原签名逐字节相同；
        不一致（resign）时换成本机钥匙的签名，本机其他用户的日记不受影响。

        check_owner：QQ 指令侧必须为 True（装进别人账号的日记永远验不过签）；
        Web 日记本页面是管理员操作，安装后仍绑定原主人 QQ，传 False。
        """
        if check_owner and owner_id != sender_id:
            raise ValueError(
                "这份日记的主人 QQ 号和你不一致——签名绑定了原主人的 QQ 号，"
                "装进你的账号也验不过，已拒绝导入。"
            )
        local_uk = self._user_sign_key(owner_id)
        if local_uk is None:
            raise ValueError(
                "本机签名密钥不可用，无法安放日记——请管理员检查插件数据目录。"
            )
        mode = (
            "same_key" if hmac.compare_digest(local_uk, user_key) else "resign"
        )

        target = self._diary_path(sender_id)
        if target.exists():
            raise ValueError(
                "本机已经存在你的日记文件——为避免覆盖，已中止导入。"
                "请先备份并移走数据目录里的旧日记 zip，再重新导入。"
            )

        out = {self.README_ZIP_ENTRY: self._readme_text(sender_id, sender_name)}
        for n, content in entries.items():
            out[n] = content
            # 一律用本机派生钥匙签名：same_key 时与原签名相同，resign 时换签
            out[self._sig_entry(n)] = self._entry_sig(sender_id, n, content)
        self._write_zip_atomic(target, out)
        return mode

    def _zip_owner_info(self, path: Path):
        """从 zip 内 说明.txt 解析（主人QQ, 昵称）；缺失返回空串。"""
        owner, nick = "", ""
        try:
            with zipfile.ZipFile(path) as z:
                txt = z.read(self.README_ZIP_ENTRY).decode("utf-8", "replace")
            m = re.search(r"^用户ID：(\S+)\s*$", txt, re.M)
            if m:
                owner = m.group(1)
            m = re.search(r"^用户昵称：(.*)$", txt, re.M)
            if m:
                nick = m.group(1).strip()
        except Exception:
            pass
        return owner, nick

    def _owner_map_path(self) -> Path:
        return self.data_dir / "web_owner_map.json"

    def _load_owner_map(self) -> dict:
        try:
            d = json.loads(self._owner_map_path().read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _remember_owner(self, file_key: str, owner_id: str):
        try:
            d = self._load_owner_map()
            d[file_key] = owner_id
            p = self._owner_map_path()
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception as e:
            logger.warning(f"[memory_diary] 保存 Web 侧主人映射失败: {e}")

    def _resolve_zip_owner(self, path: Path, file_key: str, qq_hint: str = ""):
        """确定 zip 的主人 QQ。返回 (owner_id, 错误信息)；无法确定且未给 qq 时
        错误信息为 "OWNER_UNKNOWN"，由页面引导管理员输入。"""
        owner, _ = self._zip_owner_info(path)
        if owner:
            return owner, ""
        owner = self._load_owner_map().get(file_key, "")
        if owner:
            return owner, ""
        if not qq_hint:
            return "", "OWNER_UNKNOWN"
        # 用候选 QQ 全量验签，通过才接受——输错 QQ 就是验不过，不存在侥幸
        key = self._secret_key()
        if key is None:
            return "", "签名密钥不可用"
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                entries = sorted(n for n in names if self.ENTRY_NAME_RE.match(n))
                if not entries:
                    return "", "这个 zip 里没有日记"
                for n in entries:
                    if self._sig_entry(n) not in names:
                        return "", f"日记 {self._entry_label(n)} 缺少签名"
                    content = z.read(n).decode("utf-8").strip()
                    actual = z.read(self._sig_entry(n)).decode("utf-8").strip()
                    if not self._entry_sig_matches(qq_hint, n, content, actual):
                        return "", "QQ 号与日记签名对不上，请确认是日记主人的 QQ"
        except Exception as e:
            return "", f"校验失败：{e}"
        self._remember_owner(file_key, qq_hint)
        return qq_hint, ""

    # ---- 查看密码（仅挡 Web 日记本页面；服务器文件权限不在保护范围内） ----
    # 主人在 QQ 私聊 /日记密码 设置；哈希存进本人 pref 文件，明文不落盘。
    _PW_ALGO = "pbkdf2-sha256"
    _PW_ITER = 100_000

    @classmethod
    def _make_view_password(cls, password: str) -> dict:
        salt = secrets.token_hex(16)
        h = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt), cls._PW_ITER,
        ).hex()
        return {"algo": cls._PW_ALGO, "iter": cls._PW_ITER, "salt": salt, "hash": h}

    @staticmethod
    def _verify_view_password(rec: dict, password: str) -> bool:
        try:
            h = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"),
                bytes.fromhex(rec["salt"]), int(rec["iter"]),
            ).hex()
            return hmac.compare_digest(h, str(rec["hash"]))
        except Exception:
            return False

    def _view_password_rec(self, owner_id: str):
        rec = self._load_pref_data(owner_id).get("view_password")
        return rec if isinstance(rec, dict) and rec.get("hash") else None

    def _build_export_bundle(self, sender_id: str, diary_path: Path,
                             entries: list) -> Path:
        """打包迁移包：日记 zip + 主人的派生钥匙 + manifest + 导入说明。
        返回临时包路径，由调用方负责删除。"""
        uk = self._user_sign_key(sender_id)
        if uk is None:
            raise DiaryKeyUnavailable("签名密钥不可用")
        stamp = self._now().strftime("%Y%m%d_%H%M%S")
        export_path = (
            self.data_dir
            / f"memory_diary_export_{stamp}_{secrets.token_hex(2)}.zip"
        )
        manifest = {
            "format": "memory-diary-export/2",
            "key_kind": "user",
            "sender_id": sender_id,
            "diary_file": diary_path.name,
            "entry_count": len(entries),
            "exported_at": self._now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(diary_path, diary_path.name)
            z.writestr("user_key.txt", uk.hex())
            z.writestr(
                "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
            )
            z.writestr("导入说明.txt", self._import_readme_text(diary_path.name))
        return export_path

