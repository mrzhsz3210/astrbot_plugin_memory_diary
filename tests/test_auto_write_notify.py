# -*- coding: utf-8 -*-
"""v0.6.1 新增配置 auto_write_notify：自动写日记完成后是否告知用户。

baseline（v0.6.0）不认识该配置键、行为必然不同，因此只驱动 refactor 侧，
不做差分比较。开关只静默「写完告知」：日记照常落盘、照常注入；自动写失败
通知、「没有新素材」的 /reset 提示、remind_ratio 小提醒以及手动 /日记 的
告知都不受影响。
"""

from __future__ import annotations

import json

from astrbot.api.provider import ProviderRequest

from harness import env
from harness.fakes import make_event
from harness.world import DIARY_TEXT, FIXED_NOW, World, message

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"


def _event(text: str = "你好"):
    return make_event(text, sender_id=SENDER, sender_name="阿罗", umo=UMO)


def _request(prompt: str = "你好"):
    return ProviderRequest(
        prompt=prompt,
        contexts=[{"role": "user", "content": "你好"}],
        system_prompt="SYS",
    )


def _material_history():
    return [
        message("user", "今天聊了考试"),
        message("assistant", "祝你好运"),
    ]


async def test_scheduled_write_silent_when_notify_off():
    """定时路径-关闭：日记落盘，但不发「写好了」告知。"""
    async with World(
        config={"use_persona": False, "auto_write_notify": False}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin.config["enable_auto_diary"] = True
        plugin.config["auto_diary_time"] = "22:00"
        inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
        plugin._auto_users = {SENDER: {"umo": UMO, "name": "阿罗"}}
        inst.set_history(_material_history())
        inst.provider.replies[:] = [DIARY_TEXT]
        await plugin._auto_diary_tick()
        assert inst.entries(SENDER) == ["diaries/20261023.md"]
        assert inst.ctx.sent_messages == []


async def test_scheduled_write_notifies_by_default():
    """定时路径-开启（未配置该键 = 默认 true）：与 v0.6.0 行为一致。"""
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        assert "auto_write_notify" not in inst.config
        plugin = inst.plugin
        plugin.config["enable_auto_diary"] = True
        plugin.config["auto_diary_time"] = "22:00"
        inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
        plugin._auto_users = {SENDER: {"umo": UMO, "name": "阿罗"}}
        inst.set_history(_material_history())
        inst.provider.replies[:] = [DIARY_TEXT]
        await plugin._auto_diary_tick()
        assert inst.entries(SENDER) == ["diaries/20261023.md"]
        assert len(inst.ctx.sent_messages) == 1
        assert "今天约定的完整日记写好啦" in inst.ctx.sent_messages[0][1]


async def test_sentinel_auto_write_silent_when_notify_off():
    """哨兵路径-关闭：日记落盘、状态照旧，但不再发「写好了 + /reset」告知。"""
    async with World(
        config={
            "use_persona": False,
            "auto_write_notify": False,
            "context_token_limit": 60000,
            "remind_ratio": 0.75,
            "auto_ratio": 0.9,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "历史消息")])
        inst.provider.replies[:] = [DIARY_TEXT]
        event = _event()
        await plugin.on_llm_request(event, _request(prompt="x" * 99000))
        assert inst.entries(SENDER) == ["diaries/20261023.md"]
        assert event.result_texts() == []
        state = plugin._watch_state.get(UMO) or {}
        assert state.get("auto_written") is True


async def test_sentinel_auto_write_notifies_by_default():
    """哨兵路径-开启：保留「上下文已用约 X% + 日记写好了 + /reset」告知。"""
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
        inst.set_history([message("user", "历史消息")])
        inst.provider.replies[:] = [DIARY_TEXT]
        event = _event()
        await plugin.on_llm_request(event, _request(prompt="x" * 99000))
        sent = event.result_texts()
        assert inst.entries(SENDER) == ["diaries/20261023.md"]
        assert len(sent) == 1
        assert "上下文已用约" in sent[0]
        assert "/reset" in sent[0]
        state = plugin._watch_state.get(UMO) or {}
        assert state.get("auto_written") is True


async def test_manual_write_still_notifies_when_notify_off():
    """手动 /日记 的告知不受开关影响。"""
    async with World(
        config={
            "use_persona": False,
            "auto_write_notify": False,
            "diary_op_interval": 0,
        }
    ) as world:
        inst = world.refactor
        inst.set_history(_material_history())
        inst.provider.replies[:] = [DIARY_TEXT]
        texts = await inst.run_handler("write_diary_cmd", _event("/日记"))
        assert len(texts) == 1
        assert "📖 日记写好啦" in texts[0]
        assert inst.entries(SENDER) == ["diaries/20261023.md"]


async def test_failure_notice_still_sent_when_notify_off():
    """定时写失败通知不门闸（用户需要知道失败与 /日记 手动兜底）。"""
    async with World(
        config={"use_persona": False, "auto_write_notify": False}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin.config["enable_auto_diary"] = True
        plugin.config["auto_diary_time"] = "22:00"
        inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
        plugin._auto_users = {SENDER: {"umo": UMO, "name": "阿罗"}}
        inst.set_history(_material_history())
        inst.provider.fail_with = "boom"
        await plugin._run_auto_diaries("20261023")
        assert inst.entries(SENDER) == []
        assert len(inst.ctx.sent_messages) == 1
        assert "今天的自动日记没能写成" in inst.ctx.sent_messages[0][1]


async def test_no_material_reset_hint_still_sent_when_notify_off():
    """哨兵「没有新素材」的 /reset 提示不门闸（上下文还在涨，必须告知）。"""
    async with World(
        config={
            "use_persona": False,
            "auto_write_notify": False,
            "context_token_limit": 60000,
            "remind_ratio": 0.75,
            "auto_ratio": 0.9,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        event = _event()
        await plugin.on_llm_request(event, _request(prompt="/日记 " + "x" * 99000))
        sent = event.result_texts()
        assert inst.entries(SENDER) == []
        assert len(sent) == 1
        assert "没有新的聊天内容" in sent[0]
        assert "/reset" in sent[0]


def test_conf_schema_and_i18n_declare_auto_write_notify():
    """schema 键位/类型/默认值/condition 与 i18n 文案。"""
    root = env.REFACTOR_DIR
    schema = json.loads(
        (root / "_conf_schema.json").read_text(encoding="utf-8-sig")
    )
    assert "auto_write_notify" in schema
    keys = list(schema)
    assert keys.index("auto_write_notify") == keys.index("auto_diary_time") + 1
    cfg = schema["auto_write_notify"]
    assert cfg["type"] == "bool"
    assert cfg["default"] is True
    assert cfg["condition"] == {"enable_auto_diary": True}
    assert cfg["description"] == "自动写日记完成后告知用户"
    assert cfg["hint"]

    i18n = json.loads(
        (root / ".astrbot-plugin" / "i18n" / "zh-CN.json").read_text(
            encoding="utf-8-sig"
        )
    )
    entry = i18n["config"]["auto_write_notify"]
    assert entry["description"] == cfg["description"]
    assert entry["hint"] == cfg["hint"]
