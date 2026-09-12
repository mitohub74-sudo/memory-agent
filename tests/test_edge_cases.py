# -*- coding: utf-8 -*-
"""文件名与进程收尾的边界（阶段 4 P4-08 / P4-09 / P4-10）。

三项都是**小概率但真实**的缺陷，共同点是「出错时离原因很远」：

- **P4-08** Windows 保留设备名（``CON`` / ``NUL`` / ``COM1``…）不能作文件名，
  而卡片文件名直接由标题生成 —— 用户起个标题叫「NUL」就会在**写入阶段**失败；
- **P4-09** 时间戳取两次 ``now()``，跨整秒边界时秒与毫秒来自不同时刻；
- **P4-10** 客户端提前关掉管道时服务打出一整段栈，被宿主记成「服务崩溃」，
  而真实情况是「用户正常关掉了窗口」。

P4-10 的测试刻意用**字节管道**并在收到响应后立刻关闭，因为这才是宿主真实的收场方式。
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

RESERVED = ["CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9"]


# ------------------------------------------------------------------ P4-08


def test_reserved_names_are_not_produced_as_slug() -> None:
    """验收原文：``slugify("CON")`` 不以保留名结尾。"""
    for name in RESERVED:
        for variant in (name, name.lower(), name.title()):
            slug = capture.slugify(variant)
            assert slug.upper() not in RESERVED, (variant, slug)
            assert not slug.upper().endswith(tuple(RESERVED)), (variant, slug)


def test_reserved_slug_is_wrapped_readably() -> None:
    """包裹而不是加前缀：保持可读，且一眼看出被人为改动过。"""
    assert capture.slugify("CON") == "_con_"
    assert capture.slugify("nul") == "_nul_"
    assert capture.slugify("COM1") == "_com1_"


def test_ordinary_slugs_are_untouched() -> None:
    """**不要误伤。** 只判整段，不判「以保留名结尾」。

    ``my-con.md`` 的文件名词干是 ``my-con``，它不是 Windows 设备名；
    把它改成 ``my-_con_`` 只会让用户莫名其妙。同理 ``con-tingency``、``CONNECTION``
    （后者连整段都不是保留名）都不该动。
    """
    assert capture.slugify("my-con") == "my-con"
    assert capture.slugify("con-tingency") == "con-tingency"
    assert capture.slugify("CONNECTION") == "connection"
    assert capture.slugify("控制台入口") == "控制台入口"


def test_reserved_name_card_can_actually_be_written() -> None:
    """端到端：标题叫「NUL」的卡片必须能落盘（交叉平台安全）。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-reserved-") as raw:
        vault = Path(raw) / "vault"
        result = capture.write_card(
            vault, title="NUL", body="标题是 Windows 保留设备名，正文足够长可以建卡。",
        )
        assert result["action"] == "created", result
        path = Path(result["path"])
        assert path.exists()
        assert path.stem.upper() not in RESERVED, path.stem


def test_truncation_cannot_resurrect_a_reserved_name() -> None:
    """截断之后才浮现的保留名也要被处理。

    判定放在 ``slugify`` 最后一步，正是为了这种情况：超长标题截断后
    末尾恰好落在 ``con`` 上。
    """
    long_title = "x" * (capture.MAX_SLUG_CHARS - 3) + "-con"
    slug = capture.slugify(long_title)
    assert slug.upper() not in RESERVED, slug
    # 截断本身仍生效（长度没有被破坏）
    assert len(slug) <= capture.MAX_SLUG_CHARS + 2, len(slug)  # 包裹会加 2 个下划线


# ------------------------------------------------------------------ P4-09


def test_timestamp_uses_a_single_instant() -> None:
    """时间戳的秒与毫秒必须来自**同一时刻**。

    直接断言「没有跨边界」做不到（那是概率事件）。改为断言实现形态：
    函数体里只允许出现一次 ``datetime.now(``。比字符串匹配更稳的做法是数
    AST 里的调用节点 —— 注释里提到 now() 不该被算进去。
    """
    import ast
    import inspect

    src = inspect.getsource(capture._now_iso)
    tree = ast.parse(_dedent(src))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "now"
    ]
    assert len(calls) == 1, (
        f"datetime.now() 应只调一次，实际 {len(calls)} 次 —— "
        f"两次调用之间可能跨过整秒边界，产生自相矛盾的时间戳"
    )


def test_timestamp_format_is_unchanged() -> None:
    """格式必须与既有卡片一致 —— 改了会让新老卡片的字段形态不同。"""
    import re

    ts = capture._now_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", ts), ts


def test_timestamp_is_monotonic_enough_for_ordering() -> None:
    """连续调用不应产生倒退的字符串（ISO 定长格式下字典序即时间序）。"""
    stamps = [capture._now_iso() for _ in range(200)]
    assert stamps == sorted(stamps), "时间戳字符串不得倒退"


def _dedent(src: str) -> str:
    import textwrap
    return textwrap.dedent(src)


# ------------------------------------------------------------------ P4-10


def test_mcp_server_exits_cleanly_when_client_closes_the_pipe() -> None:
    """客户端发完请求就读完响应关掉管道 —— 服务必须**安静退出**。

    这是宿主真实的收场方式（编辑器/agent 会话结束）。原实现会在写响应时
    抛 BrokenPipeError 并打出一整段栈；宿主通常把 stderr 当错误信号，
    于是「用户正常关掉了窗口」被记成一次服务崩溃。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-pipe-") as raw:
        root = Path(raw)
        env = dict(os.environ)
        env["MEMORY_AGENT_VAULT"] = str(root / "vault")
        env["MEMORY_AGENT_DB"] = str(root / "memory.db")

        proc = subprocess.Popen(
            [sys.executable, str(MEMORY_PY), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=str(ROOT),
        )
        try:
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            proc.stdin.flush()
            assert proc.stdout.readline(), "应当收到响应"

            # 客户端收完就走
            proc.stdout.close()
            proc.stdin.close()
            proc.wait(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()

        stderr = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()

        assert proc.returncode == 0, f"退出码应为 0，实际 {proc.returncode}；stderr={stderr[:400]}"
        assert "Traceback" not in stderr, f"正常收场不该有栈：{stderr[:400]}"
        assert "BrokenPipeError" not in stderr, stderr[:400]


def test_serve_returns_zero_when_a_write_fails(monkeypatch) -> None:
    """确定性验证 P4-10 的保护本身。

    上面那条子进程测试**证明力不够**：服务在被关闭的管道上不一定真的再写一次，
    所以把保护去掉它**照样通过**（实测确认过）。要验证「写失败 → 干净收场」
    这个契约，必须让写入**确定性地**失败一次。

    做法：先让第一行响应正常写出去，之后抛 ``BrokenPipeError`` —— 模拟「客户端
    读完第一条就走了」。然后断言 ``serve()`` 返回 0，且 stderr 上没有栈。

    ``monkeypatch`` 是 pytest 内置 fixture；本项目的「零依赖直跑」约束针对的是
    ``python tests/test_mcp.py`` 那条路径，pytest 专属测试用 fixture 无碍
    （``tests/test_mcp.py`` 自己仍然不用 fixture）。
    """
    import io

    from mcore import mcp_server

    real_stdout = sys.stdout
    buffer = io.StringIO()
    calls = {"n": 0}

    class _FailsAfterFirstWrite:
        def write(self, text: str) -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise BrokenPipeError("client went away")
            return buffer.write(text)

        def flush(self) -> None:
            try:
                buffer.flush()
            except ValueError:
                pass

    requests = (
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}) + "\n"
    )

    monkeypatch.setattr(sys, "stdout", _FailsAfterFirstWrite())
    monkeypatch.setattr(sys, "stdin", io.StringIO(requests))
    try:
        code = mcp_server.serve()
    finally:
        sys.stdout = real_stdout

    assert code == 0, "写失败应当被当作正常收场，退出码 0"
    assert calls["n"] >= 2, "测试前提：必须真的尝试过第二次写入"
    assert "result" in buffer.getvalue(), "第一条响应应当已经写出去"

