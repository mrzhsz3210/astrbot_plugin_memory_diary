# -*- coding: utf-8 -*-
"""v0.6 D10 WebUI 视图与配置页：app.js 角标/卡片/兜底/警告、样式、schema、README。

依据《批注功能接口契约》第 7、8、9 节与设计方案第二部分 8.3 / D10。
视图是静态资产：这里做 node 语法检查 + 稳定标识的静态断言（不跑浏览器）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile

from harness import env
from harness.errors import SkipTest
from harness.world import World

VIEW_DIR = env.REFACTOR_DIR / "views" / "diary-book"
PACKAGE_ROOT = env.REFACTOR_DIR

EXPECTED_ANNOTATION_CONFIG = {
    "annotation_enabled": ("bool", False),
    "annotation_max_per_round": ("int", 5),
    "annotation_question_hint": ("bool", True),
    "annotation_timeout": ("int", 30),
}
ANNOTATION_KEYS_TAIL = list(EXPECTED_ANNOTATION_CONFIG)

TAMPERED_WARNING = "⚠ 页边批注验签失败，疑似在插件之外被改过，已拒绝显示。"


def _view(name: str) -> str:
    return (VIEW_DIR / name).read_text(encoding="utf-8")


def _load_schema() -> dict:
    with open(
        os.path.join(PACKAGE_ROOT, "_conf_schema.json"), encoding="utf-8-sig"
    ) as f:
        return json.load(f)


def _readme() -> str:
    path = os.path.join(PACKAGE_ROOT, "README.md")
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 语法检查
# ---------------------------------------------------------------------------


def test_diary_book_app_js_passes_node_check():
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    if not node or not os.path.exists(node):
        raise SkipTest("本机没有 node，无法做 app.js 语法检查")
    source = VIEW_DIR / "app.js"
    # app.js 是 ESM（含顶层 await），--check 需要 .mjs 扩展名
    with tempfile.TemporaryDirectory(prefix="ann_js_check_") as tmp:
        target = os.path.join(tmp, "app.mjs")
        shutil.copyfile(source, target)
        proc = subprocess.run(
            [node, "--check", target], capture_output=True, text=True
        )
    assert proc.returncode == 0, (
        f"node --check app.js 失败：\n{proc.stderr or proc.stdout}"
    )


# ---------------------------------------------------------------------------
# app.js：角标 / 卡片 / 兜底 / 验签警告 / 零批注不改 DOM / 转义
# ---------------------------------------------------------------------------


def test_view_appjs_renders_annotation_badges():
    js = _view("app.js")
    # 后端字段：只在有批注时出现 annotations，验签失败时 annotations_tampered
    assert "r.annotations" in js and "r.annotations_tampered" in js
    assert "applyAnnotations(" in js
    assert "applyAnnotations(r.annotations || [], r.annotations_tampered)" in js, (
        "showDay 渲染正文后必须调用 applyAnnotations(..., r.annotations_tampered)"
    )
    # 角标：上标 + 序号 + 命中处插入
    for marker in (
        "ann-badge",
        "ann-mark",
        "annSupNumber",
        "insertAnnMark",
        "findAnnAnchor",
        "createTreeWalker",
    ):
        assert marker in js, f"app.js 缺少角标实现标识 {marker!r}"
    # 只标第一处 / 跳过图片与角标自身
    assert "usedAnchors" in js, "同一锚点出现多次应只标第一处（去重）"
    assert "figure.diary-img" in js and "sup" in js, (
        "锚点定位必须跳过插图 figure 与角标 sup 节点"
    )


def test_view_appjs_annotation_card_two_sections_and_reply_title():
    js = _view("app.js")
    assert "ann-card" in js and "annotationCardHtml" in js and "annotationBodyHtml" in js
    assert "ann-note" in js, "卡片第一区缺少批注原文"
    # 回复区：reply_title 作标题 + summary；推测带「推测：」前缀
    assert "ann-reply-title" in js and "ann-summary" in js and "ann-spec" in js
    assert "推测：" in js, "推测分区必须以「推测：」前缀单独一行"
    # 禁止出现"批注内容"字样标题（设计 8.3 明确）——只看卡片渲染函数体，
    # 不误伤函数上方说明这条设计约束的注释。
    body_fn = js.split("function annotationBodyHtml", 1)[1].split(
        "function annotationCardHtml", 1
    )[0]
    assert "批注内容" not in body_fn, "卡片渲染不得加「批注内容」字样标题"


def test_view_appjs_anchor_mismatch_falls_back_to_margin():
    js = _view("app.js")
    assert "appendAnnMargin" in js and "ann-margin" in js, (
        "锚点失配时必须降级到当天底部的 .ann-margin 页边区，不能丢批注"
    )
    assert "missed" in js, "失配批注应收集后统一放进页边区"
    assert "ann-margin-card" in js


def test_view_appjs_tampered_warning_and_zero_annotation_no_dom_change():
    js = _view("app.js")
    assert TAMPERED_WARNING in js, "缺少验签失败警告条文案"
    assert "ann-tampered" in js
    # 零批注不改 DOM：tampered 处理后、触碰 DOM 前提前 return
    early_return = (
        "if (!Array.isArray(annotations) || annotations.length === 0) return;"
    )
    assert early_return in js, "零批注时必须在改动 DOM 前提前返回"
    tampered_block = js.index('warn.className = "ann-tampered"')
    assert tampered_block < js.index(early_return), (
        "tampered 警告要在零批注提前返回之前处理（否则警告会被跳过）"
    )


def test_view_appjs_escapes_all_llm_text():
    js = _view("app.js")
    assert "function escapeHtml" in js and "function escapeAttr" in js
    # 批注原文 / 回复 / 推测 / 标题 / 锚点都必须走 escape
    assert "escapeHtml(annText(ann && ann.note))" in js, "批注正文必须转义"
    assert "escapeHtml(annText(reply.reply_title))" in js, "回复标题必须转义"
    assert "escapeHtml(annText(reply.summary))" in js, "摘要必须转义"
    assert "escapeHtml(annText(reply.speculation_marked))" in js, "推测必须转义"
    assert "escapeAttr(annText(ann && ann.id))" in js, "data-ann-id 必须走属性转义"
    assert "escapeHtml(anchor)" in js, "页边区锚点文案必须转义"


def test_view_appjs_positions_annotation_card_as_fixed_overlay():
    """卡片是 fixed 悬浮层：打开时按角标位置 / 视口空间落位，滚动后重新钉住。"""
    js = _view("app.js")
    for marker in (
        "positionAnnCard",
        "annCardSize",
        "ANN_CARD_OVERLAP",
        "trackAnnMark",
        "repositionActiveAnnCard",
        "activeAnnMark",
    ):
        assert marker in js, f"app.js 缺少悬浮层定位实现标识 {marker!r}"
    assert 'addEventListener("scroll"' in js, "阅读栏滚动后要按角标新位置重新钉住卡片"
    assert "maxHeight" in js and ".style.left" in js and ".style.top" in js, (
        "卡片坐标与最大高度必须由 JS 写入"
    )
    # 角标序号改用普通数字：⁰¹² 上标字符只有半个字高，放大也看不清
    assert "ANN_SUPERSCRIPT_DIGITS" not in js, "角标序号不再用 ⁰¹² 上标字符"


# ---------------------------------------------------------------------------
# style.css
# ---------------------------------------------------------------------------


def test_view_css_annotation_styles():
    css = _view("style.css")
    for selector in (
        ".ann-badge",
        ".ann-mark",
        ".ann-card",
        ".ann-note",
        ".ann-reply",
        ".ann-reply-title",
        ".ann-summary",
        ".ann-spec",
        ".ann-margin",
        ".ann-margin-card",
        ".ann-tampered",
    ):
        assert selector in css, f"style.css 缺少 {selector}"
    assert "position: relative" in css, "style.css 缺少相对定位（页面 / 菜单等布局依赖）"
    # 卡片默认隐藏，hover / 点按 .open 才显示；触屏有命中区
    assert ".ann-mark:hover .ann-card" in css and ".ann-mark.open .ann-card" in css, (
        "卡片应由 :hover 与 .open 控制显隐"
    )
    assert "display: none" in css and "display: block" in css
    assert "max-width" in css, ".ann-card 窄屏不能撑破活页本"


def test_view_css_annotation_card_floats_above_binder():
    """长批注卡片曾被书页下缘裁掉一角（看着像被活页本盖住）。

    修复：卡片改成 position: fixed 的悬浮层，浮在活页本之上；同时书本投影
    从 filter: drop-shadow 换成 box-shadow —— filter 会给 fixed 子元素创建
    包含块，卡片又会退回被 .page 的 overflow: hidden 裁切。
    """
    css = _view("style.css")
    card_block = css.split(".diary-body .ann-card {", 1)[1].split("}", 1)[0]
    assert "position: fixed" in card_block, "批注卡片必须是 fixed 悬浮层"
    assert "z-index: 60" in card_block, (
        "批注卡片要盖在活页本（.spine z-index: 2）与顶栏（z-index: 10）之上"
    )
    assert "overflow-y: auto" in card_block, "超长批注在卡片内部滚动，不撑出视口"
    book_block = css.split(".book {", 1)[1].split("}", 1)[0]
    assert "filter" not in book_block, (
        ".book 不能用 filter: drop-shadow —— 它会给 fixed 卡片创建包含块，"
        "卡片会被书页 overflow: hidden 重新裁掉；书本投影改用 box-shadow"
    )
    assert "box-shadow: 0 14px 22px var(--shadow)" in book_block, (
        "书本投影改由 box-shadow 承担"
    )


def test_view_css_annotation_badge_is_readable():
    """角标放大并加深色底：v0.6.0 的 0.75em 上标字符太小，用户反馈看不清。"""
    css = _view("style.css")
    badge_block = css.split(".diary-body .ann-badge {", 1)[1].split("}", 1)[0]
    assert "font-size: 0.85em" in badge_block, "角标字号要放大到 0.85em"
    assert "font-weight: 700" in badge_block, "角标要加粗"
    assert "background: var(--badge-bg)" in badge_block, "角标要有底色才看得清"
    assert "--badge-bg" in css and "--badge-bg-strong" in css, "角标底色要随明暗主题变化"


# ---------------------------------------------------------------------------
# 配置页 schema / README / _annotation_cfg 默认值对齐
# ---------------------------------------------------------------------------


def test_conf_schema_annotation_keys_types_defaults_and_conditions():
    schema = _load_schema()
    keys = list(schema.keys())
    # v0.6.0 起「日记时区」「已废弃的启用上下文哨兵」固定在配置页最后两项；
    # 批注配置项紧接插图配置项之后、时区之前，顺序与 README §10.13 一致。
    assert keys[-2:] == ["diary_timezone", "enable_context_watch"], (
        f"配置页最后两项应为日记时区与已废弃兼容项，实际 {keys[-2:]}"
    )
    assert keys[-6:-2] == ANNOTATION_KEYS_TAIL, (
        f"批注配置项应紧跟插图配置项之后（时区之前），顺序为 {ANNOTATION_KEYS_TAIL}，"
        f"实际 {keys[-6:-2]}"
    )
    for key, (expected_type, expected_default) in EXPECTED_ANNOTATION_CONFIG.items():
        item = schema[key]
        assert item.get("type") == expected_type, (
            f"{key} 的 type 应为 {expected_type}，实际 {item.get('type')!r}"
        )
        assert item.get("default") == expected_default, (
            f"{key} 的 default 应为 {expected_default!r}，实际 {item.get('default')!r}"
        )
        assert item.get("description", "").strip(), f"{key} 缺少 description"
        assert item.get("hint", "").strip(), f"{key} 缺少 hint"
    assert "condition" not in schema["annotation_enabled"], "总开关不应有显示条件"
    for key in ("annotation_max_per_round", "annotation_question_hint", "annotation_timeout"):
        condition = schema[key].get("condition") or {}
        assert condition.get("annotation_enabled") is True, (
            f"{key} 缺少 condition.annotation_enabled=true：{condition!r}"
        )
    slider = schema["annotation_max_per_round"].get("slider") or {}
    assert (slider.get("min"), slider.get("max")) == (1, 5)
    # 默认关闭（设计拍板）：管理员必须显式开启
    assert schema["annotation_enabled"]["default"] is False


def test_annotation_cfg_defaults_match_conf_schema_defaults():
    schema = _load_schema()
    defaults = {key: item["default"] for key, item in schema.items()}

    async def _run():
        async with World(config=defaults) as world:
            return world.refactor.plugin._annotation_cfg()

    cfg = asyncio.run(_run())
    assert cfg["annotation_enabled"] is False
    assert cfg["annotation_max_per_round"] == 5
    assert cfg["annotation_question_hint"] is True
    assert cfg["annotation_timeout"] == 30


def test_readme_has_annotation_commands_and_section():
    readme = _readme()
    # §7.1 指令表两行
    assert "`/查看批注`" in readme, "README §7.1 缺少 /查看批注"
    assert "`/写回批注`" in readme, "README §7.1 缺少 /写回批注"
    # §10.13 批注配置章节 + 四个 bullet（顺序与 schema 一致）
    assert "### 10.13 批注配置（v0.6）" in readme
    start = readme.index("### 10.13 批注配置（v0.6）")
    end = readme.index("### 10.14 多用户与隐私")
    section = readme[start:end]
    # 只统计 10.13 正文的配置 bullet；10.13.1 等子章节里的说明性 bullet 不算配置项
    subsection = section.find("\n#### ")
    if subsection != -1:
        section = section[:subsection]
    expected_prefixes = [
        "批注功能总开关",
        "每轮最多生成几条批注",
        "疑问句软引导",
        "批注生成超时",
    ]
    found = []
    for line in section.splitlines():
        if line.startswith("- **"):
            label = line[4:].split("**", 1)[0]
            found.append(label)
    assert len(found) == 4, f"§10.13 应有 4 个配置 bullet，实际 {found!r}"
    for label, prefix in zip(found, expected_prefixes):
        assert label.startswith(prefix), (
            f"§10.13 bullet 顺序/label 不符：{label!r}（应以 {prefix!r} 开头）"
        )
    # 批注与插图解耦的显式说明（隐私/工程红线）在功能章 §9
    assert "不触发插图" in readme
