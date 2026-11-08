# -*- coding: utf-8 -*-
"""v0.7.5：reload 不得泄漏自动日记循环或重复触发同一天写作。"""

from __future__ import annotations

import asyncio
import copy

import memory_diary.service as service_module
from harness.world import DIARY_TEXT, FIXED_NOW, World, message
from memory_diary.service import ServiceMixin

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"
DAY = "20261023"


class _RecordingLogger:
    def __init__(self):
        self.messages = []

    def __getattr__(self, level):
        def _record(message, *args, **kwargs):
            self.messages.append(str(message))

        return _record


def _dummy_service() -> ServiceMixin:
    service = ServiceMixin()
    service._terminated = False
    service._auto_task = None
    service.config = {"enable_auto_diary": False}
    return service


def _live_auto_loops():
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done()
        and (getattr(task.get_coro(), "__qualname__", "") or "").endswith(
            "._auto_diary_loop"
        )
    ]


async def _cancel_service_tasks(services) -> None:
    tasks = []
    for service in services:
        service._terminated = True
        task = getattr(service, "_auto_task", None)
        if task is not None and not task.done():
            task.cancel()
            tasks.append(task)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    service_module._AUTO_TASK_HOLDER.update(task=None, owner=None)


async def test_reload_three_times_leaves_only_one_auto_diary_loop():
    logger = _RecordingLogger()
    original_logger = service_module.logger
    service_module.logger = logger
    services = [_dummy_service() for _ in range(3)]
    try:
        for service in services:
            service._start_auto_diary_task()
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        live = _live_auto_loops()
        assert live == [services[-1]._auto_task]

        services[-1]._start_auto_diary_task()
        await asyncio.sleep(0)
        assert _live_auto_loops() == [services[-1]._auto_task]
        assert any(
            "已清理" in message and "个旧实例泄漏的自动日记循环" in message
            for message in logger.messages
        )
    finally:
        await _cancel_service_tasks(services)
        service_module.logger = original_logger


async def test_task_scan_skips_unknown_owner_instead_of_cancelling():
    class UnknownOwnerCoroutine:
        __qualname__ = "ServiceMixin._auto_diary_loop"
        cr_frame = None

    class FakeTask:
        cancelled = False

        def done(self):
            return False

        def get_coro(self):
            return UnknownOwnerCoroutine()

        def cancel(self):
            self.cancelled = True

    service = _dummy_service()
    unknown = FakeTask()
    original_all_tasks = service_module.asyncio.all_tasks
    service_module.asyncio.all_tasks = lambda: {unknown}
    try:
        service._start_auto_diary_task()
    finally:
        service_module.asyncio.all_tasks = original_all_tasks
    assert unknown.cancelled is False
    assert service._auto_task is not None
    await _cancel_service_tasks([service])


def test_start_without_running_event_loop_skips_safely():
    service = _dummy_service()
    service._start_auto_diary_task()
    assert service._auto_task is None


async def test_terminated_loop_exits_and_logs_on_next_iteration():
    logger = _RecordingLogger()
    original_logger = service_module.logger
    original_sleep = service_module.asyncio.sleep
    service_module.logger = logger

    async def immediate_sleep(_seconds):
        await original_sleep(0)

    service = _dummy_service()
    service._terminated = True
    service_module.asyncio.sleep = immediate_sleep
    try:
        task = asyncio.create_task(service._auto_diary_loop())
        await original_sleep(0)
        await original_sleep(0)
        await task
        assert task.done() and not task.cancelled()
        assert "[memory_diary] 插件已终止，自动写日记后台任务退出" in logger.messages
    finally:
        service_module.asyncio.sleep = original_sleep
        service_module.logger = original_logger


async def test_three_stale_instances_concurrent_tick_write_and_notify_once():
    async with World(
        config={
            "use_persona": False,
            "enable_auto_diary": True,
            "auto_diary_time": "22:00",
            "auto_write_notify": True,
        }
    ) as world:
        inst = world.refactor
        original = inst.plugin
        original._auto_users = {SENDER: {"umo": UMO, "name": "阿罗"}}
        inst.set_now(FIXED_NOW.replace(hour=22, minute=0))
        inst.set_history([message("user", "今天聊了考试")])
        inst.provider.replies[:] = [DIARY_TEXT]
        original_text_chat = inst.provider.text_chat

        async def yielding_text_chat(*args, **kwargs):
            await asyncio.sleep(0)
            return await original_text_chat(*args, **kwargs)

        inst.provider.text_chat = yielding_text_chat

        instances = [original, copy.copy(original), copy.copy(original)]
        for plugin in instances:
            plugin._auto_state = {}
            plugin._writing = set()

        await asyncio.gather(*(plugin._auto_diary_tick() for plugin in instances))

        content, tampered = original._load_entry(SENDER, f"diaries/{DAY}.md")
        assert not tampered
        assert len(original._segments(content)) == 1
        assert len(inst.provider.calls) == 1
        assert len(inst.ctx.sent_messages) == 1
        state = original._load_auto_state()
        assert state["last_trigger_day"] == DAY
        assert state["done_senders"] == [SENDER]
