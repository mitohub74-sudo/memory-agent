# -*- coding: utf-8 -*-
"""store 的 schema 版本门控测试。

只用标准库 tempfile，既能被 pytest 收集，也不依赖 pytest fixture；
因此 `python tests/test_mcp.py` 的零依赖直跑路径不受影响。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from mcore import store


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
