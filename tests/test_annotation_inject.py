# -*- coding: utf-8 -*-
"""v0.6 D8 批注注入：格式 / 只注入 open+deferred / 上限与疑问句优先 / 降级 / 群聊 / 关闭态。

依据《批注功能接口契约》第 5、6.1、9 节与设计方案第二部分第 5 节（token 预算）。
注入块格式是契约里写死、要求"测试按此断言"的，所以这里逐行比对；
main.py 侧的行为通过 on_llm_request 的可观察结果驱动。
"""

from __future__ import annotations

import json

from astrbot.api.provider import ProviderRequest

from annotation_testkit import (
    DAY,
    SENDER,
    SENDER_NAME,
    UMO,
    ann_config,
    ann_data,
    make_annotation,
    long_diary,
    seed_annotations,
    seed_diary,
    tamper_annotations,
)
from harness.fakes import make_event
from harness.world import World

GROUP_UMO = "aiocqhttp:GroupMessage:88888"


def _plugin(world):
    return world.refactor.plugin


def _event(text: str = "你好", *, group_id: str = ""):
    return make_event(
        text,
        sender_id=SENDER,
        sender_name=SENDER_NAME,
        group_id=group_id,
        umo=UMO if not group_id else GROUP_UMO,
    )


def _request(prompt: str = "你好", *, contexts=None, system_prompt: str = "SYS"):
    return ProviderRequest(
        prompt=prompt,
        contexts=list(contexts)
        if contexts is not None
        else [{"role": "user", "content": "你好"}],
        system_prompt=system_prompt,
    )


def _extra_texts(req) -> list[str]:
    return [getattr(part, "text", str(part)) for part in req.extra_user_content_parts]


def _all_text(req) -> str:
    return "\n".join(_extra_texts(req)) + "\n" + (req.system_prompt or "")


def _norm_quotes(text: str) -> str:
    """统一中英文引号，避免契约排版字符差异影响逐行断言。"""
    return (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("「", '"')
        .replace("」", '"')
    )


# ---------------------------------------------------------------------------
# 注入块格式（契约第 5 节写死）
# ---------------------------------------------------------------------------


async def test_inject_block_respects_remaining_budget_char_cap():
    """P2-1：批注块按调用方算出的剩余预算裁剪；预算不足（<100）时整块跳过。"""
    async with World(config=ann_config(inject_max_chars=2000)) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin, "字" * 1200)
        seed_annotations(
            plugin,
            [
                make_annotation(
                    i,
                    anchor="字",
                    note="批注内容" * 20,
                    created_at=f"2026-10-{i:02d}T10:00:00",
                )
                for i in range(1, 6)
            ],
        )
        text, tampered, names = plugin._assemble_inject(SENDER)
        assert tampered is False and text
        cap = min(1500, max(0, 2000 - len(text) - 400))
        assert 100 <= cap < 1500, (
            f"测试构造的剩余预算应落在 [100,1500)，实际 cap={cap}（日记 {len(text)} 字）"
        )
        block = plugin._annotation_inject_block(SENDER, char_cap=cap)
        assert block, "还有剩余预算时应注入批注块"
        assert "【关于批注的纪律】" in block
        body = block.split("【关于批注的纪律】")[0].rstrip("\n")
        assert 0 < len(body) <= cap, (
            f"批注块未按剩余预算裁剪：body={len(body)} cap={cap}"
        )

        # 端到端：main.py 的 _annotation_block_for_inject 按日记已占字数自己算 cap
        e2e = await plugin._annotation_block_for_inject(
            SENDER, UMO, False, text, names, is_new_generation=False
        )
        e2e_body = e2e.split("【关于批注的纪律】")[0].rstrip("\n")
        assert 0 < len(e2e_body) <= cap, (len(e2e_body), cap)
        assert len(text) + len(e2e_body) <= 2000, (
            f"日记+批注块超出 inject_max_chars：{len(text)}+{len(e2e_body)}"
        )

        # 日记已把预算占满（cap<100）→ 本轮不注入批注（批注仍可 /查看批注）
        plugin.config["inject_max_chars"] = 1200
        skipped = await plugin._annotation_block_for_inject(
            SENDER, UMO, False, text, names, is_new_generation=False
        )
        assert skipped == "", f"预算不足时应整块跳过，实际 {skipped[:80]!r}"
        assert inst.provider.calls == [], "预算裁剪不应额外调用模型"


async def test_inject_block_matches_contract_format_and_rules():
    async with World(config=ann_config()) as world:
        plugin = _plugin(world)
        seed_annotations(
            plugin,
            [
                make_annotation(
                    1,
                    anchor="那段话没头没尾的",
                    note="这事的前因后果是什么？",
                    is_question=True,
                    created_at="2026-10-31T19:33:00",
                )
            ],
            last_title="页边的话",
        )

        block = plugin._annotation_inject_block(SENDER)
        assert block, "有 open 批注时应返回注入块"
        norm = _norm_quotes(block)
        assert norm.startswith('【页边批注 · 页边的话】（你上一轮读这些日记时在页边留下的字）'), (
            f"注入块标题行不符合契约：{block[:80]!r}"
        )
        # 条目三要素：读的日期 / 锚定的日记 / 锚点原文 / 批注（含状态）
        assert "〔页边批注 2026-10-31 读〕锚于 2026-10-23 日记" in norm, block
        assert '"那段话没头没尾的"' in norm
        assert '"这事的前因后果是什么？（未解）"' in norm, (
            f"open 批注应带「（未解）」状态标记：{block!r}"
        )

        # 纪律文案（契约第 5 节固定文案）
        for line in (
            "【关于批注的纪律】",
            "· 批注是你自己上一轮的想法，不是用户说过的话。",
            "· 不要主动提起批注、不要催促用户去看、不要报"
            '"还有几条没看"；用户不提就当它不存在。',
            "· 用户用编号或原文引用问起时，照着上面的批注内容自然展开。",
            "· 讨论收敛后可以提议写回，但必须由插件指令完成、用户明确确认才算数。",
            "· 写回是一小段"
            '"我们聊明白了什么"，不是对话转录；用户确认的事实与你的推测必须分开说。',
            "· 绝不改写日记原文。",
        ):
            assert _norm_quotes(line) in norm, f"注入纪律缺少一行：{line!r}"


async def test_inject_block_title_falls_back_to_default():
    async with World(config=ann_config()) as world:
        plugin = _plugin(world)
        seed_annotations(plugin, [make_annotation(1)], last_title="")
        block = plugin._annotation_inject_block(SENDER)
        assert block.startswith("【页边批注 · 页边的话】"), (
            f"last_title 为空时应回退「页边的话」：{block[:60]!r}"
        )


async def test_inject_block_feeds_into_prompt_with_diary():
    """批注块必须随日记一起进上下文（不是单独一条消息）。"""
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        seed_annotations(plugin, [make_annotation(1)])
        inst.provider.replies[:] = [""]

        req = _request(contexts=[{"role": "user", "content": "你好"}])
        await plugin.on_llm_request(_event(), req)
        text = _all_text(req)
        assert "<memory_diary>" in text
        assert "【页边批注" in text, "批注块没有随日记一起注入"
        # 批注在日记注入文本内部、纪律文案之前
        assert text.index("【页边批注") > text.index("<memory_diary>")


# ---------------------------------------------------------------------------
# 只注入 open/deferred；上限与「最新疑问句优先」；按时间升序
# ---------------------------------------------------------------------------


async def test_inject_only_open_and_deferred():
    async with World(config=ann_config()) as world:
        plugin = _plugin(world)
        seed_annotations(
            plugin,
            [
                make_annotation(1, note="未解的", status="open", created_at="2026-10-31T10:00:00"),
                make_annotation(2, note="悬置的", status="deferred", created_at="2026-10-31T11:00:00"),
                make_annotation(
                    3,
                    note="已解的带摘要XYZ",
                    status="resolved",
                    created_at="2026-10-31T12:00:00",
                ),
            ],
        )
        block = plugin._annotation_inject_block(SENDER)
        assert "未解的" in block and "悬置的" in block
        assert "已解的带摘要XYZ" not in block, "已解批注不应进注入（省 token）"
        assert '"悬置的（悬置）"' in _norm_quotes(block), "deferred 应显示「（悬置）」"


async def test_inject_cap_prefers_newest_questions_and_sorts_ascending():
    async with World(config=ann_config(annotation_max_per_round=2)) as world:
        plugin = _plugin(world)
        seed_annotations(
            plugin,
            [
                make_annotation(
                    1,
                    anchor="第01段",
                    note="最旧的陈述",
                    created_at="2026-10-01T09:00:00",
                ),
                make_annotation(
                    2,
                    anchor="第02段",
                    note="较早的疑问",
                    is_question=True,
                    created_at="2026-10-02T09:00:00",
                ),
                make_annotation(
                    3,
                    anchor="第03段",
                    note="最新的疑问",
                    is_question=True,
                    created_at="2026-10-03T09:00:00",
                ),
                make_annotation(
                    4,
                    anchor="第04段",
                    note="最新的陈述",
                    created_at="2026-10-04T09:00:00",
                ),
            ],
        )
        block = plugin._annotation_inject_block(SENDER)
        # 上限 2：优先保住最新的两条疑问句
        assert "最新的疑问" in block, f"超限应优先保留最新疑问句：{block!r}"
        assert "较早的疑问" in block, f"第二条额度应留给疑问句：{block!r}"
        assert "最旧的陈述" not in block
        assert "最新的陈述" not in block
        # 渲染按 created_at 升序（较早的疑问在前）
        assert block.index("较早的疑问") < block.index("最新的疑问"), (
            "注入块应按 created_at 升序渲染"
        )


async def test_inject_ascii_ordering_between_annotations():
    """条与条之间空行（契约示例格式）。"""
    async with World(config=ann_config()) as world:
        plugin = _plugin(world)
        seed_annotations(
            plugin,
            [
                make_annotation(1, note="第一条", created_at="2026-10-31T09:00:00"),
                make_annotation(2, note="第二条", created_at="2026-10-31T10:00:00"),
            ],
        )
        block = plugin._annotation_inject_block(SENDER)
        assert "第一条" in block and "第二条" in block
        assert "\n\n〔页边批注" in block, "条与条之间应空行分隔"


async def test_inject_char_cap_keeps_at_least_one():
    """超长批注触发 1500 字上限时从最旧丢、至少留一条，绝不返回半截空块。"""
    async with World(config=ann_config()) as world:
        plugin = _plugin(world)
        records = [
            make_annotation(
                i,
                anchor=f"第{i:02d}段",
                note=f"第{i}条" + "很长的批注内容" * 8,
                created_at=f"2026-10-{i:02d}T09:00:00",
            )
            for i in range(1, 10)
        ]
        seed_annotations(plugin, records)
        block = plugin._annotation_inject_block(SENDER)
        body = block.split("【关于批注的纪律】")[0].rstrip("\n")
        assert body, "超长批注时不应返回空块"
        assert len(body) <= 1500, f"注入块超出 1500 字预算保险丝：{len(body)}"
        assert "第9条" in block, "至少应保留最新一条"


# ---------------------------------------------------------------------------
# 关闭 / tampered / 群聊
# ---------------------------------------------------------------------------


async def test_inject_block_empty_when_disabled_but_annotations_exist():
    async with World(config={"annotation_enabled": False}) as world:
        plugin = _plugin(world)
        seed_diary(plugin)
        # 直接绕过开关写入批注（模拟"曾经开过、现在关了"）
        plugin.config["annotation_enabled"] = True
        seed_annotations(plugin, [make_annotation(1)])
        plugin.config["annotation_enabled"] = False

        assert plugin._annotation_inject_block(SENDER) == "", "关闭态不得注入批注"
        assert plugin._annotation_inject_block("someone") == ""


async def test_inject_block_empty_when_annotations_tampered():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        seed_annotations(plugin, [make_annotation(1)])
        tamper_annotations(plugin)
        inst.provider.replies[:] = [""]

        assert plugin._annotation_inject_block(SENDER) == "", (
            "批注验签失败时不得注入（不展示被改过的内容）"
        )
        # 日记注入照常，不因批注异常中断
        req = _request(contexts=[{"role": "user", "content": "你好"}])
        await plugin.on_llm_request(_event(), req)
        text = _all_text(req)
        assert "<memory_diary>" in text, "批注 tampered 不应影响日记注入"
        assert "【页边批注" not in text


async def test_inject_block_missing_annotations_is_silent():
    async with World(config=ann_config()) as world:
        plugin = _plugin(world)
        seed_diary(plugin)
        assert plugin._annotation_inject_block(SENDER) == ""
        assert plugin._annotation_inject_block("no-such-user") == ""


# ---------------------------------------------------------------------------
# main.py 注入钩子：关闭态逐字节 / 群聊 / 生成代际
# ---------------------------------------------------------------------------


async def test_annotation_disabled_injection_is_byte_identical_to_baseline():
    """annotation_enabled=false：注入文本、调用次数与 baseline 逐字节一致（差分）。"""
    async with World(config={"annotation_enabled": False}) as world:
        async def scenario(inst):
            inst.plugin._save_diary(
                SENDER,
                "没有批注的日记。",
                sender_name=SENDER_NAME,
                origin="private",
                mode="full",
            )
            first = _request(contexts=[{"role": "user", "content": "你好"}])
            await inst.plugin.on_llm_request(_event(), first)
            second = _request(contexts=[{"role": "user", "content": "你好"}])
            await inst.plugin.on_llm_request(_event(), second)
            return {
                "first_extra": _extra_texts(first),
                "first_system": first.system_prompt,
                "second_extra": _extra_texts(second),
                "provider_calls": len(inst.provider.calls),
            }

        pair = await world.run_pair(scenario, label="annotation disabled inject")
        value = pair.value
        assert len(value["first_extra"]) == 1
        assert "【页边批注" not in value["first_extra"][0], (
            "关闭态注入文本里出现了批注块"
        )
        assert value["second_extra"] == []
        assert value["provider_calls"] == 0, "关闭态不得为批注额外调用模型"


async def test_group_injection_never_generates_or_injects_annotations():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        # 写一篇"写于群聊"的日记，保证群聊注入过滤后仍有正文可注入
        plugin._save_diary(
            SENDER,
            "群聊里写的日记。",
            sender_name=SENDER_NAME,
            entry_name_hint=DAY,
            origin="group",
            mode="full",
        )
        inst.provider.replies[:] = [_annotation_payload()]

        req = _request(contexts=[{"role": "user", "content": "你好"}])
        await plugin.on_llm_request(_event(group_id="88888"), req)
        text = _all_text(req)
        assert "<memory_diary>" in text, "群聊日记注入逻辑应保持不变"
        assert "【页边批注" not in text, "群聊不得注入批注（隐私红线）"
        assert inst.provider.calls == [], "群聊 reset 不得生成批注"
        assert plugin.load_annotations(SENDER)[1] == "missing"


async def test_reset_first_inject_generates_once_per_generation():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        inst.provider.replies[:] = [
            _annotation_payload(),
            _annotation_payload(anchor="去了江边", note="第二轮的新批注"),
        ]

        # 新会话第一次请求：同步生成 + 注入
        first = _request(contexts=[{"role": "user", "content": "你好"}])
        await plugin.on_llm_request(_event(), first)
        assert len(inst.provider.calls) == 1, "reset 后首次注入应同步生成一次批注"
        assert "【页边批注" in _all_text(first)
        loaded, status = plugin.load_annotations(SENDER)
        assert status == "ok" and len(loaded["annotations"]) == 1

        # 同一代际的第二次请求：不重复生成、也不再注入（临时模式只注入一次）
        second = _request(contexts=[{"role": "user", "content": "你好"}])
        await plugin.on_llm_request(_event(), second)
        assert len(inst.provider.calls) == 1, "同一代际多轮不得重复生成批注"
        assert _extra_texts(second) == []

        # /reset 之后的新代际：重新生成并注入
        await plugin.on_any_message(_event("/reset"))
        third = _request(contexts=[{"role": "user", "content": "你好"}])
        await plugin.on_llm_request(_event(), third)
        assert len(inst.provider.calls) == 2, "/reset 后新代际应重新生成一次批注"
        text = _all_text(third)
        assert "【页边批注" in text
        assert "第二轮的新批注" in text, "新代际应注入新生成的批注"
        loaded, _ = plugin.load_annotations(SENDER)
        assert len(loaded["annotations"]) == 2


async def test_persist_mode_generates_once_and_injects_block_every_round():
    async with World(config=ann_config(persist_inject=True)) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        inst.provider.replies[:] = [_annotation_payload()]

        reqs = []
        for _ in range(3):
            req = _request(contexts=[{"role": "user", "content": "你好"}])
            await plugin.on_llm_request(_event(), req)
            reqs.append(_all_text(req))
        assert len(inst.provider.calls) == 1, (
            "persist 模式每轮注入日记，但批注只在首次注入时生成一次"
        )
        for i, text in enumerate(reqs):
            assert "<memory_diary>" in text, f"第 {i + 1} 轮日记注入丢失"
            assert "【页边批注" in text, f"第 {i + 1} 轮应带批注块"


async def test_generation_failure_does_not_block_diary_injection():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        seed_diary(plugin)
        inst.provider.fail_with = "provider down"  # 生成抛异常 → llm_error
        req = _request(contexts=[{"role": "user", "content": "你好"}])
        await plugin.on_llm_request(_event(), req)
        text = _all_text(req)
        assert "<memory_diary>" in text, "批注生成失败必须不影响日记注入"
        assert "【页边批注" not in text


def _annotation_payload(anchor: str = "喜欢在深夜聊天", note: str = "她总是这么晚才开口。"):
    return json.dumps(
        {"title": "页边的话", "annotations": [{"anchor": anchor, "note": note, "is_question": True}]},
        ensure_ascii=False,
    )
