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
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .tokenize import query_terms, to_query_expr

__all__ = ["Hit", "Searcher", "KeywordSearcher", "MIN_LIMIT", "MAX_LIMIT", "clamp_limit",
           "compute_coverage", "low_confidence_note"]

# 检索条数边界。CLI 与 MCP 共用同一组常量 —— 两处各写一份迟早漂移。
MIN_LIMIT = 1
MAX_LIMIT = 20


def clamp_limit(value: int | str | None) -> int:
    """把条数钳制到 ``[MIN_LIMIT, MAX_LIMIT]``，非法值回落到 ``MIN_LIMIT``。

    必须钳制的原因：SQLite 的 ``LIMIT -1`` 表示**无上限**。把 -1 原样传给
    ``LIMIT ?`` 会把整张表倒出来 —— 调用方看到一大堆结果，以为「搜到了很多」，
    实际是边界没处理。
    """
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = MIN_LIMIT
    return max(MIN_LIMIT, min(n, MAX_LIMIT))


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
    matched: str = "all"  # 实际生效的匹配档位：all / all-prefix / any / any-prefix
    # 查询词覆盖率：命中的查询词占比。
    #
    # **只用于展示与置信提示，绝不参与排序。** 这不是一句客套话 —— 本项目
    # 曾经给 OR 档加过「按命中词数重排」，bench 实测证伪且是负优化（目标卡从
    # 第 4 名掉到第 9 名，封顶 P@5 由 66.7% 降到 55.6%）。所以：
    # 加了这个字段 ≠ 可以用它排序。改动前先读 README「检索行为」那一节的记录。
    #
    # 语义：``all`` / ``all-prefix`` 档按定义恒为 1.0（全部词都命中了）；
    # 只有 ``any`` / ``any-prefix`` 档才可能小于 1。
    coverage: float = 1.0
    # 仅供覆盖率高精度计算使用的正文，**不对外暴露**（repr=False）。
    # 为什么不复用 snippet：snippet 是给模型看的一小段，覆盖率必须在全文上算，
    # 否则同一张卡会因为片段截断而得到不同的置信度。
    _body: str = field(default="", repr=False, compare=False)


def _term_is_covered(term: str, indexed: set[str], prefix: bool) -> bool:
    """查询词是否被这张卡覆盖（与 FTS5 的档位语义保持一致）。

    - ``prefix=False``：整词命中；
    - ``prefix=True``：仅当词长 >= 2 时按前缀判（与 ``to_query_expr`` 一致 ——
      单字前缀是纯噪音，那里就没给它加 ``*``）。

    **精度说明（刻意接受的不精确）**：这里按条目文本再分词后做集合查找，
    而 FTS5 实际还会把 ``id_ed25519`` 这类再切一次（见 ``tokenize`` 的说明）。
    所以覆盖率是**近似值**，用于「这批结果可不可信」的判断，不用于排序。
    要它完全等价于 FTS5，就得把 FTS5 的判定搬进来 —— 那既复杂又会让这个
    展示字段悄悄长出排序能力，正是要避免的事。
    """
    if not term:
        return False
    if not prefix or len(term) < 2:
        return term in indexed
    return any(tok.startswith(term) for tok in indexed)


def compute_coverage(body: str, title: str, query: str, *, matched: str) -> float:
    """算这张卡覆盖了多少查询词，返回 ``[0, 1]``。

    ``all`` / ``all-prefix`` 档直接返回 1.0：那些档位**按定义**就是全部词都命中了，
    再算一遍只会引入与档位不一致的数值（例如近似算法算出 0.8），反而让人怀疑
    「是不是有 bug」。真实的不一致应当修算法，不该靠这里掩盖。
    """
    if matched in ("all", "all-prefix"):
        return 1.0

    from .tokenize import tokenize

    terms = query_terms(query)
    if not terms:
        return 0.0
    indexed = set(tokenize(f"{title}\n{body}"))
    prefix = matched == "any-prefix"
    hit = sum(1 for t in terms if _term_is_covered(t, indexed, prefix))
    return round(hit / len(terms), 4)


def low_confidence_note(matched: str, coverage: float) -> str:
    """低置信提示。命中放宽档且覆盖不全时给出可操作建议。

    为什么要有这句：调用方（agent）看到 `any` 档的一堆结果时，无法自己判断
    「这批到底可不可信」。把档位与覆盖率翻译成一句可执行的建议，比让它猜要划算。
    """
    if matched not in ("any", "any-prefix"):
        return ""
    if coverage >= 1.0:
        return ("注意：本次命中的是放宽档（OR），虽然查询词都出现了，"
                "但排序可信度低于精确档。")
    return ("置信度偏低：这是放宽档（OR）结果，只命中了部分查询词"
            "（覆盖率 {:.0%}）。建议改用关键词/实体名重试，例如直接传"
            "「标识符」或「命令名」而不是整句问句。").format(coverage)


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
    """FTS5 + bigram 关键词检索。

    匹配档位从精确到宽松，**前一档零召回才降级** —— 保证精确匹配的既有
    行为不被放宽匹配污染：

        1. ``all``         AND 精确，全部词整词命中
        2. ``all-prefix``  AND 前缀，全部词前缀命中（``sqlite`` 命中 ``sqlite3``）
        3. ``any``         OR  精确，任一整词命中
        4. ``any-prefix``  OR  前缀，任一前缀命中

    实测（``python memory.py bench``，49 张卡）：关键词查询**全部**在 ``all``
    档命中，从不降级；只有整句自然语言查询会落到 ``any``。所以 OR 档的排序
    质量只影响自然语言查询。

    曾经给 OR 档加过「先按命中词数重排、再按 BM25」的逻辑，动机是怀疑 BM25
    在 OR 档失真。实测证伪并发现它是**负优化**：唯一一条受影响的查询
    「本地装了什么模型」，目标卡从第 4 名被推到第 9 名，封顶 P@5 由 66.7%
    降到 55.6%，其余查询无变化。故已移除 —— 不要再加回来，除非基准
    （``bench/retrieval_quality.py``）显示有正收益。

    ``fallback_any=False`` 时只走前两档（AND 精确 + AND 前缀），
    用于「宁缺毋滥」的调用场景。
    """

    name = "keyword"

    # (matched 值, 连接方式, 是否前缀)
    TIERS = (
        ("all", "all", False),
        ("all-prefix", "all", True),
        ("any", "any", False),
        ("any-prefix", "any", True),
    )

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
        # bm25() 返回负值，越小越相关 —— 升序即最佳优先。
        # **这一行是排序的唯一来源。** 任何展示用字段（如 coverage）都不得插进来：
        # OR 档按命中词数重排已被 bench 实测证伪为负优化，别再试。
        sql.append(" ORDER BY score LIMIT ?")
        params.append(limit)
        return self.conn.execute("\n".join(sql), params).fetchall()

    @staticmethod
    def _to_hit(row, query: str, mode: str) -> Hit:
        body = row["body"]
        return Hit(
            card_id=int(row["id"]),
            rel_path=row["rel_path"],
            title=row["title"],
            kind=row["kind"],
            source=row["source"],
            score=-float(row["score"]),  # 取负，使「越大越相关」符合直觉
            snippet=make_snippet(body, query),
            matched=mode,
            coverage=compute_coverage(body, row["title"], query, matched=mode),
            _body=body,
        )

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        kind: str | None = None,
        source: str | None = None,
    ) -> list[Hit]:
        terms = query_terms(query)
        if not terms:
            return []

        tiers = self.TIERS if self.fallback_any else self.TIERS[:2]
        for mode, joiner, prefix in tiers:
            expr = to_query_expr(query, joiner, prefix=prefix)
            if not expr:
                continue
            rows = self._run(expr, limit, kind, source)
            if rows:
                return [self._to_hit(r, query, mode) for r in rows]
        return []
