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

# Windows 控制台默认不是 UTF-8。**三个流都要改**：只改 stdout/stderr 时，
# 写出去的干净、读进来的已经烂了 —— 中文系统上 stdin 按 cp936 解码，
# 管道喂进来的 UTF-8 正文会变成乱码，再编码时直接抛
# UnicodeEncodeError: surrogates not allowed（capture 读 stdin 就踩过这个坑）。
# errors="surrogateescape" 是防弹衣：宁愿留替换字符，也不让整个进程崩掉。
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is None:
        continue
    try:
        _reconfigure(encoding="utf-8", errors="surrogateescape")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcore import capture, config, importer, mcp_server, readtext, search, store  # noqa: E402
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
    accessed = store.access_stats(conn, limit=5)
    conn.close()

    payload = {
        "ok": True,
        "db": str(db),
        "db_bytes": db.stat().st_size,
        **s,
        # 访问统计在独立表里，index --rebuild 不会清它。
        # 口径：被 show/get 取过全文的次数，不含「仅被检索召回」。
        "most_accessed": accessed,
    }

    def render(d):
        print(f"{d['db']}  {d['db_bytes'] / 1024:.1f} KB")
        print(f"卡片 {d['total']}  正文字符 {d['chars']}")
        for label, key in (("kind", "by_kind"), ("source", "by_source"), ("status", "by_status")):
            for name, n in d[key]:
                print(f"  {label}:{name} {n}")
        if d["most_accessed"]:
            print("读取最多：")
            for item in d["most_accessed"]:
                print(f"  {item['access_count']:>3}×  {item['rel_path']}")

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

    if row is None:
        conn.close()
        payload = {"ok": False, "error": f"找不到 id={args.id}"}
        _emit(payload, args.json, lambda d: print(d["error"], file=sys.stderr))
        return 1

    # 取全文才算「读过」（与 MCP 的 memory_get 同一口径）：
    # 仅被检索召回不算访问，否则统计失去区分度。
    store.record_access(conn, row["rel_path"])
    stat = store.get_access_stat(conn, row["rel_path"]) or {}
    conn.close()

    # 长度窗口与 MCP 的 memory_get 共用同一份实现 —— 两个入口必须给出一致结果。
    window = readtext.render_window(
        row["body"],
        offset=0 if args.full else args.offset,
        max_chars=0 if args.full else args.max_chars,
    )

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
        "access_count": stat.get("access_count", 1),
        "last_accessed": stat.get("last_accessed", 0),
        **window,
    }

    def render(d):
        head = (f"# {d['title']}\n{d['path']}\n"
                f"长度 {d['length']} 字符，本次返回 {d['returned']}"
                f"（offset={d['offset']}）\n")
        print(head)
        print(d["text"])
        if d["has_more"]:
            # 截断必须说出来，并给出继续读的命令 —— 静默砍掉后半段就是假成功。
            print(f"\n…（还有 {d['length'] - d['offset'] - d['returned']} 字符未显示，"
                  f"继续读：memory.py show {d['id']} --offset {d['next_offset']}）",
                  file=sys.stderr)

    _emit(payload, args.json, render)
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
        on_conflict=args.on_conflict,
        reject_secrets=args.reject_secrets,
    )

    if not result["ok"]:
        # 撞车是「明确的拒绝」，与「参数不合法」要分开报：
        # 前者调用方改个标题或换策略就能过，后者得改参数。
        if result.get("action") == "conflict":
            _emit(result, args.json,
                  lambda d: print(f"标题撞车：{d['reason']}", file=sys.stderr))
            return 3
        _emit(result, args.json, lambda d: print(f"拒绝：{d['reason']}", file=sys.stderr))
        return 1

    # 索引失败不回滚 Markdown —— 真相源优先，索引可随时重建。
    indexed, warning = False, ""
    try:
        conn = store.connect(config.db_path(args.db))
        store.init(conn)
        importer.sync_one(conn, vault, result["path"])
        # 用 store.commit（自带锁重试），不要裸 conn.commit：
        # 提交也要抢写锁，并发 capture 时正是在这里失败的。
        store.commit(conn)
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
        # 敏感内容告警走 stderr：stdout 是给人/机器读的结果，告警属于旁路提示。
        # 注意告警**不改变退出码** —— 默认不阻断（见 capture.SECRET_PATTERNS 的说明）。
        for w in d.get("warnings", []) or []:
            print(f"警告：{w}", file=sys.stderr)
        if d.get("note"):
            print(d["note"], file=sys.stderr)
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
    ph.add_argument("--offset", type=int, default=0,
                    help="从第几个字符开始（长卡分页读，默认 0）")
    ph.add_argument("--max-chars", type=int, default=readtext.DEFAULT_MAX_CHARS,
                    help=f"本次最多返回多少字符（默认 {readtext.DEFAULT_MAX_CHARS}；0 表示不限）")
    ph.add_argument("--full", action="store_true",
                    help="返回完整正文（等价于 --max-chars 0）")
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
    pc.add_argument("--on-conflict", choices=capture.CONFLICT_POLICIES,
                    default="suffix",
                    help="标题撞车（同标题不同正文）时的处理："
                         "suffix=另存为 -2 新卡并回传冲突信息（默认）；reject=拒绝写入")
    pc.add_argument("--reject-secrets", action="store_true",
                    help="正文含疑似凭据时拒绝写入（默认**只告警**，见 README）")
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
