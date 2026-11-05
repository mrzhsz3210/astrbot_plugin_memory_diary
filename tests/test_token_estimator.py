# -*- coding: utf-8 -*-
"""token 估算器（Phase 1 静态估算）纯函数回归。

覆盖报告 P2-2 第 1 点（估算器边界：代码 / JSON / 大量消息 / audio_urls /
extra_user_content_parts 内嵌图片 / tool_calls_result）、第 2 点
（``v2=false + calibration=true`` 组合不产生校准样本），以及 P3-9
（extra parts 里内嵌图片按 1024/张计入）。

``memory_diary.token_estimator`` 是纯函数模块（不 import AstrBot），所以
大部分用例直接 import refactor 侧的模块、不需要 World；只有校准组合那条
需要驱动插件实例（World 提供固定时钟与固定签名 key，保证确定性）。
"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

from harness import env

# 直接加载 refactor 侧的纯函数模块：不经过插件 main.py，不依赖 AstrBot 装饰器。
if str(env.REFACTOR_DIR) not in sys.path:
    sys.path.insert(0, str(env.REFACTOR_DIR))
est = importlib.import_module("memory_diary.token_estimator")

from harness.fakes import make_event  # noqa: E402
from harness.world import World, message  # noqa: E402

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"

IMAGE_TOKENS = 1024
AUDIO_TOKENS = 200
MSG_OVERHEAD = 3


class _Req(SimpleNamespace):
    """duck-typed ProviderRequest：estimate_request 用 getattr 取属性。"""


def _estimate_extra_parts(parts, *, prompt: str = "", system_prompt: str = "") -> int:
    req = _Req(
        system_prompt=system_prompt,
        prompt=prompt,
        contexts=[],
        extra_user_content_parts=parts,
    )
    return est.estimate_request(req)


# ---------------------------------------------------------------------------
# 字符权重与边界
# ---------------------------------------------------------------------------


def test_estimate_text_empty_and_none():
    assert est.estimate_text("") == 0
    assert est.estimate_text(None) == 0


def test_estimate_text_weight_boundaries():
    # 权重常量是估算口径的一部分，先钉死（改权重必须同步改测试与文档）
    assert est.CJK_WEIGHT == 0.8
    assert est.ASCII_WEIGHT == 0.25
    assert est.OTHER_WEIGHT == 0.33
    # int() 截断：单个字符权重小于 1 时估算为 0，是既有约定（大样本才收敛）
    assert est.estimate_text("你") == 0  # 0.8 → 0
    assert est.estimate_text("你好") == 1  # 1.6 → 1
    assert est.estimate_text("a") == 0  # 0.25 → 0
    assert est.estimate_text("aaaa") == 1  # 1.0 → 1
    assert est.estimate_text("😀") == 0  # 0.33 → 0
    assert est.estimate_text("😀😀😀😀") == 1  # 1.32 → 1
    # 4 CJK = 3.2 → 3；11 ASCII = 2.75 → 2
    assert est.estimate_text("你好世界") == 3
    assert est.estimate_text("hello world") == 2
    # 混排：2×0.8 + 3×0.25 + 1×0.33 = 2.68 → 2
    assert est.estimate_text("中文abc😀") == 2


def test_estimate_text_code_and_json_samples():
    # 代码：26 个 ASCII 字符 = 6.5 → 6
    assert est.estimate_text("def f(x):\n    return x + 1") == 6
    # JSON：19 个 ASCII 字符 = 4.75 → 4
    assert est.estimate_text('{"a":1,"b":[1,2,3]}') == 4
    # 同样的字符数，CJK 权重是 ASCII 的 3.2 倍：中文必须明显更贵
    assert est.estimate_text("汉" * 100) == 80  # 80.0 → 80
    assert est.estimate_text("a" * 100) == 25  # 25.0 → 25
    # 代码/JSON 的 token 密度低于中文，但仍是正数（不能低估成 0）
    assert est.estimate_text("x = 1 + 2") > 0


# ---------------------------------------------------------------------------
# 大量消息：每条固定 +3 开销
# ---------------------------------------------------------------------------


def test_estimate_messages_adds_overhead_per_message():
    assert est.MSG_OVERHEAD_TOKENS == MSG_OVERHEAD == 3
    assert est.estimate_messages([]) == 0
    # "hi" 估算 0 token，只剩消息包装开销
    one = [{"role": "user", "content": "hi"}]
    assert est.estimate_messages(one) == MSG_OVERHEAD

    # 大量消息：50 条各 +3 = 150（不能漏算 role 包装）
    many = [{"role": "user", "content": "hi"} for _ in range(50)]
    assert est.estimate_messages(many) == 50 * MSG_OVERHEAD == 150

    # 内容也计入：两条 "中文中文"（3.2 → 3）+ 两条开销
    two = [{"role": "user", "content": "中文中文"} for _ in range(2)]
    assert est.estimate_messages(two) == 2 * est.estimate_text("中文中文") + 2 * 3


def test_estimate_messages_handles_multimodal_and_non_dict():
    # 多模态 content 列表里多个 text part 会拼接成一个字符串再估算
    # （内容里的图片由 estimate_messages 的图片计数负责，单独有图片用例）
    multimodal = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "前半"},
                {"type": "text", "text": "后半"},
            ],
        }
    ]
    assert est.estimate_messages(multimodal) == (
        est.estimate_text("前半后半") + MSG_OVERHEAD
    )
    # 非 dict 消息按 str 估文本，同样有每条开销
    assert est.estimate_messages(["你好世界"]) == (
        est.estimate_text("你好世界") + MSG_OVERHEAD
    )
    # system_prompt 只算一次、不加消息开销
    assert est.estimate_messages([], system_prompt="你好世界") == est.estimate_text(
        "你好世界"
    )


# ---------------------------------------------------------------------------
# 请求级组件：audio_urls / extra_user_content_parts / tool_calls_result
# ---------------------------------------------------------------------------


def test_estimate_request_empty_and_audio_urls():
    assert est.estimate_request(_Req()) == 0
    assert est.AUDIO_TOKEN_ESTIMATE == AUDIO_TOKENS == 200
    req = _Req(audio_urls=["http://x/1.amr", "http://x/2.amr", "http://x/3.amr"])
    assert est.estimate_request(req) == 3 * AUDIO_TOKENS == 600
    # 空 audio_urls 不产生开销
    assert est.estimate_request(_Req(audio_urls=[])) == 0


def test_estimate_request_extra_parts_images_dict_and_object():
    """P3-9：extra_user_content_parts 的内嵌图片必须算 1024/张（兼容 dict/对象）。"""
    assert est.IMAGE_TOKEN_ESTIMATE == IMAGE_TOKENS == 1024

    # 纯文本 part：只算文本，不算图片
    text_only = [{"type": "text", "text": "注入文本"}]
    assert _estimate_extra_parts(text_only) == est.estimate_text("注入文本")

    # dict part：text 键计入文本，多模态 content 列表里 1 张图 → +1024
    dict_part_image = [
        {
            "type": "user",
            "text": "看图",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "http://x/1.png"}},
            ],
        }
    ]
    assert _estimate_extra_parts(dict_part_image) == (
        est.estimate_text("看图") + IMAGE_TOKENS
    )

    # 对象 part（ImagePart 形状）：content 列表 2 张图 → +2048
    object_part_image = [
        SimpleNamespace(
            type="user",
            content=[
                {"type": "image_url", "image_url": {"url": "http://x/1.png"}},
                {"type": "image_url", "image_url": {"url": "http://x/2.png"}},
            ],
        )
    ]
    assert _estimate_extra_parts(object_part_image) == 2 * IMAGE_TOKENS == 2048

    # 对象 part 自身 type=image_url（无 content 列表）也算 1 张
    object_part_typed = [SimpleNamespace(type="image_url", text="")]
    assert _estimate_extra_parts(object_part_typed) == IMAGE_TOKENS

    # 文本注入 part（TextPart 形状，没有 content 列表）不会被误算成图片
    injected_text = [SimpleNamespace(text="<memory_diary>日记</memory_diary>")]
    assert _estimate_extra_parts(injected_text) == est.estimate_text(
        "<memory_diary>日记</memory_diary>"
    )

    # 文本 + 图片一起出现时两者相加（不能互相吞掉）
    mixed = [
        {"type": "text", "text": "说明"},
        {"type": "image_url", "image_url": {"url": "http://x/1.png"}},
    ]
    assert _estimate_extra_parts(mixed) == est.estimate_text("说明") + IMAGE_TOKENS


def test_estimate_request_counts_tool_calls_result_as_text():
    tool_calls_result = [
        {"name": "search", "arguments": {"q": "阿罗"}},
        {"name": "read", "result": "ok"},
    ]
    expected = est.estimate_text(str(tool_calls_result))
    assert expected > 0
    assert est.estimate_request(_Req(tool_calls_result=tool_calls_result)) == expected
    # 空列表是 falsy，不产生估算
    assert est.estimate_request(_Req(tool_calls_result=[])) == 0


def test_estimate_request_is_additive_across_components():
    """各组件严格可加：任何一项漏算都会让合计偏离。"""
    contexts = [{"role": "user", "content": "历史"}]
    extra = [{"type": "image_url", "image_url": {"url": "http://x/1.png"}}]
    tool_calls_result = [{"name": "t", "result": "r"}]
    req = _Req(
        system_prompt="SYS",
        prompt="你好",
        contexts=contexts,
        audio_urls=["a"],
        extra_user_content_parts=extra,
        tool_calls_result=tool_calls_result,
    )
    expected = (
        est.estimate_text("SYS")
        + est.estimate_text("你好")
        + est.estimate_messages(contexts)
        + AUDIO_TOKENS
        + IMAGE_TOKENS
        + est.estimate_text(str(tool_calls_result))
    )
    assert est.estimate_request(req) == expected


# ---------------------------------------------------------------------------
# P2-2 第 2 点：v2=false + calibration=true 不得产生校准样本
# ---------------------------------------------------------------------------


async def test_calibration_off_when_v2_disabled():
    """v2 关闭时即使 calibration_enabled=true 也不暂存、不落盘、系数恒 1.0。

    否则旧公式（字符数÷1.8）会被拿去和真实 usage 做 EWMA，把校准系数带偏。
    """
    async with World(
        config={
            "use_persona": False,
            "token_estimation_v2": False,
            "token_calibration_enabled": True,
        }
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        inst.set_history([message("user", "历史")])
        event = make_event("你好", sender_id=SENDER, sender_name="阿罗", umo=UMO)
        req = _Req(
            prompt="你好",
            system_prompt="SYS",
            contexts=[{"role": "user", "content": "历史"}],
            extra_user_content_parts=[],
        )
        await plugin.on_llm_request(event, req)
        # v2 关闭：on_llm_request 不暂存校准样本
        assert plugin._pending_estimates == {}

        # 即便 on_llm_response 拿到真实 usage，也不得写 token_calibration.json
        await plugin.on_llm_response(
            event,
            SimpleNamespace(
                usage=SimpleNamespace(input_other=1234, input_cached=0)
            ),
        )
        assert plugin._calib_table() == {}
        assert plugin._calib_factor("FakeProvider") == 1.0
        assert not plugin._token_calibration_path().exists()
