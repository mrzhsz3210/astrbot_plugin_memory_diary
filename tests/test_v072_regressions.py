# -*- coding: utf-8 -*-
"""v0.7.2 审查修复：符号检索、LRU 元数据、扩展候选与异常日志边界。"""

from __future__ import annotations

import importlib

from harness.fakes import make_event
from harness.world import World
from memory_diary.search_index import MemorySearchIndex, SearchIndexCache

SENDER = "10001"


def _document(segment_id="diaries/20261023.md#1", text="林岚 100% 完成 ++ 标记"):
    return {
        "segment_id": segment_id,
        "entry": segment_id.split("#")[0],
        "day": "20261023",
        "segment_header": "",
        "scope": "private",
        "origin": "private",
        "mode": "full",
        "source": "私聊",
        "text": text,
        "private_text": text,
        "display_text": text,
        "evidence_type": "user",
        "has_explicit": False,
        "self_masked": False,
        "has_annotations": False,
        "has_images": False,
        "source_status": "ok",
    }


async def test_short_symbol_queries_fall_back_to_linear_matching():
    index = MemorySearchIndex([_document()])
    try:
        assert len(index.search("%")) == 1
        assert len(index.search("++")) == 1
        assert index.search("不存在") == []
    finally:
        index.close()


async def test_lru_also_bounds_sender_lock_metadata():
    cache = SearchIndexCache(max_entries=2)
    try:
        for index in range(8):
            sender = str(10000 + index)
            documents = [_document(f"diaries/2026102{index}.md#1")]
            assert len(cache.search(sender, documents, "林岚")) == 1
        assert len(cache._entries) == 2
        assert len(cache._locks) == 2
        cache.invalidate("10000")
        assert len(cache._locks) == 2
    finally:
        cache.close()
    assert cache._locks == {}


async def test_expanded_candidate_is_not_starved_by_original_top20():
    async with World(config={
        "use_persona": False,
        "search_query_expansion_enabled": True,
        "search_rerank_enabled": True,
    }) as world:
        inst = world.refactor
        plugin = inst.plugin
        for index in range(25):
            plugin._save_diary(
                SENDER,
                f"共同证据 原查询{index}",
                entry_name_hint=f"202609{index + 1:02d}",
                origin="private",
                mode="full",
            )
        plugin._save_diary(
            SENDER,
            "扩展独有牛奶证据",
            entry_name_hint="20261030",
            origin="private",
            mode="full",
        )
        inst.provider.replies = ['{"queries":["牛奶"]}', '{"ids":[]}']
        await inst.run_handler("recall", make_event("/回忆 共同证据"))
        assert len(inst.provider.calls) == 2
        rerank_prompt = inst.provider.calls[1]["prompt"]
        assert "扩展独有牛奶证据" in rerank_prompt


async def test_assist_provider_errors_never_log_query_or_evidence():
    async with World(config={
        "use_persona": False,
        "search_query_expansion_enabled": True,
        "search_rerank_enabled": True,
    }) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, "共同证据 绝密牛奶原文甲", origin="private", mode="full")
        plugin._save_diary(
            SENDER, "共同证据 绝密牛奶原文乙", entry_name_hint="20261024",
            origin="private", mode="full",
        )
        module = importlib.import_module(
            inst.module.__name__.rsplit(".", 1)[0] + ".adapter.cmd_search"
        )
        observed = []
        original_warning = module.logger.warning
        module.logger.warning = lambda message: observed.append(str(message))
        inst.provider.fail_with = "异常回显：饮品 绝密牛奶原文"
        try:
            await inst.run_handler("recall", make_event("/回忆 共同证据"))
            logs = "\n".join(observed)
            assert "RuntimeError" in logs
            assert "饮品" not in logs and "绝密牛奶原文" not in logs
            assert "异常回显" not in logs
        finally:
            module.logger.warning = original_warning
