# -*- coding: utf-8 -*-
"""日记本视图的静态回归检查。

WebUI 的插件视图跑在 sandbox="allow-scripts allow-forms allow-downloads" 的
iframe 里，没有 ``allow-modals``，原生 ``window.prompt`` / ``confirm`` / ``alert``
会被浏览器直接忽略（返回 null），所以查看密码必须由页面自己提供输入框。
这里把该约定固定下来，避免以后又退回原生弹窗、导致「设了密码却没有任何输入界面」。
"""

from __future__ import annotations

from harness import env

VIEW_DIR = env.REFACTOR_DIR / "views" / "diary-book"


def _read(name: str) -> str:
    return (VIEW_DIR / name).read_text(encoding="utf-8")


def test_diary_book_view_no_native_prompt():
    js = _read("app.js")
    assert "prompt(" not in js, (
        "视图不能依赖原生 prompt：插件视图 iframe 的 sandbox 没有 allow-modals"
    )
    assert "askDialog(" in js, "页面内输入对话框 askDialog 缺失"


def test_diary_book_view_ships_password_dialog_markup():
    html = _read("index.html")
    for marker in (
        'id="btnLock"',
        'id="dialogMask"',
        'id="dialogInput"',
        'type="password"',
        'id="dialogOk"',
        'id="dialogCancel"',
    ):
        assert marker in html, f"index.html 缺少 {marker}"
    css = _read("style.css")
    assert ".dialog-mask" in css
    assert ".dialog-input" in css


def test_diary_book_view_handles_both_password_gates():
    js = _read("app.js")
    # 翻阅（need_password 响应）与导出（PASSWORD_REQUIRED / PASSWORD_WRONG 错误）
    # 两条路径都必须走页面内对话框，不能静默失败
    assert "need_password" in js
    assert "PASSWORD_REQUIRED" in js
    assert "PASSWORD_WRONG" in js
    assert "btnLock" in js
