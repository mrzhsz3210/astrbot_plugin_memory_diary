# -*- coding: utf-8 -*-
"""v0.6.2 审查修复 · 适配层回归（Lead 负责的 task-9 条目）。

覆盖：
- P2-11：`_command_arg` / `_strip_invisible` 剥掉消息尾部 `[MSG_ID:…]` 机器标记，
  带参指令（/日记范围、/写日记 完整 …）不再被判"无法识别参数"；
- P3-5：`_command_arg` 只剥真正的 @提及，正文里的 `@`（如"黑发@红色发带"）保留；
- P2-8：批注 id 的日期段取**生成当天**（不是锚点所在天），撞号追加 `_<4位hex>`；
- P3-11：token 估算把 assistant 消息的 `tool_calls` 计入上下文。

只驱动 refactor 侧；数据全在 World 的临时目录里。
"""

from __future__ import annotations

import importlib
import re
import sys

from annotation_testkit import (
    ANCHOR,
    DAY,
    DIARY_TEXT,
    NOTE,
    OTHER_DAY,
    SENDER,
    UMO,
    ann_config,
    seed_diary,
)
from harness import env
from harness.fakes import at_me, make_event
from harness.world import World

if str(env.REFACTOR_DIR) not in sys.path:
    sys.path.insert(0, str(env.REFACTOR_DIR))
est = importlib.import_module("memory_diary.token_estimator")


def _event(text: str, **kwargs):
    return make_event(text, **kwargs)


def _payload(items, title="页边的话") -> str:
    import json

    return json.dumps({"title": title, "annotations": items}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# P2-11：尾部机器标记（qq_tools show_message_id 追加 [MSG_ID:…]）
# ---------------------------------------------------------------------------


async def test_command_arg_strips_trailing_machine_decoration():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        ev = _event("/日记范围 最近7天 [MSG_ID:-2037476869]")
        assert plugin._command_arg(ev) == "最近7天"

        # 连续两个标记也要剥干净（qq_tools 可能同时挂多个）
        ev2 = _event("/写日记 完整 [MSG_ID:1][MSG_ID:2]")
        assert plugin._command_arg(ev2) == "完整"


async def test_strip_invisible_keeps_plain_and_non_marker_brackets():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        # 普通参数逐字节不变
        assert plugin._strip_invisible("最近7天") == "最近7天"
        # 零宽字符照旧剥掉
        assert plugin._strip_invisible("最近\u200b7天") == "最近7天"
        # 不是"尾部机器标记"形状的方括号内容不动（避免误伤正文）
        assert plugin._strip_invisible("备注 [不是标记]") == "备注 [不是标记]"
        assert plugin._strip_invisible("a [note] b") == "a [note] b"


async def test_decorated_scope_command_really_works():
    """带尾部机器标记的 /日记范围 必须真的生效（不只是 `_command_arg` 单测）。"""
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        ev = _event("/日记范围 最近7天 [MSG_ID:-2037476869]")
        await inst.run_handler("diary_scope", ev)
        assert plugin._load_pref(SENDER) == "days:7"


# ---------------------------------------------------------------------------
# P3-5：@ 提及只剥"真正的"，正文里的 @ 保留
# ---------------------------------------------------------------------------


async def test_command_arg_keeps_inline_at_in_body():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        ev = _event("/形象 修改 黑发@红色发带")
        assert plugin._command_arg(ev) == "修改 黑发@红色发带"


async def test_command_arg_strips_real_at_mention_forms():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        # 1) 开头独立 @提及（@昵称 后面跟参数）
        ev = _event("/注入日记 @小深 最新")
        assert plugin._command_arg(ev) == "最新"
        # 2) 消息链里的 At 组件 → 剥掉其 QQ 号的文本形式
        ev2 = _event("/注入日记 @12345678 最新", messages=[at_me("12345678")])
        assert plugin._command_arg(ev2) == "最新"


# ---------------------------------------------------------------------------
# P2-8：批注 id 日期段取生成当天 + 撞号加后缀
# ---------------------------------------------------------------------------


async def test_annotation_id_uses_generation_day_not_anchor_day():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = inst.plugin
        # 日记在「昨天」，生成发生在「今天」（World 固定时钟 2026-10-23）
        seed_diary(plugin, text=DIARY_TEXT, day=OTHER_DAY)
        text, tampered, names = plugin._assemble_inject(SENDER)
        assert tampered is False
        assert names, "种子日记应出现在注入篇目里"
        inst.provider.replies[:] = [
            _payload([{"anchor": ANCHOR, "note": NOTE, "is_question": False}])
        ]

        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 1, f"应生成 1 条批注，实际 {result}"
        data, status = plugin.load_annotations(SENDER)
        assert status == "ok"
        items = data["annotations"]
        assert len(items) == 1
        item = items[0]
        # 日期段 = 生成当天（20261023），不是锚点所在的 20261022
        assert item["id"] == f"ann_{DAY}_1", (
            f"批注 id 的日期段应取生成当天 {DAY}，实际 {item['id']}"
        )
        # entry 仍然指向锚点所在的日记
        assert item["entry"] == f"{OTHER_DAY}.md"


async def test_annotation_id_dedup_appends_hex_suffix():
    async with World(config=ann_config()) as world:
        plugin = world.refactor.plugin
        cand = plugin._unique_annotation_id(f"ann_{DAY}_1", {f"ann_{DAY}_1"})
        assert cand != f"ann_{DAY}_1"
        assert re.fullmatch(rf"ann_{DAY}_1_[0-9a-f]{{4}}", cand), cand
        assert cand not in {f"ann_{DAY}_1"}


async def test_next_annotation_seq_parses_hex_suffix_ids():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        data = {"annotations": [{"id": f"ann_{DAY}_3_ab12"}]}
        assert plugin._next_annotation_seq(data, DAY) == 4
        # 别的日期段不计入当天序号
        assert plugin._next_annotation_seq(data, OTHER_DAY) == 1


# ---------------------------------------------------------------------------
# P3-11：assistant 消息的 tool_calls 计入上下文
# ---------------------------------------------------------------------------


def _tool_calls(n_chars: int) -> list:
    return [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "search_web",
                "arguments": '{"q": "' + "a" * n_chars + '"}',
            },
        }
    ]


def test_estimate_messages_counts_tool_calls():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": _tool_calls(4000)},
    ]
    total = est.estimate_messages(messages)
    # 4000 个 ASCII 字符 ≈ 1000 token（ASCII_WEIGHT=0.25）+ 消息开销
    assert total >= 900, f"tool_calls 没有被计入：{total}"


def test_estimate_request_counts_contexts_tool_calls():

    class _Req:
        system_prompt = ""
        prompt = ""
        contexts = [
            {"role": "assistant", "content": "", "tool_calls": _tool_calls(4000)}
        ]
        extra_user_content_parts = []
        func_tool = None
        tool_calls_result = None

    baseline = est.estimate_request(
        type(
            "_EmptyReq",
            (),
            {
                "system_prompt": "",
                "prompt": "",
                "contexts": [],
                "extra_user_content_parts": [],
                "func_tool": None,
                "tool_calls_result": None,
            },
        )()
    )
    total = est.estimate_request(_Req())
    assert total - baseline >= 900, f"contexts 里的 tool_calls 未被计入：{total}"
