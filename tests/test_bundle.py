# -*- coding: utf-8 -*-
"""Migration-bundle verification / installation differential tests."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import zipfile
from pathlib import Path

from harness import compare, env
from harness.errors import SkipTest
from harness.world import DIARY_TEXT, World

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"


def _build_bundle_with_key(
    master_key: bytes,
    owner: str = SENDER,
    entry: str = "diaries/20261023.md",
    content: str = DIARY_TEXT,
    fmt: str = "memory-diary-export/2",
) -> bytes:
    """Build a migration bundle signed with an arbitrary master key."""
    user_key = hmac.new(
        master_key, f"user-key:{owner}".encode("utf-8"), hashlib.sha256
    ).digest()
    sign_key = user_key if fmt.endswith("/2") else master_key
    signature = hmac.new(
        sign_key,
        (owner + "\n" + entry + "\n" + content).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr(entry, content)
        z.writestr("sigs/20261023.sig", signature)
    manifest = {"format": fmt, "sender_id": owner, "diary_file": "diary.zip"}
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("diary.zip", inner.getvalue())
        key_entry = "user_key.txt" if fmt.endswith("/2") else "diary_key.txt"
        value = user_key.hex() if fmt.endswith("/2") else master_key.decode("utf-8")
        z.writestr(key_entry, value)
        z.writestr("manifest.json", json.dumps(manifest))
    return outer.getvalue()


async def test_verify_bundle_rejects_bad_inputs():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            path = inst.captured_dir / "case.zip"

            def run_case(payload: bytes):
                path.write_bytes(payload)
                try:
                    # v0.6：refactor 的 _verify_bundle 返回 4 元组
                    # (owner, key, entries, images)，baseline 仍是 3 元组；
                    # 差分场景只看前 3 项（图片由 test_image_storage.py 覆盖）。
                    owner, key, entries = plugin._verify_bundle(path)[:3]
                    return {
                        "ok": True,
                        "owner": owner,
                        "key_len": len(key),
                        "entries": sorted(entries),
                    }
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "error": type(exc).__name__, "message": str(exc)}

            good = _build_bundle_with_key(plugin._secret_key())
            bad_zip = run_case(b"not a zip at all")
            good_case = run_case(good)

            # missing manifest.json
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("hello.txt", b"x")
            missing_manifest = run_case(buf.getvalue())

            # unsupported format
            manifest_bad = _build_bundle_with_key(plugin._secret_key())
            manifest_bad = _replace_manifest(manifest_bad, {"format": "memory-diary-export/9"})
            bad_format = run_case(manifest_bad)

            # missing key entry
            no_key = _drop_member(good, "user_key.txt")
            missing_key = run_case(no_key)

            # missing diary file referenced by manifest
            no_diary = _drop_member(good, "diary.zip")
            missing_diary = run_case(no_diary)

            # tampered inner diary content
            tampered = _map_inner(
                good, lambda name, data: data + b"\nX" if name.startswith("diaries/") else data
            )
            tampered_case = run_case(tampered)

            # missing inner signature
            inner_no_sig = _map_inner(
                good,
                lambda name, data: data,
                drop_names=("sigs/20261023.sig",),
            )
            missing_sig = run_case(inner_no_sig)

            # empty diary (only 说明/sigs)
            empty = _map_inner(
                good,
                lambda name, data: data,
                drop_names=("diaries/20261023.md", "sigs/20261023.sig"),
            )
            empty_case = run_case(empty)

            return {
                "good": good_case,
                "bad_zip": bad_zip,
                "missing_manifest": missing_manifest,
                "bad_format": bad_format,
                "missing_key": missing_key,
                "missing_diary": missing_diary,
                "tampered": tampered_case,
                "missing_sig": missing_sig,
                "empty": empty_case,
            }

        pair = await world.run_pair(
            scenario,
            label="verify bundle",
            snapshot_exclude=("memory_diary_export_*.zip", "case*.zip"),
        )
        value = pair.value
        assert value["good"]["ok"] is True
        assert value["good"]["entries"] == ["diaries/20261023.md"]
        assert value["bad_zip"]["error"] == "BadZipFile"
        for key in (
            "missing_manifest",
            "bad_format",
            "missing_key",
            "missing_diary",
            "tampered",
            "missing_sig",
            "empty",
        ):
            assert value[key]["ok"] is False, key
            assert value[key]["error"] == "ValueError", key
        assert "manifest.json" in value["missing_manifest"]["message"]
        assert "格式版本" in value["bad_format"]["message"]
        assert "user_key.txt" in value["missing_key"]["message"]
        assert "验签失败" in value["tampered"]["message"]
        assert "缺少签名" in value["missing_sig"]["message"]
        assert "没有任何日记" in value["empty"]["message"]


def _replace_manifest(payload: bytes, patch: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as src, zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "manifest.json":
                manifest = json.loads(data.decode("utf-8"))
                manifest.update(patch)
                data = json.dumps(manifest).encode("utf-8")
            dst.writestr(name, data)
    return out.getvalue()


def _drop_member(payload: bytes, member: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as src, zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            if name == member:
                continue
            dst.writestr(name, src.read(name))
    return out.getvalue()


def _map_inner(payload: bytes, fn, drop_names=()) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as src, zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "diary.zip":
                inner = io.BytesIO()
                with zipfile.ZipFile(io.BytesIO(data)) as dsrc, zipfile.ZipFile(
                    inner, "w"
                ) as idst:
                    for iname in dsrc.namelist():
                        if iname in drop_names:
                            continue
                        idst.writestr(iname, fn(iname, dsrc.read(iname)))
                data = inner.getvalue()
            dst.writestr(name, data)
    return out.getvalue()


async def test_install_bundle_same_key_and_resign_modes():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            master = plugin._secret_key()
            same = _build_bundle_with_key(master)
            other = _build_bundle_with_key(b"a-completely-different-master-key-0123456789")
            same_path = inst.captured_dir / "same.zip"
            other_path = inst.captured_dir / "other.zip"
            same_path.write_bytes(same)
            other_path.write_bytes(other)

            # Same key -> same_key, entries verifiable locally.
            # （v0.6：refactor 返回 4 元组，baseline 3 元组；差分只看前 3 项）
            owner, key, entries = plugin._verify_bundle(same_path)[:3]
            mode_same = plugin._install_bundle(SENDER, owner, key, entries)
            same_content = plugin._load_latest(SENDER)[0]
            plugin._delete_diary(SENDER)

            # Different key -> resign; local key re-signs the entries.
            owner, key, entries = plugin._verify_bundle(other_path)[:3]
            mode_other = plugin._install_bundle(SENDER, owner, key, entries)
            other_content = plugin._load_latest(SENDER)[0]
            other_snapshot = plugin._list_entries(SENDER)

            # Wrong owner is rejected before touching the filesystem.
            wrong_owner = None
            path = inst.diary_path("10002")
            try:
                plugin._install_bundle("10002", SENDER, key, entries)
            except Exception as exc:  # noqa: BLE001
                wrong_owner = {"type": type(exc).__name__, "message": str(exc)}
            return {
                "mode_same": mode_same,
                "mode_resign": mode_other,
                "same_content": same_content,
                "other_content": other_content,
                "other_entries": other_snapshot,
                "wrong_owner": wrong_owner,
                "wrong_owner_file": path.exists(),
            }

        pair = await world.run_pair(
            scenario,
            label="install bundle",
            snapshot_exclude=("memory_diary_export_*.zip", "same.zip", "other.zip"),
        )
        value = pair.value
        assert value["mode_same"] == "same_key"
        assert value["mode_resign"] == "resign"
        assert value["same_content"] == DIARY_TEXT
        assert value["other_content"] == DIARY_TEXT
        assert value["other_entries"] == ["diaries/20261023.md"]
        assert value["wrong_owner"]["type"] == "ValueError"
        assert "主人 QQ 号" in value["wrong_owner"]["message"]
        assert value["wrong_owner_file"] is False


async def test_cross_implementation_bundle_compatibility():
    """A bundle exported by the baseline must import into the refactor (and back)."""
    if env.is_baseline_only():
        raise SkipTest("requires both baseline and refactor")
    async with World(config={"use_persona": False}) as world:
        base, new = world.baseline, world.refactor
        for inst in (base, new):
            inst.plugin._save_diary(
                SENDER,
                DIARY_TEXT,
                sender_name="阿罗",
                origin="private",
                mode="full",
            )
        base_bundle = base.plugin._build_export_bundle(
            SENDER, base.diary_path(SENDER), base.entries(SENDER)
        )
        new_bundle = new.plugin._build_export_bundle(
            SENDER, new.diary_path(SENDER), new.entries(SENDER)
        )
        # Remove local diaries, then cross-install.
        for inst in (base, new):
            inst.plugin._delete_diary(SENDER)

        # v0.6：refactor 返回 4 元组 / baseline 3 元组，跨实现只比前 3 项
        owner, key, entries = new.plugin._verify_bundle(base_bundle)[:3]
        new_mode = new.plugin._install_bundle(owner, owner, key, entries)
        owner, key, entries = base.plugin._verify_bundle(new_bundle)[:3]
        base_mode = base.plugin._install_bundle(owner, owner, key, entries)

        for bundle in (base_bundle, new_bundle):
            bundle.unlink(missing_ok=True)

        compare.assert_equal(base_mode, new_mode, "cross import mode")
        compare.assert_equal(
            base.plugin._load_latest(SENDER)[0],
            new.plugin._load_latest(SENDER)[0],
            "cross import content",
        )
        compare.assert_equal(
            base.snapshot(),
            new.snapshot(),
            "cross import data dir",
        )
        assert new_mode == "same_key"  # identical seeded key on both sides
