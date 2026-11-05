# -*- coding: utf-8 -*-
"""v0.6.2 审查修复（illustration 侧）回归。

覆盖报告的 5 条 + 一条跨文件小活：

- P3-14 插图/照片/形象标记正则排除换行 + payload 上限，不合法整段保留不解析；
- P3-15 每日额度按原记账日期退款（契约 2 的调用侧：consume_day → usage_day）；
- P3-16 形象描述提炼的 LLM 调用加 60s 超时，超时/异常降级返回空串；
- P3-18 _set_config_value 先备份 → 落盘成功才生效 → 失败回滚并返回 False；
- P2-7  生图落盘前同步复查 entry/pending_ref，删除/重写竞态下不写图、不推送、退额度；
- P3-24 插图提示按 sender 分键（_image_notice_put），不再写插件级 _image_last_notice。

只驱动 refactor 侧，不联网：生图通道用 monkeypatch 替换 generate_image。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from harness.world import World
from image_testkit import img_config, png

SENDER = "10001"
OTHER = "20002"
DAY = "20261023"
NEXT_DAY = "20261024"
UMO = "aiocqhttp:FriendMessage:10001"


def _plugin(world):
    return world.refactor.plugin


def _save_pending_entry(plugin, body="正文。\n〔插图：夜色｜竖〕\n结尾。"):
    """写一篇带 1 个 PENDING 占位的日记；返回 (entry_name, pending_ref)。"""
    parsed, decisions = plugin._parse_image_markers(body, 1)
    entry = plugin._save_diary(
        SENDER, parsed, sender_name="阿罗", origin="private", mode="full"
    )
    return entry, decisions[0]["pending_ref"]


async def _ok_generate(prompt, size, ref_path=""):
    return png(77)


def _push_texts(inst) -> list:
    return [text for _session, text in inst.ctx.sent_messages]


# ---------------------------------------------------------------------------
# P3-14：标记正则不得跨行吞正文 / payload 超长整段保留
# ---------------------------------------------------------------------------


async def test_ai_marker_missing_bracket_does_not_swallow_body():
    """模型漏写 "〕" 时，正则不能跨行把后续整段正文吞进 alt 并截断丢失。"""
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        content = (
            "开头。\n"
            "〔插图：窗边的黄昏\n"
            "后面这一整段正文不能被吞掉。\n"
            "结尾。"
        )
        parsed, decisions = plugin._parse_image_markers(content, 2)
        assert decisions == [], f"跨行标记不应产生决策：{decisions!r}"
        assert parsed == content, f"不合法标记应整段原样保留：{parsed!r}"
        assert "后面这一整段正文不能被吞掉。" in parsed
        assert "结尾。" in parsed
        # 也不能算成 AI 的插图意愿（否则会白白占额度/进闸门）
        assert plugin._count_ai_image_markers(content) == 0


async def test_ai_marker_payload_length_limit_boundary():
    """payload ≤80 字仍正常解析；超过 80 字整段保留、不解析、不计数。"""
    async with World(config=img_config()) as world:
        plugin = _plugin(world)

        at_limit = "描" * 80
        parsed_ok, decisions_ok = plugin._parse_image_markers(
            f"正文。〔插图：{at_limit}〕结尾。", 1
        )
        assert len([d for d in decisions_ok if d.get("kind") == "ai"]) == 1, (
            f"80 字 payload 应正常解析：{decisions_ok!r}"
        )
        assert "PENDING" in parsed_ok and "〔插图" not in parsed_ok

        over_limit = "描" * 81
        content = f"正文。〔插图：{over_limit}〕结尾。"
        parsed, decisions = plugin._parse_image_markers(content, 1)
        assert decisions == [], f"超长 payload 不应解析：{decisions!r}"
        assert parsed == content, "超长 payload 的整段标记应原样保留"
        assert plugin._count_ai_image_markers(content) == 0


async def test_photo_and_persona_markers_also_exclude_newlines():
    """同一类标记（用户照片 / 形象提议）跨行同样不得吞正文。"""
    async with World(config=img_config()) as world:
        plugin = _plugin(world)

        persona = "正文。\n〔形象提议：加一条\n银色发带〕\n结尾。"
        parsed, decisions = plugin._parse_image_markers(persona, 1)
        assert decisions == [], f"跨行形象提议不应解析：{decisions!r}"
        assert parsed == persona
        assert "银色发带" in parsed and "结尾。" in parsed

        photo = "正文。\n〔用户照片：1｜她拍的\n晚霞〕\n结尾。"
        new_content, next_seq = plugin._apply_user_photo_markers(
            SENDER, photo, [], DAY, 1
        )
        assert new_content == photo, f"跨行照片标记应原样保留：{new_content!r}"
        assert next_seq == 1


# ---------------------------------------------------------------------------
# P3-15：额度退款按原记账日期（契约 2 的调用侧）
# ---------------------------------------------------------------------------


async def test_schedule_illustrations_passes_consume_day_as_usage_day():
    """调度时捕获 consume 返回的记账日期，作为关键字参数传给后台任务。"""
    async with World(
        config=img_config(persona_image_desc="银发红瞳，白色长裙。")
    ) as world:
        plugin = _plugin(world)
        captured = {}

        async def fake_job(**kwargs):
            captured.update(kwargs)

        plugin._run_image_job = fake_job
        decisions = [
            {
                "kind": "ai",
                "alt": "夜色",
                "orientation": "portrait",
                "pending_ref": "![夜色](images/PENDING-0123abcd.png)",
                "marker": "〔插图：夜色｜竖〕",
            }
        ]
        await plugin._schedule_illustrations(
            SENDER, "阿罗", UMO, DAY, "full", decisions, plugin._image_cfg()
        )
        await asyncio.sleep(0)  # 让 create_task 起的假任务跑完

        assert captured.get("usage_day") == DAY, (
            f"后台任务没拿到记账日期 usage_day：{captured.get('usage_day')!r}"
        )
        # 位置参数契约不变：cfg 仍是第 9 个位置参数（这里以关键字传入）
        assert "cfg" in captured and isinstance(captured["cfg"], dict)
        assert plugin._image_usage_load()["count"] == 1


async def test_cross_midnight_refund_does_not_eat_new_day_quota():
    """占用在前一天、失败跨到第二天：退款不得扣减新一天的计数。"""
    async with World(config=img_config(image_limit_daily=6)) as world:
        inst = world.refactor
        plugin = _plugin(world)
        _entry, pending = _save_pending_entry(plugin)
        cfg = plugin._image_cfg()

        day1 = plugin._image_usage_consume()
        assert day1 == DAY

        inst.now = datetime(2026, 10, 24, 0, 30, 0)  # 跨午夜
        day2 = plugin._image_usage_consume()
        assert day2 == NEXT_DAY
        assert plugin._image_usage_load() == {"date": NEXT_DAY, "count": 1}

        async def failing_generate(prompt, size, ref_path=""):
            return None

        plugin.generate_image = failing_generate
        await plugin._run_image_job(
            sender_id=SENDER, sender_name="阿罗", umo=UMO, day=DAY,
            pending_ref=pending, alt="夜色", orientation="portrait",
            prompt="夜色", cfg=cfg, usage_day=day1,
        )
        assert plugin._image_usage_load() == {"date": NEXT_DAY, "count": 1}, (
            "跨午夜失败退款扣掉了新一天的额度（应按原记账日 day1 退）"
        )


# ---------------------------------------------------------------------------
# P3-16：形象描述提炼的 LLM 调用必须有超时
# ---------------------------------------------------------------------------


class _SlowProvider:
    async def text_chat(self, **kwargs):
        await asyncio.sleep(5)
        return type("Resp", (), {"completion_text": "慢死了"})

    async def get_using_provider_async(self, umo=None):
        return self


async def test_propose_persona_image_desc_times_out_and_degrades():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        timeout_seen = []
        real_wait_for = asyncio.wait_for

        async def spy_wait_for(aw, timeout=None):
            timeout_seen.append(timeout)
            return await real_wait_for(aw, timeout=0.01)

        async def fake_provider(umo=None):
            return _SlowProvider()

        async def fake_persona(umo):
            return "银发红瞳，白色长裙的少女"

        plugin.context.get_using_provider_async = fake_provider
        plugin._get_persona_prompt = fake_persona
        asyncio.wait_for = spy_wait_for
        try:
            loop = asyncio.get_running_loop()
            start = loop.time()
            desc = await plugin._propose_persona_image_desc(UMO)
            elapsed = loop.time() - start
        finally:
            asyncio.wait_for = real_wait_for

        assert desc == "", f"超时应降级返回空串，实际 {desc!r}"
        assert timeout_seen == [60], (
            f"应与批注路径同口径使用 60s 超时，实际 {timeout_seen!r}"
        )
        assert elapsed < 2, f"超时后应尽快返回，实际耗时 {elapsed:.1f}s"


async def test_propose_persona_image_desc_llm_error_degrades():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)

        class _BoomProvider:
            async def text_chat(self, **kwargs):
                raise RuntimeError("上游 500")

        async def fake_provider(umo=None):
            return _BoomProvider()

        async def fake_persona(umo):
            return "银发红瞳的少女"

        plugin.context.get_using_provider_async = fake_provider
        plugin._get_persona_prompt = fake_persona
        assert await plugin._propose_persona_image_desc(UMO) == ""


# ---------------------------------------------------------------------------
# P3-18：_set_config_value 落盘失败回滚（内存与返回值一致）
# ---------------------------------------------------------------------------


class _FakeConfig(dict):
    """模拟 AstrBotConfig：有 save_config()，可按模式抛异常/返回 False。"""

    def __init__(self, data=None, mode="ok"):
        super().__init__(data or {})
        self.mode = mode
        self.saves = 0

    def save_config(self):
        self.saves += 1
        if self.mode == "raise":
            raise OSError("磁盘写不进去")
        if self.mode == "false":
            return False
        return None


async def test_set_config_value_rolls_back_when_save_config_raises():
    async with World(config=img_config(persona_image_desc="旧形象")) as world:
        plugin = _plugin(world)
        cfg = _FakeConfig({"persona_image_desc": "旧形象"}, mode="raise")
        plugin.config = cfg

        assert plugin._set_config_value("persona_image_desc", "新形象") is False
        assert cfg["persona_image_desc"] == "旧形象", (
            "落盘失败后内存里的配置必须回滚，否则提示与行为矛盾"
        )
        assert plugin._persona_image_desc() == "旧形象"
        assert cfg.saves == 1


async def test_set_config_value_rolls_back_when_save_config_returns_false():
    async with World(config=img_config(persona_image_desc="旧形象")) as world:
        plugin = _plugin(world)
        cfg = _FakeConfig({"persona_image_desc": "旧形象"}, mode="false")
        plugin.config = cfg

        assert plugin._set_config_value("persona_image_desc", "新形象") is False
        assert cfg["persona_image_desc"] == "旧形象"
        # 原本不存在的键也不能在失败后残留在内存里
        assert plugin._set_config_value("image_style_prompt", "水墨淡彩") is False
        assert "image_style_prompt" not in cfg


async def test_set_config_value_only_commits_after_successful_save():
    async with World(config=img_config(persona_image_desc="旧形象")) as world:
        plugin = _plugin(world)
        cfg = _FakeConfig({"persona_image_desc": "旧形象"}, mode="ok")
        plugin.config = cfg

        assert plugin._set_config_value("persona_image_desc", "新形象") is True
        assert cfg["persona_image_desc"] == "新形象"
        assert cfg.saves == 1


async def test_apply_pending_persona_desc_keeps_pending_when_save_fails():
    async with World(config=img_config(persona_image_desc="旧形象")) as world:
        plugin = _plugin(world)
        assert plugin._save_pending_persona_desc(
            SENDER, "银发红瞳，白色长裙。", kind="init"
        )
        plugin.config = _FakeConfig({"persona_image_desc": "旧形象"}, mode="raise")

        applied, message = plugin._apply_pending_persona_desc(SENDER)
        assert applied is False
        assert "写入配置失败" in message
        assert plugin.config["persona_image_desc"] == "旧形象"
        assert plugin._load_pending_persona_desc(SENDER).get("desc") == (
            "银发红瞳，白色长裙。"
        ), "落盘失败时待确认提案不能丢（用户还能再确认一次）"


# ---------------------------------------------------------------------------
# P2-7：生图与删除/重写日记竞态
# ---------------------------------------------------------------------------


def _spy_save_image(plugin):
    saved = []
    real_save = plugin.save_image

    def spy(*args, **kwargs):
        saved.append(args)
        return real_save(*args, **kwargs)

    plugin.save_image = spy
    return saved


async def test_run_image_job_does_not_resurrect_deleted_diary():
    """整篇删除后生图完成：不写图、不重建 zip、不推送、额度退还。"""
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        entry, pending = _save_pending_entry(plugin)
        cfg = plugin._image_cfg()
        consume_day = plugin._image_usage_consume()
        assert consume_day == DAY

        # 模拟生图期间用户 /删除日记：全删光 → zip 被 unlink
        assert plugin._delete_entries(SENDER, [entry]) == 1
        assert not inst.diary_path(SENDER).exists()

        saved = _spy_save_image(plugin)
        plugin.generate_image = _ok_generate
        await plugin._run_image_job(
            sender_id=SENDER, sender_name="阿罗", umo=UMO, day=DAY,
            pending_ref=pending, alt="夜色", orientation="portrait",
            prompt="夜色", cfg=cfg, usage_day=consume_day,
        )

        assert saved == [], "目标日记已删除，不应再调用 save_image"
        assert not inst.diary_path(SENDER).exists(), (
            "已删除的日期不能因为生图任务复活成孤儿 zip"
        )
        assert plugin.list_images(SENDER) == []
        assert plugin._image_usage_load()["count"] == 0, "放弃写入应退还额度"
        assert not any("插图" in text for text in _push_texts(inst)), (
            f"不应推送「插图到了」：{_push_texts(inst)!r}"
        )


async def test_run_image_job_aborts_when_entry_rewritten_without_pending():
    """删后同日重写：entry 还在但已不含 pending_ref → 不写图、退额度。"""
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        entry, pending = _save_pending_entry(plugin)
        cfg = plugin._image_cfg()
        consume_day = plugin._image_usage_consume()
        assert consume_day == DAY

        plugin._delete_entries(SENDER, [entry])
        plugin._save_diary(
            SENDER, "重写后的正文，没有插图。",
            sender_name="阿罗", origin="private", mode="full",
        )
        assert plugin._md_entry(DAY) == entry
        assert pending not in plugin._load_entry(SENDER, entry)[0]

        saved = _spy_save_image(plugin)
        plugin.generate_image = _ok_generate
        await plugin._run_image_job(
            sender_id=SENDER, sender_name="阿罗", umo=UMO, day=DAY,
            pending_ref=pending, alt="夜色", orientation="portrait",
            prompt="夜色", cfg=cfg, usage_day=consume_day,
        )

        assert saved == [], "正文已不含占位，不应写入图片"
        assert plugin.list_images(SENDER) == []
        assert "重写后的正文，没有插图。" in plugin._load_entry(SENDER, entry)[0]
        assert plugin._image_usage_load()["count"] == 0
        assert not any("插图" in text for text in _push_texts(inst))


async def test_run_image_job_refunds_when_replace_pending_returns_false():
    """契约 1：replace_pending_images 返回 False（没改动正文）也要退额度、不推送。"""
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        entry, pending = _save_pending_entry(plugin)
        cfg = plugin._image_cfg()
        consume_day = plugin._image_usage_consume()
        assert consume_day == DAY

        plugin.replace_pending_images = lambda *args, **kwargs: False
        plugin.generate_image = _ok_generate
        await plugin._run_image_job(
            sender_id=SENDER, sender_name="阿罗", umo=UMO, day=DAY,
            pending_ref=pending, alt="夜色", orientation="portrait",
            prompt="夜色", cfg=cfg, usage_day=consume_day,
        )

        assert plugin._image_usage_load()["count"] == 0, (
            "replace_pending_images 返回 False（图没进正文）时必须退还额度"
        )
        assert not any("插图" in text for text in _push_texts(inst)), (
            "正文没替换成功却仍推送「插图到了」"
        )
        assert pending in plugin._load_entry(SENDER, entry)[0]


# ---------------------------------------------------------------------------
# P3-24：插图提示按 sender 分键，不再写插件级共享字段
# ---------------------------------------------------------------------------


async def test_image_notice_is_isolated_per_sender():
    async with World(
        config=img_config(persona_image_desc="银发红瞳，白色长裙。")
    ) as world:
        plugin = _plugin(world)
        decisions = [{"kind": "persona_proposal", "desc": "加一条银色发带"}]
        await plugin._schedule_illustrations(
            SENDER, "阿罗", UMO, DAY, "full", decisions, plugin._image_cfg()
        )
        pending = plugin._image_notice_pending()
        assert "银色发带" in pending.get(SENDER, ""), (
            f"提示应登记在写作者自己的槽位：{pending!r}"
        )
        assert OTHER not in pending, "别人的槽位不能被写入"
        assert plugin._image_last_notice == "", (
            "不应再写插件级共享字段 _image_last_notice"
        )
        # take 只清自己的槽位
        assert "银色发带" in (plugin._image_notice_take(SENDER) or "")
        assert plugin._image_notice_pending().get(SENDER, "") == ""
        assert plugin._image_notice_take(SENDER) is None


async def test_quota_notice_goes_to_own_sender_slot():
    async with World(
        config=img_config(
            persona_image_desc="银发红瞳，白色长裙。", image_limit_daily=0
        )
    ) as world:
        plugin = _plugin(world)
        _save_pending_entry(plugin)
        decisions = [
            {
                "kind": "ai",
                "alt": "夜色",
                "orientation": "portrait",
                "pending_ref": "![夜色](images/PENDING-0123abcd.png)",
                "marker": "〔插图：夜色｜竖〕",
            }
        ]
        await plugin._schedule_illustrations(
            SENDER, "阿罗", UMO, DAY, "full", decisions, plugin._image_cfg()
        )
        pending = plugin._image_notice_pending()
        assert "额度" in pending.get(SENDER, ""), f"额度提示没进自己的槽位：{pending!r}"
        assert OTHER not in pending
