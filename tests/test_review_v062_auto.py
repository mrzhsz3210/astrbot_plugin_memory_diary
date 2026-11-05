# -*- coding: utf-8 -*-
"""v0.6.2 审查修复（auto-diary）：P3-9 / P3-10 / P3-24 回归闸门。

- P3-9：定时自动写日记中途重载/取消不再丢掉当天剩余用户——状态改记"当天已完成
  的 sender 集合"（旧状态文件向后兼容），tick 只跳过已完成者，未完成的下个 tick 补写；
  失败仍不重试（失败也记完成，保持既有边界）。
- P3-10：超过 grace + 轮询窗口时打 warning 记录跳过原因（不再静默跳过一整天）。
- P3-24：插图提示行从插件级单值 `_image_last_notice` 改为按 sender 分键的
  `_image_notice_by_sender`，多人并发写日记时各自的提示只进自己的通知。

全部只驱动 refactor 侧：这些是 v0.6.2 的新行为，baseline 必然不同，不做差分比较。
"""

from __future__ import annotations

import asyncio
import sys

from harness.fakes import FakeLLMResponse, FakeProvider, make_event
from harness.world import DIARY_TEXT, FIXED_NOW, World, message
from image_testkit import data_url, img_config, png

SENDER_A = "10001"
UMO_A = "aiocqhttp:FriendMessage:10001"
SENDER_B = "10002"
UMO_B = "aiocqhttp:FriendMessage:10002"
DAY = "20261023"

# 自动写日记的公共配置：关人设、关"写好了"告知（减少无关 LLM 调用），22:00 触发。
AUTO_CFG = {
    "use_persona": False,
    "enable_auto_diary": True,
    "auto_diary_time": "22:00",
    "auto_write_notify": False,
}


def _private_event(text: str, sender_id: str, sender_name: str, umo: str):
    return make_event(text, sender_id=sender_id, sender_name=sender_name, umo=umo)


def _seed_users(inst, with_material: bool = True) -> None:
    """登记 A/B 两个私聊用户，并给各自会话准备（可选的）新素材。"""
    plugin = inst.plugin
    plugin._auto_users = {
        SENDER_A: {"umo": UMO_A, "name": "阿A"},
        SENDER_B: {"umo": UMO_B, "name": "阿B"},
    }
    if with_material:
        inst.conversation_manager.set(UMO_A, history=[message("user", "A 的素材")])
        inst.conversation_manager.set(UMO_B, history=[message("user", "B 的素材")])


def _service_module(plugin):
    """refactor 侧 memory_diary.service 模块（用于替换模块级 logger）。"""
    for cls in type(plugin).__mro__:
        name = getattr(cls, "__module__", "")
        if name.endswith(".memory_diary.service"):
            return sys.modules[name]
    raise AssertionError("找不到 refactor 侧 memory_diary.service 模块")


class _RecordingLogger:
    """最小日志记录器：按 level 收集 service 模块打出的日志文本。"""

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def __getattr__(self, level):
        def _record(message, *args, **kwargs):
            self.records.append((level, str(message)))

        return _record

    def warnings(self) -> list[str]:
        return [msg for level, msg in self.records if level == "warning"]


# ---------------------------------------------------------------------------
# P3-9：当天完成集合 + 重载/取消后补写未完成用户
# ---------------------------------------------------------------------------


async def test_review_v062_auto_state_records_done_senders_and_same_day_noop():
    async with World(config=dict(AUTO_CFG)) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
        _seed_users(inst)
        inst.provider.replies[:] = [DIARY_TEXT, DIARY_TEXT]

        await plugin._auto_diary_tick()
        state = plugin._load_auto_state()
        entries_a = inst.entries(SENDER_A)
        entries_b = inst.entries(SENDER_B)
        calls_after_first = len(inst.provider.calls)

        # 同一天再 tick：两个用户都已完成 -> 一整轮 no-op（不重复写、不重复通知）
        await plugin._auto_diary_tick()
        calls_after_second = len(inst.provider.calls)
        entries_a_after = inst.entries(SENDER_A)
        entries_b_after = inst.entries(SENDER_B)

        assert state["last_trigger_day"] == DAY
        assert state["done_senders"] == [SENDER_A, SENDER_B], (
            f"当天完成集合应记录两个已写用户：{state!r}"
        )
        assert entries_a == [f"diaries/{DAY}.md"]
        assert entries_b == [f"diaries/{DAY}.md"]
        assert calls_after_first == 2
        assert calls_after_second == calls_after_first, "同一天第二次 tick 不应再调 LLM"
        assert entries_a_after == entries_a and entries_b_after == entries_b


async def test_review_v062_auto_cancel_midway_resumes_unfinished_users():
    async with World(config=dict(AUTO_CFG)) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
        _seed_users(inst)
        inst.provider.replies[:] = [DIARY_TEXT, DIARY_TEXT]

        original_write = plugin._write_diary
        cancelled = {"raised": False}

        async def flaky_write(umo, sender_id, sender_name, **kwargs):
            if str(sender_id) == SENDER_B and not cancelled["raised"]:
                # 模拟插件重载/禁用时后台任务被 cancel：A 已写完，B 还没轮到
                cancelled["raised"] = True
                raise asyncio.CancelledError
            return await original_write(umo, sender_id, sender_name, **kwargs)

        plugin._write_diary = flaky_write
        try:
            await plugin._auto_diary_tick()
        except asyncio.CancelledError:
            pass

        state_after_cancel = plugin._load_auto_state()
        entries_a_after = inst.entries(SENDER_A)
        entries_b_after = inst.entries(SENDER_B)
        calls_after_cancel = len(inst.provider.calls)

        # 重载后同一天的后续 tick：只补写没轮到的 B，A 不重复写
        await plugin._auto_diary_tick()
        state_final = plugin._load_auto_state()
        entries_a_final = inst.entries(SENDER_A)
        entries_b_final = inst.entries(SENDER_B)
        calls_final = len(inst.provider.calls)

        assert cancelled["raised"] is True
        assert state_after_cancel["done_senders"] == [SENDER_A], (
            "被取消时只有已写完的用户能记完成，未轮到的 B 必须算未完成："
            f"{state_after_cancel!r}"
        )
        assert entries_a_after == [f"diaries/{DAY}.md"]
        assert entries_b_after == []
        assert calls_after_cancel == 1

        assert state_final["done_senders"] == [SENDER_A, SENDER_B], (
            f"补写完成后完成集合应补齐：{state_final!r}"
        )
        assert entries_a_final == [f"diaries/{DAY}.md"], "补写不应让 A 重复写一篇"
        assert entries_b_final == [f"diaries/{DAY}.md"], "重载后当天必须把 B 补写上"
        assert calls_final == 2


async def test_review_v062_auto_same_day_continues_unfinished_past_grace():
    """重载发生在很晚（已过 grace）也应补写当天未完成的用户。"""
    async with World(config=dict(AUTO_CFG)) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_now(FIXED_NOW.replace(hour=23, minute=30))  # 距 22:00 远超 125s
        _seed_users(inst)
        inst.provider.replies[:] = [DIARY_TEXT]
        # 模拟重载前已触发并完成 A，状态从磁盘恢复
        plugin._save_auto_state(
            {
                "last_trigger_day": DAY,
                "last_trigger_at": "2026-10-23 22:00:00",
                "done_senders": [SENDER_A],
            }
        )

        await plugin._auto_diary_tick()

        state = plugin._load_auto_state()
        assert inst.entries(SENDER_A) == [], "A 已完成，不应补写"
        assert inst.entries(SENDER_B) == [f"diaries/{DAY}.md"], (
            "当天已触发过、且 A 已完成时，晚到的 tick 仍应补写未完成的 B"
        )
        assert state["done_senders"] == [SENDER_A, SENDER_B]
        assert len(inst.provider.calls) == 1


async def test_review_v062_auto_user_registered_after_trigger_not_written():
    """触发之后才登记的用户不属于本轮：不在 grace 窗口之外的深夜被补写。"""
    async with World(config=dict(AUTO_CFG)) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
        plugin._auto_users = {SENDER_A: {"umo": UMO_A, "name": "阿A"}}
        inst.conversation_manager.set(UMO_A, history=[message("user", "A 的素材")])

        await plugin._auto_diary_tick()
        calls_after_trigger = len(inst.provider.calls)
        state_after_trigger = plugin._load_auto_state()

        # 22:30 才有新用户来聊天并登记（已经过了 22:00 和补写窗口）
        inst.set_now(FIXED_NOW.replace(hour=22, minute=30))
        plugin._auto_users[SENDER_B] = {"umo": UMO_B, "name": "阿B"}
        inst.conversation_manager.set(UMO_B, history=[message("user", "B 的素材")])
        await plugin._auto_diary_tick()

        assert calls_after_trigger == 1
        assert state_after_trigger["pending_senders"] == [SENDER_A], (
            "触发时应记录当时已登记的用户快照"
        )
        assert inst.entries(SENDER_B) == [], (
            "触发后才登记的用户不应在当天深夜被自动补写（超过 grace 不补写是既有设计）"
        )
        assert len(inst.provider.calls) == 1


async def test_review_v062_auto_old_state_file_is_backward_compatible():
    """旧格式状态（只有 last_trigger_day）照样能读：同一天视为已全部处理完。"""
    async with World(config=dict(AUTO_CFG)) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
        _seed_users(inst)
        plugin._save_auto_state(
            {
                "last_trigger_day": DAY,
                "last_trigger_at": "2026-10-23 22:00:00",
            }
        )

        await plugin._auto_diary_tick()

        state = plugin._load_auto_state()
        assert inst.entries(SENDER_A) == [] and inst.entries(SENDER_B) == []
        assert inst.provider.calls == [], "旧状态说今天已触发，不应重新写"
        assert "done_senders" not in state, "旧状态文件不应被改写成新格式（避免误补写）"


async def test_review_v062_auto_failed_user_marked_done_not_retried():
    """失败不重试的既有边界保留：失败也记完成，当天后续 tick 不再试。"""
    async with World(config=dict(AUTO_CFG)) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
        plugin._auto_users = {SENDER_A: {"umo": UMO_A, "name": "阿A"}}
        inst.conversation_manager.set(UMO_A, history=[message("user", "有素材")])
        inst.provider.fail_with = "boom"

        await plugin._auto_diary_tick()
        state = plugin._load_auto_state()
        sent = list(inst.ctx.sent_messages)
        calls_after_failure = len(inst.provider.calls)

        # 同一天后续 tick（provider 已恢复）：不能再重试
        inst.provider.fail_with = None
        inst.provider.replies[:] = [DIARY_TEXT]
        await plugin._auto_diary_tick()

        assert state["done_senders"] == [SENDER_A]
        assert inst.entries(SENDER_A) == []
        assert calls_after_failure == 1
        assert len(inst.provider.calls) == 1, "失败用户不应被后续 tick 重试"
        assert len(sent) == 1 and "今天的自动日记没能写成" in sent[0][1]


# ---------------------------------------------------------------------------
# P3-10：错过补写窗口要留 warning（且不刷屏）
# ---------------------------------------------------------------------------


async def test_review_v062_auto_warns_once_when_grace_window_missed():
    # 场景 1：插件启动时就已经过了补写窗口（启动太晚，既有设计不补写）
    async with World(config=dict(AUTO_CFG)) as world:
        inst = world.refactor
        plugin = inst.plugin
        service_module = _service_module(plugin)
        recorder = _RecordingLogger()
        original_logger = service_module.logger
        service_module.logger = recorder
        try:
            inst.set_now(FIXED_NOW.replace(hour=22, minute=3))  # 晚 180s > 125s
            _seed_users(inst, with_material=False)
            await plugin._auto_diary_tick()
            first = [m for m in recorder.warnings() if "自动写日记跳过" in m]
            await plugin._auto_diary_tick()  # 每 20 秒一次，不能重复刷
            second = [m for m in recorder.warnings() if "自动写日记跳过" in m]
            state_exists = plugin._auto_diary_state_path().exists()
        finally:
            service_module.logger = original_logger
        assert len(first) == 1, f"过 grace 必须留一条 warning：{recorder.records!r}"
        assert DAY in first[0] and "125" in first[0]
        assert "启动" in first[0], f"启动太晚要说明原因：{first[0]!r}"
        assert len(second) == 1, "同一天只警告一次，不能每 20 秒刷屏"
        assert state_exists is False, "跳过不落状态文件，第二天照常触发"

    # 场景 2：到点前一直在线 tick，22:00 后被阻塞超 grace（事件循环阻塞）
    async with World(config=dict(AUTO_CFG)) as world:
        inst = world.refactor
        plugin = inst.plugin
        service_module = _service_module(plugin)
        recorder = _RecordingLogger()
        original_logger = service_module.logger
        service_module.logger = recorder
        try:
            inst.set_now(FIXED_NOW.replace(hour=21, minute=59))
            await plugin._auto_diary_tick()  # 到点前：只记上次检查时间
            inst.set_now(FIXED_NOW.replace(hour=22, minute=3))
            await plugin._auto_diary_tick()
            warnings = [m for m in recorder.warnings() if "自动写日记跳过" in m]
            state_exists = plugin._auto_diary_state_path().exists()
        finally:
            service_module.logger = original_logger
        assert len(warnings) == 1
        assert "事件循环" in warnings[0], (
            f"到点前有过检查、之后错过窗口，应提示事件循环阻塞：{warnings[0]!r}"
        )
        assert state_exists is False


# ---------------------------------------------------------------------------
# P3-24：插图提示按 sender 分键（多人并发写日记不串号）
# ---------------------------------------------------------------------------


class _PerSenderProvider(FakeProvider):
    """写日记时按昵称返回不同张数的照片标记；人设通知调用返回固定句。"""

    async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
        self.calls.append(
            {
                "prompt": prompt or "",
                "system_prompt": system_prompt or "",
                "kwargs": {k: v for k, v in kwargs.items() if k == "contexts"},
            }
        )
        if "口头通知" in (system_prompt or ""):
            return FakeLLMResponse("她轻声说：日记写好了。")
        if "阿A" in (prompt or ""):
            body = "A 的日记正文。\n〔用户照片：1｜A 拍的晚霞〕\n结尾。"
        else:
            body = (
                "B 的日记正文。\n〔用户照片：1｜B 的照片一〕\n"
                "〔用户照片：2｜B 的照片二〕\n结尾。"
            )
        return FakeLLMResponse(body)


def _photo_message(index: int, text: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": data_url(png(60 + index))}},
        ],
    }


async def test_review_v062_image_notice_not_crossed_between_concurrent_writers():
    """A 的写流程 await 期间交错 B 的写流程：A 的通知里不得出现 B 的插图提示。"""
    config = dict(img_config(user_image_limit=2, image_limit_full=0))
    config.update(
        {
            "use_persona": True,
            "diary_op_interval": 0,
            "write_min_ratio": 0,
            "max_full_diaries_per_day": 0,
        }
    )
    async with World(config=config) as world:
        inst = world.refactor
        plugin = inst.plugin
        provider = _PerSenderProvider()
        inst.provider = provider
        inst.ctx.provider = provider

        # A 只发 1 张照片、B 发 2 张：串号时张数会明显错位
        inst.conversation_manager.set(
            UMO_A, history=[_photo_message(1, "A 发来晚霞")], cid="cid-a"
        )
        inst.conversation_manager.set(
            UMO_B,
            history=[_photo_message(1, "B 的照片一"), _photo_message(2, "B 的照片二")],
            cid="cid-b",
        )

        original_notice = plugin._in_character_notice
        a_waiting = asyncio.Event()
        release_a = asyncio.Event()

        async def gated_notice(umo, situation):
            # 卡住 A 的"人设口吻通知" await：此时 A 的插图提示已登记完毕，
            # 让 B 的写 + 通知完整跑完，再放 A 回来读自己的提示。
            if umo == UMO_A:
                a_waiting.set()
                await release_a.wait()
            return await original_notice(umo, situation)

        plugin._in_character_notice = gated_notice

        event_a = _private_event("/日记 完整", SENDER_A, "阿A", UMO_A)
        event_b = _private_event("/日记 完整", SENDER_B, "阿B", UMO_B)
        task_a = asyncio.create_task(inst.run_handler("write_diary_cmd", event_a))
        task_b = asyncio.create_task(inst.run_handler("write_diary_cmd", event_b))
        try:
            await asyncio.wait_for(a_waiting.wait(), timeout=10)
            texts_b = await asyncio.wait_for(task_b, timeout=10)
        finally:
            release_a.set()
        texts_a = await asyncio.wait_for(task_a, timeout=10)

        text_a = "\n".join(texts_a)
        text_b = "\n".join(texts_b)
        assert "收了你发来的 1 张照片" in text_a, (
            f"A 的通知应带自己的 1 张照片提示：{text_a!r}"
        )
        assert "2 张照片" not in text_a, f"A 的通知串到了 B 的提示：{text_a!r}"
        assert "收了你发来的 2 张照片" in text_b, (
            f"B 的通知应带自己的 2 张照片提示：{text_b!r}"
        )
        assert "1 张照片" not in text_b, f"B 的通知串到了 A 的提示：{text_b!r}"
        # 两边都读一次即清，不应有残留提示给下一次通知
        assert plugin._image_notice_pending() == {}


async def test_review_v062_image_notice_put_and_take_are_per_sender():
    """_image_notice_put/_image_notice_take：按 sender 分槽、读一次即清。"""
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        plugin._image_notice_put(SENDER_A, "A 的提示")
        plugin._image_notice_put(SENDER_B, "B 的提示")

        assert plugin._image_notice_take(SENDER_A) == "A 的提示"
        assert plugin._image_notice_take(SENDER_B) == "B 的提示"
        assert plugin._image_notice_take(SENDER_A) is None, "读一次后应清掉该 sender 的槽"

        # 空串也落键：表示"本篇没有提示"，避免误吞别的来源
        plugin._image_notice_put(SENDER_A, "")
        assert plugin._image_notice_take(SENDER_A) == ""
