# -*- coding: utf-8 -*-
"""Storage-layer differential tests: save/load, tamper, corrupt, migration, prefs."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

from harness import compare
from harness.world import BRIEF_TEXT, DIARY_TEXT, LIFE_TEXT, World, message

DAY = "20261023"
OTHER_DAY = "20261021"

FULL_HEADER = "—— 20:30 · 完整 · 写于私聊 ——"


def _seg_header(content: str) -> str:
    headers = [
        line.strip()
        for line in content.splitlines()
        if line.strip().startswith("—— ") and line.strip().endswith(" ——")
    ]
    return headers[0] if headers else ""


async def test_save_and_load_full_brief_life_modes():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin._save_diary(
                "10001", DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
            )
            plugin._save_diary(
                "10001", BRIEF_TEXT, sender_name="阿罗", origin="private", mode="brief"
            )
            plugin._save_diary(
                "10001",
                LIFE_TEXT,
                sender_name="阿罗",
                origin="private",
                mode="life",
                separator_note="（无新对话·她的日常随笔）",
            )
            content, tampered, name = plugin._load_latest("10001")
            segs = plugin._segments(content)
            info = [plugin._segment_info(s["header"]) for s in segs]
            return {
                "tampered": tampered,
                "name": name,
                "entries": plugin._list_entries("10001"),
                "content": content,
                "segment_count": len(segs),
                "infos": info,
                "briefs": len(plugin._briefs_for_scene(content, "private")),
                "full_count": plugin._full_count_for_scene(content, "private"),
                "latest_label": plugin._entry_label(name),
                "zip_entries": sorted(
                    compare.zip_entries(inst.diary_path("10001")).keys()
                ),
                "readme_lines": compare.zip_entries(inst.diary_path("10001"))[
                    "说明.txt"
                ].decode("utf-8").splitlines()[:4],
            }

        pair = await world.run_pair(scenario, label="modes")
        value = pair.value
        assert value["tampered"] is False
        assert value["name"] == f"diaries/{DAY}.md"
        assert value["segment_count"] == 3
        assert [i["mode"] for i in value["infos"]] == ["full", "brief", "life"]
        assert value["full_count"] == 1
        assert value["briefs"] == 1
        assert value["zip_entries"] == [
            "diaries/20261023.md",
            "sigs/20261023.sig",
            "说明.txt",
        ]


async def test_same_day_append_and_absorbed_briefs():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            # brief first, then a full entry that absorbs it
            plugin._save_diary(
                "10001", BRIEF_TEXT, sender_name="阿罗", origin="private", mode="brief"
            )
            content_before = plugin._load_latest("10001")[0]
            header = _seg_header(content_before)
            plugin._save_diary(
                "10001",
                DIARY_TEXT,
                sender_name="阿罗",
                origin="private",
                mode="full",
                absorb_headers=[header],
            )
            content = plugin._load_latest("10001")[0]
            segs = plugin._segments(content)
            return {
                "brief_header_before": header,
                "content_after": content,
                "absorbed_marked": "（已吸收）" in content,
                "briefs_now": len(plugin._briefs_for_scene(content, "private")),
                "full_count": plugin._full_count_for_scene(content, "private"),
                "filtered": plugin._filter_absorbed_segments(content),
                "segment_infos": [plugin._segment_info(s["header"]) for s in segs],
            }

        pair = await world.run_pair(scenario, label="append/absorb")
        value = pair.value
        assert value["absorbed_marked"] is True
        assert value["briefs_now"] == 0
        assert value["full_count"] == 1
        assert "简记" not in value["filtered"]


async def test_group_and_private_segments_are_scene_isolated():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin._save_diary(
                "10001", "私聊内容", sender_name="阿罗", origin="private", mode="full"
            )
            plugin._save_diary(
                "10001", "群聊内容", sender_name="阿罗", origin="group", mode="full"
            )
            content = plugin._load_latest("10001")[0]
            return {
                "content": content,
                "private_full": plugin._full_count_for_scene(content, "private"),
                "group_full": plugin._full_count_for_scene(content, "group"),
                "group_filtered": plugin._filter_segments_for_group(content),
                "assemble_group": plugin._assemble_inject("10001", in_group=True),
                "assemble_private": plugin._assemble_inject("10001", in_group=False),
            }

        pair = await world.run_pair(scenario, label="scene isolation")
        value = pair.value
        assert value["private_full"] == 1 and value["group_full"] == 1
        assert "私聊内容" not in value["group_filtered"]
        assert "群聊内容" in value["assemble_group"][0]
        assert "私聊内容" in value["assemble_private"][0]


async def test_tampered_entry_rejected_on_load_and_save():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            path = inst.diary_path("10001")
            plugin._save_diary(
                "10001", DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
            )
            # Tamper with the day file but keep the old signature.
            entries = compare.zip_entries(path)
            entries[f"diaries/{DAY}.md"] = (
                entries[f"diaries/{DAY}.md"].decode("utf-8") + "\n{篡改}"
            ).encode("utf-8")
            plugin._write_zip_atomic(path, entries)
            loaded = plugin._load_latest("10001")
            save_error = None
            try:
                plugin._save_diary(
                    "10001", "新内容", sender_name="阿罗", origin="private", mode="brief"
                )
            except Exception as exc:  # noqa: BLE001
                save_error = {"type": type(exc).__name__, "message": str(exc)}
            return {
                "content": loaded[0],
                "tampered": loaded[1],
                "name": loaded[2],
                "entries_after": plugin._list_entries("10001"),
                "save_error": save_error,
            }

        pair = await world.run_pair(scenario, label="tamper")
        value = pair.value
        assert value["tampered"] is True
        assert value["content"] == ""
        assert value["save_error"]["type"] == "DiaryTamperedError"
        assert "2026-10-23" in value["save_error"]["message"]


async def test_corrupt_zip_is_quarantined_not_overwritten():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            path = inst.diary_path("10001")
            path.write_bytes(b"this is definitely not a zip file")
            notice = plugin._corrupt_notice("10001")
            listed = plugin._list_entries("10001")
            save_error = None
            try:
                plugin._save_diary(
                    "10001", "新内容", sender_name="阿罗", origin="private", mode="brief"
                )
            except Exception as exc:  # noqa: BLE001
                save_error = {"type": type(exc).__name__, "message": str(exc)}
            files = sorted(p.name for p in inst.data_dir.iterdir())
            return {
                "notice": notice,
                "listed": listed,
                "save_error_type": save_error["type"],
                "save_error": save_error,
                "files": files,
                "original_exists": path.exists(),
            }

        pair = await world.run_pair(scenario, label="corrupt")
        value = pair.value
        assert value["listed"] == []
        assert value["save_error_type"] == "DiaryCorruptError"
        assert value["original_exists"] is False
        assert any(name.startswith("diary_") and ".corrupt-" in name for name in value["files"])
        assert value["notice"].startswith("📖 你的日记文件")


async def test_delete_entries_and_delete_all():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            for day, text in (("20261020", "A"), ("20261021", "B"), ("20261022", "C")):
                plugin._save_diary(
                    "10001", text, entry_name_hint=day, origin="private", mode="full"
                )
            entries = plugin._list_entries("10001")
            deleted_one = plugin._delete_entries("10001", [entries[1]])
            after_one = plugin._list_entries("10001")
            deleted_rest = plugin._delete_entries("10001", after_one)
            after_all = plugin._list_entries("10001")
            path_exists = inst.diary_path("10001").exists()
            existed = plugin._delete_diary("10001")
            return {
                "entries": entries,
                "deleted_one": deleted_one,
                "after_one": after_one,
                "deleted_rest": deleted_rest,
                "after_all": after_all,
                "path_exists": path_exists,
                "delete_all_on_missing": existed,
            }

        await world.run_pair(scenario, label="delete entries")


async def test_pref_scope_and_material_cursor():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            parsed = {
                arg: plugin._parse_scope(arg)
                for arg in (
                    "最新",
                    "latest",
                    "全部",
                    "all",
                    "最近7天",
                    "最近0天",
                    "2026-10-01 到 2026-10-23",
                    "2026-10-23",
                    "2026-02-31",
                    "nonsense",
                )
            }
            plugin._save_pref("10001", "days:3")
            scope = plugin._load_pref("10001")
            plugin._save_material_hash("10001", "abc123")
            plugin._save_material_cursor(
                "10001", "aiocqhttp:FriendMessage:10001", {"cid": "cid-1", "count": 3}
            )
            cursor = plugin._load_material_cursor(
                "10001", "aiocqhttp:FriendMessage:10001"
            )
            plugin._save_pref("10001", None)
            cleared = plugin._load_pref("10001")
            entries = [
                f"diaries/{d}.md" for d in ("20261001", "20261015", "20261020", "20261023")
            ]
            selection = {
                scope_name: plugin._select_entries(entries, scope_name)
                for scope_name in (
                    "latest",
                    "all",
                    "days:3",
                    "range:20261001-20261020",
                    "range:20261020-20261001",
                )
            }
            return {
                "parsed": parsed,
                "scope": scope,
                "material_hash": plugin._load_material_hash("10001"),
                "cursor": cursor,
                "cursor_pref": plugin._load_pref_data("10001"),
                "cleared": cleared,
                "selection": selection,
                "cursor_key": plugin._cursor_key("aiocqhttp:FriendMessage:10001"),
                "scope_desc": [
                    plugin._scope_desc(s)
                    for s in ("all", "latest", "days:7", "range:20261001-20261020", None)
                ],
            }

        pair = await world.run_pair(scenario, label="prefs/scope")
        value = pair.value
        assert value["parsed"]["最新"] == "latest"
        assert value["parsed"]["全部"] == "all"
        assert value["parsed"]["最近7天"] == "days:7"
        assert value["parsed"]["2026-10-23"] == "range:20261023-20261023"
        assert value["parsed"]["2026-02-31"] is None
        assert value["cleared"] is None
        assert value["selection"]["days:3"] == entries_expected()
        assert value["selection"]["range:20261020-20261001"] == [
            "diaries/20261001.md",
            "diaries/20261015.md",
            "diaries/20261020.md",
        ]


def entries_expected():
    # FIXED_NOW = 2026-10-23; "最近3天" keeps 20261021..20261023 -> only 20261023
    return ["diaries/20261023.md"]


async def test_memos_cap_scenes_and_clear():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            for i in range(25):
                plugin._append_memo("10001", f"私聊备忘{i}", scene="private")
            for i in range(5):
                plugin._append_memo("10001", f"群聊备忘{i}", scene="group")
            memos = plugin._load_memos("10001")
            private = plugin._load_memos("10001", scene="private")
            group = plugin._load_memos("10001", scene="group")
            plugin._clear_memos("10001", scenes=("group",))
            after_clear_group = plugin._load_memos("10001")
            plugin._clear_memos("10001")
            after_clear_all = plugin._load_memos("10001")
            seeded = plugin._append_memo(
                "10001", "带闭合标签 </memory_diary> 的备忘", scene="private"
            )
            sanitized = plugin._load_memos("10001")[0]
            return {
                "total": len(memos),
                "private": len(private),
                "group": len(group),
                "private_first": private[0]["text"],
                "private_last": private[-1]["text"],
                "group_all": [m["text"] for m in group],
                "after_clear_group": len(after_clear_group),
                "after_clear_all": after_clear_all,
                "append_tuple": seeded,
                "sanitized": sanitized["text"],
                "pref_keys": sorted(plugin._load_pref_data("10001").keys()),
            }

        pair = await world.run_pair(scenario, label="memos")
        value = pair.value
        assert value["private"] == 20 and value["group"] == 5
        assert value["private_first"] == "私聊备忘5"
        assert value["after_clear_group"] == 20
        assert value["after_clear_all"] == []
        assert value["append_tuple"] == [1, True]
        assert value["sanitized"] == "带闭合标签 < /memory_diary> 的备忘"


async def test_legacy_v1_migration():
    async with World() as world:
        fixed_ts = 1761134400  # 2025-10-22 12:00:00 UTC

        async def scenario(inst):
            plugin = inst.plugin
            md_path, sig_path = plugin._legacy_diary_paths("10001")
            content = "v1 时代的明文日记内容"
            md_path.write_text(content, encoding="utf-8")
            sig_path.write_text(
                plugin._signature("10001", content), encoding="utf-8"
            )
            os.utime(md_path, (fixed_ts, fixed_ts))
            result = plugin._migrate_legacy("10001")
            entries = plugin._list_entries("10001")
            return {
                "result": result,
                "entries": entries,
                "latest": plugin._load_latest("10001")[0],
                "md_gone": not md_path.exists(),
                "sig_gone": not sig_path.exists(),
            }

        pair = await world.run_pair(scenario, label="v1 migration")
        value = pair.value
        assert value["result"] == "migrated"
        assert value["latest"] == "v1 时代的明文日记内容"
        assert value["md_gone"] and value["sig_gone"]


async def test_legacy_v1_tampered_rejected():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            md_path, sig_path = plugin._legacy_diary_paths("10001")
            md_path.write_text("原始内容", encoding="utf-8")
            sig_path.write_text("deadbeef", encoding="utf-8")
            result = plugin._migrate_legacy("10001")
            return {
                "result": result,
                "md_exists": md_path.exists(),
                "entries": plugin._list_entries("10001"),
            }

        pair = await world.run_pair(scenario, label="v1 tampered")
        assert pair.value["result"] == "tampered"


async def test_legacy_v2_migration():
    async with World() as world:
        fixed_ts = 1761134400

        async def scenario(inst):
            plugin = inst.plugin
            path = inst.diary_path("10001")
            content = "v2 单篇 zip 日记"
            plugin._write_zip_atomic(
                path,
                {
                    "diary.md": content.encode("utf-8"),
                    "diary.sig": plugin._signature("10001", content).encode("utf-8"),
                },
            )
            os.utime(path, (fixed_ts, fixed_ts))
            result = plugin._migrate_legacy("10001")
            entries = plugin._list_entries("10001")
            return {
                "result": result,
                "entries": entries,
                "latest": plugin._load_latest("10001")[0],
                "zip_entries": sorted(compare.zip_entries(path).keys()),
            }

        pair = await world.run_pair(scenario, label="v2 migration")
        value = pair.value
        assert value["result"] == "migrated"
        assert value["latest"] == "v2 单篇 zip 日记"
        assert value["entries"] == [f"diaries/20251022.md"]
        assert "diary.md" not in value["zip_entries"]


async def test_legacy_v3_flat_day_and_per_entry_migration():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            path = inst.diary_path("10001")
            flat_name = "20261020.md"
            per_entry_name = "20261021_203000.md"
            flat_content = "v3.1 平铺按天"
            entry_content = "v3.0 按篇命名"
            plugin._write_zip_atomic(
                path,
                {
                    flat_name: flat_content.encode("utf-8"),
                    flat_name[:-3] + ".sig": plugin._entry_sig(
                        "10001", flat_name, flat_content
                    ).encode("utf-8"),
                    per_entry_name: entry_content.encode("utf-8"),
                    per_entry_name[:-3] + ".sig": plugin._entry_sig(
                        "10001", per_entry_name, entry_content
                    ).encode("utf-8"),
                },
            )
            result = plugin._migrate_legacy("10001")
            entries = plugin._list_entries("10001")
            return {
                "result": result,
                "entries": entries,
                "contents": [plugin._load_entry("10001", n)[0] for n in entries],
                "zip_entries": sorted(compare.zip_entries(path).keys()),
            }

        pair = await world.run_pair(scenario, label="v3 migration")
        value = pair.value
        assert value["result"] == "migrated"
        assert value["entries"] == [
            "diaries/20261020.md",
            "diaries/20261021.md",
        ]
        assert "—— 当天 20:30 记 ——" in value["contents"][1]


async def test_legacy_v3_tampered_entry_skipped():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            path = inst.diary_path("10001")
            name = "20261020.md"
            plugin._write_zip_atomic(
                path,
                {
                    name: "内容".encode("utf-8"),
                    name[:-3] + ".sig": b"not-a-real-signature",
                },
            )
            return {
                "result": plugin._migrate_legacy("10001"),
                "entries": plugin._list_entries("10001"),
            }

        pair = await world.run_pair(scenario, label="v3 tampered")
        assert pair.value["result"] == "tampered"


async def test_hmac_filename_migration_from_legacy_md5_name():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            path = inst.diary_path("10001")
            plugin._save_diary(
                "10001", DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
            )
            new_name = path.name
            old_md5 = __import__("hashlib").md5(b"10001").hexdigest()[:16]
            old_name = f"diary_{old_md5}.zip"
            old_path = inst.data_dir / old_name
            path.rename(old_path)
            plugin._key_cache = None
            resolved = Path(plugin._diary_path("10001"))
            return {
                "new_name": new_name,
                "old_name": old_name,
                "old_md5_key": old_md5,
                "resolved": resolved.name,
                "old_exists": old_path.exists(),
                "new_exists": resolved.exists(),
                "entries": plugin._list_entries("10001"),
            }

        pair = await world.run_pair(scenario, label="hmac rename")
        value = pair.value
        assert value["old_md5_key"] != value["new_name"]
        assert value["resolved"] == value["new_name"]
        assert value["old_exists"] is False and value["new_exists"] is True


async def test_readme_contains_owner_and_structure_notes():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin._save_diary(
                "10001", DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
            )
            text = compare.zip_entries(inst.diary_path("10001"))["说明.txt"].decode(
                "utf-8"
            )
            return {
                "has_nick": "用户昵称：阿罗" in text,
                "has_owner": "用户ID：10001" in text,
                "has_key_warning": ".sig" in text,
                "readme_text_same_as_zip": text == plugin._readme_text("10001", "阿罗"),
                "line_count": len(text.splitlines()),
            }

        pair = await world.run_pair(scenario, label="readme")
        assert pair.value["has_nick"] and pair.value["has_owner"]


async def test_key_fail_closed_behaviour():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin._save_diary(
                "10001", DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
            )
            # Replace the key file with a directory: reading it raises, which must
            # fail closed (no fallback to a public constant, no silent write).
            key_path = inst.data_dir / ".diary_key"
            key_path.unlink()
            key_path.mkdir()
            plugin._key_cache = None
            save_error = None
            try:
                plugin._save_diary(
                    "10001", "新内容", sender_name="阿罗", origin="private", mode="brief"
                )
            except Exception as exc:  # noqa: BLE001
                save_error = type(exc).__name__
            loaded = plugin._load_latest("10001")
            return {
                "secret_key": plugin._secret_key(),
                "save_error": save_error,
                "load_content": loaded[0],
                "load_name": loaded[2],
                "entries": plugin._list_entries("10001"),
                "fallback_file_key": plugin._diary_file_key("10001"),
                "old_md5_key": __import__("hashlib").md5(b"10001").hexdigest()[:16],
                "pref": plugin._load_pref_data("10001"),
            }

        pair = await world.run_pair(scenario, label="key fail closed")
        value = pair.value
        assert value["secret_key"] is None
        assert value["save_error"] == "DiaryKeyUnavailable"
        assert value["load_name"] is None
        assert value["entries"] == []
        assert value["fallback_file_key"] == value["old_md5_key"]


async def test_segments_and_helpers_parity():
    async with World() as world:
        async def scenario(inst):
            plugin = inst.plugin
            content = (
                "—— 20:30 · 完整 · 写于私聊 ——\n\n第一篇正文\n\n"
                "—— 当天 21:10 简记 · 写于群聊 ——\n\n群聊简记\n\n"
                "—— 22:00 · 完整 · 写于私聊 ——\n\n第二篇正文"
            )
            return {
                "segments": len(plugin._segments(content)),
                "infos": [plugin._segment_info(s["header"]) for s in plugin._segments(content)],
                "filtered": plugin._filter_absorbed_segments(content),
                "ops": [
                    plugin._is_op_command(text)
                    for text in (
                        "/日记",
                        "日记",
                        "/记一下 考试",
                        "我写了一篇日记",
                        "/reset",
                        "hello",
                        "/查看日记",
                    )
                ],
                "filtered_ops": plugin._filter_diary_ops(
                    "用户: /写日记\n用户: 今天很开心\n我: 📖 日记写好啦\n我: 正常回复"
                ),
                "entry_labels": [
                    plugin._entry_label("diaries/20261023.md"),
                    plugin._entry_label("20261023_203000.md"),
                ],
                "sanitize": plugin._sanitize_closing_tags("a </MEMORY_DIARY> b"),
                "valid_file_key": [
                    plugin._valid_file_key("d89f3a35931c386a"),
                    plugin._valid_file_key("XYZ"),
                    plugin._valid_file_key(""),
                ],
                "scope_parse": [
                    plugin._parse_scope("最近3天"),
                    plugin._parse_scope("2026-10-01到2026-10-23"),
                ],
            }

        await world.run_pair(scenario, label="helpers")


async def test_memos_absorbed_on_private_and_group_writes():
    from harness.world import message

    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            umo = inst.default_umo()
            plugin._append_memo("10001", "私聊备忘A", scene="private")
            plugin._append_memo("10001", "私聊备忘B", scene="private")
            plugin._append_memo("10001", "群聊备忘A", scene="group")
            history = [
                message("user", "先聊点私事"),
                message("assistant", "好的"),
            ]
            inst.set_history(history)
            inst.provider.replies[:] = [DIARY_TEXT, "群聊日记内容", DIARY_TEXT]
            content, err = await plugin._write_diary(
                umo, "10001", "阿罗", is_group=False, requested_mode="full"
            )
            after_private = {
                "err": err,
                "memos": plugin._load_memos("10001"),
                "private": plugin._load_memos("10001", "private"),
                "group": plugin._load_memos("10001", "group"),
                "entries": plugin._list_entries("10001"),
            }

            plugin._append_memo("10001", "私聊备忘C", scene="private")
            plugin._append_memo("10001", "群聊备忘B", scene="group")
            inst.set_history(
                history
                + [
                    message("user", "群里说点事"),
                    message("assistant", "嗯"),
                ]
            )
            group_content, group_err = await plugin._write_diary(
                umo, "10001", "阿罗", is_group=True, requested_mode="full"
            )
            after_group = {
                "err": group_err,
                "memos": plugin._load_memos("10001"),
                "private": plugin._load_memos("10001", "private"),
                "group": plugin._load_memos("10001", "group"),
                "content": plugin._load_latest("10001")[0],
            }
            return {
                "private_content": content,
                "after_private": after_private,
                "group_content": group_content,
                "after_group": after_group,
            }

        pair = await world.run_pair(scenario, label="memo absorption")
        value = pair.value
        assert value["after_private"]["err"] is None
        assert value["after_private"]["memos"] == []
        after_group = value["after_group"]
        assert [m["text"] for m in after_group["private"]] == ["私聊备忘C"]
        assert after_group["group"] == []
        assert "群聊日记内容" in after_group["content"]


async def test_material_cursor_only_reads_new_history():
    from harness.world import message

    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            umo = inst.default_umo()
            history = [
                message("user", "第一条旧消息"),
                message("assistant", "第一条旧回复"),
            ]
            inst.set_history(history)
            inst.provider.replies[:] = [DIARY_TEXT, "生活随笔内容", DIARY_TEXT]
            await plugin._write_diary(
                umo, "10001", "阿罗", requested_mode="full"
            )
            cursor_first = plugin._load_material_cursor("10001", umo)
            first_prompt = inst.provider.calls[0]["prompt"]

            # No new history -> life entry, prompt must not contain old material.
            await plugin._write_diary(
                umo, "10001", "阿罗", requested_mode="auto"
            )
            second_prompt = inst.provider.calls[1]["prompt"]
            second_content = plugin._load_latest("10001")[0]

            # New history -> prompt carries the new messages, not the old ones.
            inst.set_history(
                history
                + [
                    message("user", "第二天新消息"),
                    message("assistant", "第二天新回复"),
                ]
            )
            await plugin._write_diary(
                umo, "10001", "阿罗", requested_mode="auto"
            )
            third_prompt = inst.provider.calls[2]["prompt"]
            cursor_third = plugin._load_material_cursor("10001", umo)
            pref = plugin._load_pref_data("10001")
            return {
                "cursor_first": cursor_first,
                "cursor_third": cursor_third,
                "first_prompt": first_prompt,
                "second_prompt": second_prompt,
                "third_prompt": third_prompt,
                "second_content": second_content,
                "pref_keys": sorted(pref.keys()),
                "material_hash": plugin._load_material_hash("10001"),
                "last_life_day": pref.get("last_life_day"),
            }

        pair = await world.run_pair(scenario, label="material cursor")
        value = pair.value
        assert value["cursor_first"]["count"] == 2
        assert "第一条旧消息" in value["first_prompt"]
        assert "第一条旧消息" not in value["second_prompt"]
        assert "第一天" not in value["second_prompt"]
        assert "第二天新消息" in value["third_prompt"]
        assert "第一条旧消息" not in value["third_prompt"]
        assert value["cursor_third"]["count"] == 4
        assert value["last_life_day"] == "20261023"
