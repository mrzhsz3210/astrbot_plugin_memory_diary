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
