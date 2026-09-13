# -*- coding: utf-8 -*-
"""原地修改（update）语义（P3-02 / 矩阵 #1–#2）。

「更新」与「取代」是两件事，选错会让历史静默消失：

- **事实本身写错了**（错别字、漏了参数）→ ``update``：没有「当时是对的」这回事；
- **事实变了**（服务迁址、端口换了）→ ``supersede``：「当时是多少」以后还要能回答。

本文件把三件事钉死，它们同时可观察才算功能成立：

1. 改完之后**检索命中的是新内容**（只改文件不改索引 = 假成功）；
2. ``created`` **不变**、``updated`` **刷新**、**路径不变**（路径是取代与统计的锚点）；
3. 拒绝改 ``kind``，且**文件没有被移动**（这是矩阵 #2 要求的「明确失败」）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcore import capture, importer, search, store

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = ROOT / "memory.py"

GOOD_BODY = "这张卡的正文内容足够长，可以正常建卡并被索引到。"


# ------------------------------------------------------------------ 测试辅助


def _seed(root: Path, title: str = "部署信息", body: str = GOOD_BODY) -> tuple[Path, dict]:
    vault = root / "vault"
    card = capture.write_card(vault, title=title, kind="project", body=body)
    assert card["ok"] is True, card
    return vault, card


def _rel(vault: Path, path: str) -> str:
    return Path(path).resolve().relative_to(vault.resolve()).as_posix()


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
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=full_env,
        cwd=str(ROOT),
    )


def _cli_env(root: Path, vault: Path) -> dict:
    return {
        "MEMORY_AGENT_VAULT": str(vault),
        "MEMORY_AGENT_DB": str(root / "memory.db"),
    }


# ------------------------------------------------------------------ 落盘效果


def test_update_body_keeps_created_and_refreshes_updated() -> None:
    """★ 矩阵 #1：``created`` 不变、``updated`` 刷新、**路径不变**。

    先把两个时间戳都改成一个人造旧值，再更新 —— 这样断言不依赖「两次调用之间
    真的跨过了 1 毫秒」（毫秒分辨率的时钟下那是概率性的，会变成偶发红灯）。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        root = Path(raw)
        vault, card = _seed(root)
        path = Path(card["path"])

        # 人造一个「很久以前」的时间戳，作为「created 必须原样保留」的参照物
        old_ts = "2000-01-01T00:00:00.000Z"
        assert capture.update_frontmatter(
            path, {"created": old_ts, "updated": old_ts}
        ) is True

        before_meta, _ = importer.parse_frontmatter(path.read_text(encoding="utf-8"))
        assert before_meta["created"] == old_ts, before_meta

        res = capture.update_card(
            vault, _rel(vault, card["path"]),
            body="部署已经迁到新机房，正文内容整段换掉，长度足够。",
        )
        assert res["ok"] is True, res
        assert res["changed"] == ["body"], res

        after_text = path.read_text(encoding="utf-8")
        meta, body = importer.parse_frontmatter(after_text)
        assert meta["created"] == old_ts, "created 必须一个字都不动"
        assert meta["updated"] != old_ts, "updated 必须被刷新"
        assert "部署已经迁到新机房" in body
        assert res["path"] == card["path"], "路径是锚点，update 不许移动文件"


def test_update_preserves_unknown_fields_and_other_lines() -> None:
    """只动给定的键：别人写的字段、正文里的分隔符都不能被抹掉。

    卡片可能是人手写的、可能带本项目不知道的字段。整份重渲染会把它们抹掉 ——
    那是不可逆的损失，而且不会报错。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        vault = Path(raw) / "vault"
        directory = vault / "03-Knowledge"
        directory.mkdir(parents=True)
        card = directory / "手工卡.md"
        card.write_text(
            "---\n"
            "title: 手工卡\n"
            "kind: knowledge\n"
            "我自己加的字段: 别弄丢我\n"
            "tags: [a, b]\n"
            "---\n\n"
            "正文里有 --- 这样的分隔符，还有别的内容，长度足够建卡。\n",
            encoding="utf-8",
        )

        res = capture.update_card(vault, "03-Knowledge/手工卡.md", title="手工卡（改）")
        assert res["ok"] is True, res

        text = card.read_text(encoding="utf-8")
        assert "我自己加的字段: 别弄丢我" in text, "未知字段必须保留"
        assert "title: 手工卡（改）" in text
        assert "正文里有 --- 这样的分隔符" in text, "正文必须保留"
        assert card.name == "手工卡.md", "改标题不该改文件名"


def test_update_with_no_actual_change_writes_nothing() -> None:
    """给定值与现值相同 → 不写盘、不刷 updated，并如实回传 ``changed: []``。

    这是「不做能返回的假成功」的另一面：**也不做假动作**。
    为了一个没变的值重写文件，会让 mtime 与 updated 无端漂移，
    而调用方会据此以为「刚刚改过这张卡」。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        root = Path(raw)
        vault, card = _seed(root, title="没变化的卡")
        path = Path(card["path"])
        before = path.read_text(encoding="utf-8")

        res = capture.update_card(vault, _rel(vault, card["path"]),
                                 title="没变化的卡", body=GOOD_BODY)
        assert res["ok"] is True, res
        assert res["changed"] == [], res
        assert path.read_text(encoding="utf-8") == before, "没有变化就不该动文件"

        # 一个字段都没给，也不该被当成「改成功」
        res = capture.update_card(vault, _rel(vault, card["path"]))
        assert res["ok"] is True, res
        assert res["changed"] == [], res
        assert path.read_text(encoding="utf-8") == before


def test_update_rejects_a_body_below_the_capture_threshold() -> None:
    """正文长度门槛必须与 capture **同一道**。

    两处门槛不同的后果是「同一份内容换个入口就能进来」，而两个入口都返回成功 ——
    这正是本项目反复吃过的「同一个意思、几处各写」的亏。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        root = Path(raw)
        vault, card = _seed(root)
        path = Path(card["path"])
        before = path.read_text(encoding="utf-8")

        too_short = "太短"
        # 先证明 capture 会拒绝同一段正文 —— 这样「门槛一致」才是被验证的事实，
        # 而不是把同一个魔数抄了两遍。
        rejected = capture.write_card(vault, title="另一张卡", body=too_short)
        assert rejected["ok"] is False, rejected

        res = capture.update_card(vault, _rel(vault, card["path"]), body=too_short)
        assert res["ok"] is False, res
        assert path.read_text(encoding="utf-8") == before, "被拒绝时不许落盘"


def test_update_recomputes_the_fingerprint_so_recapture_stays_idempotent() -> None:
    """★ 指纹必须按新标题 + 新正文重算。

    去重判据是「文件里有没有 ``contentHash: <新指纹>``」。只改正文不重算标记的
    后果是**静默的**：内容换了、判据还指着旧值，于是同一份内容再写一次会被当成
    新内容，库里悄悄多出一张重复卡。

    这里刻意**只改正文、不改标题**：去重是按文件名的 slug 找同族文件的，
    而 update 不移动文件（路径是锚点）。标题改了而文件名没改时，
    用新标题再 capture 会落到另一个 slug 空间里 —— 那是「改标题不动文件」的
    已知代价，写在 ``update_card`` 的说明里，不在这里假装不存在。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        root = Path(raw)
        vault = root / "vault"
        title = "部署信息"
        new_body = "这是改过之后的正文内容，长度足够建卡并被索引。"
        card = capture.write_card(vault, title=title, kind="project", body=GOOD_BODY)
        rel = _rel(vault, card["path"])

        res = capture.update_card(vault, rel, body=new_body)
        assert res["ok"] is True, res
        assert res["changed"] == ["body"], res

        expected = capture.card_fingerprint(title, new_body)
        text = Path(card["path"]).read_text(encoding="utf-8")
        assert f"contentHash: {expected}" in text, "指纹没有按新内容重算"

        # 端到端证据：用同样的标题 + 正文再 capture 一次，必须被认成重复
        again = capture.write_card(vault, title=title, kind="project", body=new_body)
        assert again["action"] == "unchanged", f"重复写入没有被识别：{again}"


def test_update_of_title_recomputes_the_fingerprint_for_the_new_title() -> None:
    """改标题时指纹也要跟着新标题算 —— 否则下一次幂等比对必然失配。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        root = Path(raw)
        vault = root / "vault"
        new_title = "部署信息（新）"
        new_body = "这是改过之后的正文内容，长度足够建卡并被索引。"
        card = capture.write_card(vault, title="部署信息", kind="project",
                                 body=GOOD_BODY)

        res = capture.update_card(vault, _rel(vault, card["path"]),
                                 title=new_title, body=new_body)
        assert res["ok"] is True, res
        assert set(res["changed"]) == {"title", "body"}, res

        expected = capture.card_fingerprint(new_title, new_body)
        text = Path(card["path"]).read_text(encoding="utf-8")
        assert f"contentHash: {expected}" in text, "指纹没有按新标题重算"
        # 文件名不变是刻意的（路径是锚点），但必须回传出来，别让调用方以为卡片丢了
        assert Path(card["path"]).name == "部署信息.md", Path(card["path"]).name
        assert "文件名保持不变" in res.get("note", ""), res


# ------------------------------------------------------------------ 拒绝的路径


def test_update_refuses_kind_change_and_does_not_move_the_file() -> None:
    """★ 矩阵 #2：改 ``kind`` 必须**明确报错**，且文件没有被移动。

    刻意的不支持。静默忽略 ``kind`` 比报错更坏 —— 调用方会以为改成了，
    然后一直找不到那张卡。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        root = Path(raw)
        vault, card = _seed(root)  # kind=project → 02-Projects
        path = Path(card["path"])
        before = path.read_text(encoding="utf-8")

        res = capture.update_card(vault, _rel(vault, card["path"]), kind="knowledge")
        assert res["ok"] is False, res
        assert "不支持改类型" in res["reason"], res
        # 报错要说清「怎么才能做到」，否则调用方只会反复重试同一个参数
        assert "supersede" in res["reason"], res

        assert path.exists(), "被拒绝时文件不能动"
        assert path.read_text(encoding="utf-8") == before
        assert not (vault / "03-Knowledge").exists(), "不许产生新目录（文件被搬走过会留下它）"


def test_update_accepts_kind_when_it_equals_the_current_one() -> None:
    """传了**相同**的 kind 不算「改类型」—— 否则调用方没法写幂等的更新脚本。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        root = Path(raw)
        vault, card = _seed(root)
        res = capture.update_card(vault, _rel(vault, card["path"]),
                                 kind="project", title="部署信息（改名）")
        assert res["ok"] is True, res
        assert res["changed"] == ["title"], res


def test_update_of_a_card_without_frontmatter_is_rejected() -> None:
    """没有 frontmatter 的文件不硬塞字段 —— 半成品格式比「改不了」更糟。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        vault = Path(raw) / "vault"
        directory = vault / "03-Knowledge"
        directory.mkdir(parents=True)
        card = directory / "无元数据.md"
        card.write_text("只有正文，没有 frontmatter 段。\n", encoding="utf-8")

        res = capture.update_card(vault, "03-Knowledge/无元数据.md", body=GOOD_BODY)
        assert res["ok"] is False, res
        assert "frontmatter" in res["reason"], res
        assert card.read_text(encoding="utf-8") == "只有正文，没有 frontmatter 段。\n"


def test_update_refuses_paths_outside_the_vault() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        vault = Path(raw) / "vault"
        vault.mkdir(parents=True)
        outside = Path(raw) / "外面的文件.md"
        outside.write_text("---\ntitle: 外面的\nkind: knowledge\n---\n\n正文。\n",
                           encoding="utf-8")

        res = capture.update_card(vault, "../外面的文件.md", title="改名了")
        assert res["ok"] is False, res
        assert "不在 vault 内" in res["reason"], res
        assert outside.read_text(encoding="utf-8").startswith("---\ntitle: 外面的\n")


def test_update_rejects_a_missing_card() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        vault = Path(raw) / "vault"
        res = capture.update_card(vault, "03-Knowledge/不存在.md", title="新")
        assert res["ok"] is False, res
        assert "不存在" in res["reason"], res


# ------------------------------------------------------------------ 检索可见性


def test_updated_body_is_what_search_finds() -> None:
    """★ 矩阵 #1 的检索侧：改完之后搜到的必须是新内容，旧内容搜不到。

    只看文件内容是不够的 —— 文件改了、索引没同步，两边都不报错，
    而调用方搜到的还是旧答案。那是典型的静默假成功。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        root = Path(raw)
        vault, card = _seed(root, title="检索用卡", body="旧标记 zzoldmarker 在这里，正文足够长。")
        conn = _indexed(root, vault)
        try:
            s = search.KeywordSearcher(conn)
            assert s.search("zzoldmarker", limit=5), "改之前应当能搜到旧标记"

            res = capture.update_card(
                vault, _rel(vault, card["path"]),
                body="新标记 zznewmarker 在这里，正文同样足够长。",
            )
            assert res["ok"] is True, res

            # 模拟 CLI 的收尾动作：写盘之后同步本卡
            importer.sync_one(conn, vault, res["path"])
            store.commit(conn)

            assert s.search("zznewmarker", limit=5), "改之后必须能搜到新标记"
            assert s.search("zzoldmarker", limit=5) == [], "旧标记不该还能搜到"
        finally:
            conn.close()


# ------------------------------------------------------------------ CLI 端到端


def test_cli_update_makes_new_body_searchable_and_old_body_gone() -> None:
    """★ 真实命令行端到端：``update`` 之后 ``search`` 命中新正文、搜不到旧正文。

    store/capture 层全绿、CLI 层忘了同步索引，是本项目最典型的分叉形态 ——
    两层各自都「绿」，产品却是坏的。所以矩阵 #1 必须用真实入口跑一遍。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = _cli_env(root, vault)

        r = _run_cli(env, "capture", "--title", "CLI更新卡", "--kind", "knowledge",
                     "--body", "旧标记 zzoldcli 在正文里，长度足够建卡。", "--json")
        assert r.returncode == 0, r.stderr
        created = json.loads(r.stdout)
        assert created["indexed"] is True, created
        card_id = 1

        r = _run_cli(env, "search", "zzoldcli", "--json")
        assert r.returncode == 0, f"改之前应当搜得到：{r.stdout} {r.stderr}"

        r = _run_cli(env, "update", str(card_id),
                     "--body", "新标记 zznewcli 在正文里，长度同样足够。", "--json")
        assert r.returncode == 0, f"{r.stdout} {r.stderr}"
        payload = json.loads(r.stdout)
        assert payload["ok"] is True, payload
        assert payload["changed"] == ["body"], payload
        assert payload["indexed"] is True, payload

        # 磁盘上确实是新内容
        text = Path(payload["path"]).read_text(encoding="utf-8")
        assert "zznewcli" in text and "zzoldcli" not in text, text

        # 检索侧：新标记命中，旧标记不再命中
        r = _run_cli(env, "search", "zznewcli", "--json")
        assert r.returncode == 0, f"改之后必须搜得到新正文：{r.stdout} {r.stderr}"
        assert json.loads(r.stdout)["count"] == 1, r.stdout

        r = _run_cli(env, "search", "zzoldcli", "--json")
        assert r.returncode == 1, f"旧正文不该还能搜到：{r.stdout} {r.stderr}"
        assert json.loads(r.stdout)["count"] == 0, r.stdout

        # created 没被改掉（从索引侧核对，证明同步的是同一张卡）
        r = _run_cli(env, "show", str(card_id), "--json")
        assert r.returncode == 0, r.stderr
        shown = json.loads(r.stdout)
        # show 给的是 rel_path，capture 给的是绝对路径 —— 比文件名而不是比整串。
        assert Path(shown["path"]).name == Path(created["path"]).name, shown["path"]


def test_cli_update_kind_change_fails_loudly_and_moves_nothing() -> None:
    """★ 矩阵 #2 的 CLI 侧：报错、退出码非 0、文件原位不动。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = _cli_env(root, vault)

        r = _run_cli(env, "capture", "--title", "CLI类型卡", "--kind", "project",
                     "--body", GOOD_BODY, "--json")
        assert r.returncode == 0, r.stderr
        created = json.loads(r.stdout)
        path = Path(created["path"])
        before = path.read_text(encoding="utf-8")

        r = _run_cli(env, "update", "1", "--kind", "knowledge", "--json")
        assert r.returncode == 1, f"改类型应当明确失败：{r.returncode} {r.stdout}"
        payload = json.loads(r.stdout)
        assert payload["ok"] is False, payload
        assert "不支持改类型" in payload["reason"], payload

        assert path.exists() and path.read_text(encoding="utf-8") == before
        assert not (vault / "03-Knowledge").exists(), "文件被搬走过会在目标目录留下痕迹"


def test_cli_update_without_fields_reports_no_change_and_does_not_reindex() -> None:
    """没给任何字段 → ``changed: []`` 且 ``indexed: null``。

    ``indexed`` 刻意返回 null 而不是 false：false 表示「写盘成功但索引失败」，
    把「什么都没改」也说成 false 会让调用方以为索引坏了。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = _cli_env(root, vault)

        r = _run_cli(env, "capture", "--title", "CLI空更新卡", "--body", GOOD_BODY,
                     "--json")
        assert r.returncode == 0, r.stderr
        path = Path(json.loads(r.stdout)["path"])
        before = path.read_text(encoding="utf-8")

        r = _run_cli(env, "update", "1", "--json")
        assert r.returncode == 0, f"{r.stdout} {r.stderr}"
        payload = json.loads(r.stdout)
        assert payload["ok"] is True, payload
        assert payload["changed"] == [], payload
        assert payload["indexed"] is None, payload
        assert path.read_text(encoding="utf-8") == before


def test_cli_update_missing_id_fails_with_clear_error() -> None:
    """id 不存在 → 明确的失败，不是「改成功」。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-upd-") as raw:
        root = Path(raw)
        vault = root / "vault"
        vault.mkdir(parents=True)  # index 需要 vault 目录存在
        env = _cli_env(root, vault)

        assert _run_cli(env, "index", "--json").returncode == 0
        r = _run_cli(env, "update", "999", "--title", "随便", "--json")
        assert r.returncode == 1, f"{r.returncode} {r.stdout}"
        payload = json.loads(r.stdout)
        assert payload["ok"] is False, payload
        assert "找不到 id=999" in payload["error"], payload
