# -*- coding: utf-8 -*-
"""Isolated worlds: one baseline instance + one refactored instance per test."""

from __future__ import annotations

import copy
import inspect
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import compare, env, loader, session
from .fakes import FakeContext, FakeConversationManager, FakePersonaManager, FakeProvider

# Frozen wall clock used by every scenario. 2026-10-23 is a Friday.
FIXED_NOW = datetime(2026, 10, 23, 20, 30, 0)

# Seeded byte-identical signing key: makes diary zip names, HMAC signatures and
# 说明.txt content identical across both isolated data directories.
SEEDED_KEY = b"differential-regression-harness-fixed-key-0123456789abcdef"

DIARY_TEXT = (
    "【涵盖时间】\n2026-10-23 下午。\n\n"
    "【用户画像】\n阿罗，喜欢在深夜聊天。\n\n"
    "【我对用户的看法与评价】\n她很真诚。\n\n"
    "【给未来的我的叮嘱】\n记得温柔一点。"
)

BRIEF_TEXT = "【当天简记】\n又聊了几句，她今天心情不错。"
LIFE_TEXT = "今天没有人来找我聊天，我翻了一会儿旧日记。"
SECOND_DAILY = "第二篇对话素材。"

# Defaults from _conf_schema.json; tests override selectively.
DEFAULT_CONFIG = {
    "enable_auto_inject": True,
    "enable_context_watch": True,
    "context_token_limit": 60000,
    "remind_ratio": 0.75,
    "auto_ratio": 0.9,
    "diary_op_interval": 5,
    "write_min_ratio": 0,
    "use_persona": True,
    "enable_auto_diary": False,
    "auto_diary_time": "22:00",
    "empty_diary_as_life": True,
    "show_diary_after_write": False,
    "inject_default_days": 7,
    "inject_max_chars": 8000,
    "group_inject_filter": True,
    "persist_inject": False,
    "max_history_messages": 60,
    "group_history_multiplier": 3,
    "bot_call_names": "",
    "group_require_at": False,
    "extra_instruction": "",
    "max_full_diaries_per_day": 1,
    "web_diary_brand": "TA",
    "diary_timezone": "Asia/Shanghai",
}


def default_config(**overrides) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(overrides)
    return cfg


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def message(role: str, content: str) -> dict:
    return {"role": role, "content": content}


class Instance:
    """One plugin loaded into its own ASTRBOT_ROOT and data directory."""

    def __init__(self, side: str, world: "World"):
        self.side = side
        self.world = world
        self.root = world.tmp / f"{side}_root"
        self.captured_dir = world.tmp / f"{side}_captured"
        self.root.mkdir(parents=True, exist_ok=True)
        self.captured_dir.mkdir(parents=True, exist_ok=True)
        self.now = world.now
        self.config = copy.deepcopy(world.config)
        self.provider = FakeProvider(replies=[world.diary_text, world.second_diary_text])
        self.conversation_manager = FakeConversationManager()
        self.persona_manager = world.persona_manager
        self.module = loader.get_module(side)
        session.install(self.module)
        session.set_replies(self.module, [])
        session.set_supported(self.module, world.session_supported)
        self.ctx = FakeContext(
            provider=self.provider,
            conversation_manager=self.conversation_manager,
            persona_manager=self.persona_manager,
            captured_dir=self.captured_dir,
        )
        self.plugin = self._instantiate()
        self.data_dir = Path(self.plugin.data_dir)
        self.plugin._now = lambda: self.now
        self._seed_key()

    # -- construction -------------------------------------------------
    def _instantiate(self):
        previous = os.environ.get("ASTRBOT_ROOT")
        os.environ["ASTRBOT_ROOT"] = str(self.root)
        try:
            plugin = self.module.MemoryDiaryPlugin(self.ctx, self.config)
        finally:
            if previous is None:
                os.environ.pop("ASTRBOT_ROOT", None)
            else:
                os.environ["ASTRBOT_ROOT"] = previous
        return plugin

    def _seed_key(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key_path = self.data_dir / ".diary_key"
        key_path.write_bytes(SEEDED_KEY)
        self.plugin._key_cache = None

    # -- runtime helpers ----------------------------------------------
    def set_now(self, dt: datetime) -> None:
        self.now = dt
        self.plugin._now = lambda: self.now

    def set_history(self, messages, *, cid: str = "cid-1", persona_id: str = "") -> None:
        umo = self.default_umo()
        self.conversation_manager.set(
            umo, history=messages, cid=cid, persona_id=persona_id
        )

    def default_umo(self) -> str:
        return "aiocqhttp:FriendMessage:10001"

    def set_replies(self, replies) -> None:
        session.set_replies(self.module, replies)

    def set_session_supported(self, ok: bool) -> None:
        session.set_supported(self.module, ok)

    async def run_handler(self, handler_name: str, event):
        handler = getattr(self.plugin, handler_name)
        return await session.collect(handler, event)

    def snapshot(self, **kwargs) -> dict:
        kwargs.setdefault("tmp_root", self.world.tmp)
        return compare.snapshot(self.data_dir, **kwargs)

    def list_data_files(self) -> list[str]:
        if not self.data_dir.exists():
            return []
        return sorted(p.name for p in self.data_dir.iterdir())

    def diary_path(self, sender_id: str = "10001") -> Path:
        return Path(self.plugin._diary_path(sender_id))

    def latest_entry(self, sender_id: str = "10001"):
        return self.plugin._load_latest(sender_id)

    def entries(self, sender_id: str = "10001") -> list:
        return self.plugin._list_entries(sender_id)

    async def initialize(self) -> None:
        await self.plugin.initialize()

    async def terminate(self) -> None:
        try:
            await self.plugin.terminate()
        except Exception:
            pass


@dataclass
class PairResult:
    base: object
    refactor: object
    base_error: object = None
    refactor_error: object = None
    base_snapshot: dict = field(default_factory=dict)
    refactor_snapshot: dict = field(default_factory=dict)

    @property
    def value(self):
        """The scenario value (identical for both sides)."""
        return self.base


class World:
    """Two isolated plugin instances sharing config, clock and history setup."""

    def __init__(
        self,
        config: dict | None = None,
        *,
        now: datetime = FIXED_NOW,
        diary_text: str = DIARY_TEXT,
        second_diary_text: str = SECOND_DAILY,
        persona_prompt: str = "",
        session_supported: bool = True,
    ):
        self.tmp = Path(tempfile.mkdtemp(prefix="diary_harness_world_"))
        self.now = now
        self.diary_text = diary_text
        self.second_diary_text = second_diary_text
        self.persona_manager = (
            FakePersonaManager(default_prompt=persona_prompt) if persona_prompt else None
        )
        self.session_supported = session_supported
        self.config = default_config(**(config or {}))
        # Baseline-only sanity runs (``--baseline-only`` / env var) never import
        # the refactor, so the suite can be used while the working copy is being
        # edited or even temporarily does not import.
        self.baseline_only = os.environ.get("DIARY_HARNESS_BASELINE_ONLY") == "1"
        self.baseline = Instance(env.SIDE_BASELINE, self)
        self._refactor = None

    # -- lifecycle ----------------------------------------------------
    def inst(self, side: str) -> Instance:
        return self.baseline if side == env.SIDE_BASELINE else self.refactor

    @property
    def refactor(self) -> Instance:
        if self._refactor is None:
            self._refactor = Instance(env.SIDE_REFACTOR, self)
        return self._refactor

    @refactor.setter
    def refactor(self, value) -> None:
        self._refactor = value

    @property
    def instances(self) -> list[Instance]:
        insts = [self.baseline]
        if self._refactor is not None:
            insts.append(self._refactor)
        return insts

    async def aclose(self) -> None:
        for inst in self.instances:
            await inst.terminate()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def __aenter__(self) -> "World":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # -- differential runner -----------------------------------------
    async def run_pair(
        self,
        scenario,
        *,
        label: str = "scenario",
        compare_state: bool = True,
        snapshot_normalise=None,
        snapshot_exclude=(),
        compare_value: bool = True,
    ) -> PairResult:
        values = {}
        errors = {}
        sides = [env.SIDE_BASELINE] if self.baseline_only else list(env.SIDES)
        for side in sides:
            inst = self.inst(side)
            try:
                value = scenario(inst)
                if inspect.isawaitable(value):
                    value = await value
                values[side] = self.normalise_value(value, inst)
                errors[side] = None
            except Exception as exc:  # noqa: BLE001 - differential harness
                values[side] = None
                errors[side] = {
                    "error": type(exc).__name__,
                    "message": self.normalise_text(str(exc), inst),
                }
        base_snapshot = {}
        refactor_snapshot = {}
        if compare_state:
            for side in sides:
                inst = self.inst(side)
                snap = compare.snapshot(
                    inst.data_dir,
                    exclude=snapshot_exclude,
                    normalise=snapshot_normalise,
                    tmp_root=self.tmp,
                )
                if side == env.SIDE_BASELINE:
                    base_snapshot = snap
                else:
                    refactor_snapshot = snap
        result = PairResult(
            base=values[env.SIDE_BASELINE],
            refactor=values.get(env.SIDE_REFACTOR, values[env.SIDE_BASELINE]),
            base_error=errors[env.SIDE_BASELINE],
            refactor_error=errors.get(env.SIDE_REFACTOR),
            base_snapshot=base_snapshot,
            refactor_snapshot=refactor_snapshot,
        )
        if compare_value and not self.baseline_only:
            compare.assert_equal(
                {"value": result.base, "error": result.base_error},
                {"value": result.refactor, "error": result.refactor_error},
                f"{label} (baseline vs refactor)",
            )
        if result.base_error is not None:
            # Scenarios are expected to catch the exceptions they want to
            # compare; an uncaught exception is a harness/test bug and must not
            # silently pass just because both sides failed the same way.
            raise compare.DiffError(
                f"{label}: scenario raised on baseline: {result.base_error}"
            )
        if compare_state and not self.baseline_only:
            compare.assert_equal(
                base_snapshot,
                refactor_snapshot,
                f"{label} data directory",
            )
        return result

    # -- normalisation ------------------------------------------------
    def normalise_text(self, text: str, inst: Instance) -> str:
        replacements = [
            (str(inst.data_dir), "<DATA_DIR>"),
            (str(inst.root), "<INSTANCE_ROOT>"),
            (str(inst.captured_dir), "<CAPTURED_DIR>"),
            (str(self.tmp), "<WORLD_TMP>"),
        ]
        out = text
        for needle, marker in replacements:
            out = out.replace(needle, marker)
            out = out.replace(needle.replace("\\", "\\\\"), marker)
        return out

    def normalise_value(self, value, inst: Instance):
        if isinstance(value, str):
            return self.normalise_text(value, inst)
        if isinstance(value, bytes):
            return value
        if isinstance(value, dict):
            return {
                self.normalise_value(k, inst): self.normalise_value(v, inst)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.normalise_value(v, inst) for v in value]
        if isinstance(value, Path):
            return self.normalise_text(str(value), inst)
        return value


def assert_pair(world, scenario, **kwargs) -> PairResult:
    """Convenience wrapper for sync tests (runs the async pair runner)."""
    import asyncio

    return asyncio.run(world.run_pair(scenario, **kwargs))
