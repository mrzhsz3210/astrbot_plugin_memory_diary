# -*- coding: utf-8 -*-
"""v0.7.6：人格隔离存储 + 删除回收站 + 旧数据归属迁移。

三类验收（任务单 §7）：
1. 同 QQ 号双人格：A 写的日记 B 看不到、删不到；B 的操作不伤 A。
2. 单人格老用户升级：旧日记在默认人格下原样可见、签名不炸。
3. 删除任意日记 → /恢复日记 可找回，正文/批注完整；保留期外自动清理。
4. persona_isolation 关闭时退回 v0.7.5（全号共享一本）。
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import timedelta

from annotation_testkit import ann_data, make_annotation, seed_diary
from harness.fakes import make_event
from harness.world import (
    BRIEF_TEXT,
    DIARY_TEXT,
    FIXED_NOW,
    World,
    message,
)

SENDER = "10001"
SENDER_NAME = "阿罗"
PERSONA_A = "personaA"
IDENTITY_A = f"{SENDER}::p::{PERSONA_A}"
DAY = "20261023"
YESTERDAY = "20261022"


def private_event(text: str, **kwargs):
    return make_event(text, sender_id=SENDER, sender_name=SENDER_NAME, **kwargs)


def _persona_world(world: World, persona_id: str):
    """让指定会话用上 persona_id，并注册对应 v3 人格。"""
    inst = world.refactor
    if persona_id:
        world.persona_manager.by_id[persona_id] = f"{persona_id} 的人设"
    inst.set_history([message("user", "在吗")], persona_id=persona_id)
    return inst


def _seed_two_days(plugin, sender=SENDER):
    plugin._save_diary(
        sender, DIARY_TEXT, sender_name=SENDER_NAME,
        entry_name_hint=YESTERDAY, origin="private", mode="full",
    )
    plugin._save_diary(
        sender, BRIEF_TEXT, sender_name=SENDER_NAME,
        entry_name_hint=DAY, origin="private", mode="brief",
    )


# ---------------------------------------------------------------------------
# 一、人格隔离：身份解析
# ---------------------------------------------------------------------------


async def test_persona_identity_resolution_and_default_fallback():
    async with World(persona_prompt="默认人格") as world:
        inst = _persona_world(world, PERSONA_A)
        plugin = inst.plugin
        umo = inst.default_umo()

        identity, name = await plugin._diary_identity_info(umo, SENDER)
        assert identity == IDENTITY_A, identity
        assert name == PERSONA_A

        # 默认人格（会话没指定 persona）：身份就是原始 sender（v0.7.5 口径）
        inst.set_history([], persona_id="")
        identity2, name2 = await plugin._diary_identity_info(umo, SENDER)
        assert identity2 == SENDER and name2 == ""

        # AstrBot 系统默认人格 id（"default"）也按默认人格处理
        inst.set_history([], persona_id="default")
        identity3, _ = await plugin._diary_identity_info(umo, SENDER)
        assert identity3 == SENDER


async def test_persona_identity_fallbacks():
    # 没有 persona_manager（老 AstrBot）：一律退回原始 sender，不炸
    async with World() as world:
        inst = world.refactor
        identity = await inst.plugin._diary_identity(inst.default_umo(), SENDER)
        assert identity == SENDER

    # 会话指向的人格已被删除（库里查不到）：退回原始 sender
    async with World(persona_prompt="默认人格") as world:
        inst = world.refactor
        inst.set_history([], persona_id="ghost")
        identity = await inst.plugin._diary_identity(inst.default_umo(), SENDER)
        assert identity == SENDER

    # 关闭隔离开关：即使切了人格也退回 v0.7.5（全号共享一本）
    async with World(
        config={"persona_isolation": False}, persona_prompt="默认人格"
    ) as world:
        inst = _persona_world(world, PERSONA_A)
        identity = await inst.plugin._diary_identity(inst.default_umo(), SENDER)
        assert identity == SENDER


async def test_whitelist_matches_base_sender_of_identity():
    async with World(config={"allowed_users": "10001"}) as world:
        plugin = world.refactor.plugin
        assert plugin._is_allowed(IDENTITY_A) is True
        assert plugin._is_allowed("20002::p::personaA") is False


# ---------------------------------------------------------------------------
# 一、人格隔离：存储与指令
# ---------------------------------------------------------------------------


async def test_persona_isolated_storage_and_signatures():
    async with World(persona_prompt="默认人格") as world:
        plugin = world.refactor.plugin
        plugin._save_diary(
            IDENTITY_A, DIARY_TEXT, sender_name=SENDER_NAME,
            entry_name_hint=DAY, origin="private", mode="full",
        )

        # A 的本子有一篇；默认人格（旧本子）为空——两本日记是两个文件
        assert plugin._list_entries(IDENTITY_A) == [f"diaries/{DAY}.md"]
        assert plugin._list_entries(SENDER) == []
        assert plugin._diary_path(IDENTITY_A) != plugin._diary_path(SENDER)

        # 签名域绑定复合身份：拿原始 QQ 验不过，拿复合身份才验得过（真隔离）
        with plugin._diary_zip(plugin._diary_path(IDENTITY_A)) as z:
            content = z.read(f"diaries/{DAY}.md").decode("utf-8")
            sig = z.read(f"sigs/{DAY}.sig").decode("utf-8").strip()
            readme = z.read("说明.txt").decode("utf-8")
        assert not plugin._entry_sig_matches(SENDER, f"diaries/{DAY}.md", content, sig)
        assert plugin._entry_sig_matches(IDENTITY_A, f"diaries/{DAY}.md", content, sig)
        # 说明.txt 交代归属（Web 验签靠用户ID行回推身份）
        assert f"用户ID：{IDENTITY_A}" in readme
        assert f"所属人格：{PERSONA_A}" in readme


async def test_persona_isolation_view_and_delete_commands():
    async with World(
        config={"use_persona": False, "diary_op_interval": 0},
        persona_prompt="默认人格",
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            IDENTITY_A, DIARY_TEXT, sender_name=SENDER_NAME,
            entry_name_hint=DAY, origin="private", mode="full",
        )

        # 人格 A 的会话：/查看日记 列得出
        _persona_world(world, PERSONA_A)
        out = "\n".join(await inst.run_handler("view_diary", private_event("/查看日记")))
        assert "2026-10-23" in out

        # 切回默认人格：看不到 A 的日记
        inst.set_history([message("user", "在吗")], persona_id="")
        out = "\n".join(await inst.run_handler("view_diary", private_event("/查看日记")))
        assert "还没有写过日记" in out

        # 默认人格下删除全部：A 的日记完好
        inst.set_replies([{"text": "全部"}, {"text": "确认删除"}])
        inst.plugin.config["diary_op_interval"] = 0
        # 先给默认人格也种一篇，删除交互才有东西可删
        plugin._save_diary(
            SENDER, BRIEF_TEXT, sender_name=SENDER_NAME,
            entry_name_hint=DAY, origin="private", mode="brief",
        )
        out = "\n".join(
            await inst.run_handler("delete_diary", private_event("/删除日记"))
        )
        assert "全部日记已删除" in out
        assert plugin._list_entries(SENDER) == []
        assert plugin._list_entries(IDENTITY_A) == [f"diaries/{DAY}.md"]
        # A 的内容验签仍然有效（没被动过）
        content, tampered = plugin._load_entry(IDENTITY_A, f"diaries/{DAY}.md")
        assert not tampered and "用户画像" in content


async def test_memo_namespaced_by_persona_in_shared_pref_file():
    async with World(persona_prompt="默认人格") as world:
        plugin = world.refactor.plugin
        plugin._append_memo(IDENTITY_A, "人格A的备忘", scene="private")
        plugin._append_memo(SENDER, "默认人格的备忘", scene="private")

        assert [m["text"] for m in plugin._load_memos(IDENTITY_A)] == ["人格A的备忘"]
        assert [m["text"] for m in plugin._load_memos(SENDER)] == ["默认人格的备忘"]

        # 密码/偏好仍是用户级：pref 文件只有一份（按原始 QQ）
        pref_files = list(plugin.data_dir.glob("pref_*.json"))
        assert len(pref_files) == 1

        # 交互删除回写走分槽口径：清 A 的备忘不伤默认人格的
        assert plugin._save_memos_list(IDENTITY_A, []) is True
        assert plugin._load_memos(IDENTITY_A) == []
        assert [m["text"] for m in plugin._load_memos(SENDER)] == ["默认人格的备忘"]


# ---------------------------------------------------------------------------
# 二、旧数据迁移：升级后旧日记归属默认人格
# ---------------------------------------------------------------------------


async def test_legacy_diaries_stay_with_default_persona():
    async with World(persona_prompt="默认人格") as world:
        inst = world.refactor
        plugin = inst.plugin
        # 模拟 v0.7.5 时代写下的日记（sender 维度、旧签名域）
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name=SENDER_NAME,
            entry_name_hint=DAY, origin="private", mode="full",
        )
        legacy_path = plugin._diary_path(SENDER)

        # 切到人格 A：看不到旧日记，但旧日记在默认人格下原样可见、签名不炸
        _persona_world(world, PERSONA_A)
        identity = await plugin._diary_identity(inst.default_umo(), SENDER)
        assert identity == IDENTITY_A
        assert plugin._list_entries(identity) == []
        assert plugin._list_entries(SENDER) == [f"diaries/{DAY}.md"]
        content, tampered = plugin._load_entry(SENDER, f"diaries/{DAY}.md")
        assert not tampered and "用户画像" in content
        # 文件名 key 没有因为升级而变化（没有改名迁移）
        assert legacy_path.name == f"diary_{plugin._diary_file_key(SENDER)}.zip"
        assert legacy_path.exists()


# ---------------------------------------------------------------------------
# 二补、跨人格导出/导入：同一 QQ 号在自己的人格之间搬家是合法路径
# ---------------------------------------------------------------------------


async def test_cross_persona_bundle_import_same_qq_allowed():
    async with World(persona_prompt="默认人格") as world:
        plugin = world.refactor.plugin
        # 人格 A 名下的日记 → 导出迁移包
        plugin._save_diary(
            IDENTITY_A, DIARY_TEXT, sender_name=SENDER_NAME,
            entry_name_hint=DAY, origin="private", mode="full",
        )
        bundle = plugin._build_export_bundle(
            IDENTITY_A, plugin._diary_path(IDENTITY_A),
            plugin._list_entries(IDENTITY_A),
        )
        try:
            (owner_id, key, entries, images, annotations, provenance,
             current_self, materials, anchor_archive) = plugin._verify_bundle(bundle)
            assert owner_id == IDENTITY_A
            # 同一 QQ 在默认人格会话里导入 A 的包：按原始 QQ 比对，必须放行
            mode = plugin._install_bundle(
                SENDER, owner_id, key, entries, SENDER_NAME,
                images=images, annotations=annotations, provenance=provenance,
                current_self=current_self, materials=materials,
                anchor_archive=anchor_archive,
            )
            assert mode in ("same_key", "resign")
            assert plugin._list_entries(SENDER) == [f"diaries/{DAY}.md"]
            content, tampered = plugin._load_entry(SENDER, f"diaries/{DAY}.md")
            assert not tampered and "用户画像" in content
        finally:
            bundle.unlink(missing_ok=True)


async def test_cross_qq_bundle_import_still_rejected():
    async with World(persona_prompt="默认人格") as world:
        plugin = world.refactor.plugin
        plugin._save_diary(
            IDENTITY_A, DIARY_TEXT, sender_name=SENDER_NAME,
            entry_name_hint=DAY, origin="private", mode="full",
        )
        bundle = plugin._build_export_bundle(
            IDENTITY_A, plugin._diary_path(IDENTITY_A),
            plugin._list_entries(IDENTITY_A),
        )
        try:
            (owner_id, key, entries, images, annotations, provenance,
             current_self, materials, anchor_archive) = plugin._verify_bundle(bundle)
            # 别的 QQ 号（哪怕人格 id 同名）导入：按原始 QQ 比对，必须拒绝
            rejected = False
            try:
                plugin._install_bundle(
                    "20002::p::personaA", owner_id, key, entries, "别人",
                    images=images, annotations=annotations,
                    provenance=provenance, current_self=current_self,
                    materials=materials, anchor_archive=anchor_archive,
                )
            except ValueError:
                rejected = True
            assert rejected, "跨 QQ 导入必须被拒绝"
        finally:
            bundle.unlink(missing_ok=True)


async def test_delete_moves_entries_to_trash_and_restores():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _seed_two_days(plugin)

        deleted = plugin._delete_entries(SENDER, [f"diaries/{YESTERDAY}.md"])
        assert deleted == 1
        assert plugin._list_entries(SENDER) == [f"diaries/{DAY}.md"]

        items = plugin.list_trash(SENDER)
        assert len(items) == 1
        assert items[0]["days"] == [YESTERDAY]
        assert items[0]["kind"] == "entries"

        restored, skipped, err = plugin.restore_trash(SENDER, items[0]["id"])
        assert err == "" and restored == [YESTERDAY] and skipped == []
        assert plugin._list_entries(SENDER) == [
            f"diaries/{YESTERDAY}.md", f"diaries/{DAY}.md"
        ]
        # 恢复后验签一致（原签放回）
        content, tampered = plugin._load_entry(SENDER, f"diaries/{YESTERDAY}.md")
        assert not tampered and "用户画像" in content
        # 回收站条目已消费
        assert plugin.list_trash(SENDER) == []


async def test_delete_all_moves_whole_zip_to_trash():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _seed_two_days(plugin)

        assert plugin._delete_diary(SENDER) is True
        assert plugin._list_entries(SENDER) == []

        items = plugin.list_trash(SENDER)
        assert len(items) == 1 and items[0]["kind"] == "all"
        assert sorted(items[0]["days"]) == [YESTERDAY, DAY]

        restored, skipped, err = plugin.restore_trash(SENDER, items[0]["id"])
        assert err == "" and sorted(restored) == [YESTERDAY, DAY]
        assert len(plugin._list_entries(SENDER)) == 2


async def test_restore_does_not_overwrite_newer_content():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name=SENDER_NAME,
            entry_name_hint=DAY, origin="private", mode="full",
        )
        plugin._delete_entries(SENDER, [f"diaries/{DAY}.md"])
        # 删除后同一天又写了新日记：恢复不覆盖新内容
        plugin._save_diary(
            SENDER, BRIEF_TEXT, sender_name=SENDER_NAME,
            entry_name_hint=DAY, origin="private", mode="brief",
        )
        items = plugin.list_trash(SENDER)
        restored, skipped, err = plugin.restore_trash(SENDER, items[0]["id"])
        assert restored == [] and skipped == [DAY] and err
        content, tampered = plugin._load_entry(SENDER, f"diaries/{DAY}.md")
        assert not tampered and "简记" in content


async def test_restore_brings_annotations_back():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        seed_diary(plugin)
        plugin.save_annotations(
            SENDER, ann_data([make_annotation(1, day=DAY)])
        )

        plugin._delete_entries(SENDER, [f"diaries/{DAY}.md"])
        # 批注已级联清掉（zip 没了）
        assert plugin._list_entries(SENDER) == []

        items = plugin.list_trash(SENDER)
        restored, _skipped, err = plugin.restore_trash(SENDER, items[0]["id"])
        assert err == "" and restored == [DAY]
        loaded, status = plugin.load_annotations(SENDER)
        assert status == "ok"
        assert [a["id"] for a in loaded["annotations"]] == [f"ann_{DAY}_1"]


async def test_trash_disabled_by_zero_retention():
    async with World(
        config={"use_persona": False, "trash_retention_days": 0}
    ) as world:
        plugin = world.refactor.plugin
        _seed_two_days(plugin)
        deleted = plugin._delete_entries(SENDER, [f"diaries/{YESTERDAY}.md"])
        assert deleted == 1
        assert plugin.list_trash(SENDER) == []
        assert not (plugin.data_dir / "trash").exists()

        # /恢复日记 明确告知回收站已关闭
        out = "\n".join(
            await world.refactor.run_handler("restore_diary", private_event("/恢复日记"))
        )
        assert "回收站已被管理员关闭" in out


async def test_trash_purges_after_retention():
    async with World(
        config={"use_persona": False, "trash_retention_days": 1}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed_two_days(plugin)
        plugin._delete_entries(SENDER, [f"diaries/{YESTERDAY}.md"])
        assert len(plugin.list_trash(SENDER)) == 1

        # 保留期内：例行清理不动它
        assert plugin._purge_trash() == 0
        assert len(plugin.list_trash(SENDER)) == 1

        # 两天后：超过保留期，被物理清理
        inst.set_now(FIXED_NOW + timedelta(days=2, seconds=1))
        assert plugin._purge_trash() == 1
        assert plugin.list_trash(SENDER) == []
        assert not list((plugin.data_dir / "trash").glob("trash_*.zip"))


async def test_tampered_trash_item_not_listed_nor_restored():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        _seed_two_days(plugin)
        plugin._delete_entries(SENDER, [f"diaries/{YESTERDAY}.md"])
        trash_files = list((plugin.data_dir / "trash").glob("trash_*.zip"))
        assert len(trash_files) == 1

        # 篡改回收站 payload（改日记正文字节），manifest 签名必须兜住
        path = trash_files[0]
        with zipfile.ZipFile(path) as z:
            data = {n: z.read(n) for n in z.namelist()}
        data[f"payload/diaries/{YESTERDAY}.md"] = b"tampered content"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n, b in data.items():
                z.writestr(n, b)
        path.write_bytes(buf.getvalue())

        assert plugin.list_trash(SENDER) == []
        restored, skipped, err = plugin.restore_trash(
            SENDER, path.stem[len("trash_"):]
        )
        assert restored == [] and err


async def test_restore_command_interactive_flow():
    async with World(
        config={"use_persona": False, "diary_op_interval": 0}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed_two_days(plugin)
        plugin._delete_entries(SENDER, [f"diaries/{YESTERDAY}.md"])

        inst.set_replies([{"text": "1"}])
        out = "\n".join(
            await inst.run_handler("restore_diary", private_event("/恢复日记"))
        )
        assert "回收站" in out and "已恢复" in out
        assert "2026-10-22" in out
        assert plugin._list_entries(SENDER) == [
            f"diaries/{YESTERDAY}.md", f"diaries/{DAY}.md"
        ]


async def test_restore_command_empty_trash_and_group_reject():
    async with World(
        config={"use_persona": False, "diary_op_interval": 0}
    ) as world:
        inst = world.refactor
        out = "\n".join(
            await inst.run_handler("restore_diary", private_event("/恢复日记"))
        )
        assert "回收站是空的" in out

        group = make_event(
            "/恢复日记", sender_id=SENDER, sender_name=SENDER_NAME, group_id="88888"
        )
        out = "\n".join(await inst.run_handler("restore_diary", group))
        assert "私聊" in out


async def test_trash_is_also_persona_namespaced():
    async with World(persona_prompt="默认人格") as world:
        plugin = world.refactor.plugin
        _seed_two_days(plugin, sender=IDENTITY_A)
        plugin._delete_entries(IDENTITY_A, [f"diaries/{YESTERDAY}.md"])

        # A 的回收站有条目；默认人格的回收站为空
        assert len(plugin.list_trash(IDENTITY_A)) == 1
        assert plugin.list_trash(SENDER) == []

        # 用错误的身份恢复：拒绝
        item = plugin.list_trash(IDENTITY_A)[0]
        restored, _skipped, err = plugin.restore_trash(SENDER, item["id"])
        assert restored == [] and err
        # 正确身份恢复成功
        restored, _skipped, err = plugin.restore_trash(IDENTITY_A, item["id"])
        assert restored == [YESTERDAY] and err == ""


# ---------------------------------------------------------------------------
# 四、{persona_name} 占位符
# ---------------------------------------------------------------------------


async def test_extra_instruction_persona_placeholder():
    async with World(
        config={
            "use_persona": False,
            "extra_instruction": "请以{persona_name}的视角写日记",
        },
        persona_prompt="默认人格",
    ) as world:
        inst = _persona_world(world, PERSONA_A)
        inst.set_history(
            [message("user", "今天聊了考试"), message("assistant", "加油")],
            persona_id=PERSONA_A,
        )
        inst.provider.replies[:] = [BRIEF_TEXT]
        out = await inst.run_handler("write_diary_cmd", private_event("/日记"))
        assert any("日记写好啦" in t for t in out)
        prompts = [c["prompt"] for c in inst.provider.calls]
        assert any(f"请以{PERSONA_A}的视角写日记" in p for p in prompts), prompts

    # 没有占位符时：prompt 与旧版一致（不含人格名渲染逻辑的产物）
    async with World(
        config={"use_persona": False, "extra_instruction": "语气温柔一些"},
        persona_prompt="默认人格",
    ) as world:
        inst = _persona_world(world, PERSONA_A)
        inst.set_history(
            [message("user", "今天聊了考试"), message("assistant", "加油")],
            persona_id=PERSONA_A,
        )
        inst.provider.replies[:] = [BRIEF_TEXT]
        await inst.run_handler("write_diary_cmd", private_event("/日记"))
        prompts = [c["prompt"] for c in inst.provider.calls]
        assert any("语气温柔一些" in p for p in prompts)
