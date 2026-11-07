# -*- coding: utf-8 -*-
"""v0.7.0 D 节：暖白主题、偏好持久化与五分钟教程。"""

from __future__ import annotations

import re

from harness import env
from harness.world import World
from test_web import FakeWebRequest, _call


def _rgb(hex_color: str):
    value = hex_color.lstrip("#")
    return [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def _luminance(hex_color: str) -> float:
    channels = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in _rgb(hex_color)
    ]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_warm_theme_is_default_classic_optional_and_aa_readable():
    view = env.REFACTOR_DIR / "views" / "diary-book"
    css = (view / "style.css").read_text(encoding="utf-8")
    html = (view / "index.html").read_text(encoding="utf-8")
    js = (view / "app.js").read_text(encoding="utf-8")
    assert '--paper: #fffdf8' in css
    assert ':root[data-paper-theme="classic"]' in css
    assert 'data-paper-theme="warm"' in html
    assert 'data-paper-theme="classic"' in html
    assert 'void setPaperTheme(item.dataset.paperTheme)' in js
    assert 'savePrefs()' in js
    for ink in ("#403a33", "#6f685e", "#925240"):
        assert _contrast(ink, "#fffdf8") >= 4.5


def test_five_minute_tutorial_is_in_full_and_packaged_guides():
    for name in ("docs/README.md", "admin-guide.md"):
        text = (env.REFACTOR_DIR / name).read_text(encoding="utf-8")
        assert "五分钟上手教程" in text
        assert "/日记 完整" in text
        assert "/reset" in text
        assert "经典黄底" in text


async def test_paper_theme_is_whitelisted_and_persisted_per_account():
    async with World(config={}) as world:
        plugin = world.refactor.plugin
        saved = await _call(
            plugin.web_save_prefs,
            FakeWebRequest(
                username="admin",
                payload={
                    "prefs": {
                        "font_size": 16,
                        "paper_theme": "classic",
                        "unknown": "discard",
                    }
                },
            ),
        )
        assert saved["status"] == 200
        assert saved["body"]["prefs"]["paper_theme"] == "classic"
        assert "unknown" not in saved["body"]["prefs"]

        loaded = await _call(
            plugin.web_get_prefs, FakeWebRequest(username="admin")
        )
        assert loaded["body"]["prefs"]["paper_theme"] == "classic"
        other = await _call(
            plugin.web_get_prefs, FakeWebRequest(username="other")
        )
        assert other["body"]["prefs"] == {}

        invalid = await _call(
            plugin.web_save_prefs,
            FakeWebRequest(
                username="admin", payload={"prefs": {"paper_theme": "unknown"}}
            ),
        )
        assert "paper_theme" not in invalid["body"]["prefs"]


def test_tool_panel_has_complete_collapse_focus_and_accessibility_contract():
    view = env.REFACTOR_DIR / "views" / "diary-book"
    js = (view / "app.js").read_text(encoding="utf-8")
    html = (view / "index.html").read_text(encoding="utf-8")
    css = (view / "style.css").read_text(encoding="utf-8")
    for marker in (
        "let activeTool = null", "function closeTools(", "function syncToolButtons(",
        '$("toolsPanel").hidden = true', '$("searchTools").hidden = true',
        '$("materialTools").hidden = true', "activeTool = null",
        'if (activeTool === kind)', 'e.key === "Escape" && activeTool',
    ):
        assert marker in js, marker
    assert 'id="btnSearch" class="ctl" aria-expanded="false" aria-controls="searchTools"' in html
    assert 'id="btnMaterials" class="ctl" aria-expanded="false" aria-controls="materialTools"' in html
    assert re.search(
        r"\.tools-panel\[hidden\],\s*#searchTools\[hidden\],\s*#materialTools\[hidden\]\s*"
        r"\{\s*display:\s*none;\s*\}", css
    )


def test_theme_setter_save_failure_and_four_combo_contrast_contract():
    view = env.REFACTOR_DIR / "views" / "diary-book"
    js = (view / "app.js").read_text(encoding="utf-8")
    css = (view / "style.css").read_text(encoding="utf-8")
    assert "function setPaperTheme(theme)" in js
    assert "return new Promise((resolve)" in js
    assert "保存失败，刷新后可能恢复" in js
    assert 'item.setAttribute("aria-pressed", String(active))' in js
    assert "void setPaperTheme(item.dataset.paperTheme)" in js
    assert ":root[data-paper-theme=\"classic\"]" in css
    assert ":root[data-theme=\"dark\"][data-paper-theme=\"warm\"]" in css
    assert ":root[data-theme=\"dark\"][data-paper-theme=\"classic\"]" in css
    assert ":not([data-theme=\"dark\"])" not in css
    combinations = {
        "light-warm": ("#fffdf8", "#403a33", "#6f685e", "#925240"),
        "light-classic": ("#fbf6e9", "#4a3f2f", "#74644b", "#a8523f"),
        "dark-warm": ("#2e2a23", "#ddd2ba", "#b9ac90", "#e49a82"),
        "dark-classic": ("#3a2f1f", "#f0dfb8", "#c7b68c", "#e9a06f"),
    }
    for name, (paper, ink, soft, accent) in combinations.items():
        for label, color in (("正文", ink), ("次级", soft), ("强调", accent)):
            assert _contrast(paper, color) >= 4.5, f"{name} {label} 对比度不足"
