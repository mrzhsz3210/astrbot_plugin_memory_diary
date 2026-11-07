# -*- coding: utf-8 -*-
"""v0.7.0 收口：统一 MemoryScope、群隔离、单向共享与白名单总闸。"""

from __future__ import annotations

from astrbot.api.provider import ProviderRequest
from harness.fakes import make_event
from harness.world import DIARY_TEXT, World, message
from memory_diary.scope import memory_scope, parse_group_share_map, readable_scopes

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"


def test_scope_normalization_and_one_way_share_parser():
    assert memory_scope("private").value == "private"
    assert memory_scope("group", "888888").value == "group:888888"
    assert memory_scope("group", "bad/id").value == "group:unknown"
    assert memory_scope("group", "").value == "group:unknown"
    assert parse_group_share_map("A=B,C;B=A;broken;C=bad/id") == {
        "A": ("B", "C"),
        "B": ("A",),
    }
    assert readable_scopes("group:A", "A=B") == ("group:A", "group:B")
    assert readable_scopes("group:B", "A=B") == ("group:B",)
    assert readable_scopes("private", "A=B") == ("private",)
    assert readable_scopes("group:unknown", "unknown=A") == ("group:unknown",)


async def test_group_segments_are_isolated_and_legacy_group_never_enters_real_group():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        for group, text in (("A", "A群今天讨论发布"), ("B", "B群明天讨论考试")):
            plugin._save_diary(
                SENDER, text, origin="group", scope=f"group:{group}", mode="full"
            )
        plugin._save_diary(SENDER, "私聊的秘密", origin="private", mode="full")
        plugin._save_diary(SENDER, "旧群原文", origin="", mode="")
        path = plugin._diary_path(SENDER)
        import zipfile
        with zipfile.ZipFile(path) as z:
            entries = {name: z.read(name) for name in z.namelist()}
        name = "diaries/20261023.md"
        content = entries[name].decode() + "\n\n—— 20:00 · 完整 · 写于群聊 ——\n\n旧群无编号原文"
        entries[name] = content.encode()
        entries[plugin._sig_entry(name)] = plugin._entry_sig(SENDER, name, content).encode()
        plugin._write_zip_atomic(path, entries)

        a_text, tampered, _ = plugin._assemble_inject(
            SENDER, in_group=True, memory_scope_value="group:A"
        )
        assert not tampered
        assert "A群今天" in a_text
        assert "B群明天" not in a_text
        assert "私聊的秘密" not in a_text
        assert "旧群无编号原文" not in a_text
        unknown, _, _ = plugin._assemble_inject(
            SENDER, in_group=True, memory_scope_value="group:unknown"
        )
        assert unknown == ""
        headers = [seg["header"] for seg in plugin._segments(content)]
        assert plugin._segment_info(headers[-1])["scope"] == "group:legacy-unknown"


async def test_share_is_read_only_and_does_not_share_memos_or_quota():
    async with World(config={"group_share_map": "A=B", "use_persona": False}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, "B群共享证据", origin="group", scope="group:B", mode="full")
        shared, _, _ = plugin._assemble_inject(
            SENDER, in_group=True, memory_scope_value="group:A"
        )
        reverse, _, _ = plugin._assemble_inject(
            SENDER, in_group=True, memory_scope_value="group:B"
        )
        assert "B群共享证据" in shared
        assert "B群共享证据" in reverse

        plugin._append_memo(SENDER, "A群备忘", scope="group:A")
        plugin._append_memo(SENDER, "B群备忘", scope="group:B")
        assert [m["text"] for m in plugin._load_memos(SENDER, scope="group:A")] == ["A群备忘"]
        assert plugin._clear_memos(SENDER, scopes=("group:A",))
        assert [m["text"] for m in plugin._load_memos(SENDER)] == ["B群备忘"]

        content = "—— 10:00 · 完整 · 写于群聊 · 群:A ——\n\nA\n\n—— 11:00 · 完整 · 写于群聊 · 群:B ——\n\nB"
        assert plugin._full_count_for_scene(content, "group:A") == 1
        assert plugin._full_count_for_scene(content, "group:B") == 1


async def test_whitelist_denies_domain_writes_reads_and_external_owner():
    async with World(config={"allowed_users": SENDER}) as world:
        plugin = world.refactor.plugin
        other = "20002"
        assert plugin._append_memo(other, "不能保存")[1] is False
        assert plugin._load_memos(other) == []
        try:
            plugin._save_diary(other, "不能保存", origin="private")
        except PermissionError:
            pass
        else:
            raise AssertionError("白名单外领域写入必须拒绝")
        assert plugin._assemble_inject(other)[0] == ""
        token = plugin.create_external_token(other, "bridge")
        assert token is None
        plugin.config["allowed_users"] = ""
        token = plugin.create_external_token(other, "bridge")
        assert token
        plugin.config["allowed_users"] = SENDER
        assert plugin.resolve_external_token(token) is None
        assert plugin.external_token_owner(token) == other


async def test_all_injection_paths_use_the_same_current_group_scope():
    for injection_mode in ("temporary", "persist", "manual"):
        async with World(config={
            "use_persona": False,
            "current_self_enabled": False,
            "persist_inject": injection_mode == "persist",
        }) as world:
            inst = world.refactor
            plugin = inst.plugin
            for group, text in (("A", "A群今天讨论发布"), ("B", "B群明天讨论考试")):
                plugin._save_diary(
                    SENDER, text, origin="group", scope=f"group:{group}", mode="full"
                )
            plugin._save_diary(SENDER, "私聊的秘密", origin="private", mode="full")
            plugin._save_diary(SENDER, "无编号旧群原文", origin="group", mode="full")
            if injection_mode == "manual":
                await inst.run_handler(
                    "diary_inject", make_event("/注入日记", group_id="A")
                )
            req = ProviderRequest(
                prompt="继续",
                contexts=[{"role": "user", "content": "旧消息"}],
                system_prompt="SYS",
            )
            await plugin.on_llm_request(make_event("继续", group_id="A"), req)
            injected = req.system_prompt + "\n" + "\n".join(
                getattr(part, "text", "") for part in req.extra_user_content_parts
            )
            assert "A群今天讨论发布" in injected, injection_mode
            assert "B群明天讨论考试" not in injected, injection_mode
            assert "私聊的秘密" not in injected, injection_mode
            assert "无编号旧群原文" not in injected, injection_mode


async def test_group_injection_still_denies_non_whitelist_sender():
    async with World(config={"use_persona": False, "current_self_enabled": False}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(
            SENDER, "A群今天讨论发布", origin="group", scope="group:A", mode="full"
        )
        plugin.config["allowed_users"] = "20002"
        req = ProviderRequest(
            prompt="继续",
            contexts=[{"role": "user", "content": "旧消息"}],
            system_prompt="SYS",
        )
        await plugin.on_llm_request(make_event("继续", group_id="A"), req)
        assert req.system_prompt == "SYS"
        assert not req.extra_user_content_parts


async def test_numbered_group_full_count_auto_dedup_and_manual_notice():
    async with World(config={
        "use_persona": False,
        "current_self_enabled": False,
        "diary_op_interval": 0,
        "max_full_diaries_per_day": 2,
    }) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER, "A群已有完整篇", origin="group", scope="group:A", mode="full"
        )
        assert plugin._has_full_diary_today(SENDER, "20261023") is True
        assert plugin._full_diary_count_today(SENDER, "group:A") == 1
        assert plugin._full_diary_count_today(SENDER, "group:B") == 0
        assert plugin._full_diary_count_today(SENDER, "private") == 0

        plugin._auto_users = {
            SENDER: {"umo": UMO, "name": "阿罗", "updated": ""}
        }
        await plugin._run_auto_diaries("20261023")
        assert inst.provider.calls == []

        inst.conversation_manager.set(
            "aiocqhttp:GroupMessage:A",
            history=[message("user", "A群今天继续讨论发布")],
        )
        inst.provider.replies[:] = [DIARY_TEXT]
        output = await inst.run_handler(
            "write_diary_cmd", make_event("/日记 完整", group_id="A")
        )
        assert "今天这个场景的完整日记已写 2/2 篇" in "\n".join(output)
        assert plugin._full_diary_count_today(SENDER, "group:A") == 2

        inst.conversation_manager.set(
            "aiocqhttp:GroupMessage:A",
            history=[
                message("user", "A群今天继续讨论发布"),
                message("user", "A群又补充了发布时间"),
            ],
        )
        rejected = await inst.run_handler(
            "write_diary_cmd", make_event("/日记 完整", group_id="A")
        )
        assert "上限 2 篇" in "\n".join(rejected)
        assert len(inst.provider.calls) == 1
