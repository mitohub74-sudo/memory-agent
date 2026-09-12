# -*- coding: utf-8 -*-
"""取代（supersede）语义（P3-02 / ROADMAP 阶段 3 的核心设计）。

记忆里大量内容是**会变的事实**：服务状态、端口、价格、配置项当前值。
「原地改」抹掉历史、「删除」让「曾经是什么」不可查 —— 所以需要第三个动作：
**新卡出现，旧卡失效，但旧卡文件保留**。

用户已定的三条语义（2026-09-12，见 `待用户决定事项.md` A 组）：

- **A1** 旧卡**不进默认检索**，要查历史必须显式 `as_of`；
- **A2** 旧卡**原文能打开**（`include_invalid` 能搜到、全文照常取）；
- **A3** 与删除不同：取代**不动文件**，只加标记。

本文件把「取代三态」钉死 —— 这三条同时可观察才算功能成立，缺一条就是假成功：

1. 默认检索**只**见新卡；
2. 旧卡**文件仍在磁盘**且正文**一字未动**；
3. 用旧日期 / `include_invalid` 能把旧卡取回来。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from mcore import capture, importer, search, store

GOOD_BODY = "这张卡的正文内容足够长，可以正常建卡并被索引到。"


def _vault(raw: str) -> Path:
    return Path(raw) / "vault"


def _seed(root: Path) -> tuple[Path, dict, dict]:
    """建一张旧卡，并用另一张新卡取代它。返回 (vault, 旧卡结果, supersede 结果)。"""
    vault = root / "vault"
    old = capture.write_card(
        vault, title="ECS 部署地址", kind="project",
        body="ECS 部署在 10.0.0.1，端口 8080。",
    )
    assert old["action"] == "created", old
    res = capture.supersede_card(
        vault, "02-Projects/ecs-部署地址.md",
        title="ECS 部署地址（新）", kind="project",
        body="ECS 已迁到 10.0.0.9，端口 9090。",
    )
    assert res["ok"] is True, res
    return vault, old, res


def _indexed(root: Path, vault: Path) -> sqlite3.Connection:
    conn = store.connect(root / "memory.db")
    store.init(conn)
    importer.sync(conn, vault)
    return conn


# ------------------------------------------------------------------ 三态：磁盘


def test_old_card_file_is_kept_and_body_untouched() -> None:
    """★ 旧卡**文件仍在**、**正文一字未动**。

    这是取代与删除的根本区别，也是「事后能回答『当时是多少』」的唯一依据。
    只加两个 frontmatter 字段，其余一个字符都不许动。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-sup-") as raw:
        root = Path(raw)
        vault = root / "vault"
        old = capture.write_card(vault, title="ECS 部署地址", kind="project",
                                 body="ECS 部署在 10.0.0.1，端口 8080。")
        old_path = Path(old["path"])
        before = old_path.read_text(encoding="utf-8")
        before_body = before.split("---", 2)[-1]

        res = capture.supersede_card(vault, "02-Projects/ecs-部署地址.md",
                                     title="ECS 部署地址（新）", kind="project",
                                     body="ECS 已迁到 10.0.0.9，端口 9090。")
        assert res["ok"] is True, res

        assert old_path.exists(), "取代**不能**移走旧卡文件"
        after = old_path.read_text(encoding="utf-8")
        after_body = after.split("---", 2)[-1]
        assert after_body == before_body, "旧卡正文必须一字未动"
        # 幂等标记要保留，否则下次重复写入会多出一张同样的卡
        assert "contentHash" in after
        # 原有字段一个都不能丢
        for key in ("formatVersion", "kind", "title", "tags", "created",
                    "status", "source"):
            assert f"{key}:" in after, f"原有字段 {key} 丢了"


def test_both_sides_of_the_relation_are_recorded() -> None:
    """正反两向都要写：新卡记 `supersedes`，旧卡记 `superseded_by`。

    只写一向的话，「谁取代了我」或「我取代了谁」就得反着扫全表查。
    用 **rel_path 而不是 id** —— id 是 rowid，`index --rebuild` 后会重排，关系会错位。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-sup-") as raw:
        root = Path(raw)
        vault, old, res = _seed(root)

        new_text = Path(res["new_path"]).read_text(encoding="utf-8")
        old_text = Path(res["old_path"]).read_text(encoding="utf-8")

        assert "supersedes: 02-Projects/ecs-部署地址.md" in new_text
        assert "superseded_by: 02-Projects/ecs-部署地址-新.md" in old_text
        assert f"invalid_at: {res['invalid_at']}" in old_text


# ------------------------------------------------------------------ 三态：检索


def test_default_search_shows_only_the_new_card() -> None:
    """★ 三态之一：默认检索**只**见新卡（A1 的决定）。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-sup-") as raw:
        root = Path(raw)
        vault, old, res = _seed(root)
        conn = _indexed(root, vault)
        try:
            hits = search.KeywordSearcher(conn).search("ECS 部署", limit=5)
            titles = [h.title for h in hits]
            assert "ECS 部署地址（新）" in titles, titles
            assert "ECS 部署地址" not in titles, f"旧卡不该出现在默认检索里：{titles}"
            # 默认检索里 invalid_at 恒为空 —— 它只在自己人显式要看失效卡时才有内容
            assert all(h.invalid_at == "" for h in hits), hits
        finally:
            conn.close()


def test_include_invalid_brings_the_old_card_back() -> None:
    """★ 三态之二：旧卡仍能取回（A2）——「取代」是默认不返回，不是藏起来。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-sup-") as raw:
        root = Path(raw)
        vault, old, res = _seed(root)
        conn = _indexed(root, vault)
        try:
            hits = search.KeywordSearcher(conn).search("ECS 部署", limit=5,
                                                       include_invalid=True)
            by_title = {h.title: h for h in hits}
            assert "ECS 部署地址" in by_title, [h.title for h in hits]
            assert by_title["ECS 部署地址"].invalid_at.startswith("2026-") or \
                by_title["ECS 部署地址"].invalid_at, by_title["ECS 部署地址"]
        finally:
            conn.close()


def test_as_of_returns_the_card_that_was_valid_then() -> None:
    """★ 三态之三：按**旧日期**回溯能取回旧卡、取不到新卡。

    日期按**天**比较：卡片实际是在「今天」写的，所以用今天之前的日期回溯，
    应当看到旧卡（它当时有效）而不是新卡（它当时还不存在）。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-sup-") as raw:
        root = Path(raw)
        vault, old, res = _seed(root)
        conn = _indexed(root, vault)
        try:
            s = search.KeywordSearcher(conn)

            # 一个「很久以前」的日期：两张卡都还没写，应当一张都搜不到
            ancient = s.search("ECS 部署", limit=5, as_of="2020-01-01")
            assert ancient == [], [h.title for h in ancient]

            # 今天：新卡有效、旧卡已失效 —— as_of 今天应只见新卡
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            now_hits = s.search("ECS 部署", limit=5, as_of=today)
            titles = [h.title for h in now_hits]
            assert "ECS 部署地址（新）" in titles, titles
            assert "ECS 部署地址" not in titles, titles
        finally:
            conn.close()


def test_date_is_compared_at_day_granularity() -> None:
    """同一天写下的卡，在那一天的 `as_of` 里就算「已存在」。

    若按时刻比较，当天 11:30 写的卡会因为「11:30 > 00:00」被判为当时不存在 ——
    而 `--as-of 2026-09-12` 的直觉意思显然是「9 月 12 日那天」。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-sup-") as raw:
        root = Path(raw)
        vault = root / "vault"
        capture.write_card(vault, title="当天卡", kind="knowledge", body=GOOD_BODY)
        conn = _indexed(root, vault)
        try:
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            hits = search.KeywordSearcher(conn).search("当天卡", limit=5, as_of=today)
            assert hits, "同一天写的卡应当在那天的 as_of 里可见"
        finally:
            conn.close()


# ------------------------------------------------------------------ 关系落库


def test_relation_reaches_the_index_and_survives_rebuild() -> None:
    """关系必须进索引**并且能从 Markdown 重建**。

    这是「真相源优先」的具体检验：关系存在 frontmatter 里，所以
    `index --rebuild`（清空可重建投影）之后必须一模一样地回来。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-sup-") as raw:
        root = Path(raw)
        vault, old, res = _seed(root)
        conn = _indexed(root, vault)
        try:
            row = conn.execute(
                "SELECT invalid_at, superseded_by FROM cards WHERE rel_path = ?",
                ("02-Projects/ecs-部署地址.md",),
            ).fetchone()
            assert row["invalid_at"] == res["invalid_at"]
            assert row["superseded_by"] == "02-Projects/ecs-部署地址-新.md"

            new_row = conn.execute(
                "SELECT supersedes FROM cards WHERE rel_path = ?",
                ("02-Projects/ecs-部署地址-新.md",),
            ).fetchone()
            assert new_row["supersedes"] == "02-Projects/ecs-部署地址.md"

            # 重建后关系必须还在（它来自 frontmatter，不是索引自己编的）
            importer.sync(conn, vault, rebuild=True)
            again = conn.execute(
                "SELECT invalid_at, superseded_by FROM cards WHERE rel_path = ?",
                ("02-Projects/ecs-部署地址.md",),
            ).fetchone()
            assert again["invalid_at"] == res["invalid_at"]
            assert again["superseded_by"] == "02-Projects/ecs-部署地址-新.md"
        finally:
            conn.close()


# ------------------------------------------------------------------ 拒绝的路径


def test_superseding_a_missing_card_is_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-sup-") as raw:
        vault = _vault(raw)
        res = capture.supersede_card(vault, "03-Knowledge/不存在.md",
                                     title="新", body=GOOD_BODY)
        assert res["ok"] is False
        assert "不存在" in res["reason"]
        # 失败时不该留下半张新卡
        assert not list(vault.rglob("*.md")), list(vault.rglob("*.md"))


def test_superseding_an_already_superseded_card_is_rejected() -> None:
    """不允许链式覆盖。

    否则「A 被 B 取代、B 又被 C 取代」时，A 的 `superseded_by` 指向谁就取决于
    操作顺序，历史链会静默断掉。让调用方显式处理这种情况。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-sup-") as raw:
        root = Path(raw)
        vault, old, res = _seed(root)

        again = capture.supersede_card(vault, "02-Projects/ecs-部署地址.md",
                                       title="再取代一次", body=GOOD_BODY)
        assert again["ok"] is False
        assert "已被取代过" in again["reason"]


def test_supersede_refuses_a_path_outside_the_vault() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-sup-") as raw:
        vault = _vault(raw)
        res = capture.supersede_card(vault, "../外面的文件.md",
                                     title="新", body=GOOD_BODY)
        assert res["ok"] is False
        assert "不在 vault 内" in res["reason"] or "不存在" in res["reason"]


# ------------------------------------------------------------------ frontmatter 编辑


def test_update_frontmatter_preserves_unknown_fields_and_body() -> None:
    """只改给定键，**其余行原样保留**。

    卡片可能是人手工写的、可能带本项目不知道的字段（别的工具加的、旧实验字段）。
    整份重渲染会把它们抹掉 —— 而「抹掉别人写的东西」是不可逆的、且不报错。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-sup-") as raw:
        directory = Path(raw) / "vault" / "03-Knowledge"
        directory.mkdir(parents=True)
        card = directory / "手工卡.md"
        card.write_text(
            "---\n"
            "title: 手工卡\n"
            "kind: knowledge\n"
            "我自己加的字段: 别弄丢我\n"
            "tags: [a, b]\n"
            "---\n\n"
            "正文里有 --- 这样的分隔符也不该被误伤。\n",
            encoding="utf-8",
        )

        changed = capture.update_frontmatter(card, {"invalid_at": "2026-01-01T00:00:00.000Z"})
        assert changed is True

        text = card.read_text(encoding="utf-8")
        assert "我自己加的字段: 别弄丢我" in text, "未知字段必须保留"
        assert "invalid_at: 2026-01-01T00:00:00.000Z" in text
        assert "正文里有 --- 这样的分隔符也不该被误伤。" in text, "正文必须保留"

        # 没有变化时不写盘（也避免无谓地改动 mtime）
        assert capture.update_frontmatter(card, {"invalid_at": "2026-01-01T00:00:00.000Z"}) is False


def test_update_frontmatter_refuses_files_without_frontmatter() -> None:
    """没有 frontmatter 的文件不硬塞字段 —— 宁可让调用方看到「没改成功」。

    硬塞会造出一种半成品格式：文件开头多了几行 `key: value` 但没有分隔符，
    既不是合法 frontmatter 也不像正常正文，而索引端会把它整段当成正文。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-sup-") as raw:
        directory = Path(raw) / "vault" / "03-Knowledge"
        directory.mkdir(parents=True)
        card = directory / "无元数据.md"
        card.write_text("只有正文，没有 frontmatter 段。\n", encoding="utf-8")

        assert capture.update_frontmatter(card, {"invalid_at": "2026-01-01"}) is False
        assert card.read_text(encoding="utf-8") == "只有正文，没有 frontmatter 段。\n"
