# -*- coding: utf-8 -*-
"""v0.6.2 日记本前端的静态回归检查（审查报告 P1-6 / P2-16~18 / P3-19~22 + P3-7）。

前端没有浏览器测试框架：这里像 ``tests/test_view_assets.py`` 一样 grep
``views/diary-book`` 三件套的关键结构，把本轮修过的约定固定下来，避免以后
又退回旧实现：

* P1-6 下载导出在 ``bridge.download`` 之前主动过密码闸（宿主下载失败只剩通用
  403/400，拿不到 PASSWORD_REQUIRED / OWNER_UNKNOWN 错误码）；
* P3-7 查看密码改短时 token：解锁走 ``POST unlock/<file_key>``，读接口只带
  query ``tk``，不再把 ``pw`` 放进 query；
* P2-16 ``.book[hidden]`` 不被 ``display:flex`` 顶掉 + 空态清正文；
* P2-17 全局方向键排除表单控件、切用户/刷新与翻页互斥；
* P2-18 列表/日期读取失败要 toast + 清旧数据；
* P3-19 批量删除读后端 ``missing``；
* P3-20 owner 解析成功后更新 ``protected``（🔒 按钮）；
* P3-21 插图接口的 need_password / need_owner 不能静默降级成缺图；
* P3-22 导入迁移包按 ``file.size`` 预检 32MB、timeout 提示可能已导入。

行为级验证（headless Edge + 假 bridge）在审查目录 ``_review_v062/frontend/``
的 harness 思路上手工做过，此处只做可重复的静态断言。
"""

from __future__ import annotations

import re

from harness import env

VIEW_DIR = env.REFACTOR_DIR / "views" / "diary-book"


def _read(name: str) -> str:
    return (VIEW_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- P2-16 空态


def test_book_hidden_attribute_not_overridden_by_flex():
    """``.book { display:flex }`` 会顶掉 hidden 属性的 UA 样式：删光用户后
    书仍然可见、旧正文残留。必须像 .paper-menu[hidden] 一样显式补规则。"""
    css = _read("style.css")
    assert re.search(r"\.book\[hidden\]\s*\{\s*display:\s*none;?\s*\}", css), (
        "style.css 缺少 .book[hidden]{display:none}：hidden 会被 .book 的 flex 顶掉"
    )


def test_empty_state_clears_diary_pane():
    """用户列表变空（删光/空目录）时，目录/正文/页眉/页码必须一起清空。"""
    js = _read("app.js")
    assert re.search(r"if\s*\(!has\)\s*\{", js), "loadUsers 缺少 !has 空态分支"
    branch = re.search(r"if\s*\(!has\)\s*\{([\s\S]{0,900}?)\n  \}", js)
    assert branch, "没能定位 loadUsers 的空态分支"
    body = branch.group(1)
    for marker in (
        "days = []",
        "dayIdx = -1",
        'dayList").innerHTML = ""',
        'diaryHeader").innerHTML = ""',
        'diaryBody").textContent = ""',
        'pageInfo").textContent',
    ):
        assert marker in body, f"空态分支缺少清理：{marker}"


# ------------------------------------------------------------- P2-17 方向键


def test_keydown_ignores_form_controls():
    """焦点在 select/input/textarea/contenteditable 里时方向键不能翻页：
    在「选择用户」下拉框上按 ←/→ 会切用户，同时冒泡翻页会让左右两页不同用户。"""
    js = _read("app.js")
    assert re.search(
        r'closest\(\s*"select,\s*input,\s*textarea,\s*\[contenteditable\]"\s*\)',
        js,
    ), "全局 keydown 未排除表单控件（select/input/textarea/[contenteditable]）"


def test_paging_switch_user_mutex():
    """切用户/刷新与翻页要有代次互斥，且翻页请求进行中再翻页不能静默丢弃。"""
    js = _read("app.js")
    assert "let navToken = 0" in js, "缺少切用户/刷新的代次 navToken"
    assert "let pendingDay = null" in js, "缺少翻页排队 pendingDay（loading 期间按键不能丢）"
    assert re.search(r"pendingDay\.nav === navToken", js), (
        "排队翻页必须校验代次：切用户后旧代次的排队请求要丢弃"
    )
    # 连按两次方向键要连续翻页（以在途目标为基准，而不是还没更新的 dayIdx）
    assert "let inflightDay = null" in js, "缺少在途页下标 inflightDay"
    assert "function navigateDay(" in js, "翻页入口应统一走 navigateDay（按钮/方向键共用）"
    assert "navigateDay(-1)" in js and "navigateDay(1)" in js
    # loadDays（切用户/刷新的入口）必须递增代次
    load_days = js[js.index("async function loadDays()") :]
    load_days = load_days[: load_days.index("async function refreshAll(")]
    assert "navToken += 1" in load_days, "loadDays 未递增 navToken"


# -------------------------------------------------------------- P3-7 token


def test_password_uses_short_lived_token_not_query():
    """契约 3：解锁 POST unlock/<file_key> 换 token，读接口只带 query tk；
    密码不再放进 query（宿主 bridge 不能带自定义 header，token 只能走 tk）。"""
    js = _read("app.js")
    assert "const sessionToken = {}" in js, "缺少内存 token 表 sessionToken"
    assert "function authParams()" in js, "缺少读接口凭证参数 authParams()"
    assert re.search(r"\{\s*tk:\s*sessionToken\[currentKey\]\s*\}", js), (
        "authParams 必须把 token 放进 query 参数 tk"
    )
    assert re.search(r'apiPost\(\s*`unlock/\$\{key\}`', js), (
        "缺少 POST unlock/<file_key> 解锁请求"
    )
    # token 失效/过期：清理并重弹密码框
    assert "function clearSessionToken(" in js
    assert re.search(r"r\.need_password", js), "未处理读接口的 need_password 闸门"
    assert "PASSWORD_REQUIRED" in js and "PASSWORD_WRONG" in js
    # 旧 zip（主人 QQ 未知）：先问 QQ，再不带密码试换令牌（没设密码的用户
    # 不该被白问一次密码）；后端要求密码才继续弹密码框。
    assert re.search(
        r"apiPost\(\s*`unlock/\$\{key\}`,\s*\{\s*qq:\s*ownerQq\s*\}\s*\)", js
    ), "旧 zip 主人解析时应先不带密码试换令牌（unlock body 只带 qq）"
    # 旧实现：密码进 query / 内存里长期存明文密码，本轮必须切干净
    assert "sessionPw" not in js, "不应再保留明文的 sessionPw 缓存"
    assert not re.search(r"params\.pw", js), "不应再把 pw 放进 query 参数"
    assert not re.search(r"body\.pw", js), "不应再把 pw 放进请求体"


def test_delete_days_posts_token_in_body():
    """api:post 不能带 query（宿主 bridge 只发 body）：delete_days 的 token
    必须放请求体 ``tk`` 字段。"""
    js = _read("app.js")
    delete_fn = js[js.index("async function deleteDaysRequest(") :]
    delete_fn = delete_fn[: delete_fn.index("async function onBatchDelete(")]
    assert re.search(r"file_key: currentKey,\s*days: daysToDelete,\s*\.\.\.authParams\(\)", delete_fn), (
        "delete_days 请求体应带 ...authParams()（即 tk 字段）"
    )


# --------------------------------------------------------------- P1-6 导出


def test_export_preflights_password_gate_before_download():
    """下载路径失败时只剩 axios 通用 403/400 文案，不能只靠错误码分支判断：
    已知有查看密码且本地无 token 时必须先弹密码框，再下载。"""
    js = _read("app.js")
    assert "u.protected && !tokenAlive(currentKey)" in js, (
        "doExport 未在 bridge.download 前主动判断 u.protected / token"
    )
    assert re.search(r"/status code 403\\b/i", js), (
        "未识别下载失败的通用 403（response.data 是 Blob，拿不到 PASSWORD_* 错误码）"
    )
    assert re.search(r"/status code 400\\b/i", js), "未识别下载失败的通用 400"
    assert "u.owner_known === false" in js, (
        "通用 400 且主人 QQ 未知时应重问主人 QQ"
    )


# --------------------------------------------------------- P2-18 失败提示


def test_read_failures_are_surfaced_and_clear_stale_data():
    """loadUsers/loadDays 失败不能静默：要 toast 后端 message、右页明确
    「读取失败，请刷新」并清掉旧目录/旧正文；刷新成功后再提示「已刷新」。"""
    js = _read("app.js")
    assert "function showDiaryReadFailure(" in js, "缺少目录/正文读取失败处理"
    assert "function showUsersReadFailure(" in js, "缺少用户列表读取失败处理"
    assert "读取失败，请点「⟳ 刷新」重试" in js, "右页/空舞台缺少明确失败文案"
    assert "async function refreshAll(" in js, "缺少统一刷新入口 refreshAll"
    assert re.search(r"refreshAll\(\s*\{\s*announce:\s*true\s*\}\s*\)", js), (
        "刷新按钮应走 refreshAll({announce:true})：成功才提示「已刷新」"
    )
    # showDiaryReadFailure 必须清旧数据（不能留着上一个用户的日记）
    fn = js[js.index("function showDiaryReadFailure(") :]
    fn = fn[: fn.index("function showUsersReadFailure(")]
    for marker in ("days = []", 'dayList").innerHTML = ""', 'pageInfo").textContent'):
        assert marker in fn, f"读取失败处理未清理旧数据：{marker}"


# ------------------------------------------------------ P3-19 批量删除


def test_batch_delete_reports_missing_days():
    """web_delete_days 返回的 missing（勾选但 zip 里本来就没有）要提示。"""
    js = _read("app.js")
    assert re.search(r"Number\(r\.missing\)", js), "批量删除未读取后端 missing 字段"
    assert "本来没有日记" in js, "缺失天数的提示文案缺失"


# ------------------------------------------------- P3-20 owner 解析后刷新


def test_owner_resolved_refreshes_lock_ui():
    """旧 zip 解析出主人 QQ 后 🔒 按钮要立刻出现/状态正确（protected 只在
    列 users 时算，前端要本地更新并刷新按钮与下拉标签）。"""
    js = _read("app.js")
    assert "function markOwnerResolved(" in js, "缺少 owner 解析成功后的本地更新"
    fn = js[js.index("function markOwnerResolved(") :]
    fn = fn[: fn.index("function syncUserSelectLabels(")]
    assert "u.owner_known = true" in fn
    assert "u.protected = true" in fn, "密码验证通过后应把 protected 置 true"
    assert "syncUserSelectLabels()" in fn, "本地更新后要重写下拉框标签（🔒 前缀）"
    assert "updateLockUI()" in fn, "本地更新后要刷新顶栏锁按钮"
    # acquireToken 成功（密码已验证）时调用
    assert re.search(r"markOwnerResolved\(\s*ownerQq,\s*\{\s*passwordVerified:\s*true\s*\}\s*\)", js), (
        "acquireToken 成功换到 token 后应标 owner 已解析且密码已验证"
    )


# ------------------------------------------------------- P3-21 插图闸门


def test_image_gate_responses_use_password_flow():
    """读图接口对 need_password/need_owner 返回 200 JSON；不能当成图片缺失
    静默降级成 [插图：…]，要走闸门换 token 后重试这张图。"""
    js = _read("app.js")
    image_fn = js[js.index("async function loadDiaryImages(") :]
    image_fn = image_fn[: image_fn.index("/* 昵称里的不可见字符")]
    assert "gatedGet(" in image_fn, "插图读取必须走 gatedGet（带 tk + 闸门流程）"
    assert "img-gated" in js, "用户取消解锁时应保留「需要解锁」占位，不能标成缺图"
    css = _read("style.css")
    assert ".diary-img.img-gated" in css, "缺少解锁占位样式 .img-gated"


# ----------------------------------------------------- P3-22 导入预检


def test_import_preflight_size_limit_and_timeout_hint():
    """导入迁移包：按 file.size 预检 32MB（与后端 upload.save(max_bytes=32MB)
    一致），上传 timeout 时提示可能已导入。"""
    js = _read("app.js")
    assert re.search(r"IMPORT_MAX_BYTES\s*=\s*32\s*\*\s*1024\s*\*\s*1024", js), (
        "缺少 32MB 导入预检常量"
    )
    assert re.search(r"file\.size\s*>\s*IMPORT_MAX_BYTES", js), (
        "doImport 未按 file.size 判上限"
    )
    assert "可能已经导入完成" in js, "timeout 失败时缺少「可能已导入，请刷新确认」提示"


def test_search_and_material_panels_use_password_gate_and_retry():
    js = _read("app.js")
    search_fn = js[js.index("async function runSearch(") :]
    search_fn = search_fn[: search_fn.index("async function loadMaterials(")]
    assert "gatedGet(`search/${currentKey}`" in search_fn
    assert "if (r === null) return;" in search_fn

    materials_fn = js[js.index("async function loadMaterials(") :]
    materials_fn = materials_fn[: materials_fn.index("await loadPrefs()")]
    assert "gatedGet(`materials/${currentKey}`" in materials_fn
    assert "if (r === null) return;" in materials_fn

    post_fn = js[js.index("async function gatedPost(") :]
    post_fn = post_fn[: post_fn.index("/* ---------- 查看密码：顶栏的锁按钮")]
    for marker in (
        "bridge.apiPost", "...authParams()", "r.need_password",
        "PASSWORD_REQUIRED", "PASSWORD_WRONG", "acquireToken", "continue",
    ):
        assert marker in post_fn, f"gatedPost 缺少 {marker}"
    assert re.search(r"guard\s*=\s*0;\s*guard\s*<\s*5", post_fn)

    action_fn = js[js.index('$("materialResults").addEventListener("click"') :]
    action_fn = action_fn[: action_fn.index("try {", action_fn.index("await loadMaterials()"))]
    assert "gatedPost(`materials/${currentKey}/action`" in action_fn
    assert "if (!r || r.ok !== true)" in action_fn
