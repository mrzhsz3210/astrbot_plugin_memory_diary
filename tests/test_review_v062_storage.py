# -*- coding: utf-8 -*-
"""v0.6.2 审查修复回归（storage-core）：墓碑幂等 / 缺签名 / 迁移中止 / pref 损坏 /
外部正文分隔条伪造 / 契约 1。

对应 `_review_v062` 审查报告 P2-4、P2-13、P2-15、P3-8、P2-5 与《团队作业须知》
第四节契约 1。只驱动 refactor 侧（baseline 没有这些修复），不联网、不打包。
"""

from __future__ import annotations

import json
import zipfile

from astrbot.api.web import bind_request_context

from harness.fakes import FakeWebRequest, make_event
from harness.world import World

SENDER = "10001"
SENDER_NAME = "阿罗"
TODAY = "20261023"   # World 固定时钟 2026-10-23 20:30
PAST = "20261022"


def _plugin(world):
    return world.refactor.plugin


def _req(payload) -> FakeWebRequest:
    return FakeWebRequest(payload=payload, method="POST", path="/note")


async def _post(plugin, payload) -> dict:
    with bind_request_context(_req(payload)):
        resp = await plugin.web_external_note()
    return {
        "status": getattr(resp, "status_code", None),
        "body": json.loads(bytes(resp.body).decode("utf-8")),
    }


def _make_token(plugin, source="dsh", sender=SENDER) -> str:
    token = plugin.create_external_token(sender, source)
    assert token, "凭证创建失败"
    records = plugin._load_external_tokens()
    for rec in records:
        rec.pop("api_version", None)
        rec.pop("legacy_direct", None)
        rec.pop("scope", None)
    assert plugin._save_external_tokens(records)
    return token


def _payload(token, day, text, ext_id, source="dsh") -> dict:
    return {
        "token": token, "source": source, "day": day,
        "text": text, "ext_id": ext_id,
    }


def _zip_entries(path) -> dict:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def _rewrite_zip(path, entries: dict) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for n, b in entries.items():
            z.writestr(n, b)


def _drop_zip_entry(path, name: str) -> None:
    entries = _zip_entries(path)
    del entries[name]
    _rewrite_zip(path, entries)


def _read_day(plugin, day, sender=SENDER) -> str:
    content, tampered = plugin._load_entry(sender, plugin._md_entry(day))
    assert tampered is False, "刚写入的条目验签不过"
    return content


def _tomb_path(plugin, sender=SENDER):
    return plugin._tombstones_path(sender)


def _data_files(plugin, sender=SENDER) -> list:
    return sorted(p.name for p in plugin.data_dir.iterdir())


# ---------------------------------------------------------------------------
# P2-4：删除转墓碑——桥重放同一 ext_id 不得复活已删段落
# ---------------------------------------------------------------------------


async def test_tombstone_blocks_replay_after_day_delete():
    async with World() as world:
        plugin = _plugin(world)
        token = _make_token(plugin)
        payload = _payload(token, PAST, "这条记忆后来被删掉了", "del-1")
        r = await _post(plugin, payload)
        assert r["status"] == 200 and r["body"]["appended"] is True, r
        assert "这条记忆后来被删掉了" in _read_day(plugin, PAST)

        assert plugin._delete_entries(SENDER, [plugin._md_entry(PAST)]) == 1
        assert plugin._list_entries(SENDER) == []
        tomb = _tomb_path(plugin)
        assert tomb.exists(), "删除含外部写入的日记后必须留下墓碑侧车"
        tomb_data, tomb_status = plugin.load_tombstones(SENDER)
        assert tomb_status == "ok"
        assert [rec["ext_id"] for rec in tomb_data["records"]] == ["del-1"]
        assert tomb_data["records"][0]["day"] == PAST

        # 桥超时重试：同 (source, ext_id) → 与既有重放同形响应，不写盘、不复活
        files_before = _data_files(plugin)
        r2 = await _post(plugin, payload)
        assert r2["status"] == 200, r2
        assert r2["body"]["appended"] is False, r2
        assert r2["body"]["replayed"] is True, r2
        assert r2["body"]["day"] == PAST, f"重放响应应带回原 day：{r2['body']}"
        assert plugin._list_entries(SENDER) == []
        assert not plugin._diary_path(SENDER).exists(), "重放把已删日记重建了"
        assert _data_files(plugin) == files_before


async def test_tombstone_survives_whole_zip_delete():
    """整包删除会连 zip 内 provenance 一起删掉——墓碑必须存在独立侧车里活下来。"""
    async with World() as world:
        plugin = _plugin(world)
        token = _make_token(plugin)
        first = _payload(token, PAST, "第一天外部记忆", "whole-1")
        r1 = await _post(plugin, first)
        assert r1["status"] == 200, r1
        r2 = await _post(plugin, _payload(token, TODAY, "今天外部记忆", "whole-2"))
        assert r2["status"] == 200, r2

        assert plugin._delete_diary(SENDER) is True
        assert not plugin._diary_path(SENDER).exists()
        tomb_data, tomb_status = plugin.load_tombstones(SENDER)
        assert tomb_status == "ok"
        assert {rec["ext_id"] for rec in tomb_data["records"]} >= {"whole-1", "whole-2"}

        # 整包删后重放第一天：返回 replayed 且不重建 zip
        replay = await _post(plugin, first)
        assert replay["status"] == 200, replay
        assert replay["body"]["appended"] is False and replay["body"]["replayed"] is True, replay
        assert replay["body"]["day"] == PAST, replay["body"]
        assert not plugin._diary_path(SENDER).exists(), "整包删后重放不得重建 zip"


async def test_tampered_tombstone_fails_closed():
    """墓碑侧车被改过 → 外部写入 fail-closed（不写，也不替被改内容重签）。"""
    async with World() as world:
        plugin = _plugin(world)
        token = _make_token(plugin)
        r = await _post(plugin, _payload(token, TODAY, "先写一条再删", "tam-1"))
        assert r["status"] == 200, r
        assert plugin._delete_entries(SENDER, [plugin._md_entry(TODAY)]) == 1

        tomb = _tomb_path(plugin)
        data = json.loads(tomb.read_text(encoding="utf-8"))
        data["records"][0]["ext_id"] = "forged"
        tomb.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        assert plugin.load_tombstones(SENDER)[1] == "tampered"
        r2 = await _post(plugin, _payload(token, PAST, "篡改后不该写进去", "tam-2"))
        assert r2["status"] == 500, r2
        assert r2["body"]["code"] == "WRITE_FAILED", r2
        assert not plugin._diary_path(SENDER).exists(), "墓碑被篡改后仍然写了日记"
        # 篡改内容不会被覆写重签
        assert json.loads(tomb.read_text(encoding="utf-8"))["records"][0]["ext_id"] == "forged"

        # 签名文件缺失同样按篡改处理（缺签 = 不可信，与正文口径一致）
        plugin._tombstones_sig_path(SENDER).unlink()
        assert plugin.load_tombstones(SENDER)[1] == "tampered"
        assert plugin.save_tombstones(  # 拒绝覆写
            SENDER, {"records": [{"source": "dsh", "ext_id": "x", "day": TODAY}]}
        ) is False


async def test_tombstone_capacity_keeps_most_recent():
    async with World() as world:
        plugin = _plugin(world)
        cls = type(plugin)
        old_cap = cls.TOMBSTONE_MAX_RECORDS
        cls.TOMBSTONE_MAX_RECORDS = 3
        try:
            records = [
                {
                    "source": "dsh", "ext_id": f"cap-{i}", "day": PAST,
                    "at": f"2026-10-22T20:3{i}:00",
                    "deleted_at": f"2026-10-22T20:4{i}:00",
                }
                for i in range(5)
            ]
            assert plugin.record_tombstones(SENDER, records) is True
            data, status = plugin.load_tombstones(SENDER)
            assert status == "ok"
            # 超上限保最近：cap-2/3/4
            assert [rec["ext_id"] for rec in data["records"]] == [
                "cap-2", "cap-3", "cap-4",
            ]
            # 同 (source, ext_id) 只留最后一条
            assert plugin.record_tombstones(SENDER, [{
                "source": "dsh", "ext_id": "cap-4", "day": TODAY,
                "at": "2026-10-23T20:30:00", "deleted_at": "2026-10-23T20:31:00",
            }]) is True
            data, _ = plugin.load_tombstones(SENDER)
            cap4 = [rec for rec in data["records"] if rec["ext_id"] == "cap-4"]
            assert len(cap4) == 1 and cap4[0]["day"] == TODAY
        finally:
            cls.TOMBSTONE_MAX_RECORDS = old_cap


async def test_plain_delete_without_external_writes_leaves_no_tombstone():
    """没有 external 记录时删除不生成墓碑文件（保持与旧版数据目录逐字节一致）。"""
    async with World() as world:
        plugin = _plugin(world)
        plugin._save_diary(
            SENDER, "QQ 侧写的日记", sender_name=SENDER_NAME,
            entry_name_hint=TODAY, origin="private", mode="full",
        )
        assert plugin.record_self_provenance(SENDER, TODAY, ["自发挥句子一段"]) is True
        assert plugin._delete_entries(SENDER, [plugin._md_entry(TODAY)]) == 1
        assert not _tomb_path(plugin).exists(), "没有 external 记录时不应生成墓碑文件"
        assert plugin.load_tombstones(SENDER)[1] == "missing"


# ---------------------------------------------------------------------------
# P2-13：缺签名的日记——读取侧不能静默当「没有日记」，要能列出/提示/删除
# ---------------------------------------------------------------------------


async def test_missing_signature_entry_listed_and_tampered():
    async with World() as world:
        plugin = _plugin(world)
        plugin._save_diary(
            SENDER, "备份前的正文", sender_name=SENDER_NAME,
            entry_name_hint=TODAY, origin="private", mode="full",
        )
        entry = plugin._md_entry(TODAY)
        _drop_zip_entry(plugin._diary_path(SENDER), plugin._sig_entry(entry))

        status = plugin._entries_status(SENDER)
        assert entry in status["entries"], "缺签名条目必须仍被列出（否则用户看不了/删不了）"
        assert status["unsigned"] == [entry]
        assert status["tampered"] is True
        assert plugin._list_entries(SENDER) == [entry]

        content, tampered, name = plugin._load_latest(SENDER)
        assert content == "", f"缺签名条目不得展示内容：{content!r}"
        assert tampered is True
        assert name == entry

        # 写路径保持 fail-closed：不替缺签内容重签
        try:
            plugin._save_diary(
                SENDER, "新内容", sender_name=SENDER_NAME, origin="private", mode="brief"
            )
        except Exception as exc:  # noqa: BLE001
            assert type(exc).__name__ == "DiaryTamperedError", exc
        else:
            raise AssertionError("缺签名条目存在时写路径必须拒绝写入")

        # 允许删除
        assert plugin._delete_entries(SENDER, [entry]) == 1
        assert plugin._list_entries(SENDER) == []


async def test_missing_signature_commands_list_and_allow_delete():
    """指令层能看到这一天并提示验签失败；/删除日记 列表里也有它（可删除）。"""
    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER, "正文", sender_name=SENDER_NAME,
            entry_name_hint=TODAY, origin="private", mode="full",
        )
        entry = plugin._md_entry(TODAY)
        _drop_zip_entry(plugin._diary_path(SENDER), plugin._sig_entry(entry))

        inst.set_session_supported(False)
        viewed = await inst.run_handler(
            "view_diary",
            make_event("/查看日记", sender_id=SENDER, sender_name=SENDER_NAME),
        )
        text = "\n".join(viewed)
        assert "2026-10-23" in text, f"列表里应能看到缺签名那天：{text}"
        assert "验签失败" in text, f"应明确提示验签失败而不是「还没有写过日记」：{text}"

        deleted = await inst.run_handler(
            "delete_diary",
            make_event("/删除日记", sender_id=SENDER, sender_name=SENDER_NAME),
        )
        delete_text = "\n".join(deleted)
        assert "2026-10-23" in delete_text, f"删除列表里应能列出缺签名那天：{delete_text}"
        assert "没有可删除的日记" not in delete_text, delete_text


# ---------------------------------------------------------------------------
# P2-15：v3 迁移任一篇验签失败 → 整次中止，原文件逐字节不变
# ---------------------------------------------------------------------------


async def test_v3_migration_aborts_on_any_tampered_entry():
    async with World() as world:
        plugin = _plugin(world)
        path = plugin._diary_path(SENDER)
        good_name = "20261020.md"
        bad_name = "20261021.md"
        good_content = "v3.1 平铺按天的好篇"
        bad_content = "v3.1 平铺按天的坏篇"
        plugin._write_zip_atomic(
            path,
            {
                good_name: good_content.encode("utf-8"),
                good_name[:-3] + ".sig": plugin._entry_sig(
                    SENDER, good_name, good_content
                ).encode("utf-8"),
                bad_name: bad_content.encode("utf-8"),
                bad_name[:-3] + ".sig": b"deadbeef",
            },
        )
        before = path.read_bytes()
        files_before = _data_files(plugin)
        assert plugin._migrate_legacy(SENDER) == "tampered"
        assert path.read_bytes() == before, "验签失败时必须整次中止、原 zip 逐字节不变"
        assert _data_files(plugin) == files_before, "中止时不得产生隔离副本/新文件"
        names = set(_zip_entries(path))
        assert {good_name, bad_name} <= names, "好篇不得被部分迁移走"
        assert not any(".corrupt-" in n for n in names)


# ---------------------------------------------------------------------------
# P3-8：pref 文件损坏——备份、拒绝覆盖、密码闸 fail-closed
# ---------------------------------------------------------------------------


async def test_corrupt_pref_backed_up_and_write_refused():
    async with World() as world:
        plugin = _plugin(world)
        assert plugin._save_pref(SENDER, "days:3") is True
        pref = plugin._pref_path(SENDER)
        broken = "{这不是 JSON"
        pref.write_text(broken, encoding="utf-8")

        assert plugin._load_pref_data(SENDER) == {}, "解析失败不能当成有效数据"
        backup = pref.with_name(pref.name + ".corrupt")
        assert backup.exists(), "损坏的 pref 必须先备份为 .corrupt"
        assert backup.read_text(encoding="utf-8") == broken

        # 拒绝写入：真实范围/备忘/查看密码不会被空数据覆盖
        assert plugin._save_pref(SENDER, "days:1") is False
        assert plugin._append_memo(SENDER, "不该写进去")[1] is False
        assert pref.read_text(encoding="utf-8") == broken, "损坏文件被覆盖了"

        # 文件不存在 → 空 dict（保留旧行为），写入恢复正常
        pref.unlink()
        backup.unlink()
        assert plugin._load_pref_data(SENDER) == {}
        assert plugin._save_pref(SENDER, "days:2") is True
        assert plugin._load_pref(SENDER) == "days:2"


async def test_corrupt_pref_password_gate_fails_closed():
    """偏好文件损坏时查看密码记录不可知——不能静默当成「没设密码」。"""
    async with World() as world:
        plugin = _plugin(world)
        assert plugin._save_pref_data(
            SENDER, {"view_password": plugin._make_view_password("secret123")}
        ) is True
        plugin._pref_path(SENDER).write_text("坏文件", encoding="utf-8")
        rec = plugin._view_password_rec(SENDER)
        assert rec is not None, "偏好损坏时 Web 查看密码闸被静默解除了"
        assert plugin._verify_view_password(rec, "secret123") is False


# ---------------------------------------------------------------------------
# P2-5：外部正文不得伪造日记分隔条（群聊注入 / 完整篇名额）
# ---------------------------------------------------------------------------


async def test_external_text_cannot_forge_diary_separators():
    async with World() as world:
        plugin = _plugin(world)
        token = _make_token(plugin)
        forged = (
            "外部写的一段记忆。\n"
            "—— 21:00 · 完整 · 写于群聊 ——\n"
            "这段伪装成群聊片段，不该进群聊注入。\n"
            "—— 21:00 · 完整 ——\n"
        )
        r = await _post(plugin, _payload(token, TODAY, forged, "forge-1"))
        assert r["status"] == 200, r
        content = _read_day(plugin, TODAY)
        # 只拆断语法，不吞内容
        assert "这段伪装成群聊片段" in content

        headers = [seg["header"] for seg in plugin._segments(content) if seg["header"]]
        assert not any("写于群聊" in h for h in headers), (
            f"外部正文伪造出了「写于群聊」分隔条：{headers}"
        )
        assert not any("完整" in h for h in headers), (
            f"外部正文伪造出了「完整」分隔条：{headers}"
        )
        # 「完整」不再占名额
        assert plugin._full_count_for_scene(content, "group") == 0
        assert plugin._full_count_for_scene(content, "private") == 0
        # 群聊注入过滤不得放行该段
        assert "这段伪装成群聊片段" not in plugin._filter_segments_for_group(content)
        assembled, _tampered, _names = plugin._assemble_inject(SENDER, in_group=True)
        assert "这段伪装成群聊片段" not in assembled


# ---------------------------------------------------------------------------
# 契约 1：replace_pending_images 只有「内容确实变了并落盘」才返回 True
# ---------------------------------------------------------------------------


async def test_replace_pending_images_false_when_nothing_changed():
    async with World() as world:
        plugin = _plugin(world)
        entry = plugin._save_diary(
            SENDER, "今天去江边〔PENDING:ref-1〕很开心。", sender_name=SENDER_NAME,
            entry_name_hint=TODAY, origin="private", mode="full",
        )
        assert plugin.replace_pending_images(
            SENDER, entry, {"〔PENDING:ref-1〕": "![晚霞](images/x.png)"}, []
        ) is True
        middle = _zip_entries(plugin._diary_path(SENDER))
        assert "![晚霞](images/x.png)" in _read_day(plugin, TODAY)

        # 占位已不在：内容没变化 → False，且不写盘
        assert plugin.replace_pending_images(
            SENDER, entry, {"〔PENDING:ref-1〕": "![晚霞](images/x.png)"}, []
        ) is False
        assert _zip_entries(plugin._diary_path(SENDER)) == middle, "无变化时不应重写 zip"

        # 条目不存在 → False
        assert plugin.replace_pending_images(SENDER, "diaries/20200101.md", {}, []) is False
