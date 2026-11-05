# -*- coding: utf-8 -*-
"""v0.5.5 低危遗留问题修复回归（只驱动 refactor 侧）。

这些行为在 baseline（v0.5.4 原始实现）里不存在或语义不同，所以一律不做差分比较：

1. 日记读路径用 zip 原文验签（行尾空白不再被误判成篡改）；迁移包校验与
   Web 主人解析同口径（“能读不能迁”回归）；
2. 素材哈希死代码清理：新写入不再落 ``last_material_hash``，旧字段会被清掉；
3. 操作冷却在 aware/naive 混存时按“无冷却”兜底，不再抛 TypeError；
4. ``web_list_users`` 的 stat 竞态失败只影响 size 字段，不再 500；
5. 手动 /日记 与哨兵/定时自动写**双向**互斥（手动写期间也占住 ``_writing``），
   锁粒度是 sender_id（同一群不同用户不互相拒绝、同一用户跨场景互斥），
   且占位前会复查（避免门槛 await 期间被插队）；
6. 删掉全角空格死兜底分支后，全角空格分隔的指令参数仍能正常解析。
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from astrbot.api.provider import ProviderRequest
from astrbot.api.web import bind_request_context

from harness.fakes import FakeWebRequest, make_event
from harness.world import DIARY_TEXT, FIXED_NOW, World, message

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"


def _event(text: str, **kwargs):
    kwargs.setdefault("sender_id", SENDER)
    kwargs.setdefault("sender_name", "阿罗")
    return make_event(text, **kwargs)


def _request(prompt: str = "你好"):
    return ProviderRequest(
        prompt=prompt,
        contexts=[{"role": "user", "content": "你好"}],
        system_prompt="SYS",
    )


def _rewrite_zip(path: Path, mutate) -> None:
    with zipfile.ZipFile(path) as z:
        data = {n: z.read(n) for n in z.namelist()}
    data = mutate(data)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, blob in data.items():
            z.writestr(name, blob)


def _resign(data: dict, plugin, entry: str, raw: str) -> dict:
    """把某条目改写成 raw 并按原文重新签名（模拟将来写入路径引入行尾空白）。"""
    data[entry] = raw.encode("utf-8")
    data[plugin._sig_entry(entry)] = plugin._entry_sig(
        SENDER, entry, raw
    ).encode("utf-8")
    return data


# ---------------------------------------------------------------------------
# 1. 读路径原文验签
# ---------------------------------------------------------------------------


async def test_load_entry_verifies_raw_bytes_so_trailing_space_is_not_tamper():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
        )
        path = Path(plugin._diary_path(SENDER))
        entry = "diaries/20261023.md"
        raw = DIARY_TEXT + "\n\n  "  # 模拟将来某个写入路径引入的行尾空白

        def resign(data):
            data[entry] = raw.encode("utf-8")
            data[plugin._sig_entry(entry)] = plugin._entry_sig(
                SENDER, entry, raw
            ).encode("utf-8")
            return data

        _rewrite_zip(path, resign)
        content, tampered = plugin._load_entry(SENDER, entry)
        assert tampered is False, "带行尾空白的合法日记被误判成篡改（验签口径不一致）"
        assert content == raw.strip(), "展示内容应 strip，但验签必须用原文"

        def tamper(data):
            data[entry] = (raw + "被改过").encode("utf-8")
            return data

        _rewrite_zip(path, tamper)
        _, tampered_after = plugin._load_entry(SENDER, entry)
        assert tampered_after is True, "原文内容被改而签名未更新，仍必须判为篡改"


# ---------------------------------------------------------------------------
# 2. 素材哈希死代码清理
# ---------------------------------------------------------------------------


async def test_stale_material_hash_field_is_cleaned_and_not_written_again():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin

        # 旧版本留下的字段：任意一次偏好写回都应该把它清掉
        plugin._save_pref_data(SENDER, {"last_material_hash": "deadbeef"})
        assert "last_material_hash" not in plugin._load_pref_data(SENDER)

        # 写一篇日记（会写素材游标 -> 走偏好文件整体重写）
        inst.set_history(
            [message("user", "今天聊了很多"), message("assistant", "是啊")]
        )
        diary, err = await plugin._write_diary(
            UMO, SENDER, "阿罗", requested_mode="brief"
        )
        assert err is None and diary
        pref = plugin._load_pref_data(SENDER)
        assert "last_material_hash" not in pref
        assert "material_cursors" in pref


# ---------------------------------------------------------------------------
# 3. 操作冷却 aware/naive 混存
# ---------------------------------------------------------------------------


async def test_op_cooldown_tolerates_naive_and_aware_mixed():
    async with World(
        config={"use_persona": False, "diary_op_interval": 5}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        # 同一实例内 diary_timezone 从“未配置”改成“有时区”后，_op_last 可能残留 naive
        plugin._op_last[SENDER] = datetime(2026, 10, 23, 20, 28, 0)
        inst.set_now(datetime(2026, 10, 23, 20, 30, 0, tzinfo=timezone.utc))
        assert plugin._op_cooldown_remaining(SENDER) == 0

        # 类型一致时冷却语义不变
        plugin._op_last[SENDER] = datetime(
            2026, 10, 23, 20, 28, 0, tzinfo=timezone.utc
        )
        assert plugin._op_cooldown_remaining(SENDER) == 3


# ---------------------------------------------------------------------------
# 4. web_list_users 的 stat 竞态
# ---------------------------------------------------------------------------


async def test_web_list_users_survives_stat_race():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
        )
        target = Path(plugin._diary_path(SENDER))
        real_stat = Path.stat

        def flaky_stat(self, *args, **kwargs):
            if self == target:
                raise FileNotFoundError(str(self))  # glob 与 stat 之间被并发删掉
            return real_stat(self, *args, **kwargs)

        with bind_request_context(FakeWebRequest()):
            with mock.patch.object(Path, "stat", flaky_stat):
                resp = await plugin.web_list_users()
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert len(body["users"]) == 1
        assert body["users"][0]["size"] == 0
        assert body["users"][0]["day_count"] == 1


# ---------------------------------------------------------------------------
# 5. 手动写 / 自动写 双向互斥
# ---------------------------------------------------------------------------


async def test_manual_write_is_mutually_exclusive_with_auto_write_both_ways():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history(
            [message("user", "今天聊了很多"), message("assistant", "是啊")]
        )

        # 方向一：同一用户已有写日记流程 -> 手动入口婉拒，且不调用 LLM
        plugin._writing.add(SENDER)
        try:
            refused = await inst.run_handler("write_diary_cmd", _event("/日记"))
        finally:
            plugin._writing.discard(SENDER)
        assert any("正在写日记" in text for text in refused)
        assert inst.provider.calls == []

        # 方向二：手动写进行中必须已占住 _writing（否则 LLM 生成期间自动写会启动）
        seen = {}
        real_write = plugin._write_diary

        async def spy(*args, **kwargs):
            seen["locked"] = SENDER in plugin._writing
            return await real_write(*args, **kwargs)

        plugin._write_diary = spy
        try:
            await inst.run_handler("write_diary_cmd", _event("/日记 完整"))
        finally:
            plugin._write_diary = real_write
        assert seen.get("locked") is True, "手动写期间没有占住 _writing，自动写仍会并发"
        assert SENDER not in plugin._writing, "手动写结束后必须释放占位"
        assert inst.entries(SENDER) == ["diaries/20261023.md"]


# ---------------------------------------------------------------------------
# 5b. 锁粒度是 sender_id（不是会话 umo）
# ---------------------------------------------------------------------------


async def test_manual_write_lock_is_per_sender_not_per_conversation():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        # 同一群里 10001 正在写：10002 写的是另一个 diary_<key>.zip，不应被拒
        plugin._writing.add(SENDER)
        try:
            other = await inst.run_handler(
                "write_diary_cmd",
                _event("/日记", sender_id="10002", group_id="88888"),
            )
        finally:
            plugin._writing.discard(SENDER)
        assert not any("正在写日记" in text for text in other), "群友被别人的锁误拒"
        assert inst.entries("10002") == ["diaries/20261023.md"]


async def test_manual_write_lock_covers_same_sender_across_scenes():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        # 同一用户私聊/群聊写同一个 diary_<sender_id>.zip -> 必须互斥
        plugin._writing.add(SENDER)
        try:
            refused = await inst.run_handler(
                "write_diary_cmd", _event("/日记", group_id="88888")
            )
        finally:
            plugin._writing.discard(SENDER)
        assert any("正在写日记" in text for text in refused)
        assert inst.provider.calls == []


async def test_manual_write_rechecks_lock_after_threshold_await():
    async with World(
        config={
            "use_persona": False,
            "write_min_ratio": 0.5,
            "context_token_limit": 60000,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "今天聊了很多")])

        async def racing_context_used(umo):
            # 模拟“门槛检查的 await 期间另一个写者拿到锁”
            plugin._writing.add(SENDER)
            return 59000  # ratio ≈ 98%，通过门槛

        plugin._context_used = racing_context_used
        try:
            out = await inst.run_handler("write_diary_cmd", _event("/日记"))
        finally:
            plugin._writing.discard(SENDER)
        assert any("正在写日记" in text for text in out), "check→add 之间被插队后仍写盘"
        assert inst.provider.calls == []


# ---------------------------------------------------------------------------
# 5c. 验签口径：迁移包校验 / Web 主人解析也要用原文
# ---------------------------------------------------------------------------


async def test_export_import_roundtrip_with_trailing_whitespace_entry():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
        )
        path = Path(plugin._diary_path(SENDER))
        entry = "diaries/20261023.md"
        raw = DIARY_TEXT + "\n\n  "
        _rewrite_zip(path, lambda data: _resign(data, plugin, entry, raw))

        bundle = plugin._build_export_bundle(
            SENDER, path, plugin._list_entries(SENDER)
        )
        try:
            # 修前：_verify_bundle 先 strip 再验签 -> 这里抛“迁移包可能被改动过”
            # v0.6：返回 5 元组 (owner, key, entries, images, annotations)，
            # 这篇日记没有插图、也没有批注侧车 → images={} / annotations=None
            owner, key, entries, images, annotations = plugin._verify_bundle(bundle)
            assert owner == SENDER
            assert entries[entry] == raw
            assert images == {}, f"没有插图的迁移包 images 应为空，实际 {images!r}"
            assert annotations is None, (
                f"没有批注侧车的迁移包 annotations 应为 None，实际 {annotations!r}"
            )
            path.unlink()  # 导入要求本机还没有该用户的日记
            plugin._install_bundle(SENDER, owner, key, entries)
        finally:
            bundle.unlink(missing_ok=True)
        content, tampered = plugin._load_entry(SENDER, entry)
        assert tampered is False
        assert content == raw.strip()


async def test_resolve_zip_owner_accepts_raw_signed_entries():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
        )
        path = Path(plugin._diary_path(SENDER))
        entry = "diaries/20261023.md"
        raw = DIARY_TEXT + "\n\n  "

        def rewrite(data):
            data = _resign(data, plugin, entry, raw)
            data.pop(plugin.README_ZIP_ENTRY, None)  # 迫使走 qq_hint 全量验签
            return data

        _rewrite_zip(path, rewrite)
        owner, err = plugin._resolve_zip_owner(
            path, plugin._diary_file_key(SENDER), qq_hint=SENDER
        )
        assert (owner, err) == (SENDER, "")


# ---------------------------------------------------------------------------
# 6. 全角空格分隔的参数（死兜底分支删除后的行为回归）
# ---------------------------------------------------------------------------


async def test_fullwidth_space_argument_still_parsed():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        event = _event("/日记范围　最近7天")  # 指令与参数之间是全角空格
        assert plugin._command_arg(event) == "最近7天"
        out = await inst.run_handler("diary_scope", event)
        assert plugin._load_pref(SENDER) == "days:7"
        assert any("最近 7 天" in text for text in out)


# ---------------------------------------------------------------------------
# 7. 哨兵 / 每天定时的自动写也按 sender_id 加锁（键回退就会并发丢篇）
# ---------------------------------------------------------------------------


async def test_watch_auto_write_respects_sender_lock():
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
        plugin._writing.add(SENDER)  # 该用户已有写日记流程
        event = _event("你好")
        try:
            await plugin.on_llm_request(event, _request(prompt="x" * 99000))
        finally:
            plugin._writing.discard(SENDER)
        # 锁被占时必须直接跳过：不调 LLM、不写日记、不发提示
        assert inst.provider.calls == []
        assert inst.entries(SENDER) == []
        assert event.result_texts() == []


async def test_run_auto_diaries_respects_sender_lock():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin.config["enable_auto_diary"] = True
        plugin.config["auto_diary_time"] = "22:00"
        inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
        umo = "aiocqhttp:FriendMessage:10003"
        inst.conversation_manager.set(
            umo, history=[message("user", "有素材")], cid="cid-3"
        )
        plugin._auto_users = {"10003": {"umo": umo, "name": "阿三"}}
        plugin._writing.add("10003")  # 该用户已有写日记流程
        try:
            await plugin._run_auto_diaries("20261023")
        finally:
            plugin._writing.discard("10003")
        assert inst.provider.calls == []
        assert inst.entries("10003") == []
