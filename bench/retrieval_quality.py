#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索质量基准 —— 让分数**能下降**。

为什么需要它
------------
之前用临时脚本评测过两次，两次都得出过错误结论：

  1. 真值用整数 ``card.id`` 记录。索引一旦重建，rowid 重排，真值全部错位 ——
     卡 12 从「本机 Ollama 已装模型」变成了「渗透测试复盘」，于是「首位全错」
     这个结论本身就是假的。
  2. 真值只认「事先指定的那一张卡」。但「怎么登录远程主机」返回
     「阿里云ECS运维与DSH远程操作备忘」其实是正确答案，不该判错。
  3. 真值为空（查询词在语料里根本不存在）的查询被算成失败，污染总分。

所以本基准有三条硬规定：

  * **真值按 ``rel_path`` 记录，不按 id。** rel_path 是业务主键，重建索引不变。
  * **真值是一组「可接受卡」，不是一张「标准卡」。**
  * **真值为空的查询不计入精确率**，只单独报告 —— 语料里没有这个词，返回空
    才是正确行为。

指标口径
--------
  first_ok  首位是否可接受 —— agent 最常只看第一条
  p@5       top5 里可接受卡占比 —— 直接对应「会不会一次返回一堆无用结果」
  p@10      top10 里可接受卡占比
  tier      实际生效的匹配档位，用于判断是否降级到了 OR

分组：真值文件里可给每条查询标 ``group``（如 ``A`` 关键词 / ``B`` 自然语言），
报告会分组汇总 —— 这两类的失败机制完全不同，混在一起看没有意义。

真值文件与代码分离：默认读 ``~/.memory_agent/bench_queries.json``，
仓库内不含任何本机数据。

用法
----
    python bench/retrieval_quality.py                     人类可读
    python bench/retrieval_quality.py --json              机器可读
    python bench/retrieval_quality.py --save base.json    存基线
    python bench/retrieval_quality.py --baseline base.json   对比基线，退化则退出码 1
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcore import config, search  # noqa: E402

DEFAULT_QUERIES = "bench_queries.json"

# 退化判定用的聚合指标（越大越好）
METRICS = ("first_ok_rate", "capped_p_at_5", "capped_p_at_10")


def load_cards(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, rel_path, title, body FROM cards ORDER BY id"
        )
    ]


def auto_acceptable(cards: list[dict], query: str, mode: str) -> set[str]:
    """从卡片原文算标准答案。中文按子串，英文按词界。

    英文词界刻意把 ``_ . / -`` 当**分隔符**，与 FTS5 的 unicode61 保持一致。
    实测（fts5vocab）：索引里没有 ``id_ed25519`` 这个 term，只有 ``id`` 和
    ``ed25519`` —— unicode61 会按 ``_`` 切开。所以搜 ``ed25519`` 命中
    ``id_ed25519`` 是**正确**的，真值也必须认它，否则会误判为噪音。

    注意：本口径是**整词**匹配，``sqlite`` 不认 ``sqlite3``。这是刻意的 ——
    真值必须独立于检索实现，否则就是循环论证。前缀档多召回多少、其中多少是
    噪音，看报告里的 ``tier`` 列与 ``noise_at_5`` 自行判断。
    """
    out: set[str] = set()
    if mode == "en":
        pat = re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(query) + r"(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        for c in cards:
            if pat.search(c["title"] + "\n" + c["body"]):
                out.add(c["rel_path"])
    else:
        for c in cards:
            if query in (c["title"] + "\n" + c["body"]):
                out.add(c["rel_path"])
    return out


def run_query(searcher, cards, spec: dict, limit: int) -> dict:
    query = spec["query"]
    if "acceptable" in spec:
        gold = set(spec["acceptable"])
        source = "explicit"
    else:
        gold = auto_acceptable(cards, query, spec.get("auto", "zh"))
        source = f"auto:{spec.get('auto', 'zh')}"

    hits = searcher.search(query, limit=limit)
    paths = [h.rel_path for h in hits]

    first_rank = next((i for i, p in enumerate(paths, 1) if p in gold), None)
    in5 = sum(1 for p in paths[:5] if p in gold)
    in10 = sum(1 for p in paths[:10] if p in gold)

    # 封顶精确率：相关卡总数可能少于 5，此时取满 top5 也填不满，那是
    # 「相关卡用完了」而不是「返回了噪音」。所以分母取 min(K, 相关卡数)。
    # 这个口径才真正回答「会不会一次返回一堆无用结果」。
    cap5 = min(5, len(gold))
    cap10 = min(10, len(gold))

    return {
        "query": query,
        "group": spec.get("group", "all"),
        "gold_size": len(gold),
        "returned": len(hits),
        "gold_source": source,
        "first_ok": bool(hits) and hits[0].rel_path in gold,
        "first_rank": first_rank,
        "in_top5": in5,
        "in_top10": in10,
        "noise_at_5": round((cap5 - min(in5, cap5)) / cap5, 4) if cap5 else None,
        "tier": hits[0].matched if hits else "empty",
        "top1": hits[0].title if hits else "",
    }


def aggregate(rows: list[dict]) -> dict:
    """只统计「真值非空」的查询。

    真值为空 = 查询词在语料里不存在，此时返回空是**正确**行为，
    把它算成失败会让总分毫无意义（实测 19 条查询里有 4 条属于这种）。
    """
    scored = [r for r in rows if r["gold_size"] > 0]
    n = len(scored)
    out = {"n": n, "no_gold": len(rows) - n}
    if not n:
        return out
    tiers: dict[str, int] = {}
    for r in scored:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1

    # 封顶召回：分母是「top5 位置上实际存在的相关卡数」，把「相关卡不足 5 张」
    # 的情况排除掉，剩下的缺口才是真噪音。用微平均（总量相除），避免被
    # 单条查询的极端比例带偏。
    avail5 = sum(min(5, r["gold_size"]) for r in scored)
    avail10 = sum(min(10, r["gold_size"]) for r in scored)
    got5 = sum(min(r["in_top5"], min(5, r["gold_size"])) for r in scored)
    got10 = sum(min(r["in_top10"], min(10, r["gold_size"])) for r in scored)

    out.update(
        {
            "first_ok_rate": round(sum(1 for r in scored if r["first_ok"]) / n, 4),
            "p_at_5": round(sum(r["in_top5"] for r in scored) / (5 * n), 4),
            "p_at_10": round(sum(r["in_top10"] for r in scored) / (10 * n), 4),
            "capped_p_at_5": round(got5 / avail5, 4) if avail5 else None,
            "capped_p_at_10": round(got10 / avail10, 4) if avail10 else None,
            "noise_at_5": round((avail5 - got5) / avail5, 4) if avail5 else None,
            "tiers": dict(sorted(tiers.items())),
        }
    )
    return out


def by_group(rows: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    return {g: aggregate(v) for g, v in sorted(groups.items())}


def _pct(v) -> str:
    return f"{v * 100:.1f}%" if isinstance(v, (int, float)) else "—"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="memory-agent 检索质量基准")
    ap.add_argument("--db", help="索引库路径（默认取 config 解析结果）")
    ap.add_argument("--queries", help=f"真值文件（默认 {{data_home}}/{DEFAULT_QUERIES}）")
    ap.add_argument("--limit", type=int, default=10, help="每条查询取前 N 条（默认 10）")
    ap.add_argument("--baseline", help="基线 JSON，对比并在退化时返回退出码 1")
    ap.add_argument("--save", help="把本次结果写入 JSON 文件（可当基线用）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)

    db = config.db_path(args.db)
    qfile = (
        Path(args.queries).expanduser()
        if args.queries
        else config.data_home() / DEFAULT_QUERIES
    )

    if not db.exists():
        print(f"索引不存在：{db}", file=sys.stderr)
        return 2
    if not qfile.exists():
        print(f"真值文件不存在：{qfile}", file=sys.stderr)
        return 2

    spec = json.loads(qfile.read_text(encoding="utf-8"))
    queries = spec.get("queries", spec if isinstance(spec, list) else [])
    if not queries:
        print("真值文件里没有 queries", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db)
    cards = load_cards(conn)
    searcher = search.KeywordSearcher(conn)
    rows = [run_query(searcher, cards, q, args.limit) for q in queries]
    conn.close()

    agg = aggregate(rows)
    groups = by_group(rows)
    payload = {
        "ok": True,
        "db": str(db),
        "queries_file": str(qfile),
        "cards": len(cards),
        "limit": args.limit,
        "aggregate": agg,
        "by_group": groups,
        "per_query": rows,
    }

    if args.save:
        Path(args.save).expanduser().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    regressed: list[str] = []
    if args.baseline:
        base = json.loads(Path(args.baseline).expanduser().read_text(encoding="utf-8"))
        for scope, old_scope in (("aggregate", base.get("aggregate", {})),
                                 *[(g, base.get("by_group", {}).get(g, {}))
                                   for g in groups]):
            new_scope = agg if scope == "aggregate" else groups[scope]
            for m in METRICS:
                old, new = old_scope.get(m), new_scope.get(m)
                if old is not None and new is not None and new < old:
                    regressed.append(f"[{scope}] {m}: {old} → {new}")
        payload["baseline"] = str(args.baseline)
        payload["regressed"] = regressed

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{db}")
        print(f"{len(cards)} 张卡 · {len(queries)} 条查询 · top{args.limit}\n")
        print(f"{'查询':<26}{'组':>3}{'首位':>5}{'@5':>6}{'@10':>7}{'首个排名':>10}{'档位':>11}")
        print("-" * 70)
        for r in rows:
            rank = r["first_rank"] if r["first_rank"] else "—"
            mark = "✅" if r["first_ok"] else ("∅" if r["gold_size"] == 0 else "❌")
            print(
                f"{r['query']:<26}{r['group']:>3}{mark:>5}"
                f"{r['in_top5']:>4}/5{r['in_top10']:>4}/10{rank:>11}{r['tier']:>11}"
            )
        print("-" * 70)
        print("∅ = 查询词在语料里不存在，返回空是正确的，不计入精确率\n")

        for g, a in groups.items():
            if a.get("n"):
                print(
                    f"[{g}] {a['n']} 条计分（另有 {a['no_gold']} 条真值为空）  "
                    f"首位可接受 {_pct(a['first_ok_rate'])}  "
                    f"封顶P@5 {_pct(a['capped_p_at_5'])}  "
                    f"封顶P@10 {_pct(a['capped_p_at_10'])}  "
                    f"噪音率@5 {_pct(a['noise_at_5'])}  "
                    f"档位 {a['tiers']}"
                )
        print()
        print(
            f"总计 {agg['n']} 条计分  首位可接受 {_pct(agg['first_ok_rate'])}  "
            f"封顶P@5 {_pct(agg['capped_p_at_5'])}  封顶P@10 {_pct(agg['capped_p_at_10'])}  "
            f"噪音率@5 {_pct(agg['noise_at_5'])}"
        )
        if args.baseline:
            print()
            if regressed:
                print("⚠ 相比基线退化：")
                for line in regressed:
                    print(f"   {line}")
            else:
                print("✓ 相比基线无退化")

    return 1 if regressed else 0


if __name__ == "__main__":
    sys.exit(main())
