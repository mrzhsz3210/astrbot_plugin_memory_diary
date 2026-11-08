# -*- coding: utf-8 -*-
"""/回忆 与 memory_diary_search 模型工具；身份只来自当前事件。"""

from __future__ import annotations

import json

from ..memory_diary.log import logger
from ..memory_diary.scope import scope_from_event
from ..memory_diary.search import normalize_evidence_type, search_diaries
from ..memory_diary.search_assist import (
    merge_evidence_results,
    parse_query_expansion,
    parse_rerank_ids,
)


def _format_evidence(result: dict) -> str:
    lines = []
    for item in result.get("results") or []:
        labels = [item.get("evidence_label") or "未分级（旧记录）"]
        if item.get("has_explicit"):
            labels.append("显式交代")
        if item.get("source_status") == "partial":
            labels.append("来源未完整匹配")
        elif item.get("source_status") == "tampered":
            labels.append("来源侧车验签失败")
        lines.append(
            f"【{item['day']} · {item['source']} · {' · '.join(labels)}】\n"
            f"{item['snippet']}\n出处：{item['entry']}"
        )
    text = "\n\n".join(lines)
    if result.get("tampered"):
        text += "\n\n（部分条目验签失败已跳过；来源异常的结果标为未分级。）"
    return text


class SearchCommandsMixin:
    async def _search_context_for_expansion(self, event) -> str:
        if event.get_group_id():
            return ""
        try:
            history = await self._get_history(event.unified_msg_origin)
            return self._flatten_history(history[-4:])[-1200:]
        except Exception:
            return ""

    async def _expand_search_query(self, event, query: str, scope: str) -> tuple[list[str], str]:
        if not query or not bool(self.config.get("search_query_expansion_enabled", False)):
            return [], "disabled"
        try:
            provider = await self.context.get_using_provider_async(
                umo=event.unified_msg_origin
            )
            if provider is None:
                return [], "provider_unavailable"
            context = await self._search_context_for_expansion(event)
            prompt = (
                f"当前查询：{query}\n当前 sender：{event.get_sender_id()}\n"
                f"当前 scope：{scope or 'private'}\n"
                f"少量当前会话上下文（可能为空）：\n{context}"
            )
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=(
                    "你只负责把日记检索问题拆成关键词组。最多输出 4 组，支持中文、英文与同义表达。"
                    "不得回答查询，不得补充事实、日记内容或解释。只输出严格 JSON："
                    '{"queries":["关键词组1","关键词组2"]}。'
                ),
                max_tokens=256,
            )
            expanded = parse_query_expansion(response.completion_text or "", query)
            return expanded, "ok" if expanded else "invalid_or_empty"
        except Exception as exc:
            logger.warning(
                f"[memory_diary] 查询扩展失败，使用原查询（{type(exc).__name__}）"
            )
            return [], "call_failed"

    async def _rerank_search_candidates(self, event, query: str, candidates: list[dict]):
        if not query or len(candidates) < 2 or not bool(self.config.get("search_rerank_enabled", False)):
            return candidates
        candidates = candidates[:20]
        try:
            provider = await self.context.get_using_provider_async(
                umo=event.unified_msg_origin
            )
            if provider is None:
                return candidates
            payload = [
                {
                    "id": item["segment_id"],
                    "day": item["day"],
                    "source": item["source"],
                    "evidence_type": item["evidence_type"],
                    "text": item["snippet"],
                }
                for item in candidates
            ]
            response = await provider.text_chat(
                prompt=(
                    f"当前问题：{query}\n候选证据：\n"
                    + json.dumps(payload, ensure_ascii=False)
                ),
                system_prompt=(
                    "你只能从给定候选中挑选与当前问题相关的 ID，不得新增 ID、改写证据或回答事实。"
                    "只输出严格 JSON：{\"ids\":[\"候选ID1\",\"候选ID2\"]}；没有相关候选时输出空列表。"
                ),
                max_tokens=300,
            )
            ids = parse_rerank_ids(
                response.completion_text or "",
                {item["segment_id"] for item in candidates},
            )
            if ids is None:
                return candidates
            by_id = {item["segment_id"]: item for item in candidates}
            return [by_id[item_id] for item_id in ids]
        except Exception as exc:
            logger.warning(
                f"[memory_diary] 候选重排失败，保留 BM25 顺序（{type(exc).__name__}）"
            )
            return candidates

    async def _assisted_search(self, event, query: str, **kwargs):
        expansion = bool(self.config.get("search_query_expansion_enabled", False))
        rerank = bool(self.config.get("search_rerank_enabled", False))
        if not expansion and not rerank:
            return search_diaries(self, event.get_sender_id(), query, **kwargs)
        limit = kwargs.get("limit", 20)
        queries = [query]
        if expansion:
            expanded, status = await self._expand_search_query(
                event, query, str(kwargs.get("scope") or "")
            )
            queries.extend(expanded)
            logger.info(
                f"[memory_diary] 查询扩展状态={status}，新增关键词组={len(expanded)}"
                "（不记录关键词或日记正文）"
            )
        searches = []
        for route, item_query in enumerate(dict.fromkeys(queries), 1):
            candidate_kwargs = dict(kwargs)
            candidate_kwargs["offset"] = 0
            candidate_kwargs["limit"] = 20 if rerank else max(20, int(limit or 20))
            result = search_diaries(self, event.get_sender_id(), item_query, **candidate_kwargs)
            searches.append(result)
            if expansion:
                logger.info(
                    f"[memory_diary] 扩展检索第 {route} 路：命中 {result.get('total', 0)} 条"
                )
        candidates = merge_evidence_results(searches)
        if rerank:
            candidates = await self._rerank_search_candidates(event, query, candidates)
        return {
            "results": candidates[:max(1, int(limit or 20))],
            "total": len(candidates),
            "tampered": any(item.get("tampered") for item in searches),
            "disabled": False,
        }

    async def _handle_recall(self, event):
        if not self._should_respond(event):
            return
        if not bool(self.config.get("local_search_enabled", True)):
            yield event.plain_result("本地回忆搜索已由管理员关闭。")
            return
        query = self._command_arg(event)
        evidence_type = "user"
        parts = query.split(maxsplit=1)
        if parts and parts[0] in ("全部", "私人随笔"):
            evidence_type = "all" if parts[0] == "全部" else "self"
            query = parts[1] if len(parts) > 1 else ""
        if not query:
            yield event.plain_result(
                "用法：/回忆 [全部|私人随笔] 关键词 [更多关键词]。多个关键词需同时命中。"
            )
            return
        current_scope = str(scope_from_event(event))
        result = await self._assisted_search(
            event,
            query,
            scope="" if current_scope == "private" else current_scope,
            limit=5,
            evidence_type=evidence_type,
        )
        text = _format_evidence(result)
        if not text:
            yield event.plain_result("关键词未命中，不代表没有相关记忆。词法检索不保证语义理解，可尝试更换关键词。")
            return
        yield event.plain_result(text)

    async def _handle_memory_diary_search(
        self,
        event,
        query: str,
        day: str = "",
        max_results: int = 5,
        evidence_type: str = "user",
    ) -> str:
        if not self._is_allowed(event.get_sender_id()) or not bool(
            self.config.get("local_search_enabled", True)
        ):
            return "本地日记搜索不可用。"
        if not str(query or "").strip():
            return "请提供 query 关键词，例如 query='窗台 薄荷'；可选 day 与 evidence_type。"
        day = str(day or "").replace("-", "")
        if day and (len(day) != 8 or not day.isdigit()):
            return "day 必须是 YYYYMMDD 或 YYYY-MM-DD。"
        current_scope = str(scope_from_event(event))
        result = await self._assisted_search(
            event,
            query,
            scope="" if current_scope == "private" else current_scope,
            day=day,
            limit=min(5, max(1, int(max_results or 5))),
            evidence_type=normalize_evidence_type(evidence_type),
        )
        text = _format_evidence(result)
        if text:
            return text
        selected = normalize_evidence_type(evidence_type)
        return (
            f"关键词未命中；当前 evidence_type={selected}，day={day or '全部日期'}。"
            "这不代表其他类型或其他日期没有相关证据。不得仅凭本次结果断言整天没有相关内容；"
            "私人随笔应使用 self，需要跨类型核实使用 all。本地搜索以原文词法匹配为准。"
        )
