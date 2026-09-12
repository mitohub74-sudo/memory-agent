#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory-agent CLI —— 面向大模型的本地记忆检索。

项目定位
--------
主体是给 agent 用的，不是给人看的。因此：
  - 所有命令都提供 ``--json``，机器可直接消费
  - 输出保持朴素，不做展示层
  - 数据与代码分离，仓库内不存任何记忆

用法
----
    python memory.py paths                  查看当前生效的路径
    python memory.py index                  同步 vault 到索引
    python memory.py index --rebuild        清空索引后全量重建
    python memory.py search 密钥            检索
    python memory.py search 密钥 --json     机器可读输出
    python memory.py stats --json
    python memory.py show 27
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 控制台默认不是 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcore import capture, config, importer, mcp_server, search, store  # noqa: E402
from mcore.version import __version__  # noqa: E402


def _emit(payload: dict | list, as_json: bool, render) -> None:
    """统一出口：JSON 或朴素文本。"""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        render(payload)


def _require_db(db: Path, as_json: bool) -> bool:
    """索引不存在就报错返回 False。

    必须接收**已解析的** db 路径，而不是自己重新调一次 ``config.db_path()`` ——
    否则 ``--db X`` 指向一个不存在的库时，这里检查的仍是默认路径，守卫形同虚设。
    """
    if db.exists():
        return True
    msg = f"索引不存在：{db}"
    if as_json:
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    else:
        print(f"{msg}\n先运行：python memory.py index", file=sys.stderr)
    return False


# --------------------------------------------------------------------- 命令


def cmd_paths(args) -> int:
    info = config.describe(args.db)
    _emit(
        info,
        args.json,
        lambda d: print("\n".join(f"{k:<14} {v}" for k, v in d.items())),
    )
    return 0


def cmd_index(args) -> int:
    vault = config.vault_path(args.vault)
    db = config.db_path(args.db)
    if not vault.is_dir():
        payload = {"ok": False, "error": f"vault 目录不存在：{vault}"}
        _emit(payload, args.json, lambda d: print(d["error"], file=sys.stderr))
        return 2

    conn = store.connect(db)
    store.init(conn)
    counts = importer.sync(conn, vault, rebuild=args.rebuild)
    payload = {
        "ok": True,
        "mode": "rebuild" if args.rebuild else "incremental",
        "vault": str(vault),
        "db": str(db),
        "total": store.count_cards(conn),
        **counts,
    }
    conn.close()

    _emit(
        payload,
        args.json,
        lambda d: print(
            f"{'全量重建' if d['mode'] == 'rebuild' else '增量同步'}完成  "
            f"新增 {d['inserted']} · 更新 {d['updated']} · "
            f"未变 {d['unchanged']} · 移除 {d['removed']}  共 {d['total']} 张"
        ),
    )
    return 0


def cmd_search(args) -> int:
    db = config.db_path(args.db)
    if not _require_db(db, args.json):
        return 2

    limit = search.clamp_limit(args.limit)
    conn = store.connect(db)
    hits = search.KeywordSearcher(conn).search(
        args.query, limit=limit, kind=args.kind, source=args.source
    )
    conn.close()

    payload = {
        "ok": bool(hits),
        "query": args.query,
        "count": len(hits),
        "results": [
            {
                "id": h.card_id,
                "path": h.rel_path,
                "title": h.title,
                "kind": h.kind,
                "source": h.source,
                "score": round(h.score, 4),
                "matched": h.matched,
                "snippet": h.snippet,
            }
            for h in hits
        ],
    }

    def render(d):
        if not d["results"]:
            print(f"未命中：{d['query']}")
            return
        for i, r in enumerate(d["results"], 1):
            print(f"{i}. [{r['score']:.3f}] {r['title']}  (id={r['id']})")
            print(f"   {r['path']}  {r['kind']}  {r['source']}")
            if r["snippet"]:
                print(f"   {r['snippet']}")

    _emit(payload, args.json, render)
    return 0 if hits else 1


def cmd_stats(args) -> int:
    db = config.db_path(args.db)
    if not _require_db(db, args.json):
        return 2

    conn = store.connect(db)
    s = store.stats(conn)
    conn.close()

    payload = {"ok": True, "db": str(db), "db_bytes": db.stat().st_size, **s}

    def render(d):
        print(f"{d['db']}  {d['db_bytes'] / 1024:.1f} KB")
        print(f"卡片 {d['total']}  正文字符 {d['chars']}")
        for label, key in (("kind", "by_kind"), ("source", "by_source"), ("status", "by_status")):
            for name, n in d[key]:
                print(f"  {label}:{name} {n}")

    _emit(payload, args.json, render)
    return 0


def cmd_show(args) -> int:
    db = config.db_path(args.db)
    # 守卫必须在 connect 之前 —— connect() 会顺手建库文件，
    # 那样「索引不存在」就退化成「库存在但没这张卡」，退出码也从 2 变成 1。
    if not _require_db(db, args.json):
        return 2

    conn = store.connect(db)
    row = store.get_card(conn, args.id)
    conn.close()

    if row is None:
        payload = {"ok": False, "error": f"找不到 id={args.id}"}
        _emit(payload, args.json, lambda d: print(d["error"], file=sys.stderr))
        return 1

    payload = {
        "ok": True,
        "id": row["id"],
        "path": row["rel_path"],
        "title": row["title"],
        "kind": row["kind"],
        "source": row["source"],
        "status": row["status"],
        "tags": json.loads(row["tags"] or "[]"),
        "updated": row["updated"],
        "body": row["body"],
    }
    _emit(
        payload,
        args.json,
        lambda d: print(f"# {d['title']}\n{d['path']}\n\n{d['body']}"),
    )
    return 0


def cmd_capture(args) -> int:
    """写入一张知识卡。正文取自 --body，未给出则从 stdin 读。

    落盘后**立刻索引本卡**，与 MCP 的 ``memory_capture`` 行为一致 ——
    否则同一个动作走两个入口会有两种结果：agent 通过 MCP 写能立即搜到，
    人通过 CLI 写却搜不到，而两边返回的都是「成功」。
    """
    body = args.body or ""
    if not body and not sys.stdin.isatty():
        body = sys.stdin.read()

    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    vault = config.vault_path(args.vault)

    result = capture.write_card(
        vault,
        title=args.title,
        body=body,
        kind=args.kind,
        tags=tags,
        source=args.source,
    )

    if not result["ok"]:
        _emit(result, args.json, lambda d: print(f"拒绝：{d['reason']}", file=sys.stderr))
        return 1

    # 索引失败不回滚 Markdown —— 真相源优先，索引可随时重建。
    indexed, warning = False, ""
    try:
        conn = store.connect(config.db_path(args.db))
        store.init(conn)
        importer.sync_one(conn, vault, result["path"])
        conn.commit()
        conn.close()
        indexed = True
    except Exception as exc:
        warning = (f"卡片已落盘（{result['path']}），但索引失败：{exc}。"
                   f"可运行 python memory.py index 补建。")

    payload = {**result, "indexed": indexed}
    if warning:
        payload["warning"] = warning

    def render(d):
        mark = "已索引" if d.get("indexed") else "未索引"
        print(f"{d['action']}  {d.get('path', '')}  [{mark}]")
        if d.get("warning"):
            print(d["warning"], file=sys.stderr)

    _emit(payload, args.json, render)
    return 0


def cmd_mcp(args) -> int:
    """启动 MCP stdio server。stdout 是协议通道，日志走 stderr。"""
    return mcp_server.serve()


def cmd_bench(args) -> int:
    """运行检索质量基准。实现放在 bench/retrieval_quality.py。

    真值文件默认取数据目录下的 bench_queries.json —— 与代码分离，仓库内不含
    本机数据。真值按 rel_path 记录而非整数 id：索引重建后 rowid 会重排，
    按 id 记真值会导致错位（踩过一次，结论因此全错）。
    """
    import importlib.util

    path = Path(__file__).resolve().parent / "bench" / "retrieval_quality.py"
    spec = importlib.util.spec_from_file_location("_memory_bench", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    argv: list[str] = ["--limit", str(args.limit)]
    for flag, val in (
        ("--db", args.db),
        ("--queries", args.queries),
        ("--baseline", args.baseline),
        ("--save", args.save),
    ):
        if val:
            argv += [flag, val]
    if args.json:
        argv.append("--json")
    return mod.main(argv)


# --------------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory",
        description="memory-agent —— 面向大模型的本地记忆检索（Markdown 为真相源）",
    )
    p.add_argument("--db", help="索引库路径（默认取 ~/.memory_agent/memory.db）")
    p.add_argument("--version", action="version",
                   version=f"memory-agent {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    pp = sub.add_parser("paths", help="显示当前生效的路径")
    pp.add_argument("--json", action="store_true")
    pp.set_defaults(func=cmd_paths)

    pi = sub.add_parser("index", help="同步 vault 到索引")
    pi.add_argument("--vault", help="Markdown vault 目录")
    pi.add_argument("--rebuild", action="store_true", help="清空索引后全量重建")
    pi.add_argument("--json", action="store_true")
    pi.set_defaults(func=cmd_index)

    ps = sub.add_parser("search", help="关键词检索")
    ps.add_argument("query")
    ps.add_argument("-n", "--limit", type=int, default=10)
    ps.add_argument("--kind", help="按类型过滤")
    ps.add_argument("--source", help="按来源过滤")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_search)

    pt = sub.add_parser("stats", help="统计概览")
    pt.add_argument("--json", action="store_true")
    pt.set_defaults(func=cmd_stats)

    ph = sub.add_parser("show", help="查看卡片全文")
    ph.add_argument("id", type=int)
    ph.add_argument("--json", action="store_true")
    ph.set_defaults(func=cmd_show)

    pm = sub.add_parser("mcp", help="启动 MCP stdio server（供 agent 调用）")
    pm.set_defaults(func=cmd_mcp)

    pb = sub.add_parser("bench", help="检索质量基准（真值在数据目录，不进仓库）")
    pb.add_argument("--queries", help="真值文件，默认 {数据目录}/bench_queries.json")
    pb.add_argument("-n", "--limit", type=int, default=10, help="每条查询取前 N 条")
    pb.add_argument("--baseline", help="基线 JSON；任一指标退化则退出码 1")
    pb.add_argument("--save", help="把本次结果写入 JSON（可当基线）")
    pb.add_argument("--json", action="store_true")
    pb.set_defaults(func=cmd_bench)

    pc = sub.add_parser("capture", help="写入一张知识卡（采集端）")
    pc.add_argument("--title", required=True, help="卡片标题")
    pc.add_argument("--body", help="正文；省略则从 stdin 读取")
    pc.add_argument("--kind", default="knowledge",
                    help="类型：knowledge / project / mistake / prompt / tool / "
                         "content / business / system（默认 knowledge）")
    pc.add_argument("--tags", help="标签，逗号分隔")
    pc.add_argument("--source", default="agent", help="来源标记，默认 agent")
    pc.add_argument("--vault", help="vault 目录")
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(func=cmd_capture)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
