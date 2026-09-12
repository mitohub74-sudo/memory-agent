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


def test_cli_capture_reads_utf8_body_from_stdin() -> None:
    """管道喂进来的中文正文必须原样入库。

    这是一个真实踩过的坑：Windows 上 ``sys.stdin`` 默认按区域编码（中文系统是
    cp936）解码。只把 stdout/stderr 改成 UTF-8 是不够的 —— 写出去的干净、
    读进来的已经烂了，最后在 ``capture.write_card`` 里炸成
    ``UnicodeEncodeError: surrogates not allowed``。

    所以这里**用字节管道**喂 UTF-8，并逐字校验落盘内容，而不是只看退出码。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-cli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = {
            "MEMORY_AGENT_VAULT": str(vault),
            "MEMORY_AGENT_DB": str(root / "memory.db"),
        }
        title = "中文标题：ECS 登录方式"
        body = "正文含中文与符号：私钥 id_ed25519、别名 ecs-prod、「引号」与破折号——都要原样保留。"

        full_env = dict(os.environ)
        full_env.update(env)
        proc = subprocess.run(
            [sys.executable, str(MEMORY_PY), "capture",
             "--title", title, "--kind", "knowledge", "--json"],
            input=body.encode("utf-8"),
            capture_output=True,
            cwd=str(ROOT),
            env=full_env,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")

        payload = json.loads(proc.stdout.decode("utf-8"))
        assert payload["action"] == "created", payload
        assert payload["indexed"] is True, payload

        written = Path(payload["path"]).read_text(encoding="utf-8")
        assert title in written, "标题必须原样落盘，不能是乱码"
        assert body in written, "正文必须原样落盘，不能是乱码"

        # 落盘对了还不够：必须真的能搜到（写入即索引）
        r = _run(env, "search", "ecs-prod", "--json")
        assert r.returncode == 0, f"写入后应立刻可检索：{r.stdout} {r.stderr}"


def test_cli_capture_reports_title_collision() -> None:
    """CLI 端到端：标题撞车必须显式报出，且 reject 策略下不动盘。

    单元测试已覆盖 capture 层；这里再走一遍真实命令行，因为「store/capture 层对、
    CLI 层忘了传参数或吞掉返回值」是典型的分叉 —— 两层各自都「绿」，产品却是坏的。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-cli-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = {
            "MEMORY_AGENT_VAULT": str(vault),
            "MEMORY_AGENT_DB": str(root / "memory.db"),
        }
        title = "CLI撞车卡"
        first_body = "第一版正文，描述当前部署方式，长度足够建卡。"
        second_body = "第二版正文，部署方式已改变，与第一版不同。"

        # 首次写入：不该有冲突
        r = _run(env, "capture", "--title", title, "--body", first_body, "--json")
        assert r.returncode == 0, r.stderr
        first = json.loads(r.stdout)
        assert first["action"] == "created"
        assert "conflict" not in first, first

        # 同标题不同正文：默认另存，但必须回传冲突信息
        r = _run(env, "capture", "--title", title, "--body", second_body, "--json")
        assert r.returncode == 0, r.stderr
        second = json.loads(r.stdout)
        assert second.get("conflict") is True, second
        assert second["existing_path"] == first["path"], second
        assert second["collision_count"] == 1, second
        assert Path(second["path"]).name == "cli撞车卡-2.md", second

        # reject 策略：退出码 3（撞车是明确拒绝，不同于参数不合法的 1），且不写盘。
        # 正文必须 ≥ MIN_BODY_CHARS，否则会先被「正文过短」拦下（退出码 1）——
        # 长度校验在冲突检测之前，这里刻意给足长度，保证测的是撞车而不是长度。
        before = sorted(p.name for p in (vault / "03-Knowledge").glob("*.md"))
        r = _run(env, "capture", "--title", title,
                 "--body", "第三版正文，内容和前两版都不一样，用来验证拒绝策略。",
                 "--on-conflict", "reject", "--json")
        assert r.returncode == 3, f"撞车应以退出码 3 结束：{r.returncode} {r.stdout} {r.stderr}"
        rejected = json.loads(r.stdout)
        assert rejected["action"] == "conflict", rejected
        assert rejected["existing_path"] == first["path"], rejected

        after = sorted(p.name for p in (vault / "03-Knowledge").glob("*.md"))
        assert after == before, f"reject 不该产生新文件：{after}"


def test_access_stats_survive_index_rebuild_via_cli() -> None:
    """``index --rebuild`` 之后访问统计必须还在。

    ``card_stats`` 没有第二个来源 —— 它一旦跟着 rebuild 走，统计就是永久丢失。
    """
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
