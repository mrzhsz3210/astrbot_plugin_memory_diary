# -*- coding: utf-8 -*-
"""Daily auto-diary tick, user registration and lifecycle differential tests."""

from __future__ import annotations

from datetime import timedelta

from harness.fakes import make_event
from harness.world import DIARY_TEXT, FIXED_NOW, World, message

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"


def _private_event(text: str = "你好", **kwargs):
    return make_event(text, sender_id=SENDER, sender_name="阿罗", umo=UMO, **kwargs)


def _group_event(text: str = "你好"):
    return make_event(
        text,
        sender_id=SENDER,
        sender_name="阿罗",
        group_id="88888",
        self_id="99999",
    )


async def test_tick_gates_disabled_before_time_invalid_and_grace():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin._auto_users = {SENDER: {"umo": UMO, "name": "阿罗"}}
            no_state = {}

            plugin.config["enable_auto_diary"] = False
            await plugin._auto_diary_tick()
            no_state["disabled"] = plugin._auto_diary_state_path().exists()

            plugin.config["enable_auto_diary"] = True
            plugin.config["auto_diary_time"] = "22:00"
            inst.set_now(FIXED_NOW.replace(hour=20, minute=30))
            await plugin._auto_diary_tick()
            no_state["before_time"] = plugin._auto_diary_state_path().exists()

            plugin.config["auto_diary_time"] = "not-a-time"
            inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
            await plugin._auto_diary_tick()
            no_state["invalid_time"] = plugin._auto_diary_state_path().exists()

            plugin.config["auto_diary_time"] = "22:00"
            inst.set_now(FIXED_NOW.replace(hour=22, minute=10))
            await plugin._auto_diary_tick()
            no_state["past_grace"] = plugin._auto_diary_state_path().exists()

            return {
                "no_state": no_state,
                "entries": inst.entries(SENDER),
                "provider_calls": len(inst.provider.calls),
                "sent": list(inst.ctx.sent_messages),
                "parse": [
                    plugin._parse_auto_diary_time(),
                    plugin._parse_auto_diary_time(),
                ],
            }

        pair = await world.run_pair(scenario, label="auto tick gates")
        value = pair.value
        assert value["no_state"] == {
            "disabled": False,
            "before_time": False,
            "invalid_time": False,
            "past_grace": False,
        }
        assert value["entries"] == []
        assert value["provider_calls"] == 0
        assert value["sent"] == []


async def test_tick_triggers_auto_diary_and_daily_dedup():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin.config["enable_auto_diary"] = True
            plugin.config["auto_diary_time"] = "22:00"
            inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
            await plugin.on_any_message(_private_event("你好呀"))
            registered = dict(plugin._auto_users)

            history = [
                message("user", "今天聊了考试"),
                message("assistant", "祝你好运"),
            ]
            inst.set_history(history)
            inst.provider.replies[:] = [DIARY_TEXT]
            await plugin._auto_diary_tick()
            first_state = plugin._load_auto_state()
            first_entries = inst.entries(SENDER)
            first_sent = list(inst.ctx.sent_messages)

            # Same day: tick is a no-op.
            await plugin._auto_diary_tick()
            second_sent = list(inst.ctx.sent_messages)

            # Next day, new material: writes one more day.
            inst.set_now(FIXED_NOW.replace(day=24, hour=22, minute=0))
            inst.set_history(
                history
                + [
                    message("user", "第二天的新消息"),
                    message("assistant", "第二天的回复"),
                ]
            )
            await plugin._auto_diary_tick()
            second_state = plugin._load_auto_state()
            second_entries = inst.entries(SENDER)
            return {
                "registered": registered,
                "users_file": plugin._auto_diary_users_path().exists(),
                "first_state": first_state,
                "first_entries": first_entries,
                "first_sent": first_sent,
                "sent_after_same_day_tick": len(second_sent),
                "second_state": second_state,
                "second_entries": second_entries,
                "provider_calls": len(inst.provider.calls),
                "content_has_date": "2026-10-24" in plugin._load_latest(SENDER)[0]
                or "10-24" in plugin._load_latest(SENDER)[0],
            }

        pair = await world.run_pair(scenario, label="auto tick write")
        value = pair.value
        assert value["registered"] == {SENDER: {"umo": UMO, "name": "阿罗", "updated": "2026-10-23 22:00:00"}}
        assert value["first_state"]["last_trigger_day"] == "20261023"
        assert value["first_entries"] == ["diaries/20261023.md"]
        assert len(value["first_sent"]) == 1
        assert "今天约定的完整日记写好啦" in value["first_sent"][0][1]
        assert value["sent_after_same_day_tick"] == 1
        assert value["second_state"]["last_trigger_day"] == "20261024"
        assert value["second_entries"] == [
            "diaries/20261023.md",
            "diaries/20261024.md",
        ]
        assert value["provider_calls"] == 2


async def test_run_auto_skips_full_no_material_and_missing_umo():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin.config["enable_auto_diary"] = True
            plugin.config["auto_diary_time"] = "22:00"
            inst.set_now(FIXED_NOW.replace(hour=22, minute=0))

            # Already has a full diary today -> skip.
            plugin._save_diary(
                SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
            )
            plugin._auto_users = {
                SENDER: {"umo": UMO, "name": "阿罗"},
                "10002": {"umo": "", "name": "无会话"},
                "10003": {"umo": "aiocqhttp:FriendMessage:10003", "name": "无素材"},
            }
            await plugin._run_auto_diaries("20261023")
            sent = list(inst.ctx.sent_messages)

            # A user already in _writing is skipped as well. 锁的粒度是 sender_id。
            plugin._writing.add("10003")
            await plugin._run_auto_diaries("20261023")
            after_writing = list(inst.ctx.sent_messages)
            plugin._writing.discard("10003")

            return {
                "sent": sent,
                "after_writing": after_writing,
                "entries": inst.entries(SENDER),
                "provider_calls": len(inst.provider.calls),
            }

        pair = await world.run_pair(scenario, label="auto skips")
        value = pair.value
        assert value["sent"] == []
        assert value["after_writing"] == []
        assert value["entries"] == ["diaries/20261023.md"]
        assert value["provider_calls"] == 0


async def test_run_auto_notifies_failure_once():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin.config["enable_auto_diary"] = True
            plugin.config["auto_diary_time"] = "22:00"
            inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
            inst.set_history([message("user", "有素材")])
            inst.provider.fail_with = "boom"
            plugin._auto_users = {SENDER: {"umo": UMO, "name": "阿罗"}}
            await plugin._run_auto_diaries("20261023")
            sent = list(inst.ctx.sent_messages)
            state = plugin._load_auto_state()
            return {
                "sent": sent,
                "entries": inst.entries(SENDER),
                "state": state,
                "users": dict(plugin._auto_users),
            }

        pair = await world.run_pair(scenario, label="auto failure")
        value = pair.value
        assert len(value["sent"]) == 1
        assert "今天的自动日记没能写成" in value["sent"][0][1]
        assert "这次不会自动重试" in value["sent"][0][1]
        assert value["entries"] == []


async def test_seed_users_from_conversations_and_initialize():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            mgr = inst.conversation_manager
            mgr.set("aiocqhttp:FriendMessage:10001", history=[message("user", "私聊")])
            mgr.set("aiocqhttp:GroupMessage:88888", history=[message("user", "群聊")])
            await plugin._seed_auto_users_from_conversations()
            seeded = dict(plugin._auto_users)
            users_file_exists = plugin._auto_diary_users_path().exists()

            # initialize() reloads the user table and starts the background task.
            plugin._auto_users = {}
            await plugin.initialize()
            task_running = plugin._auto_task is not None and not plugin._auto_task.done()
            reloaded = dict(plugin._auto_users)
            await plugin.terminate()
            return {
                "seeded": seeded,
                "users_file_exists": users_file_exists,
                "reloaded": reloaded,
                "task_running": task_running,
                "task_after_terminate": plugin._auto_task,
                "terminated": plugin._terminated,
                "state_cleared": {
                    "injected": plugin._injected_cid,
                    "pending": plugin._pending_inject,
                    "watch": plugin._watch_state,
                    "writing": plugin._writing,
                    "op_last": plugin._op_last,
                    "reset": plugin._reset_pending,
                },
            }

        pair = await world.run_pair(scenario, label="seed users/initialize")
        value = pair.value
        assert list(value["seeded"].keys()) == [SENDER]
        assert value["seeded"][SENDER]["umo"] == UMO
        assert value["users_file_exists"] is True
        assert value["task_running"] is True
        assert value["reloaded"] == value["seeded"] or list(value["reloaded"]) == [SENDER]
        assert value["task_after_terminate"] is None
        assert value["terminated"] is True
        assert value["state_cleared"] == {
            "injected": {},
            "pending": {},
            "watch": {},
            "writing": set(),
            "op_last": {},
            "reset": set(),
        }


async def test_auto_diary_user_registration_rules_and_reset_flag():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            await plugin.on_any_message(_private_event("你好"))
            private_users = dict(plugin._auto_users)
            await plugin.on_any_message(_group_event("群里好"))
            group_does_not_register = dict(plugin._auto_users)

            # Messages sent by the bot itself are ignored entirely.
            self_event = make_event("自己", sender_id="99999", self_id="99999", umo=UMO)
            await plugin.on_any_message(self_event)
            self_ignored = dict(plugin._auto_users)

            await plugin.on_any_message(_private_event("/reset"))
            reset_pending = set(plugin._reset_pending)
            await plugin.on_any_message(_private_event("普通消息"))
            reset_pending_unchanged = set(plugin._reset_pending)

            parse = {}
            for raw in ("22:00", "7:05", "7：05", "24:00", "bad", ""):
                plugin.config["auto_diary_time"] = raw
                parse[raw] = plugin._parse_auto_diary_time()
            return {
                "private_users": private_users,
                "after_group": group_does_not_register,
                "self_ignored": self_ignored,
                "reset_pending": reset_pending,
                "reset_unchanged": reset_pending_unchanged,
                "parse": parse,
                "users_file": plugin._save_auto_users(),
            }

        pair = await world.run_pair(scenario, label="auto registration")
        value = pair.value
        assert list(value["private_users"]) == [SENDER]
        assert value["after_group"] == value["private_users"]
        assert value["self_ignored"] == value["private_users"]
        assert value["reset_pending"] == {UMO}
        assert value["reset_unchanged"] == {UMO}
        assert value["parse"]["22:00"] == [22, 0]
        assert value["parse"]["7:05"] == [7, 5]
        assert value["parse"]["24:00"] is None
        assert value["parse"]["bad"] is None
        assert value["parse"][""] == [22, 0]


async def test_auto_state_roundtrip_and_tick_without_users():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin.config["enable_auto_diary"] = True
            plugin.config["auto_diary_time"] = "22:00"
            inst.set_now(FIXED_NOW.replace(hour=22, minute=1))
            await plugin._auto_diary_tick()
            state_file = plugin._load_auto_state()
            plugin._auto_users = {}
            await plugin._auto_diary_tick()  # same day -> no-op
            after = plugin._load_auto_state()
            return {
                "state_file": state_file,
                "after": after,
                "sent": list(inst.ctx.sent_messages),
                "provider_calls": len(inst.provider.calls),
            }

        pair = await world.run_pair(scenario, label="auto no users")
        value = pair.value
        assert value["state_file"]["last_trigger_day"] == "20261023"
        assert value["state_file"]["last_trigger_at"] == "2026-10-23 22:01:00"
        assert value["sent"] == []
        assert value["provider_calls"] == 0
