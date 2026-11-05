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

from .common import safe_key, sniff_image_ext, strip_invisible
from .constants import IMAGE_MAX_BYTES, TOKEN_CALIBRATION_FILE
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
    #   ├── images/20261031_1_窗边的黄昏.png   插图（v0.6，日记 zip 的一等公民）
    #   ├── sigs/images_20261031_1_窗边的黄昏.png.sig  插图二进制的 HMAC 签名
    #   └── 说明.txt               日记结构与使用说明
    # 主密钥 .diary_key 在 zip 外（同目录），绝不入包——否则篡改者可自行重签。
    # 新上下文默认自动读最近 7 天的文件（可配）；更早的日期可按需查询。
    # 管理员可直接把对应用户的 zip 发给本人；插件读写 zip 对模型完全透明。

    README_ZIP_ENTRY = "说明.txt"
    ENTRY_NAME_RE = re.compile(r"^diaries/\d{8}\.md$")  # 当前格式
    _LEGACY_DAY_RE = re.compile(r"^\d{8}\.md$")  # v3.1~3.3 平铺按天，仅迁移用
    _V3_ENTRY_RE = re.compile(r"^\d{8}_\d{6}(_\d+)?\.md$")  # v3.0 按篇命名，仅迁移用

    # ---- 插图条目（v0.6）----
    # 文件名允许中文（设计示例 "20261031_1_窗边的黄昏.png"），但禁止路径分隔符、
    # 控制字符与 ".."；名字只由程序生成（LLM 描述先 sanitize），见 save_image()。
    IMAGE_NAME_RE = re.compile(
        r"^images/[^/\\\x00-\x1f\x7f]+\.(?:png|jpg|jpeg|webp|gif)$", re.IGNORECASE
    )
    IMAGE_SIG_RE = re.compile(r"^sigs/images_.+\.sig$")
    IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "gif")
    # 用户照片的 alt 前缀（注入时替换成 [用户分享的照片：…]，见 _assemble_inject）
    USER_PHOTO_ALT_PREFIX = "用户分享的照片："

    # ---- 批注侧车（v0.6，D7）----
    # 批注是读者留言，独立于日记正文：侧车文件 annotations.json 与正文一样纳入
    # 签名体系（文件整体 HMAC），防"改批注不告警"。文件放在日记 zip 内，
    # 导出迁移包时随包天然带走；旧 zip 没有这个条目时按"空批注"优雅兼容。
    # 验签三态与正文同口径：
    #   · 条目缺失          → missing（旧数据，不告警，视为空）
    #   · 签名缺失/不匹配   → tampered（与正文篡改同级：不展示、不重签、写入拒绝）
    #   · 主密钥不可用      → no_key（验不了 = 不可信）
    ANNOTATIONS_ENTRY = "annotations.json"
    ANNOTATIONS_SIG_ENTRY = "sigs/annotations.json.sig"
    ANNOTATION_STATUSES = ("open", "resolved", "deferred")
    # 容量上限：长期运行批注只增不减会拖慢注入与展示，超出后先丢最旧的已解批注
    ANNOTATIONS_MAX_RECORDS = 500

    # ---- 来源侧车（写作时剥离的 <self> 自发挥原文）----
    # 写作模型把"自己发挥/虚构的拟人内容"用 <self>…</self> 标出，保存前插件
    # 剥掉标签、把原文子串记进本侧车；批注生成时用它把这些片段从"可锚文本"
    # 里剔除，防止批注落在 AI 自编内容上。签名/搬运/清理纪律与批注侧车一致：
    # 整体 HMAC、写日记时原样搬运、删日记时同步清理；旧 zip 没有它时视为空。
    PROVENANCE_ENTRY = "provenance.json"
    PROVENANCE_SIG_ENTRY = "sigs/provenance.json.sig"
    PROVENANCE_MAX_SELF_PER_ENTRY = 50    # 单篇最多记多少条自发挥子串
    PROVENANCE_MAX_SELF_CHARS = 2000      # 单篇自发挥子串的总字数上限

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

    # ------------------------- 插图（v0.6，D1） -------------------------
    # 设计第 6 节：图片是日记 zip 的一等公民。图片存 images/，签名存
    # sigs/images_<文件名>.sig —— 签名域 = 用户 + 条目名 + 二进制，
    # 补上"正文引用行有签名、图片二进制没签名"的真空地带：
    #   · 验签不匹配 → tampered：按篡改处理，与正文篡改同级；
    #   · 条目缺失   → missing：优雅降级，正文照常读，插图位显示 alt 占位；
    #   · 正常       → ok。
    # 命名规则（设计 6.2）：日期 + 当日序号 + AI 描述（sanitize 后 ≤20 字），
    # 撞名时用内容哈希前 6 位兜底。

    @staticmethod
    def _image_basename(name: str) -> str:
        """images/xxx.png 或 xxx.png -> xxx.png（只保留一级文件名）。"""
        raw = str(name or "").strip()
        if raw.startswith("images/"):
            raw = raw[len("images/"):]
        return raw

    @classmethod
    def _image_entry(cls, name: str) -> str:
        return f"images/{cls._image_basename(name)}"

    @classmethod
    def _image_sig_entry(cls, name: str) -> str:
        """images/20261031_1_窗边的黄昏.png
        -> sigs/images_20261031_1_窗边的黄昏.png.sig"""
        return f"sigs/images_{cls._image_basename(name)}.sig"

    @classmethod
    def _valid_image_name(cls, name: str) -> bool:
        """图片条目名白名单校验（防路径穿越/奇怪的扩展名）。"""
        entry = cls._image_entry(name)
        if ".." in entry or "\\" in entry:
            return False
        if len(entry) > 160:
            return False
        return bool(cls.IMAGE_NAME_RE.match(entry))

    @classmethod
    def _sanitize_image_desc(cls, raw: str, limit: int = 20) -> str:
        """把 LLM 给的画面描述消毒成安全文件名片段（设计 6.2/隐私红线 3）。

        去路径分隔符、".."、控制/不可见字符、markdown 方括号，空白折成 "_"，
        截断到 limit 字；空了回退"插图"。
        """
        text = strip_invisible(str(raw or ""))
        text = re.sub(r"[\x00-\x1f\x7f]", "", text)
        # 方括号/路径分隔符 + URL 保留字符（# % & +）：文件名要能原样放进
        # Web 图片路由的路径段里，避免个别图名在 bridge 里被截断/解码失败。
        for ch in (
            "\\", "/", "|", "｜", "[", "]", "(", ")", "<", ">", ":",
            "*", "?", '"', "#", "%", "&", "+",
        ):
            text = text.replace(ch, "")
        text = text.replace("..", "")
        text = re.sub(r"\s+", "_", text.strip())
        text = text.strip("._-")
        text = text[: max(1, int(limit))]
        return text or "插图"

    def _image_sig(self, sender_id: str, name: str, data: bytes) -> str:
        """图片签名：HMAC-SHA256(用户派生钥匙, 用户 + 条目名 + 二进制)。"""
        uk = self._user_sign_key(sender_id)
        if uk is None:
            raise DiaryKeyUnavailable("签名密钥不可用")
        entry = self._image_entry(name)
        return hmac.new(
            uk,
            sender_id.encode("utf-8")
            + b"\n"
            + entry.encode("utf-8")
            + b"\n"
            + bytes(data),
            hashlib.sha256,
        ).hexdigest()

    def _image_sig_matches(self, sender_id: str, name: str, data: bytes,
                           actual: str) -> bool:
        """图片验签；actual 为空视为不匹配（签名缺失 = 篡改）。"""
        if not actual:
            return False
        try:
            expected = self._image_sig(sender_id, name, data)
        except DiaryKeyUnavailable:
            return False
        return hmac.compare_digest(expected, str(actual).strip())

    def list_images(self, sender_id: str) -> list:
        """列出该用户 zip 内全部 images/ 条目（升序）。"""
        path = self._diary_path(sender_id)
        if not path.exists():
            return []
        try:
            with zipfile.ZipFile(path) as z:
                return sorted(n for n in z.namelist() if self.IMAGE_NAME_RE.match(n))
        except zipfile.BadZipFile:
            logger.error(
                f"[memory_diary] 日记 zip 已损坏（{path}），无法列出插图"
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 列出插图失败: {e}")
        return []

    def save_image(self, sender_id: str, data: bytes, day: str, seq: int,
                   desc: str, ext: str = "") -> str:
        """把一张图写进日记 zip 的 images/ 并写入签名，返回 zip 内条目名。

        append 模式落盘（设计 6.1：O(1)，不重写整包）。调用方负责保证
        日记正文已经先落盘（正文引用行与图片条目同包）；这里只追加。
        day/seq 由调用方给（日期 + 当日序号），desc 是 LLM 画面描述，
        必须经过 _sanitize_image_desc；撞名用内容哈希前 6 位兜底。
        """
        data = bytes(data or b"")
        if not data:
            raise ValueError("图片内容为空")
        if len(data) > IMAGE_MAX_BYTES:
            raise ValueError(f"图片超过 {IMAGE_MAX_BYTES // (1024 * 1024)}MB 上限")
        day = re.sub(r"\D", "", str(day or ""))[:8]
        if len(day) != 8:
            day = self._now().strftime("%Y%m%d")
        try:
            seq = max(1, int(seq))
        except (TypeError, ValueError):
            seq = 1
        ext = str(ext or "").lower().lstrip(".")
        if ext not in self.IMAGE_EXTS:
            ext = sniff_image_ext(data)
        desc = self._sanitize_image_desc(desc)
        if self._secret_key() is None:
            raise DiaryKeyUnavailable("签名密钥不可用，已拒绝写入插图")

        path = self._diary_path(sender_id)
        base = f"{day}_{seq}_{desc}"
        name = f"images/{base}.{ext}"
        existing = set(self.list_images(sender_id))
        if name in existing:
            digest = hashlib.sha256(data).hexdigest()[:6]
            name = f"images/{base}_{digest}.{ext}"
            n = 2
            while name in existing:  # 极端防御：同一天同描述同内容的重复写入
                name = f"images/{base}_{digest}_{n}.{ext}"
                n += 1
        if not path.exists():
            # 正常流程不会走到（写日记时已建包）；防御性建一个带说明的空包
            self._write_zip_atomic(
                path,
                {self.README_ZIP_ENTRY: self._readme_text(sender_id, "").encode("utf-8")},
            )
        with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as z:
            z.writestr(self._image_entry(name), data)
            z.writestr(
                self._image_sig_entry(name),
                self._image_sig(sender_id, name, data).encode("utf-8"),
            )
        logger.info(
            f"[memory_diary] 用户 {sender_id} 的插图已写入 zip：{name}"
            f"（{len(data)} 字节）"
        )
        return name

    def load_image(self, sender_id: str, name: str):
        """读取一张插图并验签。返回 (data 或 None, status)。

        status:
        - "ok"       正常；
        - "missing"  条目不存在（优雅降级，正文照常读）；
        - "tampered" 签名缺失/不匹配（按篡改处理）；
        - "no_key"   主密钥不可用（验签做不了）。
        读取时 CRC 损坏也按 missing 降级（图片是装饰，不能连累正文）。
        """
        name = self._image_entry(name)
        if not self._valid_image_name(name):
            return None, "missing"
        path = self._diary_path(sender_id)
        if not path.exists():
            return None, "missing"
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if name not in names:
                    return None, "missing"
                data = z.read(name)
                sig_name = self._image_sig_entry(name)
                actual = (
                    z.read(sig_name).decode("utf-8", "ignore").strip()
                    if sig_name in names
                    else ""
                )
        except Exception as e:
            logger.error(f"[memory_diary] 读取插图失败（{name}）: {e}")
            return None, "missing"
        if self._secret_key() is None:
            logger.error(f"[memory_diary] 签名密钥不可用，无法验签插图 {name}")
            return None, "no_key"
        if not self._image_sig_matches(sender_id, name, data, actual):
            logger.warning(
                f"[memory_diary] 用户 {sender_id} 的插图 {name} 验签失败，已拒绝显示"
            )
            return None, "tampered"
        return data, "ok"

    # 兼容别名：调用方（main.py/服务层）用 _load_image 或 load_image 均可
    _load_image = load_image

    def _delete_images(self, sender_id: str, day: str) -> int:
        """删除某天日记关联的插图（按文件名前缀 "<day>_" 识别），返回删除张数。"""
        day = re.sub(r"\D", "", str(day or ""))[:8]
        path = self._diary_path(sender_id)
        if not path.exists() or len(day) != 8:
            return 0
        try:
            with zipfile.ZipFile(path) as z:
                data = {n: z.read(n) for n in z.namelist()}
        except Exception as e:
            logger.warning(f"[memory_diary] 读取日记 zip 失败，插图未清理: {e}")
            return 0
        remove = set()
        for n in data:
            if self.IMAGE_NAME_RE.match(n):
                base = n.split("/")[-1]
                if base.startswith(f"{day}_"):
                    remove.add(n)
                    remove.add(self._image_sig_entry(n))
        if not remove:
            return 0
        try:
            self._write_zip_atomic(
                path, {n: b for n, b in data.items() if n not in remove}
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 清理 {day} 的插图失败: {e}")
            return 0
        return len([n for n in remove if n.startswith("images/")])

    def replace_pending_images(self, sender_id: str, entry_name: str,
                               replacements: dict, remove: list) -> bool:
        """把某天日记里的 PENDING 占位引用替换成最终图片引用 / 删除失败占位。

        异步生图的落点：日记先落盘（占位），图到了以后由这里改写同一篇。
        改写前先按现有签名验该条目，验不过就不动（绝不替被改过的正文重签）。
        返回是否写入成功。
        """
        path = self._diary_path(sender_id)
        if not path.exists():
            return False
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if entry_name not in names:
                    return False
                raw = z.read(entry_name).decode("utf-8")
                sig_name = self._sig_entry(entry_name)
                actual = (
                    z.read(sig_name).decode("utf-8", "ignore").strip()
                    if sig_name in names
                    else ""
                )
                data = {n: z.read(n) for n in names}
        except Exception as e:
            logger.warning(f"[memory_diary] 读取日记失败，占位未替换: {e}")
            return False
        if self._secret_key() is None:
            logger.error("[memory_diary] 签名密钥不可用，占位未替换")
            return False
        if not self._entry_sig_matches(sender_id, entry_name, raw, actual):
            logger.warning(
                f"[memory_diary] {entry_name} 验签失败，拒绝替换插图占位（疑似被改过）"
            )
            return False
        content = raw
        for old, new in (replacements or {}).items():
            if old:
                content = content.replace(str(old), str(new))
        # 失败清理：只删除占位标记本身——解析是"原位替换"，AI 可能把标记写在
        # 句子中间，按整行删除会把整句正文吞掉（审查 P1-1）。删完只剩空白的行才整行去掉。
        for marker in remove or []:
            if not marker:
                continue
            marker = str(marker)
            new_lines = []
            for line in content.split("\n"):
                if marker not in line:
                    new_lines.append(line)
                    continue
                remainder = line.replace(marker, "")
                if remainder.strip():
                    new_lines.append(remainder)
                # 否则该行只有占位符：整行删掉
            content = "\n".join(new_lines)
        # 删行后可能留下连续空行：折成最多两个（保持段落分隔）
        content = re.sub(r"[ \t]+\n", "\n", content)
        content = re.sub(r"\n{3,}", "\n\n", content).strip("\n")
        if content == raw:
            return True
        data[entry_name] = content.encode("utf-8")
        data[self._sig_entry(entry_name)] = self._entry_sig(
            sender_id, entry_name, content
        ).encode("utf-8")
        try:
            self._write_zip_atomic(path, data)
        except Exception as e:
            logger.warning(f"[memory_diary] 插图占位替换写盘失败: {e}")
            return False
        return True

    @classmethod
    def _image_macro_to_placeholder(cls, content: str) -> str:
        """把日记正文里的图片语法换成注入占位符（设计 6.4）。

        - 用户照片：![用户分享的照片：描述](images/...) → [用户分享的照片：描述]
        - AI 插图：![描述](images/...)              → [插图：描述]
        图片绝不以 base64 进上下文；PENDING 占位引用按同样规则替换。
        """
        if not content or "![" not in content:
            return content

        def _user_photo(m):
            alt = m.group(1).strip()
            prefix = cls.USER_PHOTO_ALT_PREFIX
            desc = alt[len(prefix):].strip() if alt.startswith(prefix) else alt
            return f"[{prefix}{desc or '内容未识别'}]"

        content = re.sub(
            r"!\[(用户分享的照片[:：][^\]]*)\]\(images/[^)\s]*\)",
            _user_photo,
            content,
        )
        return re.sub(
            r"!\[([^\]]*)\]\(images/[^)\s]*\)",
            lambda m: "[插图：" + (m.group(1).strip() or "一幅画") + "]",
            content,
        )

    # ------------------------- 批注侧车（v0.6，D7） -------------------------
    # 批注独立成文件、不污染日记 md；文件整体 HMAC 签名，读写前验签。
    # 数据形状见《批注功能接口契约》第 3 节；这里只做存储，不做业务规则。

    @classmethod
    def _annotations_empty(cls) -> dict:
        return {
            "version": 1,
            "last_round": "",
            "last_title": "",
            "updated_at": "",
            "annotations": [],
        }

    @staticmethod
    def _dump_annotations(data: dict) -> bytes:
        """统一序列化（保存/安装/重签共用）：同一个 dict 得到逐字节一致的 JSON。"""
        return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    @classmethod
    def _trim_annotations(cls, items: list) -> list:
        """超出容量上限时先丢最旧的已解批注，再丢最旧的其余条目（保留最新）。"""
        cap = cls.ANNOTATIONS_MAX_RECORDS
        if len(items) <= cap:
            return list(items)
        excess = len(items) - cap
        drop = set()
        resolved = sorted(
            (i for i, x in enumerate(items) if str(x.get("status") or "") == "resolved"),
            key=lambda i: (str(items[i].get("created_at") or ""), i),
        )
        drop.update(resolved[:excess])
        if len(drop) < excess:
            rest = [i for i in range(len(items)) if i not in drop]
            rest.sort(key=lambda i: (str(items[i].get("created_at") or ""), i))
            drop.update(rest[: excess - len(drop)])
        return [x for i, x in enumerate(items) if i not in drop]

    @classmethod
    def _normalize_annotations(cls, data) -> dict:
        """把任意来源的数据清洗成规范 dict（不改入参），坏条目丢弃、状态非法回退 open。"""
        out = cls._annotations_empty()
        if not isinstance(data, dict):
            return out
        out["last_round"] = str(data.get("last_round") or "")
        out["last_title"] = str(data.get("last_title") or "")[:50]
        out["updated_at"] = str(data.get("updated_at") or "")
        items = data.get("annotations")
        if not isinstance(items, list):
            items = []
        normalized = []
        for item in items:
            if not isinstance(item, dict):
                continue
            anchor = str(item.get("anchor") or "").strip()
            note = str(item.get("note") or "").strip()
            if not anchor or not note:
                continue
            status = str(item.get("status") or "open")
            if status not in cls.ANNOTATION_STATUSES:
                status = "open"
            reply = item.get("reply")
            if not isinstance(reply, dict):
                reply = None
            raw_question = item.get("is_question")
            if isinstance(raw_question, str):
                is_question = raw_question.strip().lower() in (
                    "1",
                    "true",
                    "yes",
                    "y",
                )
            else:
                is_question = bool(raw_question)
            normalized.append(
                {
                    "id": str(item.get("id") or ""),
                    "round": str(item.get("round") or ""),
                    "anchor": anchor,
                    "entry": str(item.get("entry") or ""),
                    "created_at": str(item.get("created_at") or ""),
                    "note": note,
                    "is_question": is_question,
                    "status": status,
                    "reply": reply,
                }
            )
        out["annotations"] = cls._trim_annotations(normalized)
        return out

    def _annotations_sig(self, sender_id: str, raw: bytes) -> str:
        """批注文件签名：HMAC-SHA256(用户派生钥匙, 用户 + 条目名 + 文件原始字节)。"""
        uk = self._user_sign_key(sender_id)
        if uk is None:
            raise DiaryKeyUnavailable("签名密钥不可用")
        return hmac.new(
            uk,
            sender_id.encode("utf-8")
            + b"\n"
            + self.ANNOTATIONS_ENTRY.encode("utf-8")
            + b"\n"
            + bytes(raw),
            hashlib.sha256,
        ).hexdigest()

    def _annotations_sig_matches(self, sender_id: str, raw: bytes,
                                 actual: str) -> bool:
        """验签；actual 为空视为不匹配（签名缺失 = 篡改）。"""
        if not actual:
            return False
        try:
            expected = self._annotations_sig(sender_id, raw)
        except DiaryKeyUnavailable:
            return False
        return hmac.compare_digest(expected, str(actual).strip())

    def _read_annotations_entry(self, sender_id: str):
        """从 zip 读出批注原始字节与签名。返回 (raw 或 None, actual, 错误标记)。

        错误标记 "" 成功（raw 可能为 None 表示条目缺失）；"tampered" 读失败。
        """
        path = self._diary_path(sender_id)
        if not path.exists():
            return None, "", ""
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if self.ANNOTATIONS_ENTRY not in names:
                    return None, "", ""
                raw = z.read(self.ANNOTATIONS_ENTRY)
                actual = (
                    z.read(self.ANNOTATIONS_SIG_ENTRY).decode("utf-8", "ignore").strip()
                    if self.ANNOTATIONS_SIG_ENTRY in names
                    else ""
                )
                return raw, actual, ""
        except Exception as e:
            logger.error(f"[memory_diary] 读取批注文件失败: {e}")
            return None, "", "tampered"

    def load_annotations(self, sender_id: str):
        """读取批注侧车。返回 (规范 dict, status)，status ∈ ok/missing/tampered/no_key。"""
        raw, actual, err = self._read_annotations_entry(sender_id)
        if err:
            return self._annotations_empty(), "tampered"
        if raw is None:
            return self._annotations_empty(), "missing"
        if self._secret_key() is None:
            logger.error("[memory_diary] 签名密钥不可用，无法验签批注")
            return self._annotations_empty(), "no_key"
        if not self._annotations_sig_matches(sender_id, raw, actual):
            logger.warning(
                f"[memory_diary] 用户 {sender_id} 的批注文件验签失败，已拒绝读取"
            )
            return self._annotations_empty(), "tampered"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            logger.error(f"[memory_diary] 批注文件无法解析（已按篡改处理）: {e}")
            return self._annotations_empty(), "tampered"
        return self._normalize_annotations(data), "ok"

    def annotations_status(self, sender_id: str) -> str:
        """只验签不解析：返回 ok/missing/tampered/no_key（供指令层给提示）。"""
        raw, actual, err = self._read_annotations_entry(sender_id)
        if err:
            return "tampered"
        if raw is None:
            return "missing"
        if self._secret_key() is None:
            return "no_key"
        if not self._annotations_sig_matches(sender_id, raw, actual):
            return "tampered"
        try:
            json.loads(raw.decode("utf-8"))
        except Exception:
            return "tampered"
        return "ok"

    def save_annotations(self, sender_id: str, data: dict) -> bool:
        """写入批注侧车（原子重写 zip，保留其它全部条目）。

        fail-closed：旧批注文件验签失败时**拒绝写入**，绝不替被改过的批注重签。
        """
        if self._secret_key() is None:
            logger.error("[memory_diary] 签名密钥不可用，已拒绝写入批注")
            return False
        path = self._diary_path(sender_id)
        if path.exists():
            try:
                with zipfile.ZipFile(path) as z:
                    entries = {n: z.read(n) for n in z.namelist()}
            except Exception as e:
                logger.warning(f"[memory_diary] 读取日记 zip 失败，批注未写入: {e}")
                return False
            if self.ANNOTATIONS_ENTRY in entries:
                old_raw = entries[self.ANNOTATIONS_ENTRY]
                old_actual = (
                    entries.get(self.ANNOTATIONS_SIG_ENTRY, b"")
                    .decode("utf-8", "ignore")
                    .strip()
                )
                if not self._annotations_sig_matches(
                    sender_id, old_raw, old_actual
                ):
                    logger.warning(
                        f"[memory_diary] 用户 {sender_id} 的批注文件验签失败，"
                        "已拒绝覆盖写入"
                    )
                    return False
                # P1-3：签名正确但 JSON 已损坏，同样按 tampered 处理、拒绝覆盖。
                # 与读取路径 load_annotations 的三态口径保持一致（fail-closed），
                # 绝不把损坏内容当作"可重签"的旧数据冲掉。
                try:
                    json.loads(old_raw.decode("utf-8"))
                except Exception:
                    logger.warning(
                        f"[memory_diary] 用户 {sender_id} 的批注文件无法解析"
                        "（疑似损坏），已拒绝覆盖写入"
                    )
                    return False
        else:
            # 正常流程不会走到（批注只写在已有日记之后）；防御性建空包
            entries = {
                self.README_ZIP_ENTRY: self._readme_text(sender_id, "").encode("utf-8")
            }
        normalized = self._normalize_annotations(data)
        raw = self._dump_annotations(normalized)
        entries[self.ANNOTATIONS_ENTRY] = raw
        entries[self.ANNOTATIONS_SIG_ENTRY] = self._annotations_sig(
            sender_id, raw
        ).encode("utf-8")
        try:
            self._write_zip_atomic(path, entries)
        except Exception as e:
            logger.warning(f"[memory_diary] 批注写入失败: {e}")
            return False
        return True

    def update_annotations(self, sender_id: str, mutator):
        """读-验-改-写。mutator(data) 可就地修改（返回 dict 时替换）。返回 (成功, 规范数据)。"""
        data, status = self.load_annotations(sender_id)
        if status not in ("ok", "missing"):
            return False, self._annotations_empty()
        try:
            result = mutator(data)
        except Exception as e:
            logger.warning(f"[memory_diary] 修改批注失败: {e}")
            return False, self._annotations_empty()
        if isinstance(result, dict):
            data = result
        if not self.save_annotations(sender_id, data):
            return False, self._annotations_empty()
        return True, self._normalize_annotations(data)

    # ------------------------- 来源侧车（<self> 自发挥原文） -------------------------

    @classmethod
    def _provenance_empty(cls) -> dict:
        return {"version": 1, "updated_at": "", "entries": {}}

    @staticmethod
    def _dump_provenance(data: dict) -> bytes:
        """统一序列化（保存/重签共用）：同一个 dict 得到逐字节一致的 JSON。"""
        return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    @classmethod
    def _normalize_provenance(cls, data) -> dict:
        """清洗成规范 dict（不改入参）：键是 "YYYYMMDD.md"，值含 self 子串列表。

        容量双保险：单篇条数与总字数超限都从最旧的开始丢（列表按写入先后追加）。
        """
        out = cls._provenance_empty()
        if not isinstance(data, dict):
            return out
        out["updated_at"] = str(data.get("updated_at") or "")[:30]
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return out
        for key, rec in entries.items():
            name = str(key or "").strip()
            if not re.fullmatch(r"\d{8}\.md", name):
                continue
            subs = rec.get("self") if isinstance(rec, dict) else None
            if not isinstance(subs, list):
                continue
            cleaned, seen, total = [], set(), 0
            for s in subs:
                if not isinstance(s, str):
                    continue
                s = s.strip()
                if not s or s in seen:
                    continue
                if len(cleaned) >= cls.PROVENANCE_MAX_SELF_PER_ENTRY:
                    break
                if total + len(s) > cls.PROVENANCE_MAX_SELF_CHARS:
                    continue
                cleaned.append(s)
                seen.add(s)
                total += len(s)
            if cleaned:
                out["entries"][name] = {"self": cleaned}
        return out

    def _provenance_sig(self, sender_id: str, raw: bytes) -> str:
        """来源侧车签名：HMAC-SHA256(用户派生钥匙, 用户 + 条目名 + 文件原始字节)。"""
        uk = self._user_sign_key(sender_id)
        if uk is None:
            raise DiaryKeyUnavailable("签名密钥不可用")
        return hmac.new(
            uk,
            sender_id.encode("utf-8")
            + b"\n"
            + self.PROVENANCE_ENTRY.encode("utf-8")
            + b"\n"
            + bytes(raw),
            hashlib.sha256,
        ).hexdigest()

    def _provenance_sig_matches(self, sender_id: str, raw: bytes,
                                actual: str) -> bool:
        """验签；actual 为空视为不匹配（签名缺失 = 篡改）。"""
        if not actual:
            return False
        try:
            expected = self._provenance_sig(sender_id, raw)
        except DiaryKeyUnavailable:
            return False
        return hmac.compare_digest(expected, str(actual).strip())

    def _read_provenance_entry(self, sender_id: str):
        """从 zip 读出来源侧车原始字节与签名。返回 (raw 或 None, actual, 错误标记)。"""
        path = self._diary_path(sender_id)
        if not path.exists():
            return None, "", ""
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if self.PROVENANCE_ENTRY not in names:
                    return None, "", ""
                raw = z.read(self.PROVENANCE_ENTRY)
                actual = (
                    z.read(self.PROVENANCE_SIG_ENTRY).decode("utf-8", "ignore").strip()
                    if self.PROVENANCE_SIG_ENTRY in names
                    else ""
                )
                return raw, actual, ""
        except Exception as e:
            logger.error(f"[memory_diary] 读取来源侧车失败: {e}")
            return None, "", "tampered"

    def load_provenance(self, sender_id: str):
        """读取来源侧车。返回 (规范 dict, status)，status ∈ ok/missing/tampered/no_key。"""
        raw, actual, err = self._read_provenance_entry(sender_id)
        if err:
            return self._provenance_empty(), "tampered"
        if raw is None:
            return self._provenance_empty(), "missing"
        if self._secret_key() is None:
            logger.error("[memory_diary] 签名密钥不可用，无法验签来源侧车")
            return self._provenance_empty(), "no_key"
        if not self._provenance_sig_matches(sender_id, raw, actual):
            logger.warning(
                f"[memory_diary] 用户 {sender_id} 的来源侧车验签失败，已拒绝读取"
            )
            return self._provenance_empty(), "tampered"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            logger.error(f"[memory_diary] 来源侧车无法解析（已按篡改处理）: {e}")
            return self._provenance_empty(), "tampered"
        return self._normalize_provenance(data), "ok"

    def save_provenance(self, sender_id: str, data: dict) -> bool:
        """写入来源侧车（原子重写 zip，保留其它全部条目）。

        fail-closed：旧文件验签失败时**拒绝写入**，绝不替被改过的内容重签。
        """
        if self._secret_key() is None:
            logger.error("[memory_diary] 签名密钥不可用，已拒绝写入来源侧车")
            return False
        path = self._diary_path(sender_id)
        if path.exists():
            try:
                with zipfile.ZipFile(path) as z:
                    entries = {n: z.read(n) for n in z.namelist()}
            except Exception as e:
                logger.warning(f"[memory_diary] 读取日记 zip 失败，来源侧车未写入: {e}")
                return False
            if self.PROVENANCE_ENTRY in entries:
                old_raw = entries[self.PROVENANCE_ENTRY]
                old_actual = (
                    entries.get(self.PROVENANCE_SIG_ENTRY, b"")
                    .decode("utf-8", "ignore")
                    .strip()
                )
                if not self._provenance_sig_matches(sender_id, old_raw, old_actual):
                    logger.warning(
                        f"[memory_diary] 用户 {sender_id} 的来源侧车验签失败，"
                        "已拒绝覆盖写入"
                    )
                    return False
                # 与批注侧车同口径：签名正确但 JSON 已损坏，同样拒绝覆盖
                try:
                    json.loads(old_raw.decode("utf-8"))
                except Exception:
                    logger.warning(
                        f"[memory_diary] 用户 {sender_id} 的来源侧车无法解析"
                        "（疑似损坏），已拒绝覆盖写入"
                    )
                    return False
        else:
            entries = {
                self.README_ZIP_ENTRY: self._readme_text(sender_id, "").encode("utf-8")
            }
        normalized = self._normalize_provenance(data)
        raw = self._dump_provenance(normalized)
        entries[self.PROVENANCE_ENTRY] = raw
        entries[self.PROVENANCE_SIG_ENTRY] = self._provenance_sig(
            sender_id, raw
        ).encode("utf-8")
        try:
            self._write_zip_atomic(path, entries)
        except Exception as e:
            logger.warning(f"[memory_diary] 来源侧车写入失败: {e}")
            return False
        return True

    def update_provenance(self, sender_id: str, mutator):
        """读-验-改-写。mutator(data) 可就地修改（返回 dict 时替换）。返回 (成功, 规范数据)。"""
        data, status = self.load_provenance(sender_id)
        if status not in ("ok", "missing"):
            return False, self._provenance_empty()
        try:
            result = mutator(data)
        except Exception as e:
            logger.warning(f"[memory_diary] 修改来源侧车失败: {e}")
            return False, self._provenance_empty()
        if isinstance(result, dict):
            data = result
        if not self.save_provenance(sender_id, data):
            return False, self._provenance_empty()
        return True, self._normalize_provenance(data)

    def record_self_provenance(self, sender_id: str, day: str, spans: list) -> bool:
        """把一篇日记的自发挥子串并入该日的来源记录（读-验-改-写）。

        同一天多篇日记多次调用时增量追加、自动去重；任何失败都只返回 False，
        由调用方记日志——绝不因为来源标记影响日记写入本身。
        """
        name = f"{str(day or '').strip()[:8]}.md"
        spans = [str(s).strip() for s in (spans or []) if str(s or "").strip()]
        if not re.fullmatch(r"\d{8}\.md", name) or not spans:
            return False

        def _mut(data: dict):
            rec = data["entries"].setdefault(name, {"self": []})
            have = set(rec.get("self") or [])
            for s in spans:
                if s not in have:
                    rec["self"].append(s)
                    have.add(s)
            data["updated_at"] = self._now().strftime("%Y-%m-%dT%H:%M:%S")
            return data

        ok, _ = self.update_provenance(sender_id, _mut)
        return ok

    def _prune_provenance_entries(self, sender_id: str, entries: dict,
                                  days: set) -> int:
        """在 zip 条目 dict 里就地删除 entry[:8] ∈ days 的来源记录，返回删除条数。

        验签失败/密钥不可用时不改（fail-closed，与批注清理同口径）。
        """
        days = {str(d).strip()[:8] for d in (days or set()) if str(d).strip()}
        if not days or self.PROVENANCE_ENTRY not in entries:
            return 0
        raw = entries[self.PROVENANCE_ENTRY]
        actual = (
            entries.get(self.PROVENANCE_SIG_ENTRY, b"")
            .decode("utf-8", "ignore")
            .strip()
        )
        if self._secret_key() is None or not self._provenance_sig_matches(
            sender_id, raw, actual
        ):
            logger.warning("[memory_diary] 来源侧车验签失败，删除日记时未清理")
            return 0
        try:
            data = self._normalize_provenance(json.loads(raw.decode("utf-8")))
        except Exception:
            return 0
        kept = {
            k: v for k, v in data["entries"].items() if str(k)[:8] not in days
        }
        removed = len(data["entries"]) - len(kept)
        if not removed:
            return 0
        data["entries"] = kept
        data["updated_at"] = self._now().strftime("%Y-%m-%dT%H:%M:%S")
        out_raw = self._dump_provenance(data)
        entries[self.PROVENANCE_ENTRY] = out_raw
        entries[self.PROVENANCE_SIG_ENTRY] = self._provenance_sig(
            sender_id, out_raw
        ).encode("utf-8")
        return removed

    def _prune_annotations_entries(self, sender_id: str, entries: dict,
                                   days: set) -> int:
        """在 zip 条目 dict 里就地删除 entry[:8] ∈ days 的批注，返回删除条数。

        验签失败/密钥不可用时不改（fail-closed，交给读取层告警），避免替被改过的
        批注重签。删空时连 annotations.json 与其签名一起移除。
        """
        days = {str(d).strip()[:8] for d in (days or set()) if str(d).strip()}
        if not days or self.ANNOTATIONS_ENTRY not in entries:
            return 0
        raw = entries[self.ANNOTATIONS_ENTRY]
        actual = (
            entries.get(self.ANNOTATIONS_SIG_ENTRY, b"")
            .decode("utf-8", "ignore")
            .strip()
        )
        if self._secret_key() is None or not self._annotations_sig_matches(
            sender_id, raw, actual
        ):
            logger.warning("[memory_diary] 批注验签失败，删除日记时未清理批注")
            return 0
        try:
            data = self._normalize_annotations(json.loads(raw.decode("utf-8")))
        except Exception:
            return 0
        kept = [
            x
            for x in data["annotations"]
            if str(x.get("entry") or "")[:8] not in days
        ]
        removed = len(data["annotations"]) - len(kept)
        if not removed:
            return 0
        if kept:
            data["annotations"] = kept
            data["updated_at"] = self._now().strftime("%Y-%m-%dT%H:%M:%S")
            out_raw = self._dump_annotations(data)
            entries[self.ANNOTATIONS_ENTRY] = out_raw
            entries[self.ANNOTATIONS_SIG_ENTRY] = self._annotations_sig(
                sender_id, out_raw
            ).encode("utf-8")
        else:
            entries.pop(self.ANNOTATIONS_ENTRY, None)
            entries.pop(self.ANNOTATIONS_SIG_ENTRY, None)
        return removed

    def _delete_annotations_for_days(self, sender_id: str, days: set) -> int:
        """删除锚定在指定日期的批注，返回删除条数。"""
        days = {str(d).strip()[:8] for d in (days or set()) if str(d).strip()}
        path = self._diary_path(sender_id)
        if not days or not path.exists():
            return 0
        try:
            with zipfile.ZipFile(path) as z:
                entries = {n: z.read(n) for n in z.namelist()}
        except Exception as e:
            logger.warning(f"[memory_diary] 读取日记 zip 失败，批注未清理: {e}")
            return 0
        removed = self._prune_annotations_entries(sender_id, entries, days)
        removed += self._prune_provenance_entries(sender_id, entries, days)
        if not removed:
            return 0
        try:
            self._write_zip_atomic(path, entries)
        except Exception as e:
            logger.warning(f"[memory_diary] 清理批注失败: {e}")
            return 0
        return removed

    def annotations_for_day(self, sender_id: str, day: str) -> tuple:
        """返回 (某天批注列表[按 created_at 升序], status)。status 非 ok 时列表为空。"""
        data, status = self.load_annotations(sender_id)
        if status != "ok":
            return [], status
        day = str(day or "").strip()[:8]
        items = [
            dict(x)
            for x in data["annotations"]
            if str(x.get("entry") or "")[:8] == day
        ]
        items.sort(key=lambda x: (str(x.get("created_at") or ""), str(x.get("id") or "")))
        return items, status

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
            "功能上依次覆盖：时间范围 / 用户画像 / 对用户的评价与感受 / 给未来的 AI 的叮嘱"
            "（小标题由 AI 按人设自由起名）。\n"
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
        """读取一篇日记。返回 (content, tampered)。

        验签必须用 zip 里的**原文**：签名是在保存时对落盘原文算的
        （_save_diary 保存前逐条验签也传原文），strip 只是展示需要。
        若先 strip 再验签，将来任何写入路径引入行尾空白，读路径就会把全部
        合法日记误判成「被篡改」（fail-closed 误伤）——所以这里先验原文、
        返回前再 strip。
        """
        path = self._diary_path(sender_id)
        try:
            with zipfile.ZipFile(path) as z:
                raw = z.read(entry_name).decode("utf-8")
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
        if not self._entry_sig_matches(sender_id, entry_name, raw, actual):
            logger.warning(
                f"[memory_diary] 用户 {sender_id} 的日记 {entry_name} 验签失败，已拒绝读取"
            )
            return "", True
        return raw.strip(), False

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
        """读取用户偏好文件（含注入范围、素材游标、备忘等）。"""
        try:
            data = json.loads(self._pref_path(sender_id).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_pref_data(self, sender_id: str, data: dict) -> bool:
        """保存偏好数据；返回是否成功——失败时调用方必须告知用户，不能谎报（P2-5）。

        原子写（P1-1）：先写临时文件再 os.replace，避免 write_text 原地截断
        导致一次崩溃丢掉范围设置/备忘/素材游标。
        注意：会**就地**删除传入 dict 里的废弃字段（last_material_hash），
        调用方传入的都应是本函数域内新建/新加载的 dict，不要复用它。
        """
        try:
            p = self._pref_path(sender_id)
            # 已废弃字段（素材哈希机制已被素材游标取代）：旧 pref 文件里可能还留着，
            # 在这里统一清掉——否则每次「读-改-写」都会把它原样带回，永远删不掉。
            data.pop("last_material_hash", None)
            if data:
                tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
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

    # 素材哈希（last_material_hash）已随「素材游标」上线废弃：游标记录上次读到
    # 历史第几条/最后一条哈希，判断“有没有新内容”不再读整段素材哈希。旧数据里的
    # 该字段随偏好文件自然重写消失，这里不再读写。

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
            # 插图占位符化（设计 6.4）：图片语法换成一行文本，绝不 base64 进上下文。
            # 放在预算计算之前：占位符比 markdown 图片语法短，按占位后长度计预算。
            content = self._image_macro_to_placeholder(content)
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
        """把损坏的日记 zip 改名隔离（不删除、不覆盖），返回备份路径或 None。

        隔离副本名形如 diary_<hash>.corrupt-<ts>.zip，不匹配 diary_*.zip 的
        有效 key 规则，因此不会进入 Web 一键备份（P3-17，可接受）。
        """
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
        # 临时名带 pid + 随机后缀：同进程两个协程并发写同一 zip 时，
        # 固定 tmp 名会互相截断/覆盖（os.replace 后内容来源不定），导致日记丢失
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
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
        # 插图与图片签名原样搬运：_save_diary 只重写日记条目，绝不能把插图冲掉
        # （设计 6.1：图片是日记 zip 的一等公民；图片签名不重签，字节原样保留）
        image_entries: dict = {}
        # 批注/来源侧车同样原样搬运：写日记不能把已有的页边批注与来源记录冲掉。
        # 也不在这里解析/重签——验签交给各自的读取路径。
        sidecar_entries: dict = {}
        if path.exists():
            try:
                with zipfile.ZipFile(path) as z:
                    names = set(z.namelist())
                    invalid = []
                    for n in sorted(names):
                        if n in (
                            self.ANNOTATIONS_ENTRY,
                            self.ANNOTATIONS_SIG_ENTRY,
                            self.PROVENANCE_ENTRY,
                            self.PROVENANCE_SIG_ENTRY,
                        ):
                            sidecar_entries[n] = z.read(n)
                            continue
                        if self.IMAGE_NAME_RE.match(n):
                            image_entries[n] = z.read(n)
                            sig_name = self._image_sig_entry(n)
                            if sig_name in names:
                                image_entries[sig_name] = z.read(sig_name)
                            continue
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
        out.update(image_entries)
        out.update(sidecar_entries)
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
                # 已是新格式的内容（diaries/、images/、批注侧车、sigs/）原样保留
                keep = {
                    n2: b
                    for n2, b in data.items()
                    if n2.startswith(("diaries/", "images/", "sigs/"))
                    or n2 == self.ANNOTATIONS_ENTRY
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
        deleted_days = set()
        for n in names:
            if n in data:
                remove.add(n)
                remove.add(self._sig_entry(n))
                deleted_days.add(self._entry_day(n))
        if not remove:
            return 0
        # 插图和对应日期绑定（文件名词缀 "<日期>_"）：删某天的日记时一并清掉，
        # 不留孤儿图片占空间；注意"全部删除"走 _delete_diary（整包删除）。
        if deleted_days:
            for n in data:
                if self.IMAGE_NAME_RE.match(n) and n.split("/")[-1][:8] in deleted_days:
                    remove.add(n)
                    remove.add(self._image_sig_entry(n))

        # 批注级联：删某天日记时一并清掉锚定该天的批注（与插图同理，不留孤儿批注）。
        # 与日记删除在同一次原子写入里完成，避免"日记删了、批注没删"的两步状态。
        if deleted_days:
            self._prune_annotations_entries(sender_id, data, deleted_days)
            # 来源级联：该天的 <self> 来源记录一并清理（与批注同纪律）
            self._prune_provenance_entries(sender_id, data, deleted_days)

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
        """返回还需等待的分钟数；0 表示可以操作。间隔为 0 时不限制。

        ``_op_last`` 里存的是 ``_now()`` 的结果：配置了合法时区时是 tz-aware，
        未配置/非法时回退 naive。AstrBot 改配置会重载插件、实例重建，正常路径
        不会混存；但万一同一实例内动态改了 diary_timezone，aware 与 naive 相减
        会抛 TypeError，表现为日记指令整个报错。这里按“无冷却”兜底（宁可放行
        一次，也不让指令崩掉）。
        """
        interval = int(self.config.get("diary_op_interval", 5) or 0)
        if interval <= 0:
            return 0
        interval = min(interval, 60)
        last = self._op_last.get(sender_id)
        if last is None:
            return 0
        try:
            elapsed = (self._now() - last).total_seconds()
        except (TypeError, ValueError):
            logger.warning(
                "[memory_diary] 操作冷却时间类型不一致（aware/naive 混存），本次按无冷却处理"
            )
            return 0
        remaining_sec = interval * 60 - elapsed
        if remaining_sec <= 0:
            return 0
        return int((remaining_sec + 59) // 60)  # 向上取整，提示更直观

    def _op_mark(self, sender_id: str):
        self._op_last[sender_id] = self._now()
        self._cap_state(self._op_last)
    def _backup_readme_text(self, user_count: int) -> str:
        """Web 一键备份包内的说明（只含日记 zip，绝不含签名钥匙）。

        P3-17：备份只收集 `diary_*.zip`（调用方用 `_valid_file_key` 过滤
        16 位十六进制文件名），因此 `_quarantine_corrupt` 产生的
        `*.corrupt-<ts>.zip` 隔离副本不会入包——这是可接受行为：那些是损坏
        文件的残骸，迁移时不带走；管理员按需自行拷贝到新机人工修复。
        """
        return (
            "记忆日记 · 全部用户日记备份\n"
            "============================\n\n"
            f"备份时间：{self._now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"用户份数：{user_count} 份（每份一个 diary_*.zip，"
            "含该用户全部日记与防篡改签名）\n\n"
            "【这是什么】\n"
            "这是管理员从 Web 日记本页面一键导出的整机备份，"
            "包含服务器上所有用户的日记。\n"
            "包里【不含】任何签名钥匙——这是安全设计：主密钥能签发所有人的日记，"
            "泄露面远大于日记内容，浏览器下载的包比服务器目录更易外泄。\n\n"
            "【如何恢复】\n"
            "· 整机迁移：把本包里的 diary_*.zip 全部放进新机的插件数据目录，"
            "并且必须把旧机数据目录里的主密钥 .diary_key 一起拷过去。\n"
            "  每个用户的签名钥匙由主密钥按 QQ 号确定性派生（HMAC-SHA256）——"
            "主密钥在，所有派生钥匙自动重算，旧日记验签照常通过。\n"
            "· 没有主密钥的兜底：备份包里的日记在新机上会全部验签失败，"
            "此时让各用户走单用户 /导出日记 迁移包导入，"
            "导入逻辑会自动用本机钥匙重签安放，验签恢复。\n"
            "两条通道的分工：一键备份 = 管理员整机迁移（必须配 .diary_key 一起迁移）；\n"
            "单用户迁移包 = 用户级自助迁移（自动重签）。\n"
            "详见 README 第 11 节「日记存储位置与提取」。\n"
        )

    def _import_readme_text(self, diary_name: str) -> str:        return (
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
        """解析并校验迁移包。返回
        (主人QQ, 该用户的派生钥匙bytes, {日记条目名: 内容}, {图片条目名: bytes},
         批注规范 dict 或 None)；
        不可信即抛 ValueError。旧包没有 images/ 时第 4 项为 {}；
        旧包没有 annotations.json 时第 5 项为 None（向后兼容）。

        两种格式：
        - memory-diary-export/2（现行）：user_key.txt 是主人这把派生钥匙（hex），
          日记签名直接用它验；
        - memory-diary-export/1（旧版）：diary_key.txt 是源设备的主密钥，
          日记签名用它直签验，导入时换算成主人的派生钥匙再继续。

        v0.6 起图片也逐张验签（设计 6.1：图片跟随日记 zip 全程迁移，CRC 自动覆盖；
        这里再加 HMAC 防"换图不换文"）；批注文件同样整体验签（设计第二部分第 4 节）。
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
        images = {}
        annotations = None
        with zipfile.ZipFile(io.BytesIO(diary_bytes)) as dz:
            bad = dz.testzip()
            if bad is not None:
                raise ValueError(f"迁移包里的日记文件有损坏（条目 {bad} CRC 校验失败）")
            dnames = set(dz.namelist())
            # 批注侧车：包内有就必须有签名且验签通过，否则按篡改拒绝整个迁移包
            if self.ANNOTATIONS_ENTRY in dnames:
                raw = dz.read(self.ANNOTATIONS_ENTRY)
                if self.ANNOTATIONS_SIG_ENTRY not in dnames:
                    raise ValueError("批注文件缺少签名，迁移包可能被改动过")
                actual = (
                    dz.read(self.ANNOTATIONS_SIG_ENTRY)
                    .decode("utf-8", "ignore")
                    .strip()
                )
                expected = hmac.new(
                    sign_key,
                    owner_id.encode("utf-8")
                    + b"\n"
                    + self.ANNOTATIONS_ENTRY.encode("utf-8")
                    + b"\n"
                    + raw,
                    hashlib.sha256,
                ).hexdigest()
                if not hmac.compare_digest(expected, actual):
                    raise ValueError("批注文件验签失败，迁移包可能被改动过")
                try:
                    annotations = self._normalize_annotations(
                        json.loads(raw.decode("utf-8"))
                    )
                except Exception:
                    raise ValueError("批注文件无法解析，迁移包可能被改动过")
            for n in sorted(dnames):
                if n in (self.ANNOTATIONS_ENTRY, self.ANNOTATIONS_SIG_ENTRY):
                    continue
                if self.IMAGE_NAME_RE.match(n):
                    sig_name = self._image_sig_entry(n)
                    if sig_name not in dnames:
                        raise ValueError(
                            f"插图 {n.split('/')[-1]} 缺少签名，迁移包可能被改动过"
                        )
                    data = dz.read(n)
                    actual = dz.read(sig_name).decode("utf-8").strip()
                    expected = hmac.new(
                        sign_key,
                        owner_id.encode("utf-8")
                        + b"\n"
                        + n.encode("utf-8")
                        + b"\n"
                        + data,
                        hashlib.sha256,
                    ).hexdigest()
                    if not hmac.compare_digest(expected, actual):
                        raise ValueError(
                            f"插图 {n.split('/')[-1]} 验签失败，迁移包可能被改动过"
                        )
                    images[n] = data
                    continue
                if not self.ENTRY_NAME_RE.match(n):
                    continue
                sig_name = self._sig_entry(n)
                if sig_name not in dnames:
                    raise ValueError(f"日记 {self._entry_label(n)} 缺少签名，迁移包可能被改动过")
                # 验签用 zip 内原文：迁移包里的日记 zip 是逐字节副本，签名也是
                # 对落盘原文算的（与 _load_entry / _save_diary 同口径）。strip 只
                # 用于展示；若这里先 strip 再验签，带行尾空白的日记会"能读不能迁"。
                content = dz.read(n).decode("utf-8")
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
        return owner_id, user_key, entries, images, annotations

    def _install_bundle(self, sender_id: str, owner_id: str, user_key: bytes,
                        entries: dict, sender_name: str = "",
                        check_owner: bool = True, images: dict = None,
                        annotations: dict = None) -> str:
        """把验签通过的迁移包安放到位。返回模式：same_key / resign（仅作提示用）。

        现行机制不再收养主密钥：日记一律用本机派生的主人钥匙重新签名。
        包内钥匙与本机派生一致（same_key）时重签结果与原签名逐字节相同；
        不一致（resign）时换成本机钥匙的签名，本机其他用户的日记不受影响。
        images：v0.6 起迁移包里的插图 {条目名: bytes}，安装时同样用本机钥匙重签。
        annotations：v0.6 起迁移包里的批注侧车（规范 dict，可为 None），
        安装时用本机钥匙重新签名。

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
        for n, data in (images or {}).items():
            if not self._valid_image_name(n):
                continue
            name = self._image_entry(n)
            out[name] = data
            out[self._image_sig_entry(name)] = self._image_sig(sender_id, name, data)
        if annotations:
            ann_raw = self._dump_annotations(
                self._normalize_annotations(annotations)
            )
            out[self.ANNOTATIONS_ENTRY] = ann_raw
            out[self.ANNOTATIONS_SIG_ENTRY] = self._annotations_sig(
                sender_id, ann_raw
            ).encode("utf-8")
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
            tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
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
                    # 与读取/保存/迁移包校验同口径：用原文验签，strip 不参与验签
                    raw = z.read(n).decode("utf-8")
                    actual = z.read(self._sig_entry(n)).decode("utf-8").strip()
                    if not self._entry_sig_matches(qq_hint, n, raw, actual):
                        return "", "QQ 号与日记签名对不上，请确认是日记主人的 QQ"
        except Exception as e:
            return "", f"校验失败：{e}"
        self._remember_owner(file_key, qq_hint)
        return qq_hint, ""

    # ------------------------- token 估算校准（EWMA 系数持久化） -------------------------
    # 文件：数据目录下 token_calibration.json
    # 键：provider::model；值：{"factor", "samples", "last_actual", "updated"}
    # 只存数字系数，不存任何聊天内容（隐私）。
    # P3-18：key 数量上限 64——正常最多几十个 provider::model，超出说明文件被
    # 污染或长期异常膨胀；加载/写回时按 updated 只保留最近 64 条
    #（缺少 updated 的视为最旧，优先丢弃），避免内存与 /日记状态 被撑爆。
    _TOKEN_CALIB_MAX_KEYS = 64

    def _token_calibration_path(self) -> Path:
        return self.data_dir / TOKEN_CALIBRATION_FILE

    @classmethod
    def _trim_token_calibration(cls, data: dict) -> dict:
        """按 updated 降序保留最近 _TOKEN_CALIB_MAX_KEYS 个 key；未超限原样返回。

        updated 是 "%Y-%m-%d %H:%M:%S" 字符串，字典序即时间序；空串/缺失排最旧。
        隔离成纯函数，加载与写回共用同一规则（_save_token_calibration 的签名不变）。
        """
        if not isinstance(data, dict) or len(data) <= cls._TOKEN_CALIB_MAX_KEYS:
            return data

        def _updated(item) -> str:
            rec = item[1]
            return str(rec.get("updated", "") or "") if isinstance(rec, dict) else ""

        try:
            ordered = sorted(data.items(), key=_updated)
        except Exception:  # noqa: BLE001 - 排序异常时退化为截断，绝不抛给调用方
            ordered = list(data.items())
        return dict(ordered[-cls._TOKEN_CALIB_MAX_KEYS:])

    def _load_token_calibration(self) -> dict:
        try:
            data = json.loads(
                self._token_calibration_path().read_text(encoding="utf-8")
            )
            if not isinstance(data, dict):
                return {}
            out = {}
            for k, v in data.items():
                if isinstance(v, dict) and isinstance(v.get("factor"), (int, float)):
                    out[str(k)] = {
                        "factor": float(v["factor"]),
                        "samples": int(v.get("samples", 0) or 0),
                        "last_actual": int(v.get("last_actual", 0) or 0),
                        "updated": str(v.get("updated", "") or ""),
                    }
            return self._trim_token_calibration(out)
        except FileNotFoundError:
            return {}
        except Exception as e:
            # 坏文件静默重置为 {}（P3-18 保持现状，只留 warning），下次写入会覆盖
            logger.warning(f"[memory_diary] 读取 token 校准系数失败: {e}")
            return {}

    def _save_token_calibration(self, data: dict) -> bool:
        try:
            p = self._token_calibration_path()
            tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
            data = self._trim_token_calibration(data)
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
            return True
        except Exception as e:
            logger.warning(f"[memory_diary] 保存 token 校准系数失败: {e}")
            return False

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

