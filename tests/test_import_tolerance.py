# -*- coding: utf-8 -*-
"""导入容错：坏文件不中断、不静默、不丢数据（阶段 4 P4-05 / P4-06 / P4-07）。

这三项是连在一起的，分开测会漏掉最要紧的那条：

- **P4-05** 逐文件 try/except，坏文件不中断全量索引；
- **P4-06** 读文件前剥 BOM；
- **P4-07** 严格解码（不再 ``errors="replace"``）。

三者合起来要防的是**两种反向的坏结局**：

1. **一个坏文件让整批进不去** —— 全有或全无；
2. **「容错」变成静默丢数据** —— 坏文件被跳过，而它的卡片因为没进 `seen`
   被删除检测当成「文件已消失」，从索引里**删掉**。此后检索不到它，
   而 `index` 返回的是「成功」。文件其实好好地躺在磁盘上。

第 2 条是本轮实现时才想清楚的：`seen` 的语义必须是「**磁盘上存在的**卡片」，
而不是「**成功读到的**卡片」。测试里专门钉住它。

另外 P4-07 的取向要说明白：非 UTF-8 文件不该被 ``replace`` 成 ``\\ufffd``
然后当成成功 —— 那会静默改写用户内容并索引进库。它必须是一个**被明确报出来的错误**。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcore import importer, store

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = ROOT / "memory.py"

GOOD_BODY = "这张卡的正文内容足够长，可以正常建卡并被索引到。"
BROKEN_BODY = "这张卡的正文内容足够长，但文件字节不是合法的 UTF-8。"


def _vault_with(root: Path, good: int = 1, broken_bytes: bool = False,
                bom: bool = False) -> Path:
    vault = root / "vault"
    directory = vault / "03-Knowledge"
    directory.mkdir(parents=True)
    for i in range(good):
        (directory / f"好卡{i}.md").write_text(
            f"---\ntitle: 好卡{i}\nkind: knowledge\n---\n\n{GOOD_BODY}\n",
            encoding="utf-8",
        )
    if broken_bytes:
        # 手写非法 UTF-8 字节：单独的 0x80 续字节，且不构成合法序列
        (directory / "坏编码卡.md").write_bytes(
            ("---\ntitle: 坏编码卡\nkind: knowledge\n---\n\n" + BROKEN_BODY).encode("utf-8")
            + b"\n\x80\x81\xfe\n"
        )
    if bom:
        (directory / "带BOM卡.md").write_bytes(
            b"\xef\xbb\xbf"
            + "---\ntitle: 带BOM卡\nkind: knowledge\ntags: [a, b]\n---\n\nBOM 卡正文。\n".encode("utf-8")
        )
    return vault


def _sync(root: Path, vault: Path) -> tuple[dict, object]:
    conn = store.connect(root / "memory.db")
    store.init(conn)
    counts = importer.sync(conn, vault)
    return counts, conn


# ------------------------------------------------------------------ P4-05


def test_bad_file_does_not_interrupt_the_full_index() -> None:
    """一个坏文件不该让其余 N 张卡都进不去。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-bad-") as raw:
        root = Path(raw)
        vault = _vault_with(root, good=3, broken_bytes=True)
        counts, conn = _sync(root, vault)
        try:
            assert counts["inserted"] == 3, counts
            assert len(counts["errors"]) == 1, counts
            assert counts["errors"][0]["path"] == "03-Knowledge/坏编码卡.md"
            assert "UnicodeDecodeError" in counts["errors"][0]["error"]
            assert store.count_cards(conn) == 3
        finally:
            conn.close()


def test_errors_are_empty_on_a_clean_vault() -> None:
    """正常语料不该产生任何 errors —— 否则这个字段会被当成噪音忽略。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-bad-") as raw:
        root = Path(raw)
        vault = _vault_with(root, good=2)
        counts, conn = _sync(root, vault)
        try:
            assert counts["errors"] == [], counts
        finally:
            conn.close()


def test_bad_file_does_not_get_its_card_deleted_from_the_index() -> None:
    """**最要紧的一条。** 坏文件绝不能让它的卡片从索引里消失。

    `seen` 的语义是「磁盘上存在的卡片」，不是「成功读到的卡片」。
    首版实现把坏文件排除在 `seen` 之外，于是删除检测认为该文件已消失，
    把它的卡片从索引里删掉 —— 文件还在磁盘上，索引却没了它，
    而 `index` 返回「成功」。这是「容错」变成「静默丢数据」的典型路径。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-bad-") as raw:
        root = Path(raw)
        vault = _vault_with(root, good=1)

        # 先正常索引一张卡
        counts, conn = _sync(root, vault)
        try:
            assert counts["inserted"] == 1, counts
            assert store.count_cards(conn) == 1
        finally:
            conn.close()

        # 把那张卡改成非法编码（文件仍在原路径）
        card = vault / "03-Knowledge" / "好卡0.md"
        card.write_bytes(b"---\ntitle: \xff\xfe bad\n---\n\n\x80\x81\n")

        counts, conn = _sync(root, vault)
        try:
            assert len(counts["errors"]) == 1, counts
            assert counts["removed"] == 0, (
                f"坏文件不该导致卡片被移除：{counts}"
            )
            assert store.count_cards(conn) == 1, "卡片必须仍在索引里（保留旧的已索引内容）"
        finally:
            conn.close()


def test_sync_returns_errors_key_even_when_clean() -> None:
    """字段必须始终存在，调用方才能无条件读它而不必先判断有没有。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-bad-") as raw:
        root = Path(raw)
        vault = _vault_with(root, good=1)
        counts, conn = _sync(root, vault)
        try:
            assert "errors" in counts
        finally:
            conn.close()


# ------------------------------------------------------------------ P4-06


def test_bom_file_still_parses_frontmatter() -> None:
    """带 BOM 的文件必须能解析出 frontmatter。

    不剥 BOM 时第一行是 ``\\ufeff---``，与 ``---`` 不相等，于是元数据整段丢失：
    标题退化成文件名、kind/tags/source 全空 —— **而且不报错**，
    看起来只是「这张卡没写元数据」。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-bad-") as raw:
        root = Path(raw)
        vault = _vault_with(root, good=0, bom=True)
        counts, conn = _sync(root, vault)
        try:
            assert counts["errors"] == [], counts
            row = conn.execute(
                "SELECT title, kind, tags FROM cards WHERE rel_path LIKE '%bom%'"
            ).fetchone()
            assert row is not None, "带 BOM 的文件应当被索引"
            assert row["title"] == "带BOM卡", row["title"]
            assert row["kind"] == "knowledge", row["kind"]
            assert json.loads(row["tags"]) == ["a", "b"], row["tags"]
        finally:
            conn.close()


def test_parse_frontmatter_strips_bom_directly() -> None:
    meta, body = importer.parse_frontmatter("\ufeff---\ntitle: T\n---\n\n正文\n")
    assert meta.get("title") == "T", meta
    assert body.strip() == "正文"


# ------------------------------------------------------------------ P4-07


def test_non_utf8_is_reported_not_silently_replaced() -> None:
    """非 UTF-8 必须是**错误**，不是被 ``\\ufffd`` 替换后的成功。

    ``errors="replace"`` 会把非法字节变成替换字符，然后当成功继续走：
    卡片内容被静默改写、还进了索引，而调用方拿到「索引成功」。
    那正是「能返回的假成功」。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-bad-") as raw:
        root = Path(raw)
        vault = _vault_with(root, good=0, broken_bytes=True)
        counts, conn = _sync(root, vault)
        try:
            assert len(counts["errors"]) == 1, counts
            assert counts["inserted"] == 0, "坏文件不该以「改写后的内容」被写入"
            # 索引里不得出现替换字符 —— 那说明它偷偷入库了
            n = conn.execute(
                "SELECT COUNT(*) FROM cards WHERE body LIKE '%\ufffd%' OR title LIKE '%\ufffd%'"
            ).fetchone()[0]
            assert int(n) == 0, "替换字符不得进入索引"
        finally:
            conn.close()


# ------------------------------------------------------------------ CLI 端到端


def test_cli_index_reports_bad_files_and_keeps_exit_code_zero() -> None:
    """CLI：坏文件要出现在 errors 里、在 stderr 上被说出来，但整体仍算成功。

    退出码保持 0 是刻意的：同步**做完了**，只是跳过了坏文件。
    若用非零退出码，脚本化的调用方会把「有坏文件」误当成「同步失败」而重试或告警升级。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-bad-") as raw:
        root = Path(raw)
        vault = _vault_with(root, good=2, broken_bytes=True)
        env = dict(os.environ)
        env["MEMORY_AGENT_VAULT"] = str(vault)
        env["MEMORY_AGENT_DB"] = str(root / "memory.db")

        proc = subprocess.run(
            [sys.executable, str(MEMORY_PY), "index", "--json"],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT),
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["inserted"] == 2, payload
        assert payload["error_count"] == 1, payload
        assert payload["errors"][0]["path"].endswith("坏编码卡.md"), payload["errors"]

        # 非 JSON 模式下坏文件必须出现在 stderr，不能只在 JSON 字段里
        proc = subprocess.run(
            [sys.executable, str(MEMORY_PY), "index"],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT),
        )
        assert proc.returncode == 0, proc.stderr
        assert "坏编码卡.md" in proc.stderr, proc.stderr
        assert "跳过" in proc.stderr, proc.stderr
        assert "坏编码卡.md" not in proc.stdout, "告警不要混进结果输出"
