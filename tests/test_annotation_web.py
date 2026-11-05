# -*- coding: utf-8 -*-
"""v0.6 D10 Web 端：web_read_day 条件字段 / tampered 暴露 / 鉴权不回归 / 导入带批注。

依据《批注功能接口契约》第 6.5、6.7、9 节与设计方案第二部分 D10。
只驱动 refactor 侧；请求/响应都用真实 astrbot.api.web 的 fake request。
"""

from __future__ import annotations

import json

from astrbot.api.web import bind_request_context

from annotation_testkit import (
    DAY,
    SENDER,
    SENDER_NAME,
    ann_config,
    make_annotation,
    make_reply,
    seed_annotations,
    seed_diary,
    tamper_annotations,
)
from harness.fakes import (
    FakeWebRequest,
    file_component,
    make_event,
    make_upload,
)
from harness.world import World
from image_testkit import rewrite_zip, zip_entries

PASSWORD = "secret123"


def _plugin(world):
    return world.refactor.plugin


async def _call(handler, request, *args):
    with bind_request_context(request):
        response = await handler(*args)
    if isinstance(response, dict) or response is None:
        return response
    body = getattr(response, "body", b"{}")
    return {
        "status": getattr(response, "status_code", None),
        "body": json.loads(bytes(body).decode("utf-8")),
    }


def _event(text: str):
    return make_event(text, sender_id=SENDER, sender_name=SENDER_NAME)


def _seed_annotated(plugin):
    seed_diary(plugin)
    seed_annotations(
        plugin,
        [
            make_annotation(
                1,
                anchor="喜欢在深夜聊天",
                note="她总是这么晚才开口。",
                is_question=True,
                created_at="2026-10-29T10:00:00",
            ),
            make_annotation(
                2,
                anchor="去了江边",
                note="原来她也会一个人去这种地方啊。",
                status="resolved",
                created_at="2026-10-30T09:00:00",
                reply=make_reply(summary="我们聊明白了：那陣子她在等一个答复。"),
            ),
        ],
        last_title="页边的话",
    )


# ---------------------------------------------------------------------------
# web_read_day：有批注 / 无批注 / tampered / 关闭
# ---------------------------------------------------------------------------


async def test_web_read_day_includes_annotations_payload():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        _seed_annotated(plugin)
        file_key = plugin._diary_file_key(SENDER)

        resp = await _call(
            plugin.web_read_day, FakeWebRequest(query={"qq": SENDER}), file_key, DAY
        )
        assert resp["status"] == 200
        body = resp["body"]
        assert body["tampered"] is False
        assert "annotations_tampered" not in body, "验签正常时不应出现 tampered 标记"
        annotations = body["annotations"]
        assert len(annotations) == 2
        first = annotations[0]
        assert first["id"] == "ann_20261023_1"
        assert first["anchor"] == "喜欢在深夜聊天"
        assert first["note"] == "她总是这么晚才开口。"
        assert first["is_question"] is True
        assert first["status"] == "open"
        assert first["created_at"] == "2026-10-29T10:00:00"
        assert first["reply"] is None
        resolved = annotations[1]
        assert resolved["status"] == "resolved"
        assert resolved["reply"]["summary"].startswith("我们聊明白了")
        # 日记正文仍照常返回
        assert "今天下午一个人去了江边" in body["content"]


async def test_web_read_day_no_annotations_adds_no_field():
    """零批注时响应与 v0.5.5 逐字节一致（差分），且不多任何键。"""
    async with World(config=ann_config()) as world:
        async def scenario(inst):
            plugin = inst.plugin
            plugin._save_diary(
                SENDER,
                "没有批注的一天。",
                sender_name=SENDER_NAME,
                origin="private",
                mode="full",
            )
            file_key = plugin._diary_file_key(SENDER)
            return await _call(
                plugin.web_read_day, FakeWebRequest(query={"qq": SENDER}), file_key, DAY
            )

        pair = await world.run_pair(scenario, label="web read day zero annotation")
        body = pair.value["body"]
        assert pair.value["status"] == 200
        assert set(body.keys()) == {"tampered", "label", "content"}, (
            f"零批注时不得多出字段：{sorted(body)}"
        )
        assert "没有批注的一天。" in body["content"]


async def test_web_read_day_tampered_annotations_flag_without_content_leak():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        _seed_annotated(plugin)
        tamper_annotations(plugin)
        file_key = plugin._diary_file_key(SENDER)

        resp = await _call(
            plugin.web_read_day, FakeWebRequest(query={"qq": SENDER}), file_key, DAY
        )
        body = resp["body"]
        assert body.get("annotations_tampered") is True, (
            f"批注验签失败应暴露 annotations_tampered：{body!r}"
        )
        assert "annotations" not in body, "篡改时不得返回任何批注内容"
        # 日记本身没被改，正文照常可读
        assert body["tampered"] is False
        assert "今天下午一个人去了江边" in body["content"]


async def test_web_read_day_disabled_hides_annotations():
    async with World(config={"annotation_enabled": False}) as world:
        inst = world.refactor
        plugin = _plugin(world)
        # 绕过开关先写批注（模拟"曾经开过、现在关了"）
        plugin.config["annotation_enabled"] = True
        _seed_annotated(plugin)
        plugin.config["annotation_enabled"] = False
        file_key = plugin._diary_file_key(SENDER)

        resp = await _call(
            plugin.web_read_day, FakeWebRequest(query={"qq": SENDER}), file_key, DAY
        )
        body = resp["body"]
        assert "annotations" not in body and "annotations_tampered" not in body


async def test_web_read_day_diary_tampered_branch_has_no_annotation_fields():
    """日记本身验签失败的早退分支绝不附加批注字段（不泄露）。"""
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        _seed_annotated(plugin)
        path = plugin._diary_path(SENDER)
        entries = zip_entries(path)
        entry = f"diaries/{DAY}.md"
        entries[entry] = entries[entry] + "\n被改".encode("utf-8")
        rewrite_zip(path, entries)
        file_key = plugin._diary_file_key(SENDER)

        resp = await _call(
            plugin.web_read_day, FakeWebRequest(query={"qq": SENDER}), file_key, DAY
        )
        body = resp["body"]
        assert body == {
            "tampered": True,
            "label": "2026-10-23",
            "content": "",
        }, f"日记篡改早退分支的响应不应有额外字段：{body!r}"


# ---------------------------------------------------------------------------
# 鉴权/密码闸不回归
# ---------------------------------------------------------------------------


async def test_web_read_day_password_gate_still_works_with_annotations():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        _seed_annotated(plugin)
        plugin._save_pref_data(
            SENDER, {"view_password": plugin._make_view_password(PASSWORD)}
        )
        file_key = plugin._diary_file_key(SENDER)

        no_pw = await _call(plugin.web_read_day, FakeWebRequest(), file_key, DAY)
        assert no_pw["body"] == {"need_password": True}
        wrong = await _call(
            plugin.web_read_day, FakeWebRequest(query={"pw": "nope"}), file_key, DAY
        )
        assert wrong["status"] == 403
        right = await _call(
            plugin.web_read_day,
            FakeWebRequest(query={"pw": PASSWORD, "qq": SENDER}),
            file_key,
            DAY,
        )
        assert right["status"] == 200
        assert right["body"]["annotations"], "密码正确时应能读到批注"


async def test_web_read_day_owner_gate_still_works_with_annotations():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        _seed_annotated(plugin)
        path = plugin._diary_path(SENDER)
        entries = zip_entries(path)
        del entries[plugin.README_ZIP_ENTRY]  # 迫使 owner 从 qq hint 全量验签推断
        rewrite_zip(path, entries)
        file_key = plugin._diary_file_key(SENDER)

        unknown = await _call(plugin.web_read_day, FakeWebRequest(), file_key, DAY)
        assert unknown["body"] == {"need_owner": True}
        wrong_qq = await _call(
            plugin.web_read_day, FakeWebRequest(query={"qq": "20002"}), file_key, DAY
        )
        assert wrong_qq["status"] == 400
        right_qq = await _call(
            plugin.web_read_day, FakeWebRequest(query={"qq": SENDER}), file_key, DAY
        )
        assert right_qq["status"] == 200
        assert right_qq["body"]["annotations"], "主人 QQ 正确后批注字段应正常返回"


# ---------------------------------------------------------------------------
# 导入链路带批注（Web 导入 + /导入日记 指令）
# ---------------------------------------------------------------------------


async def test_web_import_round_trip_preserves_annotations():
    async with World(config=ann_config()) as src_world, World(
        config=ann_config()
    ) as dst_world:
        src_plugin = _plugin(src_world)
        _seed_annotated(src_plugin)
        bundle = src_plugin._build_export_bundle(
            SENDER, src_plugin._diary_path(SENDER), src_plugin._list_entries(SENDER)
        )
        try:
            upload = make_upload(bundle.read_bytes(), "bundle.zip")
            dst_plugin = _plugin(dst_world)
            resp = await _call(
                dst_plugin.web_import,
                FakeWebRequest(uploads={"file": upload}, method="POST"),
            )
            assert resp["status"] == 200, resp
            assert resp["body"]["owner"] == SENDER
            assert resp["body"]["imported"] == 1, resp["body"]

            loaded, status = dst_plugin.load_annotations(SENDER)
            assert status == "ok", f"Web 导入后批注验签失败：{status}"
            assert [a["id"] for a in loaded["annotations"]] == [
                "ann_20261023_1",
                "ann_20261023_2",
            ], loaded["annotations"]
            assert loaded["last_title"] == "页边的话"
            # 日记与批注都按本机钥匙重新签名（可读即证明）
            content, tampered, _ = dst_plugin._load_latest(SENDER)
            assert tampered is False and "今天下午一个人去了江边" in content
        finally:
            bundle.unlink(missing_ok=True)


async def test_import_command_round_trip_preserves_annotations():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        _seed_annotated(plugin)
        bundle = plugin._build_export_bundle(
            SENDER, plugin._diary_path(SENDER), plugin._list_entries(SENDER)
        )
        try:
            plugin._delete_diary(SENDER)  # 导入要求本机还没有该用户的日记
            inst.set_replies([{"messages": [file_component(bundle)]}])
            out = await inst.run_handler("import_diary", _event("/导入日记"))
            text = "\n".join(out)
            assert "导入" in text, f"导入指令没有结果反馈：{text!r}"

            loaded, status = plugin.load_annotations(SENDER)
            assert status == "ok", f"指令导入后批注验签失败：{status}"
            assert [a["id"] for a in loaded["annotations"]] == [
                "ann_20261023_1",
                "ann_20261023_2",
            ]
        finally:
            bundle.unlink(missing_ok=True)
