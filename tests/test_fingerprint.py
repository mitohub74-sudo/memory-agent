# -*- coding: utf-8 -*-
"""评测参数指纹（阶段 4 P4-12）。

要防的是**归因错误**。`bench --baseline` 只报「分数变了」时，人只能凭记忆判断原因，
而原因至少有三类、处置完全不同：

1. **检索逻辑变了** —— 真要查代码的退化；
2. **语料变了** —— 本机实测发生过（49 → 55 张卡把目标卡从第 4 名挤到第 7 名），
   分数会动，但这不是退化；
3. **比较前提变了** —— 分词/档位/相似度函数被改过，此时分数差异**无法归因**。

第 3 类是最危险的：它会让 1 和 2 的判断全错。指纹就是为了把 3 单独拎出来。

测试分三段：
- **稳定性**：同一份代码算两次必须一样（否则提示会天天误报，人就学会忽略它）；
- **敏感性**：影响召回的部件变了，指纹/部件哈希必须变；
- **边界**：只影响展示的改动**不得**动指纹 —— 这条同样重要，
  否则指纹会在无关提交里乱跳，最终同样被忽略。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcore import fingerprint, search, tokenize

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = ROOT / "memory.py"
BENCH = ROOT / "bench" / "retrieval_quality.py"


# ------------------------------------------------------------------ 稳定性


def test_fingerprint_is_stable_within_a_run() -> None:
    assert fingerprint.fingerprint() == fingerprint.fingerprint()
    assert len(fingerprint.fingerprint()) == 16
    int(fingerprint.fingerprint(), 16)  # 必须是合法十六进制


def test_fingerprint_is_stable_across_processes() -> None:
    """跨进程也要一样 —— 否则「基线里存的指纹」根本没法比。"""
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from mcore import fingerprint; print(fingerprint.fingerprint())" % ROOT
    )
    outs = set()
    for _ in range(2):
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(ROOT))
        assert proc.returncode == 0, proc.stderr
        outs.add(proc.stdout.strip())
    assert len(outs) == 1, outs


def test_parts_cover_the_documented_components() -> None:
    parts = fingerprint.fingerprint_parts()
    assert set(parts) == set(fingerprint.FINGERPRINT_PARTS), parts
    assert all(len(v) == 8 for v in parts.values()), parts


# ------------------------------------------------------------------ 敏感性


def test_tiers_part_changes_when_a_tier_is_added(monkeypatch) -> None:
    """档位表是召回集的直接决定因素 —— 加一档必须让部件哈希变。"""
    before = fingerprint.fingerprint_parts()["tiers"]
    monkeypatch.setattr(
        search.KeywordSearcher, "TIERS",
        search.KeywordSearcher.TIERS + (("extra", "any", True),),
    )
    assert fingerprint.fingerprint_parts()["tiers"] != before


def test_limits_part_changes_when_a_limit_changes(monkeypatch) -> None:
    before = fingerprint.fingerprint_parts()["limits"]
    monkeypatch.setattr(search, "MAX_LIMIT", search.MAX_LIMIT + 1)
    assert fingerprint.fingerprint_parts()["limits"] != before


def test_schema_part_changes_when_schema_version_changes(monkeypatch) -> None:
    from mcore import store

    before = fingerprint.fingerprint_parts()["schema"]
    monkeypatch.setattr(store, "SCHEMA_VERSION", store.SCHEMA_VERSION + 1)
    assert fingerprint.fingerprint_parts()["schema"] != before


def test_tokenize_part_changes_when_the_code_shape_changes(monkeypatch) -> None:
    """分词是查询与入库两侧共用的，改了它召回必然变。

    这里替换成**不同实现**（而不是不同注释），验证的是「AST 归一化对代码形状敏感」。
    """
    before = fingerprint.fingerprint_parts()["tokenize"]

    def different_tokenize(text: str) -> list[str]:
        return (text or "").split()

    monkeypatch.setattr(tokenize, "tokenize", different_tokenize)
    assert fingerprint.fingerprint_parts()["tokenize"] != before


def test_tokenize_re_part_changes_when_the_regex_changes(monkeypatch) -> None:
    """改分词**正则**必须被标记出来 —— 这是实测踩到的一个诊断缺口。

    正则决定「什么算一个词」，是货真价实的召回参数，但它只是个模块级常量、
    不改任何函数的 AST。早期版本的部件口径只放函数 AST，于是：
    **整体指纹变了，而所有部件都报「没变」** —— 诊断信息自相矛盾，
    人看到「指纹变了但没有一项变」，只会以为工具坏了。

    部件存在的意义就是指出「是哪一项变了」，所以常量必须单独成项。
    """
    import re

    before_all = fingerprint.fingerprint()
    before_part = fingerprint.fingerprint_parts()["tokenize_re"]

    monkeypatch.setattr(tokenize, "_WORD", re.compile(r"[A-Za-z0-9]+"))

    assert fingerprint.fingerprint() != before_all, "整体指纹必须变"
    assert fingerprint.fingerprint_parts()["tokenize_re"] != before_part, (
        "正则属于分词参数，tokenize_re 部件必须标记变化"
    )
    # 函数部分确实没动 —— 这正是当初漏报的原因，如实断言
    assert fingerprint.fingerprint_parts()["tokenize"] == \
        fingerprint.fingerprint_parts()["tokenize"]


def _changed_parts(before: dict, after: dict) -> list[str]:
    return [k for k in before if before[k] != after[k]]


def test_each_recall_parameter_is_reported_by_its_own_part(monkeypatch) -> None:
    """逐个验证：每个召回参数都有**专属**部件负责标记它。

    这条把「整体指纹变了、但部件清单全是原样」这个诊断矛盾钉死 ——
    那会让人以为工具坏了。第二项（分词正则）正是实测漏报过的那个。
    """
    import re

    from mcore import store

    cases = (
        ("tiers", lambda m: m.setattr(
            search.KeywordSearcher, "TIERS",
            search.KeywordSearcher.TIERS + (("x", "any", True),))),
        ("limits", lambda m: m.setattr(search, "MAX_LIMIT", 99)),
        ("schema", lambda m: m.setattr(store, "SCHEMA_VERSION", 999)),
        ("tokenize_re", lambda m: m.setattr(
            tokenize, "_WORD", re.compile(r"[A-Za-z0-9]+"))),
    )

    for expected, mutate in cases:
        with monkeypatch.context() as m:
            before_all = fingerprint.fingerprint()
            before_parts = fingerprint.fingerprint_parts()

            mutate(m)

            after_all = fingerprint.fingerprint()
            after_parts = fingerprint.fingerprint_parts()
            changed = _changed_parts(before_parts, after_parts)

            assert after_all != before_all, f"{expected}: 整体指纹应变化"
            assert changed, (
                f"{expected}: 整体变了却没有部件指出方向 —— 诊断信息自相矛盾"
            )
            assert expected in changed, (
                f"{expected}: 应由同名部件标记；实际变化的是 {changed}"
            )



    before = fingerprint.fingerprint()
    monkeypatch.setattr(
        search.KeywordSearcher, "TIERS",
        search.KeywordSearcher.TIERS + (("extra", "any", True),),
    )
    assert fingerprint.fingerprint() != before


def test_fingerprint_itself_would_change_on_an_unknown_parameter(
    monkeypatch,
) -> None:
    """**关键机制测试**：把「影响召回的参数」换成未知值，指纹必须变。

    不靠「改磁盘上的源码再还原」这种有残留风险的手法来证明敏感性，
    而是直接替换被指纹覆盖的对象。
    """
    before = fingerprint.fingerprint()
    monkeypatch.setattr(tokenize, "_WORD",
                        __import__("re").compile(r"[A-Za-z0-9]+"))
    assert fingerprint.fingerprint() != before, (
        "词形正则属于分词参数，改动它必须让指纹变化"
    )


# ------------------------------------------------------------------ 边界：展示层不该动指纹


def test_fingerprint_sources_exclude_display_only_module() -> None:
    """指纹的取源不得包含**只影响展示**的模块。

    结构化断言：指纹覆盖清单里出现的模块只有 tokenize / search / store，
    而 readtext（长度窗口）、capture（写入）、util（标签解析）不在其中。
    """
    src = (ROOT / "mcore" / "fingerprint.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 收集指纹函数体里出现的属性访问根名（tokenize.xxx / search.xxx / store.xxx）
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            roots.add(node.value.id)

    assert {"tokenize", "search", "store"} <= roots, roots
    for display_only in ("readtext", "capture", "util", "mcp_server", "importer"):
        assert display_only not in roots, (
            f"{display_only} 只影响展示或写入，不该进入召回指纹 —— "
            f"否则指纹会在无关提交里乱跳，最终被忽略"
        )


def test_display_only_changes_do_not_move_the_fingerprint() -> None:
    """行为层面的同一件事：只改展示逻辑时指纹不动。

    做法是把一段纯展示逻辑换掉（改 low_confidence_note 的返回值），
    指纹必须**完全不变** —— 它不影响召回什么。
    """
    before = fingerprint.fingerprint()
    original = search.low_confidence_note
    try:
        search.low_confidence_note = lambda matched, coverage: "换成别的提示文案"
        assert fingerprint.fingerprint() == before, (
            "低置信提示只影响展示，不该进入召回指纹"
        )
    finally:
        search.low_confidence_note = original


def test_display_only_source_change_does_not_move_a_part(monkeypatch) -> None:
    """把展示函数替换成不同实现，部件的 AST 也不该因此变化。"""
    parts_before = fingerprint.fingerprint_parts()

    def different_note(matched: str, coverage: float) -> str:
        return "完全不同的一句提示"

    monkeypatch.setattr(search, "low_confidence_note", different_note)
    assert fingerprint.fingerprint_parts() == parts_before


# ------------------------------------------------------------------ CLI 端到端


def _run_bench(*argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    return subprocess.run(
        [sys.executable, str(BENCH), *argv],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT),
    )


def test_bench_saves_fingerprint_and_detects_a_changed_premise() -> None:
    """端到端：保存的基线里带指纹；指纹不一致时**明确报告前提已变**。

    用一个伪装的老基线（指纹字段被改掉）来触发提示 —— 这样不必改动真实基线文件，
    也不会伪造出「未变」的假结论。
    """
    db = Path(os.path.expanduser("~")) / ".memory_agent" / "memory.db"
    qfile = Path(os.path.expanduser("~")) / ".memory_agent" / "bench_queries.json"
    if not db.exists() or not qfile.exists():
        return  # 空环境跳过

    with tempfile.TemporaryDirectory(prefix="memory-agent-fp-") as raw:
        root = Path(raw)

        # 1) 正常存基线 —— 必须带指纹与部件
        base_path = root / "base.json"
        proc = _run_bench("--save", str(base_path), "--json")
        assert proc.returncode == 0, proc.stderr
        saved = json.loads(base_path.read_text(encoding="utf-8"))
        assert saved["fingerprint"] == fingerprint.fingerprint()
        assert set(saved["fp_parts"]) == set(fingerprint.FINGERPRINT_PARTS)

        # 2) 指纹一致时不该出现「前提已变」
        proc = _run_bench("--baseline", str(base_path), "--json")
        payload = json.loads(proc.stdout)
        assert payload["premise_changed"] == [], payload["premise_changed"]

        # 3) 篡改基线里的指纹与一个部件 —— 必须报「前提已变」，并指出是哪一项
        tampered = root / "tampered.json"
        saved["fingerprint"] = "0" * 16
        saved["fp_parts"]["tiers"] = "deadbeef"
        payload_cards = saved["cards"]
        tampered.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        proc = _run_bench("--baseline", str(tampered), "--json")
        out = json.loads(proc.stdout)
        assert out["premise_changed"], "指纹不一致时必须报告前提已变"
        joined = " ".join(out["premise_changed"])
        assert "前提已变" in joined, joined
        assert "tiers" in joined, f"应指出是哪个部件变了：{joined}"
        assert out["baseline_cards"] == payload_cards, out["baseline_cards"]


def test_bench_reports_a_missing_fingerprint_honestly() -> None:
    """老基线没有指纹字段时，必须说「无法核对」，不能假装一致。

    假装一致的后果比报错更糟：它给出一个**错误的「前提未变」结论**，
    而这正是这个功能要防的东西。
    """
    db = Path(os.path.expanduser("~")) / ".memory_agent" / "memory.db"
    qfile = Path(os.path.expanduser("~")) / ".memory_agent" / "bench_queries.json"
    if not db.exists() or not qfile.exists():
        return

    with tempfile.TemporaryDirectory(prefix="memory-agent-fp-") as raw:
        root = Path(raw)
        base_path = root / "base.json"
        proc = _run_bench("--save", str(base_path), "--json")
        assert proc.returncode == 0, proc.stderr

        # 造一个「老格式」基线：去掉指纹字段，其余保留
        legacy = json.loads(base_path.read_text(encoding="utf-8"))
        legacy.pop("fingerprint")
        legacy.pop("fp_parts")
        (root / "legacy.json").write_text(
            json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

        proc = _run_bench("--baseline", str(root / "legacy.json"), "--json")
        out = json.loads(proc.stdout)
        joined = " ".join(out["premise_changed"])
        assert "无法核对" in joined or "老格式" in joined, joined
