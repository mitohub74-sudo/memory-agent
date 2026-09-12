# -*- coding: utf-8 -*-
"""store 的 schema 版本门控、迁移与访问统计测试。

只用标准库 tempfile，既能被 pytest 收集，也不依赖 pytest fixture；
因此 `python tests/test_mcp.py` 的零依赖直跑路径不受影响。

这里的迁移测试刻意**手工建一个 v1 库**，而不是「把 SCHEMA_VERSION 临时改小」——
后者测的是常量，前者测的才是真实场景：一个用户手上早就存在的旧库。
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

from mcore import importer, store

# v1 时代的真实 DDL。它**必须**在测试里固化成字面量：
# 若改成 import 当前 DDL，等下一轮迁移再加列时，这个「v1 库」会跟着变成新版，
# 迁移测试就永远测不到迁移了。
_V1_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id           INTEGER PRIMARY KEY,
    rel_path     TEXT    NOT NULL UNIQUE,
    title        TEXT    NOT NULL DEFAULT '',
    kind         TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT '',
    source       TEXT    NOT NULL DEFAULT '',
    tags         TEXT    NOT NULL DEFAULT '[]',
    created      TEXT    NOT NULL DEFAULT '',
    updated      TEXT    NOT NULL DEFAULT '',
    body         TEXT    NOT NULL DEFAULT '',
    content_hash TEXT    NOT NULL DEFAULT '',
    embedding    BLOB
);

CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
    title,
    body,
    tokenize='unicode61'
);
"""


def _make_v1_database(path: Path) -> None:
    """造一个带一张真实卡片的 v1 库（模拟用户手上的旧库）。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_V1_DDL)
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
        conn.execute(
            """INSERT INTO cards
                   (rel_path, title, kind, status, source, tags,
                    created, updated, body, content_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("03-Knowledge/旧卡.md", "旧卡标题", "knowledge", "approved", "agent",
             '["旧"]', "2026-01-01T00:00:00", "2026-01-02T00:00:00",
             "这是迁移前就存在的正文。", "abcdef1234567890"),
        )
        conn.commit()
    finally:
        conn.close()


def test_new_database_initializes_to_current_version() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-store-") as raw:
        conn = store.connect(Path(raw) / "new.db")
        try:
            store.init(conn)
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            assert row is not None
            assert int(row["value"]) == store.SCHEMA_VERSION

            store.init(conn)
            again = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            assert again is not None
            assert int(again["value"]) == store.SCHEMA_VERSION
        finally:
            conn.close()


def test_corrupted_schema_version_is_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-store-") as raw:
        conn = store.connect(Path(raw) / "corrupt.db")
        try:
            store.init(conn)
            conn.execute(
                "UPDATE meta SET value = 'not-a-version' WHERE key = 'schema_version'"
            )
            conn.commit()

            try:
                store.init(conn)
            except RuntimeError as exc:
                assert "schema_version 损坏" in str(exc)
            else:
                raise AssertionError("损坏的 schema_version 必须被拒绝")
        finally:
            conn.close()


def test_database_newer_than_code_is_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-store-") as raw:
        conn = store.connect(Path(raw) / "future.db")
        try:
            store.init(conn)
            conn.execute(
                "UPDATE meta SET value = '999' WHERE key = 'schema_version'"
            )
            conn.commit()

            try:
                store.init(conn)
            except RuntimeError as exc:
                assert "高于本代码支持的版本" in str(exc)
            else:
                raise AssertionError("高版本 schema 必须被拒绝")
        finally:
            conn.close()


def test_v1_database_migrates_to_v2_and_keeps_data() -> None:
    """v1 → v2：新表出现、新列出现、**原卡片一行不少、字段不变**。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-store-") as raw:
        db = Path(raw) / "v1.db"
        _make_v1_database(db)

        # 迁移前先确认它真的是 v1，且没有 card_stats —— 否则这个测试可能是空转
        probe = sqlite3.connect(str(db))
        try:
            version = probe.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            assert version == "1"
            with_tables = {
                r[0] for r in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "card_stats" not in with_tables
        finally:
            probe.close()

        conn = store.connect(db)
        try:
            store.init(conn)

            assert int(conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()["value"]) == 2

            tables = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "card_stats" in tables

            # 原数据必须原样还在
            row = conn.execute(
                "SELECT * FROM cards WHERE rel_path = ?", ("03-Knowledge/旧卡.md",)
            ).fetchone()
            assert row is not None, "迁移不能丢卡片"
            assert row["title"] == "旧卡标题"
            assert row["body"] == "这是迁移前就存在的正文。"
            assert row["content_hash"] == "abcdef1234567890"
            assert store.count_cards(conn) == 1

            # 新列的默认值必须可用，不能是 NULL
            assert row["priority"] == 0
            assert row["ttl"] == ""
            assert row["expires_at"] == 0

            # 迁移必须幂等：再跑一次不能报错、不能重复加列
            store.init(conn)
            assert store.count_cards(conn) == 1
        finally:
            conn.close()


def test_read_path_survives_unmigrated_v1_database() -> None:
    """读路径不能在还没迁移的旧库上炸掉。

    用户升级代码后、跑 ``index`` 之前，手里就是一个 v1 库：没有 card_stats 表，
    cards 也没有 priority/ttl 列。此时 ``show`` / ``memory_get`` 会顺手记一次访问 ——
    为此让「看一眼卡片全文」失败，代价完全不成比例。记不了就不记，
    统计随后由写入路径的迁移补上。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-store-") as raw:
        db = Path(raw) / "v1-unmigrated.db"
        _make_v1_database(db)

        conn = store.connect(db)
        try:
            # 故意**不**调 store.init —— 这就是「还没迁移」的状态
            assert store.record_access(conn, "03-Knowledge/旧卡.md") == 0
            assert store.get_access_stat(conn, "03-Knowledge/旧卡.md") is None
            assert store.access_stats(conn) == []

            # 读卡片本身必须照常工作
            row = conn.execute(
                "SELECT id FROM cards WHERE rel_path = ?", ("03-Knowledge/旧卡.md",)
            ).fetchone()
            assert row is not None
            assert store.get_card(conn, int(row["id"]))["title"] == "旧卡标题"
        finally:
            conn.close()

        # 迁移之后，同样的调用就该真的记账了
        conn = store.connect(db)
        try:
            store.init(conn)
            assert store.record_access(conn, "03-Knowledge/旧卡.md", now=1_700_000_000) == 1
            assert store.get_access_stat(conn, "03-Knowledge/旧卡.md")["access_count"] == 1
        finally:
            conn.close()


def test_rebuild_keeps_card_stats_but_resets_cards() -> None:
    """重建索引：可重建的 cards 清空重建，不可重建的 card_stats 必须留下。

    ``card_stats`` 没有第二个来源 —— 它一旦跟着 rebuild 走，统计就是永久丢失。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-store-") as raw:
        root = Path(raw)
        vault = root / "vault"
        vault.mkdir()

        card_dir = vault / "03-Knowledge"
        card_dir.mkdir()
        (card_dir / "重建测试卡.md").write_text(
            "---\ntitle: 重建测试卡\nkind: knowledge\n---\n\n重建测试用的正文内容。\n",
            encoding="utf-8",
        )

        conn = store.connect(root / "memory.db")
        try:
            store.init(conn)
            counts = importer.sync(conn, vault)
            assert counts["inserted"] == 1

            rel = "03-Knowledge/重建测试卡.md"
            assert store.record_access(conn, rel, now=1_700_000_000) == 1
            stat_before = store.get_access_stat(conn, rel)
            assert stat_before == {
                "rel_path": rel, "access_count": 1, "last_accessed": 1_700_000_000,
            }

            # 全量重建
            counts = importer.sync(conn, vault, rebuild=True)
            assert counts["inserted"] == 1, "重建后卡片应被重新导入"
            assert store.count_cards(conn) == 1

            stat_after = store.get_access_stat(conn, rel)
            assert stat_after == stat_before, "index --rebuild 不能清掉访问统计"

            # FTS 也必须跟着重建，否则「重建后搜不到」是静默故障
            fts_n = conn.execute("SELECT COUNT(*) FROM cards_fts").fetchone()[0]
            assert fts_n == 1
        finally:
            conn.close()


def test_record_access_counts_and_ignores_missing_cards() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-store-") as raw:
        root = Path(raw)
        vault = root / "vault"
        (vault / "03-Knowledge").mkdir(parents=True)
        (vault / "03-Knowledge" / "访问统计卡.md").write_text(
            "---\ntitle: 访问统计卡\nkind: knowledge\n---\n\n访问统计测试正文。\n",
            encoding="utf-8",
        )

        conn = store.connect(root / "memory.db")
        try:
            store.init(conn)
            importer.sync(conn, vault)
            rel = "03-Knowledge/访问统计卡.md"

            now = int(time.time())
            assert store.record_access(conn, rel, now=now) == 1
            assert store.record_access(conn, rel, now=now + 10) == 1
            # 同一批里重复的路径只记一次，否则调用方传重复 id 就能刷高计数
            assert store.record_access(conn, [rel, rel, rel], now=now + 20) == 1

            stat = store.get_access_stat(conn, rel)
            assert stat is not None
            assert stat["access_count"] == 3
            assert stat["last_accessed"] == now + 20

            # 不存在的卡片不记账：否则统计表里会堆积永远读不出来的幽灵行
            assert store.record_access(conn, "99-No/不存在.md") == 0
            assert store.get_access_stat(conn, "99-No/不存在.md") is None

            # 空输入是 no-op，且不抛错
            assert store.record_access(conn, []) == 0
            assert store.record_access(conn, "") == 0
        finally:
            conn.close()
