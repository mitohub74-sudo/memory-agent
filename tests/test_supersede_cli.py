# -*- coding: utf-8 -*-
"""取代的**外壳**（P3-02 的 S2：CLI ``supersede`` + ``search --as-of`` + ``show`` 标注）。

库层（``capture.supersede_card`` + 检索过滤 + ``as_of``）此前已就绪并有测试
（``tests/test_supersede.py``）。本文件补的是**产品面**：能力接没接到命令上。

为什么单独一个文件：库层全绿、CLI 忘了接上，是两层各自都「绿」而产品是坏的经典分叉。
矩阵 #5–#8 因此必须在**真实命令行**上跑一遍：

| # | 动作 | 必须观察到 |
|---|---|---|
| 5 | ``supersede`` | 新卡在磁盘；旧卡文件仍在且 frontmatter 多了 ``invalid_at`` + ``superseded_by`` |
| 6 | 取代后**默认检索** | 只返回新卡 |
| 7 | 取代后 ``search --as-of <旧日期>`` | 返回旧卡 |
| 8 | 取代后 ``show <旧 id>`` | 仍能取回全文，并显式标注已被取代 |
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mcore import search, store

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = ROOT / "memory.py"

OLD_BODY = "ECS 部署在 10.0.0.1，端口 8080，正文长度足够建卡。"
NEW_BODY = "ECS 已迁到 10.0.0.9，端口 9090，正文长度同样足够。"


def _run_cli(env: dict, *argv: str) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(
        [sys.executable, str(MEMORY_PY), *argv],
        capture_output=True, text=True, encoding="utf-8",
        env=full_env, cwd=str(ROOT),
    )


def _cli_env(root: Path, vault: Path) -> dict:
    return {"MEMORY_AGENT_VAULT": str(vault),
            "MEMORY_AGENT_DB": str(root / "memory.db")}


def _seed_via_cli(root: Path, vault: Path) -> tuple[dict, dict]:
    """用真实 CLI 建一张卡并索引。返回 (env, capture 结果)。"""
    env = _cli_env(root, vault)
    r = _run_cli(env, "capture", "--title", "ECS 部署地址", "--kind", "project",
                 "--body", OLD_BODY, "--json")
    assert r.returncode == 0, r.stderr
    return env, json.loads(r.stdout)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ 矩阵 #5


def test_cli_supersede_writes_the_new_card_and_marks_the_old_one() -> None:
    """★ 矩阵 #5：新卡在磁盘；**旧卡文件仍在**，只是多了两个字段。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env, created = _seed_via_cli(root, vault)
        old_path = Path(created["path"])
        old_body_on_disk = old_path.read_text(encoding="utf-8").split("---", 2)[-1]

        r = _run_cli(env, "supersede", "1", "--title", "ECS 部署地址（新）",
                     "--body", NEW_BODY, "--json")
        assert r.returncode == 0, f"{r.stdout} {r.stderr}"
        payload = json.loads(r.stdout)
        assert payload["ok"] is True, payload
        assert payload["indexed"] is True, payload

        new_path = Path(payload["new_path"])
        assert new_path.is_file(), "新卡必须在磁盘上"
        assert NEW_BODY in new_path.read_text(encoding="utf-8")

        # ★ 旧卡文件仍在（这是取代与删除的根本区别），正文一字未动
        assert old_path.is_file(), "取代**不能**移走旧卡文件"
        old_text = old_path.read_text(encoding="utf-8")
        assert old_text.split("---", 2)[-1].split("<!--")[0].strip() == old_body_on_disk.split("<!--")[0].strip(), (
            "旧卡正文必须一字未动"
        )
        assert "invalid_at:" in old_text, old_text
        assert "superseded_by:" in old_text, old_text
        assert "supersedes:" in new_path.read_text(encoding="utf-8")


def test_cli_supersede_inherits_the_old_cards_kind_by_default() -> None:
    """``--kind`` 不给时沿用旧卡的类型 —— 取代默认是「同一件事变了」。

    沿用是刻意的：类型决定卡片目录，而目录是路径的一部分。默认改成 knowledge
    会让「取代一张 project 卡」顺手把它搬进 03-Knowledge，而没有人要求过这件事。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env, created = _seed_via_cli(root, vault)
        assert Path(created["path"]).parent.name == "02-Projects"

        r = _run_cli(env, "supersede", "1", "--title", "继承类型的卡",
                     "--body", NEW_BODY, "--json")
        assert r.returncode == 0, f"{r.stdout} {r.stderr}"
        new_path = Path(json.loads(r.stdout)["new_path"])
        assert new_path.parent.name == "02-Projects", new_path


# ------------------------------------------------------------------ 矩阵 #6


def test_cli_default_search_shows_only_the_new_card_after_supersede() -> None:
    """★ 矩阵 #6：取代后**默认检索只返回新卡**（A1 的决定）。

    用只出现在正文里的标记词（``zzmarker``）检索，避免「标题也变了」这件事
    混进判断里 —— 要验的是**可见性**，不是标题匹配。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = _cli_env(root, vault)

        r = _run_cli(env, "capture", "--title", "标记卡", "--kind", "knowledge",
                     "--body", "这条正文里有 zzmarker，长度足够建卡并被索引到。", "--json")
        assert r.returncode == 0, r.stderr
        old_id = _only_id(env, "zzmarker")

        r = _run_cli(env, "supersede", str(old_id), "--title", "标记卡（新）",
                     "--body", "这条正文里也有 zzmarker，长度足够建卡并被索引。", "--json")
        assert r.returncode == 0, f"{r.stdout} {r.stderr}"

        r = _run_cli(env, "search", "zzmarker", "--json")
        assert r.returncode == 0, r.stdout
        titles = [x["title"] for x in json.loads(r.stdout)["results"]]
        assert titles == ["标记卡（新）"], f"默认检索只该返回新卡：{titles}"


def _only_id(env: dict, query: str) -> int:
    """按检索词取回唯一的 id（CLI 层拿 id 的唯一路径就是 search）。"""
    r = _run_cli(env, "search", query, "--json")
    assert r.returncode == 0, f"检索 {query} 失败：{r.stdout} {r.stderr}"
    results = json.loads(r.stdout)["results"]
    assert len(results) == 1, f"期望唯一命中，实得 {[x['title'] for x in results]}"
    return int(results[0]["id"])


# ------------------------------------------------------------------ 矩阵 #7


def test_cli_as_of_returns_the_old_card_and_not_the_new_one() -> None:
    """★ 矩阵 #7：``--as-of <很久以前>`` 看不到任何一张（那时都还不存在），
    ``--as-of <今天>`` 只见新卡。

    「很久以前」这条断言是关键：它证明日期条件**真的在起作用**，
    而不是被当成没传（那正是未校验的日期字符串会造成的静默后果）。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env, created = _seed_via_cli(root, vault)
        assert _run_cli(env, "supersede", "1", "--title", "ECS 部署地址（新）",
                        "--body", NEW_BODY, "--json").returncode == 0

        r = _run_cli(env, "search", "ECS", "--as-of", "2020-01-01", "--json")
        assert r.returncode == 1, f"很久以前两张卡都还不存在：{r.stdout}"
        assert json.loads(r.stdout)["count"] == 0, r.stdout

        r = _run_cli(env, "search", "ECS", "--as-of", _today(), "--json")
        assert r.returncode == 0, r.stdout
        payload = json.loads(r.stdout)
        assert payload["as_of"] == _today(), payload
        titles = [x["title"] for x in payload["results"]]
        assert "ECS 部署地址（新）" in titles, titles
        assert "ECS 部署地址" not in titles, titles


def test_cli_as_of_on_the_old_cards_own_creation_day_returns_it() -> None:
    """旧卡**被写下的那天**应当算它有效 —— 日期按天比较，不按时刻。

    写卡与取代发生在同一天（都在这次的测试里），所以按天粒度看，那一天旧卡仍有效……
    除非它当天就被取代。这条测试用**取代前一天**的日期来验证：
    那天的视图里应当同时看得到旧卡，而看不到当时还不存在的新卡。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env, created = _seed_via_cli(root, vault)
        assert _run_cli(env, "supersede", "1", "--title", "ECS 部署地址（新）",
                        "--body", NEW_BODY, "--json").returncode == 0

        # 手工把两张卡的时间戳移到「昨天写、今天取代」，让回溯真的有历史可看
        conn = store.connect(root / "memory.db")
        try:
            conn.execute("UPDATE cards SET created = '2026-01-01T08:00:00.000Z', "
                         "invalid_at = '2026-06-01T08:00:00.000Z' WHERE id = 1")
            conn.execute("UPDATE cards SET created = '2026-06-01T08:00:00.000Z' "
                         "WHERE id = 2")
            conn.commit()
        finally:
            conn.close()

        r = _run_cli(env, "search", "ECS", "--as-of", "2026-03-01", "--json")
        assert r.returncode == 0, r.stdout
        titles = [x["title"] for x in json.loads(r.stdout)["results"]]
        assert titles == ["ECS 部署地址"], f"那天有效的只有旧卡：{titles}"

        r = _run_cli(env, "search", "ECS", "--as-of", "2026-07-01", "--json")
        titles = [x["title"] for x in json.loads(r.stdout)["results"]]
        assert titles == ["ECS 部署地址（新）"], f"那天有效的只有新卡：{titles}"


def test_cli_include_invalid_returns_the_old_card_with_its_failure_time() -> None:
    """A2：旧卡**原文仍能取回**。``--include-invalid`` 要把它带回来并标注失效时间。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env, created = _seed_via_cli(root, vault)
        assert _run_cli(env, "supersede", "1", "--title", "ECS 部署地址（新）",
                        "--body", NEW_BODY, "--json").returncode == 0

        r = _run_cli(env, "search", "ECS", "--include-invalid", "--json")
        assert r.returncode == 0, r.stdout
        payload = json.loads(r.stdout)
        assert payload["include_invalid"] is True, payload
        by_title = {x["title"]: x for x in payload["results"]}
        assert "ECS 部署地址" in by_title, f"旧卡必须能取回：{list(by_title)}"
        assert by_title["ECS 部署地址"]["invalid_at"], by_title["ECS 部署地址"]
        # 默认检索里这个字段恒为空 —— 它只在自己人显式要看失效卡时才有内容
        r = _run_cli(env, "search", "ECS", "--json")
        assert all(x["invalid_at"] == "" for x in json.loads(r.stdout)["results"])


def test_cli_rejects_a_bad_as_of_instead_of_ignoring_it() -> None:
    """非法日期必须**明确报错**，不能静默当成「没传」。

    条目比较是拿日期当字符串比的，所以 ``--as-of 昨天`` 不会崩，而是安静地走成
    「存在性过滤形同不存在」—— 调用方拿到一批看起来正常的结果，
    却不知道日期条件根本没生效。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env, created = _seed_via_cli(root, vault)

        for bad in ("昨天", "2026-13-01", "2026/01/01", "not-a-date"):
            r = _run_cli(env, "search", "ECS", "--as-of", bad, "--json")
            assert r.returncode == 2, f"{bad!r} 应当被拒绝：{r.returncode} {r.stdout}"
            payload = json.loads(r.stdout)
            assert payload["ok"] is False, payload
            assert "as-of" in payload["error"], payload


def test_normalize_as_of_accepts_a_full_timestamp_and_truncates_to_day() -> None:
    """给完整时刻时只取日期部分 —— 「9 月 1 日那天」不等于「9 月 1 日零点」。"""
    assert search.normalize_as_of("2026-09-01T11:30:00.000Z") == "2026-09-01"
    assert search.normalize_as_of(" 2026-09-01 ") == "2026-09-01"
    assert search.normalize_as_of("") == ""
    assert search.normalize_as_of(None) == ""


# ------------------------------------------------------------------ 矩阵 #8


def test_cli_show_annotates_a_superseded_card_but_still_returns_the_text() -> None:
    """★ 矩阵 #8 + A2：旧卡全文照常返回，但**显式标注已被取代**。

    标注不只是给人看的：默认检索不会返回这张卡，所以打开它的人（或 agent）
    必须先看到「这不是当前事实」，否则会把历史当成现状用。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env, created = _seed_via_cli(root, vault)
        assert _run_cli(env, "supersede", "1", "--title", "ECS 部署地址（新）",
                        "--body", NEW_BODY, "--json").returncode == 0

        r = _run_cli(env, "show", "1", "--json")
        assert r.returncode == 0, r.stderr
        shown = json.loads(r.stdout)
        assert "10.0.0.1" in shown["text"], "旧卡原文必须照常返回"
        assert shown["invalid_at"], shown
        assert shown["superseded_by"], shown

        # 非 JSON 输出里也要有那行标注（人看到的才是最终产品）
        r = _run_cli(env, "show", "1")
        assert r.returncode == 0, r.stderr
        assert "已于" in r.stdout and "被" in r.stdout, r.stdout
        assert "10.0.0.1" in r.stdout, "标注不能顶掉正文"


def test_cli_show_does_not_annotate_a_live_card() -> None:
    """没失效的卡不该出现任何「已失效」字样 —— 否则标注会变成噪音。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env, created = _seed_via_cli(root, vault)

        r = _run_cli(env, "show", "1", "--json")
        payload = json.loads(r.stdout)
        assert payload["invalid_at"] == "", payload
        assert payload["superseded_by"] == "", payload

        r = _run_cli(env, "show", "1")
        assert "失效" not in r.stdout, r.stdout


# ------------------------------------------------------------------ 拒绝的路径


def test_cli_supersede_refuses_a_missing_id() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        vault.mkdir(parents=True)
        env = _cli_env(root, vault)
        assert _run_cli(env, "index", "--json").returncode == 0

        r = _run_cli(env, "supersede", "99", "--title", "新", "--body", NEW_BODY,
                     "--json")
        assert r.returncode == 1, f"{r.returncode} {r.stdout}"
        assert json.loads(r.stdout)["ok"] is False


def test_cli_supersede_refuses_to_chain() -> None:
    """已经失效的卡不能再被取代 —— 否则历史链会静默断掉。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env, created = _seed_via_cli(root, vault)
        assert _run_cli(env, "supersede", "1", "--title", "第二代",
                        "--body", NEW_BODY, "--json").returncode == 0

        r = _run_cli(env, "supersede", "1", "--title", "第三代",
                     "--body", "第三版正文，长度足够建卡并被索引。", "--json")
        assert r.returncode == 1, f"{r.returncode} {r.stdout}"
        payload = json.loads(r.stdout)
        assert payload["ok"] is False, payload
        assert "已被取代过" in payload["reason"], payload


def test_cli_supersede_requires_title_and_body() -> None:
    """两个参数都必填：取代会写出一张新卡，它的内容不该有含糊的默认值。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env, created = _seed_via_cli(root, vault)

        r = _run_cli(env, "supersede", "1", "--title", "只有标题")
        assert r.returncode == 2, f"缺 --body 应当被 argparse 拦下：{r.returncode}"

        r = _run_cli(env, "supersede", "1", "--body", NEW_BODY)
        assert r.returncode == 2, f"缺 --title 应当被 argparse 拦下：{r.returncode}"


def test_supersede_relations_survive_a_rebuild_via_cli() -> None:
    """关系存在 frontmatter（真相源）里，所以 ``index --rebuild`` 之后必须一模一样。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-supcli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env, created = _seed_via_cli(root, vault)
        assert _run_cli(env, "supersede", "1", "--title", "ECS 部署地址（新）",
                        "--body", NEW_BODY, "--json").returncode == 0

        assert _run_cli(env, "index", "--rebuild", "--json").returncode == 0

        conn = store.connect(root / "memory.db")
        try:
            row = conn.execute(
                "SELECT invalid_at, superseded_by FROM cards WHERE title = ?",
                ("ECS 部署地址",),
            ).fetchone()
            assert row is not None, "全量重建后旧卡应当还在索引里（文件还在磁盘上）"
            assert row["invalid_at"], row["invalid_at"]
            assert row["superseded_by"].endswith("ecs-部署地址-新.md"), row["superseded_by"]
            assert store.count_cards(conn) == 2
        finally:
            conn.close()

        # 默认检索仍然只见新卡（重建不能把可见性过滤弄丢）
        r = _run_cli(env, "search", "ECS", "--json")
        titles = [x["title"] for x in json.loads(r.stdout)["results"]]
        assert titles == ["ECS 部署地址（新）"], titles
