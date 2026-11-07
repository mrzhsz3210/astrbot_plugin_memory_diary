# -*- coding: utf-8 -*-
"""v0.7.0 B 节：哨兵后台写作、轻量注入与双预算。"""

from __future__ import annotations

import asyncio

from astrbot.api.provider import ProviderRequest

from harness.fakes import FakeLLMResponse, make_event
from harness.world import DIARY_TEXT, World, message, wait_for_diary_tasks
from memory_diary.token_estimator import estimate_text

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"

WATCH_CONFIG = {
    "use_persona": False,
    "auto_write_notify": False,
    "context_token_limit": 60000,
    "remind_ratio": 0.75,
    "auto_ratio": 0.9,
}


def _event(text: str = "你好"):
    return make_event(text, sender_id=SENDER, sender_name="阿罗", umo=UMO)


def _request(prompt: str = "你好"):
    return ProviderRequest(
        prompt=prompt,
        contexts=[{"role": "user", "content": "你好"}],
        system_prompt="SYS",
    )


def _extra_texts(req) -> list[str]:
    return [getattr(part, "text", str(part)) for part in req.extra_user_content_parts]


def _slow_provider(inst):
    started = asyncio.Event()
    release = asyncio.Event()

    async def text_chat(prompt=None, system_prompt=None, **kwargs):
        inst.provider.calls.append(
            {"prompt": prompt or "", "system_prompt": system_prompt or ""}
        )
        started.set()
        await release.wait()
        return FakeLLMResponse(DIARY_TEXT)

    inst.provider.text_chat = text_chat
    return started, release


async def test_sentinel_does_not_block_chat_and_finishes_in_background():
    async with World(config=WATCH_CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "历史消息")])
        started, release = _slow_provider(inst)

        await asyncio.wait_for(
            plugin.on_llm_request(_event(), _request(prompt="x" * 99000)),
            timeout=0.2,
        )
        assert inst.entries(SENDER) == []
        assert SENDER in plugin._writing
        await asyncio.wait_for(started.wait(), timeout=0.2)

        release.set()
        await wait_for_diary_tasks(plugin)
        assert inst.entries(SENDER) == ["diaries/20261023.md"]
        assert SENDER not in plugin._writing


async def test_sentinel_generation_is_scheduled_only_once():
    async with World(config=WATCH_CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "历史消息")])
        started, release = _slow_provider(inst)

        await plugin.on_llm_request(_event(), _request(prompt="x" * 99000))
        await started.wait()
        await plugin.on_llm_request(_event(), _request(prompt="x" * 99000))
        assert len(inst.provider.calls) == 1
        assert len(plugin._diary_tasks) == 1

        release.set()
        await wait_for_diary_tasks(plugin)
        assert len(inst.provider.calls) == 1


async def test_manual_write_is_rejected_while_sentinel_owns_zip():
    async with World(config=WATCH_CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "历史消息")])
        started, release = _slow_provider(inst)

        await plugin.on_llm_request(_event(), _request(prompt="x" * 99000))
        await started.wait()
        texts = await inst.run_handler("write_diary_cmd", _event("/日记"))
        assert len(texts) == 1
        assert "正在" in texts[0]
        assert len(inst.provider.calls) == 1

        release.set()
        await wait_for_diary_tasks(plugin)
        content, tampered = plugin._load_entry(SENDER, "diaries/20261023.md")
        assert not tampered
        assert DIARY_TEXT in content


async def test_terminate_cancels_and_awaits_sentinel_task():
    async with World(config=WATCH_CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "历史消息")])
        started, _release = _slow_provider(inst)

        await plugin.on_llm_request(_event(), _request(prompt="x" * 99000))
        await started.wait()
        task = next(iter(plugin._diary_tasks))
        await plugin.terminate()
        assert task.cancelled()
        assert plugin._diary_tasks == set()
        assert plugin._writing == set()
        assert inst.entries(SENDER) == []


async def test_sentinel_failure_is_consumed_and_notifies():
    async with World(config=WATCH_CONFIG) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "历史消息")])
        inst.provider.fail_with = "boom"
        event = _event()

        await plugin.on_llm_request(event, _request(prompt="x" * 99000))
        await wait_for_diary_tasks(plugin)
        assert inst.entries(SENDER) == []
        assert SENDER not in plugin._writing
        assert len(event.result_texts()) == 1
        assert "自动写日记失败" in event.result_texts()[0]


async def test_auto_inject_is_excerpt_and_manual_inject_remains_full():
    content = (
        "【涵盖时间】\n2026-10-23 下午。\n\n"
        "【只应留在全文里的细节】\n这段普通观察不应进入自动轻量注入。\n\n"
        "【原话】\n她说「别忘周六上午九点讨论发布」。\n\n"
        "【给未来的我的叮嘱】\n记得先确认时间。"
    )
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER, content, sender_name="阿罗", origin="private", mode="full"
        )

        auto_req = _request()
        await plugin.on_llm_request(_event(), auto_req)
        auto_text = _extra_texts(auto_req)[0]
        assert "轻量节选" in auto_text
        assert "别忘周六上午九点讨论发布" in auto_text
        assert "只应留在全文里的细节" not in auto_text

        inst.set_history([message("user", "新会话")], cid="cid-2")
        await inst.run_handler("diary_inject", _event("/注入日记 最新"))
        manual_req = _request()
        await plugin.on_llm_request(_event(), manual_req)
        manual_text = _extra_texts(manual_req)[0]
        assert "轻量节选" not in manual_text
        assert "只应留在全文里的细节" in manual_text
        assert content in manual_text


async def test_inject_enforces_default_token_and_character_limits():
    async with World(
        config={"use_persona": False, "inject_max_chars": 8000}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin.config.pop("inject_token_limit", None)
        for index, day in enumerate(("20261021", "20261022", "20261023")):
            plugin._save_diary(
                SENDER,
                f"第{index}天" + "长" * 1500,
                sender_name="阿罗",
                entry_name_hint=day,
                origin="private",
                mode="full",
            )
        plugin._save_pref(SENDER, "all")

        text, _tampered, names = plugin._assemble_inject(SENDER)
        assert estimate_text(text) <= 2400
        assert names[-1] == "diaries/20261023.md"
        assert "diaries/20261021.md" not in names
        assert "token 上限" in text

        plugin.config["inject_max_chars"] = 700
        plugin.config["inject_token_limit"] = 10000
        text, _tampered, names = plugin._assemble_inject(SENDER)
        assert len(text) <= 700
        assert names
        assert "长度或 token 上限" in text
