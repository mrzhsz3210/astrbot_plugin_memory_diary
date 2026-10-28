# -*- coding: utf-8 -*-
"""Two-namespace plugin loader.

AstrBot discovers a plugin by importing ``<plugin package>.main`` and letting
the real ``@register`` / ``@filter.*`` decorators populate ``star_map`` and
``star_handlers_registry``.  We reproduce that faithfully:

    <tmp>/basepkg/__init__.py
    <tmp>/basepkg/astrbot_plugin_memory_diary/__init__.py
    <tmp>/basepkg/astrbot_plugin_memory_diary/main.py   (copy of baseline)
    <tmp>/newpkg/...                                    (copy of refactor)

then ``import basepkg.astrbot_plugin_memory_diary.main``.  Relative imports in
the refactored copy keep working and the two modules never collide.
"""

from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
from pathlib import Path

from . import env

_STATE = {
    "package_root": None,
    "modules": {},
}


def _package_root() -> Path:
    if _STATE["package_root"] is not None:
        return _STATE["package_root"]
    env.ensure_astrbot_importable()
    root = Path(tempfile.mkdtemp(prefix="diary_harness_pkgs_"))
    for side, src in env.plugin_sources().items():
        if not src.is_dir():
            raise RuntimeError(f"plugin source for {side!r} not found: {src}")
        pkg = env.PACKAGE_NAMES[side]
        dest = root / pkg / env.PLUGIN_PACKAGE
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, ignore=env.copytree_ignore)
        (root / pkg / "__init__.py").write_text("", encoding="utf-8")
        (dest / "__init__.py").write_text("", encoding="utf-8")
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    _STATE["package_root"] = root
    return root


def get_module(side: str):
    """Import (once) and return the plugin ``main`` module for one side."""
    if side in _STATE["modules"]:
        return _STATE["modules"][side]
    _package_root()
    pkg = env.PACKAGE_NAMES[side]
    module = importlib.import_module(f"{pkg}.{env.PLUGIN_PACKAGE}.main")
    _STATE["modules"][side] = module
    return module


def module_name(side: str) -> str:
    return f"{env.PACKAGE_NAMES[side]}.{env.PLUGIN_PACKAGE}.main"


def loaded_modules() -> dict:
    """Modules imported so far, keyed by side (does not import anything)."""
    return dict(_STATE["modules"])


def package_root() -> Path:
    return _package_root()


def cleanup() -> None:
    """Best-effort removal of the copied package tree (call at end of run)."""
    root = _STATE.pop("package_root", None)
    _STATE["modules"].clear()
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)
