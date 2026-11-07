# -*- coding: utf-8 -*-
"""v0.7.0：可选整 ZIP 静态加密与回退边界。"""

from __future__ import annotations

import zipfile

from harness.world import DIARY_TEXT, World
from memory_diary.errors import DiaryDecryptionError, DiaryPassphraseRequired
from memory_diary.search import search_diaries

SENDER = "10001"
SECRET = "correct horse battery staple"


def test_encrypt_format_nonce_tamper_and_aad_identity():
    async_world = None
    assert async_world is None
    from memory_diary.crypto import StorageCryptoMixin
    raw = b"PK\x03\x04signed-zip-bytes"
    first = StorageCryptoMixin.encrypt_diary_bytes(raw, SECRET, "diary_a.zip")
    second = StorageCryptoMixin.encrypt_diary_bytes(raw, SECRET, "diary_a.zip")
    assert first != second
    assert StorageCryptoMixin.decrypt_diary_bytes(first, SECRET, "diary_a.zip") == raw
    for damaged, password, identity in (
        (first[:-1] + bytes([first[-1] ^ 1]), SECRET, "diary_a.zip"),
        (first, "wrong password", "diary_a.zip"),
        (first, SECRET, "diary_b.zip"),
    ):
        try:
            StorageCryptoMixin.decrypt_diary_bytes(damaged, password, identity)
        except DiaryDecryptionError:
            pass
        else:
            raise AssertionError("密文篡改、错误口令或错误文件身份必须拒绝")


async def test_encryption_roundtrip_rotation_search_and_readable_export():
    async with World(config={
        "storage_encryption_enabled": False,
        "storage_encryption_passphrase": SECRET,
        "current_self_enabled": True,
    }) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        plugin.append_current_self_pending(SENDER, "已确认事项", "用户确认")
        plugin.enqueue_external_material(SENDER, "bridge", "enc", "加密素材")
        path = plugin._diary_path(SENDER)
        original = path.read_bytes()
        assert plugin.convert_diary_encryption(SENDER, True, SECRET)
        encrypted = path.read_bytes()
        assert plugin.is_diary_encrypted(encrypted) and DIARY_TEXT.encode() not in encrypted
        backups = list(plugin.data_dir.glob("*.encrypted-backup-*.zip"))
        assert len(backups) == 1
        backup_raw = backups[0].read_bytes()
        assert plugin.is_diary_encrypted(backup_raw) and DIARY_TEXT.encode() not in backup_raw
        assert plugin.decrypt_diary_bytes(backup_raw, SECRET, path.name) == original
        assert search_diaries(plugin, SENDER, "阿罗")["total"] == 1
        assert plugin.load_current_self(SENDER)[0]["current"]["pending_items"][0]["text"] == "已确认事项"
        assert plugin.load_external_materials(SENDER)[0]["items"][0]["text"] == "加密素材"

        bundle = plugin._build_export_bundle(SENDER, path, plugin._list_entries(SENDER))
        try:
            with zipfile.ZipFile(bundle) as z:
                readable = z.read(path.name)
            assert not plugin.is_diary_encrypted(readable)
            with zipfile.ZipFile(__import__("io").BytesIO(readable)) as z:
                assert DIARY_TEXT in z.read("diaries/20261023.md").decode()
            verified = plugin._verify_bundle(bundle)
            assert verified[2]
        finally:
            bundle.unlink(missing_ok=True)

        plugin.config["storage_encryption_enabled"] = True
        plugin._save_diary(SENDER, "加密后新增内容", origin="private", mode="brief")
        assert plugin.is_diary_encrypted(path.read_bytes())
        plugin.config["storage_encryption_passphrase"] = "a new passphrase"
        assert plugin.convert_diary_encryption(
            SENDER, True, "a new passphrase", old_passphrase=SECRET
        )
        assert "加密后新增内容" in plugin._load_latest(SENDER)[0]
        assert plugin.convert_diary_encryption(SENDER, False, old_passphrase="a new passphrase")
        assert not plugin.is_diary_encrypted(path.read_bytes())
        assert original != path.read_bytes()


async def test_wrong_or_missing_passphrase_never_quarantines_or_overwrites():
    async with World(config={
        "storage_encryption_enabled": True,
        "storage_encryption_passphrase": SECRET,
    }) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        path = plugin._diary_path(SENDER)
        before = path.read_bytes()
        plugin.config["storage_encryption_passphrase"] = "wrong password"
        try:
            plugin._save_diary(SENDER, "不得覆盖", origin="private", mode="brief")
        except Exception as exc:
            assert type(exc).__name__ == "DiaryDecryptionError"
        else:
            raise AssertionError("错误口令必须拒绝写入")
        assert path.read_bytes() == before
        assert not list(plugin.data_dir.glob("*.corrupt-*.zip"))
        plugin.config["storage_encryption_passphrase"] = ""
        try:
            plugin._read_diary_bytes(path)
        except Exception as exc:
            assert type(exc).__name__ == "DiaryPassphraseRequired"
        else:
            raise AssertionError("缺口令必须明确报错")
        assert path.read_bytes() == before


async def test_initialize_reconciles_encryption_without_writing_and_is_idempotent():
    async with World(config={
        "storage_encryption_enabled": False,
        "storage_encryption_passphrase": SECRET,
        "current_self_enabled": True,
    }) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        plugin.append_current_self_pending(SENDER, "确认事项", "用户确认")
        plugin.enqueue_external_material(SENDER, "bridge", "init", "待吸收素材")
        path = plugin._diary_path(SENDER)
        assert not plugin.is_diary_encrypted(path.read_bytes())

        plugin.config["storage_encryption_enabled"] = True
        await plugin.initialize()
        assert plugin.is_diary_encrypted(path.read_bytes())
        assert len(plugin._list_entries(SENDER)) == 1
        backups = list(plugin.data_dir.glob("*.encrypted-backup-*.zip"))
        assert len(backups) == 1
        assert plugin.is_diary_encrypted(backups[0].read_bytes())
        encrypted = path.read_bytes()

        await plugin.initialize()
        assert path.read_bytes() == encrypted
        assert len(list(plugin.data_dir.glob("*.encrypted-backup-*.zip"))) == 1

        plugin.config["storage_encryption_passphrase"] = "wrong password"
        await plugin.initialize()
        assert path.read_bytes() == encrypted

        plugin.config["storage_encryption_passphrase"] = SECRET
        plugin.config["storage_encryption_enabled"] = False
        await plugin.initialize()
        assert not plugin.is_diary_encrypted(path.read_bytes())
        assert plugin.load_current_self(SENDER)[1] == "ok"
        assert plugin.load_external_materials(SENDER)[1] == "ok"
        plaintext_backups = list(plugin.data_dir.glob("*.plaintext-backup-*.zip"))
        assert len(plaintext_backups) == 1
        assert plugin.is_diary_encrypted(plaintext_backups[0].read_bytes())
        plaintext = path.read_bytes()
        await plugin.initialize()
        assert path.read_bytes() == plaintext
        assert len(list(plugin.data_dir.glob("*.plaintext-backup-*.zip"))) == 1


async def test_initialize_failure_preserves_original_and_next_run_recovers():
    async with World(config={
        "storage_encryption_enabled": False,
        "storage_encryption_passphrase": SECRET,
    }) as world:
        plugin = world.refactor.plugin
        other = "20002"
        plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        plugin._save_diary(other, DIARY_TEXT, origin="private", mode="full")
        bad_path = plugin._diary_path(SENDER)
        good_path = plugin._diary_path(other)
        valid = bad_path.read_bytes()
        with zipfile.ZipFile(bad_path) as z:
            entries = {name: z.read(name) for name in z.namelist()}
        entries["diaries/20261023.md"] += b"tampered"
        with zipfile.ZipFile(bad_path, "w") as z:
            for name, payload in entries.items():
                z.writestr(name, payload)
        damaged = bad_path.read_bytes()

        plugin.config["storage_encryption_enabled"] = True
        await plugin.initialize()
        assert bad_path.read_bytes() == damaged
        assert not plugin.is_diary_encrypted(bad_path.read_bytes())
        assert plugin.is_diary_encrypted(good_path.read_bytes())

        bad_path.write_bytes(valid)
        await plugin.initialize()
        assert plugin.is_diary_encrypted(bad_path.read_bytes())
        assert plugin._load_entry(SENDER, "diaries/20261023.md")[1] is False


async def test_initialize_encrypts_non_whitelist_history_and_backup_keeps_it():
    async with World(config={
        "storage_encryption_enabled": False,
        "storage_encryption_passphrase": SECRET,
        "current_self_enabled": True,
    }) as world:
        plugin = world.refactor.plugin
        other = "20002"
        for sender in (SENDER, other):
            plugin._save_diary(sender, DIARY_TEXT, origin="private", mode="full")
            plugin.append_current_self_pending(sender, "历史确认事项", "用户确认")
        paths = [plugin._diary_path(sender) for sender in (SENDER, other)]
        plugin.config["allowed_users"] = "30003"
        plugin.config["storage_encryption_enabled"] = True
        await plugin.initialize()
        assert all(plugin.is_diary_encrypted(path.read_bytes()) for path in paths)
        assert plugin._assemble_inject(SENDER)[0] == ""

        bundle, count, error = plugin._prepare_backup_all()
        try:
            assert error is None and count == 2
            with zipfile.ZipFile(bundle) as z:
                assert {path.name for path in paths} <= set(z.namelist())
                assert all(plugin.is_diary_encrypted(z.read(path.name)) for path in paths)
        finally:
            if bundle is not None:
                bundle.unlink(missing_ok=True)


async def test_rotation_with_wrong_old_passphrase_never_overwrites():
    async with World(config={
        "storage_encryption_enabled": True,
        "storage_encryption_passphrase": SECRET,
    }) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        path = plugin._diary_path(SENDER)
        before = path.read_bytes()
        try:
            plugin.convert_diary_encryption(
                SENDER, True, "a new passphrase", old_passphrase="wrong password"
            )
        except Exception as exc:
            assert type(exc).__name__ == "DiaryDecryptionError"
        else:
            raise AssertionError("错误旧口令必须拒绝轮换")
        assert path.read_bytes() == before
        assert plugin.convert_diary_encryption(
            SENDER, True, "a new passphrase", old_passphrase=SECRET
        )
        plugin.config["storage_encryption_passphrase"] = "a new passphrase"
        assert plugin._load_entry(SENDER, "diaries/20261023.md")[1] is False
