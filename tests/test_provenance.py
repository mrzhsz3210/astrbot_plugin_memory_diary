# -*- coding: utf-8 -*-
"""来源侧车与批注来源过滤：写作 <self> 标记、provenance 存取、可锚文本、生成过滤。

覆盖三层方案：
1. life（无新对话随笔）片段整体不参与批注锚定；
2. <self>…</self> 自发挥子串写入 provenance 侧车（HMAC 签名），
   批注可锚文本里替换为占位符；
3. 批注 prompt 读到可锚版本 + 锚点校验硬过滤 + 锚点引用占位符时丢弃。
老日记没有 provenance 时退化为只按随笔模式过滤（兼容 v0.6.0 及更早数据）。
只驱动 refactor 侧；LLM 全部用 FakeProvider 脚本化，不联网。
"""

from __future__ import annotations

import json
import zipfile

from annotation_testkit import (
    DAY,
    OTHER_DAY,
    SENDER,
    SENDER_NAME,
    UMO,
    ann_config,
    seed_diary,
)
from harness.world import World

# 与 memory_diary.constants.SELF_PLACEHOLDER 保持一致
# （harness 要在运行时才会挂 refactor 源码路径，模块级导入不可用）
SELF_PLACEHOLDER = "〔她的私人随笔〕"


def _plugin(world):
    return world.refactor.plugin


def _assembled(plugin, sender: str = SENDER):
    text, tampered, names = plugin._assemble_inject(sender)
    assert tampered is False
    return text, names


def _payload(items, title="页边的话") -> str:
    return json.dumps({"title": title, "annotations": items}, ensure_ascii=False)


def _candidate(anchor: str, note: str, is_question: bool = False) -> dict:
    return {"anchor": anchor, "note": note, "is_question": is_question}


def _zip_entries(path):
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def _rewrite_provenance_raw(plugin, sender, new_raw: bytes):
    """直接改写 zip 里的 provenance.json（模拟插件之外的手动修改）。"""
    path = plugin._diary_path(sender)
    entries = _zip_entries(path)
    entries[plugin.PROVENANCE_ENTRY] = new_raw
    plugin._write_zip_atomic(path, entries)


REAL_TEXT = (
    "她说想养猫，问我哪种猫掉毛少，还说已经看了两个星期的攻略。"
    "我告诉她橘猫好养，她笑得停不下来，说那就叫“橘子”吧。"
) * 4  # 约 200+ 字 ×4，保证额度 ≥ 2

SELF_SENTENCE = "等回复的时候我看了一下午云，云的边缘毛茸茸的。"


def _seed_mixed_day(plugin, with_provenance: bool = True):
    """一天里先写真实互动完整篇，再追加一篇自发挥句子的完整篇 + 一篇随笔。"""
    seed_diary(plugin, REAL_TEXT)
    plugin._save_diary(
        SENDER,
        f"她又问起猫粮的价格。{SELF_SENTENCE}",
        sender_name=SENDER_NAME,
        entry_name_hint=DAY,
        origin="private",
        mode="full",
    )
    plugin._save_diary(
        SENDER,
        "今天没有人来找我聊天，我把旧对话翻出来看了很多遍。",
        sender_name=SENDER_NAME,
        entry_name_hint=DAY,
        origin="private",
        mode="life",
        separator_note="（无新对话·她的日常随笔）",
    )
    if with_provenance:
        assert plugin.record_self_provenance(SENDER, DAY, [SELF_SENTENCE]) is True


# ---------------------------------------------------------------------------
# _extract_self_marks：标签剥离
# ---------------------------------------------------------------------------


async def test_extract_self_marks_paired_tags_removed_and_collected():
    async with World(config={}) as world:
        p = _plugin(world)
        text, spans = p._extract_self_marks(
            "她说想养猫。<self>等回复的时候我看了一下午云。</self>明天见。"
        )
        assert "<self>" not in text and "</self>" not in text, "标签必须剥干净"
        assert text == "她说想养猫。等回复的时候我看了一下午云。明天见。"
        assert spans == ["等回复的时候我看了一下午云。"]


async def test_extract_self_marks_multiline_case_insensitive_and_stray():
    async with World(config={}) as world:
        p = _plugin(world)
        text, spans = p._extract_self_marks(
            "开头<SELF>\n第一段\n第二段\n</SELF>结尾<self>落单"
        )
        assert "<SELF>" not in text and "<self>" not in text and "</SELF>" not in text
        assert text == "开头第一段\n第二段结尾落单", f"裸标签兜底剥离失败：{text!r}"
        assert spans == ["第一段\n第二段"]

        # 没有标签：原文不动、空列表
        same, spans2 = p._extract_self_marks("普通日记正文。")
        assert same == "普通日记正文。" and spans2 == []


# ---------------------------------------------------------------------------
# provenance 侧车：存取 / 签名三态 / 容量
# ---------------------------------------------------------------------------


async def test_provenance_roundtrip_and_zip_layout():
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_diary(plugin)
        assert plugin.record_self_provenance(SENDER, DAY, [SELF_SENTENCE]) is True

        entries = _zip_entries(plugin._diary_path(SENDER))
        assert plugin.PROVENANCE_ENTRY in entries, "provenance.json 没写进 zip"
        assert plugin.PROVENANCE_SIG_ENTRY in entries, "来源侧车签名缺失"
        raw = entries[plugin.PROVENANCE_ENTRY]
        assert (
            entries[plugin.PROVENANCE_SIG_ENTRY].decode("utf-8").strip()
            == plugin._provenance_sig(SENDER, raw)
        )

        loaded, status = plugin.load_provenance(SENDER)
        assert status == "ok"
        assert loaded["entries"][f"{DAY}.md"]["self"] == [SELF_SENTENCE]

        # 同一天再记一条：增量追加，不覆盖旧的
        assert plugin.record_self_provenance(SENDER, DAY, ["第二句自发挥"]) is True
        loaded, status = plugin.load_provenance(SENDER)
        assert status == "ok"
        assert loaded["entries"][f"{DAY}.md"]["self"] == [
            SELF_SENTENCE,
            "第二句自发挥",
        ]

        # 同一天重复记录同一句：去重，不增长
        assert plugin.record_self_provenance(SENDER, DAY, [SELF_SENTENCE]) is True
        loaded, _ = plugin.load_provenance(SENDER)
        assert loaded["entries"][f"{DAY}.md"]["self"].count(SELF_SENTENCE) == 1


async def test_provenance_missing_on_old_zip_and_bad_day():
    async with World(config={}) as world:
        plugin = _plugin(world)
        # 还没有日记 zip：missing（旧数据兼容，不告警）
        assert plugin.load_provenance(SENDER) == (
            plugin._provenance_empty(),
            "missing",
        )
        seed_diary(plugin)
        # 有 zip 没有 provenance.json：仍是 missing（v0.6.0 及更早的包）
        assert plugin.load_provenance(SENDER)[1] == "missing"
        # 非法 day / 空 spans：不写入、不抛异常
        assert plugin.record_self_provenance(SENDER, "不是日期", ["x"]) is False
        assert plugin.record_self_provenance(SENDER, DAY, []) is False
        assert plugin.load_provenance(SENDER)[1] == "missing"


async def test_provenance_tampered_fail_closed():
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_diary(plugin)
        assert plugin.record_self_provenance(SENDER, DAY, [SELF_SENTENCE]) is True

        raw = _zip_entries(plugin._diary_path(SENDER))[plugin.PROVENANCE_ENTRY]
        _rewrite_provenance_raw(
            plugin, SENDER, raw.replace("毛茸茸".encode(), "胖乎乎".encode())
        )

        loaded, status = plugin.load_provenance(SENDER)
        assert status == "tampered", f"改过来源 JSON 后应判篡改，实际 {status!r}"
        assert loaded == plugin._provenance_empty()

        # fail-closed：拒绝覆写
        assert plugin.record_self_provenance(SENDER, DAY, ["新句子"]) is False
        assert plugin.save_provenance(SENDER, {"entries": {f"{DAY}.md": {"self": ["x"]}}}) is False


async def test_provenance_normalize_sanitizes_and_caps():
    async with World(config={}) as world:
        plugin = _plugin(world)
        raw = {
            "version": 99,
            "updated_at": "x" * 40,
            "entries": {
                "不是日期.md": {"self": ["丢弃我"]},
                "20261023.md": "不是 dict",
                "20261024.md": {
                    "self": [
                        "有效",
                        "",
                        "有效",
                        123,
                        "  待规整  ",
                    ]
                },
            },
        }
        out = plugin._normalize_provenance(raw)
        assert out["version"] == 1
        assert len(out["updated_at"]) <= 30
        assert list(out["entries"].keys()) == ["20261024.md"]
        assert out["entries"]["20261024.md"]["self"] == ["有效", "待规整"]

        # 容量：条数与总字数双上限，超限丢弃
        many = [f"句{i}" for i in range(plugin.PROVENANCE_MAX_SELF_PER_ENTRY + 10)]
        out2 = plugin._normalize_provenance(
            {"entries": {f"{DAY}.md": {"self": many}}}
        )
        kept = out2["entries"][f"{DAY}.md"]["self"]
        assert len(kept) == plugin.PROVENANCE_MAX_SELF_PER_ENTRY

        assert plugin._normalize_provenance(None) == plugin._provenance_empty()
        assert plugin._normalize_provenance(["x"]) == plugin._provenance_empty()


# ---------------------------------------------------------------------------
# 与日记生命周期共存：_save_diary 保留 / 删除级联
# ---------------------------------------------------------------------------


async def test_save_diary_rewrite_preserves_provenance_bytes():
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_diary(plugin)
        assert plugin.record_self_provenance(SENDER, DAY, [SELF_SENTENCE]) is True
        raw_before = _zip_entries(plugin._diary_path(SENDER))[plugin.PROVENANCE_ENTRY]
        sig_before = _zip_entries(plugin._diary_path(SENDER))[
            plugin.PROVENANCE_SIG_ENTRY
        ]

        # 同一天再写一篇：_save_diary 整体重写 zip，侧车必须逐字节保留
        plugin._save_diary(
            SENDER, "第二篇。", sender_name=SENDER_NAME, origin="private", mode="brief"
        )
        entries = _zip_entries(plugin._diary_path(SENDER))
        assert entries[plugin.PROVENANCE_ENTRY] == raw_before
        assert entries[plugin.PROVENANCE_SIG_ENTRY] == sig_before
        assert plugin.load_provenance(SENDER)[1] == "ok"


async def test_delete_entries_cascades_provenance():
    async with World(config={}) as world:
        plugin = _plugin(world)
        seed_diary(plugin, "第一天的日记。", day=DAY)
        plugin._save_diary(
            SENDER,
            "第二天的日记。",
            sender_name=SENDER_NAME,
            entry_name_hint=OTHER_DAY,
            origin="private",
            mode="full",
        )
        assert plugin.record_self_provenance(SENDER, DAY, ["第一天自发挥"]) is True
        assert plugin.record_self_provenance(SENDER, OTHER_DAY, ["第二天自发挥"]) is True

        assert plugin._delete_entries(SENDER, [f"diaries/{DAY}.md"]) == 1
        loaded, status = plugin.load_provenance(SENDER)
        assert status == "ok"
        assert list(loaded["entries"].keys()) == [f"{OTHER_DAY}.md"], (
            f"删除日记应连带清理该天的来源记录，实际 {list(loaded['entries'])!r}"
        )


# ---------------------------------------------------------------------------
# 可锚文本：life 片段与自发挥子串替换为占位符
# ---------------------------------------------------------------------------


async def test_anchorable_text_replaces_life_segment_and_self_spans():
    async with World(config={}) as world:
        plugin = _plugin(world)
        _seed_mixed_day(plugin)

        _, _, anchorable, _search = plugin._annotation_day_texts(
            SENDER, [f"diaries/{DAY}.md"]
        )[0]
        assert "她又问起猫粮的价格。" in anchorable
        assert SELF_SENTENCE not in anchorable, "自发挥子串必须被占位符替换"
        assert SELF_PLACEHOLDER in anchorable
        assert "今天没有人来找我聊天" not in anchorable, (
            "无新对话随笔片段必须整体替换"
        )
        assert SELF_PLACEHOLDER in anchorable
        # 分隔条（结构信息）保留
        assert "无新对话" in anchorable


async def test_anchorable_text_without_provenance_keeps_old_behavior():
    async with World(config={}) as world:
        plugin = _plugin(world)
        _seed_mixed_day(plugin, with_provenance=False)

        _, _, anchorable, _search = plugin._annotation_day_texts(
            SENDER, [f"diaries/{DAY}.md"]
        )[0]
        # 老数据没有来源记录：自发挥句子仍在可锚文本里（与 v0.6.0 行为一致），
        # 但 life 片段依旧被过滤（这条不依赖来源侧车）
        assert SELF_SENTENCE in anchorable
        assert "今天没有人来找我聊天" not in anchorable


# ---------------------------------------------------------------------------
# 生成链路：prompt 用可锚版本 + 锚点硬过滤
# ---------------------------------------------------------------------------


async def test_generate_drops_anchors_on_self_content_and_life():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        _seed_mixed_day(plugin)
        text, names = _assembled(plugin)

        inst.provider.replies[:] = [
            _payload(
                [
                    _candidate("等回复的时候我看了一下午云", "云的边缘毛茸茸的。"),
                    _candidate("旧对话翻出来看了很多遍", "一个人也不坏。"),
                    _candidate("问我哪种猫掉毛少", "原来她认真考虑过这件事。"),
                ]
            )
        ]
        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 1, (
            f"只有真实互动锚点应被接受，实际 created={result['created']} "
            f"reason={result['reason']!r}"
        )
        anns, status = plugin.load_annotations(SENDER)
        assert status == "ok"
        assert [a["anchor"] for a in anns["annotations"]] == ["问我哪种猫掉毛少"]
        assert anns["annotations"][0]["entry"] == f"{DAY}.md"

        # prompt 里必须是可锚版本：占位符在、自发挥句子与随笔正文不在
        prompt = inst.provider.calls[0]["prompt"]
        assert SELF_PLACEHOLDER in prompt
        assert SELF_SENTENCE not in prompt
        assert "今天没有人来找我聊天" not in prompt


async def test_generate_rejects_placeholder_anchor():
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        _seed_mixed_day(plugin)
        text, names = _assembled(plugin)

        # 模型照抄占位符当锚点：必须被丢弃（防"锚在占位符上"）
        inst.provider.replies[:] = [
            _payload([_candidate(SELF_PLACEHOLDER, "这段写得真好。")])
        ]
        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 0
        assert result["reason"] == "no_valid"


async def test_generate_accepts_real_anchor_without_provenance():
    """老日记（无来源记录）：真实互动锚点照常工作——兼容回退不能误伤正常批注。"""
    async with World(config=ann_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        _seed_mixed_day(plugin, with_provenance=False)
        text, names = _assembled(plugin)

        inst.provider.replies[:] = [
            _payload([_candidate("问我哪种猫掉毛少", "原来她认真考虑过这件事。")])
        ]
        result = await plugin._generate_annotations(SENDER, UMO, text, names)
        assert result["created"] == 1
        anns, status = plugin.load_annotations(SENDER)
        assert status == "ok"
        assert [a["anchor"] for a in anns["annotations"]] == ["问我哪种猫掉毛少"]
