# -*- coding: utf-8 -*-
"""跨模块共用的极小工具。

为什么要有这个文件
------------------
``parse_tags`` 原本在三个地方各写了一份：

- ``importer.build_card``（从 frontmatter 读到的值）；
- ``memory.py`` 的 CLI ``capture --tags``；
- ``mcp_server.memory_capture``（工具参数）。

三份的**意图**相同，但实现并不一致：后两处对「已经是 list」的输入直接放行，
跳过了 strip / 去空 —— 于是 ``["a b ", "", "a b"]`` 这样的输入会带着空格与重复项
落进 frontmatter，而走字符串路径的 CLI 同样输入却会被规范化。同一张卡走两个
入口得到两种结果，而且两边都返回「成功」：这正是本项目在 P1（CLI/MCP capture
分叉）和 P3-04（读取窗口）已经吃过两次亏的模式，所以这类「同一个意思、几处各写」
的代码一律收口。

为什么不做成一个万能的 ``mcore/utils.py`` 大杂烩：这个模块只放**被两处以上复用**
且**没有更合适的归属**的东西。有明确归属的（分词归 ``tokenize``、档位归 ``search``）
不要往这里塞，否则它会变成垃圾桶，而垃圾桶里的东西没人敢改。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

__all__ = ["parse_tags", "parse_duration"]


def parse_duration(text: Any) -> int | None:
    """把 ``30d`` / ``12h`` / ``45m`` / ``1d12h`` 解析成**秒数**；不是合法时长返回 ``None``。

    用在两处，所以收口在这里：

    - ``importer._parse_ttl`` 解析卡片 frontmatter 的 ``ttl``；
    - 回收站清理按「删除多久了」筛选（``delete --purge --older-than 30d``）。

    两处各写一份的后果不是崩溃而是**口径漂移**：一处认 ``1d12h``、另一处不认，
    于是同一张卡在 ttl 上被认作「30 天后过期」、在清理时被判成「格式非法」——
    两边都不报错。本项目已经因为「同一个意思、几处各写」吃过多次亏。

    ``None`` 与 ``0`` 是两件事：``None`` 是「压根不是时长」（调用方该报错或忽略），
    ``0`` 是「合法的零时长」。合并成 ``0`` 会让 ``ttl: 乱写`` 被静默当成
    「永不过期」——那是把解析失败伪装成了成功。
    """
    if text is None:
        return None
    compact = re.sub(r"\s+", "", str(text).strip().strip("'\"").lower())
    if not compact:
        return None
    parts = re.findall(r"(\d+)([dhm])", compact)
    # 「整串都由 数字+单位 组成」才算合法：`1d12h30m` 可以，`30days` 不行。
    if not parts or "".join(f"{n}{u}" for n, u in parts) != compact:
        return None
    return sum(int(v) * {"d": 86400, "h": 3600, "m": 60}[u] for v, u in parts)


def parse_tags(raw: Any) -> list[str]:
    """把任意形状的 tags 输入规范化成 ``list[str]``。

    接受的形状（都是实际出现过的调用方式）：

    - ``None`` / ``""`` / ``[]`` → ``[]``
    - ``"a, b"`` → ``["a", "b"]``（CLI ``--tags`` 的写法）
    - ``'["a","b"]'`` → ``["a", "b"]``（MCP 客户端把数组塞成字符串时）
    - ``"a, b, a"`` → ``["a", "b"]``（**去重**）
    - ``[" a ", "", "a"]`` → ``["a"]``（list 输入也走同样的规范化）
    - ``[1, 2]`` → ``["1", "2"]``（非字符串元素转成字符串，不丢信息）

    三条刻意的约定：

    1. **保持出现顺序**（用 dict 去重，不用 set）—— 标签的先后是作者写的顺序，
       重排会让 diff 无端变化；
    2. **list 输入与字符串输入得到同一结果**。这是这次收口要修掉的真实不一致：
       原来 list 输入会保留空格与重复项；
    3. **不抛错**。tags 是附属信息，为一个格式不规范的标签让整次 capture 失败
       代价完全不成比例（与 ``importer._parse_priority`` 同一取向：索引端如实投影，
       不替调用方做校验）。
    """
    if raw is None:
        return []

    if isinstance(raw, str):
        parts: Iterable[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        parts = raw
    else:
        # 既不是字符串也不是容器：当成单个标签（例如 tags=5）。
        # 直接 str() 而不是报错，理由见约定 3。
        parts = [raw]

    out: dict[str, None] = {}
    for item in parts:
        text = str(item).strip()
        if text:
            out.setdefault(text, None)
    return list(out)
