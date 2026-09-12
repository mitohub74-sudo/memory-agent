# -*- coding: utf-8 -*-
"""零依赖直跑测试与 pytest 共同使用的最小辅助函数。"""

from __future__ import annotations


def require_success(code: int) -> None:
    """把自定义 harness 的退出码转成 pytest 可识别的断言。"""
    assert code == 0, f"测试 harness 失败，退出码 {code}"
