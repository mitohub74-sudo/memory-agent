# -*- coding: utf-8 -*-
"""MCP 工具描述与检索精度之间的契约（阶段 4 P4-01）。

要防的缺陷类型：**「文档说支持，实际没验证」**。
README 早就写了缓解手段 ——「工具描述里要求传实体名/标识符，不要传整句问句」，
但工具描述里当时写的是「可以是关键词，也可以是描述性短语」，两者**直接矛盾**，
而且没有任何东西会因为这个矛盾而失败。

所以这里把两件事分别钉住：

1. **描述文本必须承载那条要求**（接口契约：LLM 靠它决定怎么构造 query）。
   写成「什么都可以」等于把检索质量交给运气。
2. **那条要求必须有实测支撑**（关键词查询应当走精确档、首位命中；
   整句问句会降级到 OR 放宽档、并引入噪音）。若哪天检索行为变了，
   这条测试会失败，从而迫使重新评估那句 README 断言 ——
   而不是让一句可能已经失效的经验之谈继续留在文档里。

第 3 条测试用隔离语料构造，不依赖调用者的真实 vault；第 4 条用真实语料，
因为「整句问句会引入噪音」这件事只有在足够大的语料上才看得见。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcore import capture, config, importer, search, store
from mcore.mcp_server import TOOLS

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = ROOT / "memory.py"
README = ROOT / "README.md"


def _query_description() -> str:
    tool = next(t for t in TOOLS if t["name"] == "memory_search")
    return tool["inputSchema"]["properties"]["query"]["description"]


# ------------------------------------------------------------------ 契约文本


def test_query_description_forbids_full_sentences() -> None:
    """描述里必须出现「不要传整句问句」，并给出正反例。

    只断言「提到了」不够 —— 描述的价值在于**可执行**：调用方看完要知道
    该传什么、不该传什么。所以正向要求、正向样例、反向样例三者都要在。
    """
    d = _query_description()

    assert "不要传整句问句" in d, "必须明确禁止整句问句"
    assert "实体名" in d and "标识符" in d, "必须给出正向要求"
    assert "id_ed25519" in d or "阿里云" in d, "必须给出正向样例"
    assert "之前记过" in d or "帮我找一下" in d, "必须给出反向样例（问句长什么样）"

    # 旧的含糊措辞不得留下 —— 它正是这次要消除的矛盾
    assert "也可以是描述性短语" not in d, "含糊措辞必须移除，否则要求自我抵消"


def test_readme_mitigation_matches_tool_description() -> None:
    """README 的缓解手段与工具描述必须互相印证，不能各说一套。"""
    text = README.read_text(encoding="utf-8")

    assert "使用上的缓解手段" in text, "README 应保留这一节"
    assert "不要传整句问句" in text, "README 的缓解手段第 1 条应要求传实体名、不传问句"

    # 两边都出现同一条要求 —— 这就是「不再矛盾」的可验证形式
    assert "不要传整句问句" in _query_description()


# ------------------------------------------------------------------ 行为支撑


def _seed(root: Path) -> None:
    """隔离语料：一张含目标词的卡，一张只含问句中「虚词」的卡。

    第二张卡是关键。整句问句里除了实体名，还带着「之前」「记过」这类词，
    而它们在真实语料里必然出现在**别的**卡片上 —— 于是 AND 档无法满足、
    降级到 OR 档，把那两张卡一起召回。这正是噪音的生成机制。
    """
    vault = root / "vault"
    for title, body in (
        ("阿里云 ECS 登录方式",
         "阿里云 ECS 使用密钥登录，密钥位于 ~/.ssh/id_ed25519_aliyun，"
         "控制台入口在 RAM 权限页。"),
        # 关键：这张卡**不含**「阿里云」，但含「之前 / 记过」——问句里的虚词。
        # 于是整句问句的 AND 档失败、降到 OR 档时，它会被一起召回：噪音的生成机制。
        ("一条与云主机无关的记录",
         "之前记过的内容完全是别的事：记过一台备用机器的磁盘型号，"
         "以及机房的网线走向。"),
    ):
        result = capture.write_card(vault, title=title, body=body, kind="knowledge")
        assert result["action"] == "created", result

    conn = store.connect(root / "memory.db")
    try:
        store.init(conn)
        importer.sync(conn, vault)
    finally:
        conn.close()


def test_question_style_query_degrades_and_adds_noise() -> None:
    """实测支撑：整句问句会降级到 OR 放宽档，并把无关卡片一起召回。

    这就是「消灭问句 = 消灭噪音来源」的可验证形式。
    """
    with tempfile.TemporaryDirectory(prefix="memory-agent-query-") as raw:
        root = Path(raw)
        _seed(root)

        conn = store.connect(root / "memory.db")
        try:
            searcher = search.KeywordSearcher(conn)

            # 关键词：AND 精确档，只命中目标卡
            kw = searcher.search("阿里云", limit=5)
            assert kw, "关键词应当命中"
            assert kw[0].matched == "all", f"关键词应走 AND 精确档，实际 {kw[0].matched}"
            assert "阿里云" in kw[0].title
            assert len(kw) == 1, f"关键词不该召回无关卡：{[h.title for h in kw]}"

            # 整句问句：降级到 OR 档，噪音出现
            q = searcher.search("之前记过阿里云的内容吗", limit=5)
            assert q, "问句仍应有结果（宁可放宽也不返回空）"
            assert q[0].matched == "any", f"整句问句应降级到 OR 档，实际 {q[0].matched}"
            titles = [h.title for h in q]
            assert any("无关" in t for t in titles), (
                f"整句问句应把含虚词的无关卡一起召回（噪音）：{titles}"
            )
            assert len(q) > len(kw), "放宽档召回的条数应多于精确档"
        finally:
            conn.close()


def test_question_style_queries_can_miss_the_right_answer() -> None:
    """真实语料上的加固：整句问句不仅带噪音，还可能**答错**。

    用调用者真实 vault 跑（本机 50+ 张卡）。取几条典型的整句问句，
    断言它们不是「首位精确命中」—— 命中为空、或落在放宽档，都算符合预期。

    为什么需要这条而不只有隔离语料那条：噪音与误排是在**语料足够大**时才显形的；
    两张卡的隔离语料只能证明机制存在，证明不了「真实使用中会受影响」。
    语料太小（<5 张）时跳过，避免在空库 / 新克隆环境下误报。
    """
    db = config.db_path()
    if not db.exists():
        return  # 没有索引就没有可验证的语料，不算失败
    conn = store.connect(db)
    try:
        if store.count_cards(conn) < 5:
            return
        searcher = search.KeywordSearcher(conn)
        for q in (
            "帮我找一下数据库相关的记录",
            "我上次说的那个记忆库项目怎么样了",
            "之前有没有记过关于部署的内容",
        ):
            hits = searcher.search(q, limit=5)
            if hits and hits[0].matched == "all":
                raise AssertionError(
                    f"整句问句「{q}」竟走了 AND 精确档并首位命中 —— "
                    f"若检索行为确实改好了，请重新评估 README 的缓解手段第 1 条"
                )
    finally:
        conn.close()


def test_cli_search_help_states_the_same_requirement() -> None:
    """CLI 是同一个动作的另一个入口，帮助文本不该与 MCP 描述矛盾。"""
    env = dict(os.environ)
    env["MEMORY_AGENT_VAULT"] = str(Path(tempfile.gettempdir()) / "memory-agent-help-vault")
    proc = subprocess.run(
        [sys.executable, str(MEMORY_PY), "search", "--help"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    help_text = proc.stdout
    # 只要帮助里能看出 query 是什么，且不鼓励传问句
    assert "query" in help_text
    assert "描述性短语" not in help_text, "CLI 帮助不得留下与 MCP 相反的说法"
