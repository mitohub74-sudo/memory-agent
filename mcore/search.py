# -*- coding: utf-8 -*-
"""检索层：可替换的 Searcher 接口 + 关键词实现。

为什么抽象成接口
----------------
用户要求「后期可以维护、修改、更新」。将来加向量检索（sqlite-vec + 本地
ONNX 嵌入模型）时，只需新增一个 Searcher 实现并在 CLI 里注册，调用方无需
改动。cards 表已预留 embedding 列，回填即可。

现有实现
--------
KeywordSearcher
    FTS5 + bigram 关键词检索。BM25 排序，零外部依赖。
    AND 优先；若一条都召不回，自动降级为 OR 并在结果里标注 —— 避免用户
    因为多打了一个词就得到空结果。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .tokenize import query_terms, to_query_expr

__all__ = ["Hit", "Searcher", "KeywordSearcher"]


@dataclass
class Hit:
    """一条检索结果。"""

    card_id: int
    rel_path: str
    title: str
    kind: str
    source: str
    score: float          # 越大越相关
    snippet: str = ""
    matched: str = "all"  # 实际生效的匹配模式：all(AND) / any(OR)


@runtime_checkable
class Searcher(Protocol):
    """检索器接口。加新检索方式时实现本协议即可。"""

    name: str

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        kind: str | None = None,
        source: str | None = None,
    ) -> list[Hit]:  # pragma: no cover - 协议声明
        ...


def _find_anchor(body: str, terms: list[str]) -> tuple[int, int]:
    """在正文里找第一个出现的查询词，返回 (位置, 长度)。找不到返回 (-1, 0)。"""
    lower = body.lower()
    best_pos, best_len = -1, 0
    for term in terms:
        if len(term) < 2:
            continue
        pos = lower.find(term.lower())
        if pos >= 0 and (best_pos < 0 or pos < best_pos):
            best_pos, best_len = pos, len(term)
    return best_pos, best_len


def make_snippet(body: str, query: str, width: int = 70) -> str:
    """截取命中位置附近的正文片段。"""
    flat = " ".join(body.split())
    if not flat:
        return ""

    terms = query_terms(query)
    pos, tlen = _find_anchor(flat, terms)
    if pos < 0:
        head = flat[: width * 2]
        return head + ("…" if len(flat) > len(head) else "")

    start = max(0, pos - width // 3)
    end = min(len(flat), pos + tlen + width)
    out = flat[start:end]
    if start > 0:
        out = "…" + out
    if end < len(flat):
        out = out + "…"
    return out


class KeywordSearcher:
    """FTS5 + bigram 关键词检索。"""

    name = "keyword"

    def __init__(self, conn: sqlite3.Connection, *, fallback_any: bool = True) -> None:
        self.conn = conn
        self.fallback_any = fallback_any

    def _run(self, expr: str, limit: int, kind: str | None, source: str | None):
        sql = [
            "SELECT c.id, c.rel_path, c.title, c.kind, c.source, c.body,",
            "       bm25(cards_fts) AS score",
            "  FROM cards_fts",
            "  JOIN cards c ON c.id = cards_fts.rowid",
            " WHERE cards_fts MATCH ?",
        ]
        params: list = [expr]
        if kind:
            sql.append("   AND c.kind = ?")
            params.append(kind)
        if source:
            sql.append("   AND c.source = ?")
            params.append(source)
        # bm25() 返回负值，越小越相关 —— 升序即最佳优先
        sql.append(" ORDER BY score LIMIT ?")
        params.append(limit)
        return self.conn.execute("\n".join(sql), params).fetchall()

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        kind: str | None = None,
        source: str | None = None,
    ) -> list[Hit]:
        expr = to_query_expr(query, "all")
        if not expr:
            return []

        rows = self._run(expr, limit, kind, source)
        mode = "all"

        if not rows and self.fallback_any:
            expr_any = to_query_expr(query, "any")
            if expr_any:
                rows = self._run(expr_any, limit, kind, source)
                mode = "any"

        return [
            Hit(
                card_id=int(r["id"]),
                rel_path=r["rel_path"],
                title=r["title"],
                kind=r["kind"],
                source=r["source"],
                score=-float(r["score"]),  # 取负，使「越大越相关」符合直觉
                snippet=make_snippet(r["body"], query),
                matched=mode,
            )
            for r in rows
        ]
