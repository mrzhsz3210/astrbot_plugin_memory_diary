# -*- coding: utf-8 -*-
"""Load-path tests: both plugins must import and register via real AstrBot API."""

from __future__ import annotations

import inspect
from pathlib import Path

from harness import compare, env, loader
from harness.errors import SkipTest
from harness.world import DIARY_TEXT, World

from astrbot.api.star import Star
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import star_handlers_registry


def _require_both():
    if env.is_baseline_only():
        raise SkipTest("requires both baseline and refactor import")


def _metadata_yaml(path: Path) -> dict:
    data = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        if value.strip():
            data[key.strip()] = value.strip().strip('"')
    return data


def test_both_packages_load_in_separate_namespaces():
    _require_both()
    base = loader.get_module(env.SIDE_BASELINE)
    new = loader.get_module(env.SIDE_REFACTOR)
    assert base.__name__ == loader.module_name(env.SIDE_BASELINE)
    assert new.__name__ == loader.module_name(env.SIDE_REFACTOR)
    assert base.__name__ != new.__name__
    assert base.MemoryDiaryPlugin is not new.MemoryDiaryPlugin

    for side, module in ((env.SIDE_BASELINE, base), (env.SIDE_REFACTOR, new)):
        meta = star_map.get(module.__name__)
        assert meta is not None, f"{side}: @register did not populate star_map"
        assert meta.name == "astrbot_plugin_memory_diary"
        assert meta.author == "mrzhsz3210"
        # baseline 是重构前的 v0.5.0 快照，refactor 的版本号随 metadata.yaml 升级；
        # 两边各自与自己的 metadata.yaml 一致由 test_metadata_yaml_matches_registration 保证
        assert meta.version.startswith("v0."), f"{side}: {meta.version}"
        assert meta.module_path == module.__name__
        assert issubclass(module.MemoryDiaryPlugin, Star)
        assert module.MemoryDiaryPlugin.__module__ == module.__name__

    # The two registrations must not clobber each other.
    assert star_map[base.__name__].star_cls_type is base.MemoryDiaryPlugin
    assert star_map[new.__name__].star_cls_type is new.MemoryDiaryPlugin


def test_handlers_registered_with_correct_module_paths():
    _require_both()
    base = loader.get_module(env.SIDE_BASELINE)
    new = loader.get_module(env.SIDE_REFACTOR)
    expected_handlers = {
        "diary_help",
        "memo_cmd",
        "memo_view",
        "memo_delete",
        "write_diary_cmd",
        "view_diary",
        "delete_diary",
        "export_diary",
        "import_diary",
        "diary_password",
        "diary_status",
        "diary_scope",
        "diary_inject",
        "on_any_message",
        "on_llm_request",
    }
    for module in (base, new):
        handlers = [
            h
            for h in star_handlers_registry
            if h.handler_module_path == module.__name__
        ]
        names = {h.handler_name for h in handlers}
        assert expected_handlers <= names, f"missing handlers: {expected_handlers - names}"
        for handler in handlers:
            assert handler.handler_module_path == module.__name__

    # Command handlers are async generators (AstrBot invokes them this way).
    for module in (base, new):
        cls = module.MemoryDiaryPlugin
        for cmd in (
            "diary_help",
            "memo_cmd",
            "write_diary_cmd",
            "view_diary",
            "delete_diary",
            "export_diary",
            "import_diary",
            "diary_password",
            "diary_status",
            "diary_scope",
            "diary_inject",
        ):
            assert inspect.isasyncgenfunction(
                getattr(cls, cmd)
            ), f"{module.__name__}.{cmd} is not an async generator"
        assert inspect.iscoroutinefunction(cls.on_llm_request)
        assert inspect.iscoroutinefunction(cls.on_any_message)


def test_metadata_yaml_matches_registration():
    _require_both()
    for side, src in env.plugin_sources().items():
        module = loader.get_module(side)
        meta = star_map[module.__name__]
        yaml_meta = _metadata_yaml(src / "metadata.yaml")
        assert meta.name == yaml_meta["name"]
        assert meta.version == yaml_meta["version"]
        assert meta.author == yaml_meta["author"]


async def test_instantiation_uses_star_tools_data_dir_and_registers_web_apis():
    _require_both()
    async with World() as world:
        base, new = world.baseline, world.refactor
        assert base.data_dir != new.data_dir
        assert base.data_dir.is_dir() and new.data_dir.is_dir()
        for inst in (base, new):
            assert str(inst.data_dir).endswith("data\\plugin_data\\astrbot_plugin_memory_diary")
            assert inst.ctx.registered_web_apis, "web APIs were not registered"
        routes = sorted(r[0] for r in base.ctx.registered_web_apis)
        routes_new = sorted(r[0] for r in new.ctx.registered_web_apis)
        # v0.5.5 新增「一键备份」路由，baseline 侧补齐后再做差分比较
        routes += [
            "/astrbot_plugin_memory_diary/export_all",
            "/astrbot_plugin_memory_diary/export_all_local",
        ]
        compare.assert_equal(sorted(routes), routes_new, "web API routes")
        handler_names = sorted(r[1].__name__ for r in base.ctx.registered_web_apis)
        # v0.5.5 新增 web_export_all / web_export_all_local handler
        handler_names += ["web_export_all", "web_export_all_local"]
        compare.assert_equal(
            sorted(handler_names),
            sorted(r[1].__name__ for r in new.ctx.registered_web_apis),
            "web API handlers",
        )
        # Real handler objects (bound methods of the instance), not stubs.
        for route, handler, methods, desc in base.ctx.registered_web_apis:
            assert inspect.iscoroutinefunction(handler)
            assert methods in (["GET"], ["POST"])


async def test_seeded_signing_key_is_byte_identical():
    _require_both()
    async with World() as world:
        key_b = (world.baseline.data_dir / ".diary_key").read_bytes()
        key_n = (world.refactor.data_dir / ".diary_key").read_bytes()
        compare.assert_equal(key_b, key_n, "seeded .diary_key")
        assert world.baseline.plugin._secret_key() == key_b


async def test_roundtrip_provider_scripted_smoke():
    """Sanity: the fake provider drives a real first diary write on both sides."""
    async with World() as world:
        async def scenario(inst):
            inst.set_history([{"role": "user", "content": "你好呀"}])
            inst.provider.replies[:] = [DIARY_TEXT]
            text, err = await inst.plugin._write_diary(
                inst.default_umo(), "10001", "阿罗", requested_mode="auto"
            )
            return {
                "text": text,
                "err": err,
                "entries": inst.entries(),
                "content": inst.latest_entry()[0],
                "calls": len(inst.provider.calls),
            }

        await world.run_pair(scenario, label="smoke diary write")


def test_on_llm_response_conditional_registration():
    """P2-5：`on_llm_response` 必须条件注册，老 AstrBot 4.x 缺该钩子也能加载。

    模块导入期硬用 `@filter.on_llm_response()` 时，缺少该装饰器的老 4.x 会在
    import 阶段 AttributeError、整个插件加载失败。这里同时钉住三点：
    1) 类方法体上不再挂裸装饰器（否则条件注册可能被改回去）；
    2) 注册入口是 `hasattr(filter, "on_llm_response")` 判断；
    3) 在当前有该钩子的 AstrBot 上，refactor 侧确实把 handler 注册进了
       star_handlers_registry，且方法仍是可 await 的协程。
    """
    if env.is_baseline_only():
        return  # refactor 侧专属约定

    import ast

    source = (env.REFACTOR_DIR / "main.py").read_text(encoding="utf-8")
    assert 'hasattr(filter, "on_llm_response")' in source, (
        "on_llm_response 应采用 hasattr 条件注册（P2-5）"
    )
    tree = ast.parse(source)
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MemoryDiaryPlugin"
    )
    method = next(
        node
        for node in cls.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "on_llm_response"
    )
    assert not method.decorator_list, (
        "on_llm_response 不能再挂模块导入期的裸装饰器（老 4.x 会直接加载失败）"
    )

    module = loader.get_module(env.SIDE_REFACTOR)
    handlers = [
        handler
        for handler in star_handlers_registry
        if handler.handler_module_path == module.__name__
        and handler.handler_name == "on_llm_response"
    ]
    assert len(handlers) == 1, "当前 AstrBot 有 on_llm_response 钩子，必须注册"
    assert handlers[0].event_type.name == "OnLLMResponseEvent"
    assert inspect.iscoroutinefunction(module.MemoryDiaryPlugin.on_llm_response)
