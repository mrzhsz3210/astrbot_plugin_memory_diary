# -*- coding: utf-8 -*-
"""/回忆 与 memory_diary_search 模型工具；身份只来自当前事件。"""

from __future__ import annotations

from ..memory_diary.scope import scope_from_event
from ..memory_diary.search import normalize_evidence_type, search_diaries


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
        result = search_diaries(
            self,
            event.get_sender_id(),
            query,
            scope="" if current_scope == "private" else current_scope,
            limit=5,
            evidence_type=evidence_type,
        )
        text = _format_evidence(result)
        if not text:
            yield event.plain_result("关键词未命中，不代表没有相关记忆。本地搜索只支持原文关键词，不做语义召回。")
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
        day = str(day or "").replace("-", "")
        if day and (len(day) != 8 or not day.isdigit()):
            return "day 必须是 YYYYMMDD 或 YYYY-MM-DD。"
        current_scope = str(scope_from_event(event))
        result = search_diaries(
            self,
            event.get_sender_id(),
            query,
            scope="" if current_scope == "private" else current_scope,
            day=day,
            limit=min(5, max(1, int(max_results or 5))),
            evidence_type=normalize_evidence_type(evidence_type),
        )
        text = _format_evidence(result)
        return text or "关键词未命中，不代表没有相关记忆。本地搜索只匹配原文关键词。"
