# -*- coding: utf-8 -*-
"""读取窗口的长度上限与 offset 语义（P3-04）。

CLI 的 ``show`` 与 MCP 的 ``memory_get`` 共用 ``mcore.readtext`` —— 这两个入口
必须给出一致结果，所以窗口语义在这里钉死，两个入口只验证「确实调了它」。

刻意覆盖的坑：

1. ``has_more`` 表示「没读完」，**不等于**「被砍了」。offset > 0 时，一个完整
   落在尾部的窗口是 ``truncated=False``，值得单独断言。
2. offset 越界不报错，给空窗口 —— 调用方多翻一页是常见行为，报错会打断它。
3. 切片按**字符**而不是字节，中文不会被劈开。
"""

from __future__ import annotations

from mcore import readtext


def test_short_body_is_returned_whole() -> None:
    body = "短正文，一次就能读完。"
    w = readtext.render_window(body)
    assert w["text"] == body
    assert w["length"] == len(body)
    assert w["returned"] == len(body)
    assert w["truncated"] is False
    assert w["has_more"] is False
    assert w["next_offset"] is None


def test_long_body_is_capped_and_reports_remainder() -> None:
    body = "字" * 250
    w = readtext.render_window(body, max_chars=100)

    assert w["text"] == body[:100]
    assert w["returned"] == 100
    assert w["length"] == 250
    assert w["has_more"] is True
    assert w["truncated"] is True
    assert w["next_offset"] == 100
    # 剩下的字符数 = length - offset - returned
    assert w["length"] - w["offset"] - w["returned"] == 150


def test_offset_pages_through_the_whole_body() -> None:
    body = "".join(f"{i:03d}" for i in range(100))  # 300 字符
    collected = ""
    offset = 0
    pages = 0

    while True:
        w = readtext.render_window(body, offset=offset, max_chars=70)
        collected += w["text"]
        pages += 1
        assert pages < 20, "分页不该无限循环"
        if not w["has_more"]:
            assert w["next_offset"] is None
            break
        offset = w["next_offset"]

    assert collected == body, "分页拼接必须还原出完整正文"
    assert pages == 5, pages  # 300 / 70 向上取整


def test_last_page_is_not_marked_truncated() -> None:
    """offset>0 且窗口正好落在尾部时，「没读完」必须是 False。

    这一条对应一个容易写错的定义：把 ``truncated`` 实现成
    「原始正文比 max_chars 长」就会在最后一页误报「还有后续」，
    调用方于是继续翻页、拿到空窗口，白跑一轮。
    """
    body = "abcdefghij"  # 10 字符
    w = readtext.render_window(body, offset=5, max_chars=100)
    assert w["text"] == "fghij"
    assert w["returned"] == 5
    assert w["has_more"] is False
    assert w["truncated"] is False
    assert w["next_offset"] is None


def test_offset_beyond_length_yields_empty_window_without_error() -> None:
    body = "只有十个字符的正文呀"  # 10 字符
    w = readtext.render_window(body, offset=999)
    assert w["text"] == ""
    assert w["offset"] == len(body), "offset 应收敛到 length，而不是原样返回 999"
    assert w["returned"] == 0
    assert w["has_more"] is False
    assert w["next_offset"] is None
    assert w["length"] == len(body)


def test_max_chars_zero_or_negative_means_unlimited() -> None:
    body = "字" * 5000
    for cap in (0, -1, -9999):
        w = readtext.render_window(body, max_chars=cap)
        assert w["text"] == body, cap
        assert w["returned"] == 5000
        assert w["has_more"] is False


def test_cjk_is_split_by_character_not_byte() -> None:
    body = "中文测试" * 10
    w = readtext.render_window(body, max_chars=3)
    assert w["text"] == "中文测"
    assert len(w["text"]) == 3


def test_bad_inputs_fall_back_instead_of_raising() -> None:
    body = "正文内容足够长，用来测试异常输入的回退。"
    # 非数字 offset -> 0
    w = readtext.render_window(body, offset="不是数字")  # type: ignore[arg-type]
    assert w["offset"] == 0
    assert w["text"] == body

    # 非数字 max_chars -> 默认上限（正文很短，所以仍是全文）
    w = readtext.render_window(body, max_chars="大")  # type: ignore[arg-type]
    assert w["text"] == body

    # 负 offset -> 0
    w = readtext.render_window(body, offset=-50)
    assert w["offset"] == 0

    # 天文数字 offset 被钳制，不触发无谓的大切片
    w = readtext.render_window(body, offset=10**12)
    assert w["offset"] <= readtext.MAX_OFFSET


def test_empty_body_is_safe() -> None:
    for body in ("", None):
        w = readtext.render_window(body)  # type: ignore[arg-type]
        assert w["text"] == ""
        assert w["length"] == 0
        assert w["has_more"] is False
        assert w["next_offset"] is None


def test_default_cap_is_the_documented_value() -> None:
    """默认上限是一个对外可见的数字，改它等于改变所有调用方的行为。"""
    assert readtext.DEFAULT_MAX_CHARS == 20000
    body = "字" * 20001
    w = readtext.render_window(body)
    assert w["returned"] == 20000
    assert w["has_more"] is True
    assert w["next_offset"] == 20000
