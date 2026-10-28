# -*- coding: utf-8 -*-
"""on_llm_request injection, reset generation, watch thresholds."""

from __future__ import annotations

from astrbot.api.provider import ProviderRequest

from harness.fakes import make_event
from harness.world import DIARY_TEXT, World, message

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"

FILLER = "背景材料" * 15


def _request(prompt: str = "你好", *, contexts=None, system_prompt: str = "SYS"):
    return ProviderRequest(
        prompt=prompt,
        contexts=list(contexts) if contexts is not None else [{"role": "user", "content": "你好"}],
        system_prompt=system_prompt,
    )


def _event(text: str = "你好", *, group_id: str = ""):
    return make_event(
        text,
        sender_id=SENDER,
        sender_name="阿罗",
        group_id=group_id,
        umo=UMO if not group_id else "aiocqhttp:GroupMessage:88888",
    )


def _extra_texts(req) -> list[str]:
    return [getattr(part, "text", str(part)) for part in req.extra_user_content_parts]


def _seed_diary(inst):
    plugin = inst.plugin
    plugin._save_diary(
        SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
    )
    plugin._save_diary(
        SENDER,
        "第二天的私聊日记",
        sender_name="阿罗",
        entry_name_hint="20261022",
        origin="private",
        mode="full",
    )


async def test_temporary_injection_only_on_first_request():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            _seed_diary(inst)
            inst.set_history([message("user", "旧消息")])
            first = _request(contexts=[{"role": "user", "content": "你好"}])
            await plugin.on_llm_request(_event(), first)

            second = _request(contexts=[{"role": "user", "content": "你好"}])
            await plugin.on_llm_request(_event(), second)

            return {
                "first_extra": _extra_texts(first),
                "first_system": first.system_prompt,
                "second_extra": _extra_texts(second),
                "second_system": second.system_prompt,
                "injected_cid": dict(plugin._injected_cid),
                "watch_state": dict(plugin._watch_state),
                "provider_calls": len(inst.provider.calls),
            }

        pair = await world.run_pair(scenario, label="temp inject")
        value = pair.value
        assert len(value["first_extra"]) == 1
        assert "<memory_diary>" in value["first_extra"][0]
        assert "【2026-10-22 的日记】" in value["first_extra"][0]
        assert value["first_system"] == "SYS"
        assert value["second_extra"] == []
        assert value["injected_cid"] == {UMO: "cid-1"}
        assert "已吸收" not in value["first_extra"][0]


async def test_persist_inject_every_request_and_disable_auto_inject():
    async with World(
        config={"use_persona": False, "persist_inject": True}
    ) as world:
        async def scenario(inst):
            plugin = inst.plugin
            _seed_diary(inst)
            reqs = []
            for _ in range(3):
                req = _request()
                await plugin.on_llm_request(_event(), req)
                reqs.append({"system": req.system_prompt, "extra": _extra_texts(req)})
            plugin.config["enable_auto_inject"] = False
            disabled = _request()
            await plugin.on_llm_request(_event(), disabled)
            return {
                "requests": reqs,
                "disabled_system": disabled.system_prompt,
                "disabled_extra": _extra_texts(disabled),
            }

        pair = await world.run_pair(scenario, label="persist inject")
        value = pair.value
        assert all("<memory_diary>" in r["system"] for r in value["requests"])
        assert all(r["extra"] == [] for r in value["requests"])
        assert value["disabled_system"] == "SYS"
        assert value["disabled_extra"] == []


async def test_manual_inject_pending_reaches_next_request_even_when_auto_off():
    async with World(config={"use_persona": False, "enable_auto_inject": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            _seed_diary(inst)
            event = _event("/注入日记 最新")
            texts = await inst.run_handler("diary_inject", event)
            req = _request()
            await plugin.on_llm_request(_event(), req)
            return {
                "command_texts": texts,
                "extra": _extra_texts(req),
                "pending": dict(plugin._pending_inject),
                "injected_entries": {
                    k: sorted(v["names"]) for k, v in plugin._injected_entries.items()
                },
            }

        pair = await world.run_pair(scenario, label="manual inject pending")
        value = pair.value
        assert len(value["extra"]) == 1
        assert "第二天的私聊日记" not in value["extra"][0]
        assert value["pending"] == {}
        assert value["injected_entries"] == {UMO: ["diaries/20261023.md"]}


async def test_reset_generation_detection_via_command_and_empty_contexts():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            _seed_diary(inst)
            inst.set_history([message("user", "旧消息")])
            first = _request()
            await plugin.on_llm_request(_event(), first)

            # /reset seen by on_any_message: next request re-injects.
            await plugin.on_any_message(_event("/reset"))
            after_command = _request()
            await plugin.on_llm_request(_event(), after_command)
            reset_pending_after = set(plugin._reset_pending)

            # Empty contexts also counts as a new generation (cid unchanged).
            third = _request()
            await plugin.on_llm_request(_event(), third)
            empty_contexts = _request(contexts=[])
            await plugin.on_llm_request(_event(), empty_contexts)

            # A new conversation id re-injects as well.
            inst.set_history([message("user", "新会话消息")], cid="cid-2")
            new_cid = _request()
            await plugin.on_llm_request(_event(), new_cid)
            return {
                "first_extra": _extra_texts(first),
                "command_extra": _extra_texts(after_command),
                "reset_pending_after": reset_pending_after,
                "third_extra": _extra_texts(third),
                "empty_contexts_extra": _extra_texts(empty_contexts),
                "new_cid_extra": _extra_texts(new_cid),
                "injected_cid": dict(plugin._injected_cid),
            }

        pair = await world.run_pair(scenario, label="reset generation")
        value = pair.value
        assert len(value["first_extra"]) == 1
        assert len(value["command_extra"]) == 1
        assert value["reset_pending_after"] == set()
        assert value["third_extra"] == []
        assert len(value["empty_contexts_extra"]) == 1
        assert len(value["new_cid_extra"]) == 1
        assert value["injected_cid"] == {UMO: "cid-2"}


async def test_group_inject_filter_private_vs_group():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin._save_diary(
                SENDER, "这是私聊写的", sender_name="阿罗", origin="private", mode="full"
            )
            plugin._save_diary(
                SENDER, "这是群聊写的", sender_name="阿罗", origin="group", mode="full"
            )
            plugin._save_pref(SENDER, "all")
            group_req = _request()
            await plugin.on_llm_request(_event(group_id="88888"), group_req)
            private_req = _request()
            await plugin.on_llm_request(_event(), private_req)
            return {
                "group_extra": _extra_texts(group_req),
                "private_extra": _extra_texts(private_req),
            }

        pair = await world.run_pair(scenario, label="group filter inject")
        value = pair.value
        assert "群聊写的" in value["group_extra"][0]
        assert "私聊写的" not in value["group_extra"][0]
        assert "私聊写的" in value["private_extra"][0]
        assert "群聊写的" in value["private_extra"][0]


async def test_inject_budget_truncation_and_scope_preference():
    async with World(config={"use_persona": False, "inject_max_chars": 600}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            for i, day in enumerate(("20261020", "20261021", "20261022", "20261023")):
                plugin._save_diary(
                    SENDER,
                    f"第{i}天的正文" + "内" * 120,
                    sender_name="阿罗",
                    entry_name_hint=day,
                    origin="private",
                    mode="full",
                )
            plugin._save_pref(SENDER, "all")
            req = _request()
            await plugin.on_llm_request(_event(), req)
            text = _extra_texts(req)[0]
            return {
                "text": text,
                "has_truncation_note": "更早的日记未全部注入" in text,
                "used_names": sorted(
                    next(iter(plugin._injected_entries.values()))["names"]
                ),
                "selected": plugin._select_entries(inst.entries(SENDER), "days:2"),
            }

        pair = await world.run_pair(scenario, label="inject budget")
        value = pair.value
        assert value["has_truncation_note"] is True
        assert len(value["used_names"]) >= 2
        assert value["selected"] == ["diaries/20261022.md", "diaries/20261023.md"]


async def test_watch_reminder_threshold():
    async with World(
        config={
            "use_persona": False,
            "context_token_limit": 60000,
            "remind_ratio": 0.75,
            "auto_ratio": 0.9,
        }
    ) as world:
        async def scenario(inst):
            plugin = inst.plugin
            inst.set_history([message("user", "历史")])
            event = _event()
            req = _request(prompt="x" * 83000)
            await plugin.on_llm_request(event, req)
            state_after_first = dict(plugin._watch_state.get(UMO) or {})
            sent_first = list(event.result_texts())

            event2 = _event()
            req2 = _request(prompt="x" * 83000)
            await plugin.on_llm_request(event2, req2)
            sent_second = list(event2.result_texts())
            return {
                "sent_first": sent_first,
                "state_after_first": state_after_first,
                "sent_second": sent_second,
                "injections": len(_extra_texts(req)) + len(_extra_texts(req2)),
            }

        pair = await world.run_pair(scenario, label="watch remind")
        value = pair.value
        assert len(value["sent_first"]) == 1
        assert "📝 小提醒" in value["sent_first"][0]
        assert value["state_after_first"]["reminded"] is True
        assert value["sent_second"] == []
        assert value["injections"] == 0


async def test_watch_auto_write_threshold():
    async with World(
        config={
            "use_persona": False,
            "context_token_limit": 60000,
            "remind_ratio": 0.75,
            "auto_ratio": 0.9,
        }
    ) as world:
        async def scenario(inst):
            plugin = inst.plugin
            inst.provider.replies[:] = [DIARY_TEXT]
            inst.set_history([message("user", "历史消息")])
            event = _event()
            req = _request(prompt="x" * 99000)
            await plugin.on_llm_request(event, req)
            sent = list(event.result_texts())
            state = dict(plugin._watch_state.get(UMO) or {})
            entries = inst.entries(SENDER)

            # Second over-threshold request must not write again.
            event2 = _event()
            req2 = _request(prompt="x" * 99000)
            await plugin.on_llm_request(event2, req2)
            sent2 = list(event2.result_texts())
            return {
                "sent": sent,
                "state": state,
                "entries": entries,
                "content": plugin._load_latest(SENDER)[0],
                "provider_calls": len(inst.provider.calls),
                "sent2": sent2,
            }

        pair = await world.run_pair(scenario, label="watch auto write")
        value = pair.value
        assert len(value["sent"]) == 1
        assert "上下文已用约" in value["sent"][0]
        assert value["state"]["auto_written"] is True
        assert value["entries"] == ["diaries/20261023.md"]
        assert value["provider_calls"] == 1
        assert value["sent2"] == []


async def test_watch_auto_write_no_material_and_limit_zero():
    async with World(
        config={
            "use_persona": False,
            "context_token_limit": 60000,
            "remind_ratio": 0.75,
            "auto_ratio": 0.9,
        }
    ) as world:
        async def scenario(inst):
            plugin = inst.plugin
            event = _event()
            # prompt looks like a diary op command -> filtered out of material.
            req = _request(prompt="/日记")
            req.prompt = "/日记"
            big = "/日记 " + "x" * 99000
            req = _request(prompt=big)
            await plugin.on_llm_request(event, req)
            sent = list(event.result_texts())
            state = dict(plugin._watch_state.get(UMO) or {})

            plugin.config["context_token_limit"] = 0
            event2 = _event()
            req2 = _request(prompt="x" * 99000)
            await plugin.on_llm_request(event2, req2)
            sent2 = list(event2.result_texts())

            plugin.config["enable_context_watch"] = False
            event3 = _event()
            await plugin.on_llm_request(event3, _request(prompt="x" * 99000))
            return {
                "sent": sent,
                "state": state,
                "entries": inst.entries(SENDER),
                "sent2": sent2,
                "sent3": list(event3.result_texts()),
            }

        pair = await world.run_pair(scenario, label="watch no material/limit0")
        value = pair.value
        assert "没有新的聊天内容" in value["sent"][0]
        assert value["state"]["auto_written"] is True
        assert value["entries"] == []
        assert value["sent2"] == []
        assert value["sent3"] == []


async def test_watch_ratio_clamping_and_estimate_tokens():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin.config["remind_ratio"] = 0.95
            plugin.config["auto_ratio"] = 0.5
            clamped = plugin._watch_ratios()
            plugin.config["remind_ratio"] = 0
            plugin.config["auto_ratio"] = 5
            bounds = plugin._watch_ratios()
            req = ProviderRequest(
                prompt="abc",
                system_prompt="defg",
                contexts=[
                    {"role": "user", "content": "上下文一"},
                    {"role": "assistant", "content": [{"type": "text", "text": "上下文二"}]},
                ],
            )
            estimate = plugin._estimate_tokens(req)
            detected = {
                text: plugin._is_op_command(text)
                for text in ("/日记", "日记", "/记一下 考试", "我写了一篇日记", "hello")
            }
            return {
                "clamped": clamped,
                "bounds": bounds,
                "estimate": estimate,
                "detected": detected,
                "flattened": plugin._flatten_history(
                    [
                        {"role": "user", "content": "你好"},
                        {"role": "system", "content": "忽略"},
                        {"role": "assistant", "content": [{"type": "text", "text": "在"}]},
                    ]
                ),
            }

        await world.run_pair(scenario, label="watch helpers")


async def test_inject_delivery_system_prompt_fallback():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            _seed_diary(inst)
            module = inst.module
            saved = module.TextPart
            try:
                module.TextPart = None
                req = _request(system_prompt="SYS")
                await plugin.on_llm_request(_event(), req)
                fallback = {
                    "system": req.system_prompt,
                    "extra": _extra_texts(req),
                }
            finally:
                module.TextPart = saved
            return {"fallback": fallback}

        pair = await world.run_pair(scenario, label="deliver fallback")
        fallback = pair.value["fallback"]
        assert _extra_texts(_request()) == []
        assert "<memory_diary>" in fallback["system"]
        assert fallback["extra"] == []
