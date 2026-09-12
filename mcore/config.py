# -*- coding: utf-8 -*-
"""路径解析与本地配置。

项目本体与数据彻底分离
----------------------
代码在 git 仓库里，记忆在 ``~/.memory_agent``。仓库内不存任何记忆数据，
也不硬编码任何本机路径，可安全公开。

配置跟数据走，不跟代码走
------------------------
本机专属的设置（vault 在哪、索引库在哪）写在数据目录下的 ``config.json``，
不进仓库。这样同一份代码在任何机器上都能用，各自的配置互不干扰。

解析优先级（高 → 低）
---------------------
索引库：CLI ``--db``  >  环境变量 ``MEMORY_AGENT_DB``   >  config.json  >  默认
vault： CLI ``--vault`` >  环境变量 ``MEMORY_AGENT_VAULT`` >  config.json  >  默认
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ["data_home", "db_path", "vault_path", "config_path", "load_config", "describe"]

DB_NAME = "memory.db"
VAULT_DIRNAME = "vault"
CONFIG_NAME = "config.json"

ENV_HOME = "MEMORY_AGENT_HOME"
ENV_DB = "MEMORY_AGENT_DB"
ENV_VAULT = "MEMORY_AGENT_VAULT"


def data_home() -> Path:
    """数据根目录。默认 ``~/.memory_agent``。"""
    raw = os.environ.get(ENV_HOME, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".memory_agent"


def config_path() -> Path:
    return data_home() / CONFIG_NAME


def load_config() -> dict:
    """读取数据目录下的本地配置。不存在或损坏时返回空字典。"""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def db_path(override: str | None = None) -> Path:
    """索引库路径。"""
    if override:
        return Path(override).expanduser()
    env = os.environ.get(ENV_DB, "").strip()
    if env:
        return Path(env).expanduser()
    cfg = str(load_config().get("db") or "").strip()
    if cfg:
        return Path(cfg).expanduser()
    return data_home() / DB_NAME


def vault_path(override: str | None = None) -> Path:
    """Markdown vault 路径。"""
    if override:
        return Path(override).expanduser()
    env = os.environ.get(ENV_VAULT, "").strip()
    if env:
        return Path(env).expanduser()
    cfg = str(load_config().get("vault") or "").strip()
    if cfg:
        return Path(cfg).expanduser()
    return data_home() / VAULT_DIRNAME


def describe(db_override: str | None = None) -> dict:
    """当前生效的路径，供机器读取与排查。

    ``db_override`` 对应 CLI 的 ``--db``。传了就必须体现在结果里 ——
    否则 ``paths`` 报告的路径与实际使用的不是同一个，排查时会把人带偏。
    """
    db = db_path(db_override)
    return {
        "data_home": str(data_home()),
        "config": str(config_path()),
        "config_exists": config_path().exists(),
        "db": str(db),
        "db_exists": db.exists(),
        "vault": str(vault_path()),
        "vault_exists": vault_path().is_dir(),
    }
