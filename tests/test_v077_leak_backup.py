# -*- coding: utf-8 -*-
"""v0.7.7 任务单（二）：推理泄漏兜底 + 外部删除/篡改韧性 + 写作身份口径。

五组验收（任务单 §4）：
1. 推理模型思维链泄漏：剥离 <think> 块、识别英文推理句式与语言不一致，
   重写一次，仍失败降级简记；落盘正文零英文思维链。
2. 账号昵称 ≠ 人格名：写日记的身份素材只来自会话 persona。
3. 手工删掉 diary zip → 启动/读写有告警，/日记状态 明示 → 从 backups/ 恢复。
4. 无签名/签名错误的备份走恢复入口一律拒绝。
5. 滚动备份按份数裁剪。
"""

from __future__ import annotations

import io
import json
import zipfile

from harness.fakes import make_event
from harness.world import BRIEF_TEXT, DIARY_TEXT, World, message
from memory_diary.writing import (
    language_mismatch,
    looks_like_reasoning_leak,
    strip_reasoning_blocks,
)

SENDER = "10001"
SENDER_NAME = "阿罗"
DAY = "20261023"
DAY2 = "20261024"

LEAK_TEXT = (
    "Let me think about what happened today.\n"
    "The user talked about her exam and seemed nervous.\n"
    "Wait, I should also consider the mood of the conversation.\n"
    "Okay, so let me now write the diary entry for her."
)


def private_event(text: str, **kwargs):
    return make_event(text, sender_id=SENDER, sender_name=SENDER_NAME, **kwargs)


def _chinese_history(inst):
    inst.set_history(
        [message("user", "今天考试考完了，感觉还不错"), message("assistant", "辛苦啦")]
    )


# ---------------------------------------------------------------------------
# 1. 推理剥离
# ---------------------------------------------------------------------------


def test_strip_reasoning_blocks_unit():
    assert strip_reasoning_blocks("正常日记正文") == "正常日记正文"
    assert (
        strip_reasoning_blocks("<think>English reasoning here</think>正文")
        == "正文"
    )
    assert (
        strip_reasoning_blocks("<thinking>abc</thinking>正文")
        == "正文"
    )
    # 未闭合（模型被截断）：从标签处吃到文末
    assert strip_reasoning_blocks("前缀<think>没写完的推理") == "前缀"
    # 正常文本里的 think 单词不误伤
    assert strip_reasoning_blocks("我想 think 一下这个词") == "我想 think 一下这个词"


def test_leak_heuristics_unit():
    assert looks_like_reasoning_leak(LEAK_TEXT) is True
    assert looks_like_reasoning_leak(DIARY_TEXT) is False
    assert looks_like_reasoning_leak(BRIEF_TEXT) is False
    assert language_mismatch(LEAK_TEXT * 3, "今天考试考完了") is True
    assert language_mismatch(DIARY_TEXT, "今天考试考完了") is False
    # 基准为空/很短时不判
    assert language_mismatch(LEAK_TEXT, "") is False
    assert language_mismatch("short", "今天考试考完了") is False


async def test_diary_write_strips_think_blocks():
    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        inst = world.refactor
        _chinese_history(inst)
        inst.provider.replies[:] = [
            "<think>Let me analyze the conversation first.</think>" + BRIEF_TEXT
        ]
        out = await inst.run_handler("write_diary_cmd", private_event("/日记"))
        assert any("日记写好啦" in t for t in out)
        content, tampered = inst.plugin._load_latest(SENDER)[:2]
        assert not tampered
        assert "<think>" not in content and "analyze" not in content
        assert "简记" in content


async def test_diary_write_retries_on_reasoning_leak():
    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        inst = world.refactor
        _chinese_history(inst)
        inst.provider.replies[:] = [LEAK_TEXT, BRIEF_TEXT]
        out = await inst.run_handler("write_diary_cmd", private_event("/日记"))
        assert any("日记写好啦" in t for t in out)
        # 第一次泄漏 → 重写一次；第二次正常 → 落盘的是正常正文
        assert len(inst.provider.calls) == 2
        assert "不要输出任何分析、思考过程或英文推理" in inst.provider.calls[1]["prompt"]
        content, tampered = inst.plugin._load_latest(SENDER)[:2]
        assert not tampered
        assert "Let me think" not in content
        assert "简记" in content


async def test_diary_write_degrades_when_leak_persists():
    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        inst = world.refactor
        _chinese_history(inst)
        inst.provider.replies[:] = [LEAK_TEXT, LEAK_TEXT + "\nSo the day was fine."]
        out = await inst.run_handler("write_diary_cmd", private_event("/日记"))
        assert any("日记写好啦" in t for t in out)
        assert len(inst.provider.calls) == 2
        # 两次都泄漏：降级简记落盘（spec：不让英文思维链以 full 姿态进日记）
        content, _ = inst.plugin._load_latest(SENDER)[:2]
        assert "简记" in content


# ---------------------------------------------------------------------------
# 2. 写作身份：只来自会话人格（Bug B）
# ---------------------------------------------------------------------------


async def test_writing_identity_comes_from_session_persona_only():
    async with World(
        config={"use_persona": True, "diary_op_interval": 0},
        persona_prompt="你是玛丽安，一位安静的图书管理员。",
    ) as world:
        inst = world.refactor
        _chinese_history(inst)
        inst.provider.replies[:] = [BRIEF_TEXT]
        # 用户昵称与 bot 对外伪装名（"一条叫阿奏的鱼"）都和人格名不同
        event = make_event(
            "/日记", sender_id=SENDER, sender_name="一条叫阿奏的鱼"
        )
        await inst.run_handler("write_diary_cmd", event)
        assert inst.entries() == [f"diaries/{DAY}.md"], "日记没有落盘"
        call = inst.provider.calls[0]
        # 身份素材 = 会话人格：system_prompt 只带人格设定
        assert "玛丽安" in call["system_prompt"]
        assert "阿奏" not in call["system_prompt"]
        # 用户昵称只作为「用户」画像素材出现，不是"我是谁"
        assert "用户当前昵称：一条叫阿奏的鱼" in call["prompt"]


# ---------------------------------------------------------------------------
# 3. 滚动备份
# ---------------------------------------------------------------------------


async def test_rolling_backup_created_and_capped():
    async with World(config={"backup_copies": 2}) as world:
        plugin = world.refactor.plugin
        backup_dir = plugin.data_dir / "backups"

        plugin._save_diary(SENDER, DIARY_TEXT, sender_name=SENDER_NAME,
                           entry_name_hint=DAY, origin="private", mode="full")
        assert not backup_dir.exists() or not list(backup_dir.glob("*.zip"))

        plugin._save_diary(SENDER, BRIEF_TEXT, sender_name=SENDER_NAME,
                           entry_name_hint=DAY2, origin="private", mode="brief")
        copies = sorted(backup_dir.glob("*.zip"))
        assert len(copies) == 1
        # 第一份备份 = 覆盖前的状态（只有第一天的日记）
        with zipfile.ZipFile(copies[0]) as z:
            names = set(z.namelist())
        assert f"diaries/{DAY}.md" in names and f"diaries/{DAY2}.md" not in names

        # 再写两次：备份数被裁到上限 2
        plugin._save_diary(SENDER, BRIEF_TEXT, sender_name=SENDER_NAME,
                           entry_name_hint=DAY, origin="private", mode="brief")
        plugin._save_diary(SENDER, BRIEF_TEXT, sender_name=SENDER_NAME,
                           entry_name_hint=DAY2, origin="private", mode="brief")
        assert len(sorted(backup_dir.glob("*.zip"))) == 2


async def test_rolling_backup_disabled_by_zero():
    async with World(config={"backup_copies": 0}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, sender_name=SENDER_NAME,
                           entry_name_hint=DAY, origin="private", mode="full")
        plugin._save_diary(SENDER, BRIEF_TEXT, sender_name=SENDER_NAME,
                           entry_name_hint=DAY2, origin="private", mode="brief")
        assert not (plugin.data_dir / "backups").exists()


# ---------------------------------------------------------------------------
# 4. 外部删除/篡改告警
# ---------------------------------------------------------------------------


async def test_external_delete_alert_and_status_command():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, sender_name=SENDER_NAME,
                           entry_name_hint=DAY, origin="private", mode="full")
        # 登记册已记录该包；模拟外部删除（别的插件 shell 直接 rm）
        diary_path = plugin._diary_path(SENDER)
        assert plugin._vanished_diary_alerts() == []
        diary_path.unlink()

        alerts = plugin._vanished_diary_alerts()
        assert len(alerts) == 1 and alerts[0]["owner"] == SENDER
        assert len(alerts[0]["backups"]) == 0  # 只写过一次，还没有备份

        out = "\n".join(
            await inst.run_handler("diary_status", private_event("/日记状态"))
        )
        assert "疑似外部删除" in out
        # 插件内删除会销记，不报外部删除
        plugin._save_diary(SENDER, DIARY_TEXT, sender_name=SENDER_NAME,
                           entry_name_hint=DAY, origin="private", mode="full")
        assert plugin._delete_diary(SENDER) is True
        assert plugin._vanished_diary_alerts() == []


async def test_all_entries_tampered_detection_and_status():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, DIARY_TEXT, sender_name=SENDER_NAME,
                           entry_name_hint=DAY, origin="private", mode="full")
        assert plugin._all_entries_tampered(SENDER) is False

        # 模拟外部捏造：保留条目名，签名全部换成无效值（无签名假包的等价态）
        path = plugin._diary_path(SENDER)
        with zipfile.ZipFile(path) as z:
            data = {n: z.read(n) for n in z.namelist()}
        for n in list(data):
            if n.startswith("sigs/"):
                data[n] = b"0" * 64
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n, b in data.items():
                z.writestr(n, b)
        path.write_bytes(buf.getvalue())

        assert plugin._all_entries_tampered(SENDER) is True
        out = "\n".join(
            await inst.run_handler("diary_status", private_event("/日记状态"))
        )
        assert "疑似外部改写" in out


# ---------------------------------------------------------------------------
# 5. 备份恢复（验签放行 / 拒绝）
# ---------------------------------------------------------------------------


def _seed_two_days_with_backup(plugin):
    plugin._save_diary(SENDER, DIARY_TEXT, sender_name=SENDER_NAME,
                       entry_name_hint=DAY, origin="private", mode="full")
    plugin._save_diary(SENDER, BRIEF_TEXT, sender_name=SENDER_NAME,
                       entry_name_hint=DAY2, origin="private", mode="brief")


async def test_restore_from_backup_after_external_delete():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _seed_two_days_with_backup(plugin)
        plugin._diary_path(SENDER).unlink()  # 外部删除

        backups = plugin.list_diary_backups(SENDER)
        assert len(backups) == 1
        days, err = plugin.restore_diary_backup(SENDER, backups[0]["name"])
        assert err == "" and days == [DAY]
        assert plugin._list_entries(SENDER) == [f"diaries/{DAY}.md"]
        content, tampered = plugin._load_entry(SENDER, f"diaries/{DAY}.md")
        assert not tampered and "用户画像" in content
        # 恢复后登记册重新在册，不再报外部删除
        assert plugin._vanished_diary_alerts() == []


async def test_restore_rejects_tampered_backup():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _seed_two_days_with_backup(plugin)
        backups = plugin.list_diary_backups(SENDER)
        backup_path = plugin.data_dir / "backups" / backups[0]["name"]

        # 篡改备份正文一个字节 → 恢复必须拒绝，且现有日记不受影响
        with zipfile.ZipFile(backup_path) as z:
            data = {n: z.read(n) for n in z.namelist()}
        data[f"diaries/{DAY}.md"] = data[f"diaries/{DAY}.md"] + b"x"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n, b in data.items():
                z.writestr(n, b)
        backup_path.write_bytes(buf.getvalue())

        days, err = plugin.restore_diary_backup(SENDER, backups[0]["name"])
        assert days == [] and "验签失败" in err
        assert plugin._list_entries(SENDER) == [
            f"diaries/{DAY}.md", f"diaries/{DAY2}.md"
        ]
        # 目录穿越/别的人的备份名：拒绝
        days, err = plugin.restore_diary_backup(SENDER, "../evil.zip")
        assert days == [] and err


async def test_restore_command_backup_channel():
    async with World(
        config={"use_persona": False, "diary_op_interval": 0}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed_two_days_with_backup(plugin)
        plugin._diary_path(SENDER).unlink()  # 外部删除 → 备份通道出现

        inst.set_replies([{"text": "B1"}])
        out = "\n".join(
            await inst.run_handler("restore_diary", private_event("/恢复日记"))
        )
        assert "自动备份" in out and "已从自动备份恢复" in out
        assert plugin._list_entries(SENDER) == [f"diaries/{DAY}.md"]


async def test_backup_channel_works_when_trash_disabled():
    """回收站被管理员关闭时，自动备份仍是独立可用的救援通道。"""
    async with World(
        config={
            "use_persona": False,
            "trash_retention_days": 0,
            "diary_op_interval": 0,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed_two_days_with_backup(plugin)
        plugin._diary_path(SENDER).unlink()  # 外部删除

        inst.set_replies([{"text": "B1"}])
        out = "\n".join(
            await inst.run_handler("restore_diary", private_event("/恢复日记"))
        )
        assert "已从自动备份恢复" in out
        assert plugin._list_entries(SENDER) == [f"diaries/{DAY}.md"]
