# -*- coding: utf-8 -*-
"""影响**召回结果**的参数的指纹（阶段 4 P4-12）。

要解决的问题
------------
``bench --baseline`` 过去只看得到「分数变了」，看不到**为什么变**。而变的原因至少有三类，
它们的处置完全不同：

1. **检索逻辑变了** —— 真正需要查代码的退化（例如某次改动让某档不再命中）；
2. **语料变了** —— 本机 vault 被别的 agent 持续写入（实际发生过：49 → 55 张卡，
   把一条查询的目标卡从第 4 名挤到第 7 名）。这**不是退化**，但分数确实会动；
3. **比较前提变了** —— 分词方式、档位顺序、FTS 用的排序函数等被改过，
   此时分数差异**根本无法归因**，因为两次比较的尺子不一样。

没有指纹时，三类混在一起，人只能凭记忆判断，于是很容易把 (2) 当成 (1) 去查代码、
或者更糟：把 (1) 当成 (2) 放过。

做法
----
把「产出检索结果的代码形状」本身做成指纹：取相关函数的 **AST 归一化文本**后哈希。

为什么用 AST 而不是源码文本或喊一声「版本号」：

- 源码文本：注释、空行、排版变了指纹就变，全是噪音；
- 人工维护的版本号：**要靠人记得改** —— 而忘记改的那个提交，正是它想防的那次改动。

AST 归一化只对**代码形状**敏感：改了逻辑（换个连接词、改阈值、增删一档）必然变，
改注释/格式不变。这是「忘不掉」的指纹。

指纹里放什么
------------
只放**真正影响召回**的部分：分词、查询表达式构造、档位表、相似度函数、
检索条数上限、索引 schema 版本。

刻意**不放**：检索的排序**输出**逻辑（覆盖率、档位标签、长度窗口）——
它们只影响展示，不影响召回什么。放进来的后果是指纹天天变，人就会开始忽略这个提示，
那它就和没有一样。
"""

from __future__ import annotations

import ast
import hashlib
import inspect

__all__ = ["fingerprint", "FINGERPRINT_PARTS", "FINGERPRINT_SCHEMA"]

# 指纹自身的版本：万一以后要改「怎么算指纹」，老基线要能被识别出来，
# 而不是被当成「参数变了」。
FINGERPRINT_SCHEMA = 1


def _normalized_source(func) -> str:
    """函数的 AST 归一化文本：丢掉注释、空行与排版，只留代码形状。"""
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError) as exc:  # pragma: no cover - 交互式定义时才会发生
        return f"<unavailable:{type(exc).__name__}>"
    try:
        tree = ast.parse(src)
    except SyntaxError:  # pragma: no cover - 源码本身语法错时无法归一化
        return src
    return ast.dump(tree)


def _ast_of(obj) -> str:
    """取一个对象的定义形状；不是可解析对象时退回 repr。"""
    if callable(obj):
        return _normalized_source(obj)
    return repr(obj)


def fingerprint() -> str:
    """算出当前「影响召回的参数」指纹（16 位十六进制）。

    **同一次提交内必定稳定**；任何影响召回的代码形状变化都会让它变。
    """
    from . import search, store, tokenize

    parts: dict[str, str] = {
        "fingerprint_schema": str(FINGERPRINT_SCHEMA),
        # --- 分词：决定入库与查询两侧切成什么 token ---
        "tokenize.tokenize": _ast_of(tokenize.tokenize),
        "tokenize.to_index_text": _ast_of(tokenize.to_index_text),
        "tokenize.query_terms": _ast_of(tokenize.query_terms),
        "tokenize.to_query_expr": _ast_of(tokenize.to_query_expr),
        # --- 查询表达式：正则本身也是参数（它决定什么算一个词） ---
        "tokenize._CJK": repr(getattr(tokenize, "_CJK", None)),
        "tokenize._WORD": repr(getattr(tokenize, "_WORD", None)),
        # --- 档位与降级顺序：这一项变动直接改变召回集 ---
        "search.KeywordSearcher.TIERS": repr(search.KeywordSearcher.TIERS),
        "search.KeywordSearcher._run": _ast_of(search.KeywordSearcher._run),
        "search.KeywordSearcher.search": _ast_of(search.KeywordSearcher.search),
        # --- 条数上限：钳制值变了，召回集就变了 ---
        "search.MIN_LIMIT": repr(search.MIN_LIMIT),
        "search.MAX_LIMIT": repr(search.MAX_LIMIT),
        "search.clamp_limit": _ast_of(search.clamp_limit),
        # --- 索引结构：schema 变了，索引内容就可能不同 ---
        "store.SCHEMA_VERSION": repr(store.SCHEMA_VERSION),
        "store._DDL": repr(getattr(store, "_DDL", None)),
        "store.MIGRATIONS": repr(getattr(store, "MIGRATIONS", None)),
    }
    payload = "\n".join(f"{k}={v}" for k, v in sorted(parts.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fingerprint_parts() -> dict[str, str]:
    """逐个部件的「内容哈希」，用于诊断**是哪一项**变了。

    整体指纹只能回答「变了没有」。真变了的时候，人需要知道变的是分词还是档位 ——
    所以每个部件各给一个短哈希，比对时逐个看即可。
    """
    from . import search, store, tokenize

    items = {
        "tokenize": "\n".join([
            _ast_of(tokenize.tokenize),
            _ast_of(tokenize.query_terms),
            _ast_of(tokenize.to_query_expr),
        ]),
        # 分词用的正则**也是参数**（它决定什么算一个词），而且改它只动常量、
        # 不动任何函数的 AST —— 早期版本只放函数 AST，于是改 `_WORD` 之后
        # 整体指纹变了、而部件哈希全都报「没变」，诊断信息自相矛盾（实测踩到）。
        # 部件存在的意义就是指出「是哪一项变了」，所以常量必须单独成项。
        "tokenize_re": "\n".join([
            repr(getattr(tokenize, "_CJK", None)),
            repr(getattr(tokenize, "_WORD", None)),
        ]),
        "tiers": repr(search.KeywordSearcher.TIERS),
        "limits": f"{search.MIN_LIMIT},{search.MAX_LIMIT}",
        "schema": repr(store.SCHEMA_VERSION),
    }
    return {k: hashlib.sha256(v.encode("utf-8")).hexdigest()[:8] for k, v in items.items()}


# 指纹覆盖的部件清单（文档用途，也便于测试断言「该覆盖的都覆盖了」）。
FINGERPRINT_PARTS = ("tokenize", "tokenize_re", "tiers", "limits", "schema")
