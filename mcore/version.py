# -*- coding: utf-8 -*-
"""版本号单一来源。

为什么单独一个文件，而不是写在 ``mcore/__init__.py`` 里
--------------------------------------------------------
因为 ``mcp_server.py`` 需要这个值，而它**不能**写
``from . import __version__`` —— ``mcore/__init__.py`` 的第一行就会
``from . import ... mcp_server ...``，此时 ``__version__`` 还没被赋值，
导入链条会绕回自己。

独立的 ``version.py`` 不导入任何 mcore 模块，因此谁先导入都拿得到值。

改版本号只改这一个地方。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.5.0"
