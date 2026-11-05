# -*- coding: utf-8 -*-
"""v0.6.2 审查修复（imaging 侧）回归测试。

覆盖报告条目：
- P3-15 每日额度日期接口（契约 2）：_image_usage_consume() 返回记账日期、
  _image_usage_refund(day=...) 跨午夜不扣新一天的额度；
- P2-6 _looks_like_image 严格魔数（非图片字节不再被当插图）；
- P2-9 HTTP 响应大小上限 / 关闭重定向 / 下载私网过滤。

全部离线：HTTP 通过注入点或假 httpx 替身驱动，不联网。
"""

from __future__ import annotations

import sys
import types
from datetime import datetime

from harness.world import World
from image_testkit import GIF, JPG, PNG, WEBP, TempDir, b64, img_config

DAY1 = "20261023"  # World 固定时钟 2026-10-23 20:30
DAY2 = "20261024"


def _plugin(world):
    return world.refactor.plugin


# ---------------------------------------------------------------------------
# P3-15 契约 2：额度日期接口
# ---------------------------------------------------------------------------


async def test_image_usage_consume_returns_accounting_day_or_empty():
    async with World(config=img_config(image_limit_daily=2)) as world:
        plugin = _plugin(world)

        assert plugin._image_usage_consume() == DAY1, (
            "consume 成功时应返回实际记账日期 YYYYMMDD"
        )
        assert plugin._image_usage_consume() == DAY1
        assert plugin._image_usage_consume() == "", "额度不足时应返回空串"
        # 既有 if not 写法保持兼容（"" 为假值）
        assert not plugin._image_usage_consume()


async def test_image_usage_refund_with_matching_day_decrements():
    async with World(config=img_config(image_limit_daily=6)) as world:
        plugin = _plugin(world)
        day = plugin._image_usage_consume()
        assert day == DAY1 and plugin._image_usage_load()["count"] == 1

        plugin._image_usage_refund(day=day)
        rec = plugin._image_usage_load()
        assert rec["date"] == DAY1 and rec["count"] == 0, "同日退款应扣回今天"


async def test_image_usage_refund_across_midnight_does_not_touch_new_day():
    """跨午夜失败：占用发生在昨天，退款不得扣掉今天的计数。"""
    async with World(config=img_config(image_limit_daily=6)) as world:
        plugin = _plugin(world)
        day1 = plugin._image_usage_consume()
        assert day1 == DAY1

        world.refactor.set_now(datetime(2026, 10, 24, 0, 5, 0))
        day2 = plugin._image_usage_consume()
        assert day2 == DAY2
        assert plugin._image_usage_load()["count"] == 1

        plugin._image_usage_refund(day=day1)  # 昨天的额度，昨天已过
        rec = plugin._image_usage_load()
        assert rec["date"] == DAY2, "退款不应改写记账日期"
        assert rec["count"] == 1, "跨午夜退款不得扣减今天的计数"

        # 今天的额度照常可退
        plugin._image_usage_refund(day=day2)
        assert plugin._image_usage_load()["count"] == 0

        # day 为空保持旧行为：退当前记账文件那一天
        assert plugin._image_usage_consume() == DAY2
        plugin._image_usage_refund()
        assert plugin._image_usage_load()["count"] == 0


async def test_image_usage_refund_empty_day_keeps_old_behavior_at_midnight():
    async with World(config=img_config(image_limit_daily=6)) as world:
        plugin = _plugin(world)
        assert plugin._image_usage_consume() == DAY1
        world.refactor.set_now(datetime(2026, 10, 24, 0, 5, 0))
        # 旧调用点（无参）不知道占用日，按现行语义退今天（今天为 0，不退成负数）
        plugin._image_usage_refund()
        rec = plugin._image_usage_load()
        assert rec["date"] == DAY2 and rec["count"] == 0


# ---------------------------------------------------------------------------
# P2-6 严格魔数：非图片字节不再被当插图
# ---------------------------------------------------------------------------

HTML_PAGE = (
    b"<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head>"
    b"<body><h1>502 Bad Gateway</h1><p>upstream unreachable</p></body></html>"
)
JSON_ERROR = (
    b'{"error": {"message": "invalid api key", "type": "invalid_request_error"}}'
)


def _imaging_module():
    from image_testkit import import_memory_diary_submodule

    return import_memory_diary_submodule("imaging")


async def test_looks_like_image_rejects_html_json_and_random_bytes():
    mod = _imaging_module()
    for payload in (
        HTML_PAGE,
        JSON_ERROR,
        b"definitely-not-an-image",
        b"<html></html>",
        b"",
    ):
        assert mod._looks_like_image(payload) is False, (
            f"非图片字节被当成了图片：{payload[:40]!r}"
        )
        assert mod._strict_image_ext(payload) == "", (
            f"严格魔数嗅探把非图片认出来了：{payload[:40]!r}"
        )


async def test_looks_like_image_accepts_four_real_magic_types():
    mod = _imaging_module()
    for data, expected_ext in (
        (PNG, "png"),
        (JPG, "jpg"),
        (WEBP, "webp"),
        (GIF, "gif"),
    ):
        assert mod._strict_image_ext(data) == expected_ext
        assert mod._looks_like_image(data) is True, (
            f"真图片魔数被拒绝：{expected_ext} {data[:8]!r}"
        )


def _install_http(plugin, *, post=None, get=None):
    """替换两个既有 HTTP 注入点（测试用，不联网）。"""
    calls = {"post": [], "get": []}

    async def fake_post(url, payload, timeout, headers=None):
        calls["post"].append(url)
        if post is None:
            raise AssertionError(f"测试未准备 POST 桩，却被调用：{url}")
        return await post(url, payload, timeout, headers)

    async def fake_get(url, timeout, **kwargs):
        calls["get"].append(url)
        if get is None:
            raise AssertionError(f"测试未准备 GET 桩，却被调用：{url}")
        return await get(url, timeout, **kwargs)

    plugin._http_post_json = fake_post
    plugin._http_get = fake_get
    return calls


async def test_generate_image_drops_non_image_2xx_payload():
    """云端 2xx 但返回 HTML/JSON（错误页）：不能当插图返回（P2-6 端到端）。"""
    async with World(config=img_config(image_mode="cloud",
                                       image_api_base="https://image.example.invalid/v1",
                                       image_model="gpt-image-1")) as world:
        plugin = _plugin(world)

        async def html_post(url, payload, timeout, headers):
            return {"data": [{"b64_json": b64(HTML_PAGE)}]}

        _install_http(plugin, post=html_post)
        assert await plugin.generate_image("窗边的黄昏", (768, 1024)) is None, (
            "HTML 错误页被当作插图返回了"
        )

        async def json_post(url, payload, timeout, headers):
            return {"images": [b64(JSON_ERROR)]}

        _install_http(plugin, post=json_post)
        assert await plugin.generate_image("窗边的黄昏", (768, 1024)) is None

        # 参考图文件是 HTML：不允许当参考图读入
        with TempDir() as tmp:
            ref = tmp / "ref.png"
            ref.write_bytes(HTML_PAGE)
            assert plugin._read_ref_bytes(str(ref)) == b"", (
                "非图片参考图被读入了"
            )


# ---------------------------------------------------------------------------
# P2-9 HTTP：响应大小上限 / 关闭重定向 / 下载私网过滤（假 httpx 替身）
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    def __init__(self, status_code=200, chunks=(), headers=None):
        self.status_code = status_code
        self._chunks = list(chunks)
        self.headers = dict(headers or {})
        self.consumed = 0

    async def aiter_bytes(self):
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    """假 httpx.AsyncClient：记录构造参数与请求，按队列返回预设流式响应。"""

    instances: list = []
    responses: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        _FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        resp = (
            _FakeAsyncClient.responses.pop(0)
            if _FakeAsyncClient.responses
            else _FakeStreamResponse()
        )
        return _FakeStreamCtx(resp)


def _install_fake_httpx():
    """把 sys.modules['httpx'] 换成假模块，返回 (module, restore)。"""
    prev = sys.modules.get("httpx")
    fake = types.ModuleType("httpx")
    fake.AsyncClient = _FakeAsyncClient
    sys.modules["httpx"] = fake
    _FakeAsyncClient.instances = []
    _FakeAsyncClient.responses = []

    def restore():
        if prev is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = prev

    return fake, restore


CLOUD_PUBLIC = dict(
    image_mode="cloud",
    image_api_base="https://image.example.invalid/v1",
    image_model="gpt-image-1",
    image_local_url="",
)


async def test_httpx_client_disables_auto_redirect():
    _, restore = _install_fake_httpx()
    try:
        async with World(config=img_config()) as world:
            plugin = _plugin(world)
            client = plugin._httpx_client(5)
            assert client.kwargs.get("follow_redirects") is False, (
                "P2-9：必须关闭自动重定向，否则 302 可把请求带到本机/内网"
            )
    finally:
        restore()


async def test_http_get_aborts_oversized_response_without_buffering_all():
    mod = _imaging_module()
    _, restore = _install_fake_httpx()
    prev_limit = mod.IMAGE_MAX_BYTES
    mod.IMAGE_MAX_BYTES = 1024  # 用小上限验证真实默认路径（不改生产常量）
    try:
        resp = _FakeStreamResponse(chunks=[b"\x00" * 512 for _ in range(100)])
        _FakeAsyncClient.responses = [resp]
        async with World(config=img_config()) as world:
            plugin = _plugin(world)
            out = await plugin._http_get("https://image.example.invalid/out.png", 5)
        assert out is None, "超限响应应被丢弃"
        assert resp.consumed == 3, (
            f"超限后应立即中止读取（只该读到第 3 个 chunk），实际读了 {resp.consumed}"
        )
        assert resp.consumed < 100, "超限响应被整块读完了（内存打满）"
    finally:
        mod.IMAGE_MAX_BYTES = prev_limit
        restore()


async def test_http_get_rejects_302_redirect_to_internal_without_following():
    _, restore = _install_fake_httpx()
    try:
        resp = _FakeStreamResponse(
            status_code=302,
            headers={"location": "http://169.254.169.254/latest/meta-data/"},
        )
        _FakeAsyncClient.responses = [resp]
        async with World(config=img_config(**CLOUD_PUBLIC)) as world:
            plugin = _plugin(world)
            out = await plugin._http_get(
                "https://image.example.invalid/files/out.png", 5
            )
        assert out is None, "302 重定向应被当作失败（不能跟随到内网）"
        client = _FakeAsyncClient.instances[-1]
        assert len(client.requests) == 1, (
            f"关闭重定向后不该有第二次请求，实际 {len(client.requests)} 次"
        )
    finally:
        restore()


async def test_http_get_rejects_private_download_url_from_public_base_without_request():
    _, restore = _install_fake_httpx()
    try:
        async with World(config=img_config(**CLOUD_PUBLIC)) as world:
            plugin = _plugin(world)
            out = await plugin._http_get(
                "http://127.0.0.1:8188/view?filename=x.png", 5
            )
        assert out is None, "公网 base 下不允许下载指向本机/内网的 URL"
        assert _FakeAsyncClient.instances == [], (
            "被拒绝的私网下载地址不该真的发起请求"
        )
    finally:
        restore()


async def test_http_get_rejects_numeric_ip_private_bypass():
    """十进制 IPv4（2130706433 = 127.0.0.1）不能绕过私网过滤。"""
    _, restore = _install_fake_httpx()
    try:
        async with World(config=img_config(**CLOUD_PUBLIC)) as world:
            plugin = _plugin(world)
            assert plugin._is_local_api_base("http://2130706433:8188/") is True
            out = await plugin._http_get("http://2130706433:8188/view?f=x.png", 5)
        assert out is None, "数字形式的回环地址绕过了私网过滤"
        assert _FakeAsyncClient.instances == []
    finally:
        restore()


async def test_http_get_allows_private_cloud_base_same_origin_download():
    """自建/内网 OpenAI 兼容服务（image_api_base 本身是私网）：同源下载必须放行。"""
    _, restore = _install_fake_httpx()
    try:
        _FakeAsyncClient.responses = [_FakeStreamResponse(chunks=[PNG])]
        async with World(
            config=img_config(
                image_mode="cloud",
                image_api_base="http://192.168.1.5:8000/v1",
            )
        ) as world:
            plugin = _plugin(world)
            out = await plugin._http_get(
                "http://192.168.1.5:8000/files/out.png", 5
            )
            # 同是私网但不同源：仍然拒绝（不能被拿去找同网段的其他服务）
            cross = await plugin._http_get(
                "http://192.168.1.6:8000/files/out.png", 5
            )
        assert out == PNG, "配置的 image_api_base 是私网时，其同源下载必须放行"
        assert cross is None, "非配置 base 同源的私网地址仍应拒绝"
    finally:
        restore()


async def test_http_get_allows_local_comfyui_same_origin_download():
    _, restore = _install_fake_httpx()
    try:
        _FakeAsyncClient.responses = [
            _FakeStreamResponse(chunks=[PNG[:8], PNG[8:]])
        ]
        async with World(
            config=img_config(image_local_url="http://127.0.0.1:8188")
        ) as world:
            plugin = _plugin(world)
            out = await plugin._http_get(
                "http://127.0.0.1:8188/view?filename=out.png", 5
            )
        assert out == PNG, "本地 ComfyUI 同源下载必须放行（不能把本地通道拦死）"
        client = _FakeAsyncClient.instances[-1]
        assert client.requests[0]["method"] == "GET"
    finally:
        restore()


async def test_http_post_json_caps_response_body_and_parses_stream():
    mod = _imaging_module()
    _, restore = _install_fake_httpx()
    prev_limit = mod.HTTP_JSON_MAX_BYTES
    try:
        _FakeAsyncClient.responses = [
            _FakeStreamResponse(chunks=[b'{"data": ', b'[]}']),
            _FakeStreamResponse(chunks=[b'{"padding": "' + b"x" * 200 + b'"}']),
        ]
        mod.HTTP_JSON_MAX_BYTES = 64
        async with World(config=img_config(**CLOUD_PUBLIC)) as world:
            plugin = _plugin(world)
            ok = await plugin._http_post_json(
                "https://image.example.invalid/v1/images/generations", {}, 5
            )
            huge = await plugin._http_post_json(
                "https://image.example.invalid/v1/images/generations", {}, 5
            )
        assert ok == {"data": []}, f"流式 JSON 解析失败：{ok!r}"
        assert huge is None, "超过 JSON 响应上限应返回 None"
    finally:
        mod.HTTP_JSON_MAX_BYTES = prev_limit
        restore()


async def test_generate_cloud_rejects_private_image_url_result():
    """P2-9 端到端：云端返回 2xx + 指向本机的 url 时不得下载。"""
    _, restore = _install_fake_httpx()
    try:
        async with World(config=img_config(**CLOUD_PUBLIC)) as world:
            plugin = _plugin(world)

            async def url_post(url, payload, timeout, headers):
                return {"data": [{"url": "http://127.0.0.1:8188/view?f=x.png"}]}

            # 只替换 POST；GET 走真实 _http_get + 假 httpx，才能验证私网过滤
            plugin._http_post_json = url_post
            out = await plugin.generate_image("窗边的黄昏", (768, 1024))
        assert out is None, "私网图片 URL 被下载并当作插图返回了"
        assert _FakeAsyncClient.instances == [], "私网下载地址不该真的发起请求"
    finally:
        restore()

