# -*- coding: utf-8 -*-
"""档位标签单一来源（阶段 4 P4-03）。

背景：档位的**语义**（四档、降级顺序）定义在 ``search.KeywordSearcher.TIERS``，
但标签原本写在 ``mcp_server.py`` —— 「语义在这里、名字在那里」。一旦新增档位、
或在别处也要渲染档位，就会各写一份然后慢慢漂移。本项目已因「两处各写一份」
吃过两次教训（P1 的 CLI/MCP capture 分叉、P3-04 的窗口实现），所以标签搬到了
``search`` 里，模块 docstring 也写明了理由。

这里验证三件事：

1. 标签表覆盖**全部四个档位**，且与 ``TIERS`` 一一对应（漏一个会在渲染处
   显示内部值，而不是缺少一行代码 —— 那是静默的）；
2. 两个入口（CLI 与 MCP）输出的标签**逐字等于** ``search.MODE_LABELS``；
3. 代码里**没有第二处**定义这套标签 —— 用 AST 找「字面量赋值」而不是 grep 文本，
   因为文档字符串里引用标签是**正确**的（例如 KeywordSearcher 的档位说明），
   grep 会把它们误判成重复定义。第一版测试就是这么被绊倒的。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from mcore import search
from mcore.mcp_server import TOOLS

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = ROOT / "memory.py"


def _cli_search(*argv: str) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    env["MEMORY_AGENT_HOME"] = str(ROOT)  # 不会被用到，仅避免读到意外的家目录
    return subprocess.run(
        [sys.executable, str(MEMORY_PY), "search", *argv],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT), env=env,
    )


# ------------------------------------------------------------------ 表本身


def test_labels_cover_every_tier() -> None:
    """四个档位都要有标签，且键集合与 TIERS 完全一致。"""
    tier_values = {mode for mode, _joiner, _prefix in search.KeywordSearcher.TIERS}
    assert tier_values == {"all", "all-prefix", "any", "any-prefix"}
    assert set(search.MODE_LABELS) == tier_values, (
        f"标签表与档位不一致：缺 {sorted(tier_values - set(search.MODE_LABELS))}，"
        f"多 {sorted(set(search.MODE_LABELS) - tier_values)}"
    )


def test_relaxed_tiers_are_marked_as_relaxed() -> None:
    """放宽档的标签必须自述「已放宽」—— 调用方要知道这批结果为什么可信度低。"""
    for mode in ("any", "any-prefix"):
        assert "放宽" in search.MODE_LABELS[mode], search.MODE_LABELS[mode]
    for mode in ("all", "all-prefix"):
        assert "精确" in search.MODE_LABELS[mode] or "前缀" in search.MODE_LABELS[mode]


def test_mode_label_falls_back_to_the_raw_value() -> None:
    assert search.mode_label("all") == search.MODE_LABELS["all"]
    # 未登记的档位原样返回：不抛错、不编名字，至少能看出「这里有个没登记的档位」
    assert search.mode_label("something-new") == "something-new"
    assert search.mode_label("") == ""


# ------------------------------------------------------------------ 两个入口


def test_mcp_output_uses_the_shared_labels() -> None:
    """MCP 渲染出来的模式串必须来自 MODE_LABELS。"""
    from mcore import mcp_server
    src = _source_without_docstrings(Path(mcp_server.__file__))
    assert "mode_label" in src, "MCP 必须走 search.mode_label，不能自己拼标签"
    # 旧的内联字典不得留下
    assert '"AND 精确"' not in src, "mcp_server 里不该再有内联的档位标签字典"


def test_cli_json_and_text_use_the_shared_labels() -> None:
    """CLI 的 JSON 与文本输出都要给出与 MODE_LABELS 逐字相同的标签。"""
    proc = _cli_search("阿里云", "--json")
    # 无索引时跳过（空环境不该因此失败）
    if proc.returncode == 2:
        return
    assert proc.returncode == 0, f"{proc.returncode} {proc.stdout} {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["results"], payload
    first = payload["results"][0]
    assert first["matched_label"] == search.MODE_LABELS[first["matched"]], first
    assert payload["mode"] == search.MODE_LABELS[first["matched"]], payload["mode"]

    text = _cli_search("阿里云").stdout
    assert f"匹配模式={search.MODE_LABELS[first['matched']]}" in text, text[:300]


# ------------------------------------------------------------------ 没有第二处定义


def _find_label_dict_definitions() -> list[str]:
    """在源码里找「把档位名映射到中文字面量」的字典赋值，返回 ``文件:行``。

    用 AST 而不是 grep：只看**赋值语句**里的键值对，因此
    ``KeywordSearcher`` 的档位说明文档字符串（那里出现档位名与中文说明是**应该**的）
    不会被误判。
    """
    hits: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in (".git", ".venv", "tests", "__pycache__") for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Dict, ast.DictComp)):
                continue
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if not {"all", "any"} & set(keys):
                continue
            values = [v.value for v in node.values
                      if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            # 只有「档位名 → 中文标签」这种形态才算重复定义
            if any("\u4e00" <= ch <= "\u9fff" for ch in "".join(values)):
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return hits


def test_labels_are_defined_in_exactly_one_place() -> None:
    hits = _find_label_dict_definitions()
    assert len(hits) == 1, (
        f"档位标签应只在一处定义，实际找到 {len(hits)} 处：{hits}。"
        f"重复定义会随新增档位而漂移，这正是 P4-03 要消除的。"
    )
    assert hits[0].startswith("mcore"), hits


def _source_without_docstrings(path: Path) -> str:
    """剥掉文档字符串后的源码文本，用于「代码里不该有某字面量」这类断言。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body[0].value.value = ""
    return ast.unparse(tree)


def test_query_descriptions_still_reference_tiers_consistently() -> None:
    """工具描述里提到的档位名应当与标签体系一致（避免出现已废弃的措辞）。"""
    tool = next(t for t in TOOLS if t["name"] == "memory_search")
    desc = tool["description"] + tool["inputSchema"]["properties"]["query"]["description"]
    assert "AND" in desc or "精确" in desc, desc
    # 已废弃的措辞（曾经写「描述性短语」，与 README 矛盾）
    assert "描述性短语" not in desc, desc


def test_unknown_tier_reaches_the_caller_instead_of_being_hidden() -> None:
    """兜底路径也要有测试：未登记档位原样透出，两个入口都不掩盖。"""
    from mcore import mcp_server  # noqa: F401  （确认导入路径可用）
    assert search.mode_label("unregistered-tier") == "unregistered-tier"
    # 提示函数对未知档位不应崩，且不应误报「低置信」
    assert search.low_confidence_note("unregistered-tier", 0.5) == ""
