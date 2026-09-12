# -*- coding: utf-8 -*-
"""软删的落点 `.trash` 必须被排除出索引（P3-02 S4）。

这一条不是「顺手加的优化」，而是 `delete` 能成立的前置条件。

软删把卡片移到 `<vault>/.trash/<原相对路径>` —— 它是 vault 的**子目录**，
会被 `rglob("*.md")` 扫到。不排除的话：

    删除一张卡 → 文件移到 .trash → 下次索引又把它当新卡扫进来
    → 卡片以新路径**复活**，而 `delete` 返回的是「成功」

这是典型的**静默假成功**：命令说删了，检索照样能命中它。本文件先说清这个机理，
再用测试把它钉死 —— 因为「排除 .trash」这一行看起来太平凡，很容易被后来的人
当成冗余而删掉。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from mcore import importer, store

GOOD_BODY = "这张卡的正文内容足够长，可以正常建卡并被索引到。"


def _write(vault: Path, rel: str, title: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\nkind: knowledge\n---\n\n{GOOD_BODY}\n",
        encoding="utf-8",
    )
    return path


def test_trash_directory_is_not_indexed() -> None:
    """`.trash` 里的文件不得被索引 —— 否则软删的卡片会复活。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-trash-") as raw:
        root = Path(raw)
        vault = root / "vault"

        alive = _write(vault, "03-Knowledge/活着的卡.md", "活着的卡")
        trashed = _write(vault, ".trash/03-Knowledge/被删的卡.md", "被删的卡")

        scanned = {p.relative_to(vault).as_posix() for p in importer.iter_markdown(vault)}
        assert "03-Knowledge/活着的卡.md" in scanned, scanned
        assert ".trash/03-Knowledge/被删的卡.md" not in scanned, (
            f".trash 里的文件被扫到了 —— 删除会静默失效：{scanned}"
        )

        conn = store.connect(root / "memory.db")
        try:
            store.init(conn)
            importer.sync(conn, vault)
            rels = {r["rel_path"] for r in conn.execute("SELECT rel_path FROM cards")}
            assert rels == {"03-Knowledge/活着的卡.md"}, rels
        finally:
            conn.close()

        # 两个文件都还在磁盘上 —— 排除的是**索引**，不是删除文件
        assert alive.exists() and trashed.exists()


def test_moving_a_card_into_trash_makes_it_disappear_from_the_index() -> None:
    """模拟真实的软删流程，验证卡片确实从索引里消失（而不是换个路径复活）。

    顺序很关键：**先移文件，再同步索引**。反过来的话，同步时文件还在原地，
    索引里会留着它；等文件移走后才不会有人再同步。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-trash-") as raw:
        root = Path(raw)
        vault = root / "vault"
        card = _write(vault, "03-Knowledge/待删卡.md", "待删卡")

        conn = store.connect(root / "memory.db")
        try:
            store.init(conn)
            importer.sync(conn, vault)
            assert store.count_cards(conn) == 1

            # 软删：移到 .trash 下的原相对路径
            trash = vault / ".trash" / "03-Knowledge"
            trash.mkdir(parents=True, exist_ok=True)
            card.rename(trash / card.name)

            counts = importer.sync(conn, vault)

            assert store.count_cards(conn) == 0, "软删后卡片不该还在索引里"
            assert counts["removed"] == 1, counts
            assert counts["inserted"] == 0, f"不该把 .trash 里的文件当新卡扫进来：{counts}"

            # 文件仍在 .trash 里（可恢复）
            assert (trash / card.name).exists()
        finally:
            conn.close()


def test_git_directory_is_still_excluded() -> None:
    """顺手确认原有的 `.git` 排除没被这次改动弄坏。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-trash-") as raw:
        vault = Path(raw) / "vault"
        _write(vault, "03-Knowledge/正常卡.md", "正常卡")
        _write(vault, ".git/某个.md", "git 里的东西")

        scanned = {p.relative_to(vault).as_posix() for p in importer.iter_markdown(vault)}
        assert scanned == {"03-Knowledge/正常卡.md"}, scanned


def test_a_card_literally_named_trash_directory_is_not_confused() -> None:
    """只跳过**目录**名恰好是 `.trash` 的路径，不会误伤别的名字。

    用一个形近但不同的目录名（`.trash-old`）确认不是「模糊匹配」。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-trash-") as raw:
        vault = Path(raw) / "vault"
        _write(vault, "03-Knowledge/正常卡.md", "正常卡")
        _write(vault, ".trash-old/03-Knowledge/不叫 trash 的卡.md", "不叫 trash 的卡")

        scanned = {p.relative_to(vault).as_posix() for p in importer.iter_markdown(vault)}
        assert ".trash-old/03-Knowledge/不叫 trash 的卡.md" in scanned, (
            f"只该跳过目录名恰为 .trash 的路径：{scanned}"
        )
