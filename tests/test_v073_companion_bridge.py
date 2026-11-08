# -*- coding: utf-8 -*-
"""v0.7.3 陪伴插件 Provider：全部宿主能力均用 mock。"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.api.provider import ProviderRequest

from harness import env, loader
from harness.fakes import make_event
from harness.world import DIARY_TEXT, World, message

SENDER = "10001"
OTHER = "20002"
GROUP = "88888"
UMO = f"aiocqhttp:FriendMessage:{SENDER}"
GROUP_UMO = f"aiocqhttp:GroupMessage:{GROUP}"

_main_mod = loader.get_module(env.SIDE_REFACTOR)
bridge = importlib.import_module(
    _main_mod.__name__.rsplit(".", 1)[0] + ".adapter.companion_bridge"
)


class _MockExtensionAPI:
    def __init__(self, version=bridge.MEMORY_API_VERSION):
        self.memory_provider_api_version = version
        self.register_calls = []
        self.unregister_calls = []
        self.registrations = {}
        self._tokens = {}

    def register_memory_provider(self, provider_id, **kwargs):
        token = object()
        self.register_calls.append((provider_id, kwargs))
        self.registrations[provider_id] = kwargs
        self._tokens[provider_id] = token
        return token

    def unregister_memory_provider(self, token):
        self.unregister_calls.append(token)
        for provider_id, current in list(self._tokens.items()):
            if current is token:
                self._tokens.pop(provider_id)
                self.registrations.pop(provider_id, None)
                return True
        return False


def _star(plugin_id, api=None, *, activated=True):
    return SimpleNamespace(
        root_dir_name=plugin_id,
        name=plugin_id,
        activated=activated,
        star_cls=SimpleNamespace(extension_api=api),
    )


def _install_stars(inst, *stars):
    inst.ctx.get_all_stars = lambda: list(stars)


def _identity(sender=SENDER, *, kind="private", role="role-a"):
    return {
        "scope": {
            "installation_id": "installation-a",
            "platform": "aiocqhttp",
            "kind": kind,
            "native_scope_id": sender,
            "bot_id": "99999",
            "account_id": "99999",
            "conversation_id": sender,
            "session_id": "session-a",
        },
        "role": role,
        "role_binding_revision": "rev-1",
        # 真机实测形态（7.1.6）：SubjectRef 序列化，subject_id 带平台前缀
        "user": {"installation_id": "installation-a", "kind": "human",
                 "subject_id": f"onebot-user:{sender}"},
        "principal": {"principal_id": sender},
        "audience": [],
    }


def _read_request(
    sender=SENDER,
    *,
    kind="private",
    query="深夜",
    purpose="recall",
    max_chars=2000,
    limit=3,
):
    return {
        "schema": bridge.MEMORY_API_VERSION,
        "source_plugin": bridge.COMPANION_PLUGIN_ID,
        "purpose": purpose,
        "context": _identity(sender, kind=kind),
        "query": query,
        "limit": limit,
        "max_chars": max_chars,
    }


def _evidence_record(
    *,
    memory_id="memory-1",
    kind="persona_life",
    content="陪伴插件原始记录",
    role="role-a",
    occurred_at="2026-10-23T12:00:00+08:00",
):
    return {
        "schema": bridge.MEMORY_API_VERSION,
        "source_plugin": bridge.COMPANION_PLUGIN_ID,
        "memory_id": memory_id,
        "kind": kind,
        "content": content,
        "scope": "unknown",
        "session_id": "private_companion_v2:journal:role-a",
        "platform": "aiocqhttp",
        "occurred_at": occurred_at,
        "tags": ["journal"],
        "metadata": {"subtype": "dream"},
        "confidence": 0.8,
        "importance": 0.5,
        "context": _identity(role=role),
    }


def _save_diary(plugin, sender=SENDER, text=DIARY_TEXT, *, day="20261023", origin="private"):
    plugin._companion_observe_event(make_event(
        "观察身份", sender_id=sender,
        group_id=GROUP if origin == "group" else "",
    ))
    return plugin._save_diary(
        sender,
        text,
        sender_name="阿罗",
        entry_name_hint=day,
        origin=origin,
        scope=f"group:{GROUP}" if origin == "group" else "private",
        mode="full",
    )


def _llm_request():
    return ProviderRequest(
        prompt="你好",
        contexts=[{"role": "user", "content": "你好"}],
        system_prompt="SYS",
    )


def _extra_texts(req):
    return [getattr(part, "text", str(part)) for part in req.extra_user_content_parts]


async def test_companion_absent_or_version_mismatch_degrades_cleanly():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        await inst.initialize()
        assert plugin._companion_reg is None
        _save_diary(plugin)
        assert len(plugin._list_entries(SENDER)) == 1

        bad_api = _MockExtensionAPI("companion.external-memory.v0")
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, bad_api))
        await plugin._companion_try_register()
        assert plugin._companion_reg is None
        assert bad_api.register_calls == []


async def test_companion_registration_and_terminate_unregister():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        api = _MockExtensionAPI()
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, api))
        await inst.initialize()
        assert plugin._companion_reg is not None
        provider_id, kwargs = api.register_calls[0]
        assert provider_id == bridge.PROVIDER_ID
        assert kwargs["priority"] == 10
        assert kwargs["timeout_seconds"] == 5.0
        assert "handles_model_memory" not in kwargs
        assert callable(kwargs["read"])
        assert callable(kwargs["write"])
        assert callable(kwargs["summary"])
        assert kwargs["is_active"]() is True
        await plugin.terminate()
        assert len(api.unregister_calls) == 1
        assert plugin._companion_reg is None
        assert plugin._companion_is_active() is False


async def test_companion_registration_is_idempotent_and_recovers_from_reload():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        first = _MockExtensionAPI()
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, first))
        await plugin._companion_try_register()
        await plugin._companion_try_register()
        assert len(first.register_calls) == 1

        second = _MockExtensionAPI()
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, second))
        await plugin._companion_try_register()
        assert len(first.unregister_calls) == 1
        assert len(second.register_calls) == 1
        assert plugin._companion_api is second


async def test_companion_late_loading_uses_bounded_lazy_retry():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        await inst.initialize()
        assert plugin._companion_reg is None
        api = _MockExtensionAPI()
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, api))
        await plugin._companion_maybe_register()
        assert api.register_calls == []
        plugin._companion_last_attempt = float("-inf")
        await plugin._companion_maybe_register()
        assert len(api.register_calls) == 1


async def test_companion_read_filters_scope_whitelist_budget_and_errors():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _save_diary(plugin)
        result = await plugin._companion_read(_read_request())
        assert result["items"]
        assert all(isinstance(item["content"], str) for item in result["items"])
        assert result["items"][0]["content"].startswith("以下为历史日记资料")
        assert "深夜" in result["items"][0]["content"]
        assert sum(len(item["content"]) for item in result["items"]) <= 2000
        assert await plugin._companion_read(_read_request(kind="group")) == {"items": []}
        assert await plugin._companion_read(_read_request(max_chars=1)) == {"items": []}

        plugin.config["allowed_users"] = OTHER
        assert await plugin._companion_read(_read_request()) == {"items": []}
        plugin.config["allowed_users"] = ""
        with patch.object(bridge, "search_diaries_multi", side_effect=RuntimeError("boom")):
            assert await plugin._companion_read(_read_request()) == {"items": []}


async def test_companion_read_never_crosses_users_or_returns_evidence():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _save_diary(plugin, SENDER, "阿罗独有的私聊原文 深夜")
        _save_diary(plugin, OTHER, "另一位用户的秘密原文", day="20261022")
        result = await plugin._companion_read(_read_request(SENDER, query="另一位"))
        assert result == {"items": []}
        latest = await plugin._companion_read(
            _read_request(SENDER, query="", purpose="summary")
        )
        assert "阿罗独有" in latest["items"][0]["content"]
        assert "另一位用户" not in latest["items"][0]["content"]

        evidence_text = "绝不可回流的陪伴存证正文"
        assert await plugin._companion_write(
            _evidence_record(content=evidence_text)
        ) == "memory-1"
        evidence_result = await plugin._companion_read(
            _read_request(SENDER, query="绝不可回流")
        )
        assert evidence_result == {"items": []}


async def test_companion_write_is_signed_and_idempotent():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        record = _evidence_record()
        assert await plugin._companion_write(record) == "memory-1"
        paths = list((plugin.data_dir / "companion_evidence").glob("*/*.jsonl"))
        assert len(paths) == 1
        path = paths[0]
        raw = path.read_bytes()
        signature = path.with_name(path.name + ".sig").read_text(encoding="ascii").strip()
        assert signature == plugin._companion_evidence_sig(
            path.parent.name, path.name, raw
        )
        first = json.loads(raw.splitlines()[0])
        assert first["source_plugin"] == bridge.COMPANION_PLUGIN_ID
        assert first["received_at"]

        replay = _evidence_record(content="重放后更新的正文")
        assert await plugin._companion_write(replay) == "memory-1"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["content"] == "重放后更新的正文"

        rejected = _evidence_record(memory_id="missing-context")
        rejected["context"] = None
        assert await plugin._companion_write(rejected) is None
        assert await plugin._companion_write(
            _evidence_record(memory_id="unknown-kind", kind="unknown")
        ) is None
        assert len(path.read_text(encoding="utf-8").splitlines()) == 1


async def test_companion_write_registration_modes():
    async with World(config={"use_persona": False, "companion_write_evidence": "off"}) as world:
        inst = world.refactor
        api = _MockExtensionAPI()
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, api))
        await inst.initialize()
        assert api.register_calls[0][1]["write"] is None

    async with World(config={"use_persona": False, "companion_write_evidence": "on"}) as world:
        inst = world.refactor
        api = _MockExtensionAPI()
        _install_stars(
            inst,
            _star(bridge.COMPANION_PLUGIN_ID, api),
            _star(bridge.MEMORY_COMPANION_PLUGIN_ID),
        )
        await inst.initialize()
        assert callable(api.register_calls[0][1]["write"])

    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        api = _MockExtensionAPI()
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, api))
        await inst.initialize()
        assert callable(api.register_calls[0][1]["write"])


async def test_companion_auto_mode_protects_memory_companion_and_logs():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        api = _MockExtensionAPI()
        _install_stars(
            inst,
            _star(bridge.COMPANION_PLUGIN_ID, api),
            _star(bridge.MEMORY_COMPANION_PLUGIN_ID),
        )
        with patch.object(bridge.logger, "info") as info:
            await inst.initialize()
        assert api.register_calls[0][1]["write"] is None
        assert any(
            "检测到 MemoryCompanion" in str(call)
            for call in info.call_args_list
        )


async def test_companion_write_role_isolation_and_tamper_is_quarantined():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        assert await plugin._companion_write(_evidence_record(role="role-a")) == "memory-1"
        assert await plugin._companion_write(_evidence_record(role="role-b")) == "memory-1"
        paths = list((plugin.data_dir / "companion_evidence").glob("*/*.jsonl"))
        assert len(paths) == 2
        assert paths[0].parent.name != paths[1].parent.name

        role_key = plugin._companion_role_key(_identity(role="role-a"))
        path = next(path for path in paths if path.parent.name == role_key)
        original = path.read_bytes()
        path.write_bytes(original.replace("陪伴插件原始记录".encode(), "被篡改".encode()))
        assert await plugin._companion_write(
            _evidence_record(role="role-a", content="隔离后重新写入")
        ) == "memory-1"
        assert "隔离后重新写入" in path.read_text(encoding="utf-8")
        assert len(plugin._load_companion_evidence(path, role_key)) == 1
        assert list(path.parent.glob(f"{path.name}.corrupt-*"))


async def test_companion_evidence_expires_after_ninety_days_at_startup():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        old_day = (world.now - timedelta(days=91)).strftime("%Y-%m-%d")
        assert await plugin._companion_write(
            _evidence_record(memory_id="old", occurred_at=f"{old_day}T12:00:00")
        ) == "old"
        assert await plugin._companion_write(
            _evidence_record(memory_id="current")
        ) == "current"
        await inst.initialize()
        remaining = list((plugin.data_dir / "companion_evidence").glob("*/*.jsonl"))
        assert len(remaining) == 1
        assert json.loads(remaining[0].read_text(encoding="utf-8"))["memory_id"] == "current"
        assert not list((plugin.data_dir / "companion_evidence").glob("*/*.jsonl.sig.tmp-*"))


async def test_companion_summary_counts_only_request_identity_and_raises_on_failure():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _save_diary(plugin)
        _save_diary(plugin, text="前一天", day="20261022")
        _save_diary(plugin, OTHER, "别人", day="20261022")
        result = await plugin._companion_summary(_read_request(purpose="summary", query=""))
        assert result["counts"] == {
            "profile": 0,
            "preference": 0,
            "relationship": 0,
            "private_conversation": 0,
            "other": 2,
            "total": 2,
        }
        invalid = _read_request(purpose="summary")
        invalid["context"]["user"] = {"nickname": "不能用来识别身份"}
        invalid["context"]["principal"] = None
        try:
            await plugin._companion_summary(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("身份解析失败时 summary 必须抛错，不能伪造零计数")


async def test_companion_yield_skips_only_private_automatic_injection():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        api = _MockExtensionAPI()
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, api))
        await plugin._companion_try_register()
        _save_diary(plugin)
        inst.set_history([message("user", "旧消息")])

        private_req = _llm_request()
        await plugin.on_llm_request(make_event("你好"), private_req)
        assert _extra_texts(private_req) == []
        assert plugin._injected_cid == {}
        assert plugin._inject_confirm == {}

        manual_req = _llm_request()
        plugin._pending_inject[UMO] = "all"
        await plugin.on_llm_request(make_event("你好"), manual_req)
        assert len(_extra_texts(manual_req)) == 1

        _save_diary(plugin, text="群聊证据", origin="group")
        inst.conversation_manager.set(GROUP_UMO, history=[message("user", "群聊")])
        group_req = _llm_request()
        await plugin.on_llm_request(
            make_event("你好", group_id=GROUP, umo=GROUP_UMO), group_req
        )
        assert len(_extra_texts(group_req)) == 1


async def test_companion_yield_can_be_disabled():
    async with World(
        config={"use_persona": False, "companion_yield_inject": False}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        api = _MockExtensionAPI()
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, api))
        await plugin._companion_try_register()
        _save_diary(plugin)
        inst.set_history([message("user", "旧消息")])
        req = _llm_request()
        await plugin.on_llm_request(make_event("你好"), req)
        assert len(_extra_texts(req)) == 1


async def test_companion_callbacks_are_bounded_and_status_shows_evidence():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _save_diary(plugin)
        read_result = await asyncio.wait_for(
            plugin._companion_read(_read_request()), timeout=1
        )
        assert read_result["items"]
        memory_id = await asyncio.wait_for(
            plugin._companion_write(_evidence_record()), timeout=1
        )
        assert memory_id == "memory-1"
        summary = await asyncio.wait_for(
            plugin._companion_summary(_read_request(purpose="summary")), timeout=1
        )
        assert summary["counts"]["total"] == 1

        texts = await inst.run_handler("diary_status", make_event("/日记状态"))
        assert any("陪伴记录存证：1 条（1 个角色）" in text for text in texts)


async def test_companion_status_without_evidence_keeps_old_output():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        texts = await inst.run_handler("diary_status", make_event("/日记状态"))
        assert not any("陪伴记录存证" in text for text in texts)


def test_conf_schema_types_supported_by_astrbot():
    """配置 schema 类型必须在 AstrBot DEFAULT_VALUE_MAP 支持范围内。

    v0.7.3 真机教训：'select' 不是合法类型（下拉 = string + options），
    schema 非法会导致整个插件在 AstrBot 里加载失败，CI 必须拦住。
    """
    import pathlib

    schema_path = pathlib.Path(__file__).parent.parent / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    allowed = {
        "int", "float", "bool", "string", "text",
        "list", "file", "object", "template_list", "dict",
    }

    def walk(node, prefix=""):
        bad = []
        for key, value in node.items():
            if not isinstance(value, dict) or "type" not in value:
                continue
            if value["type"] not in allowed:
                bad.append(f"{prefix}{key}: {value['type']}")
            if value["type"] == "object" and isinstance(value.get("items"), dict):
                bad += walk(value["items"], f"{prefix}{key}.")
        return bad

    assert walk(schema) == []


async def test_companion_read_accepts_natural_language_sentence():
    """宿主传的是不带空格的整句，必须拆词后仍能命中（真机回归）。"""
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _save_diary(plugin)
        result = await plugin._companion_read(
            _read_request(query="她是不是总在深夜找你聊天")
        )
        assert result["items"]
        assert "深夜" in result["items"][0]["content"]


async def test_companion_identity_requires_observed_matching_bot_account_scope():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _save_diary(plugin)
        plugin._companion_sender_scopes.clear()
        assert await plugin._companion_read(_read_request()) == {"items": []}

        await plugin.on_any_message(make_event("你好"))
        assert ( "aiocqhttp", "99999", "99999") in plugin._companion_sender_scopes[SENDER]
        assert (await plugin._companion_read(_read_request()))["items"]
        role_request = _read_request()
        role_request["context"]["role"] = "another-role"
        assert (await plugin._companion_read(role_request))["items"]
        summary = await plugin._companion_summary(_read_request(purpose="summary"))
        assert summary["counts"]["total"] == 1

        rejected = []
        installation = _read_request()
        installation["context"]["scope"]["installation_id"] = "installation-b"
        rejected.append(installation)
        # 说明：scope.bot_id 是宿主内部的 Bot 标识（可为 "staging-bot" 等
        # 自定义值），我方无法从事件侧可靠学习到同口径值；跨 Bot 边界由
        # account_id（Bot 真实账号）承担——换 Bot 即换 account_id，必然拒绝。
        # 因此 bot_id 不一致不再是拒绝用例。
        cross_account = _read_request()
        cross_account["context"]["scope"]["account_id"] = "another-account"
        rejected.append(cross_account)
        cross_platform = _read_request()
        cross_platform["context"]["scope"]["platform"] = "qq_official"
        cross_platform["context"]["user"]["subject_id"] = f"qq-official-user:{SENDER}"
        rejected.append(cross_platform)
        # 同理：scope.platform 是平台实例名（用户自定义，如"蓝色大肥鱼"），
        # 不能当作适配器类型校验；适配器族经学习集 + account_id 锚定，
        # 前缀族与学习的适配器不符由 cross_platform 用例覆盖。
        for request in rejected:
            assert await plugin._companion_read(request) == {"items": []}

        plugin.config["allowed_users"] = OTHER
        assert await plugin._companion_read(_read_request()) == {"items": []}
        plugin.config["allowed_users"] = ""
        cross_account_summary = _read_request(purpose="summary")
        cross_account_summary["context"]["scope"]["account_id"] = "another-account"
        try:
            await plugin._companion_summary(cross_account_summary)
        except ValueError:
            pass
        else:
            raise AssertionError("跨账号 summary 必须拒绝")


async def test_companion_identity_accepts_qq_official_openid_with_observed_scope():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _save_diary(plugin, "OPENID-abc")
        event = make_event(
            "你好", sender_id="OPENID-abc", self_id="official-account",
            umo="qq_official:FriendMessage:OPENID-abc",
        )
        event.bot_id = "official-bot"
        plugin._companion_observe_event(event)
        request = _read_request("OPENID-abc")
        request["context"]["scope"].update({
            "platform": "qq_official",
            "bot_id": "official-bot",
            "account_id": "official-account",
        })
        request["context"]["user"]["subject_id"] = "qq-official-user:OPENID-abc"
        assert (await plugin._companion_read(request))["items"]


async def test_companion_search_preserves_tail_entity_and_ranks_it_above_common_terms():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _save_diary(plugin, text="薄荷已经发芽。")
        result = await plugin._companion_read(_read_request(
            query="我们今天要不要顺便认真详细回顾一下上次那盆薄荷现在怎么样了"
        ))
        assert result["items"]
        assert "薄荷" in result["items"][0]["content"]

    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _save_diary(plugin, text="我们今天继续聊天。", day="20261021")
        _save_diary(plugin, text="我们今天认真聊天，但没有植物。", day="20261022")
        _save_diary(plugin, text="窗台上的薄荷今天发芽了。", day="20261023")
        result = await plugin._companion_read(_read_request(
            query="我们今天要不要聊聊薄荷", limit=1,
        ))
        assert len(result["items"]) == 1
        assert "薄荷" in result["items"][0]["content"]
        assert "没有植物" not in result["items"][0]["content"]


async def test_companion_search_large_snapshot_is_single_pass_and_yields_event_loop():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        plugin._companion_observe_event(make_event("观察身份"))
        data = {}
        entry_count = 240
        for index in range(entry_count):
            day = (datetime(2025, 1, 1) + timedelta(days=index)).strftime("%Y%m%d")
            name = plugin._md_entry(day)
            text = "【当天简记】\n" + f"第{index}天。" + ("普通生活记录，浇水读书散步。" * 65)
            data[name] = text.encode()
            data[plugin._sig_entry(name)] = plugin._entry_sig(SENDER, name, text).encode()
        plugin._write_zip_atomic(plugin._diary_path(SENDER), data)

        original_load = plugin._load_entry
        load_calls = [0]
        def counted_load(*args, **kwargs):
            load_calls[0] += 1
            return original_load(*args, **kwargs)
        plugin._load_entry = counted_load
        heartbeat_ticks = [0]
        async def heartbeat():
            while True:
                await asyncio.sleep(0.05)
                heartbeat_ticks[0] += 1
        heartbeat_task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        started = time.perf_counter()
        result = await asyncio.wait_for(
            plugin._companion_read(_read_request(
                query="我们今天要不要顺便认真详细回顾一下上次那盆薄荷现在怎么样了"
            )),
            timeout=5,
        )
        elapsed = time.perf_counter() - started
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        assert result == {"items": []}
        assert elapsed < 5
        assert load_calls[0] == entry_count
        assert heartbeat_ticks[0] >= 1
        assert plugin._search_index.stats["builds"] + plugin._search_index.stats["rebuilds"] <= 1


def _crash_before_signature(plugin):
    original = plugin._companion_atomic_write
    def crash(path, payload):
        if str(path).endswith(".sig"):
            raise OSError("simulated crash before signature")
        return original(path, payload)
    return original, crash


async def test_companion_evidence_first_write_crash_is_quarantined_and_recovers():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        original, crash = _crash_before_signature(plugin)
        plugin._companion_atomic_write = crash
        assert await plugin._companion_write(_evidence_record()) is None
        plugin._companion_atomic_write = original
        assert await plugin._companion_write(_evidence_record(content="恢复后的正文")) == "memory-1"
        path = next((plugin.data_dir / "companion_evidence").glob("*/*.jsonl"))
        role_key = path.parent.name
        assert len(plugin._load_companion_evidence(path, role_key)) == 1
        assert list(path.parent.glob(f"{path.name}.corrupt-*"))
        assert await plugin._companion_write(_evidence_record(content="幂等重放")) == "memory-1"
        assert len(plugin._load_companion_evidence(path, role_key)) == 1
        assert plugin._companion_evidence_stats() == (1, 1)


async def test_companion_evidence_update_crash_is_quarantined_and_recovers():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        assert await plugin._companion_write(_evidence_record(content="旧正文")) == "memory-1"
        original, crash = _crash_before_signature(plugin)
        plugin._companion_atomic_write = crash
        assert await plugin._companion_write(_evidence_record(content="崩溃的新正文")) is None
        plugin._companion_atomic_write = original
        assert await plugin._companion_write(_evidence_record(content="恢复重试")) == "memory-1"
        path = next((plugin.data_dir / "companion_evidence").glob("*/*.jsonl"))
        role_key = path.parent.name
        records = plugin._load_companion_evidence(path, role_key)
        assert len(records) == 1 and records[0]["content"] == "恢复重试"
        assert list(path.parent.glob(f"{path.name}.corrupt-*"))
        assert plugin._companion_evidence_stats() == (1, 1)


async def test_companion_evidence_signature_without_body_still_rejects_write():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        assert await plugin._companion_write(_evidence_record()) == "memory-1"
        path = next((plugin.data_dir / "companion_evidence").glob("*/*.jsonl"))
        path.unlink()
        assert await plugin._companion_write(_evidence_record(content="不得写入")) is None
        assert path.with_name(path.name + ".sig").exists()
        assert not list(path.parent.glob(f"{path.name}.corrupt-*"))


async def test_companion_registration_shape_tracks_config_and_mc_changes():
    async with World(config={"use_persona": False, "companion_write_evidence": "off"}) as world:
        inst = world.refactor
        plugin = inst.plugin
        api = _MockExtensionAPI()
        stars = [_star(bridge.COMPANION_PLUGIN_ID, api)]
        _install_stars(inst, *stars)
        await inst.initialize()
        assert api.registrations[bridge.PROVIDER_ID]["write"] is None

        plugin.config["companion_write_evidence"] = "on"
        await plugin._companion_maybe_register()
        assert callable(api.registrations[bridge.PROVIDER_ID]["write"])
        plugin.config["companion_write_evidence"] = "off"
        await plugin._companion_maybe_register()
        assert api.registrations[bridge.PROVIDER_ID]["write"] is None

        plugin.config["companion_write_evidence"] = "auto"
        await plugin._companion_maybe_register()
        assert callable(api.registrations[bridge.PROVIDER_ID]["write"])
        stars.append(_star(bridge.MEMORY_COMPANION_PLUGIN_ID))
        _install_stars(inst, *stars)
        await plugin._companion_maybe_register()
        assert api.registrations[bridge.PROVIDER_ID]["write"] is None
        stars.pop()
        _install_stars(inst, *stars)
        await plugin._companion_maybe_register()
        assert callable(api.registrations[bridge.PROVIDER_ID]["write"])
        assert len(api.register_calls) == 6


async def test_companion_registration_failure_backoff_dedup_and_success_reset():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        api = _MockExtensionAPI()
        def fail_registration(*args, **kwargs):
            api.register_calls.append((args[0], kwargs))
            raise ValueError("memory provider registry is closed")
        api.register_memory_provider = fail_registration
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, api))
        with patch.object(bridge.logger, "warning") as warning:
            await plugin._companion_try_register()
            assert plugin._companion_failure_count == 1
            plugin._companion_last_attempt = time.monotonic() - 59
            await plugin._companion_maybe_register()
            assert len(api.register_calls) == 1
            plugin._companion_last_attempt = time.monotonic() - 61
            await plugin._companion_maybe_register()
            assert len(api.register_calls) == 2
            plugin._companion_last_attempt = time.monotonic() - 299
            await plugin._companion_maybe_register()
            assert len(api.register_calls) == 2
            plugin._companion_last_attempt = time.monotonic() - 301
            await plugin._companion_maybe_register()
            assert len(api.register_calls) == 3
            plugin._companion_last_attempt = time.monotonic() - 1799
            await plugin._companion_maybe_register()
            assert len(api.register_calls) == 3
            plugin._companion_last_attempt = time.monotonic() - 1801
            await plugin._companion_maybe_register()
            assert len(api.register_calls) == 4
            assert warning.call_count == 1

        working = _MockExtensionAPI()
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, working))
        assert await plugin._companion_try_register() is True
        assert plugin._companion_failure_count == 0
        assert plugin._companion_last_error == ""


async def test_companion_yield_disabled_with_local_search_keeps_auto_injection():
    async with World(config={"use_persona": False, "local_search_enabled": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        api = _MockExtensionAPI()
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, api))
        await plugin._companion_try_register()
        _save_diary(plugin)
        inst.set_history([message("user", "旧消息")])
        req = _llm_request()
        await plugin.on_llm_request(make_event("你好"), req)
        assert plugin._companion_yield_active() is False
        assert len(_extra_texts(req)) == 1


async def test_companion_yield_skips_overlapping_context_recall():
    async with World(config={"use_persona": False, "context_recall_enabled": True}) as world:
        inst = world.refactor
        plugin = inst.plugin
        api = _MockExtensionAPI()
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, api))
        await plugin._companion_try_register()
        _save_diary(plugin, text="我之前在窗台种了一盆薄荷，它已经发芽。")
        inst.set_history([message("user", "旧消息")])
        req = _llm_request()
        await plugin.on_llm_request(make_event("之前 薄荷"), req)
        assert _extra_texts(req) == []

        plugin.config["companion_yield_inject"] = False
        fallback_req = _llm_request()
        await plugin.on_llm_request(make_event("之前 薄荷"), fallback_req)
        assert any("按需回源" in text for text in _extra_texts(fallback_req))


async def test_companion_read_budget_reserves_json_wrapper_per_item():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _save_diary(plugin, text="薄荷已经发芽。")
        assert await plugin._companion_read(_read_request(query="薄荷", max_chars=50)) == {"items": []}
        result = await plugin._companion_read(_read_request(query="薄荷", max_chars=80))
        assert len(result["items"]) == 1
        assert len(result["items"][0]["content"]) + 40 <= 80


async def test_companion_status_reports_yielded_recall():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        api = _MockExtensionAPI()
        _install_stars(inst, _star(bridge.COMPANION_PLUGIN_ID, api))
        await plugin._companion_try_register()
        _save_diary(plugin)
        texts = await inst.run_handler("diary_status", make_event("/日记状态"))
        assert any("本会话由陪伴插件按需召回" in text for text in texts)
        assert not any("下一句正常对话时注入" in text for text in texts)
