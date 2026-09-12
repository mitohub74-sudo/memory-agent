# -*- coding: utf-8 -*-
"""SQLite 写锁处理（P3-05）。

验收标准是「两进程并发 capture/reindex 不抛 ``database is locked``」。
这类故障的特点是**间歇性**：手动重跑往往就好了，于是最容易被当成偶发噪音放过。
所以这里分两层验证：

1. **单元层**：重试逻辑只对锁错误生效 —— 别把缺表、语法错也一起重试，
   那既浪费时间又掩盖真错误；
2. **进程层**：真的并发跑多个 CLI 进程，断言全部成功且索引最终一致。

进程层是真正的验收证据。单元层快，但证明不了「产品在并发下能用」。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from mcore import importer, store

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = ROOT / "memory.py"


# ------------------------------------------------------------------ 单元层


def test_connect_sets_busy_timeout() -> None:
    """默认的「立刻失败」必须被换掉，否则并发写会随机炸。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-lock-") as raw:
        conn = store.connect(Path(raw) / "t.db")
        try:
            got = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert int(got) == store.BUSY_TIMEOUT_MS == 5000
        finally:
            conn.close()


def test_locked_error_detection_is_narrow() -> None:
    assert store.is_locked_error(sqlite3.OperationalError("database is locked"))
    assert store.is_locked_error(sqlite3.OperationalError("database table is locked"))
    assert store.is_locked_error(sqlite3.OperationalError("database is busy"))

    # 这些**不能**算锁错误 —— 重试它们只会浪费时间，还会掩盖真问题
    assert not store.is_locked_error(sqlite3.OperationalError("no such table: cards"))
    assert not store.is_locked_error(sqlite3.OperationalError("near \"SELET\": syntax error"))
    assert not store.is_locked_error(ValueError("完全是别的问题"))


def test_retry_succeeds_after_transient_locks() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert store.retry_on_locked(flaky) == "ok"
    assert calls["n"] == 3, "应该重试到成功为止"


def test_retry_does_not_retry_non_lock_errors() -> None:
    calls = {"n": 0}

    def broken() -> None:
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: cards")

    try:
        store.retry_on_locked(broken)
    except sqlite3.OperationalError as exc:
        assert "no such table" in str(exc)
    else:
        raise AssertionError("非锁错误必须原样抛出")
    assert calls["n"] == 1, "非锁错误只应尝试一次"


def test_retry_gives_up_and_reraises() -> None:
    calls = {"n": 0}

    def always_locked() -> None:
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    started = time.monotonic()
    try:
        store.retry_on_locked(always_locked)
    except sqlite3.OperationalError as exc:
        assert "locked" in str(exc)
    else:
        raise AssertionError("一直锁住时最终必须抛出，不能无限重试")
    assert calls["n"] == store._LOCK_RETRIES + 1, calls
    assert time.monotonic() - started >= 0.05, "重试之间应有退避，不能忙等"


def test_retry_passes_arguments_through() -> None:
    def add(a: int, b: int = 0) -> int:
        return a + b

    assert store.retry_on_locked(add, 2, b=3) == 5


# ------------------------------------------------------------------ 进程层


def _capture_once(env: dict, title: str, body: str) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(
        [sys.executable, str(MEMORY_PY), "capture",
         "--title", title, "--body", body, "--kind", "knowledge", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=full_env,
        cwd=str(ROOT),
    )


def test_concurrent_processes_can_capture_without_locked_errors() -> None:
    """多个进程同时 capture：全部成功，且索引最终与磁盘一致。

    这就是 P3-05 的验收条件本身。多进程是必须的 —— 单连接自己不会和自己抢锁，
    进程内线程共享一个 connection 也测不到跨进程的 SQLITE_BUSY。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-lock-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = {
            "MEMORY_AGENT_VAULT": str(vault),
            "MEMORY_AGENT_DB": str(root / "memory.db"),
        }
        workers = 6
        results: list[subprocess.CompletedProcess] = [None] * workers  # type: ignore[list-item]

        def run(i: int) -> None:
            results[i] = _capture_once(
                env,
                f"并发写入卡 {i}",
                f"这是第 {i} 个并发进程写入的正文，长度足够建卡，用于验证写锁处理。",
            )

        threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        failures = []
        for i, r in enumerate(results):
            if r is None:
                failures.append((i, "进程未完成"))
                continue
            if "locked" in (r.stderr or "").lower() or "locked" in (r.stdout or "").lower():
                failures.append((i, f"出现 locked：{r.stderr}{r.stdout}"))
            if r.returncode != 0:
                failures.append((i, f"退出码 {r.returncode}: {r.stderr}{r.stdout}"))
        assert not failures, f"并发写入失败：{failures}"

        # 索引必须与磁盘一致 —— 「没报错」不等于「都写进去了」
        on_disk = len(list(vault.rglob("*.md")))
        assert on_disk == workers, f"磁盘上应有 {workers} 张卡，实际 {on_disk}"

        conn = store.connect(root / "memory.db")
        try:
            indexed = store.count_cards(conn)
            assert indexed == workers, f"索引里应有 {workers} 张卡，实际 {indexed}"
        finally:
            conn.close()

        # 再跑一次增量同步：必须完全收敛（unchanged == workers）
        conn = store.connect(root / "memory.db")
        try:
            counts = importer.sync(conn, vault)
            assert counts["inserted"] == 0 and counts["updated"] == 0, counts
            assert counts["unchanged"] == workers, counts
        finally:
            conn.close()
