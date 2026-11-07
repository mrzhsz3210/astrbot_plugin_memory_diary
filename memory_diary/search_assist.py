# -*- coding: utf-8 -*-
"""检索辅助的严格 JSON 边界与克制的历史指代识别。"""

from __future__ import annotations

import json
import re


def parse_query_expansion(raw: str, original: str) -> list[str]:
    try:
        data = json.loads(str(raw or "").strip())
        queries = data["queries"]
        if not isinstance(queries, list):
            return []
    except (ValueError, TypeError, KeyError):
        return []
    original = str(original or "").strip()
    output = []
    for item in queries:
        if not isinstance(item, str):
            return []
        query = " ".join(item.split())[:120].strip()
        if not query or query == original or query in output:
            continue
        output.append(query)
        if len(output) == 4:
            break
    return output


def parse_rerank_ids(raw: str, allowed_ids: set[str]) -> list[str] | None:
    try:
        data = json.loads(str(raw or "").strip())
        ids = data["ids"]
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    if any(item not in allowed_ids for item in ids):
        return None
    return list(dict.fromkeys(ids))


def merge_evidence_results(results: list[dict]) -> list[dict]:
    merged = []
    seen = set()
    result_rows = [result.get("results") or [] for result in results]
    for index in range(max((len(row) for row in result_rows), default=0)):
        for row in result_rows:
            if index >= len(row):
                continue
            item = row[index]
            segment_id = str(item.get("segment_id") or "")
            if not segment_id or segment_id in seen:
                continue
            seen.add(segment_id)
            merged.append(item)
    return merged


_HISTORY_REFERENCE = re.compile(
    r"之前|以前|上次|当时|那时|那年|几个月前|几周前|几天前|去年|前年|"
    r"上周|上个星期|上个月|还记得|提到过|那个老师|那位老师|原来的|旧日记"
)
_SMALL_TALK = {
    "你好", "您好", "在吗", "在么", "哈哈", "哈哈哈", "谢谢", "谢谢你",
    "晚安", "早安", "早上好", "嗯", "嗯嗯", "好", "好的", "ok", "hi", "hello",
}
_OPERATION_MESSAGES = {"reset", "重置", "/reset", "/重置"}


def has_explicit_history_reference(message: str) -> bool:
    text = " ".join(str(message or "").split()).strip()
    if not text or text.casefold() in _SMALL_TALK or text.casefold() in _OPERATION_MESSAGES:
        return False
    if text.startswith(("/", "／")):
        return False
    return bool(_HISTORY_REFERENCE.search(text))


def history_recall_query(message: str) -> str:
    query = " ".join(str(message or "").split()).strip()
    query = query.replace("那个老师", " 老师 ").replace("那位老师", " 老师 ")
    query = _HISTORY_REFERENCE.sub(" ", query)
    for phrase in ("那个", "那位", "我们", "咱们", "我", "你", "她", "他"):
        query = query.replace(phrase, " ")
    query = re.sub(r"[\s，。！？!?、；;：:（）()【】\[\]“”\"']+", " ", query)
    query = re.sub(
        r"还记得|说过|提到|提过|讨论|聊了|聊了聊|说了|什么|哪里|哪个|谁|怎样|怎么|吗|呢|了|的|是",
        " ", query,
    )
    return " ".join(query.split()).strip()[:160]
