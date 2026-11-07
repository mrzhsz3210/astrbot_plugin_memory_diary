# -*- coding: utf-8 -*-
"""v0.7.1：内存 FTS、可回源检索辅助与克制的当前对话召回。"""

from __future__ import annotations

import sqlite3

from astrbot.api.provider import ProviderRequest

from harness.fakes import make_event
from harness.world import World
from memory_diary.search import search_diaries
from memory_diary.search_assist import (
    has_explicit_history_reference,
    history_recall_query,
    parse_query_expansion,
    parse_rerank_ids,
)
from memory_diary.search_index import MemorySearchIndex, SearchIndexCache
from memory_diary.token_estimator import estimate_text

SENDER = "10001"


def _document(segment_id="diaries/20200102.md#1", text="林岚报考南淮大学", day="20200102"):
    return {
        "segment_id": segment_id, "entry": segment_id.split("#")[0], "day": day,
        "segment_header": "", "scope": "private", "origin": "private", "mode": "full",
        "source": "私聊", "text": text, "private_text": text, "display_text": text,
        "evidence_type": "user", "has_explicit": False, "self_masked": False,
        "has_annotations": False, "has_images": False, "source_status": "ok",
    }


def _request(text):
    return ProviderRequest(
        prompt=text, contexts=[{"role": "user", "content": text}], system_prompt="SYS"
    )


def _extra_texts(req):
    return [getattr(part, "text", str(part)) for part in req.extra_user_content_parts]


async def test_fts_chinese_short_mixed_date_group_and_external_source():
    async with World(config={"group_share_map": "A=778899"}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(
            SENDER, "林岚最终报考南淮大学，项目代号 ReleaseCandidate42。",
            entry_name_hint="20200102", origin="private", mode="full",
        )
        plugin._save_diary(
            SENDER, "群778899的旧证据", entry_name_hint="20200103",
            origin="group", scope="group:778899", mode="full",
        )

        def external(data):
            data["entries"].setdefault("20200104.md", {})["external"] = [
                {"source": "bridge", "ext_id": "v071", "at": "2020-01-04T00:00:00"}
            ]
            return data

        plugin._save_diary(
            SENDER, "桥接旧证据", entry_name_hint="20200104",
            separator_note="（外部·bridge）", provenance_mutator=external,
        )
        for query in ("林岚", "南淮", "release", "2020-01-02"):
            assert search_diaries(plugin, SENDER, query)["total"] == 1, query
        group = search_diaries(plugin, SENDER, "778899", scope="group:A")
        assert group["total"] == 1 and group["results"][0]["scope"] == "group:778899"
        plugin.config["group_share_map"] = ""
        assert search_diaries(plugin, SENDER, "778899", scope="group:A")["total"] == 0
        source = search_diaries(plugin, SENDER, "bridge")
        assert source["total"] == 1 and source["results"][0]["source"] == "bridge"
        assert search_diaries(plugin, SENDER, "南淮")["results"][0]["day"] == "20200102"


async def test_import_invalidates_prebuilt_empty_index():
    async with World(config={}) as world:
        plugin = world.refactor.plugin
        other = "20002"
        assert search_diaries(plugin, other, "导入旧证据")["total"] == 0
        assert other in plugin._search_index._entries
        plugin._install_bundle(
            other, other, plugin._user_sign_key(other),
            {"diaries/20261023.md": "导入旧证据"},
        )
        assert other not in plugin._search_index._entries
        assert search_diaries(plugin, other, "导入旧证据")["total"] == 1


async def test_custom_ngram_backend_when_trigram_is_unavailable():
    import memory_diary.search_index as module

    real_connect = module.sqlite3.connect

    class NoTrigramConnection:
        def __init__(self):
            self.inner = real_connect(":memory:")

        def execute(self, sql, *args):
            if "tokenize='trigram'" in str(sql):
                raise sqlite3.OperationalError("trigram unavailable")
            return self.inner.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    module.sqlite3.connect = lambda *_args, **_kwargs: NoTrigramConnection()
    try:
        index = MemorySearchIndex([_document(text="林岚喜欢 Alpine42")])
        assert index.backend == "ngram"
        assert len(index.search("林岚")) == 1
        assert len(index.search("alpine")) == 1
        assert index.search("不存在") == []
        index.close()
    finally:
        module.sqlite3.connect = real_connect


async def test_index_failure_falls_back_to_linear_and_whitelist_evicts():
    async with World(config={}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, "线性降级仍能命中林岚", origin="private", mode="full")
        original = plugin._search_index.search

        def broken(*_args, **_kwargs):
            raise RuntimeError("simulated broken index")

        plugin._search_index.search = broken
        result = search_diaries(plugin, SENDER, "林岚")
        assert result["total"] == 1
        assert plugin._search_index.stats["fallbacks"] == 1
        plugin._search_index.search = original
        assert search_diaries(plugin, SENDER, "林岚")["total"] == 1
        assert SENDER in plugin._search_index._entries
        plugin.config["allowed_users"] = "20002"
        denied = search_diaries(plugin, SENDER, "林岚")
        assert denied["disabled"] is True and SENDER not in plugin._search_index._entries


async def test_plaintext_fingerprint_reuse_write_provenance_and_delete_invalidation():
    async with World(config={}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, "旧证据林岚", origin="private", mode="full")
        assert search_diaries(plugin, SENDER, "林岚")["total"] == 1
        assert search_diaries(plugin, SENDER, "林岚")["total"] == 1
        assert plugin._search_index.stats["builds"] == 1
        docs = [_document()]
        assert SearchIndexCache.fingerprint(docs, "user") == SearchIndexCache.fingerprint(docs, "user")

        private = "我独自看云。"
        plugin._save_diary(SENDER, f"新用户事实。{private}", origin="private", mode="full")
        assert plugin.record_self_provenance(SENDER, "20261023", [private])
        assert search_diaries(plugin, SENDER, private)["total"] == 0
        assert search_diaries(plugin, SENDER, private, evidence_type="self")["total"] == 1
        plugin._save_diary(
            SENDER, "次日证据", entry_name_hint="20261024", origin="private", mode="full"
        )
        assert plugin._delete_entries(SENDER, ["diaries/20261024.md"]) == 1
        assert search_diaries(plugin, SENDER, "次日证据")["total"] == 0
        assert plugin._delete_diary(SENDER) is True
        assert search_diaries(plugin, SENDER, "林岚")["total"] == 0


async def test_encrypted_search_has_no_plaintext_index_file():
    async with World(config={
        "storage_encryption_enabled": True,
        "storage_encryption_passphrase": "correct horse battery staple",
    }) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, "加密后的林岚旧证据", origin="private", mode="full")
        assert search_diaries(plugin, SENDER, "林岚")["total"] == 1
        path = plugin._diary_path(SENDER)
        with plugin._diary_zip(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        encrypted_before = path.read_bytes()
        plugin._write_zip_atomic(path, entries)
        assert path.read_bytes() != encrypted_before
        assert search_diaries(plugin, SENDER, "林岚")["total"] == 1
        assert plugin._search_index.stats["builds"] == 1
        assert plugin._search_index.stats["rebuilds"] == 0
        assert not list(plugin.data_dir.glob("**/search.db"))
        assert not list(plugin.data_dir.glob("**/*index*"))


async def test_query_expansion_default_zero_call_and_strict_fallback():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, "最终报考南淮大学", origin="private", mode="full")
        await inst.run_handler("recall", make_event("/回忆 南淮"))
        assert inst.provider.calls == []

        plugin.config["search_query_expansion_enabled"] = True
        inst.provider.replies = ['{"queries":["南淮大学","升学"]}']
        output = "\n".join(await inst.run_handler("recall", make_event("/回忆 学校")))
        assert "南淮大学" in output and len(inst.provider.calls) == 1
        assert "不得回答查询" in inst.provider.calls[0]["system_prompt"]

        inst.provider.replies = ['{"queries":[{"not":"a string"}]}']
        output = "\n".join(await inst.run_handler("recall", make_event("/回忆 学校")))
        assert "未命中" in output and len(inst.provider.calls) == 2
        assert parse_query_expansion("not json", "学校") == []


async def test_rerank_only_selects_existing_ids_and_invalid_output_keeps_bm25():
    async with World(config={"use_persona": False, "search_rerank_enabled": True}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, "共同证据 目标甲", origin="private", mode="full")
        plugin._save_diary(
            SENDER, "共同证据 目标乙", entry_name_hint="20261024", origin="private", mode="full"
        )
        base = search_diaries(plugin, SENDER, "共同证据", limit=20)["results"]
        selected = next(item for item in base if "目标甲" in item["snippet"])
        other = next(item for item in base if "目标乙" in item["snippet"])
        inst.provider.replies = ['{"ids":["' + selected["segment_id"] + '"]}']
        output = "\n".join(await inst.run_handler("recall", make_event("/回忆 共同证据")))
        assert "目标甲" in output and "目标乙" not in output
        assert len(inst.provider.calls) == 1

        inst.provider.replies = ['{"ids":["diaries/fake.md#99"]}']
        output = "\n".join(await inst.run_handler("recall", make_event("/回忆 共同证据")))
        assert "目标甲" in output and "目标乙" in output
        assert "fake.md" not in output
        assert parse_rerank_ids('{"ids":["unknown"]}', {selected["segment_id"]}) is None
        assert other["segment_id"] != selected["segment_id"]


async def test_context_recall_default_off_trigger_limit_and_budget():
    config = {
        "use_persona": False, "enable_auto_inject": False, "context_token_limit": 0,
    }
    async with World(config=config) as world:
        plugin = world.refactor.plugin
        for day in range(20, 25):
            plugin._save_diary(
                SENDER, f"南淮大学 旧证据{day}", entry_name_hint=f"202610{day}",
                origin="private", mode="full",
            )
        text = "上次我们讨论的南淮大学是什么"
        plain_req = _request(text)
        await plugin.on_llm_request(make_event(text), plain_req)
        assert _extra_texts(plain_req) == []

        plugin.config["context_recall_enabled"] = True
        req = _request(text)
        await plugin.on_llm_request(make_event(text), req)
        recalled = "\n".join(_extra_texts(req))
        assert "南淮大学" in recalled and recalled.count("出处：") <= 3
        assert estimate_text(recalled) <= 400
        assert "不代表当前事实" in recalled

        small_talk = _request("你好")
        await plugin.on_llm_request(make_event("你好"), small_talk)
        assert _extra_texts(small_talk) == []
        command = _request("/日记 上次")
        await plugin.on_llm_request(make_event("/日记 上次"), command)
        assert _extra_texts(command) == []
        assert history_recall_query(text) == "南淮大学"


async def test_history_reference_is_explicit_not_every_message():
    assert has_explicit_history_reference("之前那个老师提过什么")
    assert has_explicit_history_reference("几个月前的安排")
    assert not has_explicit_history_reference("你好")
    assert not has_explicit_history_reference("/回忆 之前")
    assert not has_explicit_history_reference("今天天气怎么样")


async def test_lru_stats_rebuild_and_terminate_close():
    async with World(config={}) as world:
        plugin = world.refactor.plugin
        for sender in ("10001", "10002", "10003", "10004", "10005"):
            plugin._save_diary(sender, f"{sender} 林岚", origin="private", mode="full")
            assert search_diaries(plugin, sender, "林岚")["total"] == 1
        assert len(plugin._search_index._entries) == 4
        assert plugin._search_index.stats["evictions"] == 1
        assert plugin._search_index.stats["last_build_ms"] >= 0
        await plugin.terminate()
        assert plugin._search_index._entries == {}


async def test_large_derived_index_build_and_query():
    documents = [
        _document(
            segment_id=f"diaries/2020{index // 28 + 1:02d}{index % 28 + 1:02d}.md#1",
            text=f"第{index}段 林岚 Release{index} " + "共同背景" * 20,
            day=f"2020{index // 28 + 1:02d}{index % 28 + 1:02d}",
        )
        for index in range(120)
    ]
    cache = SearchIndexCache(max_entries=1)
    result = cache.search(SENDER, documents, "林岚 release119")
    assert len(result) == 1 and "Release119" in result[0]["text"]
    assert cache.stats["builds"] == 1 and cache.stats["total_build_ms"] >= 0
    cache.close()


async def test_model_tool_evidence_type_guidance_and_scoped_no_hit():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        description = plugin.memory_diary_search.__doc__ or ""
        assert "私人随笔" in description and "self" in description
        assert "all" in description and "不得断言" in description
        plugin._save_diary(
            SENDER, "窗台那盆薄荷冒了新叶", origin="private", mode="life",
            separator_note="（无新对话·她的日常随笔）", entry_name_hint="20261030",
        )
        missed = await plugin.memory_diary_search(
            make_event(""), "薄荷", day="20261030", evidence_type="user"
        )
        assert "evidence_type=user" in missed and "day=20261030" in missed
        assert "不得仅凭本次结果断言" in missed
        found = await plugin.memory_diary_search(
            make_event(""), "薄荷", day="20261030", evidence_type="self"
        )
        assert "薄荷冒了新叶" in found and "diaries/20261030.md" in found


async def test_expansion_observability_counts_without_query_or_diary_text():
    import importlib

    async with World(config={
        "use_persona": False, "search_query_expansion_enabled": True,
    }) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, "我说去热杯牛奶等您收工", origin="private", mode="full")
        module = importlib.import_module(
            inst.module.__name__.rsplit(".", 1)[0] + ".adapter.cmd_search"
        )
        observed = []
        original_info = module.logger.info
        original_warning = module.logger.warning
        module.logger.info = lambda message: observed.append(str(message))
        module.logger.warning = lambda message: observed.append(str(message))
        try:
            inst.provider.replies = ['{"queries":["牛奶","乳制品"]}']
            output = "\n".join(await inst.run_handler("recall", make_event("/回忆 饮品")))
            assert "牛奶" in output
            logs = "\n".join(observed)
            assert "状态=ok" in logs and "新增关键词组=2" in logs
            assert "第 1 路" in logs and "第 2 路" in logs and "第 3 路" in logs
            assert "饮品" not in logs and "乳制品" not in logs and "牛奶" not in logs

            observed.clear()
            inst.provider.replies = ['{"queries":[{"invalid":true}]}']
            await inst.run_handler("recall", make_event("/回忆 饮品"))
            logs = "\n".join(observed)
            assert "状态=invalid_or_empty" in logs and "新增关键词组=0" in logs
            assert '{"queries"' not in logs and '"invalid":true' not in logs
        finally:
            module.logger.info = original_info
            module.logger.warning = original_warning
