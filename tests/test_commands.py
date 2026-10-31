# -*- coding: utf-8 -*-
"""Command-level differential tests (async-generator handlers, real filters)."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import shutil
import zipfile
from datetime import timedelta
from pathlib import Path

from harness import compare
from harness.fakes import at_other, file_component, make_event
from harness.world import (
    BRIEF_TEXT,
    DIARY_TEXT,
    LIFE_TEXT,
    World,
    message,
)

SENDER = "10001"
SENDER_NAME = "阿罗"


def private_event(text: str, **kwargs):
    kwargs.setdefault("captured_dir", None)
    return make_event(text, sender_id=SENDER, sender_name=SENDER_NAME, **kwargs)


def group_event(text: str, *, messages=None):
    return make_event(
        text,
        sender_id=SENDER,
        sender_name=SENDER_NAME,
        group_id="88888",
        self_id="99999",
        messages=messages,
    )


def _seed_history(inst, count: int = 2, chars: int = 10):
    history = []
    for i in range(count):
        history.append(message("user", f"用户消息{i} " + "x" * chars))
        history.append(message("assistant", f"回复{i} " + "y" * chars))
    inst.set_history(history)


def inst_umo() -> str:
    return "aiocqhttp:FriendMessage:10001"


async def test_help_command_private_and_group_addressing():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            private = await inst.run_handler("diary_help", private_event("/日记帮助"))
            group_plain = await inst.run_handler("diary_help", group_event("/日记帮助"))
            group_at_me = await inst.run_handler(
                "diary_help", group_event("/日记帮助", messages=[at_other("99999")])
            )
            group_at_other = await inst.run_handler(
                "diary_help", group_event("/日记帮助", messages=[at_other("12345")])
            )
            inst.plugin.config["group_require_at"] = True
            group_require_no_at = await inst.run_handler(
                "diary_help", group_event("/日记帮助")
            )
            return {
                "private": private,
                "group_plain": group_plain,
                "group_at_me": group_at_me,
                "group_at_other": group_at_other,
                "group_require_no_at": group_require_no_at,
            }

        pair = await world.run_pair(scenario, label="help")
        value = pair.value
        assert "📖 记忆日记 · 指令一览" in value["private"][0]
        assert "本群多 AI 防冲突" in value["group_plain"][0]
        # v0.6：帮助菜单新增 /形象（插图形象档案）。差分比较前已在
        # harness.normalise_text 里归一掉这一行（pair.value 是归一后的文本），
        # 所以这里直接驱动 refactor 侧原始输出，显式钉住它确实存在。
        raw_help = await world.refactor.run_handler(
            "diary_help", private_event("/日记帮助")
        )
        assert "/形象" in raw_help[0]
        assert "查看/确认/修改/重拟" in raw_help[0]
        # v0.6：帮助菜单新增 /查看批注、/写回批注（同样在差分前被归一掉）
        assert "/查看批注" in raw_help[0]
        assert "/写回批注" in raw_help[0]
        assert "私聊专用" in raw_help[0].split("/查看批注")[1][:40]
        assert value["group_at_other"] == []
        assert value["group_require_no_at"] == []


async def test_memo_add_and_view_commands():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            empty_view = await inst.run_handler("memo_view", private_event("/查看备忘"))
            no_arg = await inst.run_handler("memo_cmd", private_event("/记一下"))
            added = await inst.run_handler(
                "memo_cmd", private_event("/记一下 我下周三要考试")
            )
            added_group = await inst.run_handler(
                "memo_cmd", group_event("/记一下 群里的事")
            )
            view = await inst.run_handler("memo_view", private_event("/查看备忘"))
            group_view = await inst.run_handler("memo_view", group_event("/查看备忘"))
            fullwidth = await inst.run_handler(
                "memo_cmd", private_event("/记一下　全角空格备忘")
            )
            return {
                "empty_view": empty_view,
                "no_arg": no_arg,
                "added": added,
                "added_group": added_group,
                "view": view,
                "group_view": group_view,
                "fullwidth": fullwidth,
                "memos": plugin._load_memos(SENDER),
                "private_count": len(plugin._load_memos(SENDER, "private")),
                "group_count": len(plugin._load_memos(SENDER, "group")),
            }

        pair = await world.run_pair(scenario, label="memo add/view")
        value = pair.value
        assert "📌 已记下" in value["added"][0]
        assert "私聊备忘" in value["added"][0]
        assert "群聊备忘" in value["added_group"][0]
        assert "你目前有 2 条备忘" in value["view"][0]
        assert "请私聊我查看" in value["group_view"][0]
        assert value["fullwidth"][0].startswith("📌 已记下：「全角空格备忘」")


async def test_memo_delete_interactive_and_non_interactive():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            for text in ("备忘A", "备忘B", "备忘C"):
                plugin._append_memo(SENDER, text, scene="private")
            group_refusal = await inst.run_handler(
                "memo_delete", group_event("/删除备忘")
            )
            inst.set_replies([{"text": "1"}])
            one = await inst.run_handler("memo_delete", private_event("/删除备忘"))
            remaining_after_one = [m["text"] for m in plugin._load_memos(SENDER)]

            inst.set_replies([{"text": "全部"}])
            all_ = await inst.run_handler("memo_delete", private_event("/删除备忘"))
            remaining_after_all = plugin._load_memos(SENDER)

            inst.set_replies([{"text": "取消"}])
            plugin._append_memo(SENDER, "取消测试", scene="private")
            cancelled = await inst.run_handler(
                "memo_delete", private_event("/删除备忘")
            )
            still_there = [m["text"] for m in plugin._load_memos(SENDER)]

            inst.set_session_supported(False)
            fallback = await inst.run_handler(
                "memo_delete", private_event("/删除备忘")
            )
            fallback_memos = [m["text"] for m in plugin._load_memos(SENDER)]
            inst.set_session_supported(True)
            return {
                "group_refusal": group_refusal,
                "one": one,
                "remaining_after_one": remaining_after_one,
                "all_": all_,
                "remaining_after_all": remaining_after_all,
                "cancelled": cancelled,
                "still_there": still_there,
                "fallback": fallback,
                "fallback_memos": fallback_memos,
            }

        pair = await world.run_pair(scenario, label="memo delete")
        value = pair.value
        assert "请私聊我操作" in value["group_refusal"][0]
        assert value["remaining_after_one"] == ["备忘B", "备忘C"]
        assert value["remaining_after_all"] == []
        assert value["still_there"] == ["取消测试"]
        assert "未做任何改动" in value["fallback"][-1]
        assert value["fallback_memos"] == ["取消测试"]


async def test_write_diary_command_first_full_then_brief():
    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            history = [
                message("user", "第一条用户消息"),
                message("assistant", "第一条回复"),
            ]
            inst.set_history(history)
            inst.provider.replies[:] = [DIARY_TEXT, BRIEF_TEXT]
            first = await inst.run_handler("write_diary_cmd", private_event("/日记"))
            content1 = plugin._load_latest(SENDER)[0]
            content1_info = [
                plugin._segment_info(s["header"]) for s in plugin._segments(content1)
            ]

            inst.set_history(
                history
                + [
                    message("user", "第二条用户消息"),
                    message("assistant", "第二条回复"),
                ]
            )
            second = await inst.run_handler("write_diary_cmd", private_event("/日记 简记"))
            content2 = plugin._load_latest(SENDER)[0]
            content2_info = [
                plugin._segment_info(s["header"]) for s in plugin._segments(content2)
            ]

            third = await inst.run_handler("write_diary_cmd", private_event("/日记 完整"))
            bad_arg = await inst.run_handler(
                "write_diary_cmd", private_event("/日记 乱写")
            )
            return {
                "first": first,
                "second": second,
                "third": third,
                "bad_arg": bad_arg,
                "days": inst.entries(SENDER),
                "content1_info": content1_info,
                "content2_info": content2_info,
                "calls": len(inst.provider.calls),
                # v0.5.5 起日记提示词在 refactor 侧口吻化，两边 system 内容
                # 不再逐字节一致，不再做 call_digest 差分；语义断言见下方
            }

        pair = await world.run_pair(scenario, label="write command")
        value = pair.value
        assert value["first"][0].startswith("📖 日记写好啦")
        assert value["days"] == ["diaries/20261023.md"]
        assert [i["mode"] for i in value["content1_info"]] == ["full"]
        assert [i["mode"] for i in value["content2_info"]] == ["full", "brief"]
        assert "今天这个场景已经写过 1 篇完整日记" in value["third"][0]
        assert "用法" in value["bad_arg"][0]
        assert value["calls"] == 2


async def test_write_diary_mode_and_cooldown_gates():
    async with World(config={"use_persona": False, "diary_op_interval": 5}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            base_history = [
                message("user", "用户A"),
                message("assistant", "回复A"),
                message("user", "用户B"),
                message("assistant", "回复B"),
            ]
            inst.set_history(base_history)
            inst.provider.replies[:] = [DIARY_TEXT, LIFE_TEXT, BRIEF_TEXT]
            first = await inst.run_handler("write_diary_cmd", private_event("/日记"))
            cooldown = await inst.run_handler("write_diary_cmd", private_event("/日记"))
            inst.set_now(inst.now + timedelta(minutes=6))
            after_cooldown = await inst.run_handler(
                "write_diary_cmd", private_event("/日记")
            )
            inst.set_now(inst.now + timedelta(minutes=10))
            plugin.config["write_min_ratio"] = 0.5
            gated = await inst.run_handler("write_diary_cmd", private_event("/日记"))
            long_history = base_history + [
                message("user", "长" * 200) if i % 2 == 0 else message("assistant", "回" * 200)
                for i in range(400)
            ]
            inst.set_history(long_history)
            allowed = await inst.run_handler("write_diary_cmd", private_event("/日记"))
            return {
                "first": first,
                "cooldown": cooldown,
                "after_cooldown": after_cooldown,
                "gated": gated,
                "allowed": allowed,
                "entries": inst.entries(SENDER),
            }

        pair = await world.run_pair(scenario, label="write gates")
        value = pair.value
        assert "太频繁" in value["cooldown"][0]
        assert value["after_cooldown"][0].startswith("📖 日记写好啦")
        assert "还没到该写日记的时候" in value["gated"][0]
        assert value["allowed"][0].startswith("📖 日记写好啦")


async def test_write_diary_life_and_no_material_paths():
    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            inst.provider.replies[:] = [LIFE_TEXT, LIFE_TEXT, DIARY_TEXT]
            first = await inst.run_handler("write_diary_cmd", private_event("/日记"))
            content = plugin._load_latest(SENDER)[0]
            second = await inst.run_handler("write_diary_cmd", private_event("/日记"))
            return {
                "first": first,
                "second": second,
                "content": content,
                "has_life_marker": "无新对话" in content,
            }

        pair = await world.run_pair(scenario, label="life diary")
        assert pair.value["has_life_marker"] is True
        assert "随笔已经写过" in pair.value["second"][0]


async def test_write_diary_life_disabled_and_provider_missing():
    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin.config["empty_diary_as_life"] = False
            no_material = await inst.run_handler(
                "write_diary_cmd", private_event("/日记")
            )
            plugin.config["empty_diary_as_life"] = True
            _seed_history(inst, count=2)
            inst.ctx.provider = None
            no_provider = await inst.run_handler("write_diary_cmd", private_event("/日记"))
            inst.ctx.provider = inst.provider
            inst.provider.fail_with = "boom"
            failed = await inst.run_handler("write_diary_cmd", private_event("/日记"))
            inst.provider.fail_with = None
            return {
                "no_material": no_material,
                "no_provider": no_provider,
                "failed": failed,
            }

        await world.run_pair(scenario, label="write failures")


async def test_view_diary_non_interactive_and_interactive():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            empty = await inst.run_handler("view_diary", private_event("/查看日记"))
            plugin._save_diary(
                SENDER, DIARY_TEXT, sender_name=SENDER_NAME, origin="private", mode="full"
            )
            plugin._save_diary(
                SENDER,
                "第二天的日记",
                sender_name=SENDER_NAME,
                entry_name_hint="20261022",
                origin="private",
                mode="full",
            )
            group = await inst.run_handler("view_diary", group_event("/查看日记"))

            inst.set_session_supported(False)
            fallback = await inst.run_handler("view_diary", private_event("/查看日记"))
            inst.set_session_supported(True)

            inst.set_replies([{"text": "最新"}])
            latest = await inst.run_handler("view_diary", private_event("/查看日记"))
            inst.set_replies([{"text": "1"}])
            first = await inst.run_handler("view_diary", private_event("/查看日记"))
            inst.set_replies([{"text": "2026-10-22"}])
            by_date = await inst.run_handler("view_diary", private_event("/查看日记"))
            inst.set_replies([{"text": "2026-10-01"}])
            no_day = await inst.run_handler("view_diary", private_event("/查看日记"))
            inst.set_replies([{"text": "乱七八糟"}])
            unparsed = await inst.run_handler("view_diary", private_event("/查看日记"))
            inst.set_replies([{"text": "取消"}])
            cancelled = await inst.run_handler("view_diary", private_event("/查看日记"))
            inst.set_replies([{"raise": "TimeoutError"}])
            timeout = await inst.run_handler("view_diary", private_event("/查看日记"))
            return {
                "empty": empty,
                "group": group,
                "fallback": fallback,
                "latest": latest,
                "first": first,
                "by_date": by_date,
                "no_day": no_day,
                "unparsed": unparsed,
                "cancelled": cancelled,
                "timeout": timeout,
            }

        pair = await world.run_pair(scenario, label="view diary")
        value = pair.value
        assert "还没有写过日记" in value["empty"][0]
        assert "请私聊我查看" in value["group"][0]
        assert any("最新" in t for t in value["fallback"])
        assert any(DIARY_TEXT[:12] in t for t in value["latest"])
        assert "没有写日记" in value["no_day"][-1]
        assert "没看懂" in value["unparsed"][1]
        assert "超时" in value["timeout"][-1]


async def test_view_diary_corrupt_and_tampered_states():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            path = inst.diary_path(SENDER)
            plugin._save_diary(
                SENDER, DIARY_TEXT, sender_name=SENDER_NAME, origin="private", mode="full"
            )
            entries = compare.zip_entries(path)
            entries["diaries/20261023.md"] = entries["diaries/20261023.md"] + b"\nX"
            plugin._write_zip_atomic(path, entries)
            inst.set_session_supported(False)
            tampered = await inst.run_handler("view_diary", private_event("/查看日记"))
            inst.set_session_supported(True)
            path.write_bytes(b"broken")
            corrupt = await inst.run_handler("view_diary", private_event("/查看日记"))
            return {"tampered": tampered, "corrupt": corrupt}

        pair = await world.run_pair(scenario, label="view corrupt/tampered")
        value = pair.value
        assert any("篡改" in text for text in value["tampered"])
        assert "损坏" in value["corrupt"][0]


async def test_delete_diary_interactive_flows():
    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            empty = await inst.run_handler("delete_diary", private_event("/删除日记"))
            for day in ("20261020", "20261021", "20261022", "20261023"):
                plugin._save_diary(
                    SENDER, f"{day}内容", entry_name_hint=day, origin="private", mode="full"
                )
            group = await inst.run_handler("delete_diary", group_event("/删除日记"))

            inst.set_replies([{"text": "1"}])
            by_index = await inst.run_handler("delete_diary", private_event("/删除日记"))
            after_index = inst.entries(SENDER)

            inst.set_replies([{"text": "2026-10-21 到 2026-10-22"}])
            by_range = await inst.run_handler("delete_diary", private_event("/删除日记"))
            after_range = inst.entries(SENDER)

            inst.set_replies([{"text": "全部"}, {"text": "取消"}])
            all_cancelled = await inst.run_handler(
                "delete_diary", private_event("/删除日记")
            )
            after_cancel = inst.entries(SENDER)

            inst.set_replies([{"text": "全部"}, {"text": "确认删除"}])
            all_confirmed = await inst.run_handler(
                "delete_diary", private_event("/删除日记")
            )
            after_all = inst.entries(SENDER)

            plugin._save_diary(
                SENDER, "再次写入", entry_name_hint="20261023", origin="private", mode="full"
            )
            inst.set_replies([{"text": "2026-10-01"}])
            empty_day = await inst.run_handler("delete_diary", private_event("/删除日记"))
            inst.set_replies([{"text": "猜猜"}])
            unparsed = await inst.run_handler("delete_diary", private_event("/删除日记"))
            return {
                "empty": empty,
                "group": group,
                "by_index": by_index,
                "after_index": after_index,
                "by_range": by_range,
                "after_range": after_range,
                "all_cancelled": all_cancelled,
                "after_cancel": after_cancel,
                "all_confirmed": all_confirmed,
                "after_all": after_all,
                "empty_day": empty_day,
                "unparsed": unparsed,
            }

        pair = await world.run_pair(scenario, label="delete diary")
        value = pair.value
        assert "没有可删除的日记" in value["empty"][0]
        assert value["after_index"] == [
            "diaries/20261021.md",
            "diaries/20261022.md",
            "diaries/20261023.md",
        ]
        assert value["after_range"] == ["diaries/20261023.md"]
        assert value["after_cancel"] == ["diaries/20261023.md"]
        assert value["after_all"] == []
        assert "没有日记" in value["empty_day"][-1]
        assert "没看懂" in value["unparsed"][1]


async def test_delete_diary_non_interactive_fallback_and_cooldown():
    async with World(config={"use_persona": False, "diary_op_interval": 5}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin._save_diary(
                SENDER, DIARY_TEXT, sender_name=SENDER_NAME, origin="private", mode="full"
            )
            plugin._op_mark(SENDER)
            cooldown = await inst.run_handler("delete_diary", private_event("/删除日记"))
            inst.set_now(inst.now + timedelta(minutes=6))
            inst.set_session_supported(False)
            fallback = await inst.run_handler("delete_diary", private_event("/删除日记"))
            entries = inst.entries(SENDER)
            inst.set_session_supported(True)
            return {
                "cooldown": cooldown,
                "fallback": fallback,
                "entries": entries,
            }

        pair = await world.run_pair(scenario, label="delete cooldown/fallback")
        assert "太频繁" in pair.value["cooldown"][0]
        assert "不支持交互式删除" in pair.value["fallback"][-1]
        assert pair.value["entries"] == ["diaries/20261023.md"]


async def test_status_command_states():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            _seed_history(inst, count=2)
            empty = await inst.run_handler("diary_status", private_event("/日记状态"))
            plugin._save_diary(
                SENDER, DIARY_TEXT, sender_name=SENDER_NAME, origin="private", mode="full"
            )
            with_diary = await inst.run_handler("diary_status", private_event("/日记状态"))
            plugin._injected_cid[inst.default_umo()] = "cid-1"
            injected = await inst.run_handler("diary_status", private_event("/日记状态"))
            plugin.config["enable_context_watch"] = False
            watch_off = await inst.run_handler("diary_status", private_event("/日记状态"))
            return {
                "empty": empty,
                "with_diary": with_diary,
                "injected": injected,
                "watch_off": watch_off,
            }

        pair = await world.run_pair(scenario, label="status")
        value = pair.value
        assert "还没有写过日记" in value["empty"][0]
        assert "共 1 天的日记" in value["with_diary"][0]
        assert "本会话已注入" in value["injected"][0]
        assert "哨兵：已关闭" in value["watch_off"][0]


async def test_scope_command_set_and_query():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            initial = await inst.run_handler("diary_scope", private_event("/日记范围"))
            set_latest = await inst.run_handler(
                "diary_scope", private_event("/日记范围 最新")
            )
            stored_latest = plugin._load_pref(SENDER)
            query_latest = await inst.run_handler("diary_scope", private_event("/日记范围"))
            set_days = await inst.run_handler(
                "diary_scope", private_event("/日记范围 最近7天")
            )
            set_range = await inst.run_handler(
                "diary_scope", private_event("/日记范围 2026-10-01 到 2026-10-23")
            )
            stored_range = plugin._load_pref(SENDER)
            reset_default = await inst.run_handler(
                "diary_scope", private_event("/日记范围 默认")
            )
            stored_default = plugin._load_pref(SENDER)
            invalid = await inst.run_handler(
                "diary_scope", private_event("/日记范围 乱七八糟")
            )
            return {
                "initial": initial,
                "set_latest": set_latest,
                "stored_latest": stored_latest,
                "query_latest": query_latest,
                "set_days": set_days,
                "set_range": set_range,
                "stored_range": stored_range,
                "reset_default": reset_default,
                "stored_default": stored_default,
                "invalid": invalid,
            }

        pair = await world.run_pair(scenario, label="scope")
        value = pair.value
        assert value["stored_latest"] == "latest"
        assert value["stored_range"] == "range:20261001-20261023"
        assert value["stored_default"] is None
        assert "没看懂" in value["invalid"][0]
        assert "最近 7 天（默认）" in value["initial"][0]


async def test_inject_command_and_pending_state():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            no_entries = await inst.run_handler(
                "diary_inject", private_event("/注入日记 最新")
            )
            plugin._save_diary(
                SENDER, DIARY_TEXT, sender_name=SENDER_NAME, origin="private", mode="full"
            )
            no_arg = await inst.run_handler("diary_inject", private_event("/注入日记"))
            invalid = await inst.run_handler(
                "diary_inject", private_event("/注入日记 乱写")
            )
            ok = await inst.run_handler("diary_inject", private_event("/注入日记 最新"))
            pending = dict(plugin._pending_inject)
            # Simulate the next LLM request having delivered the pending scope.
            plugin._injected_entries[inst.default_umo()] = {
                "cid": "",
                "names": {"diaries/20261023.md"},
            }
            again = await inst.run_handler("diary_inject", private_event("/注入日记 最新"))
            no_range = await inst.run_handler(
                "diary_inject", private_event("/注入日记 2026-01-01")
            )
            return {
                "no_entries": no_entries,
                "no_arg": no_arg,
                "invalid": invalid,
                "ok": ok,
                "pending": pending,
                "again": again,
                "no_range": no_range,
            }

        pair = await world.run_pair(scenario, label="inject command")
        value = pair.value
        assert "还没有日记可以注入" in value["no_entries"][0]
        assert value["pending"] == {inst_umo(): "latest"}
        assert "已经读过" in value["again"][0]
        assert "没有日记" in value["no_range"][-1]


async def test_password_command_lifecycle():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            initial = await inst.run_handler("diary_password", private_event("/日记密码"))
            group = await inst.run_handler("diary_password", group_event("/日记密码"))
            short = await inst.run_handler(
                "diary_password", private_event("/日记密码 设置 123")
            )
            set_ok = await inst.run_handler(
                "diary_password", private_event("/日记密码 设置 secret123")
            )
            rec = plugin._load_pref_data(SENDER).get("view_password") or {}
            verify = {
                "algo": rec.get("algo"),
                "iter": rec.get("iter"),
                "salt_len": len(rec.get("salt", "")),
                "hash_len": len(rec.get("hash", "")),
                "right": plugin._verify_view_password(rec, "secret123"),
                "wrong": plugin._verify_view_password(rec, "nope"),
            }
            set_again = await inst.run_handler(
                "diary_password", private_event("/日记密码 设置 other123")
            )
            modify_wrong = await inst.run_handler(
                "diary_password", private_event("/日记密码 修改 wrong newpass1")
            )
            modify_ok = await inst.run_handler(
                "diary_password", private_event("/日记密码 修改 secret123 newpass1")
            )
            verify_new = plugin._verify_view_password(
                plugin._load_pref_data(SENDER)["view_password"], "newpass1"
            )
            cancel_wrong = await inst.run_handler(
                "diary_password", private_event("/日记密码 取消 badpass")
            )
            cancel_ok = await inst.run_handler(
                "diary_password", private_event("/日记密码 取消 newpass1")
            )
            rec_after = plugin._load_pref_data(SENDER).get("view_password")
            usage = await inst.run_handler(
                "diary_password", private_event("/日记密码 设置")
            )
            return {
                "initial": initial,
                "group": group,
                "short": short,
                "set_ok": set_ok,
                "verify": verify,
                "set_again": set_again,
                "modify_wrong": modify_wrong,
                "modify_ok": modify_ok,
                "verify_new": verify_new,
                "cancel_wrong": cancel_wrong,
                "cancel_ok": cancel_ok,
                "rec_after": rec_after,
                "usage": usage,
            }

        pair = await world.run_pair(
            scenario,
            label="password",
            snapshot_normalise=_password_snapshot_normaliser(),
        )
        value = pair.value
        assert "未设置" in value["initial"][0]
        assert "密码要 4~32 位" in value["short"][0]
        assert value["verify"]["algo"] == "pbkdf2-sha256"
        assert value["verify"]["iter"] == 100000
        assert value["verify"]["right"] is True and value["verify"]["wrong"] is False
        assert "已经设置过" in value["set_again"][0]
        assert "旧密码不对" in value["modify_wrong"][0]
        assert value["verify_new"] is True
        assert "没有取消" in value["cancel_wrong"][0]
        assert value["rec_after"] is None
        assert "查看密码用法" in value["usage"][0]


def _password_snapshot_normaliser():
    """Blank the random PBKDF2 salt/hash in the pref file (random by design)."""

    def normalise(relpath: str, raw: bytes) -> bytes:
        if not relpath.startswith("pref_"):
            return raw
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return raw
        rec = data.get("view_password")
        if isinstance(rec, dict):
            data["view_password"] = {
                "algo": rec.get("algo"),
                "iter": rec.get("iter"),
                "salt": "<SALT>" if rec.get("salt") else "",
                "hash": "<HASH>" if rec.get("hash") else "",
            }
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    return normalise


async def test_export_command_produces_valid_bundle():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            empty = await inst.run_handler("export_diary", private_event("/导出日记"))
            group = await inst.run_handler("export_diary", group_event("/导出日记"))
            plugin._save_diary(
                SENDER, DIARY_TEXT, sender_name=SENDER_NAME, origin="private", mode="full"
            )
            event = private_event("/导出日记", captured_dir=inst.captured_dir)
            texts = await inst.run_handler("export_diary", event)
            captured = list(event.captured_files)
            bundle_info = {}
            if captured:
                bundle = captured[0]
                manifest = compare.bundle_manifest(bundle)
                with zipfile.ZipFile(bundle) as z:
                    names = sorted(z.namelist())
                    user_key = z.read("user_key.txt").decode("utf-8")
                    inner = z.read(manifest["diary_file"])
                    with zipfile.ZipFile(io.BytesIO(inner)) as inner_zip:
                        inner_entries = sorted(inner_zip.namelist())
                bundle_info = {
                    "manifest_format": manifest["format"],
                    "key_kind": manifest["key_kind"],
                    "sender_id": manifest["sender_id"],
                    "entry_count": manifest["entry_count"],
                    "names": names,
                    "user_key_len": len(user_key),
                    "user_key_hex": all(c in "0123456789abcdef" for c in user_key),
                    "inner_entries": inner_entries,
                    "readme_present": "导入说明.txt" in names,
                }
            leftovers = [
                p.name for p in inst.data_dir.iterdir() if "memory_diary_export_" in p.name
            ]
            return {
                "empty": empty,
                "group": group,
                "texts": texts,
                "bundle_info": bundle_info,
                "leftovers": leftovers,
            }

        pair = await world.run_pair(
            scenario,
            label="export command",
            snapshot_exclude=("memory_diary_export_*.zip",),
        )
        value = pair.value
        assert "还没有写过日记" in value["empty"][0]
        assert "只能私聊操作" in value["group"][0]
        info = value["bundle_info"]
        assert info["manifest_format"] == "memory-diary-export/2"
        assert info["sender_id"] == SENDER
        assert info["entry_count"] == 1
        assert info["user_key_hex"] and info["user_key_len"] == 64
        assert "导入说明.txt" in info["names"]
        assert "diaries/20261023.md" in info["inner_entries"]
        assert value["leftovers"] == []


def _copy_bundle(bundle: Path, inst, name: str) -> Path:
    """Copy a bundle into the capture dir; the import waiter deletes its upload."""
    target = inst.captured_dir / name
    shutil.copy2(bundle, target)
    return target


def _tampered_copy(inst, bundle: Path) -> Path:
    """Return a copy of an export bundle with its inner diary tampered."""
    target = inst.captured_dir / "tampered_bundle.zip"
    with zipfile.ZipFile(bundle) as src, zipfile.ZipFile(target, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name.endswith(".zip"):
                inner = zipfile.ZipFile(io.BytesIO(data))
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as out:
                    for n in inner.namelist():
                        payload = inner.read(n)
                        if n.startswith("diaries/"):
                            payload = payload + "\n篡改".encode("utf-8")
                        out.writestr(n, payload)
                data = buf.getvalue()
            dst.writestr(name, data)
    return target


async def test_import_command_round_trip_and_rejections():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            group = await inst.run_handler("import_diary", group_event("/导入日记"))
            inst.set_session_supported(False)
            fallback = await inst.run_handler("import_diary", private_event("/导入日记"))
            inst.set_session_supported(True)

            plugin._save_diary(
                SENDER, DIARY_TEXT, sender_name=SENDER_NAME, origin="private", mode="full"
            )
            bundle = plugin._build_export_bundle(
                SENDER, inst.diary_path(SENDER), inst.entries(SENDER)
            )
            original = compare.zip_entries(inst.diary_path(SENDER))
            original_content = plugin._load_latest(SENDER)[0]
            plugin._delete_diary(SENDER)
            inst.set_replies(
                [{"messages": [file_component(_copy_bundle(bundle, inst, "roundtrip.zip"))]}]
            )
            imported = await inst.run_handler("import_diary", private_event("/导入日记"))
            restored = compare.zip_entries(inst.diary_path(SENDER))
            restored_content = plugin._load_latest(SENDER)[0]

            inst.set_replies(
                [{"messages": [file_component(_copy_bundle(bundle, inst, "dup.zip"))]}]
            )
            duplicate = await inst.run_handler("import_diary", private_event("/导入日记"))

            plugin._save_diary(
                "10002", "别人的日记", sender_name="别人", origin="private", mode="full"
            )
            other_bundle = plugin._build_export_bundle(
                "10002", inst.diary_path("10002"), inst.entries("10002")
            )
            plugin._delete_diary(SENDER)
            inst.set_replies(
                [{"messages": [file_component(_copy_bundle(other_bundle, inst, "other.zip"))]}]
            )
            wrong_owner = await inst.run_handler("import_diary", private_event("/导入日记"))

            tampered = _tampered_copy(inst, bundle)
            inst.set_replies([{"messages": [file_component(tampered)]}])
            tampered_result = await inst.run_handler(
                "import_diary", private_event("/导入日记")
            )

            inst.set_replies([{"text": "取消"}])
            cancelled = await inst.run_handler("import_diary", private_event("/导入日记"))
            inst.set_replies([{"text": "没有文件"}])
            no_file = await inst.run_handler("import_diary", private_event("/导入日记"))

            plugin._delete_diary("10002")
            return {
                "group": group,
                "fallback": fallback,
                "imported": imported,
                "original_match": original == restored,
                "restored_content": restored_content,
                "original_content": original_content,
                "duplicate": duplicate,
                "wrong_owner": wrong_owner,
                "tampered_result": tampered_result,
                "cancelled": cancelled,
                "no_file": no_file,
            }

        pair = await world.run_pair(
            scenario,
            label="import command",
            snapshot_exclude=("memory_diary_export_*.zip",),
        )
        value = pair.value
        assert "只能私聊操作" in value["group"][0]
        assert "不支持交互操作" in value["fallback"][0]
        assert "导入完成" in value["imported"][-1]
        assert value["original_match"] is True
        assert value["restored_content"] == value["original_content"]
        assert "已经存在你的日记文件" in value["duplicate"][-1]
        assert "主人 QQ 号和你不一致" in value["wrong_owner"][-1]
        assert "验签失败" in value["tampered_result"][-1]
        assert "已取消导入" in value["cancelled"][-1]
        assert any("没收到文件" in text for text in value["no_file"])


async def test_import_legacy_v1_bundle_format():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            content = DIARY_TEXT
            entry = "diaries/20261023.md"
            master = plugin._secret_key()
            signature = hmac.new(
                master,
                (SENDER + "\n" + entry + "\n" + content).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            inner_buf = io.BytesIO()
            with zipfile.ZipFile(inner_buf, "w") as inner:
                inner.writestr(entry, content)
                inner.writestr("sigs/20261023.sig", signature)
            manifest = {
                "format": "memory-diary-export/1",
                "sender_id": SENDER,
                "diary_file": "diary.zip",
            }
            bundle = inst.captured_dir / "legacy_v1_bundle.zip"
            with zipfile.ZipFile(bundle, "w") as z:
                z.writestr("diary.zip", inner_buf.getvalue())
                z.writestr("diary_key.txt", master.decode("utf-8"))
                z.writestr("manifest.json", json.dumps(manifest))
            inst.set_replies(
                [
                    {
                        "messages": [
                            file_component(
                                _copy_bundle(bundle, inst, "legacy_v1_upload.zip")
                            )
                        ]
                    }
                ]
            )
            result = await inst.run_handler("import_diary", private_event("/导入日记"))
            return {
                "result": result,
                "content": plugin._load_latest(SENDER)[0],
                "entries": inst.entries(SENDER),
            }

        pair = await world.run_pair(scenario, label="import v1 bundle")
        assert pair.value["content"] == DIARY_TEXT
        assert "导入完成" in pair.value["result"][-1]
        assert pair.value["entries"] == ["diaries/20261023.md"]


async def test_persona_voice_for_notices_and_diary_system_prompt():
    async with World(
        config={"use_persona": True, "diary_op_interval": 0},
        persona_prompt="你是小深，说话温柔，习惯用「呀」结尾。",
    ) as world:
        async def scenario(inst):
            plugin = inst.plugin
            _seed_history(inst, count=1)
            inst.provider.replies[:] = ["我记下啦～", DIARY_TEXT, "日记写好啦～"]
            memo = await inst.run_handler(
                "memo_cmd", private_event("/记一下 我下周考试")
            )
            written = await inst.run_handler("write_diary_cmd", private_event("/日记"))
            calls = inst.provider.calls
            return {
                "memo": memo,
                "written": written,
                # v0.5.5 起日记提示词在 refactor 侧口吻化，call_digest 的
                # system_sha 两边不再一致，差分只保留语义字段
                "persona_in_memo_system": "你是小深" in calls[0]["system_prompt"],
                "persona_in_diary_system": "你是小深" in calls[1]["system_prompt"],
                "persona_in_notice_system": "你是小深" in calls[2]["system_prompt"],
                "diary_prompt_has_memo": "我下周考试" in calls[1]["prompt"],
                "entries": inst.entries(SENDER),
            }

        pair = await world.run_pair(scenario, label="persona voice")
        value = pair.value
        assert value["memo"][0].startswith("我记下啦～")
        assert value["written"][0].startswith("日记写好啦～")
        assert value["persona_in_memo_system"] is True
        assert value["persona_in_diary_system"] is True
        assert value["persona_in_notice_system"] is True
        assert value["diary_prompt_has_memo"] is True
        assert value["entries"] == ["diaries/20261023.md"]


# ---------------------------------------------------------------------------
# P2-2 第 3 点 / P3-19：提示词契约的 refactor-only 断言
#   差分套件里 ④ 注入模板的新增句被 World.normalise_text 整句归一化掉、
#   日记提示词也不参与字节比较，所以「四个部分 / 顺序固定 / 小标题由人设」
#   这类承诺必须在这里单独钉住，否则文案丢失不会有任何告警。
# ---------------------------------------------------------------------------


def _refactor_constants():
    import importlib

    from harness import env, loader

    main_mod = loader.get_module(env.SIDE_REFACTOR)
    return importlib.import_module(
        main_mod.__name__.rsplit(".", 1)[0] + ".memory_diary.constants"
    )


def test_diary_system_prompt_keeps_four_section_contract():
    prompt = _refactor_constants().DIARY_SYSTEM_PROMPT
    # ① 必须明确「四个部分」
    assert "四个部分" in prompt
    # ② 顺序固定且必须严格遵守
    assert "顺序" in prompt and "严格遵守" in prompt
    # ③ 小标题由 AI 按人设自由起名
    assert "小标题" in prompt
    assert "人设" in prompt
    assert "由你自由发挥" in prompt
    # P3-6：小标题不得用「——」包裹，避免与日记片段分隔条混淆
    assert "——" in prompt and "分隔条" in prompt
    # 复查补丁：用户画像必须是拟人化观察，而不是字段清单/档案表/项目符号列表
    assert "字段清单" in prompt
    assert "像人类观察" in prompt
    assert "项目符号" in prompt
    assert "编号列表" in prompt
    assert "不得编造" in prompt


def test_diary_system_prompt_keeps_four_section_order():
    """顺序必须是「时间 → 画像 → 评价与感受 → 叮嘱」，改顺序会破坏日记结构。"""
    prompt = _refactor_constants().DIARY_SYSTEM_PROMPT
    idx_time = prompt.find("起止时间")
    idx_profile = prompt.find("用户画像")
    idx_eval = prompt.find("真实看法与评价")
    idx_remind = prompt.find("叮嘱下一个自己")
    assert -1 not in (idx_time, idx_profile, idx_eval, idx_remind)
    assert idx_time < idx_profile < idx_eval < idx_remind


def test_brief_prompt_is_not_four_section():
    """简记提示词必须与完整篇区分：明确「不需要四个部分」。"""
    brief = _refactor_constants().BRIEF_SYSTEM_PROMPT
    assert "不需要" in brief and "四个部分" in brief

