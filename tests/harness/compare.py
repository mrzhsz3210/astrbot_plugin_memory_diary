# -*- coding: utf-8 -*-
"""Comparison helpers: deterministic value and on-disk-state comparison."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

DATA_DIR_PLACEHOLDER = b"<DATA_DIR>"
TMP_ROOT_PLACEHOLDER = b"<TMP_ROOT>"

# v0.5.5 P3-1 差分豁免：日记 zip 里的「说明.txt」不再写死旧四固定标题，改为
# 功能描述（小标题由 AI 按人设自由起名）。baseline 仍是旧句、refactor 是新句，
# 两边 snapshot 逐字节比较前把这一句统一成同一串——与 world.py 的
# ``_INJECT_TEMPLATE_V055_SENTENCE`` 同类处理，只豁免这一句文案本身。
README_V055_OLD_HEADINGS = (
    "1. 涵盖时间  2. 用户画像  3. AI 对用户的看法与评价  4. 给未来的 AI 的叮嘱"
).encode("utf-8")
README_V055_NEW_HEADINGS = (
    "功能上依次覆盖：时间范围 / 用户画像 / 对用户的评价与感受 / "
    "给未来的 AI 的叮嘱（小标题由 AI 按人设自由起名）。"
).encode("utf-8")
# 无句号变体同样归一：句号是否落在这一句末尾取决于实现的换行位置，
# 两种形式都统一到带句号的规范串，避免实现微调换行时又差一个字符。
README_V055_NEW_HEADINGS_NO_PERIOD = README_V055_NEW_HEADINGS[
    : -len("。".encode("utf-8"))
]


class DiffError(AssertionError):
    pass


def _drop_json_field(raw: bytes, field: str) -> bytes:
    """从 JSON 对象字节里删掉一个字段（差分豁免用）；非 JSON 原样返回。"""
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if isinstance(data, dict) and field in data:
        data.pop(field)
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    return raw


def stable_repr(value, _depth: int = 0) -> str:
    """Deterministic repr usable in assertion messages."""
    if _depth > 8:
        return "<deep>"
    if isinstance(value, dict):
        items = ", ".join(
            f"{stable_repr(k, _depth + 1)}: {stable_repr(v, _depth + 1)}"
            for k, v in value.items()
        )
        return "{" + items + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(stable_repr(v, _depth + 1) for v in value) + "]"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes sha256={hashlib.sha256(value).hexdigest()[:16]}>"
    return repr(value)


def _first_difference(a: bytes, b: bytes, width: int = 60) -> str:
    limit = min(len(a), len(b))
    idx = next((i for i in range(limit) if a[i] != b[i]), None)
    if idx is None:
        idx = limit
    lo = max(0, idx - width)
    hi_a = a[lo : idx + width]
    hi_b = b[lo : idx + width]
    return (
        f"first differing byte at {idx} (len {len(a)} vs {len(b)})\n"
        f"  base: {hi_a!r}\n"
        f"  new : {hi_b!r}"
    )


def assert_equal(expected, actual, label: str = "value") -> None:
    if expected == actual:
        return
    if isinstance(expected, bytes) and isinstance(actual, bytes):
        raise DiffError(f"{label} differs byte-for-byte: {_first_difference(expected, actual)}")
    raise DiffError(
        f"{label} differs:\n  baseline = {stable_repr(expected)}\n  refactor = {stable_repr(actual)}"
    )


def normalise_bytes(raw: bytes, data_dir: Path, tmp_root: Path | None = None) -> bytes:
    """Strip machine-specific absolute paths from file contents."""
    text = raw
    for path, marker in (
        (data_dir, DATA_DIR_PLACEHOLDER),
        (tmp_root, TMP_ROOT_PLACEHOLDER),
    ):
        if path is None:
            continue
        needle = str(path).encode("utf-8")
        if needle:
            text = text.replace(needle, marker)
            # JSON escapes backslashes on Windows; cover the escaped form too.
            escaped = str(path).replace("\\", "\\\\").encode("utf-8")
            if escaped != needle:
                text = text.replace(escaped, marker)
    # v0.5.5 P3-1 差分豁免（见模块顶部常量注释）：说明.txt 旧四固定标题 → 功能描述。
    # 普通文件与 zip 条目都走本函数，一处替换即覆盖全部 snapshot 差异。
    text = text.replace(README_V055_OLD_HEADINGS, README_V055_NEW_HEADINGS)
    text = text.replace(
        README_V055_NEW_HEADINGS_NO_PERIOD, README_V055_NEW_HEADINGS
    )
    # v0.5.5 低危清理差分豁免：`last_material_hash` 已被素材游标取代。baseline
    # 仍会把它写进 pref_*.json；refactor 不再写、并在偏好重写时清掉旧字段。
    # 两边比较前统一删掉该键（只在 JSON 里命中，zip 条目不受影响）。
    if b'"last_material_hash"' in text:
        text = _drop_json_field(text, "last_material_hash")
    return text


def snapshot(
    data_dir: Path,
    *,
    exclude=(),
    include=None,
    normalise=None,
    tmp_root: Path | None = None,
) -> dict:
    """Return {relative posix path: sha256(hex)} for deterministic files.

    ``normalise`` is an optional ``(relpath, bytes) -> bytes`` hook for files
    that are intentionally non-deterministic (e.g. PBKDF2 salts).  ``exclude``
    is a list of glob patterns (``fnmatch``) relative to the data directory;
    a matching *directory* prefix (e.g. ``.tmp``) is enough to skip it.
    """
    import fnmatch

    result: dict = {}
    if not data_dir.exists():
        return result
    for path in sorted(data_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(data_dir).as_posix()
        if any(fnmatch.fnmatch(rel, pat) or rel.startswith(pat.rstrip("*")) for pat in exclude):
            continue
        if include is not None and not include(rel):
            continue
        raw = path.read_bytes()
        raw = normalise_bytes(raw, data_dir, tmp_root)
        if normalise is not None:
            raw = normalise(rel, raw)
        if rel.endswith(".zip"):
            result[rel] = _zip_digest(raw, data_dir, tmp_root, normalise)
        else:
            result[rel] = hashlib.sha256(raw).hexdigest()
    return result


def _zip_digest(raw: bytes, data_dir: Path, tmp_root, normalise) -> str:
    """Digest a zip through its normalised entries.

    A zip stores absolute paths in 说明.txt (the storage location line), so two
    byte-identical plugin states still live at different absolute paths.  Hash
    the *normalised* entry payloads + entry names instead.
    """
    import io

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            parts = []
            for name in sorted(z.namelist()):
                payload = normalise_bytes(z.read(name), data_dir, tmp_root)
                if normalise is not None:
                    payload = normalise(f"{name}", payload)
                parts.append(
                    name.encode("utf-8")
                    + b"\x00"
                    + hashlib.sha256(payload).hexdigest().encode("ascii")
                )
            return "zip:" + hashlib.sha256(b"\n".join(parts)).hexdigest()
    except Exception:
        return hashlib.sha256(raw).hexdigest()


def zip_entries(path: Path) -> dict:
    """Return {entry name: normalised bytes} for a diary/bundle zip."""
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in sorted(z.namelist())}


def bundle_manifest(bundle_path: Path) -> dict:
    with zipfile.ZipFile(bundle_path) as z:
        return json.loads(z.read("manifest.json").decode("utf-8"))
