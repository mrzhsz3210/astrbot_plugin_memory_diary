# -*- coding: utf-8 -*-
"""Standalone test runner (pytest is intentionally not required).

Usage:
    <astrbot venv python> tests/run.py                  # all tests
    <astrbot venv python> tests/run.py -k storage       # substring filter
    <astrbot venv python> tests/run.py -v               # per-test detail
    <astrbot venv python> tests/run.py --list           # list tests

Exit code: 0 when no failures, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import os
import sys
import time
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


from harness.errors import SkipTest  # noqa: E402


def discover() -> list[tuple[str, object]]:
    from harness import loader

    loader  # imported for side effect of path setup when needed
    tests: list[tuple[str, object]] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name == "__init__.py":
            continue
        module_name = path.stem
        module = importlib.import_module(module_name)
        for name, obj in vars(module).items():
            if name.startswith("test_") and inspect.isfunction(obj):
                if obj.__module__ != module_name:
                    continue
                tests.append((f"{module_name}::{name}", obj))
    return tests


def _reset_harness_state() -> None:
    """Give every already-loaded plugin module a fresh fake-reply queue / flags.

    Deliberately does not import the plugin modules: in ``--baseline-only``
    mode the refactor must never be imported, so a broken working copy cannot
    affect the sanity run.
    """
    from harness import loader

    for module in loader.loaded_modules().values():
        module._harness_session_replies = []
        module._SESSION_WAITER_OK = True


def run_one(name: str, fn) -> tuple[str, str | None, str | None]:
    started = time.perf_counter()
    try:
        _reset_harness_state()
        result = fn()
        if inspect.isawaitable(result):
            asyncio.run(result)
        elapsed = time.perf_counter() - started
        return "PASS", None, f"{elapsed:.2f}s"
    except SkipTest as exc:
        return "SKIP", str(exc), None
    except AssertionError:
        return "FAIL", traceback.format_exc(limit=6), None
    except Exception:
        return "FAIL", traceback.format_exc(), None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="memory_diary differential regression harness")
    parser.add_argument("-k", "--filter", default="", help="substring filter on test id")
    parser.add_argument("-v", "--verbose", action="store_true", help="show full failure output")
    parser.add_argument("--list", action="store_true", help="list collected tests and exit")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="sanity-run scenarios against the untouched baseline only (refactor not imported)",
    )
    args = parser.parse_args(argv)

    from harness import env, loader

    if args.baseline_only:
        os.environ["DIARY_HARNESS_BASELINE_ONLY"] = "1"
    env.ensure_astrbot_importable()
    tests = discover()
    if args.filter:
        tests = [(name, fn) for name, fn in tests if args.filter.lower() in name.lower()]
    if args.list:
        for name, _ in tests:
            print(name)
        return 0
    if not tests:
        print("No tests collected.")
        return 1

    mode = "baseline-only sanity mode" if args.baseline_only else "differential baseline vs refactor"
    print(f"Collected {len(tests)} tests from {TESTS_DIR} ({mode})")
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    failures: list[tuple[str, str]] = []
    started = time.perf_counter()
    for name, fn in tests:
        status, detail, timing = run_one(name, fn)
        counts[status] += 1
        suffix = f" ({timing})" if timing else ""
        print(f"[{status}] {name}{suffix}")
        if status == "FAIL":
            failures.append((name, detail or ""))
            if args.verbose:
                print(detail)
            else:
                lines = (detail or "").strip().splitlines()
                print("    " + "\n    ".join(lines[-3:]))
            if args.fail_fast:
                break
        elif status == "SKIP":
            print(f"    reason: {detail}")

    total_time = time.perf_counter() - started
    print("-" * 72)
    print(
        f"{counts['PASS']} passed, {counts['FAIL']} failed, "
        f"{counts['SKIP']} skipped in {total_time:.2f}s"
    )
    if failures:
        print("\nFailures:")
        for name, _ in failures:
            print(f"  - {name}")
    loader.cleanup()
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
