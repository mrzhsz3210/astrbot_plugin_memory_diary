# -*- coding: utf-8 -*-
"""v0.6 D7 批注存储层：侧车往返 / 签名三态 / 删除级联 / 500 条上限 / 迁移包。

依据《批注功能接口契约》第 3、4、9 节与设计方案第二部分 D7。
只驱动 refactor 侧（baseline v0.5.5 没有批注能力），不联网。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from annotation_testkit import (
    ANCHOR,
    DAY,
    NOTE,
    OTHER_DAY,
    ROUND,
    SENDER,
    ann_data,
    annotations_raw,
    annotations_sig_raw,
    assert_raises,
    broken_json_raw,
    drop_annotations_sig,
    make_annotation,
    make_bundle,
    make_reply,
    repack_inner_diary_zip,
    rewrite_annotations_raw,
    seed_annotations,
    seed_diary,
    seed_two_day_annotations,
    tamper_annotations,
    zip_entries,
)
from harness.world import World
from image_testkit import md5_key

OTHER = "20002"


def _plugin(world):
    return world.refactor.plugin


def _empty():
    return {
        "version": 1,
        "last_round": "",
        "last_title": "",
        "updated_at": "",
        "annotations": [],
    }


# ---------------------------------------------------------------------------
# 侧车写入 → 读出（含签名口径）
# ---------------------------------------------------------------------------


async def test_annotations_roundtrip_writes_sidecar_and_signature():
    async with World(config={}) as world:
        plugin = _plugin(world)
        data = ann_data(
            [
                make_annotation(1, is_question=True),
                make_annotation(
                    2,
                    anchor="去了江边",
                    note="原来她也会一个人去这种地方啊。",
                    created_at="2026-10-31T19:34:00",
                ),
            ],
            last_title="页边的话",
        )
        assert plugin.save_annotations(SENDER, data) is True

        # zip 里必须同时有侧车与签名条目
        entries = zip_entries(plugin._diary_path(SENDER))
        assert plugin.ANNOTATIONS_ENTRY in entries, "annotations.json 没写进日记 zip"
        assert plugin.ANNOTATIONS_SIG_ENTRY in entries, "批注签名条目缺失"
        raw = entries[plugin.ANNOTATIONS_ENTRY]
        assert entries[plugin.ANNOTATIONS_SIG_ENTRY].decode("utf-8").strip() == (
            plugin._annotations_sig(SENDER, raw)
        ), "zip 内签名与 _annotations_sig 计算不一致"

        loaded, status = plugin.load_annotations(SENDER)
        assert status == "ok"
        assert loaded["last_round"] == ROUND
        assert loaded["last_title"] == "页边的话"
        records = loaded["annotations"]
        assert [r["id"] for r in records] == ["ann_20261023_1", "ann_20261023_2"]
        assert records[0]["anchor"] == ANCHOR and records[0]["note"] == NOTE
        assert records[0]["is_question"] is True
        assert records[0]["status"] == "open"
        assert records[0]["entry"] == f"{DAY}.md", (
            f"entry 必须是 basename（{DAY}.md），实际 {records[0]['entry']!r}"
        )
        assert records[0]["round"] == ROUND
        assert plugin.annotations_status(SENDER) == "ok"

        # 规范序列化：同一个 data 逐字节一致（保存/安装/重签共用）
        assert plugin._dump_annotations(loaded) == raw
        assert plugin._dump_annotations(json.loads(raw.decode("utf-8"))) == raw


async def test_annotations_status_missing_when_zip_or_entry_absent():
    async with World(config={}) as world:
        plugin = _plugin(world)

        # 1) 还没有日记 zip：missing（旧用户兼容，不告警）
        assert plugin.annotations_status(SENDER) == "missing"
        loaded, status = plugin.load_annotations(SENDER)
        assert status == "missing"
        assert loaded == _empty(), f"missing 应返回空骨架，实际 {loaded!r}"

        # 2) 有日记 zip 但没有 annotations.json：仍是 missing
        seed_diary(plugin)
        assert plugin.annotations_status(SENDER) == "missing"
        assert plugin.load_annotations(SENDER) == (_empty(), "missing")

        # 3) 主密钥不可用 + 条目缺失 → 仍按 missing（旧包兼容优先于 no_key）
        md5_path = plugin.data_dir / f"diary_{md5_key(SENDER)}.zip"
        world.refactor.diary_path(SENDER).rename(md5_path)
        key_path = plugin.data_dir / ".diary_key"
        key_path.unlink()
        key_path.mkdir()
        plugin._key_cache = None
        assert plugin.load_annotations(SENDER) == (_empty(), "missing"), (
            "条目缺失时应优先返回 missing（不告警），而不是 no_key"
        )


async def test_annotations_tampered_after_raw_replaced():
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_annotations(plugin)
        tampered_raw = tamper_annotations(plugin)

        loaded, status = plugin.load_annotations(SENDER)
        assert status == "tampered", f"改过批注 JSON 后应判篡改，实际 {status!r}"
        assert loaded == _empty(), "篡改时不应返回任何批注内容"
        assert plugin.annotations_status(SENDER) == "tampered"

        # fail-closed：拒绝覆写，绝不替被改过的批注重签
        ok = plugin.save_annotations(
            SENDER, ann_data([make_annotation(9, note="新内容")])
        )
        assert ok is False, "旧批注验签失败时 save_annotations 必须拒绝写入"
        assert annotations_raw(plugin) == tampered_raw, "拒绝写入却改动了批注文件"

        # update_annotations 同样拒绝，且不调用 mutator
        called = {"n": 0}

        def mutator(data):
            called["n"] += 1
            data["annotations"].append(make_annotation(9, note="不该写入"))

        ok, data = plugin.update_annotations(SENDER, mutator)
        assert ok is False and data == _empty()
        assert called["n"] == 0, "tampered 状态不应把数据交给 mutator"


async def test_annotations_tampered_when_signature_missing():
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_annotations(plugin)
        drop_annotations_sig(plugin)

        assert plugin.annotations_status(SENDER) == "tampered"
        loaded, status = plugin.load_annotations(SENDER)
        assert status == "tampered" and loaded == _empty()
        assert plugin.save_annotations(SENDER, ann_data()) is False


async def test_annotations_broken_json_after_resign_is_tampered():
    """签名正确但 JSON 坏：解析失败按篡改处理（宁严勿宽），不展示半截数据。"""
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_annotations(plugin)
        broken_json_raw(plugin)

        assert plugin.annotations_status(SENDER) == "tampered"
        assert plugin.load_annotations(SENDER) == (_empty(), "tampered")
        assert plugin.save_annotations(
            SENDER, ann_data([make_annotation(3, note="不会写进去")])
        ) is False

        # 编辑器的脏字节（非 UTF-8）也只按篡改处理，不抛异常
        rewrite_annotations_raw(plugin, b"\xff\xfe\x00not-utf8")
        assert plugin.annotations_status(SENDER) == "tampered"


async def test_annotations_no_key_when_master_key_unavailable():
    async with World(config={}) as world:
        inst = world.refactor
        plugin = inst.plugin
        seed_annotations(plugin)

        # 模拟主密钥不可用：文件存在但读不出（把 key 变成同名目录），
        # 并把 zip 挪到旧 MD5 命名（密钥不可用时 _diary_path 会回退到它）。
        md5_path = plugin.data_dir / f"diary_{md5_key(SENDER)}.zip"
        inst.diary_path(SENDER).rename(md5_path)
        key_path = plugin.data_dir / ".diary_key"
        key_path.unlink()
        key_path.mkdir()
        plugin._key_cache = None

        assert plugin.annotations_status(SENDER) == "no_key"
        loaded, status = plugin.load_annotations(SENDER)
        assert status == "no_key" and loaded == _empty()
        assert plugin.save_annotations(SENDER, ann_data()) is False, (
            "主密钥不可用时必须拒绝写批注（验不了 = 不可信）"
        )


# ---------------------------------------------------------------------------
# 与日记生命周期共存：_save_diary 保留 / 删除级联
# ---------------------------------------------------------------------------


async def test_save_diary_rewrite_preserves_annotations_bytes():
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_annotations(plugin)
        raw_before = annotations_raw(plugin)
        sig_before = annotations_sig_raw(plugin)

        # 同一天再写一篇（_save_diary 会整体重写 zip）
        plugin._save_diary(
            SENDER, "第二篇日记。", sender_name="阿罗", origin="private", mode="brief"
        )

        assert annotations_raw(plugin) == raw_before, (
            "_save_diary 重写 zip 时批注侧车没有逐字节保留"
        )
        assert annotations_sig_raw(plugin) == sig_before, (
            "_save_diary 不应重签批注（原样搬运签名）"
        )
        loaded, status = plugin.load_annotations(SENDER)
        assert status == "ok" and loaded["annotations"], "重写后批注读不出来了"


async def test_delete_entries_cascades_annotations():
    async with World(config={}) as world:
        plugin = _plugin(world)
        records = seed_two_day_annotations(plugin)

        removed = plugin._delete_entries(SENDER, [f"diaries/{DAY}.md"])
        assert removed == 1, f"应删除 1 天，实际 {removed}"

        loaded, status = plugin.load_annotations(SENDER)
        assert status == "ok"
        remaining = [r["id"] for r in loaded["annotations"]]
        assert remaining == [records[1]["id"]], (
            f"删除日记应连带删除锚定该天的批注，实际留下 {remaining!r}"
        )

        # 删掉最后一天：整包删除，批注侧车也不存在了
        plugin._delete_entries(SENDER, [f"diaries/{OTHER_DAY}.md"])
        assert plugin.load_annotations(SENDER) == (_empty(), "missing"), (
            "删光日记后批注侧车应随 zip 一起消失"
        )


async def test_delete_annotations_for_days_counts_and_noop():
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_two_day_annotations(plugin)
        path = plugin._diary_path(SENDER)
        raw_before = annotations_raw(plugin)

        # days 为空 / 没有锚定在该天的批注 → 不改 zip
        assert plugin._delete_annotations_for_days(SENDER, set()) == 0
        assert plugin._delete_annotations_for_days(SENDER, {"20250101"}) == 0
        assert annotations_raw(plugin) == raw_before, "无批注可删时不应改写 zip"

        # 命中一天 → 返回删除条数，其余保留
        assert plugin._delete_annotations_for_days(SENDER, {DAY}) == 1
        loaded, status = plugin.load_annotations(SENDER)
        assert status == "ok"
        assert [r["id"] for r in loaded["annotations"]] == ["ann_20261022_2"]

        # 删空最后一条 → annotations.json 与签名条目一起移除
        assert plugin._delete_annotations_for_days(SENDER, {OTHER_DAY}) == 1
        entries = zip_entries(path)
        assert plugin.ANNOTATIONS_ENTRY not in entries
        assert plugin.ANNOTATIONS_SIG_ENTRY not in entries
        assert plugin.load_annotations(SENDER) == (_empty(), "missing")


# ---------------------------------------------------------------------------
# 500 条上限 / 规范化
# ---------------------------------------------------------------------------


async def test_annotations_capacity_trim_drops_oldest_resolved_first():
    async with World(config={}) as world:
        plugin = _plugin(world)
        base = datetime(2026, 1, 1)
        resolved = [
            make_annotation(
                i,
                status="resolved",
                note=f"已解{i}",
                created_at=(base + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S"),
                reply=make_reply(),
            )
            for i in range(300)
        ]
        opened = [
            make_annotation(
                1000 + i,
                status="open",
                note=f"未解{i}",
                created_at=(base + timedelta(days=10, minutes=i)).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                ),
            )
            for i in range(300)
        ]
        out = plugin._normalize_annotations(
            ann_data(resolved + opened, last_title="页边的话")
        )
        items = out["annotations"]
        assert len(items) == plugin.ANNOTATIONS_MAX_RECORDS == 500, (
            f"容量上限应为 500，实际 {len(items)}"
        )
        resolved_ids = {r["id"] for r in items if r["status"] == "resolved"}
        open_ids = {r["id"] for r in items if r["status"] == "open"}
        # 超出的 100 条全部从最旧的 resolved 里丢
        assert len(resolved_ids) == 200, f"应保留最新的 200 条已解，实际 {len(resolved_ids)}"
        assert "ann_20261023_0" not in resolved_ids, "最旧的已解批注没有被丢掉"
        assert "ann_20261023_299" in resolved_ids, "最新的已解批注被误删"
        assert len(open_ids) == 300, "还有已解可丢时不应丢未解条目"


async def test_normalize_annotations_sanitizes_input_without_mutating_it():
    async with World(config={}) as world:
        plugin = _plugin(world)
        raw = {
            "version": 99,
            "last_round": 123,
            "last_title": "标题" * 40,
            "annotations": [
                "不是 dict",
                make_annotation(1, status="weird", note="非法状态"),
                {"anchor": "", "note": "没有锚点"},
                {"anchor": "有锚点", "note": ""},
                {"anchor": "有锚点", "note": "有内容"},
                {"anchor": "有锚点2", "note": "字符串false", "is_question": "false"},
                {"anchor": "有锚点3", "note": "字符串true", "is_question": "true"},
            ],
        }
        snapshot = json.dumps(raw, ensure_ascii=False)
        out = plugin._normalize_annotations(raw)
        assert json.dumps(raw, ensure_ascii=False) == snapshot, (
            "_normalize_annotations 不应修改入参"
        )
        assert out["version"] == 1
        assert out["last_title"] and len(out["last_title"]) <= 50
        items = out["annotations"]
        assert [i["note"] for i in items] == [
            "非法状态",
            "有内容",
            "字符串false",
            "字符串true",
        ], f"坏条目应被丢弃、非法 status 回退 open，实际 {items!r}"
        assert items[0]["status"] == "open"
        assert items[0]["reply"] is None
        assert items[0]["id"] == "ann_20261023_1"
        # 规范化必须把模型写出来的字符串布尔值转成真布尔（bool("false") == True 的坑）
        assert [i["is_question"] for i in items] == [False, False, False, True], (
            f'is_question 字符串规范化错误：{[i["is_question"] for i in items]!r}'
        )

        # 非 dict 输入 → 空骨架（不抛）
        assert plugin._normalize_annotations(None) == _empty()
        assert plugin._normalize_annotations(["x"]) == _empty()


async def test_annotations_for_day_sorted_and_deep_copied():
    async with World(config={}) as world:
        plugin = _plugin(world)
        records = [
            make_annotation(
                3, day=DAY, note="第三条", created_at="2026-10-31T19:36:00"
            ),
            make_annotation(
                1, day=DAY, note="第一条", created_at="2026-10-31T19:34:00"
            ),
            make_annotation(
                2, day=DAY, note="第二条", created_at="2026-10-31T19:35:00"
            ),
            make_annotation(
                4, day=OTHER_DAY, note="别的一天", created_at="2026-10-31T19:37:00"
            ),
        ]
        seed_annotations(plugin, records)
        items, status = plugin.annotations_for_day(SENDER, DAY)
        assert status == "ok"
        assert [i["note"] for i in items] == ["第一条", "第二条", "第三条"], (
            "annotations_for_day 必须按 created_at 升序返回"
        )
        # 深拷贝：调用方改返回值不能污染落盘数据
        items[0]["note"] = "被调用方改掉"
        items.append({"x": 1})
        items2, _ = plugin.annotations_for_day(SENDER, DAY)
        assert [i["note"] for i in items2] == ["第一条", "第二条", "第三条"]
        # 别人的日记不受影响
        other, _ = plugin.annotations_for_day(SENDER, OTHER_DAY)
        assert [i["note"] for i in other] == ["别的一天"]

        # tampered → 列表为空（不返回半信半疑的数据）
        tamper_annotations(plugin)
        empty, status = plugin.annotations_for_day(SENDER, DAY)
        assert status == "tampered" and empty == []


# ---------------------------------------------------------------------------
# 迁移包：_verify_bundle 5 元组 / 篡改拒绝 / 旧包兼容 / 安装重签
# ---------------------------------------------------------------------------


async def test_verify_bundle_returns_five_tuple_with_annotations():
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_annotations(plugin)
        bundle = make_bundle(plugin)
        try:
            result = plugin._verify_bundle(bundle)
            assert isinstance(result, tuple) and len(result) == 5, (
                f"_verify_bundle 应返回 5 元组 "
                f"(owner, key, entries, images, annotations)，实际 {result!r}"
            )
            owner, user_key, entries, images, annotations = result
            assert owner == SENDER
            assert images == {}, f"没有插图的迁移包 images 应为空 dict，实际 {images!r}"
            assert isinstance(annotations, dict), "迁移包内的批注应是规范 dict"
            assert [a["id"] for a in annotations["annotations"]] == ["ann_20261023_1"]
            assert annotations["last_title"] == "页边的话"
            assert annotations["last_round"] == ROUND
            assert f"diaries/{DAY}.md" in entries
        finally:
            bundle.unlink(missing_ok=True)


async def test_verify_bundle_rejects_tampered_annotations():
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_annotations(plugin)
        bundle = make_bundle(plugin)
        try:
            def _swap(inner):
                assert plugin.ANNOTATIONS_ENTRY in inner
                inner[plugin.ANNOTATIONS_ENTRY] = (
                    inner[plugin.ANNOTATIONS_ENTRY].replace(b"open", b"evil")
                )

            repack_inner_diary_zip(bundle, _swap)
            with assert_raises(ValueError) as caught:
                plugin._verify_bundle(bundle)
            message = str(caught.value)
            assert "批注" in message or "验签" in message or "改动" in message, (
                f"批注被改后的错误信息不够明确：{message!r}"
            )

            # 签名条目缺失同样拒绝
            def _drop_sig(inner):
                inner.pop(plugin.ANNOTATIONS_SIG_ENTRY)

            seed_annotations(plugin, [make_annotation(2, note="换一份")])
            bundle2 = make_bundle(plugin)
            try:
                repack_inner_diary_zip(bundle2, _drop_sig)
                with assert_raises(ValueError) as caught2:
                    plugin._verify_bundle(bundle2)
                assert str(caught2.value)
            finally:
                bundle2.unlink(missing_ok=True)
        finally:
            bundle.unlink(missing_ok=True)


async def test_verify_bundle_old_package_without_annotations_is_none():
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_diary(plugin)
        bundle = plugin._build_export_bundle(
            SENDER, plugin._diary_path(SENDER), plugin._list_entries(SENDER)
        )
        try:
            owner, user_key, entries, images, annotations = plugin._verify_bundle(bundle)
            assert owner == SENDER
            assert annotations is None, (
                f"旧包（无 annotations.json）第 5 项应为 None，实际 {annotations!r}"
            )
            assert images == {}
        finally:
            bundle.unlink(missing_ok=True)


async def test_install_bundle_resigns_annotations_with_local_key():
    async with World(config={}) as src_world, World(config={}) as dst_world:
        src_plugin = _plugin(src_world)
        seed_annotations(src_plugin)
        bundle = make_bundle(src_plugin)
        try:
            _owner, user_key, entries, images, annotations = src_plugin._verify_bundle(
                bundle
            )
            dst_plugin = _plugin(dst_world)
            mode = dst_plugin._install_bundle(
                SENDER,
                SENDER,
                user_key,
                entries,
                images=images,
                annotations=annotations,
            )
            assert mode == "same_key", f"同测试主密钥安装应为 same_key，实际 {mode!r}"
            dst_entries = zip_entries(dst_plugin._diary_path(SENDER))
            assert dst_plugin.ANNOTATIONS_ENTRY in dst_entries
            assert dst_plugin.ANNOTATIONS_SIG_ENTRY in dst_entries
            raw = dst_entries[dst_plugin.ANNOTATIONS_ENTRY]
            assert dst_entries[dst_plugin.ANNOTATIONS_SIG_ENTRY].decode().strip() == (
                dst_plugin._annotations_sig(SENDER, raw)
            ), "安装迁移包时没有用本机钥匙重签批注"
            loaded, status = dst_plugin.load_annotations(SENDER)
            assert status == "ok", f"导入后批注验签失败：{status}"
            assert [a["id"] for a in loaded["annotations"]] == ["ann_20261023_1"]
            assert loaded["annotations"][0]["anchor"] == ANCHOR
        finally:
            bundle.unlink(missing_ok=True)


async def test_install_bundle_without_annotations_writes_no_sidecar():
    async with World(config={}) as src_world, World(config={}) as dst_world:
        src_plugin = _plugin(src_world)
        seed_diary(src_plugin)
        bundle = src_plugin._build_export_bundle(
            SENDER, src_plugin._diary_path(SENDER), src_plugin._list_entries(SENDER)
        )
        try:
            _owner, user_key, entries, images, annotations = src_plugin._verify_bundle(
                bundle
            )
            assert annotations is None
            dst_plugin = _plugin(dst_world)
            dst_plugin._install_bundle(
                SENDER, SENDER, user_key, entries, images=images, annotations=annotations
            )
            dst_entries = zip_entries(dst_plugin._diary_path(SENDER))
            assert dst_plugin.ANNOTATIONS_ENTRY not in dst_entries, (
                "annotations=None 时不应写批注条目（旧包兼容）"
            )
            assert dst_plugin.load_annotations(SENDER)[1] == "missing"
        finally:
            bundle.unlink(missing_ok=True)


async def test_annotations_isolated_between_users():
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_annotations(plugin)
        # 另一个用户没有日记也没有批注
        assert plugin.load_annotations(OTHER) == (_empty(), "missing")
        assert plugin.annotations_for_day(OTHER, DAY) == ([], "missing")

        seed_diary(plugin, "别人的日记。", sender=OTHER)
        assert plugin.save_annotations(
            OTHER, ann_data([make_annotation(7, note="别人的批注")])
        )
        mine, _ = plugin.load_annotations(SENDER)
        other, _ = plugin.load_annotations(OTHER)
        assert [a["note"] for a in mine["annotations"]] == [NOTE]
        assert [a["note"] for a in other["annotations"]] == ["别人的批注"]
