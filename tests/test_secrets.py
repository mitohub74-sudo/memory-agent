# -*- coding: utf-8 -*-
"""敏感内容告警（P3-06）。

这一项的取舍是**既定规格**，不是随手定的：默认只告警、不阻断。
理由：本库的正当用途包含渗透测试记录，硬拒会把项目本身的用途一起拒掉。
需要更严的场合由调用方显式开 ``reject_secrets``。

因此这里验证的重点有三个，缺一不可：

1. **默认不阻断** —— 含私钥头也能写入（这是验收条件原文）；
2. **告警确实产生** —— 否则「不阻断」就等于「什么都没做」；
3. **告警不复述凭据** —— 把密钥抄进告警里等于又写了一遍到返回值与日志里，
   反而扩大暴露面。

另外专门钉住**误报边界**：本库的正文里天然会出现「密钥见 ~/.ssh/xxx」这类
引用式写法，以及 ``id_ed25519`` 这类文件名 —— 把它们判成凭据会让告警迅速失去意义，
最后被所有人忽略。

测试里用拼接构造密钥样式字符串：既避免本文件自身被扫描器命中，
也让读代码的人一眼看出那是**假值**。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcore import capture

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = ROOT / "memory.py"

# 用拼接避免源码里出现完整的密钥字面量
FAKE_PEM_HEADER = "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_GITHUB_TOKEN = "ghp_" + "A" * 24
FAKE_ASSIGNMENT = "password = " + "hunter2hunter2"

BODY_WITH_SECRET = (
    f"把这台机器的登录方式记一下：\n{FAKE_PEM_HEADER}\n"
    "MIIEowIBAAKCAQEA（示例内容，非真实密钥）\n"
    f"另外 AWS 的 Key 是 {FAKE_AWS_KEY}，用的时候注意。"
)
BODY_REFERENCE_ONLY = (
    "示例主机使用密钥登录，密钥位于 ~/.ssh/id_ed25519_example，"
    "SSH 别名为 example-host。库里只记路径，不记密钥本身。"
)


# ------------------------------------------------------------------ 单元层


def test_private_key_header_is_detected() -> None:
    findings = capture.scan_secrets(BODY_WITH_SECRET)
    kinds = {f["kind"] for f in findings}
    assert "私钥头" in kinds, findings
    assert "AWS Access Key ID" in kinds, findings


def test_reference_style_body_is_not_flagged() -> None:
    """引用式写法不是凭据 —— 这是最重要的误报边界。

    项目的工具描述里就是要求 agent 写「密钥见 ~/.ssh/xxx」，如果这被判成凭据，
    告警会立刻变成噪音。
    """
    assert capture.scan_secrets(BODY_REFERENCE_ONLY) == []


def test_typical_false_positives_are_not_flagged() -> None:
    for text in (
        "使用 id_ed25519 密钥登录，路径 ~/.ssh/id_ed25519。",
        "token: 见上一条记录",
        "api_key: 见 config.json",
        "secret: 空",
        "password: 已改成用密钥登录",
        "这里的 private key 指的是概念，不是内容",
    ):
        assert capture.scan_secrets(text) == [], text


def test_scan_reports_positions_but_not_the_secret() -> None:
    findings = capture.scan_secrets(BODY_WITH_SECRET)
    assert findings, "应当命中"
    for f in findings:
        assert set(f) == {"kind", "span"}
        start, end = f["span"]
        assert 0 <= start < end <= len(BODY_WITH_SECRET)
    # 结构上就不含原文：没有 value / text / match 之类的字段
    assert all("value" not in f and "text" not in f and "match" not in f for f in findings)


def test_default_only_warns_and_still_writes() -> None:
    """验收条件原文：含私钥头的 body 返回 warnings，但**仍写入**。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-secret-") as raw:
        vault = Path(raw) / "vault"
        result = capture.write_card(vault, title="含密钥的记录", body=BODY_WITH_SECRET)

        assert result["ok"] is True, result
        assert result["action"] == "created", result
        assert result["secrets_found"], result
        assert result["warnings"], result
        assert Path(result["path"]).exists(), "默认策略下必须真的落盘"

        # 告警文案不得复述凭据
        for w in result["warnings"]:
            assert FAKE_AWS_KEY not in w
            assert FAKE_PEM_HEADER not in w
            assert "疑似凭据" in w


def test_reject_secrets_flag_refuses_without_writing() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-secret-") as raw:
        vault = Path(raw) / "vault"
        result = capture.write_card(vault, title="含密钥的记录", body=BODY_WITH_SECRET,
                                    reject_secrets=True)

        assert result["ok"] is False
        assert result["action"] == "rejected"
        assert result["secrets_found"]
        # 拒绝文案同样不得复述凭据
        assert FAKE_AWS_KEY not in result["reason"]

        directory = vault / "03-Knowledge"
        assert not directory.exists() or not list(directory.glob("*.md")), \
            "reject_secrets 下不该留下任何卡片文件"


def test_reject_secrets_does_not_block_clean_content() -> None:
    with tempfile.TemporaryDirectory(prefix="memory-agent-secret-") as raw:
        vault = Path(raw) / "vault"
        result = capture.write_card(vault, title="干净内容", body=BODY_REFERENCE_ONLY,
                                    reject_secrets=True)
        assert result["ok"] is True, result
        assert "warnings" not in result


def test_assignment_style_credentials_are_detected() -> None:
    findings = capture.scan_secrets(FAKE_ASSIGNMENT)
    assert any(f["kind"] == "疑似明文凭据赋值" for f in findings), findings


def test_secret_in_title_is_also_scanned() -> None:
    """有人会把密钥塞在标题里，而标题同样会被索引与检索。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-secret-") as raw:
        vault = Path(raw) / "vault"
        result = capture.write_card(
            vault, title=f"服务器 {FAKE_GITHUB_TOKEN} 的登录",
            body=BODY_REFERENCE_ONLY,   # 正文干净，只有标题有问题
        )
        assert result["secrets_found"], result
        assert any("GitHub" in k for k in result["secrets_found"]), result


def test_warning_survives_idempotent_rewrite() -> None:
    """重复写入同一条含密钥内容：仍应带告警，而不是因为 unchanged 就把风险吞掉。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-secret-") as raw:
        vault = Path(raw) / "vault"
        capture.write_card(vault, title="含密钥的记录", body=BODY_WITH_SECRET)
        again = capture.write_card(vault, title="含密钥的记录", body=BODY_WITH_SECRET)

        assert again["action"] == "unchanged"
        assert again["secrets_found"], again
        assert again["warnings"], again


# ------------------------------------------------------------------ CLI 层


def _run(env: dict, *argv: str) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(
        [sys.executable, str(MEMORY_PY), *argv],
        capture_output=True, text=True, encoding="utf-8",
        env=full_env, cwd=str(ROOT),
    )


def test_cli_warns_by_default_and_rejects_with_flag() -> None:
    """真实命令行两条路径都要走一遍：默认告警但写入；--reject-secrets 拒绝且退出码 1。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-secret-") as raw:
        root = Path(raw)
        vault = root / "vault"
        env = {
            "MEMORY_AGENT_VAULT": str(vault),
            "MEMORY_AGENT_DB": str(root / "memory.db"),
        }

        # 默认：写入成功，且**返回值里**带告警。
        # 注意 --json 模式下走的是 JSON 出口，告警在 JSON 的 warnings 字段里，
        # 不在 stderr 上 —— 一开始把断言写错在 stderr，是测试的问题不是实现的。
        r = _run(env, "capture", "--title", "命令行含密钥卡", "--body", BODY_WITH_SECRET, "--json")
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        assert payload["action"] == "created", payload
        assert payload["indexed"] is True, payload
        assert payload["secrets_found"], payload
        assert payload["warnings"], payload

        # 非 JSON 模式下告警走 stderr（stdout 只放结果本身）
        r = _run(env, "capture", "--title", "命令行含密钥卡三", "--body", BODY_WITH_SECRET)
        assert r.returncode == 0, r.stderr
        assert "警告" in r.stderr, r.stderr
        assert "警告" not in r.stdout, "stdout 应只放结果"

        # 开开关：拒绝写入，退出码 1（参数/内容被拒，不是撞车的 3）
        r = _run(env, "capture", "--title", "命令行含密钥卡二", "--body", BODY_WITH_SECRET,
                 "--reject-secrets", "--json")
        assert r.returncode == 1, f"{r.returncode} {r.stdout} {r.stderr}"
        rejected = json.loads(r.stdout)
        assert rejected["action"] == "rejected", rejected

        # 只有前两张卡落盘（第三张被 --reject-secrets 拒掉）
        files = sorted(p.name for p in vault.rglob("*.md"))
        assert len(files) == 2, files
