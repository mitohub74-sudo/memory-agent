# -*- coding: utf-8 -*-
"""vault → SQLite 导入器。

Markdown 是真相源，本模块负责把它读进索引。特点是**增量**：
用 file_hash 比对，内容没变的卡片直接跳过，不重复写库。

frontmatter 解析刻意不引入 PyYAML —— 只支持本项目实际用到的极简子集
（``key: value`` 标量与 ``[a, b]`` 内联数组），保持零依赖。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .store import delete_cards, upsert_card

__all__ = ["parse_frontmatter", "build_card", "sync", "sync_one"]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 Markdown 开头的 YAML frontmatter，返回 (元数据, 正文)。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text

    meta: dict = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()
        if not key:
            continue
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            meta[key] = (
                [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
                if inner
                else []
            )
        else:
            meta[key] = raw.strip("'\"")

    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


def _strip_duplicate_heading(body: str, title: str) -> str:
    """去掉与标题重复的首个 H1。

    自动沉淀的卡片常见「frontmatter title + 正文首行 H1 完全相同」的冗余，
    标题已单独建索引，正文里再留一份纯属噪音。
    """
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if line.strip().lstrip("#").strip() == title.strip():
            return "\n".join(lines[i + 1 :]).strip()
        return body
    return body


def _first_heading(body: str) -> str:
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
    return ""


def build_card(vault: Path, path: Path) -> dict:
    """把一个 Markdown 文件读成卡片字典。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_frontmatter(text)
    rel = path.relative_to(vault).as_posix()

    title = str(meta.get("title") or "").strip() or _first_heading(body) or path.stem
    body = _strip_duplicate_heading(body, title)

    tags = meta.get("tags", [])
    if not isinstance(tags, list):
        tags = [t.strip() for t in str(tags).split(",") if t.strip()]

    return {
        "rel_path": rel,
        "title": title,
        "kind": str(meta.get("kind", "")),
        "status": str(meta.get("status", "")),
        "source": str(meta.get("source") or meta.get("submittedBy") or ""),
        "tags": tags,
        "created": str(meta.get("created", "")),
        "updated": str(meta.get("updated", "")),
        "body": body,
        # file_hash = 整份文件文本（含 frontmatter）的哈希，用于增量比对。
        # 与 capture 的 card_fingerprint（标题+正文）是两个不同的东西，
        # 名字必须区分，否则很容易误读成同一个值。
        "file_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    }


def iter_markdown(vault: Path):
    """遍历 vault 下的 Markdown 文件，跳过 .git。"""
    for path in sorted(vault.rglob("*.md")):
        if ".git" in path.parts:
            continue
        if path.is_file():
            yield path


def sync_one(
    conn: sqlite3.Connection,
    vault: str | Path,
    path: str | Path,
    card: dict | None = None,
) -> str:
    """把一个 Markdown 文件同步进索引，返回 ``inserted`` / ``updated`` / ``unchanged``。

    这是**唯一**的单文件索引入口 —— 全量 :func:`sync` 与采集端都走这里。

    为什么要强调唯一：两处各写一套「建卡 + 写库」逻辑，迟早会在解析细节上分叉
    （某一边先支持了新字段、某一边忘了处理 BOM）。分叉是**静默**的 ——
    同一张卡在两条路径下得到不同结果，索引里出现 Markdown 中不存在的状态，
    没有任何报错。

    ``card`` 可传入已构建好的卡片字典，供全量同步复用，避免重复读文件。
    """
    if card is None:
        card = build_card(Path(vault), Path(path))
    return upsert_card(conn, card)


def sync(conn: sqlite3.Connection, vault: str | Path, rebuild: bool = False) -> dict:
    """把 vault 同步进索引。

    返回各类计数：inserted / updated / unchanged / removed。
    ``rebuild=True`` 会先清空索引再全量导入（Markdown 不受影响）。

    每个文件都经由 :func:`sync_one` 落库 —— 全量同步只是「遍历 + 逐个 sync_one」，
    不另起一条写入路径。
    """
    vault = Path(vault)
    if not vault.is_dir():
        raise FileNotFoundError(f"vault 目录不存在：{vault}")

    if rebuild:
        conn.execute("DELETE FROM cards_fts")
        conn.execute("DELETE FROM cards")
        conn.commit()

    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    seen: set[str] = set()

    for path in iter_markdown(vault):
        card = build_card(vault, path)
        seen.add(card["rel_path"])
        counts[sync_one(conn, vault, path, card=card)] += 1

    existing = {r["rel_path"] for r in conn.execute("SELECT rel_path FROM cards")}
    counts["removed"] = delete_cards(conn, sorted(existing - seen))
    conn.commit()
    return counts
