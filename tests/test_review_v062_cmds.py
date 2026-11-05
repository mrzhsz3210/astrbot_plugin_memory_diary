# -*- coding: utf-8 -*-
"""v0.6.2 审查修复（cmd-layer）：P3-2 / P3-4 / P3-6 / P2-19。

覆盖条目：
- P3-2：`str.isdigit()` 放行 `²` 等上标数字后 `int()` 抛 ValueError，打断交互。
  这里驱动 refactor 侧的 /查看日记、/删除日记、/删除备忘、/写回批注、/查看批注，
  确认这些输入只回"没看懂/请回复序号"，不抛异常、不改数据；同时钉住"全角数字
  照旧可用"这一既有能力（不能因为改成 try/except 而回退）。
- P3-4：同文本同分钟的两条备忘按序号删除时只能删一条。
- P3-6：群聊 /日记帮助 的尾部提示按 group_require_at 输出两种文案。
- P2-19：写形象档案（修改/确认）只有 AstrBot 管理员能做；只读查看与重拟提案
  保持白名单用户可用。

这些行为都是 v0.6.2 修正（baseline 是旧行为），所以全部用 world.refactor 单独驱动，
不走差分；固定文案断言配合 use_persona=False，避免依赖模型回复。
"""

from __future__ import annotations

from annotation_testkit import (
    SENDER,
    SENDER_NAME,
    UMO,
    ann_config,
    make_annotation,
    seed_annotations,
    seed_diary,
)
from harness.fakes import at_me, make_event
from harness.world import World
from image_testkit import img_config

SUPERSCRIPT = "²"  # isdigit() == True，但 int("²") 抛 ValueError
FULLWIDTH_ONE = "１"  # isdigit() == True，int("１") == 1（既有能力，不能回退）


def _event(text: str, *, group_id: str = "", sender_id: str = SENDER, messages=None):
    return make_event(
        text,
        sender_id=sender_id,
        sender_name=SENDER_NAME,
        group_id=group_id,
        umo=("aiocqhttp:GroupMessage:88888" if group_id else UMO),
        messages=messages,
    )


def _admin_event(text: str, **kwargs):
    """假事件没有 role，用 role="admin" 标记 AstrBot 管理员（P2-19 判定口径）。"""
    event = _event(text, **kwargs)
    event.role = "admin"
    return event


# ---------------------------------------------------------------------------
# P3-2：上标数字不得打断交互（isdigit 但 int 解析失败）
# ---------------------------------------------------------------------------


async def test_superscript_reply_rejected_without_crash():
    async with World(config=ann_config(diary_op_interval=0)) as world:
        inst = world.refactor
        plugin = inst.plugin
        seed_annotations(plugin, [make_annotation(1, note="这条批注不该被删改。")])

        # /查看日记：列表后用户回复「²」→ 应回"没看懂"，不能抛 ValueError
        inst.set_replies([{"text": SUPERSCRIPT}])
        view_out = await inst.run_handler("view_diary", _event("/查看日记"))
        assert any("没看懂" in text for text in view_out), view_out

        # /删除日记：回「²」→ 没看懂且一篇都不删
        inst.set_replies([{"text": SUPERSCRIPT}])
        delete_out = await inst.run_handler("delete_diary", _event("/删除日记"))
        assert any("没看懂" in text for text in delete_out), delete_out
        assert inst.entries(SENDER) == ["diaries/20261023.md"], "没看懂时不能删日记"

        # /删除备忘：回「²」→ 没看懂且两条备忘都原样保留
        plugin._append_memo(SENDER, "备忘A", scene="private")
        plugin._append_memo(SENDER, "备忘B", scene="private")
        inst.set_replies([{"text": SUPERSCRIPT}])
        memo_out = await inst.run_handler("memo_delete", _event("/删除备忘"))
        assert any("没看懂" in text for text in memo_out), memo_out
        assert [m["text"] for m in plugin._load_memos(SENDER)] == ["备忘A", "备忘B"]

        # /写回批注：选号阶段回「²」→ 旧实现 int() 崩，现在应回"请回复序号"
        inst.set_replies([{"text": SUPERSCRIPT}])
        writeback_out = await inst.run_handler(
            "writeback_annotation", _event("/写回批注")
        )
        assert any("请回复序号" in text for text in writeback_out), writeback_out

        # /查看批注 后面跟「²」：只读指令不解析参数，不能报错
        view_ann_out = await inst.run_handler("view_annotations", _event("/查看批注 ²"))
        assert view_ann_out and any("批注" in text for text in view_ann_out), view_ann_out


async def test_fullwidth_digits_still_parse_as_before():
    """P3-2 不能回退既有能力：全角数字 isdigit()+int() 现在仍要能用。"""
    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        inst = world.refactor
        plugin = inst.plugin
        seed_diary(plugin, "第一天写在前面。", day="20261022")
        seed_diary(plugin, "第二天写在后面。", day="20261023")

        # /查看日记 回「１」→ 读第一篇（最早一天）
        inst.set_replies([{"text": FULLWIDTH_ONE}])
        view_out = await inst.run_handler("view_diary", _event("/查看日记"))
        assert any("第一天写在前面。" in text for text in view_out), view_out

        # /删除日记 回「１」→ 删掉第一篇
        inst.set_replies([{"text": FULLWIDTH_ONE}])
        delete_out = await inst.run_handler("delete_diary", _event("/删除日记"))
        assert any("已删除" in text for text in delete_out), delete_out
        assert inst.entries(SENDER) == ["diaries/20261023.md"]

        # /删除备忘 回「１」→ 删掉第一条
        plugin._append_memo(SENDER, "全角备忘A", scene="private")
        plugin._append_memo(SENDER, "全角备忘B", scene="private")
        inst.set_replies([{"text": FULLWIDTH_ONE}])
        memo_out = await inst.run_handler("memo_delete", _event("/删除备忘"))
        assert any("已删除备忘" in text for text in memo_out), memo_out
        assert [m["text"] for m in plugin._load_memos(SENDER)] == ["全角备忘B"]


# ---------------------------------------------------------------------------
# P3-4：按序号只删一条（同文本同分钟也不许一次删两条）
# ---------------------------------------------------------------------------


async def test_memo_delete_by_index_removes_only_one_duplicate():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        # 两次追加同文本，固定时钟下 time 也相同 —— 正是旧 bug 的触发条件
        plugin._append_memo(SENDER, "同一件事", scene="private")
        plugin._append_memo(SENDER, "同一件事", scene="private")
        plugin._append_memo(SENDER, "另一件", scene="private")
        before = plugin._load_memos(SENDER)
        assert [m["text"] for m in before] == ["同一件事", "同一件事", "另一件"]
        assert before[0]["time"] == before[1]["time"], "两条备忘应在同一分钟"

        inst.set_replies([{"text": "2"}])
        out = await inst.run_handler("memo_delete", _event("/删除备忘"))
        assert any("已删除备忘" in text for text in out), out
        after = [m["text"] for m in plugin._load_memos(SENDER)]
        # 第 2 条是重复项：只删它一个，第 1 条必须还在（旧实现会把两条都删掉）
        assert after == ["同一件事", "另一件"], f"按序号 2 只应删一条，实际 {after!r}"

        # 「全部」清空行为保持原样
        inst.set_replies([{"text": "全部"}])
        await inst.run_handler("memo_delete", _event("/删除备忘"))
        assert plugin._load_memos(SENDER) == []


# ---------------------------------------------------------------------------
# P3-6：群聊 /日记帮助 提示语跟 group_require_at 一致
# ---------------------------------------------------------------------------


async def test_group_help_tip_follows_group_require_at():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor

        # 默认 group_require_at=false：群里不用 @ 也能用，提示语不能要求 @
        default_out = await inst.run_handler(
            "diary_help", _event("/日记帮助", group_id="88888")
        )
        default_text = "\n".join(default_out)
        assert "本群多 AI 防冲突" in default_text
        assert "直接发指令即可" in default_text
        assert "在指令后 @我 才是叫我哦" not in default_text

        # 群里 @我 时默认同样是 false 文案
        at_out = await inst.run_handler(
            "diary_help",
            _event("/日记帮助", group_id="88888", messages=[at_me("99999")]),
        )
        assert "直接发指令即可" in "\n".join(at_out)

        # 配置改成必须 @：才提示"在指令后 @我"
        inst.plugin.config["group_require_at"] = True
        require_out = await inst.run_handler(
            "diary_help",
            _event("/日记帮助", group_id="88888", messages=[at_me("99999")]),
        )
        require_text = "\n".join(require_out)
        assert "在指令后 @我 才是叫我哦" in require_text
        assert "直接发指令即可" not in require_text

        # 私聊不追加任何群聊提示
        private_text = "\n".join(
            await inst.run_handler("diary_help", _event("/日记帮助"))
        )
        assert "本群多 AI 防冲突" not in private_text


# ---------------------------------------------------------------------------
# P2-19：形象档案写操作限 AstrBot 管理员；查看/重拟保持白名单可用
# ---------------------------------------------------------------------------


async def test_persona_write_requires_admin_but_read_and_propose_allowed():
    async with World(
        config=img_config(persona_image_desc="旧形象，黑色长发。"),
        persona_prompt="你是银发红瞳的少女，常穿白色长裙。",
    ) as world:
        inst = world.refactor
        plugin = inst.plugin

        # 1) 普通用户 /形象 修改 → 拒绝，配置不变
        modify_out = await inst.run_handler(
            "persona_image_cmd", _event("/形象 修改 普通用户想改的新形象。")
        )
        modify_text = "\n".join(modify_out)
        assert "只有 AstrBot 管理员能改" in modify_text, modify_text
        assert plugin.config.get("persona_image_desc") == "旧形象，黑色长发。"

        # 2) 只读查看仍可用，且用法里写清写操作的权限
        show_text = "\n".join(
            await inst.run_handler("persona_image_cmd", _event("/形象"))
        )
        assert "旧形象，黑色长发。" in show_text
        assert "管理员" in show_text

        # 3) 普通用户 /形象 重拟 仍可用（只生成待确认提案，不写配置）
        inst.provider.replies[:] = ["银发红瞳，白色长裙，颈间一枚月牙吊坠。"]
        propose_text = "\n".join(
            await inst.run_handler("persona_image_cmd", _event("/形象 重拟"))
        )
        assert "新的形象提案" in propose_text, propose_text
        pending = plugin._load_pending_persona_desc(SENDER)
        assert pending.get("desc") == "银发红瞳，白色长裙，颈间一枚月牙吊坠。"
        assert plugin.config.get("persona_image_desc") == "旧形象，黑色长发。"

        # 4) 普通用户 /形象 确认 → 拒绝，提案保留、配置不变
        confirm_out = await inst.run_handler("persona_image_cmd", _event("/形象 确认"))
        assert "只有 AstrBot 管理员能改" in "\n".join(confirm_out)
        assert plugin.config.get("persona_image_desc") == "旧形象，黑色长发。"
        assert plugin._load_pending_persona_desc(SENDER).get("desc") == "银发红瞳，白色长裙，颈间一枚月牙吊坠。"

        # 5) 管理员 /形象 修改 → 生效
        admin_modify = await inst.run_handler(
            "persona_image_cmd", _admin_event("/形象 修改 管理员定的新形象。")
        )
        assert "已更新" in "\n".join(admin_modify), admin_modify
        assert plugin.config.get("persona_image_desc") == "管理员定的新形象。"

        # 6) 管理员 /形象 确认 → 采纳待确认提案
        admin_confirm = await inst.run_handler(
            "persona_image_cmd", _admin_event("/形象 确认")
        )
        assert "已确认并写入配置" in "\n".join(admin_confirm), admin_confirm
        assert (
            plugin.config.get("persona_image_desc")
            == "银发红瞳，白色长裙，颈间一枚月牙吊坠。"
        )
        assert plugin._load_pending_persona_desc(SENDER) == {}


def test_persona_admin_detection_paths():
    """P2-19 判定口径：is_admin() 优先，异常/缺失时回退 role，都取不到按非管理员。"""
    import asyncio

    async def scenario():
        async with World(config=img_config()) as world:
            plugin = world.refactor.plugin

            by_method = _event("/形象")
            by_method.is_admin = lambda: True
            assert plugin._is_admin_event(by_method) is True

            by_role = _event("/形象")
            by_role.role = "admin"
            assert plugin._is_admin_event(by_role) is True

            plain = _event("/形象")
            assert plugin._is_admin_event(plain) is False

            class _BrokenIsAdmin:
                role = "admin"

                def is_admin(self):
                    raise RuntimeError("boom")

            # is_admin 抛异常 → 回退 role 判定
            assert plugin._is_admin_event(_BrokenIsAdmin()) is True

    asyncio.run(scenario())
