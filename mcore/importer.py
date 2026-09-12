# -*- coding: utf-8 -*-
"""vault → SQLite 导入器。

Markdown 是真相源，本模块负责把它读进索引。特点是**增量**：
用 file_hash 比对，内容没变的卡片直接跳过，不重复写库。

frontmatter 解析刻意不引入 PyYAML —— 只支持本项目实际用到的极简子集
（``key: value`` 标量与 ``[a, b]`` 内联数组），保持零依赖。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from datetime import datetime
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


def _parse_priority(raw) -> int:
    """解析 frontmatter 的 priority。非法值退回 0，不抛错。

    索引端的职责是如实投影真相源，不是校验它。一张卡写了
    ``priority: 高`` 只该被当作「没设优先级」，而不是让整个 vault 同步失败 ——
    为了一个排序提示词让 49 张卡都索引不进去，代价完全不成比例。
    """
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def _parse_ttl(raw) -> int:
    """解析 frontmatter 的 ttl，返回**过期时刻**（unix 秒）。``0`` 表示永不过期。

    支持两种写法：

    - 时长：``30d`` / ``12h`` / ``45m``（d/h/m 后缀，可组合如 ``1d12h``）
    - 绝对时刻：``2026-10-01`` 或 ``2026-10-01T12:00:00``

    相对时长从**现在**起算，不绑定卡片时间戳 —— 卡片被编辑时 ttl 语义就是
    「从这次编辑起再活 X」，用 created 起算会让改一次就立即过期。
    """
    if raw is None:
        return 0
    text = str(raw).strip().strip("'\"")
    if not text or text.lower() in ("none", "never", "-"):
        return 0

    # 绝对时刻
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue

    # 相对时长：1d12h30m（整串只允许数字与 d/h/m，别的一律不认）
    compact = re.sub(r"\s+", "", text.lower())
    parts = re.findall(r"(\d+)([dhm])", compact)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != compact:
        return 0
    seconds = 0
    for value, unit in parts:
        seconds += int(value) * {"d": 86400, "h": 3600, "m": 60}[unit]
    return int(time.time()) + seconds if seconds else 0


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

    ttl_raw = str(meta.get("ttl", "") or "")

    return {
        "rel_path": rel,
        "title": title,
        "kind": str(meta.get("kind", "")),
        "status": str(meta.get("status", "")),
        "source": str(meta.get("source") or meta.get("submittedBy") or ""),
        "tags": tags,
        "created": str(meta.get("created", "")),
        "updated": str(meta.get("updated", "")),
        # priority / ttl 放在 frontmatter（真相源），不放索引：
        # 索引是可重建的，放索引里的字段重建即丢。
        "priority": _parse_priority(meta.get("priority", 0)),
        "ttl": ttl_raw,
        "expires_at": _parse_ttl(ttl_raw),
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

    本函数自己保证 schema 就位（``store.init``），不依赖调用方记得先初始化。
    顺序需要说明一下：``sync(rebuild=True)`` 会先清表，然后才逐个调本函数，
    ``init`` 里最后那遍 DDL 全是 ``IF NOT EXISTS``，所以不会把刚清空的表又「补」出内容。
    这样安排之后，任何写入路径都不会在缺表 / 缺列的库上跑到一半才报错。

    为什么要强调唯一：两处各写一套「建卡 + 写库」逻辑，迟早会在解析细节上分叉
    （某一边先支持了新字段、某一边忘了处理 BOM）。分叉是**静默**的 ——
    同一张卡在两条路径下得到不同结果，索引里出现 Markdown 中不存在的状态，
    没有任何报错。

    ``card`` 可传入已构建好的卡片字典，供全量同步复用，避免重复读文件。
    """
    from .store import init as store_init

    store_init(conn)
    if card is None:
        card = build_card(Path(vault), Path(path))
    return upsert_card(conn, card)


def sync(conn: sqlite3.Connection, vault: str | Path, rebuild: bool = False) -> dict:
    """把 vault 同步进索引。

    返回各类计数：inserted / updated / unchanged / removed。
    ``rebuild=True`` 会先清空索引再全量导入（Markdown 不受影响）。

    **``rebuild`` 不清 ``card_stats``** —— 访问统计不是从 vault 推导出来的投影，
    它没有别的来源；跟着 cards 一起清掉就是不可恢复的丢失。清掉「可重建的」、
    留下「不可重建的」，这条界线的依据是**能否从真相源重新算出来**，
    不是「表名像不像索引」。

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
