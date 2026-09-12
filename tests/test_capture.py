# -*- coding: utf-8 -*-
"""采集端冲突语义测试（P3-03）。

要防的是**静默**：同一个标题、不同的正文，过去会悄悄多出一个 `slug-2.md`，
调用方只看到「写好了」，却不知道库里已经有两张同标题的卡 —— 以后检索会同时
命中两张，而没有任何信息能判断该信哪张。

这里验证四件事：

1. 撞车被**显式回传**（`conflict` / `existing_path` / `collision_paths`）；
2. 撞车时**不覆盖**任何已有卡片；
3. `reject` 策略下不写盘，且调用方能拿到「在跟谁撞车」；
4. 幂等不能被撞车逻辑破坏 —— 生成过 `-2` 之后，原内容再写一次仍须是 `unchanged`。
   这一条最关键：它保证新逻辑没有把「重复写入」退化成「每次加一个序号」。

只用标准库，保持「零依赖直跑 + pytest」双模可用。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from mcore import capture

BODY_A = "第一版正文，描述服务器当前的部署方式与端口。"
BODY_B = "第二版正文，部署方式已经改变，端口也换了。"
BODY_C = "第三版正文，完全是另一件事，只是标题恰好相同。"


def _vault(raw: str) -> Path:
    return Path(raw) / "vault"


def test_collision_is_reported_and_nothing_is_overwritten() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-conflict-") as raw:
        vault = _vault(raw)

        first = capture.write_card(vault, title="撞车测试卡", body=BODY_A)
        assert first["ok"] is True
        assert first["action"] == "created"
        assert "conflict" not in first, "首次写入不该报冲突"

        second = capture.write_card(vault, title="撞车测试卡", body=BODY_B)

        # 兼容：默认策略仍然是另存，不覆盖
        assert second["ok"] is True
        assert second["action"] == "created"
        assert second["path"] != first["path"]

        # 但必须显式说出来
        assert second["conflict"] is True
        assert second["existing_path"] == first["path"]
        assert second["collision_count"] == 1
        assert second["collision_paths"] == [first["path"]]
        assert "note" in second

        files = sorted(p.name for p in (vault / "03-Knowledge").glob("*.md"))
        assert files == ["撞车测试卡-2.md", "撞车测试卡.md"], files

        # 原卡内容未被改动 —— 这是「不抄/不覆盖用户已有数据」的底线
        original = Path(first["path"]).read_text(encoding="utf-8")
        assert BODY_A in original
        assert BODY_B not in original


def test_reject_policy_writes_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-conflict-") as raw:
        vault = _vault(raw)

        first = capture.write_card(vault, title="拒绝策略卡", body=BODY_A)
        before = sorted(p.name for p in (vault / "03-Knowledge").glob("*.md"))

        rejected = capture.write_card(vault, title="拒绝策略卡", body=BODY_B,
                                      on_conflict="reject")

        assert rejected["ok"] is False
        assert rejected["action"] == "conflict"
        # 调用方要能知道「在跟谁撞车」—— 这就是 existing_path 的用途
        assert rejected["existing_path"] == first["path"]
        assert rejected["collision_paths"] == [first["path"]]
        assert rejected["path"] == "", "拒绝时不该报告一个写入路径"

        after = sorted(p.name for p in (vault / "03-Knowledge").glob("*.md"))
        assert after == before, "reject 策略必须一个字节都不写"


def test_idempotency_survives_after_a_collision() -> None:
    """撞车产生 -2 之后，原内容再写一次必须仍是 unchanged。

    这是最容易写错的地方：如果扫描逻辑「看到第一个内容不同的就停」，
    那么 foo.md 正文不同、foo-2.md 恰是重复内容时，就会又生成 foo-3.md。
    重复累积是静默的，所以必须钉死。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-conflict-") as raw:
        vault = _vault(raw)

        a = capture.write_card(vault, title="幂等撞车卡", body=BODY_A)
        b = capture.write_card(vault, title="幂等撞车卡", body=BODY_B)
        assert b["conflict"] is True

        # 再写 A：命中第一张（基名），unchanged
        again_a = capture.write_card(vault, title="幂等撞车卡", body=BODY_A)
        assert again_a["action"] == "unchanged", again_a
        assert again_a["path"] == a["path"]

        # 再写 B：命中第二张（-2），仍是 unchanged，**不能**再生成 -3
        again_b = capture.write_card(vault, title="幂等撞车卡", body=BODY_B)
        assert again_b["action"] == "unchanged", again_b
        assert again_b["path"] == b["path"]

        files = sorted(p.name for p in (vault / "03-Knowledge").glob("*.md"))
        assert files == ["幂等撞车卡-2.md", "幂等撞车卡.md"], files


def test_three_way_collision_counts_all() -> None:
    """第三次不同正文应被记为撞车 2 张，并落到 -3。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-conflict-") as raw:
        vault = _vault(raw)

        capture.write_card(vault, title="三向卡", body=BODY_A)
        capture.write_card(vault, title="三向卡", body=BODY_B)
        third = capture.write_card(vault, title="三向卡", body=BODY_C)

        assert third["conflict"] is True
        assert third["collision_count"] == 2
        assert len(third["collision_paths"]) == 2
        assert Path(third["path"]).name == "三向卡-3.md"


def test_unknown_policy_is_rejected_not_silently_ignored() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-conflict-") as raw:
        vault = _vault(raw)
        bad = capture.write_card(vault, title="策略校验卡", body=BODY_A,
                                 on_conflict="whatever")
        assert bad["ok"] is False
        assert bad["action"] == "rejected"
        assert "on_conflict" in bad["reason"]

        # 非法策略不该留下任何文件
        assert not (vault / "03-Knowledge").exists() or not list(
            (vault / "03-Knowledge").glob("*.md")
        )


def test_similar_titles_that_differ_in_slug_are_not_conflicts() -> None:
    """只有 slug 相同才算撞车。标题不同 -> 不同卡片，不该误报。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-conflict-") as raw:
        vault = _vault(raw)

        capture.write_card(vault, title="部署方式 A", body=BODY_A)
        other = capture.write_card(vault, title="部署方式 B", body=BODY_B)

        assert other["action"] == "created"
        assert "conflict" not in other, "不同 slug 不该被判为撞车"
        assert Path(other["path"]).name == "部署方式-b.md"


def test_existing_card_without_hash_marker_is_still_matched() -> None:
    """兼容手工写的旧卡：没有 contentHash 标记，但正文一致时仍按幂等处理。

    这条锁住的是「判据 2」——按同一标题重算指纹。若哪天有人把这一步改成
    「从文件里解析标题再算」，既有卡片的幂等匹配会静默失效。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-conflict-") as raw:
        vault = _vault(raw)
        directory = vault / "03-Knowledge"
        directory.mkdir(parents=True)

        # 手工造一张卡：结构同采集端，但**不带** contentHash 标记
        (directory / "手工卡.md").write_text(
            "---\ntitle: 手工卡\nkind: knowledge\n---\n\n" + BODY_A + "\n",
            encoding="utf-8",
        )

        result = capture.write_card(vault, title="手工卡", body=BODY_A)
        assert result["action"] == "unchanged", result
        assert Path(result["path"]).name == "手工卡.md"
        assert len(list(directory.glob("*.md"))) == 1, "不该产生第二张卡"
