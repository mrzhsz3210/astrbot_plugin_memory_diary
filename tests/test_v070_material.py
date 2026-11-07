# -*- coding: utf-8 -*-
"""v0.7.0 收口：外部素材 v2 签名队列与 AI 自主吸收。"""

from __future__ import annotations

import json
import zipfile

from astrbot.api.web import bind_request_context
from harness.fakes import FakeWebRequest
from harness.world import DIARY_TEXT, World, message

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"


async def _post_material(plugin, payload):
    request = FakeWebRequest(payload=payload, method="POST", path="/material")
    with bind_request_context(request):
        response = await plugin.web_external_material()
    return getattr(response, "status_code", None), json.loads(bytes(response.body).decode())


async def test_v2_credentials_queue_idempotently_and_cannot_legacy_write():
    async with World() as world:
        plugin = world.refactor.plugin
        token = plugin.create_external_token(SENDER, "bridge", scope="group:A")
        assert token
        payload = {"token": token, "source": "bridge", "text": "群A的外部事实", "ext_id": "e1"}
        status, body = await _post_material(plugin, payload)
        assert status == 200 and body["queued"] is True and body["status"] == "pending"
        replay_status, replay = await _post_material(plugin, payload)
        assert replay_status == 200 and replay["replayed"] is True
        data, state = plugin.load_external_materials(SENDER)
        assert state == "ok" and len(data["items"]) == 1
        assert data["items"][0]["scope"] == "group:A"
        assert plugin.external_material_batch(SENDER, "group:B") == []
        assert plugin.external_material_batch(SENDER, "private") == []
        assert len(plugin.external_material_batch(SENDER, "group:A")) == 1

        with bind_request_context(FakeWebRequest(payload={**payload, "day": "20261023"}, method="POST")):
            response = await plugin.web_external_note()
        assert response.status_code == 403
        assert json.loads(bytes(response.body).decode())["code"] == "LEGACY_DIRECT_FORBIDDEN"


async def test_ai_absorb_defer_discard_and_missing_decision_are_atomic():
    async with World(config={"use_persona": False, "current_self_enabled": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        for ext_id, text in (("a", "可信素材"), ("b", "以后再看"), ("c", "无关素材"), ("d", "缺少决定")):
            item, status = plugin.enqueue_external_material(SENDER, "bridge", ext_id, text)
            assert status == "queued"
        ids = {item["ext_id"]: item["id"] for item in plugin.load_external_materials(SENDER)[0]["items"]}
        decisions = {
            "decisions": [
                {"material_id": ids["a"], "action": "absorb"},
                {"material_id": ids["b"], "action": "defer", "reason": "以后"},
                {"material_id": ids["c"], "action": "discard", "reason": "无关"},
            ]
        }
        inst.set_history([message("user", "今天讨论发布")])
        inst.provider.replies[:] = [
            DIARY_TEXT + "\n<material_decisions>" + json.dumps(decisions) + "</material_decisions>"
        ]
        diary, err = await plugin._write_diary(UMO, SENDER, "阿罗", requested_mode="full")
        assert err is None
        assert "<material_decisions>" not in diary
        assert ids["a"] in diary and "source" not in decisions
        data, status = plugin.load_external_materials(SENDER)
        assert status == "ok"
        states = {item["ext_id"]: item["status"] for item in data["items"]}
        assert states == {"a": "absorbed", "b": "deferred", "c": "discarded", "d": "pending"}
        absorbed = next(item for item in data["items"] if item["ext_id"] == "a")
        assert absorbed["absorbed_entry"] == "diaries/20261023.md"
        with zipfile.ZipFile(plugin._diary_path(SENDER)) as z:
            raw = z.read(plugin.MATERIALS_ENTRY)
            actual = z.read(plugin.MATERIALS_SIG_ENTRY).decode().strip()
        assert plugin._materials_sig(SENDER, raw) == actual


async def test_queue_limit_retention_and_admin_actions():
    async with World(config={"external_material_retention_days": 30}) as world:
        plugin = world.refactor.plugin
        for index in range(100):
            _item, status = plugin.enqueue_external_material(SENDER, "bridge", str(index), "x")
            assert status == "queued"
        assert plugin.enqueue_external_material(SENDER, "bridge", "overflow", "x")[1] == "full"
        data = plugin.load_external_materials(SENDER)[0]
        data["items"][0]["submitted_at"] = "2026-09-01T00:00:00"
        assert plugin._write_external_materials(SENDER, data)
        assert plugin.expire_external_materials(SENDER) == 1
        expired = plugin.load_external_materials(SENDER)[0]["items"][0]
        assert expired["status"] == "expired" and expired["text"] == ""

        item, _ = plugin.enqueue_external_material(SENDER, "bridge", "retry", "待定")
        assert plugin.set_external_material_status(SENDER, item["id"], "discard")
        assert plugin.set_external_material_status(SENDER, item["id"], "retry") is False


async def test_material_queue_migration_roundtrip_and_tamper_rejection():
    async with World() as src_world, World() as dst_world:
        src = src_world.refactor.plugin
        src._save_diary(SENDER, DIARY_TEXT, origin="private", mode="full")
        item, status = src.enqueue_external_material(SENDER, "bridge", "migrate", "待迁移素材")
        assert status == "queued"
        bundle = src._build_export_bundle(SENDER, src._diary_path(SENDER), src._list_entries(SENDER))
        try:
            owner, key, entries, images, annotations, provenance, current_self, materials, anchor_archive = src._verify_bundle(bundle)
            assert materials is not None
            dst = dst_world.refactor.plugin
            dst._install_bundle(
                SENDER, owner, key, entries, images=images, annotations=annotations,
                provenance=provenance, current_self=current_self, materials=materials,
            )
            loaded, load_status = dst.load_external_materials(SENDER)
            assert load_status == "ok"
            assert loaded["items"][0]["text"] == "待迁移素材"
            replay, replay_status = dst.enqueue_external_material(
                SENDER, "bridge", "migrate", "不能重复入队"
            )
            assert replay_status == "replayed" and replay["text"] == "待迁移素材"
        finally:
            bundle.unlink(missing_ok=True)


async def test_deleted_material_replay_hits_tombstone_without_requeue():
    async with World() as world:
        plugin = world.refactor.plugin
        item, status = plugin.enqueue_external_material(SENDER, "bridge", "deleted", "待删素材")
        assert status == "queued"
        assert plugin._delete_diary(SENDER)
        replay, replay_status = plugin.enqueue_external_material(
            SENDER, "bridge", "deleted", "不得复活"
        )
        assert replay_status == "tombstone"
        assert replay["status"] == "discarded"
        data, load_status = plugin.load_external_materials(SENDER)
        assert load_status == "missing" and data["items"] == []


async def test_replay_queued_flag_matches_material_status_and_tombstone():
    async with World() as world:
        plugin = world.refactor.plugin
        token = plugin.create_external_token(SENDER, "bridge")
        statuses = ("pending", "deferred", "absorbed", "discarded", "expired")
        for status in statuses:
            item, enqueue_status = plugin.enqueue_external_material(
                SENDER, "bridge", status, f"{status} 原文"
            )
            assert enqueue_status == "queued"
            if status != "pending":
                data, load_status = plugin.load_external_materials(SENDER)
                assert load_status == "ok"
                target = next(x for x in data["items"] if x["id"] == item["id"])
                target["status"] = status
                target["status_at"] = "2026-10-23T20:30:00"
                if status == "absorbed":
                    target["absorbed_entry"] = "diaries/20261023.md"
                assert plugin._write_external_materials(SENDER, data)

        assert plugin.record_tombstones(SENDER, [{
            "source": "bridge", "ext_id": "tombstone", "day": "20261023"
        }])
        for status in (*statuses, "tombstone"):
            http_status, body = await _post_material(plugin, {
                "token": token,
                "source": "bridge",
                "text": f"{status} 原文",
                "ext_id": status,
            })
            assert http_status == 200 and body["ok"] is True
            assert body["replayed"] is True
            expected_status = "discarded" if status == "tombstone" else status
            assert body["status"] == expected_status
            assert body["queued"] is (status in ("pending", "deferred"))


async def test_large_material_is_a_complete_singleton_and_oversize_is_rejected():
    async with World() as world:
        plugin = world.refactor.plugin
        text = "汉" * 2500
        assert len(text.encode("utf-8")) == 7500
        item, status = plugin.enqueue_external_material(SENDER, "bridge", "large", text)
        assert status == "queued"
        batch = plugin.external_material_batch(SENDER, "private")
        assert len(batch) == 1 and batch[0]["id"] == item["id"]
        assert batch[0]["text"] == text

        too_large = "汉" * 4000
        rejected, status = plugin.enqueue_external_material(
            SENDER, "bridge", "too-large", too_large
        )
        assert rejected is None and status == "too_large"
        data, load_status = plugin.load_external_materials(SENDER)
        assert load_status == "ok" and len(data["items"]) == 1

        token = plugin.create_external_token(SENDER, "bridge")
        http_status, body = await _post_material(plugin, {
            "token": token,
            "source": "bridge",
            "text": too_large,
            "ext_id": "too-large-api",
        })
        assert http_status == 413 and body["code"] == "PAYLOAD_TOO_LARGE"
        assert len(plugin.load_external_materials(SENDER)[0]["items"]) == 1


async def test_normal_material_batch_budget_and_abnormal_legacy_warning():
    async with World() as world:
        plugin = world.refactor.plugin
        for index in range(3):
            _item, status = plugin.enqueue_external_material(
                SENDER, "bridge", str(index), "汉" * 800
            )
            assert status == "queued"
        batch = plugin.external_material_batch(SENDER, "private")
        assert len(batch) == 2
        from memory_diary.token_estimator import estimate_text
        assert sum(estimate_text(item["text"]) + 30 for item in batch) <= 2000

        data, _status = plugin.load_external_materials(SENDER)
        data["items"][0]["text"] = "汉" * 4000
        assert plugin._write_external_materials(SENDER, data)
        import sys
        from unittest import mock
        module = sys.modules[plugin.external_material_batch.__func__.__module__]
        with mock.patch.object(module.logger, "warning") as warning:
            abnormal_batch = plugin.external_material_batch(SENDER, "private")
        assert abnormal_batch
        assert all(item["ext_id"] != "0" for item in abnormal_batch)
        assert any(
            "超过可处理上限" in str(call.args[0])
            for call in warning.call_args_list
        )
