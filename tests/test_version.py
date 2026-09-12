# -*- coding: utf-8 -*-
"""版本号单一来源的回归测试。"""

from __future__ import annotations

import mcore
from mcore import mcp_server
from mcore.version import __version__


def test_version_is_shared_by_package_and_mcp_server() -> None:
    assert mcore.__version__ == __version__ == mcp_server.SERVER_VERSION
