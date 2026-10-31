# -*- coding: utf-8 -*-
"""v0.6 插图测试共用工具（不是测试模块，run.py 不会收集它）。

只放三件事：
1. 假图片字节 / data URL 构造；
2. zip 读取与原地重写（模拟"某人换了 zip 里的图"）；
3. 报告用的 assert_raises（runner 不是 pytest，没有 pytest.raises）。
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import os
import tempfile
import time
import zipfile
from pathlib import Path

# 只认魔数的最小"图片"：storage/imaging 的嗅探都只看头部字节，
# 不需要真正可解码的图片（测试不联网、不引第三方图像库）。
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"v0.6-test-png" + b"\x00" * 8
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"v0.6-test-jpg" + b"\xff\xd9"
WEBP = b"RIFF\x40\x00\x00\x00WEBPVP8 " + b"v0.6-test-webp"
GIF = b"GIF89a" + b"v0.6-test-gif"


def png(tag: int = 0, size: int = 0) -> bytes:
    """内容不同（tag 不同）的 PNG 假图；size>0 时补零到指定字节数。"""
    data = PNG + bytes([tag % 256]) * 8
    if size and len(data) < size:
        data = data + b"\x00" * (size - len(data))
    return data


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def data_url(data: bytes = PNG, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{b64(data)}"


def img_config(**overrides) -> dict:
    """v0.6 插图配置的默认档位（对应契约第 7 节），测试按需覆盖。"""
    cfg = {
        "image_enabled": True,
        "image_mode": "local",
        "image_local_url": "http://127.0.0.1:7860",
        "image_api_base": "",
        "image_api_key": "",
        "image_model": "",
        "image_prob_memo": 0.0,
        "image_prob_full": 0.3,
        "image_limit_memo": 0,
        "image_limit_full": 2,
        "image_limit_daily": 6,
        "image_limit_group": 0,
        "image_concurrency": 2,
        "image_timeout": 120,
        "persona_image_desc": "",
        "image_style_prompt": "",
        "user_image_enabled": True,
        "user_image_limit": 1,
        "vision_api_base": "",
        "vision_api_key": "",
        "vision_api_model": "",
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# zip 工具
# ---------------------------------------------------------------------------


def zip_entries(path) -> dict:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def write_zip(path, entries: dict) -> None:
    path = Path(path)
    tmp = path.with_name(f"{path.name}.test-{os.getpid()}-{time.time_ns()}")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    os.replace(tmp, path)


def rewrite_zip(path, entries: dict) -> None:
    """覆盖写（用于"拿到数据目录的人改了 zip"的篡改场景）。"""
    write_zip(path, entries)


def replace_zip_entry(path, entry_name: str, data: bytes) -> None:
    entries = zip_entries(path)
    assert entry_name in entries, f"zip 里没有条目 {entry_name}"
    entries[entry_name] = data
    rewrite_zip(path, entries)


def drop_zip_entry(path, entry_name: str) -> None:
    entries = zip_entries(path)
    assert entry_name in entries, f"zip 里没有条目 {entry_name}"
    del entries[entry_name]
    rewrite_zip(path, entries)


def repack_inner_diary_zip(bundle_path, mutate) -> None:
    """对迁移包内层日记 zip 做 mutate(entries) 后重新打包成迁移包。

    bundle = {日记 zip, user_key.txt, manifest.json, 导入说明.txt}；
    这里只重写内层日记 zip，外层清单/钥匙原样保留，用于验证图片验签。
    """
    import io
    import json

    outer = zip_entries(bundle_path)
    manifest = json.loads(outer["manifest.json"].decode("utf-8"))
    inner_name = manifest["diary_file"]
    with zipfile.ZipFile(io.BytesIO(outer[inner_name])) as z:
        inner = {n: z.read(n) for n in z.namelist()}
    mutate(inner)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in inner.items():
            z.writestr(n, data)
    outer[inner_name] = buf.getvalue()
    rewrite_zip(bundle_path, outer)


# ---------------------------------------------------------------------------
# 小型断言工具
# ---------------------------------------------------------------------------


class _Raises:
    def __init__(self, exc_type):
        self.exc_type = exc_type
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"预期抛出 {self.exc_type.__name__}，但没有抛")
        if not issubclass(exc_type, self.exc_type):
            return False  # 让原始异常冒出来，traceback 更有用
        self.value = exc
        return True


def assert_raises(exc_type):
    return _Raises(exc_type)


def assert_status(result, expected_status, label=""):
    """校验 load_image 风格的 (data, status) 返回。"""
    assert isinstance(result, tuple) and len(result) == 2, (
        f"{label}: 预期 (data, status) 二元组，实际 {result!r}"
    )
    data, status = result
    assert status == expected_status, f"{label}: 预期 status={expected_status!r}，实际 {status!r}"
    return data


# ---------------------------------------------------------------------------
# 临时目录（给不用 World 的纯单元测试用）
# ---------------------------------------------------------------------------


class TempDir:
    """with TempDir() as tmp: ... —— 退出时尽力删除。"""

    def __init__(self, prefix: str = "diary_image_test_"):
        self.prefix = prefix
        self.path = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix=self.prefix))
        return self.path

    def __exit__(self, exc_type, exc, tb):
        import shutil

        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)
        return False


def md5_key(raw: str) -> str:
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def import_memory_diary_submodule(name: str):
    """按 refactor 侧的包路径导入 memory_diary.<name>（镜像已装载的临时副本）。

    Lead 还没落地的模块会转成 AssertionError，让单条测试失败而不是整个 run.py
    收集阶段崩溃（这样全量里其它 149 条的结果仍然可读）。
    """
    from harness import env, loader

    main = loader.get_module(env.SIDE_REFACTOR)
    package = main.__name__.rsplit(".", 1)[0]
    try:
        return importlib.import_module(f"{package}.memory_diary.{name}")
    except Exception as exc:  # noqa: BLE001 - 报告用
        raise AssertionError(
            f"memory_diary.{name} 无法导入（契约模块尚未落地或导入失败）：{exc!r}"
        ) from exc
