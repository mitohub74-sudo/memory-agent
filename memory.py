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

from mcore import capture, config, importer, mcp_server, readtext, search, store, util  # noqa: E402
from mcore.util import parse_tags  # noqa: E402
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
    # errors 单独取出来给渲染用：它是「哪些卡没进去」的清单，
    # 不能让调用方从 total 的数字差里猜。
    errors = counts.get("errors", [])
    payload = {
        "ok": True,
        "mode": "rebuild" if args.rebuild else "incremental",
        "vault": str(vault),
        "db": str(db),
        "total": store.count_cards(conn),
        "error_count": len(errors),
        **counts,
    }
    conn.close()

    _emit(
        payload,
        args.json,
        lambda d: _render_index(d, errors),
    )
    return 0


def _render_index(d: dict, errors: list) -> None:
    """同步结果的人类可读输出。坏文件走 stderr —— 它们是告警，不是结果。"""
    print(
        f"{'全量重建' if d['mode'] == 'rebuild' else '增量同步'}完成  "
        f"新增 {d['inserted']} · 更新 {d['updated']} · "
        f"未变 {d['unchanged']} · 移除 {d['removed']}  共 {d['total']} 张"
    )
    # 坏文件必须被明确说出来。静默跳过等于「索引成功」掩盖了「有几张没进去」，
    # 而调用方会据此以为检索覆盖了全部语料。
    for item in errors:
        print(f"跳过（未索引）{item['path']}：{item['error']}", file=sys.stderr)


def cmd_search(args) -> int:
    db = config.db_path(args.db)
    if not _require_db(db, args.json):
        return 2

    limit = search.clamp_limit(args.limit)
    try:
        # 日期在这里校验（与 MCP 共用同一个实现）。不合法就明确失败 ——
        # 静默当成「没传日期」会让调用方拿到一批看起来正常的结果。
        as_of = search.normalize_as_of(args.as_of)
    except ValueError as exc:
        payload = {"ok": False, "error": str(exc)}
        _emit(payload, args.json, lambda d: print(d["error"], file=sys.stderr))
        return 2

    conn = store.connect(db)
    hits = search.KeywordSearcher(conn).search(
        args.query, limit=limit, kind=args.kind, source=args.source,
        as_of=as_of, include_invalid=args.include_invalid,
    )
    conn.close()

    payload = {
        "ok": bool(hits),
        "query": args.query,
        "count": len(hits),
        # 档位标签与 MCP 输出同源（search.MODE_LABELS）—— 两个入口不能各写一份。
        "mode": search.mode_label(hits[0].matched) if hits else "",
        # 低置信提示：对整批结果的判断，让调用方知道该不该信这批结果。
        # coverage 只反映「命中了多少查询词」，**没有参与排序**（见 search.Hit 的说明）。
        "confidence_note": (search.low_confidence_note(hits[0].matched, hits[0].coverage)
                            if hits else ""),
        # 回溯参数原样回传：调用方（和人）必须能看出「这批结果是哪个时点的视图」，
        # 否则历史结果会被当成当前事实。
        "as_of": as_of,
        "include_invalid": bool(args.include_invalid),
        "results": [
            {
                "id": h.card_id,
                "path": h.rel_path,
                "title": h.title,
                "kind": h.kind,
                "source": h.source,
                "score": round(h.score, 4),
                "matched": h.matched,
                "matched_label": search.mode_label(h.matched),
                "coverage": h.coverage,
                "snippet": h.snippet,
                # 默认检索里恒为空；显式要失效卡时用它标注「这条已经过期」。
                "invalid_at": h.invalid_at,
            }
            for h in hits
        ],
    }

    def render(d):
        if not d["results"]:
            print(f"未命中：{d['query']}")
            return
        scope = ""
        if d["as_of"]:
            scope = f"，回溯到 {d['as_of']} 当时有效"
        elif d["include_invalid"]:
            scope = "，含已失效的卡"
        print(f"命中 {d['count']} 条  (query={d['query']}, 匹配模式={d['mode']}{scope})\n")
        for i, r in enumerate(d["results"], 1):
            # 精确档覆盖率恒为 1.0，显示出来只是噪音，所以只在放宽档显示。
            cover = "" if r["matched"] in ("all", "all-prefix") else f"  覆盖率={r['coverage']:.0%}"
            stale = f"  ⚠已于 {r['invalid_at']} 失效" if r["invalid_at"] else ""
            print(f"{i}. [{r['score']:.3f}] {r['title']}  (id={r['id']}){cover}{stale}")
            print(f"   {r['path']}  {r['kind']}  {r['source']}")
            if r["snippet"]:
                print(f"   {r['snippet']}")
        if d["confidence_note"]:
            print(f"\n{d['confidence_note']}")

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

    # 回收站单独报：A3 的决定是「不自动清理，攒到一定量提醒」，所以 stats 是
    # 用户看到「该清了」的自然位置。这里只数，绝不自动动手。
    trash = capture.trash_summary(config.vault_path(args.vault))

    payload = {
        "ok": True,
        "db": str(db),
        "db_bytes": db.stat().st_size,
        **s,
        # 访问统计在独立表里，index --rebuild 不会清它。
        # 口径：被 show/get 取过全文的次数，不含「仅被检索召回」。
        "most_accessed": accessed,
        "trash": trash,
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
        trash = d["trash"]
        if trash["count"]:
            print(f"回收站 {trash['count']} 张 · {trash['bytes'] / 1024:.1f} KB"
                  f"（不自动清理：python memory.py delete --purge --older-than 30d）")
            if trash["unknown_deleted_at"]:
                print(f"  其中 {trash['unknown_deleted_at']} 张没有删除时间记录，"
                      f"--older-than 会跳过它们", file=sys.stderr)

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
        # 失效与取代关系（A2）：旧卡**原文照常返回**，只是明确标注它已经被取代。
        # 「取代」的语义是默认检索里不再返回，不是把内容藏起来 ——
        # 拿到 id 却打不开，会让 --as-of 的价值大打折扣。
        "invalid_at": row["invalid_at"] or "",
        "superseded_by": row["superseded_by"] or "",
        "supersedes": row["supersedes"] or "",
        **window,
    }

    def render(d):
        # 标注放在正文**之前**：默认检索不会返回它，所以打开它的人（或 agent）
        # 必须先看到「这条已经不是当前事实」，否则会把历史当成现状用。
        if d["invalid_at"]:
            by = f"，被 {d['superseded_by']} 取代" if d["superseded_by"] else ""
            print(f"⚠ 这张卡已于 {d['invalid_at']} 失效{by} —— "
                  f"默认检索不会再返回它；要查当时的事实用 search <词> --as-of <日期>\n")
        if d["supersedes"]:
            print(f"（本卡取代了 {d['supersedes']}）\n")
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

    # 与 MCP 的 memory_capture 共用同一个解析实现（util.parse_tags），
    # 两个入口不能各写一份 —— 那正是 P1 分叉的成因。
    tags = parse_tags(args.tags)
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


def cmd_update(args) -> int:
    """原地修改一张卡（P3-02）。

    两个入口的分工写在帮助里，因为这是调用方最容易选错的地方：
    **事实写错了用 update，事实变了用 supersede**（后者保留「当时是多少」）。

    先用 id 在索引里查出 rel_path，再按路径改文件 —— id 是 rowid，
    ``index --rebuild`` 后会重排，不能拿它当落盘依据。
    """
    db = config.db_path(args.db)
    if not _require_db(db, args.json):
        return 2

    conn = store.connect(db)
    row = store.get_card(conn, args.id)
    conn.close()

    if row is None:
        payload = {"ok": False, "error": f"找不到 id={args.id}"}
        _emit(payload, args.json, lambda d: print(d["error"], file=sys.stderr))
        return 1

    vault = config.vault_path(args.vault)
    # tags 必须区分「没传」（None，别动）与「传了空串」（清空标签）。
    # 写成 `or None` 就会把后者也当成前者，于是「清空标签」静默失效。
    result = capture.update_card(
        vault,
        row["rel_path"],
        title=args.title,
        body=args.body,
        kind=args.kind,
        tags=parse_tags(args.tags) if args.tags is not None else None,
        priority=args.priority,
        ttl=args.ttl,
        source=args.source,
    )

    if not result["ok"]:
        _emit(result, args.json,
              lambda d: print(f"未改动：{d['reason']}", file=sys.stderr))
        return 1

    # indexed 三态，刻意不用 false 表示「没变化」：
    #   true  已重新索引
    #   false 写盘成功但索引失败（见 warning）
    #   null  没有字段变化，未写盘、无需索引
    # 用 false 兼表后两者，调用方会把「什么都没改」错读成「索引坏了」。
    indexed: bool | None = None
    warning = ""
    if result["changed"]:
        try:
            conn = store.connect(db)
            store.init(conn)
            importer.sync_one(conn, vault, result["path"])
            # 提交统一走自带锁重试的 store.commit，不要裸 conn.commit()。
            store.commit(conn)
            conn.close()
            indexed = True
        except Exception as exc:
            indexed = False
            warning = (f"卡片已改（{result['path']}），但索引失败：{exc}。"
                       f"可运行 python memory.py index 补建。")

    payload = {"id": row["id"], **result, "indexed": indexed}
    if warning:
        payload["warning"] = warning

    def render(d):
        if not d["changed"]:
            print(f"未改动：{d['reason']}")
        else:
            mark = "已索引" if d["indexed"] else "未索引"
            print(f"已更新 {d['path']}  [{mark}]")
            print(f"  改动字段：{'、'.join(d['changed'])}")
        # note / warning 走 stderr：stdout 是结果，这两条是旁路提示。
        for key in ("note", "warning"):
            if d.get(key):
                print(d[key], file=sys.stderr)

    _emit(payload, args.json, render)
    return 0


def cmd_delete(args) -> int:
    """删除一张卡（P3-02）。

    三种模式，**默认是软删**：

    - ``delete <id>``：移到 ``<vault>/.trash/<原相对路径>``，可恢复。**保留读取统计**；
    - ``delete --restore <rel_path>``：把回收站里的卡移回原位；
    - ``delete --purge [<rel_path> | --older-than 30d | --all]``：**彻底删除**，
      只对回收站里的内容生效 —— 这条约束是「purge 不可能删掉一张活着的卡」的机制保证。
    """
    vault = config.vault_path(args.vault)

    if args.purge is not None:
        return _delete_purge(args, vault)
    if args.restore is not None:
        return _delete_restore(args, vault)
    return _delete_soft(args, vault)


def _delete_soft(args, vault) -> int:
    db = config.db_path(args.db)
    if not _require_db(db, args.json):
        return 2

    if args.id is None:
        payload = {"ok": False,
                   "error": "软删需要给出卡片 id。恢复用 --restore <rel_path>，"
                            "彻底删除用 --purge。"}
        _emit(payload, args.json, lambda d: print(d["error"], file=sys.stderr))
        return 1

    conn = store.connect(db)
    row = store.get_card(conn, args.id)
    conn.close()
    if row is None:
        payload = {"ok": False, "error": f"找不到 id={args.id}"}
        _emit(payload, args.json, lambda d: print(d["error"], file=sys.stderr))
        return 1

    rel = row["rel_path"]
    result = capture.delete_card(vault, rel)
    if not result["ok"]:
        hint = ""
        if "不存在" in result["reason"]:
            # 文件不在磁盘上但索引里还有 —— 索引脏了，指到能修它的命令上。
            hint = "（文件已不在磁盘上，但索引里还有它 —— 可运行 python memory.py index 清理索引）"
        payload = {**result, "hint": hint}
        _emit(payload, args.json,
              lambda d: print(f"未删除：{d['reason']}{d.get('hint', '')}", file=sys.stderr))
        return 1

    # 索引：**精确摘掉这一行，但保留 card_stats**（软删可恢复，统计要留到 purge）。
    # 不走 importer.sync 全量 —— 那是 O(语料)，而这里已经知道要删的是哪一行。
    indexed, warning = False, ""
    try:
        conn = store.connect(db)
        store.init(conn)
        store.delete_cards(conn, [rel], keep_stats=True)
        store.commit(conn)
        conn.close()
        indexed = True
    except Exception as exc:
        warning = (f"文件已移入回收站（{result['trash_path']}），但索引未更新：{exc}。"
                   f"可运行 python memory.py index 补建。")

    summary = capture.trash_summary(vault)
    payload = {"id": args.id, **result, "indexed": indexed,
               "trash_count": summary["count"],
               "trash_bytes": summary["bytes"]}
    if warning:
        payload["warning"] = warning
    # A3 的决定：**不自动清理**，攒到一定量提醒用户清理。
    if summary["count"] >= capture.TRASH_REMIND_THRESHOLD:
        payload["reminder"] = (
            f"回收站已积累 {summary['count']} 张卡（{summary['bytes'] / 1024:.1f} KB）。"
            f"确认不再需要后可清理：python memory.py delete --purge --older-than 30d"
        )

    def render(d):
        mark = "已索引" if d["indexed"] else "未索引"
        print(f"已软删 {d['rel_path']}  ->  {d['trash_path']}  [{mark}]")
        print(f"  恢复：python memory.py delete --restore {d['rel_path']}")
        print(f"  回收站现有 {d['trash_count']} 张")
        for key in ("warning", "reminder"):
            if d.get(key):
                print(d[key], file=sys.stderr)

    _emit(payload, args.json, render)
    return 0


def _delete_restore(args, vault) -> int:
    result = capture.restore_card(vault, args.restore, force=args.force)
    if not result["ok"]:
        _emit(result, args.json,
              lambda d: print(f"未恢复：{d['reason']}", file=sys.stderr))
        return 1

    # 只索引这一张卡（不跑全量）。索引不存在时不因此让恢复失败 ——
    # 移回文件才是这次操作的主体，索引随时可以补建。
    db = config.db_path(args.db)
    indexed, warning = False, ""
    if db.exists():
        try:
            conn = store.connect(db)
            store.init(conn)
            importer.sync_one(conn, vault, result["path"])
            store.commit(conn)
            conn.close()
            indexed = True
        except Exception as exc:
            warning = (f"文件已恢复（{result['rel_path']}），但索引未更新：{exc}。"
                       f"可运行 python memory.py index 补建。")
    else:
        warning = f"索引不存在（{db}），文件已恢复但尚未可检索：先运行 python memory.py index"

    payload = {**result, "indexed": indexed}
    if warning:
        payload["warning"] = warning

    def render(d):
        mark = "已索引" if d["indexed"] else "未索引"
        print(f"已恢复 {d['rel_path']}  [{mark}]")
        for key in ("note", "warning"):
            if d.get(key):
                print(d[key], file=sys.stderr)

    _emit(payload, args.json, render)
    return 0


def _delete_purge(args, vault) -> int:
    if args.older_than and args.all:
        payload = {"ok": False, "error": "--older-than 与 --all 互斥："
                                    "前者按删除时间筛选，后者是清空回收站。"}
        _emit(payload, args.json, lambda d: print(d["error"], file=sys.stderr))
        return 1

    if not args.purge:
        # 批量：必须显式给条件。没有条件等于「清空回收站」，
        # 那种操作不该由一次手滑触发。
        older_seconds = None
        if args.older_than:
            older_seconds = util.parse_duration(args.older_than)
            if older_seconds is None:
                payload = {"ok": False,
                           "error": f"无法识别的时长：{args.older_than!r}。"
                                    f"可用 30d / 12h / 45m / 1d12h。"}
                _emit(payload, args.json, lambda d: print(d["error"], file=sys.stderr))
                return 1
        if not args.all and older_seconds is None:
            payload = {"ok": False,
                       "error": "批量彻底删除必须给条件：--older-than <时长> 或 --all。"
                                "（只删一张请给路径：delete --purge <rel_path>）"}
            _emit(payload, args.json, lambda d: print(d["error"], file=sys.stderr))
            return 1
        result = capture.purge_trash(vault, older_seconds=older_seconds,
                                     everything=args.all)
        rels = [e["rel_path"] for e in result.get("purged", [])]
    else:
        result = capture.purge_card(vault, args.purge)
        rels = [result["rel_path"]] if result["ok"] else []

    if not result["ok"]:
        _emit(result, args.json,
              lambda d: print(f"未删除：{d['reason']}", file=sys.stderr))
        return 1

    # 真删才是统计的终点：清索引行 + 清 card_stats。
    # 软删时 cards 行已经摘掉、统计刻意留着，所以这里必须显式再清一次统计 ——
    # 只调 delete_cards 会因为它「找不到这一行就跳过」而把统计留成幽灵。
    stats_dropped, warning = 0, ""
    db = config.db_path(args.db)
    if db.exists():
        try:
            conn = store.connect(db)
            store.init(conn)
            store.delete_cards(conn, rels, keep_stats=False)
            stats_dropped = store.drop_stats(conn, rels)
            store.commit(conn)
            conn.close()
        except Exception as exc:
            warning = (f"文件已彻底删除，但索引/统计未更新：{exc}。"
                       f"可运行 python memory.py index 补建。")
    else:
        warning = f"索引不存在（{db}），跳过索引与统计清理。"

    payload = {**result, "stats_dropped": stats_dropped,
               "trash_count": capture.trash_summary(vault)["count"]}
    if warning:
        payload["warning"] = warning

    def render(d):
        if d.get("purged") is not None:
            print(f"已彻底删除 {d.get('count', 0)} 张回收站卡片"
                  f"（不可恢复）；回收站剩余 {d['trash_count']} 张")
            skipped = d.get("skipped") or []
            if skipped:
                # 跳过必须逐条说出来：这些是「我们没能判断该不该删」的卡，
                # 静默留着它们、只报成功，就是让人以为回收站已经清干净了。
                print(f"跳过 {len(skipped)} 张：", file=sys.stderr)
                for item in skipped:
                    print(f"  {item['rel_path']}：{item.get('skip_reason', '')}",
                          file=sys.stderr)
        else:
            print(f"已彻底删除 {d['rel_path']}（不可恢复）；回收站剩余 {d['trash_count']} 张")
        if d.get("stats_dropped"):
            print(f"  同时清掉了 {d['stats_dropped']} 条读取统计")
        if d.get("warning"):
            print(d["warning"], file=sys.stderr)

    _emit(payload, args.json, render)
    return 0


def cmd_supersede(args) -> int:
    """写一张新卡，并让指定的旧卡失效（P3-02 的取代语义）。

    **成对操作**：新卡出现与旧卡失效必须同时发生。所以它做成一个命令，
    而不是「capture 时传 --supersedes」—— 两步的话，中间失败会留下
    「两张卡都有效」的状态，而检索看不出异常。
    """
    db = config.db_path(args.db)
    if not _require_db(db, args.json):
        return 2

    conn = store.connect(db)
    old = store.get_card(conn, args.old_id)
    conn.close()
    if old is None:
        payload = {"ok": False, "error": f"找不到 id={args.old_id}"}
        _emit(payload, args.json, lambda d: print(d["error"], file=sys.stderr))
        return 1

    vault = config.vault_path(args.vault)
    # --kind 不给时**沿用旧卡的类型**：取代默认是「同一件事变了」，
    # 类型跟着变会让卡片悄悄换目录（而目录是路径的一部分）。想换类型要显式说。
    kind = args.kind or (old["kind"] or capture.DEFAULT_KIND)
    result = capture.supersede_card(
        vault,
        old["rel_path"],
        title=args.title,
        body=args.body,
        kind=kind,
        tags=parse_tags(args.tags),
        source=args.source,
    )

    if not result["ok"]:
        _emit(result, args.json,
              lambda d: print(f"未取代：{d['reason']}", file=sys.stderr))
        return 1

    # **两张卡都要重新索引**：新卡是新面孔，旧卡的 frontmatter 多了 invalid_at ——
    # 只索引新卡的话，旧卡在索引里仍是「有效」，默认检索照样返回它，
    # 而命令返回的是「成功」。
    indexed, warning = False, ""
    try:
        conn = store.connect(db)
        store.init(conn)
        importer.sync_one(conn, vault, result["new_path"])
        importer.sync_one(conn, vault, result["old_path"])
        store.commit(conn)
        conn.close()
        indexed = True
    except Exception as exc:
        warning = (f"两张卡都已落盘，但索引未更新：{exc}。"
                   f"可运行 python memory.py index 补建。")

    payload = {"old_id": args.old_id, **result, "indexed": indexed}
    if warning:
        payload["warning"] = warning

    def render(d):
        mark = "已索引" if d["indexed"] else "未索引"
        print(f"已取代 {d['old_rel']}  ->  {d['new_rel']}  [{mark}]")
        print(f"  旧卡文件仍在磁盘上，只是标了失效（{d['invalid_at']}）")
        print("  回溯当时的事实的用法：memory.py search <词> --as-of <该日期>")
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

    ps = sub.add_parser("search", help="关键词检索（传实体名/标识符，不要传整句问句）")
    ps.add_argument("query", help="检索词：实体名、标识符、命令、路径片段。"
                                  "整句问句会把匹配档位从 AND 精确降到 OR 放宽并引入噪音")
    ps.add_argument("-n", "--limit", type=int, default=10)
    ps.add_argument("--kind", help="按类型过滤")
    ps.add_argument("--source", help="按来源过滤")
    ps.add_argument("--as-of", dest="as_of", metavar="YYYY-MM-DD",
                    help="回溯：只看**那一天当时有效**的卡（默认只看当前有效的）。"
                         "旧卡被取代后不进默认检索，要查当时的事实用这个")
    ps.add_argument("--include-invalid", dest="include_invalid", action="store_true",
                    help="连已失效（被取代）的卡一起返回，并在结果里标注失效时间")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_search)

    pt = sub.add_parser("stats", help="统计概览")
    pt.add_argument("--vault", help="vault 目录（用于统计回收站占用）")
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

    pu = sub.add_parser(
        "update",
        help="原地修改一张卡（事实本身写错了用这个；事实变了用 supersede）")
    pu.add_argument("id", type=int, help="卡片 id，来自 search 结果")
    pu.add_argument("--title", help="新标题；文件**不会**改名（路径是取代与统计的锚点）")
    pu.add_argument("--body", help="新正文。**不给就不动正文** —— 刻意不从 stdin 读，"
                                   "否则「没打算改正文」会变成「把管道内容写成正文」")
    pu.add_argument("--kind", help="**只用来显式报错**：不支持改类型，"
                                   "因为 kind 决定卡片目录、改它等于移动文件")
    pu.add_argument("--tags", help="标签，逗号分隔；传空字符串表示清空标签")
    pu.add_argument("--priority", type=int, help="优先级（整数）")
    pu.add_argument("--ttl", help="有效期，如 30d / 12h / 2026-10-01；空字符串表示永不过期")
    pu.add_argument("--source", help="来源标记")
    pu.add_argument("--vault", help="vault 目录")
    pu.add_argument("--json", action="store_true")
    pu.set_defaults(func=cmd_update)

    pd = sub.add_parser(
        "delete",
        help="删除一张卡（默认**软删**到 .trash，可 --restore 恢复；--purge 才真删）")
    pd.add_argument("id", nargs="?", type=int, help="卡片 id（软删时必填）")
    pd.add_argument("--restore", metavar="REL_PATH",
                    help="把回收站里的卡移回原位，如 03-Knowledge/某卡.md")
    pd.add_argument("--purge", nargs="?", const="", default=None, metavar="REL_PATH",
                    help="**彻底删除**（不可恢复），只对回收站里的生效。"
                         "给路径只删那一张；不给则配合 --older-than / --all 批量")
    pd.add_argument("--older-than", metavar="时长",
                    help="与 --purge 连用：只删「删除时间」早于该时长的，如 30d / 12h")
    pd.add_argument("--all", action="store_true",
                    help="与 --purge 连用：清空回收站（含没有删除时间记录的条目）")
    pd.add_argument("--force", action="store_true",
                    help="--restore 时目标位置已存在：把占位的那张也移进回收站再恢复")
    pd.add_argument("--vault", help="vault 目录")
    pd.add_argument("--json", action="store_true")
    pd.set_defaults(func=cmd_delete)

    psup = sub.add_parser(
        "supersede",
        help="写一张新卡并让旧卡失效（事实**变了**用这个；事实写错了用 update）")
    psup.add_argument("old_id", type=int, help="被取代的旧卡 id（来自 search 结果）")
    psup.add_argument("--title", required=True, help="新卡标题")
    psup.add_argument("--body", required=True,
                      help="新卡正文。**必填且不从 stdin 读** —— 取代会写出一张新卡，"
                           "它的内容必须由调用方明确给出；含糊的默认值等于让"
                           "「新事实是什么」变成一个意外")
    psup.add_argument("--kind", help="新卡类型；不给则沿用旧卡的类型")
    psup.add_argument("--tags", help="新卡标签，逗号分隔")
    psup.add_argument("--source", default="agent", help="新卡来源标记，默认 agent")
    psup.add_argument("--vault", help="vault 目录")
    psup.add_argument("--json", action="store_true")
    psup.set_defaults(func=cmd_supersede)

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
