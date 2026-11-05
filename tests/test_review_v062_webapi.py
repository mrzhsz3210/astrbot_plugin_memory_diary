# -*- coding: utf-8 -*-
"""v0.6.2 审查修复回归：Web 查看密码换短期令牌（P3-7）+ XFF 本机判定（P3-23）。

新接口 / 新的安全口径只存在于 refactor 侧（baseline 老实现没有 /unlock）；
``--baseline-only`` 模式下直接跳过，保证 sanity run 不导入工作副本。
"""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest import mock

from astrbot.api.web import bind_request_context

from harness import compare, env
from harness.fakes import FakeWebRequest
from harness.world import DIARY_TEXT, FIXED_NOW, World

SENDER = "10001"
SENDER_NAME = "阿罗"
OTHER = "20002"
PW = "secret123"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _json(resp) -> dict:
    return {
        "status": getattr(resp, "status_code", None),
        "body": json.loads(bytes(resp.body).decode("utf-8")),
    }


async def _call(handler, request, *args):
    """绑定 AstrBot request context 后调用 handler，返回状态码 + JSON/文件类型。"""
    with bind_request_context(request):
        response = await handler(*args)
    if type(response).__name__ == "FileResponse":
        return {
            "status": getattr(response, "status_code", None),
            "kind": "FileResponse",
        }
    if isinstance(response, dict) or response is None:
        return response
    return _json(response)


def _post(payload, query=None):
    return FakeWebRequest(method="POST", payload=payload, query=query)


def _seed(inst, sender: str = SENDER, name: str = SENDER_NAME, day: str | None = None):
    kwargs = {"entry_name_hint": day} if day else {}
    inst.plugin._save_diary(
        sender, DIARY_TEXT, sender_name=name, origin="private", mode="full", **kwargs
    )


def _protect(inst, owner: str, pw: str) -> None:
    inst.plugin._save_pref_data(
        owner, {"view_password": inst.plugin._make_view_password(pw)}
    )


# ---------------------------------------------------------------------------
# P3-7：/unlock 换短期令牌，之后读/导/删接口带 tk
# ---------------------------------------------------------------------------


async def test_web_unlock_token_flow():
    """解锁拿 token → days/day/image/export/export_local/delete_days 全部放行。"""
    if env.is_baseline_only():
        return  # /unlock 只存在于 refactor 侧
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed(inst, day="20261022")
        _seed(inst, day="20261023")
        plugin.save_image(SENDER, PNG, day="20261023", seq=1, desc="窗边")
        image_name = plugin.list_images(SENDER)[0].split("/", 1)[1]
        _protect(inst, SENDER, PW)
        file_key = plugin._diary_file_key(SENDER)

        async def unlock(payload, query=None):
            return await _call(
                plugin.web_unlock, _post(payload, query=query), file_key
            )

        # 路由已登记（否则前端点了没反应）
        assert any(
            route[0].endswith("/unlock/<file_key>") and list(route[2]) == ["POST"]
            for route in inst.ctx.registered_web_apis
        )

        wrong = await unlock({"pw": "nope"})
        missing = await unlock({})
        # 兼容红线：不带 tk 也不带 pw 时的响应与改造前逐字节一致
        no_pw = await _call(
            plugin.web_read_day, FakeWebRequest(), file_key, "20261023"
        )
        no_pw_export = await _call(plugin.web_export, FakeWebRequest(), file_key)
        ok = await unlock({"pw": PW})
        token = ok["body"].get("token")

        assert wrong["status"] == 403
        assert wrong["body"]["message"] == "PASSWORD_WRONG"
        assert missing["status"] == 403
        assert missing["body"]["message"] == "PASSWORD_REQUIRED"
        assert no_pw["status"] == 200 and no_pw["body"] == {"need_password": True}
        assert no_pw_export["status"] == 403
        assert no_pw_export["body"]["message"] == "PASSWORD_REQUIRED"
        assert ok["status"] == 200
        assert ok["body"]["unlocked"] is True
        assert ok["body"]["expires_in"] == 1800
        assert isinstance(token, str) and len(token) == 48
        assert int(token, 16) >= 0

        # 令牌只放内存：数据目录里任何文件都不该出现它
        for path in inst.data_dir.rglob("*"):
            if path.is_file():
                assert token.encode("ascii") not in path.read_bytes()

        tk = FakeWebRequest(query={"tk": token})
        days = await _call(plugin.web_list_days, tk, file_key)
        day = await _call(plugin.web_read_day, tk, file_key, "20261023")
        image = await _call(plugin.web_get_diary_image, tk, file_key, image_name)
        export = await _call(plugin.web_export, tk, file_key)

        assert days["status"] == 200 and len(days["body"]["days"]) == 2
        assert day["status"] == 200
        assert day["body"]["content"].startswith("—— 20:30")
        assert image["body"]["found"] is True
        assert export["kind"] == "FileResponse"

        # 「存到本机」类导出同样认 tk（Path.home / Popen 打桩，绝不动真实目录）
        home = inst.root / "home"
        home.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(Path, "home", classmethod(lambda cls: home)):
            with mock.patch.object(subprocess, "Popen", lambda *a, **k: None):
                local = await _call(plugin.web_export_local, tk, file_key)
        assert local["status"] == 200
        assert (home / local["body"]["filename"]).exists()

        # 批量删除是 POST：宿主 bridge 不能带 query，令牌放请求体 tk
        deleted = await _call(
            plugin.web_delete_days,
            _post({"file_key": file_key, "days": ["20261022"], "tk": token}),
        )
        assert deleted["body"] == {"deleted": 1, "remaining": 1, "missing": 0}

        # 旧 pw 通道兼容保留
        legacy = await _call(
            plugin.web_read_day, FakeWebRequest(query={"pw": PW}), file_key, "20261023"
        )
        assert legacy["status"] == 200
        # days 列表本来就不在密码闸内（既有行为不变）
        assert (await _call(plugin.web_list_days, FakeWebRequest(), file_key))[
            "status"
        ] == 200


async def test_web_unlock_without_password_issues_token():
    """没设查看密码的日记也能换令牌（前端统一带 tk，后端行为不变）。"""
    if env.is_baseline_only():
        return
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed(inst)
        file_key = plugin._diary_file_key(SENDER)
        ok = await _call(plugin.web_unlock, _post({}), file_key)
        assert ok["status"] == 200
        assert ok["body"]["unlocked"] is True
        assert len(ok["body"]["token"]) == 48
        assert ok["body"]["expires_in"] == 1800


async def test_web_token_scope_and_expiry():
    """令牌绑定 (file_key, owner)：跨用户无效、30 分钟后过期、回退旧 pw 仍可用。"""
    if env.is_baseline_only():
        return
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed(inst)
        _seed(inst, sender=OTHER, name="阿堵")
        _protect(inst, SENDER, PW)
        _protect(inst, OTHER, "otherpw")
        key_a = plugin._diary_file_key(SENDER)
        key_b = plugin._diary_file_key(OTHER)

        ok = await _call(plugin.web_unlock, _post({"pw": PW}), key_a)
        token = ok["body"]["token"]

        cross = await _call(
            plugin.web_read_day, FakeWebRequest(query={"tk": token}), key_b, "20261023"
        )
        assert cross["body"] == {"need_password": True}

        same = await _call(
            plugin.web_read_day, FakeWebRequest(query={"tk": token}), key_a, "20261023"
        )
        assert same["status"] == 200

        # TTL 1800s：过期后令牌失效，读接口回 need_password（前端据此重弹密码框）
        inst.set_now(FIXED_NOW + timedelta(seconds=1801))
        expired = await _call(
            plugin.web_read_day, FakeWebRequest(query={"tk": token}), key_a, "20261023"
        )
        assert expired["status"] == 200 and expired["body"] == {"need_password": True}
        # 下载类接口过期后保持既有 403 PASSWORD_REQUIRED 形态
        expired_export = await _call(
            plugin.web_export, FakeWebRequest(query={"tk": token}), key_a
        )
        assert expired_export["status"] == 403
        assert expired_export["body"]["message"] == "PASSWORD_REQUIRED"

        legacy = await _call(
            plugin.web_read_day, FakeWebRequest(query={"pw": PW}), key_a, "20261023"
        )
        assert legacy["status"] == 200


async def test_web_token_capacity_is_bounded():
    """令牌表有上限（256），超出时丢最旧的；值形如 {file_key, owner, exp}。"""
    if env.is_baseline_only():
        return
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        file_key = "0123456789abcdef"
        first = plugin._web_issue_token(SENDER, file_key)
        for _ in range(plugin._WEB_TOKEN_MAX * 2):
            plugin._web_issue_token(SENDER, file_key)
        store = plugin._web_tokens
        assert len(store) == plugin._WEB_TOKEN_MAX
        assert first not in store
        rec = next(iter(store.values()))
        assert set(rec) == {"file_key", "owner", "exp"}
        assert plugin._WEB_TOKEN_TTL == 1800


async def test_web_unlock_old_zip_owner_then_password():
    """旧 zip（说明.txt 没有用户ID）：先 need_owner，带 qq 后一次成功。"""
    if env.is_baseline_only():
        return
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed(inst)
        _protect(inst, SENDER, PW)
        file_key = plugin._diary_file_key(SENDER)
        path = inst.diary_path(SENDER)
        entries = compare.zip_entries(path)
        entries["说明.txt"] = entries["说明.txt"].replace(
            f"用户ID：{SENDER}\n".encode("utf-8"), b""
        )
        plugin._write_zip_atomic(path, entries)

        async def unlock(payload):
            return await _call(plugin.web_unlock, _post(payload), file_key)

        need_owner = await unlock({"pw": PW})
        wrong_qq = await unlock({"pw": PW, "qq": "99999"})
        right_qq = await unlock({"pw": PW, "qq": SENDER})
        again = await unlock({"pw": PW})

        assert need_owner["status"] == 200
        assert need_owner["body"] == {"need_owner": True}
        assert wrong_qq["status"] == 400
        assert right_qq["status"] == 200 and right_qq["body"]["unlocked"] is True
        assert plugin._load_owner_map().get(file_key) == SENDER
        # 主人 Q 解析成功后会记进 owner_map，再解锁不必重复输
        assert again["status"] == 200


# ---------------------------------------------------------------------------
# P3-23：X-Forwarded-For 可被伪造 → 本机判定只信最后一跳
# ---------------------------------------------------------------------------


async def test_web_local_panel_trusts_last_hop_only():
    """首项伪造 127.0.0.1 不再骗过本机判定；真实远程面板不能触发本机落盘。"""
    if env.is_baseline_only():
        return  # P3-23 只改 refactor 侧
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin

        def is_local(req) -> bool:
            with bind_request_context(req):
                return plugin._web_is_local_panel()

        assert is_local(FakeWebRequest()) is True
        assert is_local(FakeWebRequest(client_host="10.0.0.5")) is False
        assert is_local(FakeWebRequest(forwarded_for="203.0.113.9")) is False
        # 核心回归：首项伪造成回环、真实来源在最后一跳 → 必须按远程处理
        assert (
            is_local(FakeWebRequest(forwarded_for="127.0.0.1, 203.0.113.9"))
            is False
        )
        # 同机代理转发本机客户端：最后一跳是回环 → 仍可用
        assert (
            is_local(FakeWebRequest(forwarded_for="203.0.113.9, 127.0.0.1")) is True
        )
        # x-real-ip（无 XFF 时）同样只认代理给出的真实来源
        real_remote = FakeWebRequest()
        real_remote.headers["x-real-ip"] = "203.0.113.9"
        assert is_local(real_remote) is False
        real_local = FakeWebRequest()
        real_local.headers["x-real-ip"] = "127.0.0.1"
        assert is_local(real_local) is True

        # 端到端：伪造首项的远程面板不能触发「一键备份到本机」
        _seed(inst)
        forged = await _call(
            plugin.web_export_all_local,
            FakeWebRequest(forwarded_for="127.0.0.1, 203.0.113.9"),
        )
        assert forged["status"] == 400
        assert "REMOTE_PANEL" in forged["body"]["message"]


# ---------------------------------------------------------------------------
# P1-5：产品口径（用户拍板）——「一键备份全部」是管理员信任域例外，不加密码闸
# ---------------------------------------------------------------------------


async def test_web_export_all_not_behind_view_password():
    """整机备份不受查看密码限制；单用户导出仍受保护（锁死 P1-5 的口径）。"""
    if env.is_baseline_only():
        return  # export_all 只存在于 refactor 侧
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed(inst)
        _protect(inst, SENDER, PW)
        file_key = plugin._diary_file_key(SENDER)

        backup = await _call(plugin.web_export_all, FakeWebRequest())
        single = await _call(plugin.web_export, FakeWebRequest(), file_key)

        assert backup["kind"] == "FileResponse"
        assert single["status"] == 403
        assert single["body"]["message"] == "PASSWORD_REQUIRED"
