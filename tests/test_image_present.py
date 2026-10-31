# -*- coding: utf-8 -*-
"""v0.6 D4/D5/D6 呈现层与配置：QQ 图文穿插、Web 图片路由、/形象 指令、配置页 schema。

依据《插图功能接口契约》第 6、7、8 节与设计方案第一部分 D4/D5/D6。
main.py / views 由 adapter-main 与 views-schema 实现；本文件是它们的回归闸门。
"""

from __future__ import annotations

import base64
import json
import os

from astrbot.api.web import bind_request_context

from harness import env, loader
from harness.fakes import FakeWebRequest, make_event
from harness.world import DIARY_TEXT, World
from image_testkit import (
    img_config,
    md5_key,
    png,
    replace_zip_entry,
    rewrite_zip,
    zip_entries,
)

SENDER = "10001"
SENDER_NAME = "阿罗"
OTHER = "20002"
UMO = "aiocqhttp:FriendMessage:10001"
PASSWORD = "secret123"

_main_mod = loader.get_module(env.SIDE_REFACTOR)
PACKAGE_ROOT = os.path.dirname(_main_mod.__file__)


# ---------------------------------------------------------------------------
# 事件 / Web 调用小工具
# ---------------------------------------------------------------------------


def private_event(text: str, **kwargs):
    return make_event(text, sender_id=SENDER, sender_name=SENDER_NAME, **kwargs)


def group_event(text: str, **kwargs):
    return make_event(
        text, sender_id=SENDER, sender_name=SENDER_NAME, group_id="88888", **kwargs
    )


async def _call(handler, request, *args):
    with bind_request_context(request):
        response = await handler(*args)
    if isinstance(response, dict) or response is None:
        return response
    body = getattr(response, "body", b"{}")
    return {
        "status": getattr(response, "status_code", None),
        "body": json.loads(bytes(body).decode("utf-8")),
    }


def _seed_diary_with_image(plugin, data=None, alt="窗边的黄昏", save=False):
    """写一篇带一张插图的日记；返回 (entry_name, image_name)。"""
    data = png(1) if data is None else data
    plugin._save_diary(
        SENDER,
        f"今天很安静。\n![{alt}](images/pending-placeholder.png)\n结尾。",
        sender_name=SENDER_NAME,
        entry_name_hint="20261023",
        origin="private",
        mode="full",
    )
    name = plugin.save_image(SENDER, data, "20261023", 1, alt)
    # 把正文里的占位引用改成真实图片名（模拟 PENDING 已被替换）
    path = plugin._diary_path(SENDER)
    entries = zip_entries(path)
    entry = "diaries/20261023.md"
    content = entries[entry].decode("utf-8").replace(
        "images/pending-placeholder.png", name
    )
    entries[entry] = content.encode("utf-8")
    entries[plugin._sig_entry(entry)] = plugin._entry_sig(SENDER, entry, content).encode(
        "utf-8"
    )
    rewrite_zip(path, entries)
    return entry, name


# ---------------------------------------------------------------------------
# D4：QQ 端图文穿插
# ---------------------------------------------------------------------------


async def test_send_diary_content_interleaves_text_and_images():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = inst.plugin
        name = plugin.save_image(SENDER, png(2), "20261023", 1, "窗边的黄昏")
        content = f"开头文字。\n![窗边的黄昏]({name})\n结尾文字。"

        event = private_event("")
        await plugin._send_diary_content(event, SENDER, content)

        kinds = [r.kind for r in event.sent]
        assert kinds == ["plain", "chain", "plain"], (
            f"图文穿插顺序应为 文字→图→文字，实际 {kinds}：{event.sent!r}"
        )
        assert event.sent[0].text.strip() == "开头文字。"
        assert event.sent[2].text.strip() == "结尾文字。"
        assert event.sent_chains == [["Image"]], (
            f"图片段必须是 Image 组件发出，实际组件 {event.sent_chains!r}"
        )
        # 发完即删临时文件：数据目录不留第二份图片副本
        temp_files = list((inst.data_dir / "temp").glob("*")) if (inst.data_dir / "temp").is_dir() else []
        assert temp_files == [], f"插图临时文件没有清理：{temp_files!r}"


async def test_send_diary_content_two_images_message_count():
    async with World(config=img_config()) as world:
        plugin = world.refactor.plugin
        a = plugin.save_image(SENDER, png(3), "20261023", 1, "第一幅")
        b = plugin.save_image(SENDER, png(4), "20261023", 2, "第二幅")
        content = f"一。\n![第一幅]({a})\n二。\n![第二幅]({b})\n三。"

        event = private_event("")
        await plugin._send_diary_content(event, SENDER, content)

        kinds = [r.kind for r in event.sent]
        assert kinds == ["plain", "chain", "plain", "chain", "plain"], (
            f"单篇 2 图应发 5 条（文字/图/文字/图/文字），实际 {kinds}"
        )
        assert event.sent_chains == [["Image"], ["Image"]]
        joined = "".join(r.text for r in event.sent)
        assert "一。" in joined and "二。" in joined and "三。" in joined


async def test_send_diary_content_missing_image_degrades_to_placeholder():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = inst.plugin
        name = plugin.save_image(SENDER, png(5), "20261023", 1, "窗边的黄昏")
        content = f"文字一。\n![窗边的黄昏]({name})\n文字二。"
        # 图片条目丢失：优雅降级（不告警）
        entries = zip_entries(inst.diary_path(SENDER))
        del entries[name]
        del entries[plugin._image_sig_entry(name)]
        rewrite_zip(inst.diary_path(SENDER), entries)

        event = private_event("")
        await plugin._send_diary_content(event, SENDER, content)

        texts = [r.text for r in event.sent]
        assert "[插图：窗边的黄昏]" in texts, f"缺图未降级为占位文本：{texts!r}"
        assert event.sent_chains == [], "缺图不应尝试发 Image 组件"
        joined = "".join(texts)
        assert "文字一。" in joined and "文字二。" in joined


async def test_send_diary_content_tampered_image_warns():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = inst.plugin
        name = plugin.save_image(SENDER, png(6), "20261023", 1, "窗边的黄昏")
        content = f"文字一。\n![窗边的黄昏]({name})\n文字二。"
        replace_zip_entry(inst.diary_path(SENDER), name, png(7) + b"TAMPERED")

        event = private_event("")
        await plugin._send_diary_content(event, SENDER, content)

        texts = [r.text for r in event.sent]
        warning = [t for t in texts if "⚠" in t]
        assert len(warning) == 1, f"被换图应发 1 条警告：{texts!r}"
        assert "验签失败" in warning[0]
        assert event.sent_chains == [], "篡改的图绝不能作为 Image 发出"


async def test_view_diary_zero_image_output_is_byte_identical_to_baseline():
    """零图片日记的 /查看日记 输出必须与 v0.5.5 逐字节一致（差分）。"""
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            inst.plugin._save_diary(
                SENDER,
                "没有插图的一天。",
                sender_name=SENDER_NAME,
                origin="private",
                mode="full",
            )
            inst.set_session_supported(False)
            fallback = await inst.run_handler("view_diary", private_event("/查看日记"))
            inst.set_session_supported(True)
            inst.set_replies([{"text": "最新"}])
            latest = await inst.run_handler("view_diary", private_event("/查看日记"))
            inst.set_replies([{"text": "1"}])
            first = await inst.run_handler("view_diary", private_event("/查看日记"))
            return {"fallback": fallback, "latest": latest, "first": first}

        pair = await world.run_pair(scenario, label="zero image view")
        value = pair.value
        # 差分通过 = 两边逐字节一致；再显式核对输出形态（2 条：列表 + 正文）
        for key in ("fallback", "latest", "first"):
            assert "没有插图的一天。" in "".join(value[key]), f"{key} 没显示日记正文"
            assert "📖 2026-10-23 的日记：" in "".join(value[key])

        # refactor 侧：零图片正文只发一条纯文本，不出现空图片段
        event = private_event("")
        await world.refactor.plugin._send_diary_content(
            event, SENDER, "纯文字。\n第二行。"
        )
        assert len(event.sent) == 1
        assert event.sent[0].kind == "plain"
        assert event.sent[0].text == "纯文字。\n第二行。"
        assert event.sent_chains == []


async def test_write_diary_notice_appends_image_notice_and_clears_it():
    """手动 /写日记 的通知会追加 _image_last_notice（读一次即清空）。"""
    async with World(
        config=img_config(diary_op_interval=0, write_min_ratio=0, use_persona=False)
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        notice = "\n\n（她正在给这篇日记画一幅插图，画好会告诉你。）"
        state = {"calls": 0}

        async def fake_write(umo, sender_id, sender_name, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                plugin._image_last_notice = notice
            return "假日记正文", None

        plugin._write_diary = fake_write
        out = await inst.run_handler("write_diary_cmd", private_event("/写日记"))
        text = "\n".join(out)
        assert "正在给这篇日记画一幅插图" in text, f"写日记通知没有追加插图提示：{text!r}"
        assert plugin._image_last_notice == "", "插图提示行读一次后没有清空"

        # 第二次不再追加（清空生效）
        out2 = await inst.run_handler("write_diary_cmd", private_event("/写日记"))
        assert "正在给这篇日记画一幅插图" not in "\n".join(out2)


# ---------------------------------------------------------------------------
# D5：Web 图片路由（base64 JSON + 与 day 路由同口径鉴权）
# ---------------------------------------------------------------------------


async def test_web_get_diary_image_ok_base64():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = inst.plugin
        _entry, name = _seed_diary_with_image(plugin, data=png(11))
        file_key = plugin._diary_file_key(SENDER)

        result = await _call(
            plugin.web_get_diary_image,
            FakeWebRequest(query={"qq": SENDER}),
            file_key,
            name.split("/", 1)[1],
        )
        assert result["status"] == 200, result
        body = result["body"]
        assert body["found"] is True
        assert body["mime"] == "image/png"
        assert base64.b64decode(body["image"]) == png(11)
        # bridge 会解包顶层 data：字段必须叫 image，不能叫 data
        assert "data" not in body, f"图片字段名不能是 data（会被 bridge 当信封解包）：{body!r}"

        # 契约允许 name 传 images/xxx.png 全名
        result2 = await _call(
            plugin.web_get_diary_image,
            FakeWebRequest(query={"qq": SENDER}),
            file_key,
            name,
        )
        assert result2["status"] == 200 and result2["body"]["found"] is True


async def test_web_get_diary_image_missing_and_tampered():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = inst.plugin
        _entry, name = _seed_diary_with_image(plugin, data=png(12))
        file_key = plugin._diary_file_key(SENDER)

        # tampered：zip 内换图
        replace_zip_entry(inst.diary_path(SENDER), name, png(13) + b"TAMPERED")
        tampered = await _call(
            plugin.web_get_diary_image,
            FakeWebRequest(query={"qq": SENDER}),
            file_key,
            name.split("/", 1)[1],
        )
        assert tampered["status"] == 200
        assert tampered["body"] == {"found": False, "status": "tampered"}, tampered

        # missing：条目被删（正文引用还在，Web 端降级占位）
        entries = zip_entries(inst.diary_path(SENDER))
        del entries[name]
        del entries[plugin._image_sig_entry(name)]
        rewrite_zip(inst.diary_path(SENDER), entries)
        missing = await _call(
            plugin.web_get_diary_image,
            FakeWebRequest(query={"qq": SENDER}),
            file_key,
            name.split("/", 1)[1],
        )
        assert missing["status"] == 200
        assert missing["body"] == {"found": False, "status": "missing"}, missing


async def test_web_get_diary_image_password_gate():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = inst.plugin
        _entry, name = _seed_diary_with_image(plugin, data=png(14))
        plugin._save_pref_data(
            SENDER, {"view_password": plugin._make_view_password(PASSWORD)}
        )
        file_key = plugin._diary_file_key(SENDER)
        short = name.split("/", 1)[1]

        no_pw = await _call(
            plugin.web_get_diary_image, FakeWebRequest(), file_key, short
        )
        wrong_pw = await _call(
            plugin.web_get_diary_image,
            FakeWebRequest(query={"pw": "nope"}),
            file_key,
            short,
        )
        right_pw = await _call(
            plugin.web_get_diary_image,
            FakeWebRequest(query={"pw": PASSWORD}),
            file_key,
            short,
        )
        assert no_pw["body"] == {"need_password": True}, no_pw
        assert wrong_pw["status"] == 403, wrong_pw
        assert right_pw["status"] == 200 and right_pw["body"]["found"] is True


async def test_web_get_diary_image_rejects_bad_key_and_traversal():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = inst.plugin
        _entry, name = _seed_diary_with_image(plugin, data=png(15))
        file_key = plugin._diary_file_key(SENDER)

        bad_key = await _call(
            plugin.web_get_diary_image,
            FakeWebRequest(),
            "../../etc",
            name.split("/", 1)[1],
        )
        unknown_key = await _call(
            plugin.web_get_diary_image,
            FakeWebRequest(),
            "0123456789abcdef",
            name.split("/", 1)[1],
        )
        traversal = await _call(
            plugin.web_get_diary_image,
            FakeWebRequest(query={"qq": SENDER}),
            file_key,
            "../../../etc/passwd",
        )
        wrong_name = await _call(
            plugin.web_get_diary_image,
            FakeWebRequest(query={"qq": SENDER}),
            file_key,
            "not-in-zip.png",
        )
        assert bad_key["status"] == 400, bad_key
        assert unknown_key["status"] == 404, unknown_key
        assert traversal["body"] == {"found": False, "status": "missing"}, traversal
        assert wrong_name["body"] == {"found": False, "status": "missing"}, wrong_name


async def test_web_get_diary_image_resolves_owner_from_qq_hint():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = inst.plugin
        _entry, name = _seed_diary_with_image(plugin, data=png(16))
        file_key = plugin._diary_file_key(SENDER)
        short = name.split("/", 1)[1]

        # 模拟旧 zip（说明.txt 没有用户ID 行）：需要前端传 qq 才能确定主人
        path = inst.diary_path(SENDER)
        entries = zip_entries(path)
        readme = entries["说明.txt"].decode("utf-8")
        entries["说明.txt"] = "\n".join(
            line for line in readme.split("\n") if not line.startswith("用户ID：")
        ).encode("utf-8")
        plugin._write_zip_atomic(path, entries)

        need_owner = await _call(
            plugin.web_get_diary_image, FakeWebRequest(), file_key, short
        )
        assert need_owner["body"] == {"need_owner": True}, need_owner

        wrong_qq = await _call(
            plugin.web_get_diary_image,
            FakeWebRequest(query={"qq": "99999"}),
            file_key,
            short,
        )
        assert wrong_qq["status"] == 400, wrong_qq

        right_qq = await _call(
            plugin.web_get_diary_image,
            FakeWebRequest(query={"qq": SENDER}),
            file_key,
            short,
        )
        assert right_qq["status"] == 200 and right_qq["body"]["found"] is True
        # 记住主人后不再需要 qq
        remembered = await _call(
            plugin.web_get_diary_image, FakeWebRequest(), file_key, short
        )
        assert remembered["status"] == 200 and remembered["body"]["found"] is True


# ---------------------------------------------------------------------------
# D6：/形象 指令（仅私聊，固定文案）
# ---------------------------------------------------------------------------


async def test_persona_image_cmd_show_current_and_pending():
    async with World(
        config=img_config(persona_image_desc="银发红瞳，白色长裙。")
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_pending_persona_desc(SENDER, "黑发金瞳，深色风衣。", kind="evolve")

        out = await inst.run_handler("persona_image_cmd", private_event("/形象"))
        text = "\n".join(out)
        assert "形象档案" in text
        assert "银发红瞳，白色长裙。" in text, f"没有显示当前形象档案：{text!r}"
        assert "黑发金瞳，深色风衣。" in text, f"没有显示待确认提案：{text!r}"
        assert "/形象 确认" in text and "/形象 修改" in text and "/形象 重拟" in text


async def test_persona_image_cmd_modify_truncates_to_200_chars():
    async with World(config=img_config()) as world:
        inst = world.refactor
        plugin = inst.plugin

        desc = "黑" * 260
        out = await inst.run_handler(
            "persona_image_cmd", private_event(f"/形象 修改 {desc}")
        )
        text = "\n".join(out)
        stored = plugin.config.get("persona_image_desc", "")
        assert len(stored) == 200, f"超过 200 字应截断，实际 {len(stored)} 字"
        assert stored == "黑" * 200
        assert "已截断" in text, f"截断应明确告知用户：{text!r}"

        empty = await inst.run_handler("persona_image_cmd", private_event("/形象 修改"))
        assert "用法" in "\n".join(empty)


async def test_persona_image_cmd_propose_then_confirm():
    async with World(
        config=img_config(persona_image_desc=""),
        persona_prompt="你是一位银发红瞳的少女，常穿白色长裙。",
    ) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin.config["use_persona"] = True
        inst.provider.replies[:] = ["银发红瞳，白色长裙，颈间一枚月牙吊坠。"]

        proposed = await inst.run_handler("persona_image_cmd", private_event("/形象 重拟"))
        text = "\n".join(proposed)
        assert "新的形象提案" in text, f"重拟没有给出提案：{text!r}"
        assert "银发红瞳" in text
        pending = plugin._load_pending_persona_desc(SENDER)
        assert pending.get("desc") == "银发红瞳，白色长裙，颈间一枚月牙吊坠。"
        assert pending.get("kind") == "init", "首次提案 kind 应为 init"
        assert plugin.config.get("persona_image_desc", "") == "", "提案不能直接写进配置"

        confirmed = await inst.run_handler("persona_image_cmd", private_event("/形象 确认"))
        assert "已确认并写入配置" in "\n".join(confirmed)
        assert (
            plugin.config.get("persona_image_desc")
            == "银发红瞳，白色长裙，颈间一枚月牙吊坠。"
        )
        assert plugin._load_pending_persona_desc(SENDER) == {}

        again = await inst.run_handler("persona_image_cmd", private_event("/形象 确认"))
        assert "没有确认成功" in "\n".join(again)


async def test_persona_image_cmd_group_rejected_and_unknown_argument():
    async with World(config=img_config()) as world:
        inst = world.refactor
        group = await inst.run_handler("persona_image_cmd", group_event("/形象"))
        assert "只能私聊" in "\n".join(group), f"群聊应拒绝：{group!r}"

        unknown = await inst.run_handler(
            "persona_image_cmd", private_event("/形象 随便说点什么")
        )
        text = "\n".join(unknown)
        assert "用法" in text or "/形象 修改" in text, f"未知参数应回用法：{text!r}"


# ---------------------------------------------------------------------------
# D6：_conf_schema.json 新配置项（键名/类型/默认值/条件/隐私提示）
# ---------------------------------------------------------------------------

EXPECTED_IMAGE_CONFIG = {
    "image_enabled": ("bool", False),
    "image_mode": ("string", "local"),
    "image_local_url": ("string", ""),
    "image_api_base": ("string", ""),
    "image_api_key": ("string", ""),
    "image_model": ("string", ""),
    "image_size_preset": (
        "string",
        # schema 默认值按 JSON 文本字面量写（空格可有可无，比较时归一化）
        '{"portrait":[768,1024],"landscape":[1024,768],"square":[1024,1024]}',
    ),
    "image_prob_memo": ("float", 0.0),
    "image_prob_full": ("float", 0.3),
    "image_limit_memo": ("int", 0),
    "image_limit_full": ("int", 2),
    "image_limit_daily": ("int", 6),
    "image_limit_group": ("int", 0),
    "image_concurrency": ("int", 2),
    "image_timeout": ("int", 120),
    "persona_image_desc": ("string", ""),
    "image_style_prompt": ("string", ""),
    "user_image_enabled": ("bool", True),
    "user_image_limit": ("int", 1),
    "user_ref_photo": ("string", ""),
    "vision_api_base": ("string", ""),
    "vision_api_key": ("string", ""),
    "vision_api_model": ("string", ""),
}


def _load_schema() -> dict:
    with open(os.path.join(PACKAGE_ROOT, "_conf_schema.json"), encoding="utf-8-sig") as f:
        return json.load(f)


def test_conf_schema_image_keys_types_defaults_and_conditions():
    schema = _load_schema()
    for key, (expected_type, expected_default) in EXPECTED_IMAGE_CONFIG.items():
        assert key in schema, f"_conf_schema.json 缺少插图配置项 {key}"
        item = schema[key]
        assert item.get("type") == expected_type, (
            f"{key} 的 type 应为 {expected_type}，实际 {item.get('type')!r}"
        )
        actual_default = item.get("default")
        if key == "image_size_preset":
            # JSON 文本默认值按解析后的内容比较（空格/键序不影响语义）
            assert json.loads(actual_default) == json.loads(expected_default), (
                f"{key} 的 default 解析后不一致：{actual_default!r}"
            )
        else:
            assert actual_default == expected_default, (
                f"{key} 的 default 应为 {expected_default!r}，实际 {actual_default!r}"
            )
        # 除总开关外，其余项都应在总开关关闭时隐藏（配置页无感）
        if key != "image_enabled":
            condition = item.get("condition") or {}
            assert condition.get("image_enabled") is True, (
                f"{key} 缺少 condition.image_enabled=true：{item.get('condition')!r}"
            )

    # 通道选项
    assert schema["image_mode"].get("options") == ["local", "cloud"]
    # 条件显示：本地/云端各自的字段
    assert (schema["image_local_url"].get("condition") or {}).get("image_mode") == "local"
    for key in ("image_api_base", "image_api_key", "image_model"):
        assert (schema[key].get("condition") or {}).get("image_mode") == "cloud"
    # 云端隐私提示（设计隐私红线：显式 opt-in + 知情提示）
    assert "隐私" in schema["image_api_base"].get("hint", ""), (
        "云端生图 API 地址的 hint 缺少隐私提示"
    )
    # 概率/并发用 slider，避免填出离谱值
    assert "slider" in schema["image_prob_full"]
    assert schema["image_concurrency"]["slider"]["min"] == 1
    assert schema["image_concurrency"]["slider"]["max"] == 8


def test_image_cfg_defaults_match_conf_schema_defaults():
    """schema 里写的默认值必须就是 _image_cfg() 实际回退到的值（防两处漂移）。"""
    schema = _load_schema()
    defaults = {key: item["default"] for key, item in schema.items()}
    import asyncio

    async def _run():
        async with World(config=defaults) as world:
            return world.refactor.plugin._image_cfg()

    cfg = asyncio.run(_run())
    assert cfg["image_enabled"] is False
    assert cfg["image_mode"] == "local"
    assert cfg["image_local_url"] == ""
    assert float(cfg["image_prob_memo"]) == 0.0
    assert float(cfg["image_prob_full"]) == 0.3
    assert int(cfg["image_limit_memo"]) == 0
    assert int(cfg["image_limit_full"]) == 2
    assert int(cfg["image_limit_daily"]) == 6
    assert int(cfg["image_limit_group"]) == 0
    assert int(cfg["image_concurrency"]) == 2
    assert int(cfg["image_timeout"]) == 120
    assert bool(cfg["user_image_enabled"]) is True
    assert int(cfg["user_image_limit"]) == 1
    preset = cfg["image_size_preset"]
    for key, expected in {
        "portrait": [768, 1024],
        "landscape": [1024, 768],
        "square": [1024, 1024],
    }.items():
        assert list(preset[key]) == expected, (
            f"schema 默认 image_size_preset 与 _image_cfg 解析不一致：{key}={preset[key]!r}"
        )


# ---------------------------------------------------------------------------
# D5：WebUI 静态资产（renderContent 图片特判 / 样式规范）
# ---------------------------------------------------------------------------

VIEW_DIR = env.REFACTOR_DIR / "views" / "diary-book"


def _view(name: str) -> str:
    return (VIEW_DIR / name).read_text(encoding="utf-8")


def test_view_appjs_renders_image_syntax():
    js = _view("app.js")
    # 只有图片语法是契约里的 images/ 开头才当图
    assert "images/" in js
    assert "renderContent" in js
    assert "diary-img" in js
    assert "data-entry" in js
    # 从 bridge 响应读 image 字段（不是 data，见 bridge 信封规则）
    assert "r.image" in js or "res.image" in js, "插图 base64 应读取响应字段 image"
    # PENDING 占位不请求后端、显示"还在画"
    assert "PENDING" in js
    assert "img-generating" in js
    # missing / tampered 两态
    assert "img-missing" in js
    assert "img-tampered" in js
    assert "missing" in js and "tampered" in js
    # 图片服务路由调用
    assert "image/" in js and "apiGet" in js


def test_view_css_image_sizing_rules():
    css = _view("style.css")
    assert "diary-img" in css
    # 横图通栏、竖图限宽 ~70%、圆角
    assert "landscape" in css and "portrait" in css and "square" in css
    assert "max-width: 100%" in css
    assert "max-width: 70%" in css
    assert "border-radius" in css
    # PENDING / 篡改 / 缺图视觉态
    assert "img-generating" in css
    assert "img-tampered" in css
    assert "img-missing" in css
