# -*- coding: utf-8 -*-
"""v0.6 D3 写日记接入：闸门 / 标记解析 / 用户照片 / 异步落图 / 推送切段辅助。

设计依据：设计方案第一部分 D3、D6 与《插图功能接口契约》第 2、5 节。
只驱动 refactor 侧，不联网（生图通道用 monkeypatch 替换 generate_image）。
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.world import World, message
from image_testkit import (
    JPG,
    TempDir,
    b64,
    data_url,
    drop_zip_entry,
    img_config,
    png,
    replace_zip_entry,
    rewrite_zip,
    zip_entries,
)

SENDER = "10001"
DAY = "20261023"
OTHER_DAY = "20261022"
UMO = "aiocqhttp:FriendMessage:10001"


def _plugin(world):
    return world.refactor.plugin


def _set_usage(plugin, count: int, day: str = DAY):
    usage_file = Path(plugin.data_dir) / plugin.IMAGE_USAGE_FILE
    usage_file.write_text(json.dumps({"date": day, "count": count}), encoding="utf-8")


# ---------------------------------------------------------------------------
# 总开关
# ---------------------------------------------------------------------------


async def test_image_enabled_flag():
    async with World(config=img_config(image_enabled=False)) as world:
        assert _plugin(world)._image_enabled() is False
    async with World(config=img_config(image_enabled=True)) as world:
        assert _plugin(world)._image_enabled() is True
    async with World(config={}) as world:
        # 配置缺失时默认关闭（设计默认 false）
        assert _plugin(world)._image_enabled() is False


# ---------------------------------------------------------------------------
# _image_gate：AI 意愿 × 概率 × 单篇限额 × 群聊限额 × 每日余量
# ---------------------------------------------------------------------------


async def test_image_gate_probability_and_wanted():
    async with World(config=img_config(image_prob_full=0.0, image_limit_full=2)) as world:
        plugin = _plugin(world)
        assert plugin._image_gate(SENDER, False, "full", 3) == 0, (
            "概率为 0 时不应插图（AI 意愿再高也不画）"
        )
        assert plugin._image_gate(SENDER, False, "full", 0) == 0, "AI 意愿为 0 时应返回 0"

    async with World(config=img_config(image_prob_full=1.0, image_limit_full=2)) as world:
        plugin = _plugin(world)
        assert plugin._image_gate(SENDER, False, "full", 3) == 2, (
            "概率通过但超单篇限额时应截到 image_limit_full"
        )
        assert plugin._image_gate(SENDER, False, "full", 1) == 1
        assert plugin._image_gate(SENDER, False, "full", 0) == 0


async def test_image_gate_brief_and_life_use_memo_settings():
    async with World(
        config=img_config(
            image_prob_full=1.0,
            image_limit_full=2,
            image_prob_memo=1.0,
            image_limit_memo=1,
        )
    ) as world:
        plugin = _plugin(world)
        assert plugin._image_gate(SENDER, False, "brief", 3) == 1, (
            "简记应使用 image_prob_memo / image_limit_memo"
        )
        assert plugin._image_gate(SENDER, False, "life", 3) == 1, (
            "随笔应使用 image_prob_memo / image_limit_memo"
        )

    async with World(
        config=img_config(image_prob_full=1.0, image_prob_memo=0.0, image_limit_memo=2)
    ) as world:
        plugin = _plugin(world)
        assert plugin._image_gate(SENDER, False, "brief", 2) == 0, (
            "简记概率默认 0 时不画"
        )


async def test_image_gate_group_limit():
    async with World(
        config=img_config(
            image_prob_full=1.0, image_limit_full=2, image_limit_group=0
        )
    ) as world:
        plugin = _plugin(world)
        assert plugin._image_gate(SENDER, True, "full", 2) == 0, (
            "群聊限额 0（默认）时群聊不应插图"
        )
        # 私聊不受群聊限额影响
        assert plugin._image_gate(SENDER, False, "full", 2) == 2

    async with World(
        config=img_config(
            image_prob_full=1.0, image_limit_full=2, image_limit_group=1
        )
    ) as world:
        plugin = _plugin(world)
        assert plugin._image_gate(SENDER, True, "full", 2) == 1, (
            "群聊应按 image_limit_group 截断"
        )


async def test_image_gate_daily_remaining_caps_but_does_not_consume():
    async with World(
        config=img_config(
            image_prob_full=1.0,
            image_limit_full=2,
            image_limit_daily=6,
        )
    ) as world:
        plugin = _plugin(world)
        # 已用 5 张，今天只剩 1 张额度
        for _ in range(5):
            assert plugin._image_usage_consume() is True
        assert plugin._image_usage_remaining() == 1

        assert plugin._image_gate(SENDER, False, "full", 2) == 1, (
            "每日余量只剩 1 张时应截到 1"
        )
        # 闸门本身不消耗额度：连续两次结果不变
        assert plugin._image_gate(SENDER, False, "full", 2) == 1
        assert plugin._image_usage_remaining() == 1

        assert plugin._image_usage_consume() is True
        assert plugin._image_gate(SENDER, False, "full", 2) == 0, (
            "每日额度用完后不应再允许插图"
        )
        assert plugin._image_usage_consume() is False

    async with World(config=img_config(image_prob_full=1.0, image_limit_daily=0)) as world:
        plugin = _plugin(world)
        # 实现语义（lead 定稿）：image_limit_daily<=0 表示"今天不画"（全局兜底关闭即不画）。
        # 注意：_conf_schema.json 里该键的 hint 目前写的是"0 = 不限制"，与实现相反，
        # 已作为文档不一致项报给 lead，测试以实现语义为准。
        assert plugin._image_usage_remaining() == 0
        assert plugin._image_usage_consume() is False
        assert plugin._image_gate(SENDER, False, "full", 2) == 0


# ---------------------------------------------------------------------------
# _parse_image_markers：标记解析 / PENDING 占位 / 用户照片标记保留
# ---------------------------------------------------------------------------


def _pending_refs(content: str) -> list:
    import re

    return re.findall(r"!\[[^\]]*\]\(images/PENDING-[0-9a-f]{8}\.png\)", content)


async def test_parse_image_markers_replaces_first_allowed_with_pending():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        content = (
            "第一段。\n"
            "〔插图：窗边的黄昏｜横〕\n"
            "中间。\n"
            "〔插图：夜色｜竖〕\n"
            "〔插图：她的侧影｜方〕\n"
            "〔用户照片：1｜她拍的晚霞〕\n"
            "结尾。"
        )
        parsed, decisions = plugin._parse_image_markers(content, 2)

        assert isinstance(decisions, list)
        ai_decisions = [d for d in decisions if d.get("kind") == "ai"]
        assert len(ai_decisions) == 2, (
            f"allowed=2 应产生 2 个 AI 插图决策，实际 {len(ai_decisions)}：{decisions!r}"
        )

        pendings = _pending_refs(parsed)
        assert len(pendings) == 2, f"allowed=2 应写 2 个 PENDING 占位，实际 {pendings!r}"
        assert len(set(pendings)) == 2, "两个 PENDING 占位 token 重复了"
        for decision, pending in zip(ai_decisions, pendings):
            assert decision.get("pending_ref") == pending, (
                f"决策里的 pending_ref 与正文占位不一致：{decision!r} / {pending!r}"
            )
            assert decision.get("alt"), f"AI 决策缺少 alt：{decision!r}"
            assert "marker" in decision, f"AI 决策缺少原始 marker：{decision!r}"

        # 标记全部从正文移除；超额的第三个插图标记整段删除
        assert "〔插图" not in parsed, f"AI 插图标记没有从正文移除：{parsed!r}"
        assert "〔形象提议" not in parsed
        # 用户照片标记按定稿语义保留，等待 _apply_user_photo_markers 二次扫描
        assert "〔用户照片：1｜她拍的晚霞〕" in parsed, (
            "用户照片标记应保留在正文里交给 _apply_user_photo_markers"
        )
        # 正文文字与顺序保住了
        assert parsed.index("第一段。") < parsed.index("中间。") < parsed.index("结尾。")

        orientations = {d["orientation"] for d in ai_decisions}
        allowed_orientations = {
            "横", "竖", "方", "portrait", "landscape", "square", "vertical", "horizontal",
        }
        assert orientations <= allowed_orientations, (
            f"决策里的方向值不在约定集合内：{orientations!r}"
        )
        assert len(orientations) == 2, (
            f"allowed=2 的两个方向标记应解析出两种方向：{orientations!r}"
        )

        parsed_all, decisions_all = plugin._parse_image_markers(content, 3)
        assert len(_pending_refs(parsed_all)) == 3
        orientations_all = {d["orientation"] for d in decisions_all if d.get("kind") == "ai"}
        assert len(orientations_all) == 3, (
            f"横/竖/方三个方向应解析出三种方向：{orientations_all!r}"
        )


async def test_parse_image_markers_allowed_zero_and_more_markers_than_allowed():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)

        parsed, decisions = plugin._parse_image_markers(
            "正文〔插图：窗边的黄昏｜横〕结尾", 0
        )
        assert _pending_refs(parsed) == [], "allowed=0 时不应出现 PENDING 占位"
        assert "〔插图" not in parsed
        assert [d for d in decisions if d.get("kind") == "ai"] == []
        assert "正文" in parsed and "结尾" in parsed

        parsed2, decisions2 = plugin._parse_image_markers(
            "a〔插图：一｜横〕b〔插图：二｜竖〕c〔插图：三｜方〕d", 1
        )
        assert len(_pending_refs(parsed2)) == 1, "allowed=1 时只应有 1 个 PENDING 占位"
        assert "〔插图" not in parsed2
        assert len([d for d in decisions2 if d.get("kind") == "ai"]) == 1
        for ch in ("a", "b", "c", "d"):
            assert ch in parsed2


async def test_parse_image_markers_removes_persona_proposal():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        content = "正文。\n〔形象提议：把发色画深一点，加一条银色发带〕\n结尾。"
        parsed, decisions = plugin._parse_image_markers(content, 1)
        assert "〔形象提议" not in parsed, "形象提议标记没有从正文移除"
        assert "把发色画深一点" not in parsed
        assert "正文。" in parsed and "结尾。" in parsed
        assert [d for d in decisions if d.get("kind") == "ai"] == []


# ---------------------------------------------------------------------------
# _collect_user_photos：从会话历史提取用户照片
# ---------------------------------------------------------------------------


def _image_part(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


async def test_collect_user_photos_from_data_url_with_caption():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        img1 = png(21)
        img2 = JPG + b"second-photo"
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "你看我拍的晚霞"},
                    _image_part(data_url(img1)),
                ],
            },
            {"role": "assistant", "content": "好看，像橘子汽水。"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "还有这张，路边的小猫"},
                    _image_part(data_url(img2, "image/jpeg")),
                ],
            },
        ]
        photos = plugin._collect_user_photos(history, 5)
        assert len(photos) == 2, f"应从历史提取 2 张用户照片，实际 {photos!r}"

        first, second = photos
        assert first["index"] == 1 and second["index"] == 2
        assert first["data"] == img1, "第一张照片字节不一致"
        assert first["mime"] == "image/png"
        assert first["caption"] == "你看我拍的晚霞", (
            f"配文应取同一条消息的文本，实际 {first.get('caption')!r}"
        )
        assert "time" in first

        assert second["data"] == img2
        assert second["mime"] == "image/jpeg"
        assert "路边的小猫" in second["caption"]

        # max_n 限制
        only_one = plugin._collect_user_photos(history, 1)
        assert len(only_one) == 1, f"max_n=1 应只返回 1 张，实际 {len(only_one)}"


async def test_collect_user_photos_skips_oversize_and_broken_sources():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        huge = png(23, size=9 * 1024 * 1024)
        history = [
            {"role": "user", "content": [_image_part(data_url(png(24)))]},
            {"role": "user", "content": [_image_part(data_url(huge))]},  # > 8MB 跳过
            {
                "role": "user",
                "content": [_image_part("file:///definitely/not/exists.png")],
            },  # 读不到跳过
        ]
        photos = plugin._collect_user_photos(history, 5)
        assert len(photos) == 1, f"超大/读不到的照片应跳过，实际 {[p.get('index') for p in photos]!r}"
        assert photos[0]["data"] == png(24)


async def test_collect_user_photos_tolerates_malformed_history():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        for history in (
            [],
            None,
            ["不是字典"],
            [{"role": "user", "content": "纯文本"}],
            [{"role": "user", "content": [{"type": "image_url"}]}],
            [{"role": "user", "content": [_image_part("data:image/png;base64,@@@不是base64@@@")]}],
        ):
            photos = plugin._collect_user_photos(history, 3)
            assert photos == [], f"畸形历史不应抛出/不应提取出照片：{history!r} -> {photos!r}"


async def test_collect_user_photos_from_local_file():
    with TempDir() as tmp:
        path = Path(tmp) / "photo.png"
        path.write_bytes(png(25))
        async with World(config=img_config()) as world:
            plugin = _plugin(world)
            for url in (path.as_uri(), str(path)):
                photos = plugin._collect_user_photos(
                    [{"role": "user", "content": [_image_part(url)]}], 3
                )
                assert len(photos) == 1, f"本地路径 {url!r} 没有被读取：{photos!r}"
                assert photos[0]["data"] == png(25)


# ---------------------------------------------------------------------------
# _apply_user_photo_markers：标记 -> 已落盘图片引用
# ---------------------------------------------------------------------------


async def _collect_photos(plugin, data_list, captions):
    history = []
    for data, caption in zip(data_list, captions):
        history.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": caption},
                    _image_part(data_url(data)),
                ],
            }
        )
    return plugin._collect_user_photos(history, len(data_list))


async def test_apply_user_photo_markers_saves_image_and_replaces_marker():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        photos = await _collect_photos(
            plugin, [png(31), png(32)], ["她拍的晚霞", "路边的小猫"]
        )
        plugin._save_diary(
            SENDER, "写日记", sender_name="阿罗", origin="private", mode="full"
        )

        content = "正文开头。\n〔用户照片：1｜她拍的晚霞〕\n结尾。"
        new_content, next_seq = plugin._apply_user_photo_markers(
            SENDER, content, photos, DAY, 1
        )

        assert "〔用户照片" not in new_content, "用户照片标记没有被替换掉"
        assert "![用户分享的照片：她拍的晚霞](images/" in new_content, (
            f"没有生成用户照片引用：{new_content!r}"
        )
        assert next_seq == 2, f"落盘一张照片后 next_seq 应为 2，实际 {next_seq}"

        images = plugin.list_images(SENDER)
        assert len(images) == 1, f"用户照片没有写进 zip：{images!r}"
        data, status = plugin.load_image(SENDER, images[0])
        assert status == "ok" and data == png(31)
        assert images[0] in new_content

        # 第二张照片用序号 2、描述"内容未识别"
        content2 = "开头。\n〔用户照片：2｜内容未识别〕\n结尾。"
        new_content2, next_seq2 = plugin._apply_user_photo_markers(
            SENDER, content2, photos, DAY, next_seq
        )
        assert "〔用户照片" not in new_content2
        assert "![用户分享的照片：内容未识别](images/" in new_content2
        assert next_seq2 == 3, f"第二张落盘后 next_seq 应为 3，实际 {next_seq2}"
        images2 = plugin.list_images(SENDER)
        assert len(images2) == 2
        assert any(
            plugin.load_image(SENDER, n) == (png(32), "ok") for n in images2
        ), "第二张照片字节没有落进 zip"


async def test_apply_user_photo_markers_without_vision_uses_honest_placeholder():
    """无视觉接口时 desc 用"内容未识别"——不臆造画面内容。"""
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        photos = await _collect_photos(plugin, [png(33)], ["发来一张照片"])
        plugin._save_diary(SENDER, "写日记", sender_name="阿罗",
                           origin="private", mode="full")
        content, next_seq = plugin._apply_user_photo_markers(
            SENDER, "正文〔用户照片：1｜内容未识别〕结尾", photos, DAY, 1
        )
        assert "用户分享的照片：内容未识别" in content
        assert next_seq == 2


async def test_apply_user_photo_markers_removes_bad_index_without_crash():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        photos = await _collect_photos(plugin, [png(34)], ["仅有的一张"])
        content, next_seq = plugin._apply_user_photo_markers(
            SENDER, "正文〔用户照片：9｜不存在的照片〕结尾", photos, DAY, 1
        )
        assert "〔用户照片" not in content, "越界序号标记也应从正文移除"
        assert plugin.list_images(SENDER) == [], "越界序号不应写图"
        assert "正文" in content and "结尾" in content
        assert next_seq == 1, f"没有落盘照片时不应递增序号，实际 {next_seq}"


# ---------------------------------------------------------------------------
# PENDING 占位落盘 -> 替换 / 删除
# ---------------------------------------------------------------------------


async def test_pending_placeholder_saved_then_replaced_on_success():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)

        parsed, decisions = plugin._parse_image_markers(
            "日记正文。\n〔插图：窗边的黄昏｜横〕\n结尾。", 1
        )
        pending = decisions[0]["pending_ref"]
        entry = plugin._save_diary(
            SENDER, parsed, sender_name="阿罗", origin="private", mode="full"
        )

        saved, tampered, _ = plugin._load_latest(SENDER)
        assert tampered is False
        assert pending in saved, "PENDING 占位没有随正文落盘"
        assert "PENDING" in zip_entries(inst.diary_path(SENDER))[entry].decode("utf-8")

        final_name = plugin.save_image(SENDER, png(41), DAY, 1, "窗边的黄昏")
        final_ref = f"![窗边的黄昏]({final_name})"
        ok = plugin.replace_pending_images(SENDER, entry, {pending: final_ref}, [])
        assert ok is True, "成功落图后替换 PENDING 占位失败"

        loaded, tampered, _ = plugin._load_latest(SENDER)
        assert tampered is False
        assert final_ref in loaded, f"正文没有替换成最终图片引用：{loaded!r}"
        assert "PENDING" not in loaded
        assert plugin.load_image(SENDER, final_name) == (png(41), "ok")


async def test_pending_placeholder_removed_on_failure():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)

        parsed, decisions = plugin._parse_image_markers(
            "另一天正文。\n〔插图：夜色｜竖〕\n收尾。", 1
        )
        pending = decisions[0]["pending_ref"]
        entry = plugin._save_diary(
            SENDER, parsed, sender_name="阿罗", entry_name_hint=DAY,
            origin="private", mode="full",
        )

        ok = plugin.replace_pending_images(SENDER, entry, {}, [pending])
        assert ok is True, "生图失败时删除 PENDING 占位失败"

        loaded, tampered, _ = plugin._load_latest(SENDER)
        assert tampered is False, "删占位后正文验签失败"
        assert "PENDING" not in loaded, "失败后占位仍留在正文里"
        assert "另一天正文。" in loaded and "收尾。" in loaded
        assert plugin.list_images(SENDER) == [], "生图失败不应凭空多出图片"


async def test_replace_pending_images_refuses_tampered_entry():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        parsed, decisions = plugin._parse_image_markers(
            "正文。\n〔插图：窗边的黄昏｜横〕", 1
        )
        pending = decisions[0]["pending_ref"]
        entry = plugin._save_diary(
            SENDER, parsed, sender_name="阿罗", origin="private", mode="full"
        )

        # 模拟正文被手动改过（不重签）：替换必须拒绝，且不能替改过的正文重签
        entries = zip_entries(inst.diary_path(SENDER))
        entries[entry] = entries[entry] + "\n偷偷加的一行".encode("utf-8")
        rewrite_zip(inst.diary_path(SENDER), entries)

        ok = plugin.replace_pending_images(
            SENDER, entry, {pending: "![x](images/x.png)"}, []
        )
        assert ok is False, "正文被篡改后仍替换 PENDING 占位（会替篡改内容重签）"
        after = zip_entries(inst.diary_path(SENDER))[entry].decode("utf-8")
        assert "偷偷加的一行" in after
        assert pending in after, "拒绝写入时正文不应被改动"


# ---------------------------------------------------------------------------
# _run_image_job：异步任务的成功/失败落点
# ---------------------------------------------------------------------------


async def test_run_image_job_success_saves_image_and_pushes_notice():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        parsed, decisions = plugin._parse_image_markers(
            "正文。\n〔插图：窗边的黄昏｜横〕\n结尾。", 1
        )
        pending = decisions[0]["pending_ref"]
        plugin._save_diary(
            SENDER, parsed, sender_name="阿罗", origin="private", mode="full"
        )

        async def fake_generate(prompt, size, ref_path=""):
            return png(51)

        plugin.generate_image = fake_generate
        # 注意：实现里第 9 个参数是 sem（信号量），cfg 由任务内部 _image_cfg() 取；
        # 用关键字调用避免契约/实现签名差异影响其他断言。
        await plugin._run_image_job(
            sender_id=SENDER, sender_name="阿罗", umo=UMO, day=DAY,
            pending_ref=pending, alt="窗边的黄昏", orientation="landscape",
            prompt="窗边的黄昏", cfg=plugin._image_cfg(),
        )

        images = plugin.list_images(SENDER)
        assert len(images) == 1, f"生图任务成功后 zip 里应有 1 张图，实际 {images!r}"
        assert plugin.load_image(SENDER, images[0]) == (png(51), "ok")
        loaded, tampered, _ = plugin._load_latest(SENDER)
        assert tampered is False
        assert images[0] in loaded and "PENDING" not in loaded
        # 成功推送"插图到了"（主动消息走 context.send_message）
        push_texts = [text for _session, text in inst.ctx.sent_messages]
        assert any("插图" in text for text in push_texts), (
            f"生图成功没有推送插图通知：{push_texts!r}"
        )


async def test_run_image_job_failure_clears_pending_and_keeps_diary_intact():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        parsed, decisions = plugin._parse_image_markers(
            "失败正文。\n〔插图：夜色｜竖〕\n结尾。", 1
        )
        pending = decisions[0]["pending_ref"]
        plugin._save_diary(
            SENDER, parsed, sender_name="阿罗", origin="private", mode="full"
        )

        async def failing_generate(prompt, size, ref_path=""):
            return None

        plugin.generate_image = failing_generate
        await plugin._run_image_job(
            sender_id=SENDER, sender_name="阿罗", umo=UMO, day=DAY,
            pending_ref=pending, alt="夜色", orientation="portrait",
            prompt="夜色", cfg=plugin._image_cfg(),
        )

        assert plugin.list_images(SENDER) == [], "生图失败不应写入图片"
        loaded, tampered, _ = plugin._load_latest(SENDER)
        assert tampered is False, "生图失败后日记必须完整可读"
        assert "PENDING" not in loaded, "生图失败后占位应被清除"
        assert "失败正文。" in loaded and "结尾。" in loaded


# ---------------------------------------------------------------------------
# _split_images_for_send：QQ 端图文穿插切段
# ---------------------------------------------------------------------------


async def test_split_images_for_send_normal_order():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        name = plugin.save_image(SENDER, png(61), DAY, 1, "窗边的黄昏")
        content = f"开头文字。\n![窗边的黄昏]({name})\n结尾文字。"

        segments = plugin._split_images_for_send(SENDER, content)
        assert isinstance(segments, list) and segments
        kinds = [s["type"] for s in segments]
        assert kinds == ["text", "image", "text"], f"图文穿插顺序不对：{segments!r}"

        image_seg = segments[1]
        assert image_seg["name"] == name, f"图片段条目名不对：{image_seg!r}"
        assert image_seg["alt"] == "窗边的黄昏"

        texts = "".join(s["text"] for s in segments if s["type"] == "text")
        assert "开头文字。" in texts and "结尾文字。" in texts


async def test_split_images_for_send_placeholder_when_missing():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        name = plugin.save_image(SENDER, png(62), DAY, 1, "窗边的黄昏")
        content = f"文字一。\n![窗边的黄昏]({name})\n文字二。"
        # 图片条目丢失：优雅降级为 [插图：alt]，不是警告
        drop_zip_entry(inst.diary_path(SENDER), name)

        segments = plugin._split_images_for_send(SENDER, content)
        kinds = [s["type"] for s in segments]
        assert "image" not in kinds, "缺图时不应返回 image 段"
        placeholders = [s for s in segments if s["type"] == "placeholder"]
        assert len(placeholders) == 1, f"缺图应降级为 1 个占位段：{segments!r}"
        assert placeholders[0]["text"] == "[插图：窗边的黄昏]", (
            f"缺图占位文案不符合契约：{placeholders[0]!r}"
        )
        texts = "".join(s.get("text", "") for s in segments)
        assert "文字一。" in texts and "文字二。" in texts


async def test_split_images_for_send_warns_when_tampered():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        name = plugin.save_image(SENDER, png(63), DAY, 1, "窗边的黄昏")
        content = f"文字一。\n![窗边的黄昏]({name})\n文字二。"
        replace_zip_entry(inst.diary_path(SENDER), name, png(64) + b"TAMPERED")

        segments = plugin._split_images_for_send(SENDER, content)
        warnings = [s for s in segments if s["type"] == "warning"]
        assert len(warnings) == 1, f"被换图应产生 1 个警告段：{segments!r}"
        text = warnings[0]["text"]
        assert "⚠" in text, f"篡改警告缺少 ⚠ 标记：{text!r}"
        assert "验签失败" in text, f"篡改警告文案不明确：{text!r}"
        assert "image" not in [s["type"] for s in segments]


async def test_split_images_for_send_pure_text_matches_original():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        for content in ("纯文字日记，没有任何插图。", "", "第一行\n第二行"):
            segments = plugin._split_images_for_send(SENDER, content)
            if not content:
                assert segments in ([], [{"type": "text", "text": ""}]), (
                    f"空正文的分段不合理：{segments!r}"
                )
                continue
            assert all(s["type"] == "text" for s in segments), (
                f"零图片正文不应产生非 text 段：{segments!r}"
            )
            assert "".join(s["text"] for s in segments) == content, (
                f"零图片正文的分段拼接与原文不一致：{segments!r}"
            )


# ---------------------------------------------------------------------------
# _image_prompt_addon / _image_style_summary
# ---------------------------------------------------------------------------


async def test_image_prompt_addon_contains_persona_style_photos_and_markers():
    async with World(
        config=img_config(
            image_style_prompt="水墨淡彩，纸面留白",
            persona_image_desc="银发红瞳，白色长裙，颈间一枚月牙吊坠。",
            image_prob_full=1.0,
            image_limit_full=2,
        )
    ) as world:
        plugin = _plugin(world)
        photos = [
            {
                "index": 1,
                "data": png(71),
                "mime": "image/png",
                "caption": "她拍的晚霞",
                "time": "2026-10-23 19:00",
            }
        ]
        addon = plugin._image_prompt_addon(
            "full", photos, "银发红瞳，白色长裙，颈间一枚月牙吊坠。", ["窗边的黄昏"]
        )
        assert addon, "开启插图时 prompt 追加段不应为空"
        assert "银发红瞳" in addon, "追加段没有原样嵌入形象档案"
        assert "水墨淡彩" in addon, "追加段没有画风锚 image_style_prompt"
        assert "〔插图：" in addon, "追加段没有插图标记语法说明"
        assert "〔用户照片：" in addon, "追加段没有用户照片标记语法说明"
        assert "她拍的晚霞" in addon, "追加段没有用户照片清单/配文"
        assert "窗边的黄昏" in addon, "追加段没有注入最近图描述"
        assert "内容未识别" in addon, "追加段缺少无视觉能力时的诚实降级说明"


async def test_image_prompt_addon_empty_when_disabled():
    async with World(config=img_config(image_enabled=False)) as world:
        plugin = _plugin(world)
        assert plugin._image_prompt_addon("full", [], "银发红瞳", ["旧的图"]) == "", (
            "image_enabled=false 时追加段必须为空（零额外调用）"
        )


async def test_image_style_summary_returns_recent_alts_limited():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        days = ["20261020", "20261021", "20261022", "20261023"]
        alts = ["第一天的图", "第二天的图", "第三天的图", "第四天的图"]
        for day, alt in zip(days, alts):
            plugin._save_diary(
                SENDER, f"{alt}正文\n![{alt}](images/{day}_1_{alt}.png)",
                sender_name="阿罗", entry_name_hint=day, origin="private", mode="full",
            )
            plugin.save_image(SENDER, png(int(day[-1])), day, 1, alt)

        summary = plugin._image_style_summary(SENDER, limit=3)
        assert isinstance(summary, list), f"_image_style_summary 应返回 list：{summary!r}"
        assert 1 <= len(summary) <= 3, f"最近图描述数量应在 1~limit：{summary!r}"
        joined = "\n".join(str(s) for s in summary)
        assert "第四天的图" in joined, f"最近图描述缺少最新一天的插图：{summary!r}"


# ---------------------------------------------------------------------------
# D6：形象档案与配置写入
# ---------------------------------------------------------------------------


async def test_persona_image_desc_reads_config_and_set_config_value():
    async with World(config=img_config(persona_image_desc="")) as world:
        plugin = _plugin(world)
        assert plugin._persona_image_desc() == ""

        assert plugin._set_config_value("persona_image_desc", "黑发金瞳，深色风衣。") is True
        assert plugin.config.get("persona_image_desc") == "黑发金瞳，深色风衣。"
        assert plugin._persona_image_desc() == "黑发金瞳，深色风衣。"


async def test_pending_persona_desc_save_load_apply_clear():
    async with World(config=img_config(persona_image_desc="旧形象")) as world:
        plugin = _plugin(world)

        assert plugin._load_pending_persona_desc(SENDER) == {}, (
            "没有提案时应返回空 dict"
        )

        assert plugin._save_pending_persona_desc(
            SENDER, "银发红瞳，白色长裙，月牙吊坠。", kind="init"
        ) is True
        rec = plugin._load_pending_persona_desc(SENDER)
        assert rec.get("desc") == "银发红瞳，白色长裙，月牙吊坠。"
        assert rec.get("kind") == "init"
        assert rec.get("created_at"), f"pending 记录缺少 created_at：{rec!r}"

        applied, _message = plugin._apply_pending_persona_desc(SENDER)
        assert applied is True, "确认后应把 pending 形象写入配置"
        assert plugin.config.get("persona_image_desc") == "银发红瞳，白色长裙，月牙吊坠。"
        assert plugin._load_pending_persona_desc(SENDER) == {}, "确认后应清空 pending"

        applied_again, _message2 = plugin._apply_pending_persona_desc(SENDER)
        assert applied_again is False, "没有 pending 时再次确认应返回 False"

        # clear 单独可用
        plugin._save_pending_persona_desc(SENDER, "临时提案", kind="evolve")
        plugin._clear_pending_persona_desc(SENDER)
        assert plugin._load_pending_persona_desc(SENDER) == {}


# ---------------------------------------------------------------------------
# 可选视觉接口：_caption_user_photos（设计 7.4）
# ---------------------------------------------------------------------------

VISION_CFG = dict(
    vision_api_base="https://vision.example.invalid/v1",
    vision_api_key="vk-test",
    vision_api_model="gpt-4o-mini",
)


async def test_caption_user_photos_unconfigured_keeps_honest_blank():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        calls = []

        async def boom(url, payload, timeout, headers=None):
            calls.append(url)
            raise AssertionError("未配置视觉接口时不应联网识别照片")

        plugin._http_post_json = boom
        photos = [
            {"index": 1, "data": png(31), "caption": "她拍的晚霞"},
            {"index": 2, "data": png(32), "caption": "路边的小猫"},
        ]
        out = await plugin._caption_user_photos(photos)
        assert len(out) == 2
        assert calls == [], f"未配置视觉接口却联网了：{calls!r}"
        for photo in out:
            assert photo.get("vision_desc", "") == "", (
                f"未配置视觉接口时应保持空描述（内容未识别）：{photo!r}"
            )


async def test_caption_user_photos_fills_vision_desc_when_configured():
    async with World(config=img_config(**VISION_CFG)) as world:
        plugin = _plugin(world)
        replies = iter(["一只橘猫趴在窗台上", "橘红色的晚霞铺满半边天"])
        calls = []

        async def fake_post(url, payload, timeout, headers=None):
            calls.append(payload)
            return {"choices": [{"message": {"content": next(replies)}}]}

        plugin._http_post_json = fake_post
        photos = [
            {"index": 1, "data": png(33), "caption": "她拍的晚霞"},
            {"index": 2, "data": png(34), "caption": "路边的小猫"},
        ]
        out = await plugin._caption_user_photos(photos)
        assert out[0]["vision_desc"] == "一只橘猫趴在窗台上"
        assert out[1]["vision_desc"] == "橘红色的晚霞铺满半边天"
        assert len(calls) == 2, f"应逐张识别 2 次，实际 {len(calls)} 次"

        # 已有 vision_desc 的照片不重复调用
        calls.clear()
        again = await plugin._caption_user_photos(list(out))
        assert calls == [], "已经有画面描述的照片不应重复识别"
        assert again[0]["vision_desc"] == "一只橘猫趴在窗台上"


# ---------------------------------------------------------------------------
# _image_prompt_addon：照片清单里的视觉描述 / 零 token / 只收照片分支
# ---------------------------------------------------------------------------


async def test_image_prompt_addon_photo_list_includes_vision_desc_and_fallback():
    async with World(
        config=img_config(
            image_prob_memo=0.0,
            image_limit_memo=0,
            user_image_enabled=True,
            user_image_limit=2,
        )
    ) as world:
        plugin = _plugin(world)
        photos = [
            {
                "index": 1,
                "data": png(35),
                "caption": "她拍的晚霞",
                "vision_desc": "橘红色天空，远处有楼顶剪影",
            },
            {"index": 2, "data": png(36), "caption": "路边的小猫"},
        ]
        addon = plugin._image_prompt_addon("brief", photos, "银发红瞳", [])

        # 简记没有 AI 插图额度：只给照片标记段，不含 AI 插图标记语法
        assert addon, "有可收照片时应有照片清单追加段"
        assert "〔用户照片：" in addon
        assert "〔插图：" not in addon, "无 AI 插图额度时不应塞 AI 插图标记语法"
        assert "画面描述（视觉接口）：橘红色天空，远处有楼顶剪影" in addon, (
            f"照片清单没有带视觉接口描述：{addon!r}"
        )
        assert "画面内容未识别（只写你知道的，别猜）" in addon, (
            "没有视觉描述的照片应诚实降级提示"
        )


async def test_image_prompt_addon_zero_cost_when_no_ai_quota_and_no_photo():
    async with World(
        config=img_config(image_prob_memo=0.0, image_limit_memo=0)
    ) as world:
        plugin = _plugin(world)
        assert plugin._image_prompt_addon("brief", [], "银发红瞳", []) == "", (
            "简记无插图额度且没有可收照片时应返回空串（零额外 token）"
        )
        assert plugin._image_prompt_addon("life", [], "", []) == ""
        # full 模式默认有 2 张额度，仍应给出完整决策提示
        full = plugin._image_prompt_addon("full", [], "银发红瞳", [])
        assert "〔插图：" in full


# ---------------------------------------------------------------------------
# 生成失败退还每日额度（配置页承诺"生成失败不计数"）
# ---------------------------------------------------------------------------


def _pending_entries(plugin, count: int):
    markers = "\n".join(
        f"〔插图：画面{ i }｜竖〕" for i in range(1, count + 1)
    )
    parsed, decisions = plugin._parse_image_markers(f"正文段落。\n{markers}\n结尾。", count)
    entry = plugin._save_diary(
        SENDER, parsed, sender_name="阿罗", origin="private", mode="full"
    )
    return entry, [d["pending_ref"] for d in decisions]


async def test_run_image_job_refunds_quota_on_failure_but_not_on_success():
    async with World(config=img_config(image_limit_daily=6)) as world:
        inst = world.refactor
        plugin = _plugin(world)
        entry, pendings = _pending_entries(plugin, 3)
        cfg = plugin._image_cfg()

        # 失败（返回 None）：先占额度 1，失败后应退还到 0
        assert plugin._image_usage_consume() is True
        assert plugin._image_usage_load()["count"] == 1

        async def fail_generate(prompt, size, ref_path=""):
            return None

        plugin.generate_image = fail_generate
        await plugin._run_image_job(
            sender_id=SENDER, sender_name="阿罗", umo=UMO, day=DAY,
            pending_ref=pendings[0], alt="画面1", orientation="portrait",
            prompt="画面1", cfg=cfg,
        )
        assert plugin._image_usage_load()["count"] == 0, (
            "生图失败应退还每日额度（生成失败不计数）"
        )

        # 异常路径：同样退还
        assert plugin._image_usage_consume() is True

        async def broken_generate(prompt, size, ref_path=""):
            raise RuntimeError("上游 500")

        plugin.generate_image = broken_generate
        await plugin._run_image_job(
            sender_id=SENDER, sender_name="阿罗", umo=UMO, day=DAY,
            pending_ref=pendings[1], alt="画面2", orientation="portrait",
            prompt="画面2", cfg=cfg,
        )
        assert plugin._image_usage_load()["count"] == 0, "任务异常也应退还额度"

        # 成功路径：额度不退还（真的产出了一张图）
        assert plugin._image_usage_consume() is True

        async def ok_generate(prompt, size, ref_path=""):
            return png(41)

        plugin.generate_image = ok_generate
        await plugin._run_image_job(
            sender_id=SENDER, sender_name="阿罗", umo=UMO, day=DAY,
            pending_ref=pendings[2], alt="画面3", orientation="portrait",
            prompt="画面3", cfg=cfg,
        )
        assert plugin._image_usage_load()["count"] == 1, "成功生成不应退还额度"
        assert len(plugin.list_images(SENDER)) == 1


# ---------------------------------------------------------------------------
# _write_diary 集成：用户照片入日记 + 张数提示（N=0/1/2）
# ---------------------------------------------------------------------------


async def _run_write_with_photos(n_photos: int):
    history = []
    markers = []
    for i in range(1, n_photos + 1):
        history.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"照片配文{i}"},
                    _image_part(data_url(png(50 + i))),
                ],
            }
        )
        markers.append(f"〔用户照片：{i}｜照片描述{i}〕")
    reply = "今天写日记。\n" + "\n".join(markers) + "\n结尾。"
    config = img_config(
        image_prob_full=0.0,
        user_image_limit=2,
        use_persona=False,
    )
    async with World(config=config) as world:
        inst = world.refactor
        plugin = inst.plugin
        if history:
            inst.set_history(history)
        else:
            inst.set_history([message("user", "今天没什么特别的事")])
        inst.provider.replies[:] = [reply]
        content, err = await plugin._write_diary(
            UMO, SENDER, "阿罗", requested_mode="full"
        )
        return {
            "content": content,
            "err": err,
            "notice": plugin._image_last_notice or "",
            "images": plugin.list_images(SENDER),
            "saved": plugin._load_latest(SENDER)[0],
            "saved_tampered": plugin._load_latest(SENDER)[1],
        }


async def test_write_diary_user_photo_count_notice_n0_n1_n2():
    zero = await _run_write_with_photos(0)
    assert zero["err"] is None and zero["content"]
    assert zero["images"] == []
    assert "张照片" not in zero["notice"], (
        f"没有收照片时不应提示张数：{zero['notice']!r}"
    )

    one = await _run_write_with_photos(1)
    assert one["err"] is None
    assert len(one["images"]) == 1, f"1 张用户照片应入 zip：{one['images']!r}"
    assert "收了你发来的 1 张照片" in one["notice"], (
        f"N=1 的张数提示不对：{one['notice']!r}"
    )
    assert one["saved_tampered"] is False
    assert one["saved"].count("![用户分享的照片：") == 1

    two = await _run_write_with_photos(2)
    assert two["err"] is None
    assert len(two["images"]) == 2, f"2 张用户照片应入 zip：{two['images']!r}"
    assert "收了你发来的 2 张照片" in two["notice"], (
        f"N=2 的张数提示不对：{two['notice']!r}"
    )
    assert two["saved_tampered"] is False
    assert two["saved"].count("![用户分享的照片：") == 2
