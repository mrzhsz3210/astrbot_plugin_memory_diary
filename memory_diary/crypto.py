# -*- coding: utf-8 -*-
"""可选的整 ZIP 静态加密；明文只在已解锁进程内存中存在。"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import struct
import zipfile
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .errors import DiaryDecryptionError, DiaryPassphraseRequired
from .log import logger


class StorageCryptoMixin:
    DIARY_ENCRYPT_MAGIC = b"MDIARY\x00ENC"
    DIARY_ENCRYPT_VERSION = 1
    SCRYPT_N = 1 << 14
    SCRYPT_R = 8
    SCRYPT_P = 1

    def _encryption_passphrase(self) -> str:
        return str(self.config.get("storage_encryption_passphrase", "") or "")

    def _encryption_enabled(self) -> bool:
        return bool(self.config.get("storage_encryption_enabled", False))

    @classmethod
    def is_diary_encrypted(cls, raw: bytes) -> bool:
        return bytes(raw).startswith(cls.DIARY_ENCRYPT_MAGIC)

    @classmethod
    def _derive_storage_key(cls, passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
        return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))

    @classmethod
    def encrypt_diary_bytes(cls, raw: bytes, passphrase: str, identity: str) -> bytes:
        if len(passphrase) < 12:
            raise DiaryPassphraseRequired("静态加密口令至少需要 12 个字符")
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        metadata = {
            "version": cls.DIARY_ENCRYPT_VERSION,
            "kdf": "scrypt",
            "n": cls.SCRYPT_N,
            "r": cls.SCRYPT_R,
            "p": cls.SCRYPT_P,
            "salt": salt.hex(),
            "cipher": "AES-256-GCM",
        }
        header = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        aad = b"memory-diary-zip\0" + bytes([cls.DIARY_ENCRYPT_VERSION]) + identity.encode("utf-8")
        key = cls._derive_storage_key(passphrase, salt, cls.SCRYPT_N, cls.SCRYPT_R, cls.SCRYPT_P)
        ciphertext = AESGCM(key).encrypt(nonce, raw, aad)
        return cls.DIARY_ENCRYPT_MAGIC + struct.pack(">I", len(header)) + header + nonce + ciphertext

    @classmethod
    def decrypt_diary_bytes(cls, raw: bytes, passphrase: str, identity: str) -> bytes:
        if not passphrase:
            raise DiaryPassphraseRequired("读取加密日记需要口令")
        try:
            offset = len(cls.DIARY_ENCRYPT_MAGIC)
            header_len = struct.unpack(">I", raw[offset:offset + 4])[0]
            offset += 4
            metadata = json.loads(raw[offset:offset + header_len].decode())
            offset += header_len
            version = int(metadata.get("version"))
            if version != cls.DIARY_ENCRYPT_VERSION or metadata.get("cipher") != "AES-256-GCM":
                raise DiaryDecryptionError("不支持的日记加密格式")
            salt = bytes.fromhex(metadata["salt"])
            n, r, p = int(metadata["n"]), int(metadata["r"]), int(metadata["p"])
            if n != cls.SCRYPT_N or r != cls.SCRYPT_R or p != cls.SCRYPT_P or len(salt) != 16:
                raise DiaryDecryptionError("日记加密参数不受支持")
            nonce, ciphertext = raw[offset:offset + 12], raw[offset + 12:]
            if len(nonce) != 12:
                raise DiaryDecryptionError("日记密文格式不完整")
            aad = b"memory-diary-zip\0" + bytes([version]) + identity.encode("utf-8")
            key = cls._derive_storage_key(passphrase, salt, n, r, p)
            return AESGCM(key).decrypt(nonce, ciphertext, aad)
        except DiaryDecryptionError:
            raise
        except Exception as exc:
            raise DiaryDecryptionError("口令错误或密文认证失败") from exc

    def _read_diary_bytes(
        self, path: Path, passphrase: str | None = None, identity: str = ""
    ) -> bytes:
        raw = path.read_bytes()
        if not self.is_diary_encrypted(raw):
            return raw
        secret = self._encryption_passphrase() if passphrase is None else passphrase
        return self.decrypt_diary_bytes(raw, secret, identity or path.name)

    @contextmanager
    def _diary_zip(self, path: Path):
        raw = self._read_diary_bytes(path)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            yield archive

    def _encode_diary_write(self, plain: bytes, path: Path) -> bytes:
        if not self._encryption_enabled():
            return plain
        return self.encrypt_diary_bytes(plain, self._encryption_passphrase(), path.name)

    def _verify_plain_diary(self, sender_id: str, raw: bytes) -> None:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            bad = archive.testzip()
            if bad:
                raise DiaryDecryptionError(f"日记条目 {bad} 校验失败")
            names = set(archive.namelist())
            for name in names:
                if self.ENTRY_NAME_RE.fullmatch(name):
                    content = archive.read(name).decode("utf-8")
                    sig_name = self._sig_entry(name)
                    actual = archive.read(sig_name).decode().strip() if sig_name in names else ""
                    if not self._entry_sig_matches(sender_id, name, content, actual):
                        raise DiaryDecryptionError("日记 HMAC 验签失败，拒绝转换加密状态")
                elif self.IMAGE_NAME_RE.fullmatch(name):
                    payload = archive.read(name)
                    sig_name = self._image_sig_entry(name)
                    actual = archive.read(sig_name).decode().strip() if sig_name in names else ""
                    if not self._image_sig_matches(sender_id, name, payload, actual):
                        raise DiaryDecryptionError("插图 HMAC 验签失败，拒绝转换加密状态")
            sign_key = self._user_sign_key(sender_id)
            for entry, sig_entry in (
                (self.CURRENT_SELF_ENTRY, self.CURRENT_SELF_SIG_ENTRY),
                (self.MATERIALS_ENTRY, self.MATERIALS_SIG_ENTRY),
                (self.CURRENT_SELF_ARCHIVE_ENTRY, self.CURRENT_SELF_ARCHIVE_SIG_ENTRY),
                (self.ANNOTATIONS_ENTRY, self.ANNOTATIONS_SIG_ENTRY),
                (self.PROVENANCE_ENTRY, self.PROVENANCE_SIG_ENTRY),
            ):
                if entry not in names:
                    continue
                if sign_key is None:
                    raise DiaryDecryptionError("签名密钥不可用，拒绝转换加密状态")
                payload = archive.read(entry)
                actual = archive.read(sig_entry).decode().strip() if sig_entry in names else ""
                expected = hmac.new(
                    sign_key,
                    sender_id.encode("utf-8") + b"\n" + entry.encode("utf-8") + b"\n" + payload,
                    hashlib.sha256,
                ).hexdigest()
                if not hmac_compare(expected, actual):
                    raise DiaryDecryptionError(f"侧车 {entry} 验签失败，拒绝转换加密状态")

    def convert_diary_encryption(
        self, sender_id: str, enabled: bool, passphrase: str = "", old_passphrase: str = ""
    ) -> bool:
        """先完整读取验签并备份，再原子转换；口令轮换也走此入口。"""
        path = self._diary_path(sender_id)
        if not path.exists():
            return False
        target_secret = passphrase or self._encryption_passphrase()
        if enabled and len(target_secret) < 12:
            raise DiaryPassphraseRequired("静态加密口令至少需要 12 个字符")
        current_secret = old_passphrase or self._encryption_passphrase()
        plain = self._read_diary_bytes(path, current_secret)
        self._verify_plain_diary(sender_id, plain)
        transformed = self.encrypt_diary_bytes(plain, target_secret, path.name) if enabled else plain
        suffix = "encrypted" if enabled else "plaintext"
        backup = path.with_name(f"{path.stem}.{suffix}-backup-{secrets.token_hex(4)}.zip")
        original = path.read_bytes()
        backup_payload = (
            self.encrypt_diary_bytes(original, target_secret, path.name)
            if enabled and not self.is_diary_encrypted(original)
            else original
        )
        backup.write_bytes(backup_payload)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
        try:
            tmp.write_bytes(transformed)
            os.replace(tmp, path)
            return True
        except Exception:
            logger.warning("[memory_diary] 加密状态转换失败，原文件保留")
            return False
        finally:
            tmp.unlink(missing_ok=True)

    def _reconcile_diary_encryption(self):
        """初始化时把现有日记转换到当前开关状态；逐文件失败可下次恢复。"""
        enabled = self._encryption_enabled()
        owner_map = None
        for path in sorted(self.data_dir.glob("diary_*.zip")):
            match = re.fullmatch(r"diary_([0-9a-f]{16})\.zip", path.name)
            if match is None:
                continue
            file_key = match.group(1)
            try:
                raw = path.read_bytes()
                encrypted = self.is_diary_encrypted(raw)
                if encrypted == enabled:
                    continue
                owner, _nick = self._zip_owner_info(path)
                if not owner:
                    if owner_map is None:
                        owner_map = self._load_owner_map()
                    owner = str(owner_map.get(file_key) or "")
                if not owner:
                    logger.warning(
                        f"[memory_diary] {path.name} 无法确认主人，未转换加密状态，原文件保留"
                    )
                    continue
                converted = self.convert_diary_encryption(owner, enabled)
                if converted:
                    logger.info(
                        f"[memory_diary] {path.name} 已转换为{'密文' if enabled else '明文'}存储"
                    )
                else:
                    logger.warning(
                        f"[memory_diary] {path.name} 加密状态未转换，原文件保留"
                    )
            except Exception as exc:
                logger.warning(
                    f"[memory_diary] {path.name} 加密状态转换失败，原文件保留: {exc}"
                )


def hmac_compare(left: str, right: str) -> bool:
    return bool(left) and hmac.compare_digest(left, right)
