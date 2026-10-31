# -*- coding: utf-8 -*-
"""v0.5.5 新特性测试：白名单 / 零宽字符 / 一键备份 / token 估算与校准。

这些行为在 baseline（v0.5.4 原始实现）里不存在或语义不同，
因此一律只驱动 refactor 侧实例，不做差分比较；
旧公式回退路径的等价性由既有差分套件保证（harness 默认 v2 关闭）。
"""

from __future__ import annotations

import importlib
import json
import os
import zipfile

from astrbot.api.provider import ProviderRequest
from astrbot.api.web import bind_request_context

from harness import env, loader
from harness.fakes import FakeWebRequest, at_me, at_other, make_event
from harness.world import DIARY_TEXT, World, message

SENDER = "10001"
OTHER = "20002"
UMO = "aiocqhttp:FriendMessage:10001"
UMO_OTHER = "aiocqhttp:FriendMessage:20002"

# 重构侧包内的纯函数估算器模块
_main_mod = loader.get_module(env.SIDE_REFACTOR)
est = importlib.import_module(
    _main_mod.__name__.rsplit(".", 1)[0] + ".memory_diary.token_estimator"
)


def _event(text: str = "你好", *, sender_id: str = SENDER, group_id: str = "",
           umo: str = "", messages=None):
    return make_event(
        text,
        sender_id=sender_id,
        sender_name="阿罗",
        group_id=group_id,
        umo=umo or (f"aiocqhttp:FriendMessage:{sender_id}" if not group_id else
                    "aiocqhttp:GroupMessage:88888"),
        messages=messages,
    )


def _request(prompt: str = "你好", *, contexts=None, system_prompt: str = "SYS"):
    return ProviderRequest(
        prompt=prompt,
        contexts=list(contexts) if contexts is not None
        else [{"role": "user", "content": "你好"}],
        system_prompt=system_prompt,
    )


def _extra_texts(req) -> list:
    return [getattr(part, "text", str(part)) for part in req.extra_user_content_parts]


class _Usage:
    def __init__(self, input_other=0, input_cached=0):
        self.input_other = input_other
        self.input_cached = input_cached


class _Resp:
    def __init__(self, usage):
        self.usage = usage


# ---------------------------------------------------------------------------
# ① 白名单
# ---------------------------------------------------------------------------


async def test_whitelist_parse_and_empty_allows_all():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        # 空名单：所有人放行（向后兼容）
        assert plugin._is_allowed("10001") is True
        assert plugin._is_allowed("") is True

        plugin.config["allowed_users"] = "10001, 20002，30003、40004\n50005 60006"
        assert plugin._is_allowed("10001") is True
        assert plugin._is_allowed("30003") is True
        assert plugin._is_allowed("50005") is True
        assert plugin._is_allowed("60006") is True
        assert plugin._is_allowed("99999") is False
        assert plugin._is_allowed("") is False  # fail closed
        assert plugin._is_allowed(None) is False


async def test_whitelist_blocks_commands_silently():
    async with World(
        config={"use_persona": False, "allowed_users": SENDER, "diary_op_interval": 0}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        blocked_help = await inst.run_handler("diary_help", _event("/日记帮助", sender_id=OTHER))
        blocked_memo = await inst.run_handler("memo_cmd", _event("/记一下 考试", sender_id=OTHER))
        allowed_help = await inst.run_handler("diary_help", _event("/日记帮助"))
        assert blocked_help == []  # 静默：不回复、不刷屏
        assert blocked_memo == []
        assert plugin._load_memos(OTHER) == []  # 名单外不落任何数据
        assert any("指令一览" in t for t in allowed_help)


async def test_whitelist_denied_command_hints_once_in_private():
    """附录 A 修复：白名单外用户在内置测试聊天发指令时，私聊回一次性配置提示。

    v0.5.5 的问题：白名单静默拦截，内置测试聊天的 sender_id 不是用户 QQ 号，
    发 /删除日记 完全没反应，分不清是不在白名单还是插件坏了。
    """
    async with World(
        config={"use_persona": False, "allowed_users": SENDER, "diary_op_interval": 0}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        # 名单外私聊 + 指令形态 → 一次提示（带 UID 与配置页指引）
        first = _event("/删除日记", sender_id=OTHER)
        await plugin.on_any_message(first)
        texts = first.result_texts()
        assert len(texts) == 1, texts
        assert OTHER in texts[0]
        assert "白名单" in texts[0]
        assert "允许使用插件的用户" in texts[0]
        # 同一身份再来一条指令：不再提示（一次性，防刷屏）
        second = _event("/查看日记", sender_id=OTHER)
        await plugin.on_any_message(second)
        assert second.result_texts() == []
        # 名单外普通闲聊：不提示（防打扰、不暴露插件）
        chat = _event("在吗", sender_id=OTHER)
        await plugin.on_any_message(chat)
        assert chat.result_texts() == []
        # 群聊里的名单外指令：保持静默（不暴露插件、不抢话）
        group = _event("/删除日记", sender_id=OTHER, group_id="88888")
        await plugin.on_any_message(group)
        assert group.result_texts() == []
        # 名单内用户：不提示（照常由指令 handler 回应，这里只看 on_any_message 无额外输出）
        allowed = _event("/日记帮助", sender_id=SENDER)
        await plugin.on_any_message(allowed)
        assert allowed.result_texts() == []
        # 取不到 UID（内置测试聊天可能如此）：也提示一次，引导查日志/临时清空白名单
        blank = _event("/删除日记", sender_id="")
        await plugin.on_any_message(blank)
        assert len(blank.result_texts()) == 1, blank.result_texts()
        assert "允许使用插件的用户" in blank.result_texts()[0]
        # terminate 清空一次性状态（重载插件后可重新提示）
        await plugin.terminate()
        assert plugin._whitelist_hint_sent == set()


async def test_whitelist_blocks_injection_and_sentinel():
    async with World(
        config={"use_persona": False, "allowed_users": SENDER}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            OTHER, DIARY_TEXT, sender_name="阿堵", origin="private", mode="full"
        )
        # 名单外用户：超大上下文也不注入、不触发哨兵
        req = _request(prompt="聊" * 99000)
        ev = _event(sender_id=OTHER)
        await plugin.on_llm_request(ev, req)
        assert _extra_texts(req) == []
        assert ev.result_texts() == []
        assert plugin._watch_state.get(UMO_OTHER) in (None, {})

        # 名单内用户：照常注入
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
        )
        req2 = _request()
        await plugin.on_llm_request(_event(), req2)
        assert len(_extra_texts(req2)) == 1
        assert "<memory_diary>" in _extra_texts(req2)[0]


async def test_whitelist_filters_auto_diary_registration_and_trigger():
    async with World(
        config={"use_persona": False, "allowed_users": SENDER}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        # on_any_message 登记：名单外不登记
        await plugin.on_any_message(_event("你好", sender_id=OTHER))
        await plugin.on_any_message(_event("你好"))
        assert OTHER not in plugin._auto_users
        assert SENDER in plugin._auto_users

        # 从会话库补齐：名单外不补
        inst.conversation_manager.set(UMO_OTHER, history=[message("user", "x")])
        plugin._auto_users.pop(OTHER, None)
        await plugin._seed_auto_users_from_conversations()
        assert OTHER not in plugin._auto_users

        # 真正触发前再过滤：已登记的名单外用户也不写
        plugin._auto_users[OTHER] = {"umo": UMO_OTHER, "name": "阿堵"}
        inst.set_history([message("user", "今天聊了考试"), message("assistant", "加油")])
        inst.provider.replies[:] = [DIARY_TEXT]
        await plugin._run_auto_diaries("20261023")
        assert inst.entries(SENDER) == ["diaries/20261023.md"]
        assert inst.entries(OTHER) == []
        assert all(not umo.endswith(OTHER) for umo, _ in inst.ctx.sent_messages)


# ---------------------------------------------------------------------------
# ⑤ 零宽字符加固
# ---------------------------------------------------------------------------


async def test_zero_width_chars_stripped_in_command_args():
    async with World(
        config={"use_persona": False, "diary_op_interval": 0}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "今天聊了考试"), message("assistant", "加油")])
        inst.provider.replies[:] = [DIARY_TEXT]
        # 参数尾巴带零宽字符：应正确识别为"完整"
        texts = await inst.run_handler("write_diary_cmd", _event("/日记 完整\u200b\u200c"))
        assert any("日记写好啦" in t for t in texts)
        assert inst.entries(SENDER) == ["diaries/20261023.md"]

        # memo 链路（不走 _command_arg）：指令 token 上的零宽字符仍剥离，
        # 但备忘内容按 P3-13 新语义「原文保留」——内容里的不可见字符不再被删。
        memo = await inst.run_handler("memo_cmd", _event("/记一下\u200b 下周三考试\ufeff"))
        assert any("已记下" in t for t in memo)
        memos = plugin._load_memos(SENDER)
        assert memos and memos[-1]["text"] == "下周三考试\ufeff"

        # 范围解析：尾巴的零宽字符不再导致"没看懂"
        scope = await inst.run_handler("diary_scope", _event("/日记范围 最近7天\u200b"))
        assert any("最近 7 天" in t for t in scope)


# ---------------------------------------------------------------------------
# 群聊多 AI 防冲突：bot_call_names 已删除，文本 @ 一律沉默
# ---------------------------------------------------------------------------


async def test_text_at_in_command_token_always_silences():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin.config["bot_call_names"] = "小深"  # 旧配置残留不再生效
        # 指令 token 内的文本 @（即便写的是"我的名字"）一律视为叫别人
        texts = await inst.run_handler(
            "diary_help", _event("/日记帮助@小深", group_id="88888", messages=[])
        )
        assert texts == []
        # QQ 原生 At 组件指向我：照常响应
        ok = await inst.run_handler(
            "diary_help",
            _event("/日记帮助", group_id="88888", messages=[at_me("99999")]),
        )
        assert any("指令一览" in t for t in ok)
        # At 组件指向别人：沉默
        other = await inst.run_handler(
            "diary_help",
            _event("/日记帮助", group_id="88888", messages=[at_other("12345")]),
        )
        assert other == []


# ---------------------------------------------------------------------------
# ③ Web 一键备份全部用户日记
# ---------------------------------------------------------------------------


async def _call_web(handler, request, *args):
    with bind_request_context(request):
        return await handler(*args)


async def test_web_export_all_backup_bundle():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        # 还没有任何日记：400
        empty = await _call_web(plugin.web_export_all, FakeWebRequest())
        assert getattr(empty, "status_code", None) == 400

        plugin._save_diary(SENDER, DIARY_TEXT, sender_name="阿罗",
                           origin="private", mode="full")
        plugin._save_diary(OTHER, DIARY_TEXT, sender_name="阿堵",
                           origin="private", mode="full")
        # P3-16：构造真实的临时残留命名 diary_<16hex>.zip.tmp-<pid>，否则断言
        # 「tmp 残留不入包」等于没验证（旧写法 paper_tmp_deadbeef 本来就不匹配
        # diary_*.zip）。
        stray = inst.data_dir / "diary_a1b2c3d4e5f60718.zip.tmp-4242"
        stray.write_text("x", encoding="utf-8")
        assert stray.exists()

        resp = await _call_web(plugin.web_export_all, FakeWebRequest())
        assert type(resp).__name__ == "FileResponse"
        with zipfile.ZipFile(resp.path) as z:
            names = set(z.namelist())
            readme = z.read("备份说明.txt").decode("utf-8")
        expected = {
            plugin._diary_path(SENDER).name,
            plugin._diary_path(OTHER).name,
            "备份说明.txt",
        }
        assert names == expected
        assert not any("key" in n.lower() for n in names)
        assert not any(".zip.tmp-" in n for n in names)
        assert stray.name not in names
        assert "用户份数：2 份" in readme
        assert ".diary_key" in readme  # 说明里指明主密钥要一起迁移

        # 路由已注册
        routes = [r[0] for r in inst.ctx.registered_web_apis]
        assert any(r.endswith("/export_all") for r in routes)


# ---------------------------------------------------------------------------
# ② token 估算器（纯函数）
# ---------------------------------------------------------------------------


def test_estimator_text_weights():
    assert est.estimate_text("") == 0
    # 4 个 CJK 字符 ≈ 3.2 → 3
    assert est.estimate_text("你好世界") == 3
    # 4 个 ASCII 字符 ≈ 1
    assert est.estimate_text("abcd") == 1
    # 中文估算明显高于旧公式的字符数÷1.8 以外的错误量级：同长度中文 > 英文
    assert est.estimate_text("汉" * 100) > est.estimate_text("a" * 100)
    # 混合文本落在合理区间（2×0.8 + 10×0.25 = 4.1 → 4）
    mixed = est.estimate_text("你好 world 123")
    assert 3 <= mixed <= 12


def test_estimator_messages_and_request_consistency():
    contexts = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": [{"type": "text", "text": "在的"}]},
    ]
    msgs = est.estimate_messages(contexts)
    # 两条消息各 +3 开销
    assert msgs == est.estimate_text("你好") + est.estimate_text("在的") + 6
    # 请求入口 = system + prompt + contexts（同输入必同输出）
    req = _request(prompt="今天怎么样", contexts=contexts, system_prompt="SYS")
    assert est.estimate_request(req) == (
        est.estimate_text("SYS") + est.estimate_text("今天怎么样") + msgs
    )
    # 裸消息列表入口带 system_prompt 时一致
    assert est.estimate_messages(contexts, system_prompt="SYS") == (
        msgs + est.estimate_text("SYS")
    )


def test_estimator_counts_images_tools_and_extras():
    base = _request(prompt="看图", contexts=[], system_prompt="")
    with_images = _request(prompt="看图", contexts=[], system_prompt="")
    with_images.image_urls = ["http://x/1.png", "http://x/2.png"]
    assert est.estimate_request(with_images) - est.estimate_request(base) == 2048

    inline = est.estimate_messages([
        {"role": "user", "content": [
            {"type": "text", "text": "这是什么"},
            {"type": "image_url", "image_url": {"url": "http://x/1.png"}},
        ]}
    ])
    assert inline == est.estimate_text("这是什么") + 3 + 1024

    class _Tool:
        def model_dump(self):
            return {"name": "search", "description": "搜索" * 50}

    class _ToolSet:
        tools = [_Tool()]

    with_tools = _request(prompt="用工具", contexts=[], system_prompt="")
    with_tools.func_tool = _ToolSet()
    base2 = _request(prompt="用工具", contexts=[], system_prompt="")
    assert est.estimate_request(with_tools) > est.estimate_request(base2)


async def test_sentinel_uses_v2_estimator_when_enabled():
    """v2 开启时哨兵按分类权重估算：中文为主的上下文不再被严重低估。"""
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            # P2-1 后「暂存校准估算 E」与哨兵解耦、只在 v2+calibration 开启时进行；
            # 本用例要断言暂存的 E，所以必须同时打开校准（哨兵本身照常工作）。
            "token_calibration_enabled": True,
            "context_token_limit": 60000,
            "remind_ratio": 0.75,
            "auto_ratio": 0.9,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "历史")])
        # 6 万 CJK 字符 ≈ 48000+ tokens ≈ 80% —— 旧公式只会估出 ~55%，不触发
        ev = _event()
        await plugin.on_llm_request(ev, _request(prompt="聊" * 60000))
        sent = ev.result_texts()
        assert len(sent) == 1
        assert "📝 小提醒" in sent[0]
        # 暂存了待校准的静态估算（键为 provider 类型名）
        pending = plugin._pending_estimates.get(UMO)
        assert pending is not None
        assert pending["key"] == "FakeProvider"
        assert pending["estimate"] >= 48000


async def test_legacy_fallback_when_v2_disabled():
    """token_estimation_v2 关闭：估算与 v0.5.4 旧公式逐点一致。"""
    async with World(
        config={"use_persona": False, "token_estimation_v2": False}
    ) as world:
        plugin = world.refactor.plugin
        req = _request(
            prompt="abc",
            system_prompt="defg",
            contexts=[
                {"role": "user", "content": "上下文一"},
                {"role": "assistant", "content": [{"type": "text", "text": "上下文二"}]},
            ],
        )
        total_chars = 3 + 4 + 4 + 4
        assert plugin._estimate_tokens(req) == int(total_chars / 1.8)


# ---------------------------------------------------------------------------
# ② EWMA 自适应校准
# ---------------------------------------------------------------------------


async def test_calibration_ewma_update_and_persistence():
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            "token_calibration_enabled": True,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        key = "FakeProvider"
        # factor = 1.0×0.8 + 2.0×0.2 = 1.2
        plugin._update_calibration(key, 1000, 2000)
        rec = plugin._calib_table()[key]
        assert abs(rec["factor"] - 1.2) < 1e-6
        assert rec["samples"] == 1
        assert rec["last_actual"] == 2000
        # 再来一次 2.0：1.2×0.8 + 2.0×0.2 = 1.36
        plugin._update_calibration(key, 1000, 2000)
        assert abs(plugin._calib_table()[key]["factor"] - 1.36) < 1e-6

        # 异常值跳过：比例 >10
        before = dict(plugin._calib_table()[key])
        plugin._update_calibration(key, 100, 5000)
        assert plugin._calib_table()[key] == before
        # 0 值跳过
        plugin._update_calibration(key, 0, 100)
        plugin._update_calibration(key, 100, 0)
        assert plugin._calib_table()[key] == before

        # 持久化往返一致
        plugin._token_calib = None
        loaded = plugin._load_token_calibration()
        assert loaded[key]["factor"] == before["factor"]
        assert loaded[key]["samples"] == before["samples"]
        # 文件里只有数字系数，没有任何聊天内容
        raw = json.loads(
            plugin._token_calibration_path().read_text(encoding="utf-8")
        )
        assert set(raw[key]) == {"factor", "samples", "last_actual", "updated"}


async def test_calibration_disabled_or_factor_clamped():
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            "token_calibration_enabled": False,
        }
    ) as world:
        plugin = world.refactor.plugin
        plugin._save_token_calibration(
            {"FakeProvider": {"factor": 99.0, "samples": 3, "last_actual": 1,
                              "updated": "2026-10-23 20:30:00"}}
        )
        # 校准关闭：系数恒 1.0，观测行为空
        assert plugin._calib_factor("FakeProvider") == 1.0
        assert plugin._calib_status_line("FakeProvider") == ""

    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            "token_calibration_enabled": True,
        }
    ) as world:
        plugin = world.refactor.plugin
        plugin._save_token_calibration(
            {"FakeProvider": {"factor": 99.0, "samples": 3, "last_actual": 1,
                              "updated": "2026-10-23 20:30:00"}}
        )
        # 越界系数读取时钳制到上限
        assert plugin._calib_factor("FakeProvider") == 5.0


async def test_calibration_flow_request_to_response():
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            "token_calibration_enabled": True,
            "context_token_limit": 60000,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "历史")])
        req = _request(prompt="你好")
        await plugin.on_llm_request(_event(), req)
        pending = plugin._pending_estimates.get(UMO)
        assert pending is not None
        E = pending["estimate"]
        assert E > 0

        # 无 usage 的响应：不更新、但消费掉暂存
        await plugin.on_llm_response(_event(), _Resp(None))
        assert plugin._calib_table() == {}
        assert UMO not in plugin._pending_estimates

        # 有 usage：actual = 2E → 系数 1.2
        await plugin.on_llm_request(_event(), _request(prompt="你好"))
        await plugin.on_llm_response(_event(), _Resp(_Usage(input_other=2 * E)))
        rec = plugin._calib_table()["FakeProvider"]
        assert abs(rec["factor"] - 1.2) < 1e-6
        assert rec["samples"] == 1

        # /日记状态：v2 格式行 + 校准观测行
        texts = await inst.run_handler("diary_status", _event("/日记状态"))
        assert any("估算" in t and "tokens" in t for t in texts)
        assert any("校准：上次实际" in t and "系数 1.20" in t for t in texts)


async def test_status_legacy_format_when_v2_disabled():
    async with World(
        config={"use_persona": False, "token_estimation_v2": False}
    ) as world:
        inst = world.refactor
        inst.set_history([message("user", "你好")])
        texts = await inst.run_handler("diary_status", _event("/日记状态"))
        assert any("估算值）" in t for t in texts)
        assert not any("校准：" in t for t in texts)


# ---------------------------------------------------------------------------
# v0.5.5 修订：状态页/门槛与哨兵同口径、校准 E 采样、旧配置兼容、密码零宽
# ---------------------------------------------------------------------------


async def test_status_and_write_threshold_share_sentinel_estimate():
    """v2 下 /日记状态、手动写日记门槛必须复用哨兵最近一次请求的估算。

    否则会出现"哨兵说该写了、状态页说没满"——方案 ② 的核心诉求。
    """
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
        # 先发一次大请求：哨兵算到约 80%（历史本身只有十几 token）
        await plugin.on_llm_request(_event(), _request(prompt="聊" * 1000))
        stored = plugin._last_context_used.get(UMO)
        assert stored is not None
        used, limit = stored["used"], stored["limit"]
        pct = int(used / limit * 100)
        assert pct >= 75

        # /日记状态：必须显示哨兵同一个数
        texts = await inst.run_handler("diary_status", _event("/日记状态"))
        joined = "\n".join(texts)
        assert f"估算 {used:,} / {limit:,} tokens（{pct}%）" in joined

        # 手动写日记门槛：按历史估算只有 0%，必须复用哨兵数字才会放行
        inst.provider.replies[:] = [DIARY_TEXT]
        out = await inst.run_handler("write_diary_cmd", _event("/日记 完整"))
        joined_out = "\n".join(out)
        assert "还没到该写日记的时候" not in joined_out
        assert "日记写好啦" in joined_out


async def test_calibration_estimate_recomputed_after_diary_injection():
    """校准样本 E 必须按注入后的完整请求重算，与真实 actual 同口径。"""
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            "token_calibration_enabled": True,
            "context_token_limit": 60000,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
        )
        req = _request(prompt="你好")
        await plugin.on_llm_request(_event(), req)
        pending = plugin._pending_estimates.get(UMO)
        assert pending is not None
        # 注入完成后按最终 req 重算过
        assert pending["estimate"] == est.estimate_request(req)
        assert pending["estimate"] > plugin._last_context_used[UMO]["used"]


async def test_password_command_strips_zero_width():
    """⑤ 同类故障：/日记密码 自己 split message_str，零宽字符也必须剥离。"""
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        texts = await inst.run_handler(
            "diary_password",
            _event("/日记密码\u200b 设置 abcd1234\ufeff"),
        )
        assert any("设置好啦" in t for t in texts)
        rec = plugin._view_password_rec(SENDER)
        assert rec is not None
        assert plugin._verify_view_password(rec, "abcd1234") is True


def test_schema_keeps_deprecated_context_watch_for_migration():
    """旧键必须留在 schema 里，否则 AstrBot 加载配置时会把它清洗掉。"""
    schema_path = os.path.join(
        os.path.dirname(_main_mod.__file__), "_conf_schema.json"
    )
    with open(schema_path, encoding="utf-8-sig") as f:
        schema = json.load(f)
    item = schema["enable_context_watch"]
    assert item["default"] is True
    assert "废弃" in item["description"]

    # 两项改进版 token 估算能力是「实验性功能」，默认关闭
    v2 = schema["token_estimation_v2"]
    assert v2["default"] is False
    assert "实验性" in v2["description"]
    cal = schema["token_calibration_enabled"]
    assert cal["default"] is False
    assert "实验性" in cal["description"]
    # 配置页联动：只有打开「改进版 token 估算」才显示自动校准开关
    assert cal.get("condition") == {"token_estimation_v2": True}
    # 配置页最后两项固定为「日记时区」→「已废弃的启用上下文哨兵」（废弃项最后）
    assert list(schema.keys())[-2:] == ["diary_timezone", "enable_context_watch"]

    # 完整日记是质量锚点：默认 1 篇/场景/天，配置说明必须明确不建议多设
    mf = schema["max_full_diaries_per_day"]
    assert mf["default"] == 1
    assert "质量锚点" in mf["hint"]
    assert "强烈建议" in mf["hint"]
    # 配置页纯文本提示：用 obvious_hint 让 AstrBot 渲染 ‼️ 强调
    assert mf.get("obvious_hint") is True


async def test_provider_key_uses_configured_provider_id():
    """校准 key 的 provider 段应取 provider_config["id"]，不是类名。"""
    from types import SimpleNamespace

    class _Provider:
        provider_config = {"id": "deepseek"}
        model_name = "deepseek-chat"

        def meta(self):
            return SimpleNamespace(id="meta-id", name="MetaName")

    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.ctx.provider = _Provider()
        assert await plugin._provider_key(UMO) == "deepseek::deepseek-chat"


# ---------------------------------------------------------------------------
# P2-1：校准采样与哨兵解耦（两档阈值都为 0 = 哨兵停用的合法配置）
# ---------------------------------------------------------------------------


async def test_calibration_staged_when_sentinel_disabled():
    """哨兵停用（remind_ratio=auto_ratio=0）时，校准样本仍要采到并落盘。

    修复前暂存 E 写在 `_watch_context` 里、被阈值提前 return 跳过，用户按文档
    把两档设 0 后 EWMA 永远不更新；修复后由 `on_llm_request` 统一暂存。
    """
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            "token_calibration_enabled": True,
            "context_token_limit": 60000,
            "remind_ratio": 0,
            "auto_ratio": 0,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "历史")])
        event = _event()
        await plugin.on_llm_request(event, _request(prompt="你好"))
        # 哨兵停用：既不提醒、也不自动写
        assert event.result_texts() == []
        pending = plugin._pending_estimates.get(UMO)
        assert pending is not None, "哨兵停用时校准估算也必须暂存（P2-1）"
        assert pending["key"] == "FakeProvider"
        E = pending["estimate"]
        assert E > 0

        # 真实 usage = E → 系数 1.0、样本 1
        await plugin.on_llm_response(_event(), _Resp(_Usage(input_other=E)))
        rec = plugin._calib_table()["FakeProvider"]
        assert rec["samples"] == 1
        assert abs(rec["factor"] - 1.0) < 1e-6
        # 落盘：token_calibration.json 有样本（重启后仍能读到）
        path = plugin._token_calibration_path()
        assert path.exists()
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["FakeProvider"]["samples"] == 1
        assert raw["FakeProvider"]["last_actual"] == E

        # /日记状态：哨兵停用不显示提醒/自动写触发状态，但校准观测行要在
        texts = await inst.run_handler("diary_status", _event("/日记状态"))
        joined = "\n".join(texts)
        assert "本会话哨兵：已关闭" in joined
        assert "已提醒" not in joined
        assert "已自动写日记" not in joined
        assert "校准：上次实际" in joined


# ---------------------------------------------------------------------------
# P3-8：/日记状态 的系数显示也要按读取口径钳制
# ---------------------------------------------------------------------------


async def test_calib_status_line_clamps_out_of_range_factor():
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            "token_calibration_enabled": True,
        }
    ) as world:
        plugin = world.refactor.plugin
        plugin._save_token_calibration(
            {
                "FakeProvider": {
                    "factor": 99.0,
                    "samples": 3,
                    "last_actual": 24000,
                    "updated": "2026-10-23 20:30:00",
                }
            }
        )
        line = plugin._calib_status_line("FakeProvider")
        # 实际生效系数被钳到 5.0，显示也必须一致（不能显示「系数 99.00」）
        assert "系数 5.00" in line
        assert "99.00" not in line
        assert "上次实际 24,000" in line
        assert plugin._calib_factor("FakeProvider") == 5.0


# ---------------------------------------------------------------------------
# P3-12：白名单里的零宽 / 全角 QQ 号
# ---------------------------------------------------------------------------


async def test_whitelist_tolerates_invisible_and_fullwidth_ids():
    """管理员粘贴带零宽/全角字符的 QQ 号时不能把自己挡在门外。"""
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        plugin.config["allowed_users"] = "100\u200b01，\uff12\uff10\uff10\uff10\uff12\ufeff、30003"
        assert plugin._is_allowed("10001") is True  # 零宽已剥离
        assert plugin._is_allowed("20002") is True  # 全角数字 NFKC 归一
        assert plugin._is_allowed("30003") is True
        assert plugin._is_allowed("99999") is False
        assert plugin._is_allowed("") is False  # fail closed


# ---------------------------------------------------------------------------
# P3-13：memo 内容保留原文（ZWJ emoji / ZWNJ 不被破坏）
# ---------------------------------------------------------------------------


async def test_memo_content_preserves_zwj_emoji():
    """只剥「指令 token」用于解析，备忘内容必须原文保存。

    对整条消息做零宽剥离会把 👨‍👩‍👧 的 ZWJ 删掉、变成 👨👩👧（三个独立
    emoji），也会破坏阿拉伯/波斯文正字法必需的 ZWNJ。
    """
    async with World(
        config={"use_persona": False, "diary_op_interval": 0}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"  # 👨‍👩‍👧
        note = f"家庭 {family}"
        texts = await inst.run_handler("memo_cmd", _event(f"/记一下 {note}"))
        assert any("已记下" in t for t in texts)
        memos = plugin._load_memos(SENDER)
        assert memos[-1]["text"] == note
        assert "\u200d" in memos[-1]["text"], "ZWJ 序列不能被剥离"
        assert memos[-1]["text"] == "家庭 \U0001F468\u200d\U0001F469\u200d\U0001F467"

        # 指令 token 上的零宽仍要剥离（否则指令本身都认不出）
        memo2 = await inst.run_handler(
            "memo_cmd", _event("/记一下\u200b 下周三考试")
        )
        assert any("已记下" in t for t in memo2)
        assert plugin._load_memos(SENDER)[-1]["text"] == "下周三考试"


# ---------------------------------------------------------------------------
# P3-14：waiter / reset 判断前的零宽剥离
# ---------------------------------------------------------------------------


async def test_zero_width_cancel_in_waiter_is_recognized():
    """waiter 收到带零宽的「取消」时必须走取消分支，而不是「没看懂」。"""
    async with World(
        config={"use_persona": False, "diary_op_interval": 0}
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        await inst.run_handler("memo_cmd", _event("/记一下 下周三考试"))
        inst.set_replies([{"text": "取消\u200b"}])
        texts = await inst.run_handler("memo_delete", _event("/删除备忘"))
        joined = "\n".join(texts)
        assert "已取消" in joined
        assert "没看懂" not in joined, "零宽导致取消口令没被识别"
        assert len(plugin._load_memos(SENDER)) == 1  # 备忘原样保留


async def test_zero_width_reset_is_detected():
    """/reset 双保险的代际识别也要先剥零宽，否则新上下文不重新注入日记。"""
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        await plugin.on_any_message(_event("/reset\u200b"))
        assert UMO in plugin._reset_pending


# ---------------------------------------------------------------------------
# P3-19：④ 注入模板新增句的 refactor-only 断言
#     （这句子在差分里被 world.normalise_text 整句删掉，丢了不会报警）
# ---------------------------------------------------------------------------


def test_inject_template_states_persona_headings():
    from harness import world as world_module

    const = importlib.import_module(
        _main_mod.__name__.rsplit(".", 1)[0] + ".memory_diary.constants"
    )
    template = const.INJECT_TEMPLATE
    assert "小标题" in template and "人设" in template
    # 与差分豁免删除的句子完全一致：句子一旦被改/删，差分仍然看不出，
    # 这条断言就是那条文案的唯一告警点。
    assert world_module.World._INJECT_TEMPLATE_V055_SENTENCE in template


# ---------------------------------------------------------------------------
# P3-11：on_llm_request 的白名单判定异常必须 fail-closed
# ---------------------------------------------------------------------------


async def test_llm_request_fail_closed_when_whitelist_check_raises():
    """白名单判定抛异常时按拒绝处理：不注入、不跑哨兵、不暂存校准样本。

    修复前这里是「按放行处理」（fail-open），与 _is_allowed 的 fail-closed
    原则相反——身份不明时反而把私密日记塞进请求。
    """
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            "token_calibration_enabled": True,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
        )
        inst.set_history(
            [message("user", "今天聊了考试"), message("assistant", "加油")]
        )

        def _boom(_sender_id):
            raise RuntimeError("boom")

        plugin._is_allowed = _boom  # 模拟白名单解析/判定异常
        event = _event()
        req = _request(prompt="你好")
        await plugin.on_llm_request(event, req)  # 不得把异常抛给 AstrBot

        assert _extra_texts(req) == []  # 本轮不注入
        assert "<memory_diary>" not in (req.system_prompt or "")
        assert UMO not in plugin._last_context_used  # 不跑哨兵
        assert UMO not in plugin._pending_estimates  # 不暂存校准样本
        assert event.result_texts() == []  # 不提醒、不刷屏


# ---------------------------------------------------------------------------
# P3-7：启动时清理残留的临时导出/备份包
# ---------------------------------------------------------------------------


async def test_initialize_purges_stale_export_and_backup_bundles():
    """上次进程崩溃留下的临时包必须在启动时清掉，真日记 zip 不能动。"""
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        export_stale = inst.data_dir / "memory_diary_export_20260101_000000_abcd.zip"
        backup_stale = inst.data_dir / "memory_diary_backup_20260101_000000_abcd.zip"
        diary = inst.data_dir / "diary_0123456789abcdef.zip"
        for path in (export_stale, backup_stale, diary):
            path.write_bytes(b"stale")

        await inst.initialize()

        assert not export_stale.exists(), "残留的导出迁移包应被启动清理删除"
        assert not backup_stale.exists(), "残留的一键备份包应被启动清理删除"
        assert diary.exists(), "真正的日记 zip 不能被启动清理误删"


# ---------------------------------------------------------------------------
# 复查补丁：context_token_limit 填 0/负数 = 停用比例体系
# （原实现用 `... or 60000` 读取，0 会被静默当成默认 60000，0 停用不生效）
# ---------------------------------------------------------------------------


async def test_context_limit_helper_zero_negative_and_fallbacks():
    async with World(
        config={"use_persona": False, "token_estimation_v2": True}
    ) as world:
        plugin = world.refactor.plugin
        # 合法值原样返回
        plugin.config["context_token_limit"] = 60000
        assert plugin._context_limit() == 60000
        # 0 / 负数表示停用，必须原样返回（不能被 `or 60000` 吞掉）
        plugin.config["context_token_limit"] = 0
        assert plugin._context_limit() == 0
        plugin.config["context_token_limit"] = -1
        assert plugin._context_limit() == -1
        # 缺省/非法值回退默认 60000
        plugin.config["context_token_limit"] = None
        assert plugin._context_limit() == 60000
        plugin.config["context_token_limit"] = "not-a-number"
        assert plugin._context_limit() == 60000


async def test_zero_context_limit_disables_sentinel_status_and_manual_gate():
    """窗口 0 = 哨兵/比例门槛停用：不提醒、不自动写、状态页标注、手动门槛跳过。"""
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            "context_token_limit": 0,
            "remind_ratio": 0.75,
            "auto_ratio": 0.9,
            "write_min_ratio": 0.8,
            "diary_op_interval": 0,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history(
            [message("user", "今天聊了考试"), message("assistant", "加油")]
        )
        inst.provider.replies[:] = [DIARY_TEXT]

        # 超大请求：旧实现会按 60000 分母算出 100%+ 并触发提醒/自动写
        event = _event()
        await plugin.on_llm_request(event, _request(prompt="聊" * 99000))
        assert event.result_texts() == []  # 不提醒、不自动写
        assert UMO not in plugin._last_context_used  # 不记录哨兵口径
        assert inst.entries(SENDER) == []  # 没有自动写日记

        # 状态页：明确标注停用，不显示会误导的 0% / 0 tokens
        texts = await inst.run_handler("diary_status", _event("/日记状态"))
        joined = "\n".join(texts)
        assert "哨兵与比例门槛已停用" in joined
        assert "本会话哨兵：已关闭" in joined
        assert "估算 0 / 0 tokens" not in joined

        # 手动写日记的比例门槛同样跳过（write_min_ratio=0.8 但窗口 <= 0）
        out = await inst.run_handler("write_diary_cmd", _event("/日记 完整"))
        joined_out = "\n".join(out)
        assert "还没到该写日记的时候" not in joined_out
        assert "日记写好啦" in joined_out
        assert inst.entries(SENDER) == ["diaries/20261023.md"]


# ---------------------------------------------------------------------------
# 复查补丁：群聊里 /日记状态 不展示个人日记统计与实例级校准观测
# ---------------------------------------------------------------------------


async def test_diary_status_hides_personal_stats_and_calibration_in_group():
    """群聊同群可见：日记篇数/最新日期、provider::model/样本数/上次实际都不展示。

    私聊仍完整展示；群聊保留上下文占用与哨兵等"会话本身"的通用状态。
    """
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": True,
            "token_calibration_enabled": True,
            "diary_op_interval": 0,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
        )
        plugin._save_token_calibration(
            {
                "FakeProvider": {
                    "factor": 1.2,
                    "samples": 7,
                    "last_actual": 24000,
                    "updated": "2026-10-23 20:30:00",
                }
            }
        )

        # 私聊：日记统计与校准观测都正常展示
        private = "\n".join(
            await inst.run_handler("diary_status", _event("/日记状态"))
        )
        assert "共 1 天的日记" in private
        assert "校准：上次实际" in private

        # 群聊：个人日记统计与实例级校准观测都不出现
        group = "\n".join(
            await inst.run_handler(
                "diary_status", _event("/日记状态", group_id="88888")
            )
        )
        assert "群聊不展示个人日记统计" in group
        assert "共 1 天的日记" not in group
        assert "校准：上次实际" not in group
        assert "当前上下文" in group  # 上下文占用是群会话信息，照常展示


# ---------------------------------------------------------------------------
# 复查补丁：配置页（_conf_schema.json）顺序必须与 README §8 的介绍顺序一致
# ---------------------------------------------------------------------------

_CONF_README_ORDER = [
    # 10.1 上下文哨兵
    "context_token_limit", "remind_ratio", "auto_ratio",
    "token_estimation_v2", "token_calibration_enabled",
    # 10.5 新会话读日记（注入）
    "enable_auto_inject", "inject_default_days", "inject_max_chars",
    "group_inject_filter", "persist_inject",
    # 10.6 写作
    "enable_auto_diary", "auto_diary_time", "empty_diary_as_life",
    "max_full_diaries_per_day", "max_history_messages",
    "group_history_multiplier", "write_min_ratio", "extra_instruction",
    # 10.7 隐私与安全
    "allowed_users", "show_diary_after_write",
    # 10.8 反馈风格
    "use_persona",
    # 10.9 群聊行为
    "group_require_at",
    # 10.10 限额与防护
    "diary_op_interval",
    # 10.11 界面
    "web_diary_brand",
    # 10.12 插图配置与形象档案（v0.6，顺序与 README bullet 一致）
    "image_enabled", "image_mode", "image_local_url", "image_api_base",
    "image_api_key", "image_model", "image_size_preset",
    "image_prob_memo", "image_prob_full",
    "image_limit_memo", "image_limit_full", "image_limit_daily",
    "image_limit_group", "image_concurrency", "image_timeout",
    "persona_image_desc", "image_style_prompt",
    "user_image_enabled", "user_image_limit", "user_ref_photo",
    "vision_api_base", "vision_api_key", "vision_api_model",
    # 10.13 批注配置（v0.6，顺序与 README bullet 一致）
    "annotation_enabled", "annotation_max_per_round",
    "annotation_question_hint", "annotation_timeout",
    # 10.16 时区与兼容项（配置页最后，废弃项是最后一个）
    "diary_timezone", "enable_context_watch",
]

_CONF_LABEL_PREFIXES = [
    ("模型上下文窗口", "context_token_limit"),
    ("提醒阈值", "remind_ratio"),
    ("自动写日记阈值", "auto_ratio"),
    ("改进版 token 估算", "token_estimation_v2"),
    ("token 估算自动校准", "token_calibration_enabled"),
    ("新会话自动读取日记", "enable_auto_inject"),
    ("默认注入最近几天的日记", "inject_default_days"),
    ("注入日记的最大字符数", "inject_max_chars"),
    ("群聊注入过滤", "group_inject_filter"),
    ("注入持久化", "persist_inject"),
    ("每天定时自动写完整日记", "enable_auto_diary"),
    ("自动写日记时间", "auto_diary_time"),
    ("无新素材时写拟人日常", "empty_diary_as_life"),
    ("每个场景每天最多几篇完整日记", "max_full_diaries_per_day"),
    ("写日记时读取的最大消息条数", "max_history_messages"),
    ("群聊素材倍率", "group_history_multiplier"),
    ("手动写日记的最低上下文占用", "write_min_ratio"),
    ("写日记的附加要求", "extra_instruction"),
    ("允许使用插件的用户", "allowed_users"),
    ("写完后展示日记全文", "show_diary_after_write"),
    ("人设口吻", "use_persona"),
    ("群里只响应@我的指令", "group_require_at"),
    ("日记操作间隔", "diary_op_interval"),
    ("（已废弃）启用上下文哨兵", "enable_context_watch"),
    ("日记本页面的称呼", "web_diary_brand"),
    ("日记时区", "diary_timezone"),
    # 10.12 插图配置与形象档案（v0.6）
    ("插图功能总开关", "image_enabled"),
    ("生图通道", "image_mode"),
    ("本地生图服务地址", "image_local_url"),
    ("云端生图 API 地址", "image_api_base"),
    ("云端生图 API Key", "image_api_key"),
    ("云端生图模型", "image_model"),
    ("插图尺寸档位", "image_size_preset"),
    ("简记插图触发概率", "image_prob_memo"),
    ("完整日记插图触发概率", "image_prob_full"),
    ("单篇简记最多几张插图", "image_limit_memo"),
    ("单篇完整日记最多几张插图", "image_limit_full"),
    ("每日插图总量上限", "image_limit_daily"),
    ("群聊单篇最多几张插图", "image_limit_group"),
    ("同时生成插图的最大数量", "image_concurrency"),
    ("单张插图生成超时", "image_timeout"),
    ("AI 形象描述", "persona_image_desc"),
    ("画风锚", "image_style_prompt"),
    ("把用户发来的照片收进日记", "user_image_enabled"),
    ("单篇日记最多收几张用户照片", "user_image_limit"),
    ("参考照片路径", "user_ref_photo"),
    ("可选视觉接口 API 地址", "vision_api_base"),
    ("可选视觉接口 API Key", "vision_api_key"),
    ("可选视觉接口模型", "vision_api_model"),
    # 10.13 批注配置（v0.6）
    ("批注功能总开关", "annotation_enabled"),
    ("每轮最多生成几条批注", "annotation_max_per_round"),
    ("疑问句软引导", "annotation_question_hint"),
    ("批注生成超时", "annotation_timeout"),
]


def test_conf_schema_order_matches_readme_sections():
    """配置页按 schema 键顺序渲染，必须与 README §10 的介绍顺序逐项一致。

    任何一侧单独重排（schema 或 README）都会让本测试失败——这正是「配置页顺序
    与 README 介绍一致」的回归闸门。
    """
    import re

    root = os.path.dirname(_main_mod.__file__)

    schema_path = os.path.join(root, "_conf_schema.json")
    with open(schema_path, encoding="utf-8-sig") as f:
        schema = json.load(f)
    assert list(schema.keys()) == _CONF_README_ORDER

    readme_path = os.path.join(root, "README.md")
    with open(readme_path, encoding="utf-8") as f:
        readme = f.read()
    start = readme.index("## 10 配置项与多用户隐私")
    end = readme.index("## 11 标准使用流程")
    section = readme[start:end]

    found = []
    current = ""
    for line in section.splitlines():
        hm = re.match(r"^### (10\.\d+) ", line)
        if hm:
            current = hm.group(1)
            continue
        # 10.2~10.4 是判读/排障说明，10.14/10.15 是隐私与人设说明，都没有对应配置项
        if current not in {
            "10.1", "10.5", "10.6", "10.7", "10.8", "10.9", "10.10", "10.11",
            "10.12", "10.13", "10.16",
        }:
            continue
        # 允许缩进一层：如「token 估算自动校准」作为「改进版 token 估算」的子项，
        # 在 README 里就是嵌套 bullet；其余未映射的说明性 bullet 统一跳过。
        bm = re.match(r"^[ \t]*- \*\*(.+?)\*\*", line)
        if not bm:
            continue
        label = bm.group(1)
        for prefix, key in _CONF_LABEL_PREFIXES:
            if label.startswith(prefix):
                found.append(key)
                break
        else:
            # 10.1 里说明两个实验性开关关系的 bullet、10.12.x 里的
            # "填了/没填的区别"/排障 Q&A 等，是说明性文字而不是配置项本身
            continue

    assert found == _CONF_README_ORDER, found


# ---------------------------------------------------------------------------
# 复查补丁：/日记 完整 成功后提示今日剩余完整篇名额与重置时间
# ---------------------------------------------------------------------------


async def test_full_diary_quota_hint_after_write():
    async with World(
        config={
            "use_persona": False,
            "max_full_diaries_per_day": 2,
            "diary_op_interval": 0,
            "empty_diary_as_life": False,
        }
    ) as world:
        inst = world.refactor
        inst.set_history(
            [message("user", "今天聊了考试"), message("assistant", "加油")]
        )
        inst.provider.replies[:] = [DIARY_TEXT]

        out1 = "\n".join(
            await inst.run_handler("write_diary_cmd", _event("/日记 完整"))
        )
        assert "日记写好啦" in out1
        assert "已写 1/2 篇" in out1
        assert "还剩 1 篇" in out1
        assert "次日 00:00 重置" in out1

        # 补充新素材后写第二篇完整日记：名额用尽，提示重置
        inst.set_history(
            [
                message("user", "今天聊了考试"),
                message("assistant", "加油"),
                message("user", "还聊了志愿"),
                message("assistant", "嗯嗯"),
            ]
        )
        out2 = "\n".join(
            await inst.run_handler("write_diary_cmd", _event("/日记 完整"))
        )
        assert "已写 2/2 篇" in out2
        assert "名额已用完" in out2
        assert "次日 00:00 重置" in out2
