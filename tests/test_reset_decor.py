# -*- coding: utf-8 -*-
"""v0.6.1 ①：/reset 识别前剥离尾部机器标记（issue #2 / qq_tools 冲突）。

issue #2 的根因：``astrbot_plugin_qq_tools`` 开了 ``show_message_id`` 后，会往
``event.message_str`` 尾部追加 ``[MSG_ID:-2037476869]`` 之类的机器标记；
``reset`` 识别是精确匹配，带上后缀就认不出来，``_reset_pending`` 不置位，
下一轮就跳过自动注入（表现为"reset 后她失忆"）。

修复只归一化**判断用**的文本，不回写 ``event.message_str``（与 P3-14 同一纪律）。
baseline（v0.6.0）没有尾部剥离，因此这里全部只驱动 refactor 侧，不做差分。
"""

from __future__ import annotations

import importlib

from astrbot.api.provider import ProviderRequest

from harness import env, loader
from harness.fakes import make_event
from harness.world import DIARY_TEXT, World, message

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"

_main_mod = loader.get_module(env.SIDE_REFACTOR)
common = importlib.import_module(
    _main_mod.__name__.rsplit(".", 1)[0] + ".memory_diary.common"
)


def _event(text: str, *, sender_id: str = SENDER, umo: str = UMO):
    return make_event(text, sender_id=sender_id, sender_name="阿罗", umo=umo)


def _request(prompt: str = "你好", *, contexts=None):
    return ProviderRequest(
        prompt=prompt,
        contexts=list(contexts) if contexts is not None else [
            {"role": "user", "content": "你好"}
        ],
        system_prompt="SYS",
    )


def _extra_texts(req) -> list:
    return [getattr(part, "text", str(part)) for part in req.extra_user_content_parts]


# ---------------------------------------------------------------------------
# 纯函数：strip_trailing_decorations
# ---------------------------------------------------------------------------


def test_strip_trailing_decorations_pure_function():
    strip = common.strip_trailing_decorations
    # 单个/多个尾部标记都要剥掉（qq_tools 可能同时挂多个）
    assert strip("/reset [MSG_ID:-2037476869]") == "/reset"
    assert strip("/reset [MSG_ID:1][MSG_ID:2]") == "/reset"
    assert strip("reset [MSG_ID:1] ") == "reset"
    assert strip("/重置\t[MSG_ID:123]") == "/重置"
    # 纯标记剥成空串（不会命中 reset 精确匹配，行为与 v0.6.0 一致）
    assert strip("[MSG_ID:1]") == ""
    # 没有尾部标记时逐字节不变
    for text in ("/reset", "reset", "/重置", "重置", "", "你好", "/reset 你好"):
        assert strip(text) == text
    # 只剥"尾部"：中间的方括号内容不动，避免误伤正文
    assert strip("请解释[note:abc]这个标记") == "请解释[note:abc]这个标记"
    assert strip("前缀[MSG_ID:1] 后缀") == "前缀[MSG_ID:1] 后缀"
    # 组合纪律：先剥不可见字符、再剥尾部装饰，得到可精确匹配的文本
    combined = common.strip_trailing_decorations(
        common.strip_invisible("/reset\u200b[MSG_ID:1]")
    )
    assert combined.strip().lower() == "/reset"


# ---------------------------------------------------------------------------
# 集成：on_any_message 识别带后缀的 /reset
# ---------------------------------------------------------------------------


async def test_decorated_reset_sets_pending_and_next_turn_reinjects():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
        )
        plugin._save_pref(SENDER, "all")
        plugin._injected_entries.clear()
        plugin._injected_cid.clear()

        # 先跑一轮，建立"本会话已注入过"的状态
        first = _request()
        await plugin.on_llm_request(_event("你好"), first)
        first_extra = _extra_texts(first)

        # qq_tools 追加后缀后的 /reset：仍要置 reset 标记
        decorated = "/reset [MSG_ID:-2037476869]"
        event = _event(decorated)
        await plugin.on_any_message(event)
        pending_after = set(plugin._reset_pending)
        # 纪律：只用于判断，原文不许被回写
        raw_after = event.message_str

        second = _request()
        await plugin.on_llm_request(_event("你好"), second)

        # 非 reset 文本（例如带参数）不能被误判成 reset
        noise = _event("/reset 你好 [MSG_ID:1]")
        await plugin.on_any_message(noise)
        pending_noise = set(plugin._reset_pending)

        assert len(first_extra) == 1, "第一轮应注入日记"
        assert pending_after == {UMO}, "带 [MSG_ID:...] 后缀的 /reset 必须置标记"
        assert raw_after == decorated, "reset 归一化只能用于判断，不能回写消息原文"
        assert len(_extra_texts(second)) == 1, "reset 后下一轮必须重新注入日记"
        assert pending_noise == set(), "带额外参数的文本不是 /reset，不应置标记"


async def test_plain_reset_and_zero_width_reset_still_work():
    """/reset 原有两条识别路径（纯文本、带零宽字符）保持 v0.6.0 行为。"""
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        plain = _event("/reset")
        await plugin.on_any_message(plain)
        pending_plain = set(plugin._reset_pending)
        plugin._reset_pending.clear()

        weird = _event("重置\u200b")
        await plugin.on_any_message(weird)
        pending_zero = set(plugin._reset_pending)
        plugin._reset_pending.clear()

        upper = _event("/RESET [MSG_ID:42]")
        await plugin.on_any_message(upper)
        pending_upper = set(plugin._reset_pending)

        assert pending_plain == {UMO}
        assert pending_zero == {UMO}
        assert pending_upper == {UMO}


async def test_non_reset_messages_do_not_set_pending():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        for text in ("你好", "/查看日记", "reset 吗", "重置啊", ""):
            await plugin.on_any_message(_event(text))
        assert set(plugin._reset_pending) == set()
