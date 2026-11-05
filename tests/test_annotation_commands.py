# -*- coding: utf-8 -*-
"""v0.6 D9/D10 批注指令与 QQ 呈现：/查看批注、/写回批注、/查看日记 第五部分。

依据《批注功能接口契约》第 5、6.2~6.4、9 节与设计方案第二部分 8.1/8.2。
写回全流程用 harness 的 session_waiter 假实现驱动；固定文案按 Lead 定稿断言。
"""

from __future__ import annotations

import json

from annotation_testkit import (
    DAY,
    OLD_ROUND,
    OTHER_DAY,
    ROUND,
    SENDER,
    SENDER_NAME,
    UMO,
    ann_config,
    make_annotation,
    make_reply,
    seed_annotations,
    seed_diary,
    tamper_annotations,
)
from harness.fakes import make_event
from harness.world import World, message

GROUP_UMO = "aiocqhttp:GroupMessage:88888"

CLOSED_TEXT = "📖 批注功能未开启（管理员可在配置页开启）。"
VIEW_EMPTY_TEXT = "📖 这轮读日记，我还没有在页边留下批注。"
WRITEBACK_EMPTY_TEXT = "📖 现在没有可以写回的批注。"
GROUP_TEXT = "📖 批注是只给本人看的哦，请私聊我查看～"

DISTILL_JSON = json.dumps(
    {
        "reply_title": "主人的回复",
        "summary": "我们聊明白了：那阵子她确实在等一个答复。",
        "speculation_marked": "推测：也许那晚她还有别的话没说。",
    },
    ensure_ascii=False,
)


def _plugin(world):
    return world.refactor.plugin


def _event(text: str, *, group_id: str = ""):
    return make_event(
        text,
        sender_id=SENDER,
        sender_name=SENDER_NAME,
        group_id=group_id,
        umo=UMO if not group_id else GROUP_UMO,
    )


def _view_records():
    """本轮 open + 历史 deferred + 本轮 resolved + 历史 resolved（不展示）。"""
    return [
        make_annotation(
            1,
            anchor="那段话没头没尾的",
            note="这事的前因后果是什么？",
            is_question=True,
            status="open",
            created_at="2026-10-31T10:00:00",
            round_=ROUND,
        ),
        make_annotation(
            2,
            anchor="自己一个人去了江边",
            note="原来她也会一个人去这种地方啊。",
            status="deferred",
            created_at="2026-10-28T09:00:00",
            round_=OLD_ROUND,
        ),
        make_annotation(
            3,
            anchor="项目终于交了",
            note="当时看起来松了口气。",
            status="resolved",
            created_at="2026-10-31T11:00:00",
            round_=ROUND,
            reply=make_reply(
                reply_title="主人的回复",
                summary="我们聊明白了：项目收尾压力很大。",
                speculation_marked="也许还夹杂着别的情绪。",
            ),
        ),
        make_annotation(
            4,
            anchor="旧轮次已解的批注",
            note="这条历史已解不该出现在/查看批注",
            status="resolved",
            created_at="2026-10-20T09:00:00",
            round_=OLD_ROUND,
            reply=make_reply(summary="历史摘要"),
        ),
    ]


def _seed_view_data(plugin):
    seed_diary(plugin)
    seed_annotations(plugin, _view_records())


# ---------------------------------------------------------------------------
# /查看批注
# ---------------------------------------------------------------------------


async def test_view_annotations_disabled_and_group():
    async with World(config={"annotation_enabled": False}) as world:
        inst = world.refactor
        closed = await inst.run_handler("view_annotations", _event("/查看批注"))
        assert closed == [CLOSED_TEXT], closed

        group = await inst.run_handler(
            "view_annotations", _event("/查看批注", group_id="88888")
        )
        assert group == [GROUP_TEXT], group
        assert inst.provider.calls == [], "查看批注不得额外调用模型"


async def test_view_annotations_empty_state():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        out = await inst.run_handler("view_annotations", _event("/查看批注"))
        assert out == [VIEW_EMPTY_TEXT], out
        assert inst.plugin.annotations_view_has_any(SENDER) is False
        assert inst.provider.calls == []


async def test_view_annotations_lists_current_round_and_deferred():
    async with World(config=ann_config(inject_default_days=7)) as world:
        inst = world.refactor
        plugin = _plugin(world)
        _seed_view_data(plugin)

        text = "\n".join(
            await inst.run_handler("view_annotations", _event("/查看批注"))
        )
        assert text.startswith("📖 这 7 天日记里，我留下了 3 条批注："), (
            f"头部文案/条数不对：{text[:80]!r}"
        )
        # 三要素：批注时间 · 状态 / 锚点原文 / 批注内容；条间空行
        assert "[1] 2026-10-28 读 · 悬置" in text, text
        assert "[2] 2026-10-31 读 · 未解" in text
        assert "[3] 2026-10-31 读 · 已解" in text
        assert "锚于：" in text and "批注：" in text
        assert "这事的前因后果是什么？" in text
        assert "原来她也会一个人去这种地方啊。" in text
        assert "\n\n[1]" in text and "\n\n[2]" in text and "\n\n[3]" in text
        # 已解附蒸馏摘要 + 推测分区
        assert "10-31 聊明：我们聊明白了：项目收尾压力很大。" in text
        assert "其中推测：也许还夹杂着别的情绪。" in text
        # 历史轮次的已解批注不展示
        assert "这条历史已解不该出现" not in text
        assert plugin.annotations_view_has_any(SENDER) is True


async def test_view_annotations_tampered_warns_and_hides():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        _seed_view_data(plugin)
        tamper_annotations(plugin)

        text = "\n".join(
            await inst.run_handler("view_annotations", _event("/查看批注"))
        )
        assert text.startswith("📖 页边批注验签失败"), f"应明确告警：{text!r}"
        assert "已拒绝展示" in text
        assert "这事的前因后果是什么？" not in text, "篡改时不得展示任何批注内容"
        assert plugin.annotations_view_has_any(SENDER) is False


# ---------------------------------------------------------------------------
# /写回批注：列表 / 选号 / 草稿 / 确认 / 悬置 / 取消 / 非法输入 / 超时 / 非交互
# ---------------------------------------------------------------------------


async def test_writeback_disabled_group_and_empty():
    async with World(config={"annotation_enabled": False}) as world:
        inst = world.refactor
        closed = await inst.run_handler("writeback_annotation", _event("/写回批注"))
        assert closed == [CLOSED_TEXT], closed
        group = await inst.run_handler(
            "writeback_annotation", _event("/写回批注", group_id="88888")
        )
        assert group == [GROUP_TEXT], group

    async with World(config=ann_config()) as world:
        inst = world.refactor
        empty = await inst.run_handler("writeback_annotation", _event("/写回批注"))
        assert empty == [WRITEBACK_EMPTY_TEXT], empty


async def test_open_annotation_list_and_index_are_consistent():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        records = [
            make_annotation(
                1, note="第一条未解", status="open", created_at="2026-10-31T10:00:00"
            ),
            make_annotation(
                2, note="第二条悬置", status="deferred", created_at="2026-10-31T11:00:00"
            ),
            make_annotation(
                3, note="已解的不列", status="resolved", created_at="2026-10-31T12:00:00"
            ),
        ]
        seed_diary(plugin)
        seed_annotations(plugin, records)

        listing = plugin.open_annotation_list_text(SENDER)
        assert "可以写回的批注有 2 条" in listing, listing
        assert "[1]" in listing and "[2]" in listing and "[3]" not in listing
        assert "第一条未解" in listing and "第二条悬置" in listing
        assert "回复序号" in listing and "取消" in listing

        first = plugin.annotation_by_index(SENDER, 1)
        second = plugin.annotation_by_index(SENDER, 2)
        assert first["id"] == "ann_20261023_1" and first["note"] == "第一条未解"
        assert second["id"] == "ann_20261023_2"
        assert plugin.annotation_by_index(SENDER, 0) is None
        assert plugin.annotation_by_index(SENDER, 3) is None
        assert plugin.annotation_by_index(SENDER, "abc") is None
        # record 是副本，改它不影响落盘数据
        first["note"] = "被改"
        assert plugin.annotation_by_index(SENDER, 1)["note"] == "第一条未解"


async def test_writeback_confirm_flow_writes_signed_reply():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        seed_annotations(plugin, [make_annotation(1)])
        inst.set_history(
            [message("user", "我们聊了那天的事"), message("assistant", "嗯，我明白")]
        )
        inst.provider.replies[:] = [DISTILL_JSON]
        inst.set_replies([{"text": "1"}, {"text": "确认"}])

        out = await inst.run_handler("writeback_annotation", _event("/写回批注"))
        text = "\n".join(out)
        # 列表 → 草稿 → 确认结果
        assert "可以写回的批注有 1 条" in text
        assert "锚于：" in text and "回复标题：主人的回复" in text
        assert "摘要：我们聊明白了：那阵子她确实在等一个答复。" in text
        assert "其中推测：也许那晚她还有别的话没说。" in text, (
            f"草稿的推测分区文案不对：{text[-300:]!r}"
        )
        assert "回复「确认」写回；回复「悬置」先放着；回复「取消」不写。" in text
        assert "已确认写回" in text

        loaded, status = plugin.load_annotations(SENDER)
        assert status == "ok"
        record = loaded["annotations"][0]
        assert record["status"] == "resolved", record
        reply = record["reply"]
        assert reply["confirmed_by_user"] is True
        assert reply["reply_title"] == "主人的回复"
        assert reply["summary"] == "我们聊明白了：那阵子她确实在等一个答复。"
        assert "也许那晚" in reply["speculation_marked"]
        assert reply["resolved_at"] == "2026-10-23T20:30:00", reply["resolved_at"]


async def test_writeback_defer_keeps_status_and_last_round():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        seed_annotations(plugin, [make_annotation(1)])
        inst.set_history([message("user", "聊一聊")])
        inst.provider.replies[:] = [DISTILL_JSON]
        inst.set_replies([{"text": "1"}, {"text": "悬置"}])

        out = "\n".join(
            await inst.run_handler("writeback_annotation", _event("/写回批注"))
        )
        assert "已悬置" in out, out
        loaded, _ = plugin.load_annotations(SENDER)
        record = loaded["annotations"][0]
        assert record["status"] == "deferred"
        assert record["reply"] is None, "悬置不得写回 reply"
        assert loaded["last_round"] == ROUND, "悬置不应改动 batch 的 last_round"


async def test_writeback_cancel_and_illegal_input():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        seed_annotations(plugin, [make_annotation(1)])
        inst.set_history([message("user", "聊一聊")])

        # 取消：草稿后取消，数据不动
        inst.provider.replies[:] = [DISTILL_JSON]
        inst.set_replies([{"text": "1"}, {"text": "取消"}])
        cancelled = "\n".join(
            await inst.run_handler("writeback_annotation", _event("/写回批注"))
        )
        assert "已取消" in cancelled
        loaded, _ = plugin.load_annotations(SENDER)
        assert loaded["annotations"][0]["status"] == "open"

        # 非法输入 → 提示重输并 keep，然后正常选号确认
        inst.provider.replies[:] = [DISTILL_JSON]
        inst.set_replies([{"text": "abc"}, {"text": "999"}, {"text": "1"}, {"text": "确认"}])
        retried = "\n".join(
            await inst.run_handler("writeback_annotation", _event("/写回批注"))
        )
        assert "请回复序号" in retried, retried
        assert "没有这个序号" in retried, retried
        assert "已确认写回" in retried
        loaded, _ = plugin.load_annotations(SENDER)
        assert loaded["annotations"][0]["status"] == "resolved"


async def test_writeback_distill_failure_keeps_data():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        seed_annotations(plugin, [make_annotation(1)])
        inst.set_history([message("user", "聊一聊")])
        inst.provider.replies[:] = ["聊了但模型没按格式输出"]
        inst.set_replies([{"text": "1"}])

        out = "\n".join(
            await inst.run_handler("writeback_annotation", _event("/写回批注"))
        )
        assert "这次没聊出能写回的内容" in out, out
        loaded, _ = plugin.load_annotations(SENDER)
        assert loaded["annotations"][0]["status"] == "open"
        assert loaded["annotations"][0]["reply"] is None


async def test_writeback_timeout_keeps_data():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        seed_annotations(plugin, [make_annotation(1)])
        inst.set_replies([{"raise": "TimeoutError"}])

        out = "\n".join(
            await inst.run_handler("writeback_annotation", _event("/写回批注"))
        )
        assert "超时" in out, f"超时应有固定文案：{out!r}"
        loaded, _ = plugin.load_annotations(SENDER)
        assert loaded["annotations"][0]["status"] == "open"


async def test_writeback_non_interactive_env_refuses_without_changes():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        seed_annotations(plugin, [make_annotation(1)])
        inst.set_session_supported(False)

        out = "\n".join(
            await inst.run_handler("writeback_annotation", _event("/写回批注"))
        )
        assert "不支持交互" in out, f"非交互环境应明确拒绝：{out!r}"
        loaded, _ = plugin.load_annotations(SENDER)
        assert loaded["annotations"][0]["status"] == "open", "非交互环境不得静默改数据"


async def test_apply_and_defer_unknown_annotation_id_return_false():
    """P2-2：id 不存在时不能返回 True（否则用户以为写回了/悬置了、实际数据没动）。"""
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        seed_annotations(plugin, [make_annotation(1)])
        draft = {
            "reply_title": "主人的回复",
            "summary": "这条不该写进任何批注",
            "speculation_marked": "",
        }
        assert plugin.apply_annotation_reply(
            SENDER, "ann_20261023_999", draft
        ) is False, "写回不存在的批注 id 应返回 False"
        assert plugin.defer_annotation(
            SENDER, "ann_20261023_999"
        ) is False, "悬置不存在的批注 id 应返回 False"
        loaded, _ = plugin.load_annotations(SENDER)
        record = loaded["annotations"][0]
        assert record["status"] == "open", "不存在的 id 不应改动任何批注"
        assert record["reply"] is None

        # 真实 id 仍需成功（防修过头）
        assert plugin.defer_annotation(SENDER, record["id"]) is True
        again, _ = plugin.load_annotations(SENDER)
        assert again["annotations"][0]["status"] == "deferred"


async def test_annotation_draft_text_partitions_confirmed_and_speculation():
    async with World(config=ann_config()) as world:
        plugin = _plugin(world)
        ann = make_annotation(1)
        draft = {
            "reply_title": "主人的回复",
            "summary": "用户确认过的事实。",
            "speculation_marked": "",
        }
        text = plugin.annotation_draft_text(ann, draft)
        for needle in ("锚于：", "批注：", "回复标题：主人的回复", "摘要：用户确认过的事实。"):
            assert needle in text, f"草稿缺少 {needle!r}：{text!r}"
        assert "其中推测" not in text, "没有推测时不应出现推测分区"
        assert "回复「确认」写回；回复「悬置」先放着；回复「取消」不写。" in text

        draft["speculation_marked"] = "也许她当时在等回信。"
        text2 = plugin.annotation_draft_text(ann, draft)
        assert "其中推测：也许她当时在等回信。" in text2, "推测必须单独分区标注"


# ---------------------------------------------------------------------------
# /查看日记 第五部分（QQ）
# ---------------------------------------------------------------------------


async def test_view_diary_zero_annotation_output_identical_to_baseline():
    """annotation_enabled=true 但零批注时，/查看日记 输出与 v0.5.5 逐字节一致。"""
    async with World(config=ann_config()) as world:
        async def scenario(inst):
            inst.plugin._save_diary(
                SENDER,
                "没有批注的一天。",
                sender_name=SENDER_NAME,
                origin="private",
                mode="full",
            )
            inst.set_session_supported(False)
            fallback = await inst.run_handler("view_diary", _event("/查看日记"))
            inst.set_session_supported(True)
            inst.set_replies([{"text": "最新"}])
            latest = await inst.run_handler("view_diary", _event("/查看日记"))
            inst.set_replies([{"text": "1"}])
            first = await inst.run_handler("view_diary", _event("/查看日记"))
            return {"fallback": fallback, "latest": latest, "first": first}

        pair = await world.run_pair(scenario, label="zero annotation view")
        value = pair.value
        for key in ("fallback", "latest", "first"):
            joined = "".join(value[key])
            assert "没有批注的一天。" in joined, f"{key} 没显示日记正文"
            assert "📖 2026-10-23 的日记：" in joined
            assert "页边的话" not in joined, f"{key} 零批注时不应出现第五部分标题"


async def test_view_diary_appends_annotation_section_at_end():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin, "正文第一行。\n正文第二行。")
        seed_annotations(
            plugin,
            [
                make_annotation(
                    1,
                    anchor="正文第一行",
                    note="这里她停了一下。",
                    created_at="2026-10-31T10:00:00",
                )
            ],
            last_title="顺手记下的",
        )
        inst.set_session_supported(False)  # 走"直接发最新一篇"的分支
        out = await inst.run_handler("view_diary", _event("/查看日记"))
        full = "\n".join(out)
        assert "📖 2026-10-23 的日记：" in full
        assert "顺手记下的" in full, f"第五部分标题缺失：{full!r}"
        assert "[1] 2026-10-31 读 · 未解" in full
        assert '锚于："正文第一行"' in full
        assert "批注：这里她停了一下。" in full
        # 批注统一收在文末：正文在第五部分之前，且没有穿插进正文四部分
        assert full.index("正文第一行") < full.index("顺手记下的")
        assert "页边批注" not in full.split("顺手记下的")[0], "批注不能穿插在正文里"


async def test_view_diary_annotation_section_tampered_warns():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin, "正文。")
        seed_annotations(plugin, [make_annotation(1)])
        tamper_annotations(plugin)
        inst.set_session_supported(False)

        out = await inst.run_handler("view_diary", _event("/查看日记"))
        full = "\n".join(out)
        assert "（页边批注验签失败，已略去）" in full, f"篡改应给一行警告：{full!r}"
        assert "她总是这么晚才开口。" not in full, "篡改时不得展示批注内容"


async def test_qq_annotation_section_is_per_day_and_switchable():
    async with World(config=ann_config()) as world:
        plugin = _plugin(world)
        seed_diary(plugin, "第一天的日记。", day=DAY)
        seed_diary(plugin, "第二天的日记。", day=OTHER_DAY)
        seed_annotations(
            plugin,
            [
                make_annotation(1, day=DAY, anchor="第一天的日记", note="第一天的批注"),
                make_annotation(
                    2, day=OTHER_DAY, anchor="第二天的日记", note="第二天的批注"
                ),
            ],
            last_title="页边的话",
        )
        day1 = plugin.qq_annotation_section_text(SENDER, DAY)
        day2 = plugin.qq_annotation_section_text(SENDER, OTHER_DAY)
        assert "第一天的批注" in day1 and "第二天的批注" not in day1, day1
        assert "第二天的批注" in day2 and "第一天的批注" not in day2, day2
        assert day1.startswith("页边的话"), f"第五部分应以标题开头：{day1!r}"
        assert "\n\n[1]" in day1
        # 没有批注的那天 / 关闭态 → ""
        assert plugin.qq_annotation_section_text(SENDER, "20260101") == ""
        plugin.config["annotation_enabled"] = False
        assert plugin.qq_annotation_section_text(SENDER, DAY) == ""


# ---------------------------------------------------------------------------
# 帮助菜单
# ---------------------------------------------------------------------------


async def test_help_menu_lists_annotation_commands():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        raw = await inst.run_handler("diary_help", _event("/日记帮助"))
        text = "\n".join(raw)
        assert "/查看批注" in text, "帮助菜单缺少 /查看批注"
        assert "/写回批注" in text, "帮助菜单缺少 /写回批注"
        assert "私聊" in text.split("/查看批注")[1][:40]
