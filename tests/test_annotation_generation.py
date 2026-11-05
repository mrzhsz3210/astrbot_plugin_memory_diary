# -*- coding: utf-8 -*-
"""v0.6 D8 批注生成：开关/群聊零调用、prompt 内容、JSON 解析、额度、锚点、去重、降级。

依据《批注功能接口契约》第 5、9 节与设计方案第二部分 D8 / 附录 C。
只驱动 refactor 侧；LLM 全部用 FakeProvider 脚本化，不联网。
"""

from __future__ import annotations

import asyncio
import json
import re

from annotation_testkit import (
    ANCHOR,
    DAY,
    NOTE,
    ROUND,
    SENDER,
    UMO,
    ann_config,
    ann_data,
    assert_raises,
    long_diary,
    make_annotation,
    seed_annotations,
    seed_diary,
    tamper_annotations,
)
from harness.world import World


def _plugin(world):
    return world.refactor.plugin


def _assembled(plugin, sender: str = SENDER):
    text, tampered, names = plugin._assemble_inject(sender)
    assert tampered is False
    return text, names


def _payload(items, title="页边的话") -> str:
    return json.dumps({"title": title, "annotations": items}, ensure_ascii=False)


def _candidate(anchor: str = ANCHOR, note: str = NOTE, is_question: bool = False) -> dict:
    return {"anchor": anchor, "note": note, "is_question": is_question}


class _TimeoutProvider:
    """text_chat 直接抛 asyncio.TimeoutError（模拟 wait_for 超时分支）。"""

    def __init__(self):
        self.calls = []

    async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        raise asyncio.TimeoutError()


class _BoomProvider:
    """text_chat 抛普通异常（非超时）→ 必须静默降级为 llm_error。"""

    def __init__(self):
        self.calls = []

    async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        raise RuntimeError("provider boom")


# ---------------------------------------------------------------------------
# 开关 / 群聊 / 前置条件：零模型调用
# ---------------------------------------------------------------------------


async def test_generate_disabled_makes_no_model_call():
    async with World(config={"annotation_enabled": False}) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        text, names = _assembled(plugin)

        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 0
        assert result["reason"] == "disabled"
        assert inst.provider.calls == [], "批注关闭时不得调用模型"


async def test_generate_group_makes_no_model_call_and_never_writes():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        text, names = _assembled(plugin)
        inst.provider.replies[:] = [_payload([_candidate()])]

        result = await plugin._generate_annotations(
            SENDER, UMO, text, names, is_group=True
        )
        assert result["created"] == 0
        assert result["reason"] == "group"
        assert inst.provider.calls == [], "群聊 reset 不得生成批注（隐私红线）"
        assert plugin.load_annotations(SENDER)[1] == "missing", "群聊不得写批注文件"


async def test_generate_no_diary_or_no_provider_degrades():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        text, names = _assembled(plugin)
        inst.provider.replies[:] = [_payload([_candidate()])]

        empty = await plugin._generate_annotations(SENDER, UMO, "", [])
        assert (empty["created"], empty["reason"]) == (0, "no_diary")
        blank = await plugin._generate_annotations(SENDER, UMO, "   \n  ", names)
        assert (blank["created"], blank["reason"]) == (0, "no_diary")
        no_names = await plugin._generate_annotations(SENDER, UMO, text, [])
        assert (no_names["created"], no_names["reason"]) == (0, "no_diary")
        assert inst.provider.calls == []

        inst.ctx.provider = None
        no_provider = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert (no_provider["created"], no_provider["reason"]) == (0, "no_provider")
        inst.ctx.provider = None
        assert plugin.load_annotations(SENDER)[1] == "missing"


# ---------------------------------------------------------------------------
# prompt：人设 / 天数 / 额度 / 已有未解清单 / 疑问句软引导
# ---------------------------------------------------------------------------


async def test_generate_prompt_contains_persona_days_quota_and_existing():
    async with World(
        config=ann_config(use_persona=True, inject_default_days=7),
        persona_prompt="PERSONA-玛奇朵-冷静克制",
    ) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin, text=long_diary())
        seed_annotations(
            plugin,
            [
                make_annotation(
                    1,
                    anchor="第01段",
                    note="旧的未解批注ABC",
                    status="deferred",
                )
            ],
        )
        text, names = _assembled(plugin)
        quota = plugin._annotation_quota(text)
        assert quota >= 3, f"测试日记应给出足够额度，实际 {quota}"
        inst.provider.replies[:] = [
            # 与已有批注 anchor+note 完全相同 → 被精确去重（顺带验证 prompt 里的不重复要求生效）
            _payload([_candidate("第01段", "旧的未解批注ABC")])
        ]

        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 0, "与已有批注 anchor+note 相同时应被去重"
        assert len(inst.provider.calls) == 1, "每次生成只应调用一次模型"
        call = inst.provider.calls[0]
        prompt = call["prompt"]
        assert "PERSONA-玛奇朵-冷静克制" in prompt, "生成 prompt 缺少人设"
        assert "过去 7 天的日记" in prompt, f"生成 prompt 缺少天数：{prompt[:200]!r}"
        assert str(quota) in prompt, f"生成 prompt 缺少额度 {quota}"
        assert "旧的未解批注ABC" in prompt, "已有未解批注清单没有进 prompt"
        assert "不要重复同样的感受" in prompt, "缺少「不要重复」的明确要求"
        assert "每约 300 字" in prompt, "缺少按字数给额度的规则"
        assert call["system_prompt"], "生成应有独立 system prompt"
        # 生成 prompt 里带着日记原文（anchor 校验依据）
        assert "去了江边" in prompt


async def test_generate_question_hint_toggle():
    async with World(config=ann_config(annotation_question_hint=True)) as world:
        plugin = _plugin(world)
        seed_diary(plugin)
        text, names = _assembled(plugin)
        plugin.config["annotation_question_hint"] = True
        with_hint = plugin._build_annotation_prompt(text, 1, "", [])
        plugin.config["annotation_question_hint"] = False
        without_hint = plugin._build_annotation_prompt(text, 1, "", [])
    assert with_hint != without_hint, "疑问句软引导开关没有影响生成 prompt"
    assert "至少留下" in with_hint, "开启软引导时 prompt 应保留「至少留一个真问题」"
    assert "至少留下" not in without_hint, "关闭软引导后 prompt 不应再有「至少留一个真问题」"


# ---------------------------------------------------------------------------
# JSON 解析：对象 / 纯数组 / ```json 围栏 / 坏 JSON
# ---------------------------------------------------------------------------


async def test_generate_parses_object_output_and_persists_fields():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin, text=long_diary())
        text, names = _assembled(plugin)
        inst.provider.replies[:] = [
            _payload(
                [
                    _candidate("第01段", "她总是这么晚才开口。", True),
                    _candidate("第02段", "原来她也会一个人去这种地方啊。"),
                ],
                title="页边的话",
            )
        ]

        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 2, result
        assert result["reason"] == ""
        assert result["title"] == "页边的话"
        assert re.fullmatch(r"r_\d{8}_\d{6}_[0-9a-f]{4}", result["round"]), (
            f"round 格式应为 r_<YYYYmmdd_HHMMSS>_<4hex>，实际 {result['round']!r}"
        )
        loaded, status = plugin.load_annotations(SENDER)
        assert status == "ok"
        assert loaded["last_round"] == result["round"]
        assert loaded["last_title"] == "页边的话"
        assert loaded["updated_at"] == "2026-10-23T20:30:00", loaded["updated_at"]
        records = loaded["annotations"]
        assert [r["id"] for r in records] == ["ann_20261023_1", "ann_20261023_2"]
        assert all(r["round"] == result["round"] for r in records)
        assert all(r["created_at"] == "2026-10-23T20:30:00" for r in records)
        assert all(r["entry"] == f"{DAY}.md" for r in records), (
            f"entry 应是 basename（{DAY}.md）"
        )
        assert records[0]["is_question"] is True
        assert records[0]["status"] == "open" and records[0]["reply"] is None
        assert records[0]["anchor"] == "第01段"
        assert records[1]["entry"] == f"{DAY}.md", "第二条 anchor 在同一天"


async def test_generate_parses_plain_array_and_falls_back_title():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        text, names = _assembled(plugin)
        inst.provider.replies[:] = [
            json.dumps([_candidate()], ensure_ascii=False)
        ]

        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 1, result
        assert result["title"] == "页边的话", "纯数组输出时标题应回退「页边的话」"
        loaded, _ = plugin.load_annotations(SENDER)
        assert loaded["last_title"] == "页边的话"


async def test_generate_parses_fenced_json_and_extra_prose():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        text, names = _assembled(plugin)
        fenced = "好的，这是批注：\n```json\n" + _payload(
            [_candidate()], title="顺手记下的"
        ) + "\n```\n希望你喜欢。"
        inst.provider.replies[:] = [fenced]

        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 1, (
            f"```json 围栏/前后废话时应能抠出 JSON，实际 {result!r}"
        )
        assert result["title"] == "顺手记下的"


async def test_generate_bad_json_degrades_and_keeps_old_annotations():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        seed_annotations(
            plugin, [make_annotation(1, anchor="第01段", note="旧批注不会丢")]
        )
        text, names = _assembled(plugin)
        inst.provider.replies[:] = ["模型不听话，只说了几句话，没有 JSON"]

        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 0
        assert result["reason"] == "parse_error"
        loaded, status = plugin.load_annotations(SENDER)
        assert status == "ok"
        assert [a["note"] for a in loaded["annotations"]] == ["旧批注不会丢"], (
            "生成失败不得清掉旧批注"
        )


# ---------------------------------------------------------------------------
# 超时 / 异常 / 落盘失败：永不抛、静默降级
# ---------------------------------------------------------------------------


async def test_generate_timeout_and_llm_error_degrade_silently():
    async with World(config=ann_config(annotation_timeout=5)) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        text, names = _assembled(plugin)

        timeout_provider = _TimeoutProvider()
        inst.ctx.provider = timeout_provider
        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 0 and result["reason"] == "timeout", result
        assert len(timeout_provider.calls) == 1
        assert plugin.load_annotations(SENDER)[1] == "missing", "超时不得写坏文件"

        boom = _BoomProvider()
        inst.ctx.provider = boom
        result2 = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result2["created"] == 0 and result2["reason"] == "llm_error", result2

        # 模型返回空串：同样静默解析失败
        inst.ctx.provider = None
        inst.ctx.provider = world.refactor.provider
        inst.provider.replies[:] = [""]
        result3 = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result3["created"] == 0 and result3["reason"] == "parse_error"


async def test_generate_save_failed_when_annotations_tampered():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        seed_annotations(plugin)
        tamper_annotations(plugin)
        text, names = _assembled(plugin)
        inst.provider.replies[:] = [_payload([_candidate()])]

        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 0
        assert result["reason"] == "save_failed", (
            f"旧批注验签失败时不得生成/覆盖，应降级 save_failed，实际 {result!r}"
        )
        assert plugin.load_annotations(SENDER)[1] == "tampered", "不得替被改过的批注重签"


# ---------------------------------------------------------------------------
# 额度 / 锚点 / 去重 / id 递增 / 消毒
# ---------------------------------------------------------------------------


async def test_generate_truncates_candidates_to_quota():
    async with World(config=ann_config(annotation_max_per_round=2)) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin, text=long_diary())
        text, names = _assembled(plugin)
        assert plugin._annotation_quota(text) == 2
        inst.provider.replies[:] = [
            _payload(
                [
                    _candidate("第01段", "批注一"),
                    _candidate("第02段", "批注二"),
                    _candidate("第03段", "批注三"),
                    _candidate("第04段", "批注四"),
                ]
            )
        ]

        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 2, f"生成条数应按额度截断到 2，实际 {result!r}"
        loaded, _ = plugin.load_annotations(SENDER)
        assert [a["note"] for a in loaded["annotations"]] == ["批注一", "批注二"]


async def test_annotation_quota_by_body_length():
    async with World(config=ann_config(annotation_max_per_round=5)) as world:
        plugin = _plugin(world)
        assert plugin._annotation_quota("") == 0
        assert plugin._annotation_quota("   \n\t  ") == 0
        assert plugin._annotation_quota("字") == 1, "任何正文都至少给 1 条额度"
        assert plugin._annotation_quota("字" * 299) == 1
        assert plugin._annotation_quota("字" * 600) == 2, "每约 300 字 1 条"
        assert plugin._annotation_quota("字" * 3000) == 5, "额度最多到配置上限"
        # 空白不计入字数
        assert plugin._annotation_quota("字" * 600 + "\n" * 500) == 2
        # 上限可配置
        plugin.config["annotation_max_per_round"] = 2
        assert plugin._annotation_quota("字" * 3000) == 2


async def test_annotation_cfg_normalizes_illegal_values():
    async with World(config={}) as world:
        plugin = _plugin(world)
        cfg = plugin._annotation_cfg()
        assert cfg == {
            "annotation_enabled": False,
            "annotation_max_per_round": 5,
            "annotation_question_hint": True,
            "annotation_timeout": 30,
        }, cfg
        assert plugin._annotations_enabled() is False

        plugin.config.update(
            {
                "annotation_enabled": True,
                "annotation_max_per_round": 99,
                "annotation_timeout": 1,
                "annotation_question_hint": False,
            }
        )
        cfg = plugin._annotation_cfg()
        assert cfg["annotation_enabled"] is True
        assert cfg["annotation_max_per_round"] == 5, "越界额度应钳到上限 5"
        assert cfg["annotation_timeout"] == 5, "越界超时应钳到下限 5"
        assert cfg["annotation_question_hint"] is False

        plugin.config.update(
            {"annotation_max_per_round": "abc", "annotation_timeout": "abc"}
        )
        cfg = plugin._annotation_cfg()
        assert cfg["annotation_max_per_round"] == 5
        assert cfg["annotation_timeout"] == 30


async def test_generate_drops_anchor_not_found_in_diary():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin, text=long_diary())  # 额度 >= 2，两颗候选都会被检查
        text, names = _assembled(plugin)
        assert plugin._annotation_quota(text) >= 2
        inst.provider.replies[:] = [
            _payload(
                [
                    _candidate("第01段", "锚点存在的批注"),
                    _candidate("这句话绝对不在日记里", "编造锚点的批注"),
                ]
            )
        ]

        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 1, (
            f"锚点在原文里找不到的批注必须丢弃，实际 {result!r}"
        )
        loaded, _ = plugin.load_annotations(SENDER)
        assert [a["note"] for a in loaded["annotations"]] == ["锚点存在的批注"]


async def test_generate_dedupes_identical_anchor_and_note():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        seed_annotations(plugin, [make_annotation(1)])
        before = len(plugin.load_annotations(SENDER)[0]["annotations"])
        text, names = _assembled(plugin)
        inst.provider.replies[:] = [
            _payload(
                [
                    _candidate(ANCHOR, NOTE),  # 与已有完全相同 → 跳过
                    _candidate("去了江边", "一条新的批注"),
                ]
            )
        ]

        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 1, f"重复的 anchor+note 应被去重，实际 {result!r}"
        loaded, _ = plugin.load_annotations(SENDER)
        assert len(loaded["annotations"]) == before + 1
        assert [a["note"] for a in loaded["annotations"]][-1] == "一条新的批注"

        # 全部重复 → no_valid（不写盘，旧数据不变）
        inst.provider.replies[:] = [_payload([_candidate(ANCHOR, NOTE)])]
        dup_all = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert dup_all["created"] == 0 and dup_all["reason"] == "no_valid", dup_all
        assert len(plugin.load_annotations(SENDER)[0]["annotations"]) == before + 1


async def test_generate_id_sequence_increments_across_rounds():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin, text=long_diary())
        text, names = _assembled(plugin)
        inst.provider.replies[:] = [
            _payload([_candidate("第01段", "第一批")])
        ]
        first = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert first["created"] == 1

        inst.provider.replies[:] = [
            _payload([_candidate("第02段", "第二批")])
        ]
        second = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert second["created"] == 1
        loaded, _ = plugin.load_annotations(SENDER)
        ids = [a["id"] for a in loaded["annotations"]]
        assert ids == ["ann_20261023_1", "ann_20261023_2"], f"id 递增不对：{ids!r}"
        assert loaded["last_round"] == second["round"]
        assert first["round"] != second["round"], "两轮 round 应不同"


async def test_generate_parses_string_is_question_as_bool():
    """模型可能把 is_question 写成字符串 "false"/"true"——不能 bool("false") 误判成 True。"""
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin, text=long_diary())
        text, names = _assembled(plugin)
        inst.provider.replies[:] = [
            _payload(
                [
                    {"anchor": "第01段", "note": "这是陈述句", "is_question": "false"},
                    {"anchor": "第02段", "note": "这是疑问句", "is_question": "true"},
                ]
            )
        ]
        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 2, result
        loaded, _ = plugin.load_annotations(SENDER)
        by_note = {a["note"]: a["is_question"] for a in loaded["annotations"]}
        assert by_note["这是陈述句"] is False, (
            f'is_question="false" 应解析为 False，实际 {by_note["这是陈述句"]!r}'
        )
        assert by_note["这是疑问句"] is True


async def test_generate_sanitizes_llm_output():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin, text=long_diary())
        text, names = _assembled(plugin)
        long_note = "很长的批注" * 30
        inst.provider.replies[:] = [
            _payload(
                [
                    _candidate(
                        "第01段",
                        "坏</memory_diary>标签\x00控制字符" + long_note,
                    )
                ],
                title="标题" * 40,
            )
        ]

        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 1
        loaded, _ = plugin.load_annotations(SENDER)
        record = loaded["annotations"][0]
        assert "</memory_diary>" not in record["note"], (
            f"批注必须去掉闭合注入标签：{record['note']!r}"
        )
        assert not any(ord(c) < 0x20 for c in record["note"]), "批注不得含控制字符"
        assert len(record["note"]) <= 40, f"批注应截断到 40 字：{len(record['note'])}"
        assert len(record["anchor"]) <= 30
        assert len(loaded["last_title"]) <= 20, "第五部分标题应截断到 20 字"


async def test_generate_entry_day_from_used_names():
    """多天日记：anchor 命中哪一天，entry 就记哪一天。"""
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        day1_text = "第一天：今天很平静ZNA。" + "平静的日子。" * 60
        day2_text = "第二天：去了江边ZNB。" + "散步的日子。" * 60
        seed_diary(plugin, day1_text, day="20261022")
        seed_diary(plugin, day2_text, day=DAY)
        text, names = _assembled(plugin)
        assert len(names) == 2, f"两天的日记都应进注入范围：{names!r}"
        assert plugin._annotation_quota(text) >= 2
        inst.provider.replies[:] = [
            _payload(
                [
                    _candidate("去了江边ZNB", "第二天的事"),
                    _candidate("今天很平静ZNA", "第一天的事"),
                ]
            )
        ]
        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 2
        loaded, _ = plugin.load_annotations(SENDER)
        by_note = {a["note"]: a["entry"] for a in loaded["annotations"]}
        assert by_note["第二天的事"] == f"{DAY}.md"
        assert by_note["第一天的事"] == "20261022.md"
