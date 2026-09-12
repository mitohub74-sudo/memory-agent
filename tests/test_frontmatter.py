# -*- coding: utf-8 -*-
"""importer 的 frontmatter 解析测试：priority / ttl → 索引字段。

priority 与 ttl 都放在 frontmatter（真相源）里，因为索引是可重建的：
放索引里的字段重建即丢。这里验证的是「真相源 → 索引」这一步没有走样。

只用标准库 tempfile，不依赖 pytest fixture，保持零依赖直跑路径可用。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from mcore import importer, store


def _write(vault: Path, name: str, frontmatter: str, body: str = "") -> None:
    directory = vault / "03-Knowledge"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        f"---\n{frontmatter.strip()}\n---\n\n{body or '解析测试用的正文内容。'}\n",
        encoding="utf-8",
    )


def test_priority_and_ttl_are_indexed_from_frontmatter() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-frontmatter-") as raw:
        root = Path(raw)
        vault = root / "vault"
        _write(vault, "有优先级.md", "title: 有优先级\nkind: knowledge\npriority: 7\nttl: 30d")
        _write(vault, "无字段.md", "title: 无字段\nkind: knowledge")

        conn = store.connect(root / "memory.db")
        try:
            store.init(conn)
            importer.sync(conn, vault)

            high = conn.execute(
                "SELECT * FROM cards WHERE rel_path = ?", ("03-Knowledge/有优先级.md",)
            ).fetchone()
            assert high["priority"] == 7
            assert high["ttl"] == "30d"
            # 30d ≈ 30 天；给 5 分钟容差，避免测试在整秒边界上抖
            expected = int(time.time()) + 30 * 86400
            assert abs(high["expires_at"] - expected) < 300

            plain = conn.execute(
                "SELECT * FROM cards WHERE rel_path = ?", ("03-Knowledge/无字段.md",)
            ).fetchone()
            assert plain["priority"] == 0
            assert plain["ttl"] == ""
            assert plain["expires_at"] == 0, "没有 ttl 就是永不过期，不能算成已过期"
        finally:
            conn.close()


def test_priority_falls_back_to_zero_instead_of_failing_the_sync() -> None:
    """非法的 priority 是「没设优先级」，不是「整个 vault 索引失败」。

    索引端的职责是如实投影真相源，不是校验它。为一个排序提示词让 49 张卡
    都同步不进去，代价完全不成比例。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-frontmatter-") as raw:
        root = Path(raw)
        vault = root / "vault"
        _write(vault, "非法优先级.md", "title: 非法优先级\npriority: 高")
        _write(vault, "空优先级.md", "title: 空优先级\npriority:")

        conn = store.connect(root / "memory.db")
        try:
            store.init(conn)
            counts = importer.sync(conn, vault)
            assert counts["inserted"] == 2, "非法值不能让别的卡也索引不进去"

            for name in ("非法优先级", "空优先级"):
                row = conn.execute(
                    "SELECT priority FROM cards WHERE rel_path = ?",
                    (f"03-Knowledge/{name}.md",),
                ).fetchone()
                assert row["priority"] == 0, name
        finally:
            conn.close()


def test_parse_ttl_accepts_durations_and_absolute_dates() -> None:
    now = int(time.time())

    # 相对时长
    assert abs(importer._parse_ttl("1d") - (now + 86400)) < 5
    assert abs(importer._parse_ttl("12h") - (now + 43200)) < 5
    assert abs(importer._parse_ttl("45m") - (now + 2700)) < 5
    assert abs(importer._parse_ttl("1d12h") - (now + 86400 + 43200)) < 5
    assert abs(importer._parse_ttl(" 30D ") - (now + 30 * 86400)) < 5

    # 绝对日期（本地时区解析，只断言它确实指向 2026-10-01 这一天）
    absolute = importer._parse_ttl("2026-10-01")
    assert absolute > 0
    assert time.strftime("%Y-%m-%d", time.localtime(absolute)) == "2026-10-01"
    assert importer._parse_ttl("2026-10-01T12:00:00") > 0

    # 「永不过期」的各种写法，以及不认识的写法 —— 一律 0，绝不猜
    for raw in ("", "  ", "none", "NEVER", "-", None, "下个月", "30", "d", "abc1d"):
        assert importer._parse_ttl(raw) == 0, repr(raw)
