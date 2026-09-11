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

英文侧保留 _ . / - 等字符，让 ecs-prod、id_ed25519、var/www 这类
标识符保持完整，不被切成碎片。
"""

from __future__ import annotations

import re

__all__ = ["tokenize", "to_index_text", "query_terms", "to_query_expr"]

# 连续的中日韩统一表意文字（基本区 + 扩展 A）
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")

# 英文 / 数字 / 标识符：保留 _ . / - 以便匹配 ecs-prod、id_ed25519、var/www
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


def to_query_expr(query: str, mode: str = "all") -> str:
    """把用户查询转成 FTS5 的 MATCH 表达式。

    每个 token 都加双引号，原因：FTS5 的 MATCH 语法里 ``- . /`` 等字符有
    特殊含义。例如 ``ecs-prod`` 会被解析成「列 prod」而直接抛
    ``no such column`` 错误。加引号后变成短语匹配，语义安全。

    mode="all" -> 用 AND 连接（精确，默认）
    mode="any" -> 用 OR 连接（宽松，召回不足时降级使用）
    """
    terms = query_terms(query)
    if not terms:
        return ""
    joiner = " AND " if mode == "all" else " OR "
    return joiner.join(f'"{t}"' for t in terms)
