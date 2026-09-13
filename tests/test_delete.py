# -*- coding: utf-8 -*-
"""软删 / 恢复 / 彻底删除（P3-02 的 S4，矩阵 #3–#4）。

用户已定的语义（2026-09-12，`待用户决定事项.md` A3）：

- 删除是**软删到 `.trash`，可恢复**；
- **不自动清理**，攒到一定量**提醒**用户清理；
- 真正的销毁只在 ``--purge``，那时才丢读取统计。

本文件钉死四件事，它们同时可观察才算成立：

1. 软删之后**再跑一次索引也搜不到**（`.trash` 被排除出索引）—— 否则卡片会换个路径
   复活，而 ``delete`` 返回的是「成功」；
2. 软删**保留读取统计**（``card_stats`` 是全项目唯一不可重建的数据）；
3. ``--restore`` 之后**重新可检索**；
4. ``--purge`` 只对**回收站里**的卡生效，且那时才清统计。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcore import capture, importer, search, store

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = ROOT / "memory.py"

GOOD_BODY = "这张卡的正文内容足够长，可以正常建卡并被索引到。"
DAY = 86400


# ------------------------------------------------------------------ 测试辅助


def _seed(root: Path, title: str = "待删卡",
          body: str = GOOD_BODY) -> tuple[Path, Path, str]:
    """建一张卡并索引。返回 (vault, 卡片路径, rel_path)。"""
    vault = root / "vault"
    card = capture.write_card(vault, title=title, kind="knowledge", body=body)
    assert card["ok"] is True, card
    path = Path(card["path"])
    return vault, path, path.resolve().relative_to(vault.resolve()).as_posix()


def _indexed(root: Path, vault: Path):
    conn = store.connect(root / "memory.db")
    store.init(conn)
    importer.sync(conn, vault)
    return conn


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


# ------------------------------------------------------------------ 软删：磁盘


def test_soft_delete_moves_the_file_and_keeps_it_intact() -> None:
    """★ 软删 = 移动，不是改写。回收站里的那一份必须与原文件逐字节相同。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root)
        before = path.read_bytes()

        res = capture.delete_card(vault, rel)
        assert res["ok"] is True, res
        assert res["trash_path"] == f".trash/{rel}", res

        assert not path.exists(), "原位置不该还有文件"
        trashed = vault / res["trash_path"]
        assert trashed.is_file()
        assert trashed.read_bytes() == before, "回收站里的内容必须与原文件完全一致"

        # 清单要记下删除时间（--older-than 唯一的判据来源）
        manifest = json.loads((vault / ".trash" / ".manifest.json").read_text("utf-8"))
        assert manifest[rel]["deleted_at"] == res["deleted_at"], manifest


def test_soft_delete_keeps_the_read_counts() -> None:
    """★ 软删**不清** ``card_stats`` —— 它是可恢复的，而统计不可重建。

    这条与「真删才清统计」是一对。若软删顺手把统计清了，恢复回来的卡片读取次数
    就永久归零，而且不会有任何报错。

    **注意它守的是哪一层**：这里手工调 ``store.delete_cards(keep_stats=True)``，
    所以它钉的是**那个函数参数本身**的语义；至于「CLI 有没有真的传 True」，
    由 ``test_cli_restore_makes_the_card_searchable_again`` 和
    ``test_cli_purge_clears_the_read_counts_it_left_behind`` 守着
    （变异测试验证过：把 CLI 那行改成 ``keep_stats=False``，这两条会红，本条不会）。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root)
        conn = _indexed(root, vault)
        try:
            assert store.record_access(conn, rel) == 1
            assert store.record_access(conn, rel) == 1
            assert store.get_access_stat(conn, rel)["access_count"] == 2

            assert capture.delete_card(vault, rel)["ok"] is True
            store.delete_cards(conn, [rel], keep_stats=True)
            store.commit(conn)

            assert store.count_cards(conn) == 0, "软删后索引里不该还有这张卡"
            stat = store.get_access_stat(conn, rel)
            assert stat is not None, "软删不该清掉读取统计"
            assert stat["access_count"] == 2, stat
        finally:
            conn.close()


def test_soft_deleted_card_does_not_reappear_after_a_full_reindex() -> None:
    """★ 矩阵 #3：软删之后**再跑一次索引**仍然搜不到它。

    这是 ``.trash`` 排除的端到端证据。没有它，卡片会在下次索引时以
    ``.trash/...`` 这个新路径复活，而所有命令都返回成功 ——
    删除变成了一次静默的「搬家」。增量与全量重建都要验，因为两条路径都会扫 vault。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root, body="删掉之后就搜不到了 zzgone，正文足够长。")
        conn = _indexed(root, vault)
        try:
            s = search.KeywordSearcher(conn)
            assert s.search("zzgone", limit=5), "删之前应当搜得到"
            assert capture.delete_card(vault, rel)["ok"] is True
            store.delete_cards(conn, [rel], keep_stats=True)
            store.commit(conn)

            importer.sync(conn, vault)
            assert s.search("zzgone", limit=5) == [], "增量索引后不该复活"

            importer.sync(conn, vault, rebuild=True)
            assert store.count_cards(conn) == 0, "全量重建后回收站里的卡不该被扫进来"
            assert s.search("zzgone", limit=5) == [], "全量重建后不该复活"
        finally:
            conn.close()


def test_second_delete_does_not_overwrite_the_first_trash_copy() -> None:
    """同一路径被删两次时**不覆盖**回收站里的那一份，而是另存一个有序名字。

    真实路径：删一张 → 之后原地又出现一张同路径的卡（人手工写的、别的工具写的）→ 再删。
    直接 ``os.replace`` 会**静默销毁**回收站里那一份 —— 而回收站的全部意义就是它还在。

    第二张卡是**直接写到同一个路径**的，不走 ``capture``：走 capture 的话撞车策略会
    把它另存成 ``同名卡-2.md``，于是根本不会撞上同一个 rel_path（这条测试就测不到东西了）。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root, title="同名卡",
                                 body="第一版正文内容，长度足够建卡并被索引到。")
        first = path.read_bytes()

        assert capture.delete_card(vault, rel)["ok"] is True

        # 同一个路径上又出现了一张卡（内容不同）
        second_body = "第二版正文内容，内容不同，长度同样足够建卡。"
        path.write_text(f"---\ntitle: 同名卡\nkind: knowledge\n---\n\n{second_body}\n",
                        encoding="utf-8")

        second = capture.delete_card(vault, rel)
        assert second["ok"] is True, second
        assert second["trash_path"] != f".trash/{rel}", second
        assert second["trash_path"] == ".trash/03-Knowledge/同名卡-2.md", second

        # 第一份必须在原来的位置、内容一字不差
        assert (vault / ".trash" / rel).read_bytes() == first, "最早那一份被覆盖了"
        # 第二份也在，内容就是第二版
        assert second_body in (vault / second["trash_path"]).read_text(encoding="utf-8")

        # 两份都在回收站里（都能被列出来）
        assert len(capture.list_trash(vault)) == 2, capture.list_trash(vault)


def test_delete_refuses_a_card_already_in_the_trash() -> None:
    """重复软删会把回收站里的文件再套一层 ``.trash``，必须拒绝并指路。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root)
        assert capture.delete_card(vault, rel)["ok"] is True

        res = capture.delete_card(vault, f".trash/{rel}")
        assert res["ok"] is False, res
        assert "已经在回收站里" in res["reason"], res
        assert "--purge" in res["reason"], res


def test_delete_refuses_a_path_outside_the_vault() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        vault = Path(raw) / "vault"
        vault.mkdir(parents=True)
        outside = Path(raw) / "外面的.md"
        outside.write_text("---\ntitle: 外面的\nkind: knowledge\n---\n\n正文。\n",
                           encoding="utf-8")

        res = capture.delete_card(vault, "../外面的.md")
        assert res["ok"] is False, res
        assert "不在 vault 内" in res["reason"], res
        assert outside.exists()


def test_delete_refuses_a_missing_card() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        vault = Path(raw) / "vault"
        res = capture.delete_card(vault, "03-Knowledge/不存在.md")
        assert res["ok"] is False, res
        assert "不存在" in res["reason"], res


# ------------------------------------------------------------------ 恢复


def test_restore_puts_the_card_back_and_search_hits_it_again() -> None:
    """★ 矩阵 #4：恢复之后文件回原位、**重新可检索**、读取统计接得上。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root, body="恢复之后要能搜到 zzback，正文足够长。")
        conn = _indexed(root, vault)
        try:
            assert store.record_access(conn, rel) == 1
            assert capture.delete_card(vault, rel)["ok"] is True
            store.delete_cards(conn, [rel], keep_stats=True)
            store.commit(conn)
            assert search.KeywordSearcher(conn).search("zzback", limit=5) == []

            res = capture.restore_card(vault, rel)
            assert res["ok"] is True, res
            assert path.is_file(), "文件必须回到原位"
            assert not (vault / ".trash" / rel).exists()

            importer.sync_one(conn, vault, res["path"])
            store.commit(conn)
            hits = search.KeywordSearcher(conn).search("zzback", limit=5)
            assert hits, "恢复之后必须重新可检索"
            assert store.get_access_stat(conn, rel)["access_count"] == 1, "统计应当接上"
        finally:
            conn.close()


def test_restore_refuses_to_overwrite_an_existing_card() -> None:
    """目标位置已有卡时默认拒绝 —— 覆盖是不可逆的丢失，不做「看起来成功」的顶替。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root, title="占位卡", body="回收站里那一版正文，长度足够建卡并被索引。")
        assert capture.delete_card(vault, rel)["ok"] is True

        occupant = capture.write_card(vault, title="占位卡", kind="knowledge",
                                     body="后来写的那一版正文，内容不同，长度足够建卡。")
        assert occupant["ok"] is True, occupant
        occupant_path = Path(occupant["path"])
        occupant_text = occupant_path.read_text(encoding="utf-8")

        res = capture.restore_card(vault, rel)
        assert res["ok"] is False, res
        assert "拒绝覆盖" in res["reason"], res
        assert occupant_path.read_text(encoding="utf-8") == occupant_text
        assert (vault / ".trash" / rel).exists(), "被拒绝时回收站里的那一份不能动"


def test_restore_force_moves_the_occupant_into_the_trash_too() -> None:
    """``--force`` 也不销毁任何内容：占位的那张先被移进回收站，再恢复目标。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root, title="占位卡", body="回收站里那一版正文，长度足够建卡并被索引。")
        assert capture.delete_card(vault, rel)["ok"] is True
        occupant = capture.write_card(vault, title="占位卡", kind="knowledge",
                                     body="后来写的那一版正文，内容不同，长度足够建卡。")
        occupant_path = Path(occupant["path"])
        occupant_text = occupant_path.read_text(encoding="utf-8")

        res = capture.restore_card(vault, rel, force=True)
        assert res["ok"] is True, res
        assert res.get("displaced"), res
        assert (vault / res["displaced"]).read_text(encoding="utf-8") == occupant_text, (
            "被顶替的那张必须完整地进回收站，不能丢"
        )
        assert path.is_file()


def test_purge_refuses_an_absolute_path_outside_the_vault() -> None:
    """★ 绝对路径必须被拒绝 —— ``pathlib`` 会让绝对路径**整个取代**左边的 base。

    ``Path(".trash") / "C:/…/随便什么.exe"`` 得到的就是那个绝对路径本身，
    于是限界一旦缺失，``purge_card`` 会去删一个 vault 之外的文件。
    这不是理论问题：写这条测试时它是真实存在的缺陷（当时用的是
    ``Path(rel).as_posix().lstrip("./")``，而 ``lstrip`` 按字符集剥，
    会把 ``.trash/x`` 剥成 ``trash/x``）。

    所以这一条测的不是「错误信息好不好看」，而是**回收站只会删回收站里的东西**。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault = root / "vault"
        vault.mkdir(parents=True)

        outside = root / "不归回收站管的卡.md"
        outside.write_text("---\ntitle: 外面的卡\nkind: knowledge\n---\n\n正文足够长。\n",
                           encoding="utf-8")

        res = capture.purge_card(vault, outside.as_posix())
        assert res["ok"] is False, res
        assert "不在回收站内" in res["reason"], res
        assert outside.is_file(), "★ vault 之外的文件被删掉了 —— 限界失效"

        res = capture.restore_card(vault, outside.as_posix())
        assert res["ok"] is False, res
        assert "不在回收站内" in res["reason"], res


def test_trash_helpers_refuse_traversal_paths() -> None:
    """``../`` 穿越同样要被挡住（``.trash/../../x`` 解析后不在 .trash 内）。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault = root / "vault"
        outside = root / "外面.md"
        outside.write_text("---\ntitle: 外面\nkind: knowledge\n---\n\n正文足够长。\n",
                          encoding="utf-8")

        for bad in ("../外面.md", ".trash/../../外面.md", ""):
            res = capture.purge_card(vault, bad)
            assert res["ok"] is False, (bad, res)
        assert outside.is_file()

        # 顺带确认误加的 .trash/ 前缀是真的被剥掉、而不是被按字符集削平
        from mcore.capture import _normalize_rel
        assert _normalize_rel(".trash/03-Knowledge/x.md") == "03-Knowledge/x.md"
        assert _normalize_rel("././a.md") == "a.md"


def test_restore_reports_a_missing_trash_entry() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        vault = Path(raw) / "vault"
        res = capture.restore_card(vault, "03-Knowledge/不在回收站.md")
        assert res["ok"] is False, res
        assert "回收站里没有" in res["reason"], res


def test_restore_accepts_a_full_trash_path() -> None:
    """误传 ``.trash/...`` 前缀是很常见的，剥掉它比报错好（无歧义）。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root)
        assert capture.delete_card(vault, rel)["ok"] is True

        res = capture.restore_card(vault, f".trash/{rel}")
        assert res["ok"] is True, res
        assert res["rel_path"] == rel, res
        assert path.is_file()


# ------------------------------------------------------------------ 彻底删除


def test_purge_refuses_a_live_card() -> None:
    """★ ``--purge`` **只对回收站里的内容生效**。

    这不是靠调用方自觉，而是这个函数唯一能删的位置就是 ``.trash`` ——
    于是「purge 一张活着的卡」在机制上不可能发生。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root)

        res = capture.purge_card(vault, rel)
        assert res["ok"] is False, res
        assert "活着的卡片" in res["reason"], res
        assert "先 delete" in res["reason"], res
        assert path.is_file(), "被拒绝时文件必须还在"


def test_purge_removes_the_trashed_file() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root)
        assert capture.delete_card(vault, rel)["ok"] is True
        trashed = vault / ".trash" / rel
        assert trashed.is_file()

        res = capture.purge_card(vault, rel)
        assert res["ok"] is True, res
        assert not trashed.exists(), "彻底删除后文件不该还在"
        # 空的中间目录要被收掉，但 .trash 本身留着
        assert not (vault / ".trash" / "03-Knowledge").exists()
        assert (vault / ".trash").is_dir()
        assert capture.list_trash(vault) == []


def test_purge_trash_requires_an_explicit_condition() -> None:
    """没有条件就不动手 —— 那等于「清空回收站」，不该由一次误调用触发。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root)
        assert capture.delete_card(vault, rel)["ok"] is True

        res = capture.purge_trash(vault)
        assert res["ok"] is False, res
        assert "必须给条件" in res["reason"], res
        assert (vault / ".trash" / rel).exists(), "被拒绝时不该删任何东西"


def test_purge_trash_older_than_keeps_recent_entries() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root)
        assert capture.delete_card(vault, rel)["ok"] is True

        res = capture.purge_trash(vault, older_seconds=30 * DAY)
        assert res["ok"] is True, res
        assert res["count"] == 0, res
        assert (vault / ".trash" / rel).exists(), "还没到期的不该被删"


def test_purge_trash_older_than_deletes_expired_entries() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root)
        assert capture.delete_card(vault, rel)["ok"] is True

        # 把「现在」推到 60 天之后，于是这张卡算是 60 天前删的
        res = capture.purge_trash(vault, older_seconds=30 * DAY,
                                  now_ts=time.time() + 60 * DAY)
        assert res["ok"] is True, res
        assert res["count"] == 1, res
        assert not (vault / ".trash" / rel).exists()


def test_purge_trash_skips_entries_without_a_delete_time() -> None:
    """★ 没有删除时间记录的条目在 ``--older-than`` 下**跳过并报出**，绝不猜。

    手工放进 ``.trash`` 的文件（或清单丢了）没有删除时间。把它当成「很久以前删的」
    会直接删掉一个来路不明的文件 —— 不可逆，所以这里的取向是「宁可不清，也不误清」。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root)
        # 手工放一个没有清单记录的文件进回收站
        manual = vault / ".trash" / "03-Knowledge" / "手工放进来.md"
        manual.parent.mkdir(parents=True, exist_ok=True)
        manual.write_text("---\ntitle: 手工放进来\nkind: knowledge\n---\n\n正文足够长。\n",
                          encoding="utf-8")

        res = capture.purge_trash(vault, older_seconds=1, now_ts=time.time() + 999 * DAY)
        assert res["ok"] is True, res
        assert res["count"] == 0, res
        assert len(res["skipped"]) == 1, res
        assert "没有这张卡的删除时间" in res["skipped"][0]["skip_reason"], res
        assert manual.is_file(), "来路不明的文件不该被删掉"


def test_purge_trash_all_includes_entries_without_a_delete_time() -> None:
    """``--all`` 是明确的「清空」意图，不再拿时间当判据，于是全部清掉。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root)
        assert capture.delete_card(vault, rel)["ok"] is True
        manual = vault / ".trash" / "03-Knowledge" / "手工放进来.md"
        manual.parent.mkdir(parents=True, exist_ok=True)
        manual.write_text("---\ntitle: 手工放进来\nkind: knowledge\n---\n\n正文足够长。\n",
                          encoding="utf-8")

        res = capture.purge_trash(vault, everything=True)
        assert res["ok"] is True, res
        assert res["count"] == 2, res
        assert capture.list_trash(vault) == []


def test_trash_summary_counts_entries_and_flags_unknown_times() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault, path, rel = _seed(root)
        assert capture.trash_summary(vault) == {"count": 0, "unknown_deleted_at": 0,
                                                "bytes": 0}
        assert capture.delete_card(vault, rel)["ok"] is True
        summary = capture.trash_summary(vault)
        assert summary["count"] == 1, summary
        assert summary["bytes"] > 0, summary

        manual = vault / ".trash" / "03-Knowledge" / "手工.md"
        manual.write_text("x" * 10, encoding="utf-8")
        assert capture.trash_summary(vault)["unknown_deleted_at"] == 1


# ------------------------------------------------------------------ CLI 端到端


def test_cli_soft_delete_then_reindex_and_search_finds_nothing() -> None:
    """★ 矩阵 #3 走**真实命令行**：删 → **再跑一次 index** → 搜不到。

    中间那次 ``index`` 是关键：没有它，测试只证明了「我们顺手摘掉了索引里的那一行」，
    证明不了「下次全量同步不会把它捞回来」。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = _cli_env(root, vault)

        r = _run_cli(env, "capture", "--title", "CLI待删卡", "--body",
                     "这条正文里有 zzcligone，长度足够建卡。", "--json")
        assert r.returncode == 0, r.stderr
        created = json.loads(r.stdout)

        r = _run_cli(env, "search", "zzcligone", "--json")
        assert r.returncode == 0, r.stdout

        r = _run_cli(env, "delete", "1", "--json")
        assert r.returncode == 0, f"{r.stdout} {r.stderr}"
        deleted = json.loads(r.stdout)
        assert deleted["ok"] is True, deleted
        assert deleted["indexed"] is True, deleted
        assert deleted["trash_path"] == f".trash/03-Knowledge/{Path(created['path']).name}", deleted

        # 文件确实不在原位了，在回收站里
        assert not Path(created["path"]).exists()
        assert (vault / deleted["trash_path"]).is_file()

        r = _run_cli(env, "index", "--json")
        assert r.returncode == 0, r.stderr
        counts = json.loads(r.stdout)
        assert counts["inserted"] == 0, f"回收站里的卡被当成新卡扫进来了：{counts}"

        r = _run_cli(env, "search", "zzcligone", "--json")
        assert r.returncode == 1, f"删掉之后不该还能搜到：{r.stdout}"
        assert json.loads(r.stdout)["count"] == 0, r.stdout


def test_cli_restore_makes_the_card_searchable_again() -> None:
    """★ 矩阵 #4 走真实命令行：恢复 → 搜得到，且读取统计还在。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = _cli_env(root, vault)

        r = _run_cli(env, "capture", "--title", "CLI恢复卡", "--body",
                     "这条正文里有 zzcliback，长度足够建卡。", "--json")
        assert r.returncode == 0, r.stderr
        created = json.loads(r.stdout)
        rel = f"03-Knowledge/{Path(created['path']).name}"

        # 读一次全文，制造一条统计
        assert _run_cli(env, "show", "1", "--json").returncode == 0

        assert _run_cli(env, "delete", "1", "--json").returncode == 0
        assert _run_cli(env, "search", "zzcliback", "--json").returncode == 1

        r = _run_cli(env, "delete", "--restore", rel, "--json")
        assert r.returncode == 0, f"{r.stdout} {r.stderr}"
        restored = json.loads(r.stdout)
        assert restored["ok"] is True and restored["indexed"] is True, restored

        assert Path(created["path"]).is_file(), "文件必须回到原位"
        r = _run_cli(env, "search", "zzcliback", "--json")
        assert r.returncode == 0, f"恢复之后必须重新可检索：{r.stdout} {r.stderr}"

        # 软删没清统计，恢复后接得上
        r = _run_cli(env, "show", "1", "--json")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["access_count"] == 2, json.loads(r.stdout)


def test_cli_purge_clears_the_read_counts_it_left_behind() -> None:
    """★ ``--purge`` 才清统计 —— 而且必须真的清掉，不能留幽灵行。

    软删时索引行已经摘掉了，此时 ``delete_cards`` 会因为「找不到这一行」而跳过，
    它内部的统计清理也就不会执行。所以真删必须**额外**清一次统计，
    否则 ``card_stats`` 里会留下永远读不出来的幽灵行。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = _cli_env(root, vault)

        r = _run_cli(env, "capture", "--title", "CLI彻底删卡", "--body", GOOD_BODY,
                     "--json")
        assert r.returncode == 0, r.stderr
        rel = f"03-Knowledge/{Path(json.loads(r.stdout)['path']).name}"

        assert _run_cli(env, "show", "1", "--json").returncode == 0
        assert _run_cli(env, "delete", "1", "--json").returncode == 0

        # 软删之后统计还在（可恢复）
        db = root / "memory.db"
        conn = store.connect(db)
        try:
            assert store.get_access_stat(conn, rel) is not None, "软删不该清统计"
        finally:
            conn.close()

        r = _run_cli(env, "delete", "--purge", rel, "--json")
        assert r.returncode == 0, f"{r.stdout} {r.stderr}"
        purged = json.loads(r.stdout)
        assert purged["ok"] is True, purged
        assert purged["stats_dropped"] == 1, purged

        conn = store.connect(db)
        try:
            assert store.get_access_stat(conn, rel) is None, "真删必须把统计一起清掉"
        finally:
            conn.close()


def test_cli_purge_without_condition_explains_what_is_missing() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = _cli_env(root, vault)
        assert _run_cli(env, "capture", "--title", "占位", "--body", GOOD_BODY,
                        "--json").returncode == 0

        r = _run_cli(env, "delete", "--purge", "--json")
        assert r.returncode == 1, f"{r.returncode} {r.stdout}"
        payload = json.loads(r.stdout)
        assert payload["ok"] is False, payload
        assert "必须给条件" in payload["error"], payload

        # 错误的时长写法也要被明确拒绝，而不是被当成 0 天
        r = _run_cli(env, "delete", "--purge", "--older-than", "30days", "--json")
        assert r.returncode == 1, f"{r.returncode} {r.stdout}"
        assert "无法识别的时长" in json.loads(r.stdout)["error"], r.stdout


def test_cli_stats_shows_the_trash_and_the_reminder_appears_over_threshold() -> None:
    """A3 的「不自动清理、攒到一定量提醒」：stats 显示回收站，delete 超阈值给提醒。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-del-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = _cli_env(root, vault)

        # 攒够阈值 - 1 张（用库层直接删，省去 9 次子进程），再走 CLI 删第 10 张
        threshold = capture.TRASH_REMIND_THRESHOLD
        for i in range(threshold - 1):
            card = capture.write_card(vault, title=f"攒卡{i}", kind="knowledge",
                                      body=GOOD_BODY)
            rel = Path(card["path"]).resolve().relative_to(vault.resolve()).as_posix()
            assert capture.delete_card(vault, rel)["ok"] is True

        assert _run_cli(env, "index", "--json").returncode == 0
        r = _run_cli(env, "capture", "--title", "第 N 张", "--body", GOOD_BODY,
                     "--json")
        assert r.returncode == 0, r.stderr

        r = _run_cli(env, "stats", "--json")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["trash"]["count"] == threshold - 1, r.stdout

        r = _run_cli(env, "delete", "1", "--json")
        assert r.returncode == 0, f"{r.stdout} {r.stderr}"
        payload = json.loads(r.stdout)
        assert payload["trash_count"] == threshold, payload
        assert "reminder" in payload, payload
        assert "--purge --older-than" in payload["reminder"], payload

        # 回收站**没有被自动清理** —— 提醒归提醒，动手要人来
        assert capture.trash_summary(vault)["count"] == threshold
