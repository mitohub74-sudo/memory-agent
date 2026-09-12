# -*- coding: utf-8 -*-
"""中文 bigram 分词器 —— 本项目的检索基石。

为什么不能直接用 SQLite FTS5 的内置分词器
------------------------------------------
实测（SQLite 3.53.1，对照表见 README）：

  unicode61   把连续的中文当成【一个整词】。
              「登录使用私钥」是单个 token，所以搜「私钥」永远命中不了。

  trigram     要求查询【至少 3 个字符】。
              中文词绝大多数是 2 个字，「私钥」「人格」全部搜不到。

所以入库前必须自己把中文切成 2 字滑窗（bigram）：

    「阿里云」 -> 「阿里」「里云」

这样「私钥」这类 2 字词才会成为独立 token。

英文侧在**入库串**里保留 _ . / - 等字符（``ecs-prod``、``id_ed25519``、
``var/www`` 写成单个 token），但要注意这一步并不能让它们真的不被切开：
FTS5 的 unicode61 分词器还会再切一次。

实测（SQLite 3.53.1，``fts5vocab(main, cards_fts, 'row')``）：

    id_ed25519   索引里**不存在**这个 term
    id           存在（11 张卡）
    ed25519      存在（3 张卡）

即 ``_`` 是 unicode61 的分隔符，``id_ed25519`` 实际被切成 ``id`` + ``ed25519``。

后果是**好的**：搜 ``ed25519`` 能命中 ``id_ed25519``；搜 ``"id_ed25519"``
作为短语也能命中（两个 token 相邻）。所以不必自己再拆一遍。
"""

from __future__ import annotations

import re

__all__ = ["tokenize", "to_index_text", "query_terms", "to_query_expr"]

# 连续的中日韩统一表意文字（基本区 + 扩展 A）
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")

# 英文 / 数字 / 标识符：入库串里保留 _ . / - ，以便短语查询能整体命中
# （FTS5 的 unicode61 仍会按 _ . / - 再切一次，见模块 docstring）
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-/]*")


def _bigrams(run: str) -> list[str]:
    """把一段连续中文切成 2 字滑窗；单字则原样保留。"""
    if len(run) <= 1:
        return [run] if run else []
    return [run[i : i + 2] for i in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    """把原文切成 token 列表（中文 bigram + 英文小写标识符）。"""
    if not text:
        return []
    out: list[str] = []
    for run in _CJK.findall(text):
        out.extend(_bigrams(run))
    for word in _WORD.findall(text):
        out.append(word.lower())
    return out


def to_index_text(text: str) -> str:
    """生成写入 FTS5 索引的 token 串。"""
    return " ".join(tokenize(text))


def query_terms(query: str) -> list[str]:
    """查询词去重后的 token 列表（保持出现顺序），用于片段定位等场景。"""
    seen: dict[str, None] = {}
    for tok in tokenize(query):
        seen.setdefault(tok, None)
    return list(seen)


def to_query_expr(query: str, mode: str = "all", *, prefix: bool = False) -> str:
    """把用户查询转成 FTS5 的 MATCH 表达式。

    每个 token 都加双引号，原因：FTS5 的 MATCH 语法里 ``- . /`` 等字符有
    特殊含义。例如 ``ecs-prod`` 会被解析成「列 prod」而直接抛
    ``no such column`` 错误。加引号后变成短语匹配，语义安全。

    mode="all" -> 用 AND 连接（精确，默认）
    mode="any" -> 用 OR 连接（宽松，召回不足时降级使用）

    prefix=True -> 词尾加 ``*`` 走 FTS5 前缀查询。

    FTS5 的 MATCH 是【整词】匹配，正文里写了 ``sqlite3`` 就搜不到 ``sqlite``，
    写了 ``requests`` 就搜不到 ``request``。代码类内容里这种后缀差异极常见，
    所以需要前缀档位兜底。

    只对长度 >= 2 的词加 ``*``：单字前缀会命中几乎所有卡片（实测 ``"a"*``
    在 49 张卡的库里命中 45 张），纯噪音。
    """
    terms = query_terms(query)
    if not terms:
        return ""
    joiner = " AND " if mode == "all" else " OR "
    parts = []
    for t in terms:
        star = "*" if prefix and len(t) >= 2 else ""
        parts.append(f'"{t}"{star}')
    return joiner.join(parts)
