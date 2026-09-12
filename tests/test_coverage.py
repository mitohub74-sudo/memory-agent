# -*- coding: utf-8 -*-
"""查询词覆盖率（阶段 4 P4-02）。

这一项最容易做错的地方**不是**算不准覆盖率，而是**悄悄让它参与排序**。
本项目为此付过学费：曾经给 OR 档加过「按命中词数重排」，bench 实测证伪且是负优化
（目标卡从第 4 名掉到第 9 名，封顶 P@5 由 66.7% 降到 55.6%）。所以 ROADMAP 里
专门写了一条：**加 `coverage` 字段 ≠ 用它排序**，两者必须严格区分。

于是这里的测试分两类：

1. **语义正确**：AND 档恒 1.0；OR 档按实际命中的查询词占比；低置信提示只在放宽档出现；
2. **排序未被污染**：`bench --baseline` 逐项与基线相等 —— 这是「未参与排序」的
   唯一可验证证据。它不是保险条款，是**这条改动的前置条件**。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from mcore import capture, importer, search, store

ROOT = Path(__file__).resolve().parent.parent


def _corpus(root: Path) -> sqlite3.Connection:
    """三张卡：

    - 含全部查询词的卡（AND 档能独立命中）；
    - 只含查询词中一个词的卡（OR 档才会被召回 → 覆盖率 < 1）；
    - 一张完全无关的卡。
    """
    vault = root / "vault"
    for title, body in (
        ("阿里云 ECS 部署",
         "阿里云 ECS 部署方式：使用密钥登录，控制台在 RAM 权限页，"
         "部署脚本走 sqlite3 记录状态。"),
        # 刻意只用互不重叠的第三个词（阿里云 + 错误），且**不写**「部署」
        ("只有阿里云这个词的卡",
         "阿里云相关的另一条记录：某个子账号的权限申请流程，以及一次 error 排查。"),
        ("完全无关的卡",
         "备用主机的存储编号，以及机房的网线走向，与前面那些词都没有关系。"),
    ):
        assert capture.write_card(vault, title=title, body=body)["action"] == "created"

    conn = store.connect(root / "memory.db")
    store.init(conn)
    importer.sync(conn, vault)
    return conn


# ------------------------------------------------------------------ 语义


def test_all_tier_coverage_is_always_one() -> None:
    """AND 精确档按定义就是全部词命中 —— 覆盖率必须恒为 1.0。

    若这里返回小于 1 的值，说明近似算法与档位判定不一致；那种不一致应当修算法，
    而不是靠展示层掩盖（否则调用方会怀疑「明明精确档为什么说我只覆盖了 80%」）。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-cov-") as raw:
        conn = _corpus(Path(raw))
        try:
            hits = search.KeywordSearcher(conn).search("阿里云 部署", limit=5)
            assert hits, "应当命中"
            assert hits[0].matched == "all"
            for h in hits:
                assert h.coverage == 1.0, (h.title, h.coverage)
        finally:
            conn.close()


def test_or_tier_coverage_reflects_partial_match() -> None:
    """OR 档必须算出 < 1 的覆盖率，且覆盖更多的卡排前面。

    夹具的坑（第一版就踩了）：「同一张卡」上的词必须**互不重叠**，
    否则两张卡的覆盖率会一样，看不出差别。第一版里第二张卡写了
    「某台机器的磁盘型号」和「错误日志」—— 而「机」与「器」在
    「机型」「机器」里都出现了，「err」是「error」的前缀，于是两张卡
    都覆盖了 3/4 个词、覆盖率同为 75%，断言「A > B」失败。
    那次失败是**夹具的问题**，但它顺带证明覆盖率算对了：它数的是
    「各个词在这张卡里出现过没有」，同一批词在两张卡上都出现就该同分。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-cov-") as raw:
        conn = _corpus(Path(raw))
        try:
            # 加一个语料里不存在的词 → AND 必然失败 → 走 OR 档
            loose = search.KeywordSearcher(conn).search("阿里云 部署 zzznotexist", limit=10)
            assert loose and loose[0].matched == "any", loose and loose[0].matched
            for h in loose:
                assert 0.0 < h.coverage <= 1.0, (h.title, h.coverage)

            by_title = {h.title: h.coverage for h in loose}
            assert by_title["阿里云 ECS 部署"] == 0.75, by_title          # 3/4 词
            assert by_title["只有阿里云这个词的卡"] == 0.5, by_title        # 2/4 词
            assert by_title["阿里云 ECS 部署"] > by_title["只有阿里云这个词的卡"], by_title
            assert loose[0].title == "阿里云 ECS 部署", (
                f"覆盖更多的卡应排前面（这是 BM25 的结果，不是 coverage 排的）："
                f"{[h.title for h in loose]}"
            )
        finally:
            conn.close()


def test_coverage_is_bounded_and_rounded() -> None:
    for matched in ("all", "all-prefix"):
        assert search.compute_coverage("任意正文", "标题", "任意查询", matched=matched) == 1.0
    # 空查询：没有词可覆盖，返回 0 而不是 1 —— 1 会谎称「全覆盖」
    assert search.compute_coverage("正文", "标题", "", matched="any") == 0.0
    # 空正文：一个词都没覆盖
    assert search.compute_coverage("", "", "阿里云", matched="any") == 0.0


def test_low_confidence_note_only_on_relaxed_tiers() -> None:
    assert search.low_confidence_note("all", 1.0) == ""
    assert search.low_confidence_note("all-prefix", 1.0) == ""

    partial = search.low_confidence_note("any", 0.5)
    assert partial, "放宽档且覆盖不全时必须给出提示"
    assert "50%" in partial, partial
    assert "关键词" in partial or "实体名" in partial, "提示要可执行，不能只报数字"

    # 放宽档但词都命中了：仍要提示档位本身可信度较低，但不该说「只命中部分」
    full = search.low_confidence_note("any", 1.0)
    assert full and "只命中了部分" not in full, full


# ------------------------------------------------------------------ 排序未被污染


def test_ranking_comes_only_from_bm25_not_coverage() -> None:
    """「coverage 未参与排序」的直接证据。

    做法是把**排序层**（``_run`` 的 SQL 结果）与**完整检索结果**（``search()``）
    逐条对比：两者的顺序、id、分数必须完全一致。若 coverage 或任何展示逻辑
    介入了排序，顺序就会分叉 —— 而搜索只是把 ``_run`` 的行按顺序包成 Hit。

    为什么不用「bench 分数等于基线」来证明：本机语料会被外部 agent 持续写入
    （49 → 53 张），语料一变指标就会动，那是**语料变化**不是排序变化，
    拿它当断言会得到一个长期假红。排序层的顺序才是稳定且与语料规模无关的证据。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-cov-") as raw:
        conn = _corpus(Path(raw))
        try:
            searcher = search.KeywordSearcher(conn)
            for query in ("阿里云", "阿里云 部署", "阿里云 部署 zzznotexist"):
                terms = search.query_terms(query)
                assert terms, query

                # 找到实际生效的档位（与 search() 内部的降级顺序一致）
                chosen = None
                for mode, joiner, prefix in searcher.TIERS:
                    expr = search.to_query_expr(query, joiner, prefix=prefix)
                    if not expr:
                        continue
                    rows = searcher._run(expr, 10, None, None)
                    if rows:
                        chosen = (mode, rows)
                        break
                assert chosen is not None, query
                mode, rows = chosen

                hits = searcher.search(query, limit=10)
                assert [h.card_id for h in hits] == [int(r["id"]) for r in rows], (
                    f"检索结果顺序与排序层不一致（{query}）："
                    f"{[h.card_id for h in hits]} vs {[int(r['id']) for r in rows]}"
                )
                assert [round(h.score, 9) for h in hits] == \
                    [round(-float(r["score"]), 9) for r in rows], query

                # 明细：命中数与分母一致，且 coverage 落在 [0,1]
                for h in hits:
                    assert 0.0 <= h.coverage <= 1.0
                    if mode in ("all", "all-prefix"):
                        assert h.coverage == 1.0
        finally:
            conn.close()


def test_ranking_sql_contains_no_display_fields() -> None:
    """结构护栏：排序 SQL 的字符串里不得出现展示字段。

    看的是 SQL **字符串片段**而不是函数源码 —— 第一版检查源码文本，
    结果被注释里那句「任何展示用字段（如 coverage）都不得插进来」自己绊倒。
    断言要盯住真正执行的东西，不要盯住注释。
    """
    import inspect
    import re

    src = inspect.getsource(search.KeywordSearcher._run)
    # 只取 sql.append("...") 与 SQL 列表里的字面量
    literals = re.findall(r'"([^"]*)"', src)
    sql_text = " ".join(literals)
    assert "ORDER BY" in sql_text.upper(), "排序必须存在"
    assert "coverage" not in sql_text.lower(), "展示字段不得进入排序 SQL"
    # 排序来源唯一：只有 bm25
    assert "bm25(" in sql_text, "排序来源必须是 bm25"
