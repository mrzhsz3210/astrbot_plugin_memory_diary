# -*- coding: utf-8 -*-
"""v0.6 D1 存储层：图片入 zip / 签名三态 / 命名消毒 / 占位替换 / 导出导入。

设计依据：设计方案第一部分 D1 与《插图功能接口契约》第 2、3 节。
只驱动 refactor 侧（baseline v0.5.5 没有插图能力），不联网。
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from harness.world import World
from image_testkit import (
    GIF,
    JPG,
    PNG,
    WEBP,
    assert_raises,
    data_url,
    drop_zip_entry,
    img_config,
    md5_key,
    png,
    repack_inner_diary_zip,
    replace_zip_entry,
    rewrite_zip,
    zip_entries,
)

SENDER = "10001"
DAY = "20261023"
OTHER_DAY = "20261021"


def _plugin(world):
    return world.refactor.plugin


# ---------------------------------------------------------------------------
# 写入 → 读出
# ---------------------------------------------------------------------------


async def test_image_save_load_roundtrip_bytes_identical():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        data = png(1)

        name = plugin.save_image(SENDER, data, DAY, 1, "窗边的黄昏")
        assert name == f"images/{DAY}_1_窗边的黄昏.png", f"命名不符合契约：{name!r}"

        got, status = plugin.load_image(SENDER, name)
        assert status == "ok", f"刚写入的图验签失败：{status}"
        assert got == data, "写入与读出的图片字节不一致"

        assert plugin.list_images(SENDER) == [name], "list_images 没有列出刚写入的图"

        entries = zip_entries(inst.diary_path(SENDER))
        sig_entry = plugin._image_sig_entry(name)
        assert sig_entry == f"sigs/images_{DAY}_1_窗边的黄昏.png.sig", sig_entry
        assert sig_entry in entries, "图片签名没有写进 zip"
        assert entries[sig_entry].decode("utf-8").strip() == plugin._image_sig(
            SENDER, name, data
        ), "zip 里的图片签名与 _image_sig 计算不一致"
        # 兼容别名（main.py 可能用 _load_image）
        assert plugin._load_image(SENDER, name) == (data, "ok")


async def test_image_save_image_creates_package_when_zip_missing():
    """save_image 是唯一图片路径入口：zip 不存在时自己建带 说明.txt 的空包。"""
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        assert not inst.diary_path(SENDER).exists()

        name = plugin.save_image(SENDER, png(3), DAY, 1, "新包里的图")
        assert inst.diary_path(SENDER).exists(), "save_image 没有创建日记 zip"
        entries = zip_entries(inst.diary_path(SENDER))
        assert plugin.README_ZIP_ENTRY in entries, "空包缺少 说明.txt"
        assert entries[name] == png(3)
        assert plugin.load_image(SENDER, name) == (png(3), "ok")


async def test_image_ext_sniffed_from_magic_and_explicit_ext_kept():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        cases = [(PNG, "png"), (JPG, "jpg"), (WEBP, "webp"), (GIF, "gif")]
        for seq, (data, expected_ext) in enumerate(cases, start=1):
            name = plugin.save_image(SENDER, data, DAY, seq, f"嗅探{seq}")
            assert name.endswith(f".{expected_ext}"), (
                f"魔数嗅探扩展名错误：{data[:8]!r} -> {name!r}（预期 .{expected_ext}）"
            )
            assert plugin.load_image(SENDER, name)[1] == "ok"

        # 显式给合法 ext 时按调用方要求（不强制嗅探）
        explicit = plugin.save_image(SENDER, PNG, DAY, 10, "显式jpg", ext="jpg")
        assert explicit.endswith(".jpg"), explicit


async def test_image_list_images_sorted_and_isolated_per_user():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        a = plugin.save_image(SENDER, png(1), DAY, 2, "第二张")
        b = plugin.save_image(SENDER, png(2), DAY, 1, "第一张")
        other = plugin.save_image("20002", png(3), DAY, 1, "别人的图")

        assert plugin.list_images(SENDER) == sorted([a, b]), "list_images 未按升序返回"
        assert plugin.list_images("20002") == [other], "不同用户的图片串了"


# ---------------------------------------------------------------------------
# 签名三态：ok / tampered / missing / no_key
# ---------------------------------------------------------------------------


async def test_image_tampered_after_binary_replaced():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        name = plugin.save_image(SENDER, png(1), DAY, 1, "窗边的黄昏")

        # 模拟"拿到 zip 的人把图换掉"（正文与正文签名都不动）
        replace_zip_entry(inst.diary_path(SENDER), name, png(2) + b"TAMPERED")

        data, status = plugin.load_image(SENDER, name)
        assert status == "tampered", f"换图后应判篡改，实际 {status!r}"
        assert data is None, "篡改的图不应返回字节"


async def test_image_tampered_when_signature_missing():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        name = plugin.save_image(SENDER, png(1), DAY, 1, "没签名的图")

        drop_zip_entry(inst.diary_path(SENDER), plugin._image_sig_entry(name))

        data, status = plugin.load_image(SENDER, name)
        assert status == "tampered", f"签名缺失应按篡改处理，实际 {status!r}"
        assert data is None


async def test_image_missing_degrades_and_diary_still_reads():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        content = (
            "今天很安静。\n"
            f"![窗边的黄昏](images/{DAY}_1_窗边的黄昏.png)\n"
            "最后一句。"
        )
        plugin._save_diary(
            SENDER, content, sender_name="阿罗", origin="private", mode="full"
        )
        gone = plugin.save_image(SENDER, png(1), DAY, 1, "窗边的黄昏")
        kept = plugin.save_image(SENDER, png(2), DAY, 2, "她拍的晚霞")

        # 图片条目被删（不告警、优雅降级）；签名留着也不影响
        drop_zip_entry(inst.diary_path(SENDER), gone)

        data, status = plugin.load_image(SENDER, gone)
        assert (data, status) == (None, "missing"), f"缺图应返回 missing，实际 {data!r}/{status!r}"
        assert plugin.load_image(SENDER, kept) == (png(2), "ok"), "另一张图被连带影响"

        read_content, tampered, entry_name = plugin._load_latest(SENDER)
        assert tampered is False, "缺一张图不应让正文判篡改"
        assert entry_name == f"diaries/{DAY}.md"
        assert gone in read_content, "正文里的图片引用不应被缺图影响"


async def test_image_no_key_status_when_master_key_unavailable():
    """主密钥不可用时读图返回 no_key（验签做不了），不伪装成 missing/ok。"""
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        name = plugin.save_image(SENDER, png(1), DAY, 1, "无钥匙的图")

        # 把 zip 挪到旧版 MD5 命名（主密钥不可用时 _diary_path 会回退到它），
        # 再把 .diary_key 变成不可读的目录，触发 _secret_key() -> None。
        md5_path = Path(plugin.data_dir) / f"diary_{md5_key(SENDER)}.zip"
        inst.diary_path(SENDER).rename(md5_path)
        key_path = Path(plugin.data_dir) / ".diary_key"
        key_path.unlink()
        key_path.mkdir()
        plugin._key_cache = None

        data, status = plugin.load_image(SENDER, name)
        assert (data, status) == (None, "no_key"), (
            f"主密钥不可用时应返回 no_key，实际 {data!r}/{status!r}"
        )


async def test_image_load_rejects_illegal_names_as_missing():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        name = plugin.save_image(SENDER, png(1), DAY, 1, "合法图")
        bad_names = [
            "../evil.png",
            "images/../evil.png",
            "images/..\\evil.png",
            "diaries/20261023.md",
            "images/",
            "images/x.svg",
            "images/x.png/../../y.png",
        ]
        for bad in bad_names:
            result = plugin.load_image(SENDER, bad)
            assert result == (None, "missing"), f"非法图片名 {bad!r} 应返回 missing，实际 {result!r}"
        # 合法名不受影响
        assert plugin.load_image(SENDER, name) == (png(1), "ok")


# ---------------------------------------------------------------------------
# 命名 sanitize / 撞名哈希
# ---------------------------------------------------------------------------


def _desc_of(name: str, day: str, seq: int) -> str:
    prefix = f"images/{day}_{seq}_"
    assert name.startswith(prefix), f"命名前缀不符合契约：{name!r} 不以 {prefix!r} 开头"
    body = name[len(prefix):]
    return body.rsplit(".", 1)[0]


def _assert_safe_desc(desc: str, raw: str):
    assert "/" not in desc and "\\" not in desc, f"{raw!r} 消毒后仍含路径分隔符：{desc!r}"
    assert ".." not in desc, f"{raw!r} 消毒后仍含 '..'：{desc!r}"
    assert not any(ord(c) < 0x20 or ord(c) == 0x7F for c in desc), (
        f"{raw!r} 消毒后仍含控制字符：{desc!r}"
    )
    assert "]" not in desc, f"{raw!r} 消毒后仍含 ']'：{desc!r}"
    assert len(desc) <= 20, f"{raw!r} 消毒后超过 20 字：{desc!r}（{len(desc)} 字）"
    assert desc.strip(), f"{raw!r} 消毒后为空"


async def test_image_desc_sanitized_for_paths_controls_and_length():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        cases = [
            "../../etc/passwd",
            "..\\windows\\system32",
            "窗\x00边\x1f的\x7f黄昏\n第二行",
            "***???:::|||",
            "深" * 40,
            "[插图](javascript:alert(1))",
        ]
        for seq, raw in enumerate(cases, start=1):
            name = plugin.save_image(SENDER, png(seq), DAY, seq, raw)
            assert plugin.IMAGE_NAME_RE.match(name), f"{raw!r} 生成的文件名不合法：{name!r}"
            desc = _desc_of(name, DAY, seq)
            _assert_safe_desc(desc, raw)
            assert plugin.load_image(SENDER, name)[1] == "ok"

        # 空描述/纯空白回退"插图"
        empty = plugin.save_image(SENDER, png(99), DAY, 99, "")
        assert _desc_of(empty, DAY, 99) == "插图"
        blank = plugin.save_image(SENDER, png(98), DAY, 98, "   \t\n")
        assert _desc_of(blank, DAY, 98) == "插图"

        # day/seq 非法值容错（不抛，回退当天日期/序号 1）
        fallback = plugin.save_image(SENDER, png(97), "not-a-day", 0, "容错")
        assert fallback.startswith("images/20261023_1_"), fallback


async def test_image_name_collision_uses_content_hash_suffix():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        first_data = png(1)
        second_data = png(2)

        first = plugin.save_image(SENDER, first_data, DAY, 1, "同样的画")
        second = plugin.save_image(SENDER, second_data, DAY, 1, "同样的画")

        assert first != second, "同一天同序号同描述的新图片覆盖了旧图，撞名没有兜底"
        digest = hashlib.sha256(second_data).hexdigest()[:6]
        assert digest in second, f"撞名兜底名里没有内容哈希前 6 位：{second!r}"
        assert second.startswith(f"images/{DAY}_1_同样的画"), second

        # 两张图都完好可读，没有互相覆盖
        assert plugin.load_image(SENDER, first) == (first_data, "ok")
        assert plugin.load_image(SENDER, second) == (second_data, "ok")
        assert len(plugin.list_images(SENDER)) == 2


# ---------------------------------------------------------------------------
# 与日记生命周期共存：_save_diary 重写 / _delete_entries / _delete_images
# ---------------------------------------------------------------------------


async def test_save_diary_rewrite_keeps_images_and_signatures():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        plugin._save_diary(
            SENDER, "第一篇日记", sender_name="阿罗", origin="private", mode="full"
        )
        data = png(5)
        name = plugin.save_image(SENDER, data, DAY, 1, "第一篇的图")

        # 同一天再写一篇（会整体重写 zip）
        plugin._save_diary(
            SENDER, "第二篇日记", sender_name="阿罗", origin="private", mode="brief"
        )
        assert plugin.load_image(SENDER, name) == (data, "ok"), (
            "_save_diary 重写 zip 后图片或签名丢失"
        )
        assert plugin.list_images(SENDER) == [name]
        entries = zip_entries(inst.diary_path(SENDER))
        assert plugin._image_sig_entry(name) in entries
        content, tampered, _ = plugin._load_latest(SENDER)
        assert tampered is False
        assert "第二篇日记" in content


async def test_delete_entries_removes_that_days_images():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        plugin._save_diary(
            SENDER, "第一天的日记", sender_name="阿罗",
            entry_name_hint=DAY, origin="private", mode="full",
        )
        day_image = plugin.save_image(SENDER, png(1), DAY, 1, "第一天的图")
        plugin._save_diary(
            SENDER, "第二天的日记", sender_name="阿罗",
            entry_name_hint=OTHER_DAY, origin="private", mode="full",
        )
        other_image = plugin.save_image(SENDER, png(2), OTHER_DAY, 1, "第二天的图")

        removed = plugin._delete_entries(SENDER, [f"diaries/{DAY}.md"])
        assert removed == 1, f"应删除 1 天，实际 {removed}"

        assert plugin.list_images(SENDER) == [other_image], "被删那天的图片没有连带清理"
        entries = zip_entries(inst.diary_path(SENDER))
        assert day_image not in entries
        assert plugin._image_sig_entry(day_image) not in entries, "图片签名成了孤儿条目"
        assert other_image in entries and plugin._image_sig_entry(other_image) in entries

        content, tampered, entry_name = plugin._load_latest(SENDER)
        assert tampered is False and entry_name == f"diaries/{OTHER_DAY}.md"
        assert "第二天的日记" in content


async def test_delete_images_helper_only_removes_given_day():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        plugin._save_diary(SENDER, "日记", sender_name="阿罗",
                           entry_name_hint=DAY, origin="private", mode="full")
        a = plugin.save_image(SENDER, png(1), DAY, 1, "今天的图")
        b = plugin.save_image(SENDER, png(2), OTHER_DAY, 1, "昨天的图")

        assert plugin._delete_images(SENDER, DAY) == 1, "_delete_images 返回删除张数不对"
        assert plugin.list_images(SENDER) == [b], "按天删图删错/漏删"


# ---------------------------------------------------------------------------
# 注入占位符化（图片绝不进 LLM 上下文）
# ---------------------------------------------------------------------------


async def test_image_macro_to_placeholder_replaces_all_forms():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        content = (
            "前文 ![窗边的黄昏](images/20261023_1_窗边的黄昏.png) 后文\n"
            "![用户分享的照片：她拍的晚霞](images/20261023_2_她拍的晚霞.png)\n"
            "![占位](images/PENDING-3f2a9c01.png)"
        )
        out = plugin._image_macro_to_placeholder(content)
        assert "[插图：窗边的黄昏]" in out
        assert "[用户分享的照片：她拍的晚霞]" in out
        assert "[插图：占位]" in out
        assert "images/" not in out, f"图片路径仍残留在注入文本里：{out!r}"
        assert "![" not in out, f"markdown 图片语法仍残留在注入文本里：{out!r}"
        assert "data:image" not in out

        # 没有图片语法时原样返回（v0.5.5 文本不受影响）
        plain = "全是文字的一篇日记。"
        assert plugin._image_macro_to_placeholder(plain) == plain
        assert plugin._image_macro_to_placeholder("") == ""


async def test_assemble_inject_uses_placeholders_and_counts_budget():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        content = (
            "今天很安静。\n"
            f"![窗边的黄昏](images/{DAY}_1_窗边的黄昏.png)\n"
            f"![用户分享的照片：她拍的晚霞](images/{DAY}_2_她拍的晚霞.png)\n"
            f"![还没画完](images/PENDING-3f2a9c01.png)\n"
            "最后一句。"
        )
        plugin._save_diary(
            SENDER, content, sender_name="阿罗", origin="private", mode="full"
        )

        text, tampered, used_names = plugin._assemble_inject(SENDER)
        assert tampered is False
        assert used_names == [f"diaries/{DAY}.md"]
        assert "[插图：窗边的黄昏]" in text
        assert "[用户分享的照片：她拍的晚霞]" in text
        assert "[插图：还没画完]" in text
        assert "images/" not in text
        assert "![" not in text
        assert "base64" not in text


# ---------------------------------------------------------------------------
# 导出 → 导入（迁移包图片逐字节保留 + 篡改检出）
# ---------------------------------------------------------------------------


async def _make_bundle_with_image(world):
    inst = world.refactor
    plugin = _plugin(world)
    plugin._save_diary(
        SENDER,
        f"正文\n![窗边的黄昏](images/{DAY}_1_窗边的黄昏.png)\n结尾。",
        sender_name="阿罗",
        entry_name_hint=DAY,
        origin="private",
        mode="full",
    )
    data = png(7)
    name = plugin.save_image(SENDER, data, DAY, 1, "窗边的黄昏")
    entries = plugin._list_entries(SENDER)
    bundle = plugin._build_export_bundle(SENDER, inst.diary_path(SENDER), entries)
    return plugin, name, data, entries, bundle


async def test_export_import_bundle_preserves_image_bytes():
    async with World(config=img_config()) as src_world, World(
        config=img_config()
    ) as dst_world:
        src_plugin, name, data, entries, bundle = await _make_bundle_with_image(src_world)

        result = src_plugin._verify_bundle(bundle)
        assert isinstance(result, tuple) and len(result) == 5, (
            f"_verify_bundle 应返回 5 元组 "
            f"(owner, key, entries, images, annotations)，实际 {result!r}"
        )
        owner, user_key, bundle_entries, images, annotations = result
        assert annotations is None, (
            f"没有批注侧车的迁移包 annotations 应为 None，实际 {annotations!r}"
        )
        assert owner == SENDER
        assert isinstance(images, dict), "images 应是 {条目名: bytes} 字典"
        assert name in images, f"迁移包 images 里没有 {name!r}：{sorted(images)}"
        assert images[name] == data, "迁移包里的图片字节与原始不一致"
        assert entries[0] in bundle_entries

        dst_plugin = dst_world.refactor.plugin
        mode = dst_plugin._install_bundle(
            SENDER, owner, user_key, bundle_entries, images=images
        )
        # 两个 World 用同一个固定测试主密钥 -> same_key
        assert mode == "same_key", f"同密钥安装应为 same_key，实际 {mode!r}"

        got, status = dst_plugin.load_image(SENDER, name)
        assert status == "ok", f"导入后图片验签失败：{status}"
        assert got == data, "导出→导入后图片字节不逐字节一致"

        content, tampered, _ = dst_plugin._load_latest(SENDER)
        assert tampered is False
        assert name in content, "导入后的正文里缺少图片引用"


async def test_verify_bundle_detects_tampered_image():
    async with World(config=img_config()) as world:
        plugin, name, data, _entries, bundle = await _make_bundle_with_image(world)

        def _swap(inner):
            assert name in inner
            inner[name] = data + b"\x00TAMPERED-BY-TEST"

        repack_inner_diary_zip(bundle, _swap)

        with assert_raises(ValueError) as caught:
            plugin._verify_bundle(bundle)
        assert caught.value is not None
        message = str(caught.value)
        assert message, "迁移包被换图后抛出的 ValueError 没有说明原因"
        assert "插图" in message or "验签" in message or "改动" in message, (
            f"换图错误信息不够明确：{message!r}"
        )


async def test_verify_bundle_old_package_without_images_is_backward_compatible():
    """旧包（日记 zip 里没有 images/）仍可校验，images 返回空 dict。"""
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = _plugin(world)
        plugin._save_diary(
            SENDER, "没有插图的旧日记", sender_name="阿罗",
            entry_name_hint=DAY, origin="private", mode="full",
        )
        entries = plugin._list_entries(SENDER)
        bundle = plugin._build_export_bundle(SENDER, inst.diary_path(SENDER), entries)

        owner, user_key, bundle_entries, images, annotations = plugin._verify_bundle(
            bundle
        )
        assert owner == SENDER
        assert images == {}, f"没有插图的迁移包 images 应为空 dict，实际 {images!r}"
        assert annotations is None, "没有批注侧车的迁移包 annotations 应为 None"
        assert entries[0] in bundle_entries


async def test_install_bundle_writes_image_signatures_with_local_key():
    """images 安装时用本机派生钥匙重签：装完 load_image 必须是 ok。"""
    async with World(config=img_config()) as src_world, World(
        config=img_config()
    ) as dst_world:
        src_plugin, name, data, _entries, bundle = await _make_bundle_with_image(src_world)
        _owner, user_key, bundle_entries, images, _annotations = src_plugin._verify_bundle(
            bundle
        )

        dst_plugin = dst_world.refactor.plugin
        dst_plugin._install_bundle(SENDER, SENDER, user_key, bundle_entries, images=images)
        dst_entries = zip_entries(dst_world.refactor.diary_path(SENDER))
        assert dst_plugin._image_sig_entry(name) in dst_entries, (
            "安装迁移包时没有为图片写本机签名"
        )
        expected = dst_plugin._image_sig(SENDER, name, data)
        assert dst_entries[dst_plugin._image_sig_entry(name)].decode().strip() == expected
        assert dst_plugin.load_image(SENDER, name) == (data, "ok")
