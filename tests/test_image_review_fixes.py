# -*- coding: utf-8 -*-
"""v0.6 审查修复回归（Lead 追加）：行内占位清理 / 群聊不收照片 / 同日照片序号 / 云端官方参数。

对应独立审查报告的 P1-1、P1-3、P2-4、P1-2 修复点。只驱动 refactor 侧，不联网。
"""

from __future__ import annotations

from harness.world import World
from image_testkit import b64, data_url, img_config, png

SENDER = "10001"
DAY = "20261023"
UMO = "aiocqhttp:FriendMessage:10001"


def _plugin(world):
    return world.refactor.plugin


# ---------------------------------------------------------------------------
# P1-1：占位清理只删标记本身，不能吞掉写在句中的正文
# ---------------------------------------------------------------------------


async def test_inline_pending_marker_cleanup_keeps_sentence():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        parsed, decisions = plugin._parse_image_markers(
            "今天去江边〔插图：晚霞｜横〕很开心。\n第二行。", 1
        )
        assert len(decisions) == 1
        pending = decisions[0]["pending_ref"]
        assert pending in parsed, "行内插图标记应原位替换成 PENDING 占位"

        entry = plugin._save_diary(
            SENDER, parsed, sender_name="阿罗",
            entry_name_hint=DAY, origin="private", mode="full",
        )
        # 模拟生图失败：删除占位
        assert plugin.replace_pending_images(SENDER, entry, {}, [pending]) is True

        content, tampered, _ = plugin._load_latest(SENDER)
        assert tampered is False
        assert "PENDING" not in content
        assert "今天去江边" in content, f"句首正文被吞了：{content!r}"
        assert "很开心。" in content, f"句尾正文被吞了（P1-1 回归）：{content!r}"
        assert "第二行。" in content


# ---------------------------------------------------------------------------
# P1-3：群聊写日记不收集成员照片（历史无法区分发送者）
# ---------------------------------------------------------------------------


async def test_group_write_does_not_collect_user_photos():
    async with World(
        config=img_config(user_image_enabled=True, user_image_limit=1)
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "群里发的图"},
                        {"type": "image_url", "image_url": {"url": data_url(png(301))}},
                    ],
                },
                {"role": "assistant", "content": "看到了。"},
            ]
        )
        inst.provider.replies[:] = [
            "群聊日记正文。\n〔用户照片：1｜群里发的图〕\n结尾。"
        ]
        diary, err = await plugin._write_diary(
            UMO, SENDER, "阿罗", is_group=True, requested_mode="full"
        )
        assert err is None, err
        assert plugin.list_images(SENDER) == [], "群聊写日记不应把成员照片收进写作者 zip"
        content, tampered, _ = plugin._load_latest(SENDER)
        assert tampered is False
        assert "〔用户照片" not in content, "群聊照片标记应被清掉而不是保留"


# ---------------------------------------------------------------------------
# P2-4：同一天多次写日记，用户照片序号接续（不重复 _1_）
# ---------------------------------------------------------------------------


async def test_user_photo_seq_continues_across_same_day_writes():
    async with World(
        config=img_config(user_image_limit=1, max_full_diaries_per_day=0)
    ) as world:
        inst = world.refactor
        plugin = inst.plugin

        inst.set_history(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "第一张"},
                        {"type": "image_url", "image_url": {"url": data_url(png(302))}},
                    ],
                },
                {"role": "assistant", "content": "好看。"},
            ]
        )
        inst.provider.replies[:] = ["第一篇。\n〔用户照片：1｜第一张〕\n结尾。"]
        diary1, err1 = await plugin._write_diary(
            UMO, SENDER, "阿罗", is_group=False, requested_mode="full"
        )
        assert err1 is None, err1

        inst.set_history(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "第一张"},
                        {"type": "image_url", "image_url": {"url": data_url(png(302))}},
                    ],
                },
                {"role": "assistant", "content": "好看。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "第二张"},
                        {"type": "image_url", "image_url": {"url": data_url(png(303))}},
                    ],
                },
                {"role": "assistant", "content": "这张也不错。"},
            ]
        )
        inst.provider.replies[:] = ["第二篇。\n〔用户照片：1｜第二张〕\n结尾。"]
        diary2, err2 = await plugin._write_diary(
            UMO, SENDER, "阿罗", is_group=False, requested_mode="full"
        )
        assert err2 is None, err2

        images = plugin.list_images(SENDER)
        assert len(images) == 2, images
        bases = [name.split("/")[-1] for name in images]
        assert bases[0].startswith(f"{DAY}_1_"), bases
        assert bases[1].startswith(f"{DAY}_2_"), (
            f"同日第二次写日记的照片序号没有接续（P2-4 回归）：{bases!r}"
        )


# ---------------------------------------------------------------------------
# P1-2：云端第一发只用 OpenAI 官方 images API 字段（不带 seed/response_format）
# ---------------------------------------------------------------------------


async def test_cloud_first_request_uses_official_fields_only():
    image = png(304)
    cfg = img_config(
        image_mode="cloud",
        image_api_base="https://image.example.invalid/v1",
        image_api_key="sk-test",
        image_model="gpt-image-1",
    )
    async with World(config=cfg) as world:
        plugin = _plugin(world)
        calls = []

        async def fake_post(url, payload, timeout, headers=None):
            calls.append(payload)
            return {"data": [{"b64_json": b64(image)}]}

        plugin._http_post_json = fake_post
        out = await plugin.generate_image("窗边的黄昏", (1024, 1024))
        assert out == image
        first = calls[0]
        assert first["prompt"] == "窗边的黄昏"
        assert first["size"] == "1024x1024"
        assert first.get("model") == "gpt-image-1"
        assert "seed" not in first, (
            f"OpenAI 官方 images API 不接受 seed，首发不应带：{first!r}"
        )
        assert "response_format" not in first, (
            f"gpt-image-1 不支持 response_format，首发不应带：{first!r}"
        )
