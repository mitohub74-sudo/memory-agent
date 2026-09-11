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
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "connect",
    "init",
    "upsert_card",
    "delete_cards",
    "get_card",
    "iter_cards",
    "count_cards",
    "stats",
]

SCHEMA_VERSION = 1

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
    embedding    BLOB                            -- 预留：向量检索
);

CREATE INDEX IF NOT EXISTS idx_cards_kind    ON cards(kind);
CREATE INDEX IF NOT EXISTS idx_cards_status  ON cards(status);
CREATE INDEX IF NOT EXISTS idx_cards_source  ON cards(source);
CREATE INDEX IF NOT EXISTS idx_cards_updated ON cards(updated);

CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
    title,
    body,
    tokenize='unicode61'
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


def init(conn: sqlite3.Connection) -> None:
    """建表并写入 schema 版本。幂等。"""
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
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
    ``unchanged`` 表示 content_hash 未变，跳过写入 —— 这是增量索引的关键。
    """
    rel = card["rel_path"]
    row = conn.execute(
        "SELECT id, content_hash FROM cards WHERE rel_path = ?", (rel,)
    ).fetchone()

    if row is not None and row["content_hash"] == card.get("content_hash", ""):
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
        card.get("content_hash", ""),
    )

    if row is not None:
        card_id = int(row["id"])
        conn.execute(
            """UPDATE cards
                  SET title=?, kind=?, status=?, source=?, tags=?,
                      created=?, updated=?, body=?, content_hash=?
                WHERE id=?""",
            fields + (card_id,),
        )
        _fts_sync(conn, card_id, card.get("title", ""), card.get("body", ""))
        return "updated"

    cur = conn.execute(
        """INSERT INTO cards
               (rel_path, title, kind, status, source, tags,
                created, updated, body, content_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (rel,) + fields,
    )
    card_id = int(cur.lastrowid)
    _fts_sync(conn, card_id, card.get("title", ""), card.get("body", ""))
    return "inserted"


def delete_cards(conn: sqlite3.Connection, rel_paths: list[str]) -> int:
    """按路径批量删除卡片及其索引行。返回删除数量。"""
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
