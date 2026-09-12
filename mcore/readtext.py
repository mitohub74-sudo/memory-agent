# -*- coding: utf-8 -*-
"""正文读取窗口：长度上限与分页偏移。

为什么单独一个模块
------------------
CLI 的 ``show`` 与 MCP 的 ``memory_get`` 是**同一个动作的两个入口**。P1 已经
踩过一次这个坑：CLI 的 capture 忘了自动索引，而 MCP 的做了 —— 两个入口给出
两种结果，两边都返回「成功」。长度截断如果各写一份，会以完全相同的方式分叉。
所以这里只写一份，两个入口都调它。

为什么要截断
------------
卡片会长。真实语料里已经有单卡上万字符的（会话蒸馏卡）。一次把全文灌给模型
既贵又没必要 —— 多数时候只需要看到结论那一段。截断必须**显式**（``truncated``
+ ``next_offset``），不能悄悄砍掉后半段然后当作完整内容返回，那就是假成功。
"""

from __future__ import annotations

__all__ = ["DEFAULT_MAX_CHARS", "MAX_OFFSET", "render_window"]

# 单次返回的默认上限。取 20000：足以容纳绝大多数卡片全文，
# 又明显低于会拖慢上下文的量级。
DEFAULT_MAX_CHARS = 20000

# 偏移上限。防止调用方传一个天文数字进来把 start 算成负数或触发无谓的大切片。
MAX_OFFSET = 10_000_000


def render_window(body: str, offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """按窗口切出正文，返回内容与「还有没有后续」的完整描述。

    参数语义（两个入口共用，不要各自解释一遍）：

    - ``offset``：从第几个字符开始，负值按 0 处理；
    - ``max_chars``：本窗口最多返回多少字符；``<= 0`` 表示**不限长**（相当于 ``--full``）。

    返回值字段：

    ``text``
        本窗口的内容。越界时为空串。
    ``offset``
        实际生效的起始位置（已钳制并按 length 收敛）。
    ``length``
        正文总长度。调用方靠它判断「到底有多长」。
    ``returned``
        本窗口返回的字符数。
    ``truncated``
        **是否还有后续未读内容**（``offset + returned < length``）。
        注意它表示「没读完」，不是「被砍了」—— 在 offset>0 时两者可能不同，
        所以另外给了 ``has_more`` 作为等价的、语义更直白的别名。
    ``next_offset``
        下一页的 offset；没有后续时为 ``None``。
    ``has_more``
        与 ``truncated`` 同义，名字更直白，供工具描述使用。
    """
    body = body or ""
    length = len(body)

    try:
        start = int(offset)
    except (TypeError, ValueError):
        start = 0
    start = max(0, min(start, MAX_OFFSET))
    if start > length:
        start = length  # 越界不报错，给空窗口 —— 调用方多翻一页是常见行为

    try:
        cap = int(max_chars)
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_CHARS

    if cap <= 0:
        text = body[start:]
    else:
        text = body[start : start + cap]

    returned = len(text)
    has_more = start + returned < length
    return {
        "text": text,
        "offset": start,
        "length": length,
        "returned": returned,
        "truncated": has_more,
        "has_more": has_more,
        "next_offset": (start + returned) if has_more else None,
    }
