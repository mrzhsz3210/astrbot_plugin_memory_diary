# -*- coding: utf-8 -*-
"""Environment discovery for the regression harness.

Two source trees are loaded side by side:

* ``baseline``  —— the original single-file implementation, kept untouched under
  ``<repo>/baseline/astrbot_plugin_memory_diary``.
* ``refactor``  —— the current implementation.  In the published repository the
  plugin *is* the repository root, so this side is ``<repo>``.  The internal
  refactor worktree keeps the working copy under
  ``<root>/work/astrbot_plugin_memory_diary``; when that directory exists it is
  used instead.

Nothing here writes to the plugin source trees; packages are copied into a
throw-away temporary directory before import so the two implementations can be
provided with real AstrBot decorator/registration behaviour independently.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# <repo>/tests/harness/env.py -> parents[0]=harness [1]=tests [2]=<repo>
TESTS_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = Path(__file__).resolve().parents[2]

BASELINE_DIR = PLUGIN_ROOT / "baseline" / "astrbot_plugin_memory_diary"
_WORKTREE_REFACTOR_DIR = PLUGIN_ROOT / "work" / "astrbot_plugin_memory_diary"

#: "refactor" side: the internal worktree copy when present, else the repo root.
REFACTOR_DIR = (
    _WORKTREE_REFACTOR_DIR if _WORKTREE_REFACTOR_DIR.is_dir() else PLUGIN_ROOT
)
WORK_DIR = REFACTOR_DIR  # back-compat alias for older notes/scripts

SIDE_BASELINE = "baseline"
SIDE_REFACTOR = "refactor"
SIDES = (SIDE_BASELINE, SIDE_REFACTOR)

# Package names must be valid identifiers and are distinct so relative imports
# inside the refactored copy resolve inside its own namespace.
PACKAGE_NAMES = {
    SIDE_BASELINE: "basepkg",
    SIDE_REFACTOR: "newpkg",
}
PLUGIN_PACKAGE = "astrbot_plugin_memory_diary"

#: Repository scaffolding that is never part of the plugin package itself.
NON_PLUGIN_NAMES = frozenset(
    {
        "tests",
        "baseline",
        "work",
        "tools",
        "dist",
        "__pycache__",
        ".git",
        ".github",
        ".venv",
        "venv",
    }
)


def copytree_ignore(_directory, names):
    """``shutil.copytree`` ignore callback: skip scaffolding and caches."""
    return [
        name
        for name in names
        if name in NON_PLUGIN_NAMES or name.endswith((".pyc", ".pyo"))
    ]


def astrbot_core_path() -> Path | None:
    """Locate the AstrBot source checkout that provides the plugin API.

    Resolution order:

    1. ``ASTRBOT_CORE`` environment variable (explicit, works on any machine);
    2. AstrBot Launcher instance cores in ``~/.astrbot_launcher/instances``;
    3. ``astrbot`` already importable in this Python environment (``None``
       meaning there is nothing to add to ``sys.path``).
    """
    override = os.environ.get("ASTRBOT_CORE", "").strip()
    if override:
        core = Path(override).expanduser()
        if not (core / "astrbot" / "__init__.py").is_file():
            raise RuntimeError(
                f"ASTRBOT_CORE={core} does not look like an AstrBot checkout "
                "(expected <ASTRBOT_CORE>/astrbot/__init__.py)"
            )
        return core

    launcher = Path.home() / ".astrbot_launcher" / "instances"
    if launcher.is_dir():
        for instance in sorted(launcher.iterdir()):
            core = instance / "core"
            if (core / "astrbot" / "__init__.py").is_file():
                return core

    if importlib.util.find_spec("astrbot") is not None:
        return None

    raise RuntimeError(
        "AstrBot source not found. Point ASTRBOT_CORE at an AstrBot checkout "
        "(a directory containing the astrbot/ package), or install AstrBot into "
        "this Python environment. See tests/README.md."
    )


def ensure_astrbot_importable() -> None:
    """Put the AstrBot source checkout on sys.path (idempotent)."""
    core = astrbot_core_path()
    if core is None:
        return
    core_str = str(core)
    if core_str not in sys.path:
        sys.path.insert(0, core_str)


def plugin_sources():
    return {SIDE_BASELINE: BASELINE_DIR, SIDE_REFACTOR: REFACTOR_DIR}


def is_baseline_only() -> bool:
    """True when running sanity checks against the untouched baseline only.

    Set by ``python tests/run.py --baseline-only``; the refactor is then never
    imported, so the suite works while the working copy is mid-edit.
    """
    return os.environ.get("DIARY_HARNESS_BASELINE_ONLY") == "1"
