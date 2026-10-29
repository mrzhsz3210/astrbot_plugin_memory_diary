# -*- coding: utf-8 -*-
"""插件合规静态检查。

AstrBot 插件规范要求统一使用 ``astrbot.api.logger``，禁止使用标准库 ``logging``。
本文件固定两条约定，避免以后又写回 stdlib logging：

* 插件源码里不出现 ``import logging`` / ``from logging import ...``；
* 无 AstrBot 时 ``memory_diary/log.py`` 仍能导入（静默 logger），且不 import logging。
"""

from __future__ import annotations

import builtins

from harness import env

PLUGIN_ROOT = env.REFACTOR_DIR


def _plugin_python_files():
    for path in sorted(PLUGIN_ROOT.rglob("*.py")):
        rel = path.relative_to(PLUGIN_ROOT)
        if "__pycache__" in rel.parts:
            continue
        # 发布形态下 REFACTOR_DIR 就是仓库根目录，跳过 tests/ baseline/ 等仓库脚手架
        if rel.parts and rel.parts[0] in env.NON_PLUGIN_NAMES:
            continue
        yield path, rel


def test_plugin_sources_never_import_stdlib_logging():
    """插件源码不得使用标准库 logging（平台规则要求统一用 astrbot.api.logger）。"""
    offenders = []
    for path, rel in _plugin_python_files():
        text = path.read_text(encoding="utf-8")
        if "import logging" in text or "from logging import" in text:
            offenders.append(rel.as_posix())
    assert not offenders, f"插件源码不得使用标准库 logging：{offenders}"


def test_log_compat_layer_falls_back_without_astrbot_and_without_logging():
    """无 AstrBot 环境时 log.py 走静默兜底：不 import logging，方法可调用。"""
    source = (PLUGIN_ROOT / "memory_diary" / "log.py").read_text(encoding="utf-8")
    namespace = {"__name__": "memory_diary_log_probe"}
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "astrbot" or name.startswith("astrbot."):
            raise ImportError("blocked on purpose: simulate running without AstrBot")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = blocked_import
    try:
        exec(compile(source, "memory_diary/log.py", "exec"), namespace)
    finally:
        builtins.__import__ = real_import

    assert "logging" not in namespace, "log.py 的兜底分支不应再 import logging"
    logger = namespace["logger"]
    for method in ("debug", "info", "warning", "error", "exception"):
        getattr(logger, method)("compliance probe")


def test_plugin_web_local_export_uses_real_astrbot_client_host():
    """「存到本机并打开位置」的本机判断必须用 AstrBot 真实暴露的 client_host。

    ``astrbot.api.web.PluginRequest`` 只有 ``client_host``（来自
    ``request.client.host``），没有 ``remote_addr``。读错属性在真机上永远得到
    空字符串，会让「只有本机面板可用」的防护形同虚设，远程面板也能把迁移包
    （含签名钥匙）写进服务器主目录并拉起文件管理器。
    """
    source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    assert 'getattr(_web_request, "remote_addr"' not in source, (
        "AstrBot 插件 Web 请求没有 remote_addr，请改用 request.client_host"
    )
    assert "client_host" in source
