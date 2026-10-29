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


def test_diary_book_view_reads_image_from_non_envelope_key():
    """自定义纸张/背景板的 base64 必须从 r.image 读，不能读 r.data。

    AstrBot 插件视图 bridge 的响应解包规则是
    ``response.data?.data ?? response.data``：插件 JSON 里顶层出现 data 键时，
    bridge 只把 data 的值交给页面，``{found, mime, ...}`` 全部丢失。
    前端若写 ``r.data``，就会拿到一个字符串、``r.found`` 为 undefined，
    表现成「图片上传成功、后台也存下来了，但页面永远不换」。
    """
    js = _read("app.js")
    assert "r.image" in js, "自定义图片应读取后端字段 image"
    assert "r.data" not in js, (
        "不能读 r.data：顶层 data 会被 bridge 当信封解包，上传成功后也拿不到图片"
    )

