# -*- coding: utf-8 -*-
"""插图生成通道（v0.6 D2）：与聊天 LLM 完全解耦的独立生图链路。

设计第 3 节：DeepSeek 等聊天模型没有图像生成能力，插图走独立通道：

- local（默认，隐私优先）：本地 Stable Diffusion WebUI / ComfyUI，
  日记 context、形象描述、用户照片全程不出机器。地址填到根（如
  ``http://127.0.0.1:7860`` / ``http://127.0.0.1:8188``），通道自动识别。
- cloud（显式 opt-in）：OpenAI 兼容 images API（硅基流动 / OpenAI / 可灵 /
  豆包等），``image_api_base`` + ``image_api_key`` + ``image_model``。

统一接口：``generate_image(prompt, size, ref_path) -> bytes | None``。
失败（含超时）一律返回 None，绝不抛给调用方——生图失败从不阻塞日记、
从不中断对话（设计第 5 节）。并发上限与单篇/每日限额在调度层
（illustration.py）处理，这里只负责"单张图怎么生成出来"。

参考图：cloud 走 ``/images/edits``（OpenAI gpt-image-1 等支持时），local 走
SD WebUI img2img；ComfyUI 的 IP-Adapter 工作流不在本模块内置（需要用户自己的
工作流，设计只要求"支持时可垫图"，不支持的通道自动退化纯文字 + 固定 seed 范围）。

依赖：httpx（AstrBot 自带，运行时延迟 import；单测可 monkeypatch _http_* 注入点）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from pathlib import Path

from .common import sniff_image_ext, sniff_image_mime
from .constants import (
    DEFAULT_IMAGE_SIZE_PRESET,
    IMAGE_MAX_BYTES,
    IMAGE_USAGE_FILE,
)
from .log import logger


def _to_int(value, default: int, lo: int, hi: int) -> int:
    """配置整数归一：非法值（含低于下限的负值）回退 default，超上限钳制。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < lo:
        return default
    return min(hi, n)


def _to_float(value, default: float, lo: float, hi: float) -> float:
    """配置浮点归一：非法值回退 default，并钳制到 [lo, hi]。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _normalize_preset(raw) -> dict:
    """解析 image_size_preset：接受 JSON 文本或 dict，缺项/非法项回退默认。

    三档固定尺寸（设计 7.3）：portrait 3:4、landscape 4:3、square 1:1。
    管理员可用它整体替换成自己模型的原生分辨率（如 SD 的 512/768 档）。
    """
    data = {}
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
    elif isinstance(raw, dict):
        data = raw
    out = {}
    for key, default in DEFAULT_IMAGE_SIZE_PRESET.items():
        val = data.get(key) if isinstance(data, dict) else None
        size = None
        if isinstance(val, (list, tuple)) and len(val) == 2:
            try:
                w, h = int(val[0]), int(val[1])
                if 64 <= w <= 4096 and 64 <= h <= 4096:
                    size = [w, h]
            except (TypeError, ValueError):
                size = None
        out[key] = size or list(default)
    return out


def _looks_like_image(data: bytes) -> bool:
    """是不是一张认得出的图片（防止把 JSON 错误页当图写进 zip）。"""
    if not data or len(data) < 16:
        return False
    if len(data) > IMAGE_MAX_BYTES:
        return False
    return sniff_image_ext(data) in ("png", "jpg", "webp", "gif")


class ImagingMixin:
    """生图通道与每日用量；由 MemoryDiaryCore 组合（不 import AstrBot）。"""

    IMAGE_USAGE_FILE = IMAGE_USAGE_FILE

    # ---- 配置归一化 ----

    def _image_cfg(self) -> dict:
        """把插件 config 归一化成插图配置 dict（键名与配置项原名一致）。

        只读 config，不改它；调用频繁、成本可忽略（字典读取）。
        image_size_preset 在这里解析成 {"portrait":[w,h],...} 供 _image_size 用。
        """
        cfg = self.config or {}
        mode = str(cfg.get("image_mode", "local") or "local").strip().lower()
        if mode not in ("local", "cloud"):
            mode = "local"

        def _s(key: str) -> str:
            return str(cfg.get(key, "") or "").strip()

        return {
            "image_enabled": bool(cfg.get("image_enabled", False)),
            "image_mode": mode,
            "image_local_url": _s("image_local_url").rstrip("/"),
            "image_api_base": _s("image_api_base").rstrip("/"),
            "image_api_key": _s("image_api_key"),
            "image_model": _s("image_model"),
            "image_size_preset": _normalize_preset(cfg.get("image_size_preset")),
            "image_prob_memo": _to_float(
                cfg.get("image_prob_memo", 0) or 0, 0.0, 0.0, 1.0
            ),
            "image_prob_full": _to_float(
                cfg.get("image_prob_full", 0.3), 0.3, 0.0, 1.0
            ),
            "image_limit_memo": _to_int(
                cfg.get("image_limit_memo", 0) or 0, 0, 0, 20
            ),
            "image_limit_full": _to_int(
                cfg.get("image_limit_full", 2), 2, 0, 20
            ),
            "image_limit_daily": _to_int(
                cfg.get("image_limit_daily", 6), 6, 0, 500
            ),
            "image_limit_group": _to_int(
                cfg.get("image_limit_group", 0) or 0, 0, 0, 20
            ),
            "image_concurrency": _to_int(
                cfg.get("image_concurrency", 2), 2, 1, 8
            ),
            "image_timeout": _to_int(cfg.get("image_timeout", 120), 120, 10, 600),
            "persona_image_desc": _s("persona_image_desc"),
            "image_style_prompt": _s("image_style_prompt"),
            "user_ref_photo": _s("user_ref_photo"),
            "user_image_enabled": bool(cfg.get("user_image_enabled", True)),
            "user_image_limit": _to_int(
                cfg.get("user_image_limit", 1), 1, 0, 10
            ),
            "vision_api_base": _s("vision_api_base"),
            "vision_api_key": _s("vision_api_key"),
            "vision_api_model": _s("vision_api_model"),
        }

    def _image_size(self, orientation: str) -> tuple:
        """方向 → 固定尺寸档位（设计 7.3）。认不出按方图。"""
        key = self._normalize_orientation(orientation)
        sizes = self._image_cfg()["image_size_preset"]
        size = sizes.get(key) or DEFAULT_IMAGE_SIZE_PRESET["square"]
        return int(size[0]), int(size[1])

    @staticmethod
    def _normalize_orientation(raw) -> str:
        """把 AI 给的横竖方向归一成 portrait / landscape / square。"""
        text = str(raw or "").strip().lower()
        if text in ("横", "横图", "横构图", "landscape", "horizontal", "wide", "4:3"):
            return "landscape"
        if text in ("竖", "竖图", "竖构图", "portrait", "vertical", "tall", "3:4"):
            return "portrait"
        if text in ("方", "方图", "方形", "square", "1:1"):
            return "square"
        return "square"

    # ---- 图片字节 ----

    @staticmethod
    def sniff_image_ext(data: bytes) -> str:
        return sniff_image_ext(data)

    @staticmethod
    def sniff_image_mime(data: bytes) -> str:
        return sniff_image_mime(data)

    # ---- 统一生图入口 ----

    async def generate_image(self, prompt: str, size: tuple,
                             ref_path: str = "") -> bytes | None:
        """生成一张图。成功返回图片字节，任何失败/超时返回 None（并记日志）。

        ref_path 非空时先尝试带参考图；通道不支持或失败时自动退回纯文字重试一次。
        """
        cfg = self._image_cfg()
        if not cfg["image_enabled"]:
            return None
        if not str(prompt or "").strip():
            return None
        timeout = cfg["image_timeout"]
        ref = str(ref_path or "").strip()
        attempts = [ref] if ref else [""]
        if ref:
            attempts.append("")  # 参考图失败 → 纯文字兜底
        for attempt_ref in attempts:
            try:
                coro = (
                    self._generate_cloud(cfg, prompt, size, attempt_ref)
                    if cfg["image_mode"] == "cloud"
                    else self._generate_local(cfg, prompt, size, attempt_ref)
                )
                data = await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[memory_diary] 生图超时（{timeout}s，"
                    f"mode={cfg['image_mode']}），本次放弃"
                )
                return None
            except Exception as e:
                logger.warning(
                    f"[memory_diary] 生图失败（mode={cfg['image_mode']}）: {e}"
                )
                return None
            if data and _looks_like_image(data):
                return data
            if data:
                logger.warning(
                    "[memory_diary] 生图服务返回的内容不像图片，已丢弃"
                )
            if attempt_ref:
                logger.info("[memory_diary] 参考图生成失败，改用纯文字重试一次")
        return None

    # ---- local 通道：SD WebUI / ComfyUI ----

    async def _generate_local(self, cfg: dict, prompt: str, size: tuple,
                              ref_path: str) -> bytes | None:
        base = cfg["image_local_url"]
        if not base:
            logger.warning(
                "[memory_diary] 本地生图模式但 image_local_url 为空，本次不画"
            )
            return None
        kind = self._local_kind_hint(base)
        if kind == "comfy":
            return await self._generate_comfyui_safe(base, prompt, size, ref_path)
        if kind == "sdwebui":
            return await self._generate_sd_webui(base, prompt, size, ref_path)
        # 未知端口：先按最常见的 SD WebUI 试一次，失败再试 ComfyUI
        #（用户只填一个根地址；两种本地部署都能开箱即用，坏地址时多一次失败请求）
        try:
            data = await self._generate_sd_webui(base, prompt, size, ref_path)
        except Exception as e:
            logger.debug(f"[memory_diary] SD WebUI 通道不可用（{base}）: {e}")
            data = None
        if data:
            return data
        return await self._generate_comfyui_safe(base, prompt, size, ref_path)

    @staticmethod
    def _local_kind_hint(base: str) -> str:
        """按端口猜本地通道类型：8188 = ComfyUI；7860~7865 = SD WebUI；其余 auto。"""
        try:
            from urllib.parse import urlparse

            port = urlparse(base).port or 0
        except Exception:
            port = 0
        if port == 8188:
            return "comfy"
        if port in (7860, 7861, 7862, 7863, 7864, 7865):
            return "sdwebui"
        return "auto"

    async def _generate_comfyui_safe(self, base: str, prompt: str,
                                     size: tuple, ref_path: str):
        """ComfyUI 失败不抛（自动探测路径里最容易受坏地址/缺节点影响）。"""
        try:
            return await self._generate_comfyui(base, prompt, size, ref_path)
        except Exception as e:
            logger.warning(f"[memory_diary] ComfyUI 通道生成失败（{base}）: {e}")
            return None

    async def _generate_sd_webui(self, base: str, prompt: str, size: tuple,
                                 ref_path: str) -> bytes | None:
        """Stable Diffusion WebUI（AUTOMATIC1111）txt2img / img2img。"""
        w, h = int(size[0]), int(size[1])
        seed = self._image_seed()
        payload = {
            "prompt": prompt,
            "negative_prompt": (
                "文字, 水印, 签名, 边框, logo, text, watermark, signature, border"
            ),
            "width": w,
            "height": h,
            "steps": 28,
            "cfg_scale": 6.0,
            "seed": seed,
            "sampler_name": "DPM++ 2M Karras",
        }
        url = f"{base}/sdapi/v1/txt2img"
        if ref_path:
            ref_b64 = self._read_ref_b64(ref_path)
            if ref_b64:
                url = f"{base}/sdapi/v1/img2img"
                payload.update(
                    {
                        "init_images": [ref_b64],
                        "denoising_strength": 0.55,
                    }
                )
        resp = await self._http_post_json(
            url, payload, self._image_cfg()["image_timeout"]
        )
        if not isinstance(resp, dict):
            return None
        images = resp.get("images")
        if not isinstance(images, list) or not images:
            return None
        return self._decode_b64_image(images[0])

    async def _generate_comfyui(self, base: str, prompt: str, size: tuple,
                                ref_path: str) -> bytes | None:
        """ComfyUI：用内置的标准 txt2img 工作流（节点 id 固定）提交并轮询取图。

        注意：参考图需要用户自己的 IP-Adapter 工作流，本实现不内置（设计只要求
        "本地部署具备参考图能力时效果最佳"，不强制）；ref_path 在这里忽略。
        """
        if ref_path:
            logger.debug(
                "[memory_diary] ComfyUI 通道暂不支持参考图垫图，改用纯文字生成"
            )
        ckpt = await self._comfy_first_checkpoint(base)
        if not ckpt:
            logger.warning(
                "[memory_diary] ComfyUI 里没找到可用的 checkpoint，本次不画"
            )
            return None
        w, h = int(size[0]), int(size[1])
        negative = (
            "文字, 水印, 签名, 边框, logo, text, watermark, signature, border"
        )
        workflow = {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": self._image_seed(),
                    "steps": 28,
                    "cfg": 6.0,
                    "sampler_name": "dpmpp_2m",
                    "scheduler": "karras",
                    "denoise": 1.0,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                },
            },
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": ckpt},
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": w, "height": h, "batch_size": 1},
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["4", 1]},
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative, "clip": ["4", 1]},
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": "memory_diary", "images": ["8", 0]},
            },
        }
        client_id = secrets.token_hex(8)
        resp = await self._http_post_json(
            f"{base}/prompt", {"prompt": workflow, "client_id": client_id}, 30
        )
        prompt_id = ""
        if isinstance(resp, dict):
            prompt_id = str(resp.get("prompt_id") or "")
        if not prompt_id:
            return None
        # 轮询 /history/<prompt_id>；最多等 90 秒（上层还有整体超时兜底）
        for _ in range(180):
            await asyncio.sleep(0.5)
            hist = await self._http_get(f"{base}/history/{prompt_id}", 15)
            if not hist:
                continue
            try:
                data = json.loads(hist.decode("utf-8", "ignore"))
            except Exception:
                continue
            record = data.get(prompt_id) if isinstance(data, dict) else None
            if not isinstance(record, dict):
                continue
            images = []
            for node_output in (record.get("outputs") or {}).values():
                if isinstance(node_output, dict):
                    images.extend(node_output.get("images") or [])
            if not images:
                continue
            info = images[0] if isinstance(images[0], dict) else {}
            filename = str(info.get("filename") or "")
            if not filename:
                continue
            query = (
                f"?filename={_quote(filename)}"
                f"&subfolder={_quote(str(info.get('subfolder') or ''))}"
                f"&type={_quote(str(info.get('type') or 'output'))}"
            )
            return await self._http_get(f"{base}/view{query}", 30)
        return None

    async def _comfy_first_checkpoint(self, base: str) -> str:
        """从 ComfyUI /object_info 取第一个可用 checkpoint 名。

        取不到（老版本/接口被裁剪/测试桩没提供）时回退一个常见 checkpoint 名：
        ComfyUI 会以 "checkpoint 不存在" 报错 → 上层按失败处理并清占位，
        不会留下坏状态；这里不抛。
        """
        default_ckpt = "sd_xl_base_1.0.safetensors"
        try:
            raw = await self._http_get(
                f"{base}/object_info/CheckpointLoaderSimple", 15
            )
        except Exception:
            return default_ckpt
        if not raw:
            return default_ckpt
        try:
            data = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            return default_ckpt
        try:
            required = data["CheckpointLoaderSimple"]["input"]["required"]
            choices = required["ckpt_name"][0]
            if isinstance(choices, list) and choices:
                return str(choices[0])
        except Exception:
            pass
        return default_ckpt

    # ---- cloud 通道：OpenAI 兼容 images API ----

    async def _generate_cloud(self, cfg: dict, prompt: str, size: tuple,
                              ref_path: str) -> bytes | None:
        base = cfg["image_api_base"]
        if not base:
            logger.warning(
                "[memory_diary] 云端生图模式但 image_api_base 为空，本次不画"
            )
            return None
        # 空 key 也照常请求：由服务端鉴权决定（部分自建/内网 OpenAI 兼容服务不校验
        # key；真正需要 key 的服务商返回 401 后按失败处理，不阻塞日记）。
        headers = {}
        if cfg["image_api_key"]:
            headers["Authorization"] = f"Bearer {cfg['image_api_key']}"
        w, h = int(size[0]), int(size[1])
        size_text = f"{w}x{h}"
        url = self._cloud_endpoint(base, "generations")

        if ref_path:
            ref_bytes = self._read_ref_bytes(ref_path)
            if ref_bytes:
                edited = await self._cloud_edit(
                    cfg, url, headers, prompt, size_text, ref_bytes
                )
                if edited:
                    return edited
                logger.info("[memory_diary] 云端参考图生成失败，改用纯文字重试一次")

        # 第一发只带 OpenAI 官方 images API 认识的字段（model/prompt/n/size）。
        # 不带 seed（OpenAI 不收，严格实现会 400）；不带 response_format
        # （gpt-image-1 不支持；不带时多数服务返回 url，我们照样能下载）。
        payload = {"prompt": prompt, "n": 1, "size": size_text}
        if cfg["image_model"]:
            payload["model"] = cfg["image_model"]
        resp = await self._http_post_json(
            url, payload, self._image_cfg()["image_timeout"], headers=headers or None
        )
        data = await self._cloud_image_from_response(resp)
        if data:
            return data
        # 兼容只认 image_size / batch_size 的 OpenAI 兼容服务商（如部分硅基流动模型）
        payload.pop("size", None)
        payload["image_size"] = size_text
        payload["batch_size"] = 1
        resp = await self._http_post_json(
            url, payload, self._image_cfg()["image_timeout"], headers=headers or None
        )
        return await self._cloud_image_from_response(resp)

    @staticmethod
    def _is_local_api_base(base: str) -> bool:
        """云端地址是否指向本机/内网（这类自建 OpenAI 兼容服务常不校验 key）。"""
        try:
            from ipaddress import ip_address
            from urllib.parse import urlparse

            host = (urlparse(base).hostname or "").strip().lower()
        except Exception:
            return False
        if not host or host in ("localhost", "::1"):
            return bool(host)
        try:
            ip = ip_address(host)
        except ValueError:
            return host.endswith(".local")
        return ip.is_private or ip.is_loopback

    async def _cloud_image_from_response(self, resp):
        """从 images 响应里取图：b64 直接解，url 则下载；都没有返回 None。"""
        if not isinstance(resp, dict):
            return None
        items = resp.get("data")
        if not isinstance(items, list):
            items = resp.get("images")
        if not isinstance(items, list) or not items:
            return None
        item = items[0]
        if isinstance(item, str):
            if item.startswith("http://") or item.startswith("https://"):
                return await self._http_get(item, 60)
            return _decode_b64(item)
        if not isinstance(item, dict):
            return None
        for key in ("b64_json", "image", "base64", "b64"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return _decode_b64(val)
        url = item.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return await self._http_get(url, 60)
        return None

    async def _cloud_edit(self, cfg: dict, url: str, headers: dict, prompt: str,
                          size_text: str, ref_bytes: bytes) -> bytes | None:
        """OpenAI images/edits：带一张参考图做垫图（gpt-image-1 等支持时）。"""
        edit_url = url.replace("/images/generations", "/images/edits")
        if edit_url == url:
            return None
        files = {"image": ("ref.png", ref_bytes, "image/png")}
        form = {
            "prompt": prompt,
            "size": size_text,
            "n": "1",
        }
        if cfg["image_model"]:
            form["model"] = cfg["image_model"]
        resp = await self._http_post_multipart(
            edit_url, form, files, 120, headers=headers or None
        )
        if not isinstance(resp, dict):
            return None
        return await self._cloud_image_from_response(resp)

    @staticmethod
    def _cloud_endpoint(base: str, kind: str) -> str:
        """拼 OpenAI 兼容端点：兼容 base 带/不带 /v1 以及直接给全路径的写法。"""
        base = base.rstrip("/")
        if base.endswith(f"/images/{kind}"):
            return base
        if base.endswith("/images"):
            return f"{base}/{kind}"
        if base.endswith("/v1"):
            return f"{base}/images/{kind}"
        return f"{base}/v1/images/{kind}"

    @staticmethod
    def _extract_cloud_image(resp) -> bytes | None:
        """从 images API 响应里取图片字节（b64_json 或 url 两种返回都兼容）。"""
        if not isinstance(resp, dict):
            return None
        items = resp.get("data")
        if not isinstance(items, list):
            items = resp.get("images")
        if not isinstance(items, list) or not items:
            return None
        item = items[0]
        if isinstance(item, str):
            b64 = item
            return _decode_b64(b64)
        if not isinstance(item, dict):
            return None
        for key in ("b64_json", "image", "base64", "b64"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return _decode_b64(val)
        return None

    # ---- 参考图与 seed ----

    @staticmethod
    def _read_ref_bytes(path: str) -> bytes:
        """读本地参考图文件；不存在/不可读/非图返回 b""（不抛）。"""
        try:
            p = Path(str(path)).expanduser()
            if not p.is_file():
                return b""
            data = p.read_bytes()
            if not _looks_like_image(data):
                logger.warning(f"[memory_diary] 参考图 {path} 不是可识别的图片")
                return b""
            return data
        except Exception as e:
            logger.warning(f"[memory_diary] 读取参考图失败（{path}）: {e}")
            return b""

    @staticmethod
    def _read_ref_b64(path: str) -> str:
        data = ImagingMixin._read_ref_bytes(path)
        return base64.b64encode(data).decode("ascii") if data else ""

    def _image_seed(self) -> int:
        """固定 seed 范围：以形象档案哈希为基准 + 小抖动，兼顾一致性与变化。

        设计 4.2：云端无参考图能力时退化为"文字形象 + 固定 seed 范围"。
        """
        cfg = self._image_cfg()
        base_text = (cfg["persona_image_desc"] or cfg["image_style_prompt"] or "memory_diary")
        try:
            import hashlib

            digest = hashlib.sha256(base_text.encode("utf-8")).hexdigest()
            base = int(digest[:8], 16) % 900_000
        except Exception:
            base = 0
        return base + secrets.randbelow(100_000)

    @staticmethod
    def _decode_b64_image(value) -> bytes | None:
        return _decode_b64(value)

    # ---- 可选视觉接口（设计 7.4：配置后 AI 才能写出真实画面描述）----

    async def caption_image(self, data: bytes) -> str:
        """调可选视觉接口给一张用户照片写一句客观画面描述；未配置/失败返回 ""。

        只返回"看得见的内容"，不猜背景故事——没配置视觉接口时调用方保持
        "内容未识别"的诚实降级（绝不让文本模型臆断画面）。
        隐私：照片只发往管理员显式配置的视觉接口（vision_api_*）。
        """
        cfg = self._image_cfg()
        base = str(cfg.get("vision_api_base") or "").strip().rstrip("/")
        model = str(cfg.get("vision_api_model") or "").strip()
        if not base or not model or not data:
            return ""
        # 常见的 OpenAI 兼容多模态端点：/chat/completions
        if base.endswith("/chat/completions"):
            url = base
        elif base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"
        headers = {}
        key = str(cfg.get("vision_api_key") or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        b64 = base64.b64encode(bytes(data)).decode("ascii")
        mime = sniff_image_mime(data)
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "请用一句不超过 30 字的中文客观描述这张图片里"
                                "看得见的内容；不要猜背景故事、不要写评价。"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                }
            ],
            "max_tokens": 120,
        }
        try:
            resp = await self._http_post_json(
                url, payload, min(60, cfg["image_timeout"]), headers=headers or None
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 视觉接口调用失败: {e}")
            return ""
        text = ""
        if isinstance(resp, dict):
            choices = resp.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                content = message.get("content") if isinstance(message, dict) else ""
                if isinstance(content, list):
                    text = "".join(
                        str(p.get("text", ""))
                        for p in content
                        if isinstance(p, dict)
                    )
                elif isinstance(content, str):
                    text = content
        text = " ".join(str(text or "").split()).strip().strip("\"'“”「」")
        if not text:
            return ""
        logger.info("[memory_diary] 已用视觉接口识别一张用户照片")
        return text[:60]

    # ---- 每日用量（全局兜底 image_limit_daily）----

    def _image_usage_path(self) -> Path:
        return self.data_dir / self.IMAGE_USAGE_FILE

    def _image_usage_load(self) -> dict:
        """读取今日用量；跨天自动归零。文件坏掉按 0 处理（不阻塞生图）。"""
        today = self._now().strftime("%Y%m%d")
        try:
            raw = self._image_usage_path().read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("date") == today:
                return {"date": today, "count": max(0, int(data.get("count", 0) or 0))}
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"[memory_diary] 读取插图用量失败，按 0 处理: {e}")
        return {"date": today, "count": 0}

    def _image_usage_remaining(self) -> int:
        """今日还能画几张；image_limit_daily<=0 表示今天不画（字面语义）。"""
        cfg = self._image_cfg()
        limit = cfg["image_limit_daily"]
        if limit <= 0:
            return 0
        return max(0, limit - self._image_usage_load()["count"])

    def _image_usage_consume(self) -> bool:
        """占用今日一张额度；额度用完/每日上限为 0 时返回 False。成功时原子写文件。"""
        cfg = self._image_cfg()
        usage = self._image_usage_load()
        limit = cfg["image_limit_daily"]
        if limit <= 0 or usage["count"] >= limit:
            return False
        usage["count"] += 1
        try:
            p = self._image_usage_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f"{p.name}.tmp-{secrets.token_hex(4)}")
            tmp.write_text(json.dumps(usage, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except Exception as e:
            # 记账失败不阻塞生图（今天可能多画几张，但宁可多画不可崩）
            logger.warning(f"[memory_diary] 插图用量记账失败（本次照常生成）: {e}")
        return True

    def _image_usage_refund(self) -> None:
        """生成失败时退还今日额度（配置页承诺"生成失败不计数"）。失败只记日志。"""
        usage = self._image_usage_load()
        if usage["count"] <= 0:
            return
        usage["count"] -= 1
        try:
            p = self._image_usage_path()
            tmp = p.with_name(f"{p.name}.tmp-{secrets.token_hex(4)}")
            tmp.write_text(json.dumps(usage, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except Exception as e:
            logger.warning(f"[memory_diary] 插图用量退还失败（下次重载后按整数计）: {e}")

    # ---- HTTP 注入点（测试 monkeypatch 这三个方法即可，不联网）----

    def _httpx_client(self, timeout: float):
        import httpx  # 运行时才 import：无 httpx 环境只影响生图，不影响插件加载

        return httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def _http_get(self, url: str, timeout: float, headers=None):
        """HTTP GET → bytes；失败/非 200 返回 None（绝不抛）。"""
        try:
            async with self._httpx_client(timeout) as client:
                resp = await client.get(url, headers=headers or None)
                if resp.status_code < 200 or resp.status_code >= 300:
                    logger.warning(
                        f"[memory_diary] 生图通道 GET {url} 返回 {resp.status_code}"
                    )
                    return None
                return resp.content
        except Exception as e:
            logger.warning(f"[memory_diary] 生图通道 GET {url} 失败: {e}")
            return None

    async def _http_post_json(self, url: str, payload: dict, timeout: float,
                              headers=None):
        """HTTP POST JSON → dict；失败/非 200/非 JSON 返回 None（绝不抛）。"""
        try:
            async with self._httpx_client(timeout) as client:
                resp = await client.post(url, json=payload, headers=headers or None)
                if resp.status_code < 200 or resp.status_code >= 300:
                    logger.warning(
                        f"[memory_diary] 生图通道 POST {url} 返回 "
                        f"{resp.status_code}: {resp.text[:200]}"
                    )
                    return None
                try:
                    data = resp.json()
                except Exception:
                    return None
                return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning(f"[memory_diary] 生图通道 POST {url} 失败: {e}")
            return None

    async def _http_post_multipart(self, url: str, form: dict, files: dict,
                                   timeout: float, headers=None):
        """HTTP POST multipart → dict；失败返回 None（绝不抛）。"""
        try:
            async with self._httpx_client(timeout) as client:
                resp = await client.post(
                    url, data=form, files=files, headers=headers or None
                )
                if resp.status_code < 200 or resp.status_code >= 300:
                    logger.warning(
                        f"[memory_diary] 生图通道 multipart {url} 返回 "
                        f"{resp.status_code}"
                    )
                    return None
                try:
                    data = resp.json()
                except Exception:
                    return None
                return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning(f"[memory_diary] 生图通道 multipart {url} 失败: {e}")
            return None


def _decode_b64(value) -> bytes | None:
    """解 base64（兼容 data URL 前缀与 URL-safe 变体），非法返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("data:"):
        comma = text.find(",")
        if comma < 0:
            return None
        text = text[comma + 1:]
    try:
        return base64.b64decode(text, validate=False)
    except Exception:
        return None


def _quote(raw: str) -> str:
    """URL query 值编码（只保留常见安全字符）。"""
    from urllib.parse import quote

    return quote(raw, safe="")
