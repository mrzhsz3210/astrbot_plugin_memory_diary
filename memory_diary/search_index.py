# -*- coding: utf-8 -*-
"""按 sender 隔离的内存 SQLite FTS 派生索引；权威原文始终留在签名 ZIP。"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import threading
import time

from .log import logger


class SearchIndexUnavailable(RuntimeError):
    pass


def _normalized_keywords(query: str) -> list[str]:
    from .search import normalize_search_text

    return [normalize_search_text(part) for part in str(query or "").split() if part]


def _metadata_text(doc: dict) -> str:
    day = str(doc.get("day") or "")
    formatted_day = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else day
    return "\n".join(
        str(doc.get(key) or "")
        for key in ("source", "scope", "origin", "mode", "entry")
    ) + "\n" + day + "\n" + formatted_day


def searchable_document_text(doc: dict, *, private: bool = False) -> str:
    from .search import normalize_search_text

    text_key = "private_text" if private else "text"
    return normalize_search_text(str(doc.get(text_key) or "") + "\n" + _metadata_text(doc))


def document_matches(doc: dict, keywords: list[str], *, private: bool = False) -> bool:
    from .search import normalize_search_text

    normalized = normalize_search_text(searchable_document_text(doc, private=private))
    return all(keyword in normalized for keyword in keywords)


def _ngram_tokens(text: str) -> list[str]:
    from .search import normalize_search_text

    normalized = normalize_search_text(text)
    tokens = []
    for char in normalized:
        if char.isalnum():
            tokens.append("u" + char)
    compact = "".join(char if char.isalnum() else " " for char in normalized)
    for run in compact.split():
        for size, prefix in ((2, "b"), (3, "t")):
            tokens.extend(
                prefix + run[index:index + size]
                for index in range(0, len(run) - size + 1)
            )
    return tokens


def _match_string(keywords: list[str], backend: str) -> str:
    parts = []
    for keyword in keywords:
        if backend == "trigram":
            parts.append('"' + keyword.replace('"', '""') + '"')
            continue
        tokens = _ngram_tokens(keyword)
        if len(keyword) == 1:
            tokens = [token for token in tokens if token.startswith("u")]
        elif len(keyword) == 2:
            tokens = [token for token in tokens if token.startswith("b")]
        else:
            tokens = [token for token in tokens if token.startswith("t")]
        parts.extend('"' + token.replace('"', '""') + '"' for token in tokens)
    return " AND ".join(parts)


class MemorySearchIndex:
    def __init__(self, documents: list[dict]):
        self.documents = list(documents)
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.backend = "ngram"
        try:
            self.connection.execute(
                "CREATE TABLE documents ("
                "id INTEGER PRIMARY KEY, segment_id TEXT UNIQUE NOT NULL, "
                "entry TEXT NOT NULL, day TEXT NOT NULL, scope TEXT NOT NULL, "
                "origin TEXT NOT NULL, mode TEXT NOT NULL, source TEXT NOT NULL, "
                "text TEXT NOT NULL, private_text TEXT NOT NULL, display_text TEXT NOT NULL, "
                "evidence_type TEXT NOT NULL, has_explicit INTEGER NOT NULL, "
                "self_masked INTEGER NOT NULL, has_annotations INTEGER NOT NULL, "
                "has_images INTEGER NOT NULL)"
            )
            self.connection.executemany(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        index,
                        doc["segment_id"], doc["entry"], doc["day"], doc["scope"],
                        doc["origin"], doc["mode"], doc["source"], doc["text"],
                        doc["private_text"], doc["display_text"], doc["evidence_type"],
                        int(bool(doc["has_explicit"])), int(bool(doc["self_masked"])),
                        int(bool(doc["has_annotations"])), int(bool(doc["has_images"])),
                    )
                    for index, doc in enumerate(self.documents, 1)
                ],
            )
            try:
                self.connection.execute(
                    "CREATE VIRTUAL TABLE trigram_probe USING fts5(text, tokenize='trigram')"
                )
                self.connection.execute("DROP TABLE trigram_probe")
                self.backend = "trigram"
            except sqlite3.Error:
                self.backend = "ngram"
            for table in ("user_fts", "private_fts", "user_ngram", "private_ngram"):
                tokenizer = "trigram" if table.endswith("_fts") and self.backend == "trigram" else "unicode61"
                self.connection.execute(
                    f"CREATE VIRTUAL TABLE {table} USING fts5(content, tokenize='{tokenizer}')"
                )
            for index, doc in enumerate(self.documents, 1):
                user_text = searchable_document_text(doc)
                private_text = searchable_document_text(doc, private=True)
                self.connection.executemany(
                    "INSERT INTO user_fts(rowid, content) VALUES (?, ?)",
                    [(index, user_text if self.backend == "trigram" else " ".join(_ngram_tokens(user_text)))],
                )
                self.connection.executemany(
                    "INSERT INTO private_fts(rowid, content) VALUES (?, ?)",
                    [(index, private_text if self.backend == "trigram" else " ".join(_ngram_tokens(private_text)))],
                )
                self.connection.execute(
                    "INSERT INTO user_ngram(rowid, content) VALUES (?, ?)",
                    (index, " ".join(_ngram_tokens(user_text))),
                )
                self.connection.execute(
                    "INSERT INTO private_ngram(rowid, content) VALUES (?, ?)",
                    (index, " ".join(_ngram_tokens(private_text))),
                )
            self.connection.commit()
        except Exception:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def search(self, query: str, *, evidence_type: str = "user") -> list[dict]:
        keywords = _normalized_keywords(query)
        if not keywords:
            return list(self.documents)
        private = evidence_type in ("self", "all")
        short_query = any(len(keyword) < 3 for keyword in keywords)
        backend = "ngram" if short_query else self.backend
        table = ("private_" if private else "user_") + ("ngram" if backend == "ngram" else "fts")
        match = _match_string(keywords, backend)
        if not match:
            return [
                doc for doc in self.documents
                if document_matches(doc, keywords, private=private)
            ]
        rows = self.connection.execute(
            f"SELECT documents.id, bm25({table}) AS rank "
            f"FROM {table} JOIN documents ON documents.id = {table}.rowid "
            f"WHERE {table} MATCH ? ORDER BY rank, documents.day DESC, documents.id ASC",
            (match,),
        ).fetchall()
        ranked = []
        for row_id, rank in rows:
            doc = self.documents[int(row_id) - 1]
            if document_matches(doc, keywords, private=private):
                ranked.append((float(rank), doc))
        return [doc for _rank, doc in ranked]


class SearchIndexCache:
    def __init__(self, max_entries: int = 4):
        self.max_entries = max(1, int(max_entries))
        self._entries = OrderedDict()
        self._locks = {}
        self._guard = threading.RLock()
        self.stats = {
            "builds": 0, "rebuilds": 0, "queries": 0, "fallbacks": 0,
            "evictions": 0, "last_build_ms": 0.0, "total_build_ms": 0.0,
        }

    @contextmanager
    def _sender_lock(self, sender_id: str):
        sender_id = str(sender_id)
        with self._guard:
            record = self._locks.get(sender_id)
            if record is None:
                record = {"lock": threading.RLock(), "users": 0}
                self._locks[sender_id] = record
            record["users"] += 1
        try:
            with record["lock"]:
                yield
        finally:
            with self._guard:
                record["users"] -= 1
                if record["users"] == 0 and sender_id not in self._entries:
                    self._locks.pop(sender_id, None)

    @staticmethod
    def fingerprint(documents: list[dict], evidence_type: str) -> str:
        payload = json.dumps(
            {"evidence_type": evidence_type, "documents": documents},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def search(self, sender_id: str, documents: list[dict], query: str, *, evidence_type: str = "user"):
        sender_id = str(sender_id)
        fingerprint = self.fingerprint(documents, evidence_type)
        with self._sender_lock(sender_id):
            with self._guard:
                entry = self._entries.get(sender_id)
            if entry is None or entry["fingerprint"] != fingerprint:
                started = time.perf_counter()
                index = MemorySearchIndex(documents)
                elapsed = (time.perf_counter() - started) * 1000
                with self._guard:
                    old = self._entries.pop(sender_id, None)
                    if old is not None:
                        old["index"].close()
                    self._entries[sender_id] = {"fingerprint": fingerprint, "index": index}
                    self.stats["rebuilds" if entry is not None else "builds"] += 1
                    self.stats["last_build_ms"] = elapsed
                    self.stats["total_build_ms"] += elapsed
                    while len(self._entries) > self.max_entries:
                        old_sender, removed = self._entries.popitem(last=False)
                        removed["index"].close()
                        old_lock = self._locks.get(old_sender)
                        if old_lock is not None and old_lock["users"] == 0:
                            self._locks.pop(old_sender, None)
                        self.stats["evictions"] += 1
                    logger.info(
                        f"[memory_diary] 内存检索索引已构建：{len(documents)} 段，"
                        f"{elapsed:.1f} ms，后端 {index.backend}"
                    )
            with self._guard:
                entry = self._entries[sender_id]
                self._entries.move_to_end(sender_id)
                self.stats["queries"] += 1
                return entry["index"].search(query, evidence_type=evidence_type)

    def invalidate(self, sender_id: str):
        sender_id = str(sender_id)
        with self._sender_lock(sender_id):
            with self._guard:
                entry = self._entries.pop(sender_id, None)
            if entry is not None:
                entry["index"].close()

    def note_fallback(self):
        with self._guard:
            self.stats["fallbacks"] += 1

    def close(self):
        with self._guard:
            entries = list(self._entries.values())
            self._entries.clear()
            self._locks.clear()
        for entry in entries:
            entry["index"].close()
