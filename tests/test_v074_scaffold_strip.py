# -*- coding: utf-8 -*-
"""v0.7.4：固定形状提示脚手架不得进入写作与显式交代素材。"""

from __future__ import annotations

from memory_diary.common import strip_prompt_scaffold
from memory_diary.service import ServiceMixin
from memory_diary.writing import (
    DiaryResult,
    ensure_explicit_notes,
    is_explicit_message,
    parse_diary_result,
)

REMINDER = (
    "<system_reminder>User ID: 123456, Nickname: 大肥鱼, "
    "Current datetime: 2026-11-08 22:00:00</system_reminder>"
)
VOICE = "[你上一条回复是念成语音发出去的，内容是「记得周六买鱼粮」]"
SERVICE = ServiceMixin()


def test_strip_prompt_scaffold_preserves_real_message_and_fast_path():
    plain = "那我们明天几点出发"
    assert strip_prompt_scaffold(plain + REMINDER + VOICE) == plain
    assert strip_prompt_scaffold(plain) is plain


def test_unclosed_system_reminder_stops_at_line_end():
    text = "<system_reminder>User ID: 123456\n下一行是真实发言"
    assert strip_prompt_scaffold(text) == "\n下一行是真实发言"


def test_flatten_history_strips_scaffold_and_keeps_assistant_reply():
    flattened = SERVICE._flatten_history(
        [
            {"role": "user", "content": "那我们明天几点出发" + REMINDER + VOICE},
            {"role": "assistant", "content": "上午九点，可以吗？"},
        ]
    )
    assert flattened == "用户: 那我们明天几点出发\n我: 上午九点，可以吗？"
    assert "User ID:" not in flattened
    assert "念成语音发出去" not in flattened


def test_explicit_materials_rejects_two_scaffolded_rounds():
    history = [
        {"role": "user", "content": "那我们明天几点出发" + REMINDER + VOICE},
        {"role": "user", "content": "我带身份证" + REMINDER + VOICE},
    ]
    assert SERVICE._explicit_materials(history, []) == []


def test_real_explicit_message_survives_but_quoted_narration_does_not_trigger():
    original = "记得周六买鱼粮，别忘带伞"
    history = [{"role": "user", "content": original + REMINDER + VOICE}]
    assert SERVICE._explicit_materials(history, []) == [original]
    assert is_explicit_message(original)
    assert not is_explicit_message("转述「记得周六开会」")
    assert not is_explicit_message('转述"记得周六开会"')
    # 已知权衡：引号无法区分转述与本人强调，整句放进引号的真交代会漏召。
    assert not is_explicit_message("「记得周六开会」")


def test_ensure_explicit_notes_cleans_required_and_deduplicates_bidirectionally():
    clean = "记得周六买鱼粮，别忘带伞"
    parsed = parse_diary_result(f"今天讨论了采购。<explicit>{clean}</explicit>")
    before = parsed.text
    ensured = ensure_explicit_notes(parsed, [clean + REMINDER + VOICE])
    assert ensured.text == before
    assert ensured.explicit_notes == [clean]
    assert "【显式交代】" not in ensured.text

    shorter_note = DiaryResult(
        status="ok", text="已有正文", explicit_notes=[clean]
    )
    ensured = ensure_explicit_notes(shorter_note, [clean + "，上午十点出发"])
    assert ensured.text == "已有正文"
    assert ensured.explicit_notes == [clean]


def test_pure_scaffold_is_skipped_by_both_history_entry_points():
    history = [{"role": "user", "content": REMINDER + VOICE}]
    assert SERVICE._flatten_history(history) == ""
    assert SERVICE._explicit_materials(history, []) == []


def test_hyphen_variant_and_split_fragments_are_stripped():
    hyphen = "<system-reminder>User ID: 123456</system-reminder>"
    assert strip_prompt_scaffold("保留" + hyphen) == "保留"
    fragments = "User ID: 123456, Nickname: 大肥鱼\nCurrent datetime: 2026-11-08\n真实发言"
    assert strip_prompt_scaffold(fragments) == "\n\n真实发言"
