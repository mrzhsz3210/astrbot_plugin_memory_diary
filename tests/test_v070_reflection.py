# -*- coding: utf-8 -*-
"""v0.7.0 E 节：四类内容、事实级确认、否决与交接信回写。"""

from __future__ import annotations

import json
import zipfile

from annotation_testkit import (
    SENDER,
    UMO,
    ann_config,
    long_diary,
    make_annotation,
    seed_annotations,
    seed_diary,
)
from harness.world import World


def _payload(records) -> str:
    return json.dumps(
        {"title": "重新理解", "annotations": records}, ensure_ascii=False
    )


def _record(content_type="fact", reflection_kind="fact"):
    return {
        "anchor": "喜欢在深夜聊天",
        "note": "这里需要重新确认她的作息。" if reflection_kind == "fact" else "当时证据有限。",
        "is_question": False,
        "content_type": content_type,
        "reflection_kind": reflection_kind,
    }


async def test_generation_marks_four_types_and_fact_reflection_pending():
    async with World(
        config=ann_config(
            reflection_annotation_enabled=True, current_self_enabled=True
        )
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        seed_annotations(plugin, [])
        seed_diary(plugin, long_diary())
        text, _tampered, names = plugin._assemble_inject(SENDER)
        inst.provider.replies[:] = [
            _payload([
                _record("observation", "ordinary"),
                _record("fact", "fact"),
            ])
        ]
        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 2
        assert "content_type" in inst.provider.calls[0]["prompt"]
        for marker in ("fact", "observation", "speculation", "self"):
            assert marker in inst.provider.calls[0]["prompt"]

        data, status = plugin.load_annotations(SENDER)
        assert status == "ok"
        ordinary, fact = data["annotations"]
        assert ordinary["status"] == "open"
        assert ordinary["content_type"] == "observation"
        assert fact["status"] == "pending_confirmation"
        assert fact["reflection_kind"] == "fact"
        block = plugin._annotation_inject_block(SENDER)
        assert ordinary["note"] in block
        assert fact["note"] not in block
        assert "待确认" in plugin.annotations_view_text(SENDER)


async def test_reflection_switch_off_restores_v062_generation_shape():
    async with World(
        config=ann_config(reflection_annotation_enabled=False)
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        seed_annotations(plugin, [])
        text, _tampered, names = plugin._assemble_inject(SENDER)
        inst.provider.replies[:] = [_payload([_record()])]
        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 1
        assert "记忆反思分类" not in inst.provider.calls[0]["prompt"]
        data, _status = plugin.load_annotations(SENDER)
        item = data["annotations"][0]
        assert item["status"] == "open"
        assert "content_type" not in item
        assert "reflection_kind" not in item


async def test_fact_reflection_confirmation_writes_current_self_not_diary():
    async with World(
        config=ann_config(
            reflection_annotation_enabled=True, current_self_enabled=True
        )
    ) as world:
        plugin = world.refactor.plugin
        record = make_annotation(1, status="pending_confirmation")
        record["content_type"] = "fact"
        record["reflection_kind"] = "fact"
        seed_annotations(plugin, [record])
        diary_path = plugin._diary_path(SENDER)
        with zipfile.ZipFile(diary_path) as z:
            diary_before = z.read("diaries/20261023.md")

        assert plugin._annotation_inject_block(SENDER) == ""
        assert plugin.load_current_self(SENDER)[1] == "missing"
        assert plugin.apply_annotation_reply(
            SENDER,
            record["id"],
            {"reply_title": "确认的新认识", "summary": "她的作息需要重新确认。"},
        )
        data, _status = plugin.load_annotations(SENDER)
        assert data["annotations"][0]["status"] == "resolved"
        current, status = plugin.load_current_self(SENDER)
        assert status == "ok"
        item = current["current"]["pending_items"][0]
        assert item["text"] == "她的作息需要重新确认。"
        assert item["source"] == "diaries/20261023.md"
        assert "她的作息需要重新确认" in plugin._current_self_inject_block(SENDER)
        with zipfile.ZipFile(diary_path) as z:
            assert z.read("diaries/20261023.md") == diary_before


async def test_rejected_reflection_is_kept_but_never_active():
    async with World(
        config=ann_config(
            reflection_annotation_enabled=True, current_self_enabled=True
        )
    ) as world:
        plugin = world.refactor.plugin
        record = make_annotation(1, status="pending_confirmation")
        record["reflection_kind"] = "fact"
        seed_annotations(plugin, [record])
        assert plugin.reject_annotation(SENDER, record["id"])
        data, status = plugin.load_annotations(SENDER)
        assert status == "ok"
        item = data["annotations"][0]
        assert item["status"] == "rejected"
        assert item["note"] == record["note"]
        assert plugin._select_inject_annotations(data) == []
        assert plugin._current_self_inject_block(SENDER) == ""
        assert "已否决" in plugin.annotations_view_text(SENDER)


async def test_switch_off_does_not_write_confirmed_reply_to_current_self():
    async with World(
        config=ann_config(
            reflection_annotation_enabled=False, current_self_enabled=True
        )
    ) as world:
        plugin = world.refactor.plugin
        record = make_annotation(1)
        seed_annotations(plugin, [record])
        assert plugin.apply_annotation_reply(
            SENDER,
            record["id"],
            {"reply_title": "结论", "summary": "普通旧版写回。"},
        )
        assert plugin.load_current_self(SENDER)[1] == "missing"
