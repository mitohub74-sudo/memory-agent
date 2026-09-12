# -*- coding: utf-8 -*-
"""CLI 级端到端测试：访问统计必须跨 `index --rebuild` 存活。

pytest 里已经直接测过 store 层（见 test_store.py）。这里再走一遍**真实命令行**，
理由是：store 层正确、CLI 层忘记调用，是很典型的分叉 —— 两者都「绿」但产品是坏的。
真实验收只能用真实入口跑出来。

子进程全程使用隔离的 vault / db，**不触碰调用者真实的记忆库**。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = ROOT / "memory.py"


def _run(env: dict, *argv: str) -> subprocess.CompletedProcess:
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


def test_access_stats_survive_index_rebuild_via_cli() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-cli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        (vault / "03-Knowledge").mkdir(parents=True)
        (vault / "03-Knowledge" / "CLI统计卡.md").write_text(
            "---\ntitle: CLI统计卡\nkind: knowledge\n---\n\n命令行端到端测试正文。\n",
            encoding="utf-8",
        )
        env = {
            "MEMORY_AGENT_VAULT": str(vault),
            "MEMORY_AGENT_DB": str(root / "memory.db"),
        }

        # 1) 建索引
        r = _run(env, "index", "--json")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["inserted"] == 1

        # 2) 取全文两次 —— 这才是「访问」，检索召回不算
        for _ in range(2):
            r = _run(env, "show", "1", "--json")
            assert r.returncode == 0, r.stderr
        shown = json.loads(r.stdout)
        assert shown["access_count"] == 2, shown

        # 3) 全量重建索引
        r = _run(env, "index", "--rebuild", "--json")
        assert r.returncode == 0, r.stderr

        # 4) 统计必须还在 —— 这是「重建不清 card_stats」的端到端证据
        r = _run(env, "stats", "--json")
        assert r.returncode == 0, r.stderr
        stats = json.loads(r.stdout)
        assert stats["total"] == 1, "重建后卡片应还在"
        most = stats["most_accessed"]
        assert len(most) == 1, most
        assert most[0]["rel_path"] == "03-Knowledge/CLI统计卡.md"
        assert most[0]["access_count"] == 2, most

        # 5) 重建后 FTS 也要能用（否则「统计留下了但搜不到」是更糟的静默故障）
        r = _run(env, "search", "命令行", "--json")
        assert r.returncode == 0, f"重建后应能检索到：{r.stdout} {r.stderr}"
