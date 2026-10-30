# -*- coding: utf-8 -*-
"""日记本视图的静态回归检查。

WebUI 的插件视图跑在 sandbox="allow-scripts allow-forms allow-downloads" 的
iframe 里，没有 ``allow-modals``，原生 ``window.prompt`` / ``confirm`` / ``alert``
会被浏览器直接忽略（返回 null），所以查看密码必须由页面自己提供输入框。
这里把该约定固定下来，避免以后又退回原生弹窗、导致「设了密码却没有任何输入界面」。
"""

from __future__ import annotations

import re

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


def test_diary_book_view_uses_blob_url_for_large_custom_images():
    """大尺寸自定义图片必须先转成 blob: URL，不能直接放进 CSS 自定义属性。

    Chromium/Edge 会把 CSS 自定义属性值截断到 1,000,000 字符；一张 2MB 图片的
    base64 约 3MB，直接塞进 ``--d-paper-image`` 会被截断成非法 URL，背景画不出来，
    表现成「上传成功、纸的边框消失，但图片没出现」。blob URL 很短，不受此限制。
    """
    js = _read("app.js")
    assert "dataUrlToObjectUrl" in js
    assert "createObjectURL" in js
    assert "revokeObjectURL" in js


def test_diary_book_view_ships_backup_all_entry():
    """P2-2 第 5 点：一键备份全部日记的前端入口必须存在。

    后端路由 ``/export_all`` 有了、前端没有按钮（或按钮没有接到 bridge 调用）
    时，这个功能对用户等于不存在；只断言 refactor 资产。
    """
    html = _read("index.html")
    assert 'data-export="backup_all"' in html, "缺少「备份全部日记」按钮"
    js = _read("app.js")
    assert re.search(r'bridge\.download\(\s*"export_all"', js), (
        "缺少 bridge.download(\"export_all\", ...) 调用"
    )
    # 按钮值必须真的被派发到备份分支（不能只留一个孤立按钮）
    assert '"backup_all"' in js and "doBackupAll" in js

    # 复查补丁：再提供一个「备份全部日记并打开位置」入口（仅本机面板可用）
    assert 'data-export="backup_all_local"' in html, (
        "缺少「备份全部日记并打开位置」按钮"
    )
    assert re.search(r'bridge\.apiGet\(\s*"export_all_local"', js), (
        "缺少 bridge.apiGet(\"export_all_local\") 调用"
    )
    assert "doBackupAllLocal" in js


