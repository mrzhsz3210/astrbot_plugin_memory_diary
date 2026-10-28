# -*- coding: utf-8 -*-
"""Comparison helpers: deterministic value and on-disk-state comparison."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

DATA_DIR_PLACEHOLDER = b"<DATA_DIR>"
TMP_ROOT_PLACEHOLDER = b"<TMP_ROOT>"


class DiffError(AssertionError):
    pass


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
