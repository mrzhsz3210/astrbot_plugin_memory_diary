# -*- coding: utf-8 -*-
"""v0.6.2 审查修复：注入状态隔离 / 重复注入 / 失败回滚 / 代际失效 / EWMA 采样。

覆盖审查条目：

- P1-4 群聊多用户：注入去重、``/注入日记`` 与哨兵状态按 (umo, sender_id) 命名空间化；
- P2-10 临时注入模式下 ``/注入日记`` 与自动注入同轮重复注入；
- P2-12 首轮请求失败后本代际永不注入（注入状态提前落位）；
- P3-12 ``/new`` 换会话后 ``_last_context_used`` 未失效；
- P3-13 EWMA 把首步静态估算与多步工具 agent 最终步 usage 配对。

这些行为在 baseline（v0.6.2 发布包）里就是待修的缺陷，所以全部只驱动
refactor 侧，不做 baseline 差分对比（参考 test_v055_features.py 的写法）。
"""

from __future__ import annotations

from types import SimpleNamespace

from astrbot.api.provider import ProviderRequest

from harness.fakes import make_event
from harness.world import DIARY_TEXT, World, message, wait_for_diary_tasks

SENDER_A = "10001"
SENDER_B = "10002"
SENDER_C = "10003"
GROUP = "88888"
GROUP_UMO = f"aiocqhttp:GroupMessage:{GROUP}"
PRIVATE_UMO_A = f"aiocqhttp:FriendMessage:{SENDER_A}"

# 两篇内容可区分的群聊日记，用来断言"注入的是当前说话人的日记"
BODY_A = "A的群聊日记独有正文"
BODY_B = "B的群聊日记独有正文"


def _request(prompt: str = "你好", *, contexts=None, system_prompt: str = "SYS"):
    return ProviderRequest(
        prompt=prompt,
        contexts=(
            list(contexts)
            if contexts is not None
            else [{"role": "user", "content": "你好"}]
        ),
        system_prompt=system_prompt,
    )


def _group_event(
    text: str = "你好",
    *,
    sender_id: str = SENDER_A,
    sender_name: str = "阿罗",
    umo: str = GROUP_UMO,
):
    return make_event(
        text,
        sender_id=sender_id,
        sender_name=sender_name,
        group_id=GROUP,
        umo=umo,
    )


def _private_event(
    text: str = "你好", *, sender_id: str = SENDER_A, sender_name: str = "阿罗"
):
    return make_event(text, sender_id=sender_id, sender_name=sender_name)


def _extra_texts(req) -> list[str]:
    return [getattr(part, "text", str(part)) for part in req.extra_user_content_parts]


def _all_text(req) -> str:
    return "\n".join(_extra_texts(req)) + "\n" + (req.system_prompt or "")


def _ack(plugin, event, resp=None):
    """模拟 AstrBot 在请求成功后触发的 on_llm_response（P2-12 才确认注入）。

    harness 不会自动调钩子，多轮注入用例必须显式 ack，否则会被当作
    "首轮请求失败"从而允许重新注入。resp 默认传 None，用来顺带确认实现能容忍
    "无 usage 的响应"（getattr(resp,"usage",None) 拿不到就直接返回，不抛异常）。
    """
    return plugin.on_llm_response(event, resp)


def _seed_group_diaries(plugin) -> None:
    plugin._save_diary(
        SENDER_A, BODY_A, sender_name="阿罗", origin="group",
        scope=f"group:{GROUP}", mode="full"
    )
    plugin._save_diary(
        SENDER_B, BODY_B, sender_name="阿堵", origin="group",
        scope=f"group:{GROUP}", mode="full"
    )


# ---------------------------------------------------------------------------
# P1-4：群聊多用户的注入按 (umo, sender) 隔离
# ---------------------------------------------------------------------------


async def test_group_second_sender_is_injected():
    """A 先被注入后，B 的请求仍要注入 B 自己的日记（修复前 B 永远为空）。"""
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed_group_diaries(plugin)
        inst.conversation_manager.set(
            GROUP_UMO, history=[message("user", "群聊历史")]
        )

        req_a = _request()
        event_a = _group_event()
        await plugin.on_llm_request(event_a, req_a)
        await _ack(plugin, event_a)
        req_b = _request()
        event_b = _group_event(sender_id=SENDER_B, sender_name="阿堵")
        await plugin.on_llm_request(event_b, req_b)
        await _ack(plugin, event_b)

        text_a, text_b = _all_text(req_a), _all_text(req_b)
        assert BODY_A in text_a, "A 的请求应注入 A 的日记"
        assert BODY_B not in text_a, "A 的请求不得注入 B 的日记"
        assert BODY_B in text_b, "B 的请求应注入 B 自己的日记（修复前为空）"
        assert BODY_A not in text_b, "B 的请求不得注入 A 的日记"
        # 去重/代际状态按 (umo, sender) 各一份
        assert f"{GROUP_UMO}::{SENDER_A}" in plugin._injected_cid
        assert f"{GROUP_UMO}::{SENDER_B}" in plugin._injected_cid


async def test_group_inject_command_not_blocked_by_other_sender():
    """A 已注入后，B 的 /注入日记 不能回"本会话都已经读过了"，且 B 能生效。"""
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed_group_diaries(plugin)
        inst.conversation_manager.set(
            GROUP_UMO, history=[message("user", "群聊历史")]
        )

        req_a = _request()
        event_a = _group_event()
        await plugin.on_llm_request(event_a, req_a)
        assert BODY_A in _all_text(req_a)
        await _ack(plugin, event_a)

        # B 的 /日记状态：A 注入过不代表 B 注入过
        status_b = "\n".join(
            await inst.run_handler(
                "diary_status",
                _group_event("/日记状态", sender_id=SENDER_B, sender_name="阿堵"),
            )
        )
        assert "本会话日记注入：本会话未注入" in status_b

        # B 的 /注入日记 必须能排进 B 的待注入队列
        texts = await inst.run_handler(
            "diary_inject",
            _group_event("/注入日记 最新", sender_id=SENDER_B, sender_name="阿堵"),
        )
        joined = "\n".join(texts)
        assert "都已经读过了" not in joined, "B 的篇目不能因 A 读过而被判重复"
        assert f"{GROUP_UMO}::{SENDER_B}" in plugin._pending_inject

        # B 的下一轮请求：拿到 B 的日记，且只出现一次（P2-10 同轮去重）
        req_b = _request()
        await plugin.on_llm_request(
            _group_event(sender_id=SENDER_B, sender_name="阿堵"), req_b
        )
        text_b = _all_text(req_b)
        assert text_b.count(BODY_B) == 1, "同轮请求里日记正文只能出现一次"
        assert BODY_A not in text_b

        # A 的状态不受 B 影响：A 这一代际已注入，不再重复
        req_a2 = _request()
        await plugin.on_llm_request(_group_event(), req_a2)
        assert _extra_texts(req_a2) == []


async def test_group_sentinel_writes_diary_for_each_member():
    """哨兵按发送者各自判定：A 触发自动写后，B 的请求仍要给自己写日记。"""
    async with World(
        config={
            "use_persona": False,
            "context_token_limit": 60000,
            "remind_ratio": 0.75,
            "auto_ratio": 0.9,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.conversation_manager.set(
            GROUP_UMO,
            history=[
                message("user", "A 说今天考试"),
                message("assistant", "加油"),
                message("user", "B 说想去江边"),
                message("assistant", "好啊"),
            ],
        )
        inst.provider.replies[:] = [DIARY_TEXT, DIARY_TEXT]

        event_a = _group_event()
        await plugin.on_llm_request(event_a, _request(prompt="x" * 99000))
        event_b = _group_event(sender_id=SENDER_B, sender_name="阿堵")
        await plugin.on_llm_request(event_b, _request(prompt="x" * 99000))
        await wait_for_diary_tasks(plugin)

        assert inst.entries(SENDER_A) == ["diaries/20261023.md"], "A 应被哨兵写日记"
        assert inst.entries(SENDER_B) == ["diaries/20261023.md"], (
            "B 也应被哨兵写日记（修复前不会有）"
        )
        assert any("上下文已用约" in t for t in event_a.result_texts())
        assert any("上下文已用约" in t for t in event_b.result_texts())


async def test_group_session_isolation_keeps_working():
    """开启 AstrBot 会话隔离（每人 group umo 不同）时行为不变坏。"""
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed_group_diaries(plugin)
        umo_a = f"{GROUP_UMO}:iso-a"
        umo_b = f"{GROUP_UMO}:iso-b"

        req_a = _request()
        await plugin.on_llm_request(_group_event(umo=umo_a), req_a)
        req_b = _request()
        await plugin.on_llm_request(
            _group_event(sender_id=SENDER_B, sender_name="阿堵", umo=umo_b), req_b
        )

        assert BODY_A in _all_text(req_a)
        assert BODY_A not in _all_text(req_b)
        assert BODY_B in _all_text(req_b)


async def test_private_injection_keys_stay_umo():
    """私聊 umo 本身按用户唯一，键仍用 umo（与既有状态/差分基线同口径）。"""
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER_A, "A 的私聊日记", sender_name="阿罗", origin="private", mode="full"
        )
        plugin._save_diary(
            SENDER_B, "B 的私聊日记", sender_name="阿堵", origin="private", mode="full"
        )
        req_a = _request()
        event_a = _private_event()
        await plugin.on_llm_request(event_a, req_a)
        await _ack(plugin, event_a)
        req_b = _request()
        event_b = _private_event(sender_id=SENDER_B, sender_name="阿堵")
        await plugin.on_llm_request(event_b, req_b)
        await _ack(plugin, event_b)
        assert "A 的私聊日记" in _all_text(req_a)
        assert "B 的私聊日记" in _all_text(req_b)
        assert set(plugin._injected_cid) == {PRIVATE_UMO_A, f"aiocqhttp:FriendMessage:{SENDER_B}"}


async def test_reset_clears_only_its_conversation_namespaces():
    """/reset 只清该会话（群聊含其下全部发送者）的命名空间，其它会话不受影响。"""
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed_group_diaries(plugin)
        plugin._save_diary(
            SENDER_C, "C 的私聊日记", sender_name="丙", origin="private", mode="full"
        )
        inst.conversation_manager.set(
            GROUP_UMO, history=[message("user", "群聊历史")]
        )
        private_c_umo = f"aiocqhttp:FriendMessage:{SENDER_C}"

        # 先让 C（私聊）与 A、B（群聊）都进入"已注入"
        await plugin.on_llm_request(
            _private_event(sender_id=SENDER_C, sender_name="丙"), _request()
        )
        await _ack(plugin, _private_event(sender_id=SENDER_C, sender_name="丙"))
        event_a = _group_event()
        await plugin.on_llm_request(event_a, _request())
        await _ack(plugin, event_a)
        event_b = _group_event(sender_id=SENDER_B, sender_name="阿堵")
        await plugin.on_llm_request(event_b, _request())
        await _ack(plugin, event_b)
        assert private_c_umo in plugin._injected_cid

        # A 在群里 /reset：群聊这一代的注入状态清空，C 的私聊状态不动
        await plugin.on_any_message(_group_event("/reset"))
        req_a2 = _request()
        event_a2 = _group_event()
        await plugin.on_llm_request(event_a2, req_a2)
        await _ack(plugin, event_a2)
        assert private_c_umo in plugin._injected_cid, "其它会话的状态不能被误清"
        assert f"{GROUP_UMO}::{SENDER_A}" in plugin._injected_cid
        assert f"{GROUP_UMO}::{SENDER_B}" not in plugin._injected_cid

        # C 的下一轮私聊请求：仍然不重复注入
        req_c2 = _request()
        await plugin.on_llm_request(
            _private_event(sender_id=SENDER_C, sender_name="丙"), req_c2
        )
        assert _extra_texts(req_c2) == []


# ---------------------------------------------------------------------------
# P2-10：临时模式下 /注入日记 与自动注入同轮重复
# ---------------------------------------------------------------------------


async def test_temporary_mode_pending_inject_not_duplicated():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        body = "重复注入检查正文"
        plugin._save_diary(
            SENDER_A, body, sender_name="阿罗", origin="private", mode="full"
        )

        # 新会话里先 /注入日记，再发第一条普通消息：本轮同时有「手动追加」与
        # 「自动注入」两条路径，临时模式必须像持久模式一样排除已追加的篇目。
        await inst.run_handler(
            "diary_inject", _private_event("/注入日记 最新")
        )
        assert PRIVATE_UMO_A in plugin._pending_inject

        req = _request(contexts=[])
        event = _private_event()
        await plugin.on_llm_request(event, req)
        await _ack(plugin, event)

        text = _all_text(req)
        assert len(_extra_texts(req)) == 1, f"extra parts 应为 1，实际 {len(_extra_texts(req))}"
        assert text.count(body) == 1, "同一篇日记在本轮请求里只能出现一次"
        assert text.count("<memory_diary>") == 1

        # 本代际已注入过：后续轮次不再重复整篇注入
        req2 = _request()
        await plugin.on_llm_request(_private_event(), req2)
        assert _extra_texts(req2) == []


# ---------------------------------------------------------------------------
# P2-12：首轮请求失败（无 on_llm_response）后，下一轮仍要注入
# ---------------------------------------------------------------------------


async def test_failed_first_request_next_turn_reinjects():
    """首轮请求在 provider 端失败（不触发 on_llm_response）后，下一轮仍要注入。

    对应报告复现路径：用户重发消息 → 新的 on_llm_request。修复前 `_injected_cid`
    已经落位，第二次请求直接 return，整个代际失忆。
    """
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER_A, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
        )
        inst.set_history([message("user", "旧消息")])

        first = _request()
        event_first = _private_event()
        await plugin.on_llm_request(event_first, first)
        assert len(_extra_texts(first)) == 1
        assert PRIVATE_UMO_A in plugin._inject_confirm, "交付后应等待响应确认"
        assert PRIVATE_UMO_A not in plugin._injected_cid, "确认前不能算已注入"

        # 模拟 provider 失败：不触发 on_llm_response；用户重发消息
        second = _request()
        event_second = _private_event()
        await plugin.on_llm_request(event_second, second)
        assert len(_extra_texts(second)) == 1, "首轮失败后下一轮必须重新注入"

        # 成功路径：on_llm_response 确认后，同代际不再重复注入
        await _ack(plugin, event_second)
        assert PRIVATE_UMO_A in plugin._injected_cid
        assert PRIVATE_UMO_A not in plugin._inject_confirm
        third = _request()
        await plugin.on_llm_request(_private_event(), third)
        assert _extra_texts(third) == []


# ---------------------------------------------------------------------------
# P3-7 配套：查看密码变更后立即吊销面板短期令牌
# ---------------------------------------------------------------------------


async def test_password_change_revokes_web_tokens():
    """设置/修改/取消查看密码后，旧的面板令牌必须立即作废（不多活 30 分钟）。

    附带校验不会凭空创建 ``_web_tokens`` 实例属性——令牌表只由 web-api 懒初始化，
    adapter 这边只清零，绝不能给类加默认 dict（会跨插件实例共享）。
    """
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        file_key = "0123456789abcdef"

        # 设置密码：本来没有令牌表，事后也不该凭空建出来
        out = "\n".join(
            await inst.run_handler(
                "diary_password", _private_event("/日记密码 设置 abcd1234")
            )
        )
        assert "设置好啦" in out
        assert not hasattr(plugin, "_web_tokens"), "设置密码时不得凭空创建令牌表"

        # 修改密码：已签发的令牌立即失效
        plugin._web_issue_token(SENDER_A, file_key)
        assert len(plugin._web_tokens) == 1
        out = "\n".join(
            await inst.run_handler(
                "diary_password",
                _private_event("/日记密码 修改 abcd1234 efgh5678"),
            )
        )
        assert "改好啦" in out
        assert plugin._web_tokens == {}, "改密码后旧令牌必须立即作废"

        # 取消密码：同样立即失效
        plugin._web_issue_token(SENDER_A, file_key)
        assert len(plugin._web_tokens) == 1
        out = "\n".join(
            await inst.run_handler(
                "diary_password", _private_event("/日记密码 取消 efgh5678")
            )
        )
        assert "查看密码已取消" in out
        assert plugin._web_tokens == {}, "取消密码后旧令牌必须立即作废"


# ---------------------------------------------------------------------------
# P3-12：/new（cid 变化）后 _last_context_used 失效
# ---------------------------------------------------------------------------


async def test_new_conversation_invalidates_last_context_used():
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            "token_calibration_enabled": False,
            "context_token_limit": 1000,
            "remind_ratio": 0.75,
            "auto_ratio": 0.95,
            "write_min_ratio": 0.8,
            "diary_op_interval": 0,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history(
            [message("user", "今天聊了考试"), message("assistant", "加油")]
        )
        # 旧会话：大请求让哨兵记下约 80% 的占用
        await plugin.on_llm_request(_private_event(), _request(prompt="聊" * 1000))
        used_old = await plugin._context_used(PRIVATE_UMO_A)
        assert used_old >= 750

        # AstrBot 内置 /new：换 conversation_id，历史为空
        inst.set_history([], cid="cid-2")
        used_new = await plugin._context_used(PRIVATE_UMO_A)
        assert used_new < 100, "换会话后不得继续复用旧会话的占用数"

        status = "\n".join(
            await inst.run_handler("diary_status", _private_event("/日记状态"))
        )
        assert f"估算 {used_new:,} / 1,000 tokens" in status

        # 手动写日记门槛同口径：新会话占用远低于 80% → 应被拦下
        inst.provider.replies[:] = [DIARY_TEXT]
        out = "\n".join(
            await inst.run_handler("write_diary_cmd", _private_event("/日记 完整"))
        )
        assert "还没到该写日记的时候" in out


# ---------------------------------------------------------------------------
# P3-13：多步工具 agent 的校准样本跳过采样
# ---------------------------------------------------------------------------


async def test_calibration_skips_multi_step_tool_rounds():
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            "token_calibration_enabled": True,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin

        # 多步工具 agent：同一 ProviderRequest 上追加了工具调用结果
        req = _request()
        await plugin.on_llm_request(_private_event(), req)
        pending = plugin._pending_estimates.get(PRIVATE_UMO_A)
        assert pending is not None and pending["estimate"] > 0
        E = pending["estimate"]
        req.tool_calls_result = [SimpleNamespace(name="web_search")]
        await plugin.on_llm_response(
            _private_event(),
            SimpleNamespace(
                usage=SimpleNamespace(input_other=2 * E, input_cached=0)
            ),
        )
        assert plugin._calib_table() == {}, "多步工具 agent 的样本必须跳过，不能污染系数"

        # 单步请求照常采样：actual = 2E → 系数 1.2
        req2 = _request()
        await plugin.on_llm_request(_private_event(), req2)
        E2 = plugin._pending_estimates[PRIVATE_UMO_A]["estimate"]
        await plugin.on_llm_response(
            _private_event(),
            SimpleNamespace(
                usage=SimpleNamespace(input_other=2 * E2, input_cached=0)
            ),
        )
        rec = plugin._calib_table()["FakeProvider"]
        assert abs(rec["factor"] - 1.2) < 1e-6
        assert rec["samples"] == 1
