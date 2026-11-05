# -*- coding: utf-8 -*-
"""v0.6 D2 生图通道：SD-WebUI / ComfyUI / cloud（OpenAI 兼容）+ 尺寸 + 嗅探 + 用量。

设计依据：设计方案第一部分 D2 与《插图功能接口契约》第 4、7 节。
全部通过 monkeypatch 契约第 4 节的 HTTP 注入点（_http_get / _http_post_json）驱动，
不联网、不依赖真实生图服务。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from harness.world import World
from image_testkit import (
    GIF,
    JPG,
    PNG,
    WEBP,
    TempDir,
    b64,
    img_config,
    png,
)

SENDER = "10001"
DEFAULT_PRESET = {
    "portrait": (768, 1024),
    "landscape": (1024, 768),
    "square": (1024, 1024),
}


def _plugin(world):
    return world.refactor.plugin


def _as_pair(value):
    assert isinstance(value, (tuple, list)) and len(value) == 2, (
        f"尺寸档位应返回 (w, h) 二元组，实际 {value!r}"
    )
    return (int(value[0]), int(value[1]))


def _install_http(plugin, *, post=None, get=None):
    """把契约规定的两个注入点替换成测试桩，并返回调用记录。"""
    calls = {"post": [], "get": []}

    async def fake_post(url, payload, timeout, headers=None):
        calls["post"].append(
            {"url": url, "payload": payload, "timeout": timeout, "headers": headers}
        )
        if post is None:
            raise AssertionError(f"测试未准备 POST 桩，却被调用：{url}")
        return await post(url, payload, timeout, headers)

    async def fake_get(url, timeout, **kwargs):
        calls["get"].append({"url": url, "timeout": timeout, "kwargs": kwargs})
        if get is None:
            raise AssertionError(f"测试未准备 GET 桩，却被调用：{url}")
        return await get(url, timeout, **kwargs)

    plugin._http_post_json = fake_post
    plugin._http_get = fake_get
    return calls


# ---------------------------------------------------------------------------
# _image_cfg()：归一化 + 非法值回退
# ---------------------------------------------------------------------------


async def test_image_cfg_defaults_when_config_has_no_image_keys():
    async with World(config={"image_enabled": True}) as world:
        plugin = _plugin(world)
        cfg = plugin._image_cfg()
        assert cfg["image_enabled"] is True
        assert cfg["image_mode"] == "local"
        assert cfg["image_local_url"] == ""
        assert float(cfg["image_prob_memo"]) == 0.0
        assert float(cfg["image_prob_full"]) == 0.3
        assert int(cfg["image_limit_memo"]) == 0
        assert int(cfg["image_limit_full"]) == 2
        assert int(cfg["image_limit_daily"]) == 6
        assert int(cfg["image_limit_group"]) == 0
        assert int(cfg["image_concurrency"]) == 2
        assert int(cfg["image_timeout"]) == 120
        assert cfg["persona_image_desc"] == ""
        assert cfg["image_style_prompt"] == ""
        assert bool(cfg["user_image_enabled"]) is True
        assert int(cfg["user_image_limit"]) == 1

        preset = cfg["image_size_preset"]
        for key, expected in DEFAULT_PRESET.items():
            assert key in preset, f"_image_cfg 的 image_size_preset 缺少 {key}"
            assert _as_pair(preset[key]) == expected


async def test_image_cfg_illegal_values_fall_back_without_raising():
    async with World(
        config=img_config(
            image_mode="weird-mode",
            image_prob_full="not-a-number",
            image_limit_daily=-5,
            image_concurrency=99,
            image_timeout="bad",
            image_size_preset="{not valid json",
        )
    ) as world:
        plugin = _plugin(world)
        cfg = plugin._image_cfg()  # 不抛

        assert cfg["image_mode"] == "local", "非法 image_mode 应回退 local"
        assert float(cfg["image_prob_full"]) == 0.3, "非法概率应回退默认 0.3"
        assert int(cfg["image_limit_daily"]) == 6, "非法每日限额应回退默认 6"
        assert 1 <= int(cfg["image_concurrency"]) <= 8, (
            f"并发上限应钳制在 1~8，实际 {cfg['image_concurrency']}"
        )
        assert int(cfg["image_timeout"]) == 120, "非法超时应回退默认 120"
        preset = cfg["image_size_preset"]
        for key, expected in DEFAULT_PRESET.items():
            assert _as_pair(preset[key]) == expected, (
                f"非法 image_size_preset 应整体回退默认，{key}={preset.get(key)!r}"
            )


async def test_image_cfg_size_preset_accepts_json_string_and_dict():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)

        plugin.config["image_size_preset"] = json.dumps(
            {"portrait": [512, 768], "landscape": [768, 512], "square": [512, 512]}
        )
        preset = plugin._image_cfg()["image_size_preset"]
        assert _as_pair(preset["portrait"]) == (512, 768)
        assert _as_pair(preset["landscape"]) == (768, 512)
        assert _as_pair(preset["square"]) == (512, 512)

        plugin.config["image_size_preset"] = {
            "portrait": [640, 800],
            "landscape": [800, 640],
            "square": [800, 800],
        }
        preset = plugin._image_cfg()["image_size_preset"]
        assert _as_pair(preset["portrait"]) == (640, 800)


# ---------------------------------------------------------------------------
# _image_size()：方向档位
# ---------------------------------------------------------------------------


async def test_image_size_orientation_aliases_and_custom_preset():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)

        assert _as_pair(plugin._image_size("portrait")) == (768, 1024)
        assert _as_pair(plugin._image_size("landscape")) == (1024, 768)
        assert _as_pair(plugin._image_size("square")) == (1024, 1024)
        for alias, expected in (
            ("竖", (768, 1024)),
            ("横", (1024, 768)),
            ("方", (1024, 1024)),
            ("vertical", (768, 1024)),
            ("horizontal", (1024, 768)),
            ("square", (1024, 1024)),
        ):
            assert _as_pair(plugin._image_size(alias)) == expected, (
                f"方向别名 {alias!r} 映射错误"
            )

        # 未知方向不抛，回退到三档之一
        fallback = _as_pair(plugin._image_size("???"))
        assert fallback in DEFAULT_PRESET.values(), f"未知方向回退值非法：{fallback}"

        # 自定义档位要生效（适配本地模型原生分辨率）
        plugin.config["image_size_preset"] = json.dumps(
            {"portrait": [512, 768], "landscape": [768, 512], "square": [640, 640]}
        )
        assert _as_pair(plugin._image_size("portrait")) == (512, 768)
        assert _as_pair(plugin._image_size("竖")) == (512, 768)
        assert _as_pair(plugin._image_size("landscape")) == (768, 512)


# ---------------------------------------------------------------------------
# 图片字节嗅探
# ---------------------------------------------------------------------------


async def test_sniff_image_ext_and_mime():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        cases = [
            (PNG, "png", "image/png"),
            (JPG, "jpg", "image/jpeg"),
            (WEBP, "webp", "image/webp"),
            (GIF, "gif", "image/gif"),
        ]
        for data, ext, mime in cases:
            assert plugin.sniff_image_ext(data) == ext, f"扩展名嗅探错误：{data[:8]!r}"
            assert plugin.sniff_image_mime(data) == mime, f"MIME 嗅探错误：{data[:8]!r}"
        # 认不出时回退 png（不信 Content-Type / 扩展名）
        assert plugin.sniff_image_ext(b"definitely-not-an-image") == "png"
        assert plugin.sniff_image_mime(b"definitely-not-an-image") == "image/png"
        assert plugin.sniff_image_ext(b"") == "png"


# ---------------------------------------------------------------------------
# SD-WebUI 通道（mock /sdapi/v1/txt2img）
# ---------------------------------------------------------------------------


async def test_generate_sd_webui_success():
    async with World(config=img_config(image_timeout=42)) as world:
        plugin = _plugin(world)
        image = png(11)

        async def fake_post(url, payload, timeout, headers):
            return {"images": [b64(image)], "info": "{}"}

        async def fake_get(url, timeout, **kwargs):
            # 通道探测：ComfyUI /system_stats 探不到，SD WebUI /sdapi/v1/options 探得到
            if "/system_stats" in url:
                return None
            if "/sdapi/v1/options" in url:
                return b"{}"
            raise AssertionError(f"SD-WebUI 通道不应 GET 其他地址：{url}")

        calls = _install_http(plugin, post=fake_post, get=fake_get)
        out = await plugin.generate_image("窗边的黄昏", (768, 1024))

        assert out == image, "SD-WebUI 通道没有返回解码后的图片字节"
        assert calls["post"], "SD-WebUI 通道没有发起请求"
        call = calls["post"][-1]
        assert "sdapi/v1/txt2img" in call["url"], f"请求地址不对：{call['url']}"
        assert call["payload"]["width"] == 768
        assert call["payload"]["height"] == 1024
        assert "窗边的黄昏" in str(call["payload"].get("prompt", "")), (
            f"prompt 没有进 SD 请求：{call['payload']!r}"
        )
        assert call["timeout"] and call["timeout"] > 0, "HTTP 超时参数没有传给注入点"


# ---------------------------------------------------------------------------
# ComfyUI 通道（mock /prompt + /history + /view 轮询）
# ---------------------------------------------------------------------------


async def test_generate_comfyui_success_polls_history_and_downloads_image():
    async with World(
        config=img_config(image_local_url="http://127.0.0.1:8188")
    ) as world:
        plugin = _plugin(world)
        image = png(12)
        prompt_id = "pid-test-1"
        history = {
            prompt_id: {
                "outputs": {
                    "9": {
                        "images": [
                            {"filename": "final.png", "subfolder": "", "type": "output"}
                        ]
                    }
                }
            }
        }

        async def fake_post(url, payload, timeout, headers):
            if "sdapi" in url:
                return None  # SD-WebUI 探测失败 -> 走 ComfyUI
            if url.rstrip("/").endswith("/prompt"):
                return {"prompt_id": prompt_id}
            raise AssertionError(f"ComfyUI 通道出现未知 POST：{url}")

        async def fake_get(url, timeout, **kwargs):
            if "/system_stats" in url:
                return b'{"system": {"os": "test"}}'
            if "/object_info/CheckpointLoaderSimple" in url:
                return json.dumps(
                    {
                        "CheckpointLoaderSimple": {
                            "input": {
                                "required": {"ckpt_name": [["test-model.safetensors"]]}
                            }
                        }
                    }
                ).encode("utf-8")
            if "/history/" in url:
                return json.dumps(history).encode("utf-8")
            if "/view" in url:
                return image
            raise AssertionError(f"ComfyUI 通道出现未知 GET：{url}")

        calls = _install_http(plugin, post=fake_post, get=fake_get)
        out = await plugin.generate_image("窗边的黄昏", (1024, 768))

        assert out == image, "ComfyUI 通道没有返回下载到的图片字节"
        post_urls = [c["url"] for c in calls["post"]]
        get_urls = [c["url"] for c in calls["get"]]
        assert any("prompt" in u for u in post_urls), f"没有提交 ComfyUI 工作流：{post_urls}"
        assert any("/history/" in u for u in get_urls), f"没有轮询 ComfyUI 历史：{get_urls}"
        assert any("/view" in u for u in get_urls), f"没有下载 ComfyUI 输出图：{get_urls}"


# ---------------------------------------------------------------------------
# cloud 通道（OpenAI 兼容 images API）
# ---------------------------------------------------------------------------

CLOUD_CFG = dict(
    image_mode="cloud",
    image_api_base="https://image.example.invalid/v1",
    image_api_key="sk-test-key",
    image_model="gpt-image-1",
    image_local_url="",
)


async def test_generate_cloud_b64_json_success():
    async with World(config=img_config(**CLOUD_CFG)) as world:
        plugin = _plugin(world)
        image = png(13)

        async def fake_post(url, payload, timeout, headers):
            return {"data": [{"b64_json": b64(image)}]}

        calls = _install_http(plugin, post=fake_post)
        out = await plugin.generate_image("窗边的黄昏", (768, 1024))

        assert out == image, "cloud 通道没有返回解码后的图片字节"
        call = calls["post"][-1]
        assert call["url"].rstrip("/").endswith("/images/generations"), (
            f"cloud 请求地址不是 OpenAI images API：{call['url']}"
        )
        assert "窗边的黄昏" in str(call["payload"].get("prompt", ""))
        headers = call["headers"] or {}
        auth = {str(k).lower(): v for k, v in headers.items()}.get("authorization", "")
        assert "sk-test-key" in str(auth), f"cloud 请求没有带 API key：{headers!r}"


async def test_generate_cloud_can_download_url_result():
    async with World(config=img_config(**CLOUD_CFG)) as world:
        plugin = _plugin(world)
        image = png(14)
        image_url = "https://image.example.invalid/files/out.png"

        async def fake_post(url, payload, timeout, headers):
            return {"data": [{"url": image_url}]}

        async def fake_get(url, timeout, **kwargs):
            return image

        calls = _install_http(plugin, post=fake_post, get=fake_get)
        out = await plugin.generate_image("窗边的黄昏", (768, 1024))

        assert out == image, "cloud 返回 URL 时没有下载图片字节"
        assert any(image_url in c["url"] for c in calls["get"]), (
            "cloud 返回 URL 时没有去下载：%r" % [c["url"] for c in calls["get"]]
        )


# ---------------------------------------------------------------------------
# 失败/超时：返回 None，绝不抛给调用方
# ---------------------------------------------------------------------------


async def test_generate_returns_none_on_timeout_and_failure_never_raises():
    for mode_cfg in (
        dict(image_mode="local", image_local_url="http://127.0.0.1:7860"),
        img_config(**CLOUD_CFG),
    ):
        async with World(config=img_config(**mode_cfg)) as world:
            plugin = _plugin(world)

            async def timeout_post(url, payload, timeout, headers):
                raise asyncio.TimeoutError()

            async def timeout_get(url, timeout, **kwargs):
                raise asyncio.TimeoutError()

            _install_http(plugin, post=timeout_post, get=timeout_get)
            out = await plugin.generate_image("窗边的黄昏", (768, 1024))
            assert out is None, f"{mode_cfg['image_mode']} 超时应返回 None，实际 {out!r}"

            async def broken_post(url, payload, timeout, headers):
                raise RuntimeError("upstream 500")

            async def broken_get(url, timeout, **kwargs):
                raise RuntimeError("upstream 500")

            _install_http(plugin, post=broken_post, get=broken_get)
            out = await plugin.generate_image("窗边的黄昏", (768, 1024))
            assert out is None, f"{mode_cfg['image_mode']} 上游报错应返回 None，实际 {out!r}"

            async def none_post(url, payload, timeout, headers):
                return None

            async def none_get(url, timeout, **kwargs):
                return None

            _install_http(plugin, post=none_post, get=none_get)
            out = await plugin.generate_image("窗边的黄昏", (768, 1024))
            assert out is None


async def test_generate_cloud_without_api_key_degrades_without_fake_auth():
    """cloud 模式空 key：允许请求自建/内网 OpenAI 兼容服务（可能不校验 key），
    但绝不能伪造 Authorization 头；服务端拒绝时返回 None。"""
    async with World(
        config=img_config(
            image_mode="cloud",
            image_api_base="https://image.example.invalid/v1",
            image_api_key="",
            image_model="gpt-image-1",
        )
    ) as world:
        plugin = _plugin(world)

        async def denied_post(url, payload, timeout, headers):
            return None  # 服务端鉴权拒绝

        async def denied_get(url, timeout, **kwargs):
            return None

        calls = _install_http(plugin, post=denied_post, get=denied_get)
        out = await plugin.generate_image("窗边的黄昏", (768, 1024))
        assert out is None, "cloud 通道请求被拒绝时应返回 None"
        assert calls["post"], "空 key 仍应尝试请求（兼容不校验 key 的自建服务）"
        headers = calls["post"][0]["headers"] or {}
        assert not any(str(k).lower() == "authorization" for k in headers), (
            f"没有 API key 时不应伪造 Authorization 头：{headers!r}"
        )


async def test_generate_retries_once_without_reference_photo():
    """参考图只在通道支持时传；失败自动退回纯文字重试一次。"""
    with TempDir() as tmp:
        ref = Path(tmp) / "ref.png"
        ref.write_bytes(png(90))
        async with World(
            config=img_config(image_local_url="http://127.0.0.1:7860")
        ) as world:
            plugin = _plugin(world)
            image = png(15)
            payloads = []

            async def flaky_post(url, payload, timeout, headers):
                payloads.append(payload)
                if len(payloads) == 1:
                    return None  # 带参考图的那次失败
                return {"images": [b64(image)]}

            _install_http(plugin, post=flaky_post)
            out = await plugin.generate_image(
                "窗边的黄昏", (768, 1024), ref_path=str(ref)
            )

            assert out == image, "参考图失败后没有退回纯文字重试"
            assert len(payloads) == 2, (
                f"参考图失败应恰好多重试一次，实际请求 {len(payloads)} 次"
            )


# ---------------------------------------------------------------------------
# 每日用量（全局兜底 image_limit_daily）
# ---------------------------------------------------------------------------


async def test_image_usage_count_limit_and_file_format():
    async with World(config=img_config(image_limit_daily=2)) as world:
        plugin = _plugin(world)

        assert plugin._image_usage_remaining() == 2
        assert plugin._image_usage_consume() is True
        assert plugin._image_usage_consume() is True
        assert plugin._image_usage_consume() is False, "超过每日上限仍允许消耗"
        assert plugin._image_usage_remaining() == 0

        rec = plugin._image_usage_load()
        assert rec.get("date") == "20261023", f"用量记录日期不对：{rec!r}"
        assert int(rec.get("count", -1)) == 2, f"用量记录计数不对：{rec!r}"

        usage_file = Path(plugin.data_dir) / plugin.IMAGE_USAGE_FILE
        assert usage_file.exists(), "每日用量没有落盘"
        on_disk = json.loads(usage_file.read_text(encoding="utf-8"))
        assert on_disk.get("date") == "20261023"
        assert int(on_disk.get("count", -1)) == 2


async def test_image_usage_resets_on_new_day():
    async with World(config=img_config(image_limit_daily=3)) as world:
        plugin = _plugin(world)
        usage_file = Path(plugin.data_dir) / plugin.IMAGE_USAGE_FILE
        usage_file.write_text(
            json.dumps({"date": "20261022", "count": 3}), encoding="utf-8"
        )
        plugin._image_usage_load()  # 触发加载（可能缓存）

        assert plugin._image_usage_remaining() == 3, "跨天后每日用量没有重置"
        assert plugin._image_usage_consume() is True
        rec = plugin._image_usage_load()
        assert rec.get("date") == "20261023"
        assert int(rec.get("count", -1)) == 1


async def test_image_usage_remaining_never_negative_when_count_over_limit():
    async with World(config=img_config(image_limit_daily=1)) as world:
        plugin = _plugin(world)
        usage_file = Path(plugin.data_dir) / plugin.IMAGE_USAGE_FILE
        usage_file.write_text(
            json.dumps({"date": "20261023", "count": 9}), encoding="utf-8"
        )
        plugin._image_usage_load()
        assert plugin._image_usage_remaining() == 0, "超限后剩余量应为 0，不能是负数"
        assert plugin._image_usage_consume() is False


# ---------------------------------------------------------------------------
# 可选视觉接口：caption_image（设计 7.4 诚实降级；隐私：只在显式配置时联网）
# ---------------------------------------------------------------------------

VISION_CFG = dict(
    vision_api_base="https://vision.example.invalid/v1",
    vision_api_key="vk-test",
    vision_api_model="gpt-4o-mini",
)


async def test_caption_image_unconfigured_returns_empty_without_request():
    async with World(config=img_config()) as world:
        plugin = _plugin(world)
        calls = _install_http(plugin)  # 未准备桩：真被调用会抛 AssertionError
        text = await plugin.caption_image(png(31))
        assert text == "", "未配置视觉接口时应返回空串（保持内容未识别）"
        assert calls["post"] == [] and calls["get"] == [], (
            "未配置视觉接口时不应发起任何网络请求"
        )


async def test_caption_image_success_parses_openai_multimodal_response():
    async with World(config=img_config(**VISION_CFG)) as world:
        plugin = _plugin(world)
        image = png(32)

        async def fake_post(url, payload, timeout, headers):
            return {
                "choices": [
                    {"message": {"content": "一只橘猫趴在窗台上，尾巴自然垂着。"}}
                ]
            }

        calls = _install_http(plugin, post=fake_post)
        text = await plugin.caption_image(image)

        assert text == "一只橘猫趴在窗台上，尾巴自然垂着。"
        call = calls["post"][-1]
        assert call["url"] == "https://vision.example.invalid/v1/chat/completions", (
            f"视觉接口地址不对：{call['url']}"
        )
        assert call["payload"]["model"] == "gpt-4o-mini"
        messages = call["payload"]["messages"]
        parts = messages[0]["content"]
        image_parts = [p for p in parts if p.get("type") == "image_url"]
        assert image_parts, f"请求里没有图片输入：{parts!r}"
        url = image_parts[0]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,"), url
        assert b64(image) in url
        headers = call["headers"] or {}
        auth = {str(k).lower(): v for k, v in headers.items()}.get("authorization", "")
        assert "vk-test" in str(auth), f"视觉接口没有带 API key：{headers!r}"


async def test_caption_image_handles_failures_and_content_list_form():
    async with World(config=img_config(**VISION_CFG)) as world:
        plugin = _plugin(world)

        async def timeout_post(url, payload, timeout, headers):
            raise asyncio.TimeoutError()

        _install_http(plugin, post=timeout_post)
        assert await plugin.caption_image(png(33)) == "", "视觉接口超时/异常应返回空串"

        async def none_post(url, payload, timeout, headers):
            return None

        _install_http(plugin, post=none_post)
        assert await plugin.caption_image(png(33)) == ""

        async def empty_post(url, payload, timeout, headers):
            return {"choices": []}

        _install_http(plugin, post=empty_post)
        assert await plugin.caption_image(png(33)) == ""

        # content 为分段列表（部分服务商）也要能拼起来
        async def list_post(url, payload, timeout, headers):
            return {
                "choices": [
                    {"message": {"content": [{"type": "text", "text": "窗台上的橘猫"}]}}
                ]
            }

        _install_http(plugin, post=list_post)
        assert await plugin.caption_image(png(33)) == "窗台上的橘猫"

        # 空图片字节：不发请求
        calls = _install_http(plugin, post=list_post)
        assert await plugin.caption_image(b"") == ""
        assert calls["post"] == []


# ---------------------------------------------------------------------------
# 每日额度退还：生成失败不计数（配置页承诺）
# ---------------------------------------------------------------------------


async def test_image_usage_refund_returns_quota_and_never_goes_negative():
    async with World(config=img_config(image_limit_daily=6)) as world:
        plugin = _plugin(world)
        assert plugin._image_usage_consume() is True
        assert plugin._image_usage_load()["count"] == 1

        plugin._image_usage_refund()
        assert plugin._image_usage_load()["count"] == 0, "退还后计数应回到 0"

        plugin._image_usage_refund()
        assert plugin._image_usage_load()["count"] == 0, "退还不能把计数减成负数"
