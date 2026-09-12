# -*- coding: utf-8 -*-
"""tags 解析单一来源（阶段 4 P4-04）。

合并前的真实不一致（不是假想的洁癖）：

- ``importer`` 与 ``mcp_server`` 对「已经是 list」的输入**直接放行**，
  跳过了 strip / 去空 / 去重；
- 而 CLI 走的是字符串路径，会被规范化。

于是同一批标签走两个入口得到两种结果 —— 前端数组 ``[" a ", "", "a"]``
会带着空格、空项、重复项落进 frontmatter，而 CLI 的 ``"a, a"`` 却是干净的。
两边都返回「成功」，所以谁也不会发现。这正是本项目吃过两次亏的模式
（P1 的 CLI/MCP capture 分叉、P3-04 的读取窗口），故收口到 ``util.parse_tags``。

测试分三段：`parse_tags` 自身语义、**两个入口得到同一结果**、以及结构护栏
（没有第二处再写一份）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcore import capture, importer, store
from mcore.util import parse_tags

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = ROOT / "memory.py"


# ------------------------------------------------------------------ 自身语义


def test_roadmap_cases() -> None:
    """P4-04 验收里点名的三种形状。"""
    assert parse_tags("a, b") == ["a", "b"]
    assert parse_tags(["a", "b"]) == ["a", "b"]
    assert parse_tags("") == []


def test_empty_and_none_inputs() -> None:
    for raw in (None, "", "   ", ",", ",,", [], (), set()):
        assert parse_tags(raw) == [], repr(raw)


def test_whitespace_is_trimmed_on_every_path() -> None:
    """这就是合并前 list 路径漏掉的一步。"""
    assert parse_tags([" a ", "b\t", "  c"]) == ["a", "b", "c"]
    assert parse_tags(" a , b ,  c ") == ["a", "b", "c"]
    assert parse_tags([" a ", "", "   ", "a"]) == ["a"]


def test_duplicates_are_dropped_while_order_is_kept() -> None:
    """去重但**保序**：标签先后是作者写的顺序，重排会让 diff 无端变化。"""
    assert parse_tags("b, a, b, c, a") == ["b", "a", "c"]
    assert parse_tags(["b", "a", "b"]) == ["b", "a"]


def test_json_array_string_from_a_client_is_handled() -> None:
    """MCP 客户端把数组序列化成字符串发过来（实际发生过）。"""
    got = parse_tags('["a","b"]')
    # 方括号与引号会被当作标签内容的一部分，但**不丢信息、不抛错**：
    # 这不是「正确解析 JSON」，而是「尽力而为」。真正解析 JSON 需要
    # 判断字符串是不是 JSON —— 那是猜测，而猜错会把普通标签搞坏。
    assert got == ['["a"', '"b"]'], got


def test_non_string_elements_are_coerced_not_dropped() -> None:
    assert parse_tags([1, 2]) == ["1", "2"]
    assert parse_tags(5) == ["5"]


def test_result_is_always_a_fresh_list() -> None:
    """返回新列表，调用方改动不会污染输入或后续调用。"""
    src = ["a", "b"]
    out = parse_tags(src)
    out.append("c")
    assert src == ["a", "b"]
    assert parse_tags(src) == ["a", "b"]


# ------------------------------------------------------------------ 两个入口必须一致


def _write_card(vault: Path, name: str, tags_line: str) -> None:
    directory = vault / "03-Knowledge"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        f"---\ntitle: {name}\nkind: knowledge\ntags: {tags_line}\n---\n\n测试正文内容足够长。\n",
        encoding="utf-8",
    )


def test_frontmatter_and_cli_agree_on_the_same_tags() -> None:
    """同一个标签集合，走 frontmatter 与走 CLI 必须落成同一个列表。

    这是合并的核心收益：两侧由同一份代码决定，所以**不依赖它们恰好同源**。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-tags-") as raw:
        root = Path(raw)
        vault = root / "vault"
        # frontmatter 的内联数组写法（importer 路径）
        _write_card(vault, "由frontmatter写入.md", "[a, b, a]")

        conn = store.connect(root / "memory.db")
        try:
            store.init(conn)
            importer.sync(conn, vault)
            row = conn.execute(
                "SELECT tags FROM cards WHERE rel_path = ?",
                ("03-Knowledge/由frontmatter写入.md",),
            ).fetchone()
            from_frontmatter = json.loads(row["tags"])
        finally:
            conn.close()

        # CLI 路径：--tags 传字符串
        env = dict(os.environ)
        env["MEMORY_AGENT_VAULT"] = str(vault)
        env["MEMORY_AGENT_DB"] = str(root / "memory.db")
        proc = subprocess.run(
            [sys.executable, str(MEMORY_PY), "capture",
             "--title", "由CLI写入",
             "--body", "命令行写入的正文内容，长度必须不少于二十个字符才可能建卡。",
             "--tags", "a, b, a", "--json"],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT),
        )
        assert proc.returncode == 0, proc.stderr

        conn = store.connect(root / "memory.db")
        try:
            row = conn.execute(
                "SELECT tags FROM cards WHERE rel_path LIKE '%由cli写入%'"
            ).fetchone()
            assert row is not None, "CLI 写入应已落库"
            from_cli = json.loads(row["tags"])
        finally:
            conn.close()

        assert from_frontmatter == from_cli == ["a", "b"], (from_frontmatter, from_cli)


def test_mcp_capture_accepts_both_string_and_array() -> None:
    """MCP 参数既可能是数组（标准）也可能是字符串（客户端差异），结果必须一致。"""
    with tempfile.TemporaryDirectory(prefix="memory-agent-tags-") as raw:
        vault = Path(raw) / "vault"
        body = "正文内容必须足够长，因为过短的正文会被 capture 直接拒绝（下限二十个字符）。"
        as_array = capture.write_card(vault, title="数组标签", body=body,
                                      tags=parse_tags(["p", "q", "p"]))
        as_string = capture.write_card(vault, title="字符串标签", body=body,
                                       tags=parse_tags("p, q, p"))
        assert as_array["action"] == as_string["action"] == "created", (as_array, as_string)

        texts = [Path(as_array["path"]).read_text(encoding="utf-8"),
                 Path(as_string["path"]).read_text(encoding="utf-8")]
        for text in texts:
            assert "tags: [p, q]" in text, text[:200]


# ------------------------------------------------------------------ 结构护栏


def test_tag_parsing_exists_in_exactly_one_module() -> None:
    """除 util.py 外，不应再有第二处 tags 解析实现。

    两条判据，缺一不可：

    1. ``def parse_tags`` 定义在 util.py 之外；
    2. 「按逗号切分 + strip 出列表」的表达式，**且该处代码/上下文提到 tags**。

    判据 2 为什么要附带「提到 tags」：只看形态会误伤 —— ``importer.parse_frontmatter``
    里解析**内联 YAML 数组**（``[a, b]``）的表达式形态与 tags 解析一模一样，
    但它管的是任意 frontmatter 数组，不是 tags。这是**第二次**在同一处犯同一类错：
    第一次按变量名匹配（换个参数名就溜过），第二次按纯形态匹配（把正当代码判成违规）。
    判据要盯行为里**专属**于 tags 的部分，而不是它能被套用的形状。
    """
    import ast
    import re

    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in (".git", ".venv", "tests", "__pycache__") for part in path.parts):
            continue
        if path.name == "util.py":
            continue  # 唯一允许的地方
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (SyntaxError, UnicodeDecodeError):
            continue
        lines = text.splitlines()

        for node in ast.walk(tree):
            # 判据 1：重复定义同名函数
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == "parse_tags":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}  def parse_tags")
                continue

            # 判据 2：切分形态 **且** 上下文提到 tags
            if not isinstance(node, ast.ListComp):
                continue
            if not any(isinstance(g.iter, ast.Call)
                       and isinstance(g.iter.func, ast.Attribute)
                       and g.iter.func.attr == "split"
                       for g in node.generators):
                continue
            lo = max(0, node.lineno - 4)
            hi = min(len(lines), node.lineno + 3)
            window = "\n".join(lines[lo:hi])
            if re.search(r"tags?", window, re.IGNORECASE):
                offenders.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}  {ast.unparse(node)[:60]}"
                )

    assert not offenders, (
        f"tags 解析应只在 mcore/util.py 里实现，另找到 {len(offenders)} 处：{offenders}"
    )
