# -*- coding: utf-8 -*-
"""Web API differential tests (fake request bound into AstrBot's request context)."""

from __future__ import annotations

import base64
import io
import json
import re
import subprocess
from pathlib import Path
from unittest import mock
import zipfile

from astrbot.api.web import bind_request_context

from harness import compare, env
from harness.fakes import FakeWebRequest, make_upload
from harness.world import DIARY_TEXT, World

SENDER = "10001"
SENDER_NAME = "阿罗"
OTHER = "20002"


def _json(resp) -> dict:
    return {
        "status": getattr(resp, "status_code", None),
        "body": json.loads(bytes(resp.body).decode("utf-8")),
    }


_BUNDLE_NAME_RE = re.compile(r"memory_diary_export_\d{8}_\d{6}_[0-9a-f]{4}\.zip")


def _scrub_bundle_name(text: str) -> str:
    return _BUNDLE_NAME_RE.sub("memory_diary_export_<STAMP>_<RANDOM>.zip", text or "")


def _file(resp) -> dict:
    headers = dict(getattr(resp, "headers", {}) or {})
    return {
        "status": getattr(resp, "status_code", None),
        "kind": type(resp).__name__,
        "exists": Path(resp.path).exists(),
        "filename": _scrub_bundle_name(headers.get("content-disposition", "")),
    }


def _bind(request):
    return bind_request_context(request)


def _bridge_unwrap(body):
    """复刻 AstrBot 插件视图 bridge 的响应解包规则。

    ``dashboard/src/views/PluginViewPage.vue`` 里：

        sendBridgeResponse(requestId, true, response.data?.data ?? response.data)

    也就是说响应体顶层只要出现 ``data`` 键，bridge 就把它当成信封，只把 ``data``
    交给 iframe 里的页面。插件自己的 JSON 字段叫 ``data`` 会被这层吞掉，
    页面拿到的就不是 ``{found, mime, ...}`` 而是字段值本身。
    """
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _seed_diary(inst, sender: str = SENDER, name: str = SENDER_NAME):
    inst.plugin._save_diary(
        sender, DIARY_TEXT, sender_name=name, origin="private", mode="full"
    )


async def test_web_list_users_and_days():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            empty_users = await _call(plugin.web_list_users, FakeWebRequest())
            _seed_diary(inst)
            users = await _call(plugin.web_list_users, FakeWebRequest())
            file_key = users["body"]["users"][0]["file_key"]
            days_ok = await _call(
                plugin.web_list_days, FakeWebRequest(), file_key
            )
            days_bad_key = await _call(
                plugin.web_list_days, FakeWebRequest(), "../../etc"
            )
            days_missing = await _call(
                plugin.web_list_days, FakeWebRequest(), "0123456789abcdef"
            )
            return {
                "empty_users": empty_users,
                "user_count": len(users["body"]["users"]),
                "user_fields": {
                    k: users["body"]["users"][0][k]
                    for k in ("owner_known", "day_count", "latest", "nickname")
                },
                "days_ok": days_ok,
                "days_bad_key": days_bad_key,
                "days_missing": days_missing,
            }

        pair = await world.run_pair(scenario, label="web list")
        value = pair.value
        assert value["empty_users"]["body"] == {"users": []}
        assert value["user_count"] == 1
        assert value["user_fields"]["owner_known"] is True
        assert value["user_fields"]["day_count"] == 1
        assert value["days_ok"]["body"]["days"][0]["day"] == "20261023"
        assert value["days_bad_key"]["status"] == 400
        assert value["days_missing"]["status"] == 404


async def _call(handler, request, *args):
    with _bind(request):
        response = await handler(*args)
    if isinstance(response, (dict,)) or response is None:
        return response
    if type(response).__name__ == "FileResponse":
        return _file(response)
    return _json(response)


async def test_web_read_day_and_owner_resolution():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            _seed_diary(inst)
            file_key = plugin._diary_file_key(SENDER)
            ok = await _call(
                plugin.web_read_day,
                FakeWebRequest(query={"qq": SENDER}),
                file_key,
                "20261023",
            )
            missing_day = await _call(
                plugin.web_read_day, FakeWebRequest(), file_key, "20261001"
            )
            bad_day = await _call(
                plugin.web_read_day, FakeWebRequest(), file_key, "23-10"
            )
            missing_file = await _call(
                plugin.web_read_day,
                FakeWebRequest(),
                "0123456789abcdef",
                "20261023",
            )

            # Remove the 用户ID line from 说明.txt to simulate an old zip whose
            # owner must be confirmed by trying the QQ hint.
            path = inst.diary_path(SENDER)
            entries = compare.zip_entries(path)
            entries["说明.txt"] = entries["说明.txt"].replace(
                f"用户ID：{SENDER}\n".encode("utf-8"), b""
            )
            plugin._write_zip_atomic(path, entries)
            need_owner = await _call(
                plugin.web_read_day, FakeWebRequest(), file_key, "20261023"
            )
            wrong_qq = await _call(
                plugin.web_read_day,
                FakeWebRequest(query={"qq": "99999"}),
                file_key,
                "20261023",
            )
            right_qq = await _call(
                plugin.web_read_day,
                FakeWebRequest(query={"qq": SENDER}),
                file_key,
                "20261023",
            )
            owner_map = await _call(
                plugin.web_read_day, FakeWebRequest(), file_key, "20261023"
            )
            return {
                "ok": ok,
                "missing_day": missing_day,
                "bad_day": bad_day,
                "missing_file": missing_file,
                "need_owner": need_owner,
                "wrong_qq": wrong_qq,
                "right_qq": right_qq,
                "owner_map_used": owner_map,
                "owner_map_file": plugin._load_owner_map(),
            }

        pair = await world.run_pair(scenario, label="web read day")
        value = pair.value
        assert value["ok"]["status"] == 200
        assert value["ok"]["body"]["content"].startswith("—— 20:30")
        assert value["missing_day"]["body"]["content"] == ""
        assert value["bad_day"]["status"] == 400
        assert value["missing_file"]["status"] == 404
        assert value["need_owner"]["body"] == {"need_owner": True}
        assert value["wrong_qq"]["status"] == 400
        assert value["right_qq"]["status"] == 200
        assert value["owner_map_used"]["status"] == 200


async def test_web_password_gate_for_read_and_export():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            _seed_diary(inst)
            plugin._save_pref_data(
                SENDER,
                {"view_password": plugin._make_view_password("secret123")},
            )
            file_key = plugin._diary_file_key(SENDER)
            no_pw = await _call(
                plugin.web_read_day, FakeWebRequest(), file_key, "20261023"
            )
            wrong_pw = await _call(
                plugin.web_read_day,
                FakeWebRequest(query={"pw": "nope"}),
                file_key,
                "20261023",
            )
            right_pw = await _call(
                plugin.web_read_day,
                FakeWebRequest(query={"pw": "secret123"}),
                file_key,
                "20261023",
            )
            export_no_pw = await _call(
                plugin.web_export, FakeWebRequest(), file_key
            )
            export_wrong = await _call(
                plugin.web_export, FakeWebRequest(query={"pw": "bad"}), file_key
            )
            export_ok = await _call(
                plugin.web_export,
                FakeWebRequest(query={"pw": "secret123"}),
                file_key,
            )
            return {
                "no_pw": no_pw,
                "wrong_pw": wrong_pw,
                "right_pw": right_pw,
                "export_no_pw": export_no_pw,
                "export_wrong": export_wrong,
                "export_ok": export_ok,
                "users_protected": (
                    await _call(plugin.web_list_users, FakeWebRequest())
                )["body"]["users"][0]["protected"],
            }

        pair = await world.run_pair(
            scenario,
            label="web password",
            snapshot_exclude=("memory_diary_export_*.zip",),
            snapshot_normalise=_password_normaliser(),
        )
        value = pair.value
        assert value["no_pw"]["body"] == {"need_password": True}
        assert value["wrong_pw"]["status"] == 403
        assert value["right_pw"]["status"] == 200
        assert value["export_no_pw"]["status"] == 403
        assert value["export_wrong"]["status"] == 403
        assert value["export_ok"]["kind"] == "FileResponse"
        assert value["users_protected"] is True


def _password_normaliser():
    def normalise(relpath: str, raw: bytes) -> bytes:
        if not relpath.startswith("pref_"):
            return raw
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return raw
        rec = data.get("view_password")
        if isinstance(rec, dict):
            data["view_password"] = {
                "algo": rec.get("algo"),
                "iter": rec.get("iter"),
                "salt": "<SALT>",
                "hash": "<HASH>",
            }
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    return normalise


async def test_web_export_bundle_and_local_export():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            _seed_diary(inst)
            file_key = plugin._diary_file_key(SENDER)
            export = await _call(plugin.web_export, FakeWebRequest(), file_key)
            # Local export: redirect Path.home() to a per-instance temp dir so the
            # test never touches the real Downloads folder; Popen is stubbed.
            home = inst.root / "home"
            home.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(Path, "home", classmethod(lambda cls: home)):
                with mock.patch.object(subprocess, "Popen", lambda *a, **k: None):
                    local = await _call(
                        plugin.web_export_local, FakeWebRequest(), file_key
                    )
            saved = home / local["body"]["filename"] if local.get("body") else None
            local_info = {
                "status": local["status"],
                "saved_exists": bool(saved and saved.exists()),
                "filename": _scrub_bundle_name(
                    local["body"].get("filename") if local.get("body") else ""
                ),
            }
            leftovers = sorted(
                _scrub_bundle_name(p.name)
                for p in inst.data_dir.iterdir()
                if "memory_diary_export_" in p.name
            )
            return {
                "export": export,
                "local": local_info,
                "leftovers": leftovers,
            }

        pair = await world.run_pair(
            scenario,
            label="web export",
            snapshot_exclude=("memory_diary_export_*.zip",),
        )
        value = pair.value
        assert value["export"]["kind"] == "FileResponse"
        assert "memory_diary_export_" in value["export"]["filename"]
        assert value["local"]["status"] == 200
        assert value["local"]["saved_exists"] is True
        if not env.is_baseline_only():
            # 远程 / 反向代理转发必须被拒绝（baseline 老实现误读 remote_addr，
            # 这里只对 refactor 侧断言；见 _web_is_local_panel）。
            refactor = world.refactor.plugin
            refactor_key = refactor._diary_file_key(SENDER)
            remote = await _call(
                refactor.web_export_local,
                FakeWebRequest(client_host="10.0.0.5"),
                refactor_key,
            )
            proxied = await _call(
                refactor.web_export_local,
                FakeWebRequest(
                    client_host="127.0.0.1", forwarded_for="203.0.113.9"
                ),
                refactor_key,
            )
            for resp in (remote, proxied):
                assert resp["status"] == 400
                assert "REMOTE_PANEL" in resp["body"]["message"]


async def test_web_export_all_local_saves_to_downloads_and_opens():
    """一键备份全部日记也能「存到本机并打开位置」（仅本机面板）。"""
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name=SENDER_NAME, origin="private", mode="full"
        )
        plugin._save_diary(
            OTHER, DIARY_TEXT, sender_name="阿堵", origin="private", mode="full"
        )
        # 把 Path.home() 指向临时目录，绝不动真实「下载」文件夹；Popen 也打桩
        home = inst.root / "home"
        (home / "Downloads").mkdir(parents=True, exist_ok=True)
        started = []
        with mock.patch.object(Path, "home", classmethod(lambda cls: home)):
            with mock.patch.object(
                subprocess, "Popen", lambda *a, **k: started.append(a)
            ):
                local = await _call(
                    plugin.web_export_all_local, FakeWebRequest()
                )
                remote = await _call(
                    plugin.web_export_all_local,
                    FakeWebRequest(client_host="10.0.0.5"),
                )

        assert local["status"] == 200
        target = Path(local["body"]["saved_to"])
        assert target.exists(), "备份包应真的落到本机下载目录"
        assert target.parent == home / "Downloads"
        assert started, "保存后应尝试打开文件位置"
        with zipfile.ZipFile(target) as z:
            names = set(z.namelist())
        assert plugin._diary_path(SENDER).name in names
        assert plugin._diary_path(OTHER).name in names
        assert "备份说明.txt" in names
        assert not any("key" in n.lower() for n in names), "备份包绝不能含钥匙"
        # 数据目录里不残留临时备份包（已移走）
        assert not list(inst.data_dir.glob("memory_diary_backup_*.zip"))
        # 远程面板不允许触发本机文件操作
        assert remote["status"] == 400
        assert "REMOTE_PANEL" in remote["body"]["message"]


async def test_web_import_round_trip_and_errors():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            _seed_diary(inst)
            bundle = plugin._build_export_bundle(
                SENDER, inst.diary_path(SENDER), inst.entries(SENDER)
            )
            bundle_bytes = bundle.read_bytes()
            plugin._delete_diary(SENDER)

            bad_upload = await _call(
                plugin.web_import,
                FakeWebRequest(
                    uploads={"file": make_upload(b"not a zip", "bad.zip")}
                ),
            )
            missing_upload = await _call(
                plugin.web_import, FakeWebRequest(uploads={})
            )
            invalid_buf = io.BytesIO()
            with zipfile.ZipFile(invalid_buf, "w") as invalid_zip:
                invalid_zip.writestr("hello.txt", b"not a migration bundle")
            bad_format = await _call(
                plugin.web_import,
                FakeWebRequest(
                    uploads={
                        "file": make_upload(invalid_buf.getvalue(), "invalid.zip")
                    }
                ),
            )
            # The bundle's inner diary was built from the still-exporter's state;
            # delete local diary first (done above) so install succeeds.
            imported = await _call(
                plugin.web_import,
                FakeWebRequest(
                    uploads={"file": make_upload(bundle_bytes, "bundle.zip")}
                ),
            )
            entries = inst.entries(SENDER)
            content = plugin._load_latest(SENDER)[0]
            return {
                "bad_upload": bad_upload,
                "missing_upload": missing_upload,
                "bad_format": bad_format,
                "imported": imported,
                "entries": entries,
                "content": content,
                "tmp_leftovers": sorted(
                    p.name for p in inst.data_dir.iterdir() if p.name.startswith("import_tmp_")
                ),
            }

        pair = await world.run_pair(
            scenario,
            label="web import",
            snapshot_exclude=("memory_diary_export_*.zip",),
        )
        value = pair.value
        assert value["bad_upload"]["status"] == 500
        assert value["missing_upload"]["status"] == 400
        assert value["bad_format"]["status"] == 400
        assert "导入被拒绝" in value["bad_format"]["body"]["message"]
        assert "manifest.json" in value["bad_format"]["body"]["message"]
        assert value["imported"]["status"] == 200
        assert value["imported"]["body"]["imported"] == 1
        assert value["entries"] == ["diaries/20261023.md"]
        assert value["content"].startswith("—— 20:30")
        assert value["tmp_leftovers"] == []


async def test_web_view_prefs_and_images():
    async with World(config={"use_persona": False}) as world:
        async def scenario(inst):
            plugin = inst.plugin
            defaults = await _call(plugin.web_get_prefs, FakeWebRequest(username="admin"))
            saved = await _call(
                plugin.web_save_prefs,
                FakeWebRequest(
                    username="admin",
                    payload={
                        "prefs": {
                            "font_size": 99,
                            "font_family": "kai",
                            "paper": "custom",
                            "backdrop": "default",
                            "evil": "ignored",
                        }
                    },
                ),
            )
            after = await _call(plugin.web_get_prefs, FakeWebRequest(username="admin"))
            invalid = await _call(
                plugin.web_save_prefs,
                FakeWebRequest(username="admin", payload={"prefs": "nope"}),
            )

            no_paper = await _call(plugin.web_get_paper, FakeWebRequest())
            png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
            up = await _call(
                plugin.web_upload_paper,
                FakeWebRequest(
                    uploads={"file": make_upload(png, "paper.png", "image/png")}
                ),
            )
            paper = await _call(plugin.web_get_paper, FakeWebRequest())
            wrong_mime = await _call(
                plugin.web_upload_paper,
                FakeWebRequest(
                    uploads={"file": make_upload(b"x", "x.gif", "image/gif")}
                ),
            )
            too_big = await _call(
                plugin.web_upload_backdrop,
                FakeWebRequest(
                    uploads={
                        "file": make_upload(
                            b"x" * (3 * 1024 * 1024 + 1), "big.png", "image/png"
                        )
                    }
                ),
            )
            no_file = await _call(plugin.web_upload_backdrop, FakeWebRequest())
            paper_body = paper["body"]
            # 新实现把字段叫 image（避开 bridge 信封），baseline 老实现叫 data。
            # 差分对比用归一化后的值，新字段约定在 refactor 分支单独断言。
            paper_image = paper_body.get("image")
            if paper_image is None:
                paper_image = paper_body.get("data")
            if inst.side == env.SIDE_REFACTOR and not env.is_baseline_only():
                assert "data" not in paper_body, (
                    "响应顶层不能出现 data：AstrBot 视图 bridge 会把它当信封解包"
                )
                assert "image" in paper_body, "自定义图片字段应为 image"
            return {
                "defaults": defaults,
                "saved": saved,
                "after": after,
                "invalid": invalid,
                "no_paper": no_paper,
                "up": up,
                "paper": {
                    "status": paper["status"],
                    "found": paper_body.get("found"),
                    "mime": paper_body.get("mime"),
                    "image": paper_image,
                },
                "wrong_mime": wrong_mime,
                "too_big": too_big,
                "no_file": no_file,
                "paper_files": sorted(
                    p.name for p in inst.data_dir.iterdir() if p.name.startswith("web_paper")
                ),
            }

        pair = await world.run_pair(scenario, label="web prefs/images")
        value = pair.value
        assert value["defaults"]["body"]["prefs"] == {}
        assert value["defaults"]["body"]["brand"] == "TA"
        assert value["saved"]["body"]["prefs"]["font_size"] == 22
        assert "evil" not in value["saved"]["body"]["prefs"]
        assert value["after"]["body"]["prefs"]["font_family"] == "kai"
        assert value["invalid"]["status"] == 400
        assert value["no_paper"]["body"] == {"found": False}
        assert value["paper"] == {
            "status": 200,
            "found": True,
            "mime": "image/png",
            "image": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode("ascii"),
        }
        assert value["wrong_mime"]["status"] == 400
        assert value["too_big"]["status"] == 400
        assert value["no_file"]["status"] == 400
        assert value["paper_files"] == ["web_paper.img", "web_paper.json"]


async def test_web_image_payload_survives_bridge_unwrap():
    """图片接口的响应经 bridge 解包后仍带着 found/mime/image。

    AstrBot 的 ``PluginViewPage.vue`` 用 ``response.data?.data ?? response.data``
    解包插件响应：顶层一旦有 ``data``，iframe 里拿到的就只是 ``data`` 的值。
    纸张/背景板接口的 base64 字段若叫 data，前端就变成 ``r = "<base64>"``，
    ``r.found`` 为 undefined → 页面按「没有自定义图片」处理（上传看似没反应）。
    """
    if env.is_baseline_only():
        return  # 新字段约定只存在于 refactor 侧
    async with World(config={"use_persona": False}) as world:
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 32

        async def scenario(inst):
            plugin = inst.plugin
            await _call(
                plugin.web_upload_paper,
                FakeWebRequest(
                    uploads={"file": make_upload(png, "paper.png", "image/png")}
                ),
            )
            return await _call(plugin.web_get_paper, FakeWebRequest())

        pair = await world.run_pair(
            scenario, compare_value=False, label="image bridge contract"
        )
        body = pair.refactor["body"]
        assert "data" not in body
        payload = _bridge_unwrap(body)
        assert payload["found"] is True
        assert payload["mime"] == "image/png"
        assert base64.b64decode(payload["image"]) == png


# ---------------------------------------------------------------------------
# P2-2 第 4 点：一键备份全部用户日记（/export_all）
#   该接口是 v0.5.5 新特性，baseline 不存在，因此只驱动 refactor 侧。
# ---------------------------------------------------------------------------


async def _call_raw(handler, request, *args):
    """与 _call 相同地绑定 request context，但返回原始响应对象。

    ``FileResponse`` 需要读 ``resp.path`` 才能检查包内容，_call 会把它转成
    dict 摘要、丢掉 path。
    """
    with _bind(request):
        return await handler(*args)


async def test_web_export_all_backup_bundle():
    if env.is_baseline_only():
        return  # 新接口只存在于 refactor 侧
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin

        # 还没有任何日记：必须 400，不能给一个空包
        empty = await _call_raw(plugin.web_export_all, FakeWebRequest())
        assert getattr(empty, "status_code", None) == 400

        _seed_diary(inst)
        _seed_diary(inst, OTHER, "阿堵")
        # P3-16：构造真实命名的临时残留 diary_<16hex>.zip.tmp-<pid>，不应入包
        stray = inst.data_dir / "diary_0123456789abcdef.zip.tmp-4242"
        stray.write_text("x", encoding="utf-8")
        assert stray.exists()

        resp = await _call_raw(plugin.web_export_all, FakeWebRequest())
        assert type(resp).__name__ == "FileResponse"
        path = Path(resp.path)
        assert path.exists()
        assert path.name.startswith("memory_diary_backup_")
        assert path.name.endswith(".zip")
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            readme = z.read("备份说明.txt").decode("utf-8")
        expected = {
            plugin._diary_path(SENDER).name,
            plugin._diary_path(OTHER).name,
            "备份说明.txt",
        }
        assert names == expected
        # 绝不包含主密钥/签名钥匙；临时残留也不入包
        assert not any("key" in n.lower() for n in names)
        assert stray.name not in names
        assert not any(".zip.tmp-" in n for n in names)
        # 备份说明：份数 + 整机迁移要连同 .diary_key 一起备份
        assert "用户份数：2 份" in readme
        assert ".diary_key" in readme

        # 路由已注册（否则前端按钮点了没反应）
        routes = [r[0] for r in inst.ctx.registered_web_apis]
        assert any(r.endswith("/export_all") for r in routes)


# ---------------------------------------------------------------------------
# v0.6.1 ②：Web 日记本批量删除（delete_days）
#   新接口只存在于 refactor 侧，因此只驱动 refactor；删除本身的级联/原子语义
#   由 storage 的差分测试兜底，这里验"接口把参数、主人、密码闸接对了"。
# ---------------------------------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _post(payload, query=None):
    return FakeWebRequest(method="POST", payload=payload, query=query)


def _seed_days(inst, days, sender: str = SENDER):
    for day in days:
        inst.plugin._save_diary(
            sender,
            f"{day} 的日记。",
            sender_name=SENDER_NAME,
            entry_name_hint=day,
            origin="private",
            mode="full",
        )


async def test_web_delete_days_batch_and_cascade():
    """删 2 天：这两天及其插图/批注/签名全消失，其余天验签通过；删光后 zip 消失。"""
    if env.is_baseline_only():
        return  # 新接口只存在于 refactor 侧
    async with World(config={"use_persona": False, "annotation_enabled": True}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed_days(inst, ["20261021", "20261022", "20261023"])
        # 插图与批注都锚在 20261021/20261022：删除时要级联清掉，不留孤儿
        plugin.save_image(SENDER, _PNG, day="20261021", seq=1, desc="江边")
        plugin.save_image(SENDER, _PNG, day="20261023", seq=1, desc="路灯")
        plugin.save_annotations(
            SENDER,
            {
                "version": 1,
                "last_round": "r_20261023_203000_ab12",
                "last_title": "页边的话",
                "updated_at": "2026-10-23T20:30:00",
                "annotations": [
                    {
                        "id": "ann_20261021_1",
                        "round": "r_20261023_203000_ab12",
                        "anchor": "20261021 的日记",
                        "entry": "20261021.md",
                        "created_at": "2026-10-23T20:30:00",
                        "note": "第一天的批注。",
                        "is_question": False,
                        "status": "open",
                        "reply": None,
                    },
                    {
                        "id": "ann_20261022_2",
                        "round": "r_20261023_203000_ab12",
                        "anchor": "20261022 的日记",
                        "entry": "20261022.md",
                        "created_at": "2026-10-23T20:30:00",
                        "note": "第二天的批注。",
                        "is_question": False,
                        "status": "open",
                        "reply": None,
                    },
                ],
            },
        )
        file_key = plugin._diary_file_key(SENDER)

        # 同时接受 YYYY-MM-DD 与 YYYYMMDD；重复日期去重（deleted 不虚报）
        resp = await _call(
            plugin.web_delete_days,
            _post({
                "file_key": file_key,
                "days": ["2026-10-21", "20261021", "20261022"],
            }),
        )

        path = plugin._diary_path(SENDER)
        entries = compare.zip_entries(path)
        remaining_entries = sorted(
            n for n in entries if plugin.ENTRY_NAME_RE.match(n)
        )
        images = plugin.list_images(SENDER)
        ann_21 = plugin.annotations_web_payload(SENDER, "20261021")
        ann_22 = plugin.annotations_web_payload(SENDER, "20261022")
        content_23, tampered_23 = plugin._load_entry(
            SENDER, plugin._md_entry("20261023")
        )
        days_resp = await _call(plugin.web_list_days, FakeWebRequest(), file_key)

        assert resp["status"] == 200
        assert resp["body"] == {"deleted": 2, "remaining": 1, "missing": 0}
        assert remaining_entries == ["diaries/20261023.md"]
        assert not any("20261021" in n or "20261022" in n for n in entries), (
            "被删两天的签名/插图/批注条目必须一起消失"
        )
        assert images == ["images/20261023_1_路灯.png"]
        assert ann_21 == ([], False) and ann_22 == ([], False)
        assert "20261023 的日记" in content_23 and tampered_23 is False
        assert days_resp["body"]["days"] == [
            {"day": "20261023", "label": "2026-10-23"}
        ]

        # 删除最后一天：用户日记 zip 整体删除，用户列表里不再出现
        last = await _call(
            plugin.web_delete_days,
            _post({"file_key": file_key, "days": ["20261023"]}),
        )
        users = await _call(plugin.web_list_users, FakeWebRequest())
        assert last["body"] == {"deleted": 1, "remaining": 0, "missing": 0}
        assert not path.exists()
        assert users["body"] == {"users": []}

        # 路由已注册（否则前端按钮点了没反应）
        routes = [r[0] for r in inst.ctx.registered_web_apis]
        assert any(r.endswith("/delete_days") for r in routes)


async def test_web_list_users_exposes_owner_qq():
    """v0.6.1：用户列表返回主人 QQ（界面下拉框显示用，baseline 无此字段）。

    昵称可以是纯不可见字符，只显示昵称时下拉框一片空白、分不清谁是谁；
    前端拿到 owner 后显示「昵称（QQ xxx · N 天）」，隐形昵称降级成「QQ xxx」。
    """
    if env.is_baseline_only():
        return  # owner 字段只存在于 refactor 侧
    from image_testkit import rewrite_zip, zip_entries
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed_diary(inst)
        users = await _call(plugin.web_list_users, FakeWebRequest())
        u = users["body"]["users"][0]
        assert u["owner"] == SENDER
        assert u["owner_known"] is True
        # 旧 zip（无 说明.txt）未解析出主人时 owner 为空串而不是缺键，
        # 前端按空串走「（未知昵称）（N 天）」旧文案
        _seed_diary(inst, sender=OTHER)
        path = plugin._diary_path(OTHER)
        entries = zip_entries(path)
        del entries[plugin.README_ZIP_ENTRY]
        rewrite_zip(path, entries)
        users2 = await _call(plugin.web_list_users, FakeWebRequest())
        u2 = next(x for x in users2["body"]["users"]
                  if x["file_key"] == plugin._diary_file_key(OTHER))
        assert u2["owner"] == "" and u2["owner_known"] is False


async def test_web_delete_days_validation_owner_and_password():
    """参数校验、旧 zip 主人解析、查看密码闸，以及"选了不存在的日期"的口径。"""
    if env.is_baseline_only():
        return  # 新接口只存在于 refactor 侧
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        _seed_days(inst, ["20261021", "20261022", "20261023"])
        file_key = plugin._diary_file_key(SENDER)

        async def call(payload):
            return await _call(plugin.web_delete_days, _post(payload))

        bad_key = await call({"file_key": "../../etc", "days": ["20261023"]})
        missing_file = await call({
            "file_key": plugin._diary_file_key(OTHER),
            "days": ["20261023"],
        })
        no_days = await call({"file_key": file_key})
        not_list = await call({"file_key": file_key, "days": "20261023"})
        bad_date = await call({"file_key": file_key, "days": ["2026-13-01"]})
        junk_date = await call({"file_key": file_key, "days": ["not-a-date"]})
        too_many = await call({
            "file_key": file_key,
            "days": [f"2026{i // 280 + 1:02d}{i % 28 + 1:02d}" for i in range(367)],
        })
        unknown_day = await call({
            "file_key": file_key,
            "days": ["20260101"],
        })
        assert bad_key["status"] == 400
        assert missing_file["status"] == 404
        assert no_days["status"] == 400
        assert not_list["status"] == 400
        assert bad_date["status"] == 400
        assert junk_date["status"] == 400
        assert too_many["status"] == 400
        assert unknown_day["body"] == {"deleted": 0, "remaining": 3, "missing": 1}

        # 查看密码闸：POST 的密码放在请求体里
        plugin._save_pref_data(
            SENDER, {"view_password": plugin._make_view_password("secret123")}
        )
        need_pw = await call({"file_key": file_key, "days": ["20261021"]})
        wrong_pw = await call({
            "file_key": file_key,
            "days": ["20261021"],
            "pw": "nope",
        })
        right_pw = await call({
            "file_key": file_key,
            "days": ["20261021"],
            "pw": "secret123",
        })
        assert need_pw["body"] == {"need_password": True}
        assert wrong_pw["status"] == 403
        assert right_pw["body"] == {"deleted": 1, "remaining": 2, "missing": 0}

        # 旧 zip（说明.txt 没有主人 ID）：先要 QQ 提示，验签通过才删
        path = plugin._diary_path(SENDER)
        entries = compare.zip_entries(path)
        entries["说明.txt"] = entries["说明.txt"].replace(
            f"用户ID：{SENDER}\n".encode("utf-8"), b""
        )
        plugin._write_zip_atomic(path, entries)
        need_owner = await call({
            "file_key": file_key,
            "days": ["20261022"],
            "pw": "secret123",
        })
        wrong_qq = await call({
            "file_key": file_key,
            "days": ["20261022"],
            "pw": "secret123",
            "qq": "99999",
        })
        right_qq = await call({
            "file_key": file_key,
            "days": ["20261022"],
            "pw": "secret123",
            "qq": SENDER,
        })
        assert need_owner["body"] == {"need_owner": True}
        assert wrong_qq["status"] == 400
        assert right_qq["body"] == {"deleted": 1, "remaining": 1, "missing": 0}
        assert plugin._load_owner_map().get(file_key) == SENDER
        # 剩下那天仍能正常验签读取
        content, tampered = plugin._load_entry(
            SENDER, plugin._md_entry("20261023")
        )
        assert "20261023 的日记" in content and tampered is False


async def test_web_delete_days_export_delete_import_round_trip():
    """验收项：导出备份 → 全部删除 → 重新导入，数据与图片逐字节回到原样。"""
    if env.is_baseline_only():
        return  # 新接口只存在于 refactor 侧
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        days = ["20261021", "20261022"]
        _seed_days(inst, days)
        plugin.save_image(SENDER, _PNG, day="20261022", seq=1, desc="江边")
        image_name = plugin.list_images(SENDER)[0]
        before = {
            day: plugin._load_entry(SENDER, plugin._md_entry(day))[0]
            for day in days
        }
        bundle = plugin._build_export_bundle(
            SENDER, plugin._diary_path(SENDER), inst.entries(SENDER)
        )
        bundle_bytes = bundle.read_bytes()
        bundle.unlink(missing_ok=True)

        file_key = plugin._diary_file_key(SENDER)
        deleted = await _call(
            plugin.web_delete_days,
            _post({"file_key": file_key, "days": days}),
        )
        assert deleted["body"] == {"deleted": 2, "remaining": 0, "missing": 0}
        assert not plugin._diary_path(SENDER).exists()

        imported = await _call(
            plugin.web_import,
            FakeWebRequest(
                uploads={"file": make_upload(bundle_bytes, "bundle.zip")}
            ),
        )
        after = {
            day: plugin._load_entry(SENDER, plugin._md_entry(day))
            for day in days
        }
        image_data, image_status = plugin.load_image(SENDER, image_name)
        assert imported["status"] == 200
        assert imported["body"]["imported"] == len(days)
        assert sorted(plugin._list_entries(SENDER)) == [
            plugin._md_entry(day) for day in days
        ]
        for day in days:
            content, tampered = after[day]
            assert content == before[day], "删除后重新导入的正文必须与原文件一致"
            assert tampered is False, "重新导入的日记必须能通过验签"
        assert image_data == _PNG and image_status == "ok"


