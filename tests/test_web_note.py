# -*- coding: utf-8 -*-
"""外部写入 API（v0.6.2，issue #3）测试：POST /astrbot_plugin_memory_diary/note。

只跑 refactor 侧（baseline 没有此功能）。覆盖方案 B4 的四道安全闸：
鉴权 / 幂等 / 只追加 / fail-closed，外加限流与凭证管理指令。
"""

from __future__ import annotations

import asyncio
import json
import zipfile

from astrbot.api.web import bind_request_context

from harness.fakes import FakeWebRequest, make_event
from harness.world import World

SENDER = "10001"
SENDER_NAME = "阿罗"

# World 的固定时钟是 2026-10-23 20:30（tests/harness/world.py FIXED_NOW）
TODAY = "20261023"
PAST = "20261022"
FUTURE = "20261024"


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
    return token


def _zip_path(plugin, sender=SENDER):
    return plugin._diary_path(sender)


def _zip_bytes(path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def _read_day(plugin, day, sender=SENDER) -> str:
    content, tampered = plugin._load_entry(sender, plugin._md_entry(day))
    assert not tampered, "新写入的条目验签不过"
    return content


# ------------------------- 闸一：鉴权 -------------------------


async def test_note_missing_or_bad_token_rejected():
    async with World() as world:
        plugin = world.refactor.plugin
        good = _make_token(plugin)
        for payload in (
            {},
            {"token": "", "source": "dsh", "day": TODAY, "text": "x", "ext_id": "a"},
            {"token": "mdw_forged", "source": "dsh", "day": TODAY, "text": "x",
             "ext_id": "a"},
        ):
            r = await _post(plugin, payload)
            assert r["status"] == 401, r
            assert r["body"]["code"] == "UNAUTHORIZED"
        # 一枚都没写入
        assert not _zip_path(plugin).exists()
        assert good  # token 本身有效（下个用例验证）


async def test_note_source_whitelist_enforced():
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin, source="dsh")
        r = await _post(plugin, {
            "token": token, "source": "evil", "day": TODAY,
            "text": "x", "ext_id": "a1",
        })
        assert r["status"] == 403 and r["body"]["code"] == "SOURCE_NOT_ALLOWED", r
        assert not _zip_path(plugin).exists()


async def test_note_unconfigured_owner_defaults_deny():
    """未配置任何凭证时默认拒绝（fail-closed）。"""
    async with World() as world:
        plugin = world.refactor.plugin
        r = await _post(plugin, {
            "token": "mdw_whatever", "source": "dsh", "day": TODAY,
            "text": "x", "ext_id": "a1",
        })
        assert r["status"] == 401, r
        assert not _zip_path(plugin).exists()


async def test_note_revoked_token_rejected():
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        assert plugin.revoke_external_token(SENDER, 1)
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "x", "ext_id": "a1",
        })
        assert r["status"] == 401, r


# ------------------------- 字段校验 -------------------------


async def test_note_bad_day_rejected():
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        for bad in ("20261301", "20261132", "not-a-day", "2026-13-01",
                    "../etc", "2026-10-23/../../x", ""):
            r = await _post(plugin, {
                "token": token, "source": "dsh", "day": bad,
                "text": "x", "ext_id": "a1",
            })
            assert r["status"] == 400, (bad, r)
        assert not _zip_path(plugin).exists()


async def test_note_future_day_rejected():
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": FUTURE,
            "text": "x", "ext_id": "a1",
        })
        assert r["status"] == 400, r
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "今天可以", "ext_id": "a2",
        })
        assert r["status"] == 200 and r["body"]["appended"] is True, r


async def test_note_text_limits():
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        # 空文本
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "   ", "ext_id": "a1",
        })
        assert r["status"] == 400, r
        # 超长（8 KB 字节上限）
        big = "汉" * 3000  # 9000 字节 UTF-8
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": big, "ext_id": "a2",
        })
        assert r["status"] == 413 and r["body"]["code"] == "PAYLOAD_TOO_LARGE", r
        assert not _zip_path(plugin).exists()


async def test_note_ext_id_rules():
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        for bad in (None, "", "has space", "含中文", "x" * 65, 123):
            r = await _post(plugin, {
                "token": token, "source": "dsh", "day": TODAY,
                "text": "x", "ext_id": bad,
            })
            assert r["status"] == 400, (bad, r)
        assert not _zip_path(plugin).exists()


async def test_note_non_dict_payload_rejected():
    async with World() as world:
        plugin = world.refactor.plugin
        with bind_request_context(_req(["not", "a", "dict"])):
            resp = await plugin.web_external_note()
        assert getattr(resp, "status_code", None) == 400


# ------------------------- 闸二：幂等 -------------------------


async def test_note_idempotent_replay():
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        payload = {
            "token": token, "source": "dsh", "day": PAST,
            "text": "第一段", "ext_id": "unique-1",
        }
        r1 = await _post(plugin, payload)
        assert r1["status"] == 200 and r1["body"]["appended"] is True, r1
        before = _zip_bytes(_zip_path(plugin))
        # 客户端重试：同 ext_id → 重放，zip 一个字节都不动
        r2 = await _post(plugin, payload)
        assert r2["status"] == 200, r2
        assert r2["body"]["appended"] is False and r2["body"]["replayed"] is True
        assert r2["body"]["day"] == PAST
        assert _zip_bytes(_zip_path(plugin)) == before
        # 内容确实只有一段
        assert _read_day(plugin, PAST).count("第一段") == 1


async def test_note_failed_write_leaves_no_idempotency_record():
    """中断重试：写入中途失败 + 客户端重试 → 不产生重复条目，也不虚假重放。"""
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        payload = {
            "token": token, "source": "dsh", "day": PAST,
            "text": "落盘前崩了", "ext_id": "crash-1",
        }
        original = plugin._write_zip_atomic

        def boom(*args, **kwargs):
            raise OSError("模拟落盘崩溃")

        plugin._write_zip_atomic = boom
        try:
            r = await _post(plugin, payload)
            assert r["status"] == 500 and r["body"]["code"] == "WRITE_FAILED", r
        finally:
            plugin._write_zip_atomic = original
        assert not _zip_path(plugin).exists()
        # 重试同 ext_id：不是重放（第一次根本没写成），正常追加
        r2 = await _post(plugin, payload)
        assert r2["status"] == 200 and r2["body"]["appended"] is True, r2
        assert _read_day(plugin, PAST).count("落盘前崩了") == 1


# ------------------------- 闸三：只追加与并发 -------------------------


async def test_note_appends_without_overwriting():
    async with World() as world:
        inst = world.refactor
        plugin = inst.plugin
        token = _make_token(plugin)
        # QQ 侧先有一篇日记
        plugin._save_diary(SENDER, "QQ 侧原文", sender_name=SENDER_NAME,
                           origin="private", mode="full")
        before = _read_day(plugin, TODAY)
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "外部补的一段", "ext_id": "app-1",
        })
        assert r["status"] == 200 and r["body"]["appended"] is True, r
        after = _read_day(plugin, TODAY)
        assert after.startswith(before), "外部写入覆盖了既有内容"
        assert "外部补的一段" in after
        assert "（外部·dsh）" in after, "分隔条缺来源标记"
        # 昵称说明不被外部写入冲掉
        _, nick = plugin._zip_owner_info(_zip_path(plugin))
        assert nick == SENDER_NAME


async def test_note_first_write_of_day_has_header():
    """外部写入是当天首篇时也有分隔条（不丢来源标记）。"""
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": PAST,
            "text": "首篇外部段落", "ext_id": "first-1",
        })
        assert r["status"] == 200, r
        content = _read_day(plugin, PAST)
        assert "——" in content and "（外部·dsh）" in content, content
        assert "首篇外部段落" in content


async def test_note_concurrent_writes_both_succeed():
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        results = await asyncio.gather(*[
            _post(plugin, {
                "token": token, "source": "dsh", "day": TODAY,
                "text": f"并发段落{i}", "ext_id": f"cc-{i}",
            })
            for i in range(3)
        ])
        assert all(r["status"] == 200 for r in results), results
        content = _read_day(plugin, TODAY)
        for i in range(3):
            assert f"并发段落{i}" in content
        with zipfile.ZipFile(_zip_path(plugin)) as z:
            assert z.testzip() is None, "并发写入后 zip CRC 校验失败"


# ------------------------- 闸四：fail-closed -------------------------


async def test_note_tampered_provenance_refuses_write():
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": PAST,
            "text": "先写一条", "ext_id": "t-1",
        })
        assert r["status"] == 200, r
        # 篡改 provenance.json（内容改掉、签名不动）
        path = _zip_path(plugin)
        with zipfile.ZipFile(path) as z:
            data = {n: z.read(n) for n in z.namelist()}
        prov = json.loads(data["provenance.json"].decode("utf-8"))
        prov["entries"][f"{PAST}.md"]["external"][0]["ext_id"] = "forged"
        data["provenance.json"] = json.dumps(
            prov, ensure_ascii=False, indent=2
        ).encode("utf-8")
        with zipfile.ZipFile(path, "w") as z:
            for n, b in data.items():
                z.writestr(n, b)
        before = _zip_bytes(path)
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "不该写进去", "ext_id": "t-2",
        })
        assert r["status"] == 500, r
        assert _zip_bytes(path) == before, "篡改后仍然写了"


# ------------------------- 限流 -------------------------


async def test_note_rate_limit():
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        for i in range(plugin._EXT_RATE_LIMIT):
            r = await _post(plugin, {
                "token": token, "source": "dsh", "day": TODAY,
                "text": f"第{i}条", "ext_id": f"rl-{i}",
            })
            assert r["status"] == 200, (i, r)
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "超限", "ext_id": "rl-over",
        })
        assert r["status"] == 429 and r["body"]["code"] == "RATE_LIMITED", r


# ------------------------- 凭证纪律 -------------------------


async def test_token_file_stores_only_hashes():
    """凭证文件只存 SHA-256 哈希，明文不落盘。"""
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        raw = (plugin.data_dir / "external_write_tokens.json").read_text(
            encoding="utf-8"
        )
        assert token not in raw, "凭证明文落盘了"
        assert plugin._external_token_hash(token) in raw


async def test_token_file_not_in_backup_bundle():
    """一键备份只打包 diary_*.zip，凭证文件不随导出外流。"""
    async with World() as world:
        plugin = world.refactor.plugin
        _make_token(plugin)
        plugin._save_diary(SENDER, "内容", sender_name=SENDER_NAME,
                           origin="private", mode="full")
        bundle, count, err = plugin._prepare_backup_all()
        assert err is None and count == 1
        try:
            with zipfile.ZipFile(bundle) as z:
                names = z.namelist()
            assert not any("token" in n.lower() for n in names), names
        finally:
            bundle.unlink(missing_ok=True)


# ------------------------- 指令：/日记凭证 -------------------------


async def test_diary_token_command_flow():
    # use_persona=False：_say 直接给固定文案，断言语句才稳定
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        # 群聊拒绝
        out = await inst.run_handler(
            "diary_token",
            make_event("/日记凭证 新办 dsh", sender_id=SENDER,
                       sender_name=SENDER_NAME, group_id="88888"),
        )
        assert any("私聊" in t for t in out), out
        # 私聊新办
        out = await inst.run_handler(
            "diary_token", make_event("/日记凭证 新办 dsh", sender_id=SENDER,
                                      sender_name=SENDER_NAME)
        )
        text = "\n".join(out)
        assert "只显示这一次" in text and "mdw_" in text, text
        token = [ln for ln in text.split("\n") if ln.startswith("mdw_")][0]
        # 列表
        out = await inst.run_handler(
            "diary_token", make_event("/日记凭证 列表", sender_id=SENDER,
                                      sender_name=SENDER_NAME)
        )
        assert any("dsh" in t and "在用" in t for t in out), out
        assert token not in "\n".join(out), "列表泄露了凭证本体"
        # 新办的凭证真能用
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "指令办的凭证", "ext_id": "cmd-1",
        })
        assert r["status"] == 200, r
        # 吊销后即失效
        out = await inst.run_handler(
            "diary_token", make_event("/日记凭证 吊销 1", sender_id=SENDER,
                                      sender_name=SENDER_NAME)
        )
        assert any("已吊销" in t for t in out), out
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "x", "ext_id": "cmd-2",
        })
        assert r["status"] == 401, r


# ------------------------- 来源显示名 -------------------------


async def test_note_source_display_name_override():
    """配置的 external_source_display 只改分隔条展示名，provenance 仍记机器标识。"""
    async with World(config={"external_source_display": "dsh=零陵香"}) as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "她那边记了一条重要的事", "ext_id": "disp-1",
        })
        assert r["status"] == 200, r
        content = _read_day(plugin, TODAY)
        assert "（外部·零陵香）" in content, content
        assert "（外部·dsh）" not in content, content
        prov, status = plugin.load_provenance(SENDER)
        assert status == "ok", status
        recs = prov["entries"]["20261023.md"]["external"]
        assert recs[-1]["source"] == "dsh", recs


async def test_note_source_display_name_fallback_and_malformed():
    """未配置 / 配置格式坏时回退原始 source 标识。"""
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "默认显示名", "ext_id": "disp-2",
        })
        assert r["status"] == 200, r
        content = _read_day(plugin, TODAY)
        assert "（外部·dsh）" in content, content

    async with World(config={"external_source_display": "=无键对;dsh= ;app2=茴香"}) as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "坏配置回退", "ext_id": "disp-3",
        })
        assert r["status"] == 200, r
        content = _read_day(plugin, TODAY)
        assert "（外部·dsh）" in content, content


# ------------------------- 多用户隔离 -------------------------


async def test_note_two_users_same_source_write_to_own_diaries():
    """凭证即定位主人：同来源标识的两枚凭证各写各的日记，互不越界。"""
    async with World() as world:
        plugin = world.refactor.plugin
        token_a = _make_token(plugin, sender="10001")
        token_b = _make_token(plugin, sender="20002")
        r = await _post(plugin, {
            "token": token_a, "source": "dsh", "day": TODAY,
            "text": "A 的记忆", "ext_id": "iso-a1",
        })
        assert r["status"] == 200, r
        r = await _post(plugin, {
            "token": token_b, "source": "dsh", "day": TODAY,
            "text": "B 的记忆", "ext_id": "iso-b1",
        })
        assert r["status"] == 200, r
        # 各写各的
        assert "A 的记忆" in _read_day(plugin, TODAY, sender="10001")
        assert "B 的记忆" in _read_day(plugin, TODAY, sender="20002")
        # 交叉验证：互不可见
        assert "B 的记忆" not in _read_day(plugin, TODAY, sender="10001")
        assert "A 的记忆" not in _read_day(plugin, TODAY, sender="20002")


# ------------------------- 审查报告回归（_review_v062） -------------------------


async def test_note_ext_id_trailing_newline_still_idempotent():
    """P2-3：ext_id 尾部换行先归一化再判重——首次写入与重放是同一幂等键，不重复追加。"""
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        payload = {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "同一篇记忆", "ext_id": "abc\n",
        }
        r1 = await _post(plugin, payload)
        assert r1["status"] == 200 and r1["body"]["appended"] is True, r1
        r2 = await _post(plugin, {**payload, "ext_id": "abc"})
        assert r2["body"].get("replayed") is True, r2
        assert _read_day(plugin, TODAY).count("同一篇记忆") == 1


async def test_note_idempotency_window_keeps_most_recent_at_capacity():
    """P1-1：单篇条目 external 达上限时保留最近记录——刚写入的 ext_id 重放不重复追加。"""
    async with World() as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        cap = plugin.PROVENANCE_MAX_EXTERNAL_PER_ENTRY
        for i in range(cap + 1):
            plugin._ext_rate_buckets().clear()  # 绕过限流，专测容量边界
            r = await _post(plugin, {
                "token": token, "source": "dsh", "day": TODAY,
                "text": f"容量第 {i} 条", "ext_id": f"cap-{i}",
            })
            assert r["status"] == 200, (i, r)
        prov, status = plugin.load_provenance(SENDER)
        assert status == "ok"
        recs = prov["entries"]["20261023.md"]["external"]
        assert len(recs) == cap, len(recs)
        assert recs[-1]["ext_id"] == f"cap-{cap}", recs[-3:]
        plugin._ext_rate_buckets().clear()
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": f"容量第 {cap} 条", "ext_id": f"cap-{cap}",
        })
        assert r["body"].get("replayed") is True, r
        assert _read_day(plugin, TODAY).count(f"容量第 {cap} 条") == 1


async def test_note_triggers_legacy_migration_before_write():
    """P1-2：外部写入前先迁移旧格式——旧日记绝不因整包重写永久丢失。"""
    async with World() as world:
        plugin = world.refactor.plugin
        legacy = "旧格式 v2.0 的全部日记内容"
        plugin._write_zip_atomic(
            plugin._diary_path(SENDER),
            {
                "diary.md": legacy.encode("utf-8"),
                "diary.sig": plugin._signature(SENDER, legacy).encode("utf-8"),
            },
        )
        token = _make_token(plugin)
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "外部写入的新段落", "ext_id": "mig-1",
        })
        assert r["status"] == 200, r
        content = _read_day(plugin, TODAY)
        assert "外部写入的新段落" in content
        # 旧内容落在迁移日（v2.0 迁移按 zip 修改时间归档，非固定时钟），
        # 所以全包 diaries/ 范围断言，不按固定日期钉死。
        with zipfile.ZipFile(_zip_path(plugin)) as z:
            all_text = "".join(
                z.read(n).decode("utf-8", "ignore")
                for n in z.namelist()
                if n.startswith("diaries/")
            )
        assert legacy in all_text, "旧日记内容在外部写入后丢失"


async def test_note_display_name_sanitized_against_separator_forgery():
    """P3-1：显示名剔除换行/分隔条字符并截断，配置值无法伪造日记分隔条。"""
    async with World(config={
        "external_source_display": "dsh=坏——名（x）" + "y" * 30,
    }) as world:
        plugin = world.refactor.plugin
        token = _make_token(plugin)
        r = await _post(plugin, {
            "token": token, "source": "dsh", "day": TODAY,
            "text": "消毒测试", "ext_id": "san-1",
        })
        assert r["status"] == 200, r
        content = _read_day(plugin, TODAY)
        assert "（外部·坏名x" in content, content
        assert "坏——名" not in content
