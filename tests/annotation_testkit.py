# -*- coding: utf-8 -*-
"""v0.6 批注测试共用工具（不是测试模块，run.py 不会收集它）。

只放三件事：
1. 批注配置 / 数据构造（annotations.json 的记录形状见接口契约第 3 节）；
2. 写日记 + 写批注的种子流程（所有批注测试都从"已有日记的用户"开始）；
3. 篡改批注侧车（改原文 / 重签 / 丢签名）与迁移包内层改写的便捷函数。

只读测试：全部数据都在 World 的临时目录里，不触碰仓库源码。
"""

from __future__ import annotations

import json

from image_testkit import (  # noqa: F401  （re-export 给测试直接用）
    assert_raises,
    drop_zip_entry,
    repack_inner_diary_zip,
    replace_zip_entry,
    rewrite_zip,
    zip_entries,
)

SENDER = "10001"
SENDER_NAME = "阿罗"
OTHER_SENDER = "20002"
UMO = "aiocqhttp:FriendMessage:10001"
GROUP_UMO = "aiocqhttp:GroupMessage:88888"
DAY = "20261023"
OTHER_DAY = "20261022"
ROUND = "r_20261031_193300_ab12"
OLD_ROUND = "r_20261028_101500_cd34"

ANCHOR = "喜欢在深夜聊天"
NOTE = "她总是这么晚才开口。"

# anchor 必须是日记原文子串（生成时的锚点校验只看原文/占位化文本）
DIARY_TEXT = "今天下午一个人去了江边，风很大。\n阿罗，喜欢在深夜聊天。"


def ann_config(**overrides) -> dict:
    """批注功能开启时的配置（与 _conf_schema.json 默认值对齐，测试按需覆盖）。"""
    cfg = {
        "annotation_enabled": True,
        "annotation_max_per_round": 5,
        "annotation_question_hint": True,
        "annotation_timeout": 30,
        # 关掉人设口吻：注释类断言用固定文案，避免 _say 多消耗 provider 脚本回复
        "use_persona": False,
    }
    cfg.update(overrides)
    return cfg


def long_diary(paragraphs: int = 20) -> str:
    """足够长的日记正文：额度按 len(去空白)//300 折算，20 段约 1000+ 字。"""
    lines = []
    for i in range(1, paragraphs + 1):
        lines.append(
            f"第{i:02d}段，今天下午一个人去了江边，风很大，她慢慢走回家。"
            "路灯一盏一盏亮起来，她没打伞，也没觉得冷。"
        )
    return "\n".join(lines)


def make_annotation(
    seq: int = 1,
    *,
    day: str = DAY,
    anchor: str = ANCHOR,
    note: str = NOTE,
    is_question: bool = False,
    status: str = "open",
    created_at: str = "2026-10-31T19:33:00",
    round_: str = ROUND,
    reply=None,
) -> dict:
    """按契约第 3 节构造一条批注记录（entry 是 basename）。"""
    return {
        "id": f"ann_{day}_{seq}",
        "round": round_,
        "anchor": anchor,
        "entry": f"{day}.md",
        "created_at": created_at,
        "note": note,
        "is_question": is_question,
        "status": status,
        "reply": reply,
    }


def make_reply(
    *,
    resolved_at: str = "2026-10-31T20:10:00",
    reply_title: str = "主人的回复",
    summary: str = "我们聊明白了：那阵子她确实在等一个答复。",
    speculation_marked: str = "",
) -> dict:
    return {
        "resolved_at": resolved_at,
        "reply_title": reply_title,
        "summary": summary,
        "confirmed_by_user": True,
        "speculation_marked": speculation_marked,
    }


def ann_data(
    records=None,
    *,
    last_round: str = ROUND,
    last_title: str = "页边的话",
    updated_at: str = "2026-10-31T19:33:00",
) -> dict:
    return {
        "version": 1,
        "last_round": last_round,
        "last_title": last_title,
        "updated_at": updated_at,
        "annotations": list(records if records is not None else [make_annotation()]),
    }


# ---------------------------------------------------------------------------
# 种子：写日记 → 写批注
# ---------------------------------------------------------------------------


def seed_diary(
    plugin,
    text: str = DIARY_TEXT,
    sender: str = SENDER,
    day: str = DAY,
):
    """写一篇私聊完整日记（批注必须写在已有日记 zip 之上）。返回 zip 条目名。

    参数顺序：text 是第二个位置参数（调用处大量写 ``seed_diary(plugin, "正文")``）。
    """
    return plugin._save_diary(
        sender,
        text,
        sender_name=SENDER_NAME,
        entry_name_hint=day,
        origin="private",
        mode="full",
    )


def seed_annotations(plugin, records=None, sender: str = SENDER, **kwargs) -> dict:
    """先保证有日记 zip，再写批注侧车；返回写入的规范前数据。

    注意参数顺序：records 是第二个位置参数（调用处大量写
    ``seed_annotations(plugin, [make_annotation(1)])``）。
    """
    seed_diary(plugin, sender=sender)
    data = ann_data(records, **kwargs)
    ok = plugin.save_annotations(sender, data)
    assert ok, "测试种子批注落盘失败（save_annotations 返回 False）"
    return data


def seed_two_day_annotations(plugin, sender: str = SENDER):
    """两天的日记 + 每天各一条批注（删除级联 / 按天取批注用）。"""
    seed_diary(plugin, "第一天的日记。", sender=sender, day=DAY)
    seed_diary(plugin, "第二天的日记。", sender=sender, day=OTHER_DAY)
    records = [
        make_annotation(1, day=DAY, anchor="第一天的日记", note="第一天写的。"),
        make_annotation(2, day=OTHER_DAY, anchor="第二天的日记", note="第二天写的。"),
    ]
    ok = plugin.save_annotations(sender, ann_data(records))
    assert ok
    return records


# ---------------------------------------------------------------------------
# 侧车读写 / 篡改
# ---------------------------------------------------------------------------


def annotations_raw(plugin, sender: str = SENDER) -> bytes:
    entries = zip_entries(plugin._diary_path(sender))
    return entries[plugin.ANNOTATIONS_ENTRY]


def annotations_sig_raw(plugin, sender: str = SENDER) -> str:
    entries = zip_entries(plugin._diary_path(sender))
    return entries[plugin.ANNOTATIONS_SIG_ENTRY].decode("utf-8").strip()


def rewrite_annotations_raw(
    plugin, raw: bytes, sender: str = SENDER, *, resign: bool = False
) -> None:
    """把 zip 里的 annotations.json 换成 raw；resign=False 时保留旧签名（=篡改）。"""
    path = plugin._diary_path(sender)
    entries = zip_entries(path)
    entries[plugin.ANNOTATIONS_ENTRY] = raw
    if resign:
        entries[plugin.ANNOTATIONS_SIG_ENTRY] = plugin._annotations_sig(
            sender, raw
        ).encode("utf-8")
    rewrite_zip(path, entries)


def drop_annotations_sig(plugin, sender: str = SENDER) -> None:
    drop_zip_entry(plugin._diary_path(sender), plugin.ANNOTATIONS_SIG_ENTRY)


def tamper_annotations(plugin, sender: str = SENDER) -> bytes:
    """把批注 JSON 改一个字（签名不动）→ 读取应判 tampered。返回改坏后的字节。"""
    raw = annotations_raw(plugin, sender)
    tampered = raw.replace("她总是".encode("utf-8"), "她被改".encode("utf-8"))
    if tampered == raw:  # 兜底：种子里可能没有可替换的字
        tampered = raw + b" "
    rewrite_annotations_raw(plugin, tampered, sender)
    return tampered


def make_bundle(plugin, sender: str = SENDER, day: str = DAY):
    """构造迁移包（含批注侧车），返回临时包路径（调用方负责删除）。"""
    seed_diary(plugin, sender, day=day)
    path = plugin._diary_path(sender)
    return plugin._build_export_bundle(sender, path, plugin._list_entries(sender))


def broken_json_raw(plugin, sender: str = SENDER) -> bytes:
    """签名正确但 JSON 解析不了的侧车内容（用于"坏 JSON 按篡改"用例）。"""
    raw = b'{"version": 1, "annotations": ['
    entries = zip_entries(plugin._diary_path(sender))
    entries[plugin.ANNOTATIONS_ENTRY] = raw
    entries[plugin.ANNOTATIONS_SIG_ENTRY] = plugin._annotations_sig(
        sender, raw
    ).encode("utf-8")
    rewrite_zip(plugin._diary_path(sender), entries)
    return raw
