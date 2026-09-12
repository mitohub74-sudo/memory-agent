# -*- coding: utf-8 -*-
"""memory-agent —— 面向大模型的本地记忆检索。

架构
----
    agent 会话 --采集端--> Markdown vault --索引端--> SQLite --查询端--> agent

本包实现**索引端**与检索：把 Markdown 目录索引成 SQLite，并提供可替换的
检索接口。

核心约束
--------
1. **Markdown 是真相源**，SQLite 只是可重建的索引。删库不影响数据。
2. **代码与数据分离**。仓库内不存任何记忆，路径由 mcore.config 解析。
3. **零外部依赖**，仅用 Python 标准库。
"""

from .version import __version__  # noqa: F401  —— 对外暴露 mcore.__version__
from . import (capture, config, fingerprint, importer, mcp_server, readtext, search,
               store, tokenize, util)

__all__ = [
    "config", "store", "tokenize", "importer", "search", "capture", "mcp_server",
    "readtext", "fingerprint", "util", "__version__",
]
