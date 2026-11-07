# -*- coding: utf-8 -*-
"""v0.7.0 C 节：current_self 签名对象、重写流程、锚点与注入。"""

from __future__ import annotations

import json
import zipfile

from harness.world import DIARY_TEXT, World, message
from memory_diary.token_estimator import estimate_text

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"
CONFIG = {
    "use_persona": False,
    "current_self_enabled": True,
    "current_self_rewrite_every": 4,
    "diary_op_interval": 0,
}


def _output(text: str, anchors=None) -> str:
    return json.dumps(
        {"text": text, "anchors": list(anchors or [])}, ensure_ascii=False
    )


def _raw_output(text: str, anchors) -> str:
    return json.dumps({"text": text, "anchors": anchors}, ensure_ascii=False)


def _save_self(plugin, text: str, anchors=None, since_rewrite: int = 0):
    data, _status = plugin.load_current_self(SENDER)
    revision = int((data["current"] or {}).get("revision") or 0) + 1
    data["current"] = {
        "revision": revision,
        "text": text,
        "updated_at": "2026-10-23T20:30:00",
        "anchors": list(anchors or []),
    }
    data["since_rewrite"] = since_rewrite
    assert plugin.save_current_self(SENDER, data)


async def test_disabled_current_self_has_zero_calls_and_no_injection():
    async with World(config={"current_self_enabled": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        assert await plugin._update_current_self(
            SENDER, UMO, DIARY_TEXT, "diaries/20261023.md"
        ) is False
        assert inst.provider.calls == []
        assert plugin._current_self_inject_block(SENDER) == ""
        assert plugin.load_current_self(SENDER)[1] == "missing"


async def test_first_write_full_rewrite_is_signed_and_has_source_metadata():
    async with World(config=CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "今天上午九点讨论发布")])
        inst.provider.replies[:] = [
            DIARY_TEXT,
            _output("我们正在准备发布。", ["上午九点讨论发布"]),
        ]

        diary, err = await plugin._write_diary(
            UMO, SENDER, "阿罗", requested_mode="full"
        )
        assert err is None and "<rewrite_self>" not in diary
        data, status = plugin.load_current_self(SENDER)
        assert status == "ok"
        assert data["current"]["text"] == "我们正在准备发布。"
        assert data["since_rewrite"] == 0
        anchor = data["current"]["anchors"][0]
        assert anchor["source"] == {
            "model": "unknown",
            "persona": "",
            "entry": "diaries/20261023.md",
        }
        with zipfile.ZipFile(plugin._diary_path(SENDER)) as z:
            assert plugin.CURRENT_SELF_ENTRY in z.namelist()
            assert plugin.CURRENT_SELF_SIG_ENTRY in z.namelist()


async def test_trigger_marker_forces_full_rewrite_and_never_enters_diary():
    async with World(config=CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "今天讨论发布")])
        inst.provider.replies[:] = [DIARY_TEXT, _output("第一版")]
        await plugin._write_diary(UMO, SENDER, "阿罗", requested_mode="full")

        inst.set_history(
            [
                message("user", "今天讨论发布"),
                message("user", "你记错了，发布时间改成周日"),
            ]
        )
        inst.provider.replies[:] = [
            DIARY_TEXT + "\n<rewrite_self>true</rewrite_self>",
            _output("第二版，时间已纠正", ["发布时间改成周日"]),
        ]
        diary, err = await plugin._write_diary(
            UMO, SENDER, "阿罗", requested_mode="brief"
        )
        assert err is None
        assert "<rewrite_self>" not in diary
        assert "整篇重写" in inst.provider.calls[-1]["prompt"]
        assert inst.provider.calls[-1]["kwargs"].get("max_tokens") == 1200
        data, _status = plugin.load_current_self(SENDER)
        assert data["current"]["text"] == "第二版，时间已纠正"
        assert data["since_rewrite"] == 0
        assert len(data["history"]) == 1
        assert len(data["current"]["anchors"]) == 1


async def test_counter_full_rewrite_skips_rolling_call():
    async with World(config=CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        _save_self(plugin, "上一版", since_rewrite=3)
        inst.provider.replies[:] = [_output("计数器触发的新版")]

        assert await plugin._update_current_self(
            SENDER, UMO, DIARY_TEXT, "diaries/20261023.md", trigger=False
        )
        assert len(inst.provider.calls) == 1
        assert "整篇重写" in inst.provider.calls[0]["prompt"]
        data, _status = plugin.load_current_self(SENDER)
        assert data["since_rewrite"] == 0


async def test_anchors_only_append_and_superseded_anchor_is_not_injected():
    async with World(config=CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        source = {"model": "m1", "persona": "p1", "entry": "diaries/20261023.md"}
        first = {"id": "a1", "text": "上午九点讨论发布", "source": source}
        _save_self(plugin, "当前交接", [first])
        data, _status = plugin.load_current_self(SENDER)
        assert plugin.supersede_current_self_anchor(
            SENDER, "a1", "user", "时间已纠正"
        )
        after, status = plugin.load_current_self(SENDER)
        assert status == "ok"
        assert after["current"]["anchors"][0]["text"] == "上午九点讨论发布"
        assert after["current"]["anchors"][0]["superseded_by"]["reason"] == "时间已纠正"
        assert "上午九点讨论发布" not in plugin._current_self_inject_block(SENDER)
        assert data["current"]["anchors"][0].get("superseded_by") is None


async def test_failed_update_preserves_previous_valid_version_byte_for_byte():
    async with World(config=CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        _save_self(plugin, "上一版仍然有效")
        path = plugin._diary_path(SENDER)
        with zipfile.ZipFile(path) as z:
            before = z.read(plugin.CURRENT_SELF_ENTRY)
        inst.provider.fail_with = "boom"

        assert await plugin._update_current_self(
            SENDER, UMO, DIARY_TEXT, "diaries/20261023.md"
        ) is False
        with zipfile.ZipFile(path) as z:
            assert z.read(plugin.CURRENT_SELF_ENTRY) == before
        data, status = plugin.load_current_self(SENDER)
        assert status == "ok" and data["current"]["text"] == "上一版仍然有效"


async def test_history_keeps_latest_five_replaceable_versions():
    async with World(config=CONFIG) as world:
        plugin = world.refactor.plugin
        for index in range(7):
            _save_self(plugin, f"第{index}版")
        data, status = plugin.load_current_self(SENDER)
        assert status == "ok"
        assert len(data["history"]) == 5
        assert [item["text"] for item in data["history"]] == [
            "第1版", "第2版", "第3版", "第4版", "第5版"
        ]
        assert data["current"]["text"] == "第6版"


async def test_current_self_is_prioritized_in_light_injection_within_budget():
    async with World(config=CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, DIARY_TEXT + "长" * 2500, origin="private", mode="full")
        _save_self(plugin, "这是当前最重要的交接内容。")
        text, tampered, names = plugin._assemble_inject(SENDER, lightweight=True)
        assert not tampered
        assert text.startswith("【当前的我】\n这是当前最重要的交接内容。")
        assert estimate_text(text) <= 2400
        assert names == ["diaries/20261023.md"]


async def test_current_self_text_is_capped_and_tamper_is_rejected():
    async with World(config=CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        _save_self(plugin, "长" * 1200)
        data, status = plugin.load_current_self(SENDER)
        assert status == "ok"
        assert len(data["current"]["text"]) == 900
        path = plugin._diary_path(SENDER)
        with zipfile.ZipFile(path) as z:
            entries = {name: z.read(name) for name in z.namelist()}
        entries[plugin.CURRENT_SELF_ENTRY] = b'{"current": null}'
        plugin._write_zip_atomic(path, entries)
        assert plugin.load_current_self(SENDER)[1] == "tampered"
        assert plugin._current_self_inject_block(SENDER) == ""


async def test_pending_items_survive_rolling_and_full_rewrite_with_valid_signature():
    for full in (False, True):
        async with World(config=CONFIG) as world:
            inst = world.refactor
            plugin = inst.plugin
            plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
            _save_self(plugin, "上一版", since_rewrite=0 if full else 3)
            assert plugin.append_current_self_pending(
                SENDER, "用户确认周日发布", "用户确认"
            )
            inst.provider.replies[:] = [_output("更新后的交接信")]

            assert await plugin._update_current_self(
                SENDER, UMO, DIARY_TEXT, "diaries/20261023.md", trigger=full
            )
            data, status = plugin.load_current_self(SENDER)
            assert status == "ok"
            assert data["current"]["pending_items"][0]["text"] == "用户确认周日发布"
            assert "用户确认周日发布" in plugin._current_self_inject_block(SENDER)
            with zipfile.ZipFile(plugin._diary_path(SENDER)) as z:
                raw = z.read(plugin.CURRENT_SELF_ENTRY)
                actual = z.read(plugin.CURRENT_SELF_SIG_ENTRY).decode().strip()
            assert plugin._current_self_sig(SENDER, raw) == actual


async def test_pending_items_survive_normalization_and_injection_budget_does_not_delete():
    async with World(config=CONFIG) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        _save_self(plugin, "交接正文" + "长" * 897)
        for index in range(55):
            assert plugin.append_current_self_pending(
                SENDER, f"已确认事项{index}", "用户确认"
            )
        before, status = plugin.load_current_self(SENDER)
        assert status == "ok" and len(before["current"]["pending_items"]) == 55
        plugin._current_self_inject_block(SENDER, token_cap=1)
        after, status = plugin.load_current_self(SENDER)
        assert status == "ok"
        assert after["current"]["pending_items"] == before["current"]["pending_items"]


async def test_export_import_preserves_current_self_with_signature():
    async with World(config=CONFIG) as src_world, World(config=CONFIG) as dst_world:
        src = src_world.refactor.plugin
        src._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        _save_self(src, "随迁移包带走的交接信")
        bundle = src._build_export_bundle(
            SENDER, src._diary_path(SENDER), src._list_entries(SENDER)
        )
        try:
            owner, key, entries, images, annotations, provenance, current_self, materials, anchor_archive = (
                src._verify_bundle(bundle)
            )
            assert current_self is not None
            dst = dst_world.refactor.plugin
            dst._install_bundle(
                SENDER,
                owner,
                key,
                entries,
                images=images,
                annotations=annotations,
                provenance=provenance,
                current_self=current_self,
                materials=materials,
                anchor_archive=anchor_archive,
            )
            loaded, status = dst.load_current_self(SENDER)
            assert status == "ok"
            assert loaded["current"]["text"] == "随迁移包带走的交接信"
        finally:
            bundle.unlink(missing_ok=True)


async def test_anchor_active_window_archives_original_records_with_signature():
    async with World(config=CONFIG) as world:
        plugin = world.refactor.plugin
        source = {"model": "m", "persona": "p", "entry": "diaries/20261023.md"}
        anchors = [
            {"id": f"a{index}", "text": f"原始锚点{index}", "source": source}
            for index in range(205)
        ]
        anchors[0]["superseded_by"] = {"by": "user", "reason": "纠正", "at": "2026-10-23"}
        _save_self(plugin, "活动窗口", anchors)
        data, status = plugin.load_current_self(SENDER)
        assert status == "ok" and len(data["current"]["anchors"]) == 200
        assert data["current"]["anchors"][0]["id"] == "a5"
        archive, archive_status = plugin.load_current_self_anchor_archive(SENDER)
        assert archive_status == "ok" and len(archive["items"]) == 5
        assert archive["items"][0]["text"] == "原始锚点0"
        assert archive["items"][0]["superseded_by"]["reason"] == "纠正"
        assert archive["items"][0]["archived_at"] and archive["items"][0]["batch"]
        injected = plugin._current_self_inject_block(SENDER)
        assert "原始锚点0" not in injected
        prompt_anchors = plugin._anchors_for_rewrite_prompt(anchors, token_cap=30)
        assert len(prompt_anchors) < len(anchors)
        assert prompt_anchors[-1]["id"] == "a204"


async def test_anchor_archive_export_import_roundtrip():
    async with World(config=CONFIG) as src_world, World(config=CONFIG) as dst_world:
        src = src_world.refactor.plugin
        src._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        source = {"model": "m", "persona": "", "entry": "diaries/20261023.md"}
        _save_self(src, "归档迁移", [
            {"id": f"a{i}", "text": f"锚点{i}", "source": source} for i in range(201)
        ])
        bundle = src._build_export_bundle(SENDER, src._diary_path(SENDER), src._list_entries(SENDER))
        try:
            owner, key, entries, images, annotations, provenance, current_self, materials, archive = src._verify_bundle(bundle)
            assert archive is not None
            dst = dst_world.refactor.plugin
            dst._install_bundle(
                SENDER, owner, key, entries, images=images, annotations=annotations,
                provenance=provenance, current_self=current_self, materials=materials,
                anchor_archive=archive,
            )
            loaded, status = dst.load_current_self_anchor_archive(SENDER)
            assert status == "ok" and len(loaded["items"]) == 1
            assert loaded["items"][0]["text"] == "锚点0"
        finally:
            bundle.unlink(missing_ok=True)


async def test_current_self_anchors_accept_one_string_and_reject_bad_structures():
    async with World(config=CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        parsed = plugin._parse_current_self_output(
            _raw_output("新版", "用户今天发布")
        )
        assert parsed["anchors"] == ["用户今天发布"]
        assert plugin._parse_current_self_output(
            _output("新版", ["上午九点", "今天发布"])
        )["anchors"] == ["上午九点", "今天发布"]
        assert plugin._parse_current_self_output(
            json.dumps({"text": "新版"})
        )["anchors"] == []
        for invalid in ({"anchor": "x"}, 7, ["有效", 7]):
            assert plugin._parse_current_self_output(
                _raw_output("不得覆盖", invalid)
            ) is None

        plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        _save_self(plugin, "上一版仍然有效")
        inst.provider.replies[:] = [_raw_output("不得覆盖", ["有效", {"bad": True}])]
        assert await plugin._update_current_self(
            SENDER, UMO, DIARY_TEXT, "diaries/20261023.md", trigger=True
        ) is False
        data, status = plugin.load_current_self(SENDER)
        assert status == "ok" and data["current"]["text"] == "上一版仍然有效"

        inst.provider.replies[:] = [_raw_output("字符串锚点版", "用户今天发布")]
        assert await plugin._update_current_self(
            SENDER, UMO, DIARY_TEXT, "diaries/20261023.md", trigger=True
        )
        data, status = plugin.load_current_self(SENDER)
        assert status == "ok"
        assert [anchor["text"] for anchor in data["current"]["anchors"]] == [
            "用户今天发布"
        ]


async def test_pending_injection_prioritizes_text_latest_items_and_anchors():
    async with World(config=CONFIG) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        source = {"model": "m", "persona": "p", "entry": "diaries/20261023.md"}
        _save_self(plugin, "当前正文必须保留", [
            {"id": "old", "text": "旧锚点", "source": source},
            {"id": "new", "text": "最新有效锚点", "source": source},
        ])
        for index in range(20):
            assert plugin.append_current_self_pending(
                SENDER, f"确认事项{index}：" + "长" * 80, "用户确认"
            )
        before, status = plugin.load_current_self(SENDER)
        assert status == "ok"
        block = plugin._current_self_inject_block(SENDER, token_cap=500)
        assert block.startswith("【当前的我】\n当前正文必须保留")
        assert "确认事项19" in block
        assert "确认事项0：" not in block
        assert "最新有效锚点" in block
        assert "其余仍完整保存在交接信中" in block
        assert estimate_text(block) <= 500
        small = plugin._current_self_inject_block(SENDER, token_cap=1)
        assert small and estimate_text(small) <= 1
        after, status = plugin.load_current_self(SENDER)
        assert status == "ok"
        assert after["current"]["pending_items"] == before["current"]["pending_items"]


async def test_55_pending_items_survive_rolling_then_full_rewrite():
    async with World(config=CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        _save_self(plugin, "上一版", since_rewrite=0)
        for index in range(55):
            assert plugin.append_current_self_pending(
                SENDER, f"完整保留事项{index}", "用户确认"
            )
        for full in (False, True):
            inst.provider.replies[:] = [_output("滚段新版" if not full else "整篇新版")]
            assert await plugin._update_current_self(
                SENDER, UMO, DIARY_TEXT, "diaries/20261023.md", trigger=full
            )
            data, status = plugin.load_current_self(SENDER)
            assert status == "ok"
            assert len(data["current"]["pending_items"]) == 55
            assert data["current"]["pending_items"][0]["text"] == "完整保留事项0"
            assert data["current"]["pending_items"][-1]["text"] == "完整保留事项54"
            assert "整篇重写" in inst.provider.calls[-1]["prompt"] if full else True
