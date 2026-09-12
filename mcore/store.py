# -*- coding: utf-8 -*-
"""SQLite 存储层：schema 定义、连接管理、卡片读写。

设计原则
--------
1. **Markdown 是真相源**，本库只是【可重建的索引】。任何时候都可以
   删掉 .db 文件，用 ``memory.py index --rebuild`` 重建。
2. **schema 带版本号**，为将来迁移留路。
3. **cards.embedding 列已预留**（BLOB）。将来加向量检索时无需改表结构，
   只需新增一个 Searcher 实现并回填该列。

表结构
------
meta        键值对，存 schema_version 等元信息
cards       卡片主表，rel_path 作为业务主键
cards_fts   FTS5 全文索引，存 bigram 分词后的文本
card_stats  访问统计（index-side 辅助数据，重建索引**不清**）
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "MIGRATIONS",
    "connect",
    "init",
    "upsert_card",
    "delete_cards",
    "get_card",
    "iter_cards",
    "count_cards",
    "stats",
    "record_access",
    "access_stats",
    "get_access_stat",
]

SCHEMA_VERSION = 2

# 迁移表：MIGRATIONS[v] 是把 schema 从 v 升到 v+1 的 SQL 脚本。
# 每一步都必须**只做增量**（ADD COLUMN / CREATE TABLE IF NOT EXISTS），
# 因为它是跑在已有数据的库上的，而那个库不是可丢弃的临时产物。
MIGRATIONS: dict[int, str] = {
    # v1 → v2：卡片生命周期所需的列，以及访问统计表。
    1: """
ALTER TABLE cards ADD COLUMN priority   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cards ADD COLUMN ttl        TEXT    NOT NULL DEFAULT '';
ALTER TABLE cards ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS card_stats (
    rel_path      TEXT    PRIMARY KEY,
    access_count  INTEGER NOT NULL DEFAULT 0,
    last_accessed INTEGER NOT NULL DEFAULT 0
);
""",
}

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id           INTEGER PRIMARY KEY,
    rel_path     TEXT    NOT NULL UNIQUE,        -- 相对 vault 的路径，业务主键
    title        TEXT    NOT NULL DEFAULT '',
    kind         TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT '',
    source       TEXT    NOT NULL DEFAULT '',
    tags         TEXT    NOT NULL DEFAULT '[]',  -- JSON 数组
    created      TEXT    NOT NULL DEFAULT '',
    updated      TEXT    NOT NULL DEFAULT '',
    body         TEXT    NOT NULL DEFAULT '',    -- 去掉 frontmatter 后的正文
    content_hash TEXT    NOT NULL DEFAULT '',    -- 增量索引用
    priority     INTEGER NOT NULL DEFAULT 0,     -- 优先级（frontmatter: priority）
    ttl          TEXT    NOT NULL DEFAULT '',    -- 原始 TTL 写法（frontmatter: ttl）
    expires_at   INTEGER NOT NULL DEFAULT 0,     -- 解析后的过期时刻（unix 秒），0 = 永不过期
    embedding    BLOB                            -- 预留：向量检索
);

CREATE INDEX IF NOT EXISTS idx_cards_kind     ON cards(kind);
CREATE INDEX IF NOT EXISTS idx_cards_status   ON cards(status);
CREATE INDEX IF NOT EXISTS idx_cards_source   ON cards(source);
CREATE INDEX IF NOT EXISTS idx_cards_updated  ON cards(updated);
CREATE INDEX IF NOT EXISTS idx_cards_priority ON cards(priority);

CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
    title,
    body,
    tokenize='unicode61'
);

-- 访问统计刻意**不放进 cards**：cards 是可重建投影，index --rebuild 会清空它，
-- 统计一旦跟着走就等于「重建即丢」。用 rel_path 作主键而不是 id ——
-- id 是 rowid，重建后必然重排（这正是 bench 真值不能用 id 记录的同一个坑）。
CREATE TABLE IF NOT EXISTS card_stats (
    rel_path      TEXT    PRIMARY KEY,
    access_count  INTEGER NOT NULL DEFAULT 0,
    last_accessed INTEGER NOT NULL DEFAULT 0
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）数据库连接。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _read_schema_version(conn: sqlite3.Connection) -> int | None:
    """读 meta.schema_version。库还没建表（全新库）时返回 None。"""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # meta 表还不存在 → 全新库
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"索引库的 schema_version 损坏：{row['value']!r}。"
            f"请从 Markdown 真相源运行 index --rebuild 重建索引。"
        ) from exc


def _write_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(version),),
    )


def init(conn: sqlite3.Connection) -> None:
    """建表并把 schema 升到当前版本。幂等。

    三种情况分开处理：

    - **全新库**（没有 meta 表，或没写过版本）→ 建表，写入当前版本
    - **库版本 < 代码版本** → 逐级跑 MIGRATIONS，跑完再补一遍 DDL
    - **库版本 > 代码版本** → **报错**。库比代码新，说明用旧代码打开了新库；
      继续跑就是拿旧结构去读新数据，而损坏是静默的。
    """
    current = _read_schema_version(conn)

    if current is None:
        conn.executescript(_DDL)
        _write_schema_version(conn, SCHEMA_VERSION)
        conn.commit()
        return

    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"索引库的 schema 版本（{current}）高于本代码支持的版本（{SCHEMA_VERSION}）。"
            f"请升级 memory-agent，或改用匹配版本的代码。"
            f"继续运行会用旧结构读新库，可能静默损坏数据。"
        )

    if current < SCHEMA_VERSION:
        for v in range(current, SCHEMA_VERSION):
            script = MIGRATIONS.get(v)
            if script is None:
                raise RuntimeError(f"缺少 v{v} → v{v + 1} 的迁移脚本")
            try:
                conn.executescript(script)
            except sqlite3.OperationalError as exc:
                raise RuntimeError(
                    f"执行 v{v} → v{v + 1} 迁移失败：{exc}。"
                    f"索引库可能不是 v{v} 结构（被人手动改过，或来自更早的实验版本）。"
                    f"它只是可重建的投影 —— 删掉它，再从 Markdown 真相源 index --rebuild 即可。"
                ) from exc
        _write_schema_version(conn, SCHEMA_VERSION)
        # 迁移只负责「补上这一步的差异」。跑完再走一遍 DDL，
        # 保证老库里缺的、与本次迁移无关的表也不会漏掉（DDL 全是 IF NOT EXISTS）。
        conn.executescript(_DDL)
        conn.commit()
        return

    # 版本一致：仍跑一遍 DDL（全部 IF NOT EXISTS），保证表齐全
    conn.executescript(_DDL)
    conn.commit()


def _fts_sync(conn: sqlite3.Connection, card_id: int, title: str, body: str) -> None:
    """重建某张卡的 FTS 索引行。"""
    from .tokenize import to_index_text

    conn.execute("DELETE FROM cards_fts WHERE rowid = ?", (card_id,))
    conn.execute(
        "INSERT INTO cards_fts(rowid, title, body) VALUES (?, ?, ?)",
        (card_id, to_index_text(title), to_index_text(body)),
    )


def upsert_card(conn: sqlite3.Connection, card: dict) -> str:
    """写入或更新一张卡，并同步 FTS 索引。

    返回 ``"inserted"`` / ``"updated"`` / ``"unchanged"``。
    ``unchanged`` 表示 file_hash 未变，跳过写入 —— 这是增量索引的关键。

    注：入库的 ``card`` 字典用 ``file_hash`` 作为键（来源是 importer），
    而 DB 列名仍是历史命名 ``content_hash``，两者刻意不统一 ——
    改列名要迁移，收益为零。
    """
    rel = card["rel_path"]
    row = conn.execute(
        "SELECT id, content_hash FROM cards WHERE rel_path = ?", (rel,)
    ).fetchone()

    if row is not None and row["content_hash"] == card.get("file_hash", ""):
        return "unchanged"

    fields = (
        card.get("title", ""),
        card.get("kind", ""),
        card.get("status", ""),
        card.get("source", ""),
        json.dumps(card.get("tags", []), ensure_ascii=False),
        card.get("created", ""),
        card.get("updated", ""),
        card.get("body", ""),
        card.get("file_hash", ""),
        int(card.get("priority", 0) or 0),
        card.get("ttl", ""),
        int(card.get("expires_at", 0) or 0),
    )

    if row is not None:
        card_id = int(row["id"])
        conn.execute(
            """UPDATE cards
                  SET title=?, kind=?, status=?, source=?, tags=?,
                      created=?, updated=?, body=?, content_hash=?,
                      priority=?, ttl=?, expires_at=?
                WHERE id=?""",
            fields + (card_id,),
        )
        _fts_sync(conn, card_id, card.get("title", ""), card.get("body", ""))
        return "updated"

    cur = conn.execute(
        """INSERT INTO cards
               (rel_path, title, kind, status, source, tags,
                created, updated, body, content_hash,
                priority, ttl, expires_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rel,) + fields,
    )
    card_id = int(cur.lastrowid)
    _fts_sync(conn, card_id, card.get("title", ""), card.get("body", ""))
    return "inserted"


def delete_cards(conn: sqlite3.Connection, rel_paths: list[str]) -> int:
    """按路径批量删除卡片及其索引行。返回删除数量。

    **不删 card_stats** —— 统计记的是「这张卡被读过多少次」，
    只在卡片真的离开 vault 时才该失去意义。当前删除的唯一来源是
    「文件已不在 vault」，那时 P3-07 会连同统计一起清理（批量化时一起做）。
    """
    n = 0
    for rel in rel_paths:
        row = conn.execute(
            "SELECT id FROM cards WHERE rel_path = ?", (rel,)
        ).fetchone()
        if row is None:
            continue
        conn.execute("DELETE FROM cards_fts WHERE rowid = ?", (row["id"],))
        conn.execute("DELETE FROM cards WHERE id = ?", (row["id"],))
        n += 1
    return n


def get_card(conn: sqlite3.Connection, card_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()


def iter_cards(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM cards ORDER BY rel_path")


def count_cards(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])


def _has_card_stats(conn: sqlite3.Connection) -> bool:
    """card_stats 是否已存在。

    存在的意义：**读路径不能在旧库上炸掉**。一个 v1 的老库（用户升级代码后
    还没跑过 index）里没有这张表，而 ``show`` / ``memory_get`` 会顺手记一次访问。
    为此让「看一眼卡片全文」失败，代价完全不成比例 —— 所以记不了就不记，
    统计随后由写入路径的迁移（``importer.sync_one`` → ``init``）补上。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='card_stats'"
    ).fetchone()
    return row is not None


def record_access(conn: sqlite3.Connection, rel_paths, now: int | None = None) -> int:
    """把若干张卡记一次访问。返回实际记账的卡片数。

    只对**当前存在于索引里**的路径记账 —— 否则一本「已删除卡片」的统计账
    会永久留在库里，且对外不可见（读的时候 join cards 就消失了），
    等于静默膨胀。

    ``rel_paths`` 允许重复：同一批里重复出现只加一次计数，
    避免调用方传了重复 id 就把计数刷高。
    """
    if isinstance(rel_paths, str):
        rel_paths = [rel_paths]
    unique = {p for p in rel_paths if p}
    if not unique:
        return 0
    if not _has_card_stats(conn):
        return 0

    ts = int(time.time()) if now is None else int(now)
    n = 0
    for rel in sorted(unique):
        exists = conn.execute(
            "SELECT 1 FROM cards WHERE rel_path = ?", (rel,)
        ).fetchone()
        if exists is None:
            continue
        conn.execute(
            """INSERT INTO card_stats(rel_path, access_count, last_accessed)
                    VALUES (?, 1, ?)
               ON CONFLICT(rel_path) DO UPDATE SET
                    access_count  = access_count + 1,
                    last_accessed = excluded.last_accessed""",
            (rel, ts),
        )
        n += 1
    conn.commit()
    return n


def access_stats(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """访问最多的卡片。``limit`` 被钳制在 [1, 100]。"""
    if not _has_card_stats(conn):
        return []
    limit = max(1, min(int(limit), 100))
    rows = conn.execute(
        """SELECT s.rel_path, s.access_count, s.last_accessed, c.title
             FROM card_stats s
             LEFT JOIN cards c ON c.rel_path = s.rel_path
            ORDER BY s.access_count DESC, s.rel_path
            LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "rel_path": r["rel_path"],
            "title": r["title"] or "",
            "access_count": int(r["access_count"]),
            "last_accessed": int(r["last_accessed"]),
        }
        for r in rows
    ]


def get_access_stat(conn: sqlite3.Connection, rel_path: str) -> dict | None:
    if not _has_card_stats(conn):
        return None
    row = conn.execute(
        "SELECT rel_path, access_count, last_accessed FROM card_stats WHERE rel_path = ?",
        (rel_path,),
    ).fetchone()
    if row is None:
        return None
    return {
        "rel_path": row["rel_path"],
        "access_count": int(row["access_count"]),
        "last_accessed": int(row["last_accessed"]),
    }


def stats(conn: sqlite3.Connection) -> dict:
    """汇总统计，供 CLI 的 stats 命令使用。"""

    def group(col: str) -> list[tuple[str, int]]:
        rows = conn.execute(
            f"SELECT {col} AS k, COUNT(*) AS n FROM cards "
            f"GROUP BY {col} ORDER BY n DESC"
        ).fetchall()
        return [(r["k"] or "(空)", int(r["n"])) for r in rows]

    total = count_cards(conn)
    body_bytes = int(
        conn.execute("SELECT COALESCE(SUM(LENGTH(body)), 0) FROM cards").fetchone()[0]
    )
    return {
        "total": total,
        "chars": body_bytes,
        "by_kind": group("kind"),
        "by_source": group("source"),
        "by_status": group("status"),
        "latest": [
            (r["rel_path"], r["updated"])
            for r in conn.execute(
                "SELECT rel_path, updated FROM cards ORDER BY updated DESC LIMIT 5"
            ).fetchall()
        ],
    }
