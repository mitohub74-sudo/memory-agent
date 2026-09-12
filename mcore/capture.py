# -*- coding: utf-8 -*-
"""采集端：把知识写成 Markdown 卡片，落进 vault。

为什么由 agent 来蒸馏
--------------------
记忆的价值在于压缩。把整段会话原样倒进 vault，只会制造噪音，检索时反而
更难找到重点。本模块**不做 LLM 调用** —— 蒸馏交给调用方：agent 本身就是
LLM，让它先想清楚「什么值得记、怎么写得让人以后能看懂」，再交给这里落盘。

这样零额外成本、零 API key、数据不出网。

格式兼容
--------
frontmatter 与 vault 中既有卡片完全一致，因此新旧卡片可以共存、互相检索。
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["KIND_DIRS", "DEFAULT_KIND", "write_card", "slugify"]

# kind -> 分类目录（与既有 vault 结构一致）
KIND_DIRS: dict[str, str] = {
    "system": "00-System",
    "project": "02-Projects",
    "knowledge": "03-Knowledge",
    "content": "04-Content",
    "prompt": "05-Prompts",
    "business": "06-Business",
    "tool": "07-Tools",
    "mistake": "08-Mistakes",
}
DEFAULT_KIND = "knowledge"
FORMAT_VERSION = 1

# 正文最小长度：太短的多半是碎片，不值得占一个卡片位
MIN_BODY_CHARS = 20
# 文件名长度上限（Windows 限制 255，留出后缀与扩展名余量）
MAX_SLUG_CHARS = 60

# 文件名安全化：保留中文、字母数字、连字符；其余折叠为连字符
_UNSAFE = re.compile(r"[^\w\u4e00-\u9fff-]+", re.UNICODE)
_DASHES = re.compile(r"-{2,}")


def slugify(title: str) -> str:
    """把标题转成安全的文件名片段。中文原样保留。"""
    s = (title or "").strip().lower()
    s = _UNSAFE.sub("-", s)
    s = _DASHES.sub("-", s).strip("-")
    if len(s) > MAX_SLUG_CHARS:
        s = s[:MAX_SLUG_CHARS].rstrip("-")
    return s or "untitled"


def _now_iso() -> str:
    """与既有卡片一致的 ISO 时间戳（毫秒 + Z）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def _yaml_list(items: list[str]) -> str:
    """内联数组，元素含逗号或方括号时加引号。"""
    out = []
    for it in items:
        s = str(it).strip()
        if not s:
            continue
        out.append(f'"{s}"' if any(c in s for c in ',[]"') else s)
    return "[" + ", ".join(out) + "]"


def _render(title: str, body: str, kind: str, tags: list[str], source: str,
            status: str, severity: str, reason: str, ts: str) -> str:
    fm = [
        "---",
        f"formatVersion: {FORMAT_VERSION}",
        f"kind: {kind}",
        f'title: "{title}"' if any(c in title for c in ':"') else f"title: {title}",
        f"tags: {_yaml_list(tags)}",
        f"created: {ts}",
        f"updated: {ts}",
        f"status: {status}",
        f"submittedBy: {source}",
        f"severity: {severity}",
        f"reason: {reason}",
        f"source: {source}",
        "---",
        "",
    ]
    return "\n".join(fm) + body.strip() + "\n"


def _unique_path(directory: Path, slug: str) -> Path:
    """避开重名：slug.md 被占用时依次尝试 slug-2.md、slug-3.md…"""
    path = directory / f"{slug}.md"
    n = 2
    while path.exists():
        path = directory / f"{slug}-{n}.md"
        n += 1
    return path


def write_card(
    vault: str | Path,
    *,
    title: str,
    body: str,
    kind: str = DEFAULT_KIND,
    tags: list[str] | None = None,
    source: str = "agent",
    status: str = "approved",
    severity: str = "info",
    reason: str = "agent 主动沉淀",
) -> dict:
    """写入一张卡片。

    返回 ``{"ok", "action", "path", "reason"}``。
    ``action`` 为 ``created`` / ``unchanged`` / ``rejected``。

    幂等：标题与正文都相同的卡片重复写入时不会产生副本。
    """
    vault = Path(vault)
    title = (title or "").strip()
    body = (body or "").strip()

    if not title:
        return {"ok": False, "action": "rejected", "reason": "标题不能为空"}
    if len(body) < MIN_BODY_CHARS:
        return {"ok": False, "action": "rejected",
                "reason": f"正文过短（{len(body)} < {MIN_BODY_CHARS} 字符），不值得建卡"}

    kind = (kind or DEFAULT_KIND).strip().lower()
    if kind not in KIND_DIRS:
        kind = DEFAULT_KIND
    directory = vault / KIND_DIRS[kind]

    slug = slugify(title)
    # 卡片指纹：由「标题 + 正文」算出，与 importer 的 file_hash（整份文件文本）
    # 不是一回事，所以名字必须区分开。
    card_fingerprint = hashlib.sha256(
        f"{title}\n{body}".encode("utf-8")
    ).hexdigest()[:16]

    # 幂等检查：同 slug 且指纹一致 -> 视为重复
    # 注意：写进文件的标记字符串仍是 ``contentHash``，不随变量改名而变 ——
    # 改了它，磁盘上既有卡片的标记就再也匹配不上。
    for existing in sorted(directory.glob(f"{slug}*.md")):
        try:
            text = existing.read_text(encoding="utf-8")
        except Exception:
            continue
        marker = f"contentHash: {card_fingerprint}"
        if marker in text or hashlib.sha256(
            f"{title}\n{text.split('---', 2)[-1].strip()}".encode("utf-8")
        ).hexdigest()[:16] == card_fingerprint:
            return {"ok": True, "action": "unchanged",
                    "path": str(existing), "reason": "内容相同，已存在"}

    directory.mkdir(parents=True, exist_ok=True)
    path = _unique_path(directory, slug)

    text = _render(title, body, kind, tags or [], source, status, severity,
                   reason, _now_iso())
    # 指纹写进注释，供下次幂等比对（标记名保持 contentHash 不变）
    text = text.rstrip("\n") + f"\n\n<!-- contentHash: {card_fingerprint} -->\n"

    # 原子写入：先写临时文件再改名，避免中途失败留下半截文件
    tmp = path.with_suffix(".md.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    return {"ok": True, "action": "created", "path": str(path),
            "reason": f"已写入 {KIND_DIRS[kind]}/"}
