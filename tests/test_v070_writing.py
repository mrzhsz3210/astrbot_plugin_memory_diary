# -*- coding: utf-8 -*-
"""v0.7.0 A 节：解析契约、不写决策、锚点重试与显式记忆。"""

from __future__ import annotations

from harness.fakes import make_event
from harness.world import DIARY_TEXT, World, message
from memory_diary.constants import DIARY_SYSTEM_PROMPT, ERR_NO_DIARY
from memory_diary.writing import (
    ensure_explicit_notes,
    is_explicit_message,
    parse_diary_result,
)

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"


def _event(text: str = "/日记"):
    return make_event(text, sender_id=SENDER, sender_name="阿罗", umo=UMO)


def test_diary_result_strict_no_diary_and_explicit_contract():
    result = parse_diary_result("【无日记】没有新经历")
    assert result.status == "no_diary"
    assert result.reason == "没有新经历"

    assert parse_diary_result("【无日记】原因\n还有正文").status == "ok"
    assert parse_diary_result("【无日记】<self>私人片段</self>").status == "ok"
    assert parse_diary_result("【无日记】" + "长" * 31).status == "ok"

    parsed = parse_diary_result(
        "开头<explicit>别忘  周六\n上午九点</explicit>结尾<explicit>你记错了，是周二</explicit>"
    )
    assert parsed.explicit_notes == ["别忘  周六\n上午九点", "你记错了，是周二"]
    assert "<explicit>" not in parsed.text
    assert "别忘  周六\n上午九点" in parsed.text

    forced = ensure_explicit_notes(result, ["别忘周六上午九点讨论发布"])
    assert forced.status == "ok"
    assert forced.explicit_notes == ["别忘周六上午九点讨论发布"]


def test_explicit_trigger_detection_prefers_recall():
    positives = (
        "记住我周六考试",
        "别忘带文件",
        "以后注意先问时间",
        "你记错了，是周二",
        "不对，是周二",
        "我记得你上次说过这件事",
    )
    assert all(is_explicit_message(text) for text in positives)
    assert not is_explicit_message("今天天气不错")
    assert not is_explicit_message("")


async def test_no_diary_writes_only_internal_provenance_and_no_chat_output():
    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history(
            [message("user", "随便聊了几句"), message("assistant", "嗯")]
        )
        inst.provider.replies[:] = ["【无日记】没有值得长期保留的新经历"]

        texts = await inst.run_handler("write_diary_cmd", _event())
        assert texts == []
        assert inst.entries(SENDER) == []
        provenance, status = plugin.load_provenance(SENDER)
        assert status == "ok"
        assert provenance["no_write_streak"] == 1
        assert len(provenance["no_write"]) == 1
        record = provenance["no_write"][0]
        assert record["at"].startswith("2026-10-23")
        assert record["reason"] == "没有值得长期保留的新经历"
        assert record["cleanup_after"] is None
        assert "【无日记】" not in plugin._assemble_inject(SENDER)[0]


async def test_no_diary_is_silent_on_sentinel_and_scheduled_paths():
    async with World(
        config={
            "use_persona": False,
            "context_token_limit": 60000,
            "remind_ratio": 0.75,
            "auto_ratio": 0.9,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "普通闲聊")])
        inst.provider.replies[:] = ["【无日记】没有新经历"]
        event = _event("你好")
        from astrbot.api.provider import ProviderRequest

        req = ProviderRequest(prompt="x" * 99000, contexts=[], system_prompt="SYS")
        await plugin.on_llm_request(event, req)
        from harness.world import wait_for_diary_tasks

        await wait_for_diary_tasks(plugin)
        assert event.result_texts() == []
        assert inst.entries(SENDER) == []


async def test_full_diary_missing_anchor_retries_once_then_degrades_to_brief():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "随便聊聊")])
        inst.provider.replies[:] = ["只是一些空泛感受。", "依旧只是一些想法。"]

        diary, err = await plugin._write_diary(
            UMO, SENDER, "阿罗", requested_mode="full"
        )
        assert err is None
        assert diary == "依旧只是一些想法。"
        assert len(inst.provider.calls) == 2
        content, tampered = plugin._load_entry(SENDER, "diaries/20261023.md")
        assert not tampered
        assert "简记" in content
        assert "完整" not in content


async def test_full_diary_anchor_retry_success_does_not_loop():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "随便聊聊")])
        inst.provider.replies[:] = ["只是一些空泛感受。", DIARY_TEXT]

        diary, err = await plugin._write_diary(
            UMO, SENDER, "阿罗", requested_mode="full"
        )
        assert err is None
        assert diary == DIARY_TEXT
        assert len(inst.provider.calls) == 2
        content, _tampered = plugin._load_entry(SENDER, "diaries/20261023.md")
        assert "完整" in content


async def test_explicit_user_message_survives_model_no_diary_decision():
    original = "别忘周六上午九点讨论发布"
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", original)])
        inst.provider.replies[:] = ["【无日记】没有新经历"]

        diary, err = await plugin._write_diary(
            UMO, SENDER, "阿罗", requested_mode="full"
        )
        assert err is None and err != ERR_NO_DIARY
        assert original in diary
        provenance, status = plugin.load_provenance(SENDER)
        assert status == "ok"
        assert provenance["no_write"] == []
        assert provenance["entries"]["20261023.md"]["explicit"] == [original]
        assert "<explicit>" not in diary


async def test_old_four_sections_and_new_free_structure_can_be_injected_together():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
        )
        plugin._save_diary(
            SENDER,
            "【发布前的一刻】\n我们在晚上九点完成了发布。\n\n【下次】\n先跑回归。",
            sender_name="阿罗",
            entry_name_hint="20261024",
            origin="private",
            mode="full",
        )
        plugin._save_pref(SENDER, "all")

        text, tampered, names = plugin._assemble_inject(SENDER)
        assert not tampered
        assert len(names) == 2
        assert "【涵盖时间】" in text
        assert "【发布前的一刻】" in text
        assert "【下次】" in text


def test_full_diary_prompt_allows_natural_paragraphs_without_repeated_headings():
    assert "575~1035 字" in DIARY_SYSTEM_PROMPT
    assert "时间维度只用一个简短段落" in DIARY_SYSTEM_PROMPT
    assert "其余维度可按语义自然分成多个自然段" in DIARY_SYSTEM_PROMPT
    assert "同一章节有多个自然段时不要重复标题" in DIARY_SYSTEM_PROMPT
    assert "不要一句一段" in DIARY_SYSTEM_PROMPT
    assert "给你实际写到的每一段起" not in DIARY_SYSTEM_PROMPT


async def test_multiparagraph_diary_blank_lines_are_preserved_end_to_end():
    diary = (
        "【这几天】\n今天上午九点开始讨论发布。\n\n"
        "上午的检查结束后，下午又继续核对迁移包。\n\n"
        "【我眼中的她】\n她很在意旧数据是否原样保留。\n\n"
        "遇到不确定的地方，她会先停下来确认。\n\n"
        "<self>我也想把这些细节好好留住。</self>\n\n"
        "<rewrite_self>false</rewrite_self>"
    )
    parsed = parse_diary_result(diary, require_anchor=True)
    assert parsed.status == "ok"
    assert parsed.text == diary.rsplit("\n\n<rewrite_self>", 1)[0]

    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(
            SENDER, parsed.text, sender_name="阿罗", origin="private", mode="full"
        )
        content, tampered = plugin._load_entry(SENDER, "diaries/20261023.md")
        assert not tampered
        assert parsed.text in content
        assert content.count("【我眼中的她】") == 1
        assert "保留。\n\n遇到" in content


async def test_writing_provenance_commits_atomically_with_diary():
    import json

    original = "别忘周六上午九点讨论发布"
    self_text = "我独自整理了一会儿书桌。"
    response = (
        f"【今天】\n她明确说<explicit>{original}</explicit>。\n"
        f"<self>{self_text}</self>\n\n<rewrite_self>false</rewrite_self>"
    )
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", original)])
        inst.provider.replies[:] = [response]
        original_write = plugin._write_zip_atomic
        committed = []

        def observe_write(path, entries):
            if "diaries/20261023.md" in entries:
                committed.append({name: bytes(raw) for name, raw in entries.items()})
            return original_write(path, entries)

        def forbidden_post_write(*args):
            raise AssertionError("正文保存后不应再单独写 self/explicit 来源")

        plugin._write_zip_atomic = observe_write
        plugin.record_self_provenance = forbidden_post_write
        plugin.record_explicit_provenance = forbidden_post_write
        diary, err = await plugin._write_diary(
            UMO, SENDER, "阿罗", requested_mode="full"
        )
        assert err is None and self_text in diary and original in diary
        assert len(committed) == 1
        entries = committed[0]
        provenance = json.loads(entries[plugin.PROVENANCE_ENTRY].decode("utf-8"))
        record = provenance["entries"]["20261023.md"]
        assert record["self"] == [self_text]
        assert record["explicit"] == [original]
        assert plugin.PROVENANCE_SIG_ENTRY in entries
        loaded, status = plugin.load_provenance(SENDER)
        assert status == "ok"
        assert loaded["entries"]["20261023.md"]["self"] == [self_text]
        assert loaded["entries"]["20261023.md"]["explicit"] == [original]
