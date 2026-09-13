# -*- coding: utf-8 -*-
"""MCP server —— 查询端。

让任何支持 Model Context Protocol 的 agent 检索本地记忆库。

传输
----
stdio，每行一条 JSON-RPC 2.0 消息。**stdout 是协议通道**，任何日志、警告、
调试信息都必须走 stderr —— 往 stdout 多写一个字符就会破坏握手。

工具描述即接口
--------------
LLM 靠 description 判断「什么时候该调用这个工具」。描述含糊，agent 就不会用；
写清触发场景，它才会在需要项目背景时主动来查。因此下面的 description 是接口
的一部分，不是注释。

启动
----
    python memory.py mcp
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import capture, config, importer, readtext, search, store
from .util import parse_tags
from .version import __version__ as SERVER_VERSION

__all__ = ["SUPPORTED_PROTOCOL_VERSIONS", "DEFAULT_PROTOCOL_VERSION", "TOOLS", "serve"]

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "memory-agent"

# JSON-RPC 错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


TOOLS: list[dict[str, Any]] = [
    {
        "name": "memory_search",
        "description": (
            "检索本地长期记忆库，返回最相关的知识卡摘要。"
            "当需要项目背景、历史决策、之前讨论过的方案、环境配置、踩过的坑，"
            "或任何「我之前是不是记过这件事」的场景时调用。"
            "查不到就说明没记过，不要凭猜测继续。"
            "支持中文词、英文标识符（如 nginx、id_ed25519）、IP、路径片段。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                # 这段描述是**接口契约的一部分**：LLM 靠它决定怎么构造 query。
                # 写成「可以是关键词，也可以是描述性短语」时，调用方大多会传整句问句，
                # 而实测正是整句问句在制造噪音（关键词查询零噪音且首位 100%，
                # 自然语言查询噪音率 33%）。所以这里必须给出**可执行的要求**，
                # 而不是「什么都可以」——含糊的描述等于把检索质量交给运气。
                "query": {
                    "type": "string",
                    "description": (
                        "检索词。**传实体名 / 标识符 / 命令 / 路径片段，不要传整句问句。**"
                        "好的例子：`阿里云`、`id_ed25519`、`sqlite3`、`ecs-prod`、"
                        "`/etc/nginx`、`database is locked`。"
                        "不要传：「之前记过阿里云的内容吗」、"
                        "「帮我找一下数据库相关的记录」——"
                        "完整问句里只有个别词有检索价值，其余词会把结果带偏，"
                        "并且会使匹配档位从精确（AND）降级到放宽（OR），"
                        "而放宽档正是噪音的主要来源。"
                    ),
                },
                "limit": {
                    "type": "number",
                    "description": "返回条数上限，默认 5，最大 20",
                },
                "kind": {
                    "type": "string",
                    "description": "可选，按卡片类型过滤：knowledge / project / mistake / prompt / tool / content",
                },
                "source": {
                    "type": "string",
                    "description": "可选，按来源 agent 过滤",
                },
                "as_of": {
                    "type": "string",
                    "description": (
                        "可选，回溯到**某一天当时有效**的记忆（格式 YYYY-MM-DD）。"
                        "默认只返回当前有效的记忆 —— 被取代的旧事实不会出现在结果里。"
                        "当你要回答「当时是什么」而不是「现在是什么」时用它，"
                        "例如「上周部署在哪台机器」「改之前端口是多少」。"
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_get",
        "description": (
            "读取某张知识卡的完整内容。先用 memory_search 拿到 id，再调用本工具展开全文。"
            "检索结果里的摘要被截断时使用。"
            "长卡会按长度上限分片返回：结果末尾会写明还有多少字符未显示，"
            "并按需给出继续读用的 offset —— 看到「还有 N 字符未显示」时，"
            "若那部分对你有用，就用返回的 offset 再调一次。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "number", "description": "卡片 id，来自 memory_search 结果"},
                "offset": {
                    "type": "number",
                    "description": "从第几个字符开始读，默认 0。分片续读时用上一次返回的 offset",
                },
                "max_chars": {
                    "type": "number",
                    "description": (
                        f"本次最多返回多少字符，默认 {readtext.DEFAULT_MAX_CHARS}；"
                        "0 表示不限长（等于取全文，长卡慎用）"
                    ),
                },
                "full": {
                    "type": "boolean",
                    "description": "true 则不分片，直接返回完整正文（长卡会占用大量上下文）",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "memory_capture",
        "description": (
            "把一条值得长期复用的知识写进记忆库，供以后所有会话和其他 agent 检索。"
            "写入后本卡会被立即索引，不需要再调用 memory_reindex。"
            "触发时机：完成一个任务、解决一个报错、确认一个环境配置、做出一个"
            "影响后续的决策之后。"
            "写入前请自己先蒸馏——你是 LLM，用一两段话把结论写清楚，"
            "不要倒贴整段对话原文。"
            "标题要写成「以后你会用什么词去搜它」，正文要写清事实、命令、路径、"
            "以及为什么这么做。"
            "不要写入：临时状态、一次性的中间过程、密码与私钥等凭据。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "卡片标题，将被检索命中，写成可搜索的事实描述",
                },
                "body": {
                    "type": "string",
                    "description": "蒸馏后的正文：结论、命令、路径、注意事项",
                },
                "kind": {
                    "type": "string",
                    "description": "knowledge / project / mistake / prompt / tool / content（默认 knowledge）",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选标签",
                },
                "source": {
                    "type": "string",
                    "description": "可选来源标记，默认取调用方 clientInfo",
                },
                "on_conflict": {
                    "type": "string",
                    "enum": ["suffix", "reject"],
                    "description": (
                        "标题已存在但正文不同时的处理。"
                        "suffix（默认）另存为 -2 新卡并在结果里回传 conflict 信息；"
                        "reject 直接拒绝写入。"
                        "若你要表达「事实变了」，不要靠这个参数 —— 用取代语义。"
                    ),
                },
                "reject_secrets": {
                    "type": "boolean",
                    "description": (
                        "true 时若正文含疑似凭据（私钥头、AWS/GitHub/Slack token、"
                        "明文密码赋值等）则拒绝写入；默认 false = 只告警不阻断。"
                        "默认不阻断是因为本库的正当用途包含渗透测试记录。"
                    ),
                },
            },
            "required": ["title", "body"],
        },
    },
    {
        "name": "memory_update",
        "description": (
            "修改一张已存在卡片的内容 —— **当这条记忆本身写错了的时候**用"
            "（错别字、错误的路径/端口、漏掉的参数、需要补充说明）。"
            "只改你传进来的字段，其余内容一字不动。"
            "**关键区别**：如果事实本身**变了**（服务迁了地址、端口换了、价格变了），"
            "不要用它 —— 那些要保留「当时是多少」，请改用 memory_supersede，"
            "否则历史会被静默抹掉。"
            "不支持改类型（kind）：类型决定卡片所在目录，改它等于移动文件，"
            "而路径是取代关系与读取统计的锚点；要换类型就先用 memory_supersede 写一张"
            "新类型的新卡，再 memory_delete 旧卡。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "number", "description": "卡片 id，来自 memory_search 结果"},
                "title": {"type": "string",
                          "description": "新标题。**文件名不会变**（路径是取代与统计的锚点）"},
                "body": {"type": "string", "description": "新正文。不给则不动正文"},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "新标签（整体替换；传空数组清空标签）"},
                "kind": {"type": "string",
                         "description": "**只用于显式报错**：不支持改类型，见工具说明"},
                "priority": {"type": "number", "description": "优先级（整数）"},
                "ttl": {"type": "string",
                        "description": "有效期，如 30d / 12h / 2026-10-01；空串表示永不过期"},
                "source": {"type": "string", "description": "来源标记"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "memory_supersede",
        "description": (
            "**事实变了**的时候用它：写一张新卡，并让被取代的旧卡失效。"
            "旧卡文件**仍在磁盘上**（只是不再出现在默认检索里），"
            "所以以后还能回答「当时是多少」—— 这正是它与 memory_update 的分工。"
            "什么时候用：地址/端口/价格/配置项当前值发生了**真实变化**，"
            "而旧值对以后仍有参考价值（迁移记录、故障复盘、变更历史）。"
            "写新卡时请给出完整的新事实（别写成「同上，只是端口改了」），"
            "因为以后检索到它时，旧卡不会一起出现。"
            "已经失效的卡不能再被取代（历史链会断），要改当前有效的那张。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "old_id": {"type": "number",
                           "description": "被取代的旧卡 id，来自 memory_search 结果"},
                "title": {"type": "string", "description": "新卡标题"},
                "body": {"type": "string", "description": "新卡正文（写清完整的新事实）"},
                "kind": {"type": "string",
                         "description": "新卡类型；不给则沿用旧卡的类型"},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "新卡标签"},
                "source": {"type": "string", "description": "新卡来源标记，默认取调用方"},
            },
            "required": ["old_id", "title", "body"],
        },
    },
    {
        "name": "memory_delete",
        "description": (
            "删除一张卡片。**这是软删，不是销毁**：卡片被移到回收站，随时可以恢复，"
            "所以不用担心「删错了就没了」。"
            "什么时候用：这条记忆确实不该留在库里（写错了且没有修正价值、"
            "过期的临时状态、不该记的内容）。"
            "若只是事实变了，请用 memory_supersede —— 那会保留旧值，"
            "而删除会让「曾经是什么」不可查。"
            "回收站不会自动清理，需要时由人来清。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "number", "description": "卡片 id，来自 memory_search 结果"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "memory_stats",
        "description": (
            "查看记忆库概览：卡片总数、类型与来源分布、最近更新。"
            "不确定库里有什么、或想确认检索范围时先调用它。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_reindex",
        "description": (
            "把 Markdown 记忆源的最新改动同步进检索索引。"
            "当确认某事已被记录、但 memory_search 检索不到时调用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rebuild": {
                    "type": "boolean",
                    "description": "true 则清空索引后全量重建，默认 false（增量同步）",
                }
            },
        },
    },
]


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _log(msg: str) -> None:
    """日志只走 stderr。stdout 是协议通道。"""
    sys.stderr.write(f"[memory-agent] {msg}\n")
    sys.stderr.flush()


def _num(value, default: int) -> int:
    """把工具参数转成 int，**保留显式的 0**。

    不能写 ``args.get(x) or default``：``offset=0`` 与 ``max_chars=0`` 都是
    **有意义的值**（0 表示从头读 / 不限长），而它们在布尔上下文里是假值，
    会被 ``or`` 悄悄换成默认值。这个坑实测踩过一次 —— 表现为「续读永远拿回
    第一片」，而且不报错。
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class MemoryServer:
    """工具实现。连接惰性打开，跨调用复用。"""

    def __init__(self) -> None:
        self._conn = None
        self.client_name = "mcp"  # 由 initialize 写入，供 capture 署名

    @property
    def conn(self):
        if self._conn is None:
            self._conn = store.connect(config.db_path())
        return self._conn

    def _index_missing(self) -> bool:
        return not config.db_path().exists()

    # ---------------------------------------------------------- 工具实现

    def memory_search(self, args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            return _err("缺少 query 参数。")

        if self._index_missing():
            return _err(
                f"索引不存在：{config.db_path()}\n"
                f"先执行：python memory.py index   （或调用 memory_reindex）"
            )

        limit = search.clamp_limit(args.get("limit") or 5)

        try:
            # 与 CLI 的 --as-of 共用同一个校验（非法日期明确报错，不静默当成没传）。
            as_of = search.normalize_as_of(args.get("as_of"))
        except ValueError as exc:
            return _err(str(exc))

        hits = search.KeywordSearcher(self.conn).search(
            query,
            limit=limit,
            kind=args.get("kind") or None,
            source=args.get("source") or None,
            as_of=as_of,
        )

        if not hits:
            if as_of:
                return _ok(f"在 {as_of} 那一天，记忆库中没有与「{query}」相关的内容。")
            return _ok(f"记忆库中没有与「{query}」相关的内容。")

        # 标签来自 search.MODE_LABELS（唯一来源）—— 档位的语义定义在那边，
        # 名字就该跟着语义走，不要在这个文件里再写一份。
        mode = search.mode_label(hits[0].matched)
        scope = f", 回溯到 {as_of} 当时有效" if as_of else ""
        lines = [f"命中 {len(hits)} 条  (query={query}, 匹配模式={mode}{scope})", ""]
        for i, h in enumerate(hits, 1):
            # 覆盖率只在放宽档显示：精确档恒为 1.0，写出来只是噪音。
            cover = "" if h.matched in ("all", "all-prefix") else f"  覆盖率={h.coverage:.0%}"
            lines.append(f"[{i}] id={h.card_id}  {h.title}")
            lines.append(f"    {h.rel_path}  kind={h.kind}  source={h.source}{cover}")
            if h.snippet:
                lines.append(f"    {h.snippet}")
            lines.append("")
        # 低置信提示放在末尾：它是对整批结果的判断，不属于某一列。
        # 这段话的作用是让调用方能**自己判断该不该信**，而不是替它重排结果。
        note = search.low_confidence_note(hits[0].matched, hits[0].coverage)
        if note:
            lines.append(note)
            lines.append("")
        lines.append("展开全文：memory_get(id=<id>)")
        return _ok("\n".join(lines))

    def memory_get(self, args: dict) -> dict:
        try:
            card_id = int(args.get("id"))
        except (TypeError, ValueError):
            return _err("缺少有效的 id 参数（数字）。")

        if self._index_missing():
            return _err(f"索引不存在：{config.db_path()}")

        row = store.get_card(self.conn, card_id)
        if row is None:
            return _err(f"找不到 id={card_id} 的卡片。")

        # 取全文才算「读过」。检索列表里出现不算 —— 那只说明它被召回，
        # 不代表内容被用上；把召回算成访问会让统计失去区分度。
        store.record_access(self.conn, row["rel_path"])
        stat = store.get_access_stat(self.conn, row["rel_path"]) or {}
        access_count = stat.get("access_count", 1)

        # 与 CLI 的 show 共用同一份窗口实现 —— 两个入口必须给出一致结果。
        # 注意用 _num 而不是 `or`：offset=0 与 max_chars=0 都是有意义的值。
        if args.get("full"):
            window = readtext.render_window(row["body"], offset=0, max_chars=0)
        else:
            window = readtext.render_window(
                row["body"],
                offset=_num(args.get("offset"), 0),
                max_chars=_num(args.get("max_chars"), readtext.DEFAULT_MAX_CHARS),
            )

        head = (
            f"# {row['title']}\n"
            f"路径：{row['rel_path']}\n"
            f"类型：{row['kind']}　来源：{row['source']}　状态：{row['status']}\n"
            f"标签：{row['tags']}\n"
            f"更新：{row['updated']}　读取次数：{access_count}\n"
            f"长度：{window['length']} 字符，本次返回 {window['returned']}"
            f"（offset={window['offset']}）\n"
            f"{'-' * 60}\n"
        )
        # A2：已失效的卡**全文照常返回**，但必须显式标注。
        # 默认检索不会返回它，所以打开它的人（或 agent）要先看到「这不是当前事实」——
        # 否则会把历史当成现状用，而这种错误是静默的。
        text = ""
        if row["invalid_at"]:
            by = f"，被 {row['superseded_by']} 取代" if row["superseded_by"] else ""
            text += (f"⚠ 这张卡已于 {row['invalid_at']} 失效{by}。"
                     f"默认检索不会再返回它 —— 这是**历史事实**，不是当前状态。"
                     f"要查当时的事实用 memory_search(as_of=<日期>)。\n"
                     f"{'-' * 60}\n")
        if row["supersedes"]:
            text += f"（本卡取代了 {row['supersedes']}）\n{'-' * 60}\n"
        text += head + window["text"]
        if window["has_more"]:
            # 截断必须显式说明并给出续读位置。悄悄砍掉后半段、当成全文返回，
            # 就是「能返回的假成功」—— 调用方会以为卡片就这么短。
            remaining = window["length"] - window["offset"] - window["returned"]
            # 用 ``next_offset=N`` 这种带名字的写法，而不是裸 ``offset=N`` ：
            # 头部那行也有一个 offset=，裸写法会让调用方（和人）解析到错的那个。
            text += (f"\n\n…（还有 {remaining} 字符未显示。"
                     f"续读请再调用 memory_get(id={row['id']}, "
                     f"next_offset={window['next_offset']})）")
        return _ok(text)

    def memory_capture(self, args: dict) -> dict:
        title = str(args.get("title") or "").strip()
        body = str(args.get("body") or "").strip()
        if not title or not body:
            return _err("title 与 body 均为必填。")

        # 与 CLI 的 capture 共用同一个解析实现（util.parse_tags）。
        tags = parse_tags(args.get("tags"))

        vault = config.vault_path()
        result = capture.write_card(
            vault,
            title=title,
            body=body,
            kind=str(args.get("kind") or "knowledge"),
            tags=[str(t) for t in tags],
            source=str(args.get("source") or self.client_name),
            on_conflict=str(args.get("on_conflict") or "suffix"),
            reject_secrets=bool(args.get("reject_secrets")),
        )

        if not result["ok"]:
            return _err(f"未写入：{result['reason']}")

        # 落盘成功后立刻索引**本卡**（单文件），不走全量 sync —— 那是 O(语料)。
        # 索引失败不回滚 Markdown：真相源优先，索引随时可用 index --rebuild 重建。
        indexed, warning = False, ""
        try:
            store.init(self.conn)
            importer.sync_one(self.conn, vault, result["path"])
            store.commit(self.conn)
            indexed = True
        except Exception as exc:
            warning = (
                f"卡片已落盘（{result['path']}），但索引失败：{exc}。"
                f"可调用 memory_reindex 补建。"
            )

        if result["action"] == "unchanged":
            note = "内容相同，已存在，未重复写入。"
        elif indexed:
            note = "已写入并索引本卡，可立即被 memory_search 检索。"
        else:
            note = "已写入磁盘，但未建立索引 —— 见 warning。"

        payload = {
            "action": result["action"],
            "path": result.get("path", ""),
            "indexed": indexed,
            "note": note,
        }
        # 标题撞车必须显式回传：否则调用方只看到「写好了」，却不知道
        # 库里多了一张同标题的卡，以后检索会同时命中两张而无法判断该信哪张。
        if result.get("conflict"):
            payload["conflict"] = True
            payload["existing_path"] = result.get("existing_path", "")
            payload["collision_count"] = result.get("collision_count", 0)
            payload["collision_paths"] = result.get("collision_paths", [])
            payload["suggestion"] = (
                "同标题但内容不同的卡片已存在。若这是对既有事实的修正，"
                "请改用更新；若事实已变而旧值仍需留存，请改用取代。"
            )
        # 敏感内容：卡片已写入，但把风险明确回传给调用方 —— 它才是能改正文的人。
        if result.get("secrets_found"):
            payload["secrets_found"] = result["secrets_found"]
            payload["warnings"] = result.get("warnings", [])
            payload["suggestion_secrets"] = (
                "这条内容里出现了疑似凭据。记忆库会被长期保留、并会被检索召回，"
                "建议改成引用方式（如「密钥见 ~/.ssh/xxx」）后重新写入。"
            )
        if warning:
            payload["warning"] = warning
        return _ok(json.dumps(payload, ensure_ascii=False, indent=2))

    def memory_update(self, args: dict) -> dict:
        """原地改卡（P3-02）。与 CLI 的 ``update`` 共用 ``capture.update_card``。"""
        try:
            card_id = int(args.get("id"))
        except (TypeError, ValueError):
            return _err("缺少有效的 id 参数（数字）。")

        if self._index_missing():
            return _err(f"索引不存在：{config.db_path()}")

        row = store.get_card(self.conn, card_id)
        if row is None:
            return _err(f"找不到 id={card_id} 的卡片。")

        vault = config.vault_path()
        # 「没传」与「传了空值」必须区分：前者=别动这个字段，后者=清空。
        # 一律用 args.get 的默认值就会把「清空标签」变成「没传」。
        result = capture.update_card(
            vault,
            row["rel_path"],
            title=args.get("title") if "title" in args else None,
            body=args.get("body") if "body" in args else None,
            kind=args.get("kind") if "kind" in args else None,
            tags=parse_tags(args.get("tags")) if "tags" in args else None,
            priority=args.get("priority") if "priority" in args else None,
            ttl=args.get("ttl") if "ttl" in args else None,
            source=args.get("source") if "source" in args else None,
        )

        if not result["ok"]:
            return _err(f"未修改：{result['reason']}")

        # indexed 三态，与 CLI 的 update 一致：
        #   true 已重新索引 / false 写盘成功但索引失败 / null 没有字段变化、未写盘
        indexed: bool | None = None
        warning = ""
        if result["changed"]:
            try:
                store.init(self.conn)
                importer.sync_one(self.conn, vault, result["path"])
                store.commit(self.conn)
                indexed = True
            except Exception as exc:
                indexed = False
                warning = (f"卡片已改（{result['path']}），但索引失败：{exc}。"
                           f"可调用 memory_reindex 补建。")

        payload = {"id": card_id, **result, "indexed": indexed}
        if warning:
            payload["warning"] = warning
        if not result["changed"]:
            payload["note"] = "给定的值与卡片现值相同，没有改动（updated 也未刷新）。"
        return _ok(json.dumps(payload, ensure_ascii=False, indent=2))

    def memory_supersede(self, args: dict) -> dict:
        """写新卡并让旧卡失效（P3-02）。与 CLI 的 ``supersede`` 同源。"""
        try:
            old_id = int(args.get("old_id"))
        except (TypeError, ValueError):
            return _err("缺少有效的 old_id 参数（数字）。")

        title = str(args.get("title") or "").strip()
        body = str(args.get("body") or "").strip()
        if not title or not body:
            return _err("title 与 body 均为必填 —— 取代会写出一张新卡，"
                        "它的内容必须写清楚（旧卡以后不会再出现在检索结果里）。")

        if self._index_missing():
            return _err(f"索引不存在：{config.db_path()}")

        old = store.get_card(self.conn, old_id)
        if old is None:
            return _err(f"找不到 id={old_id} 的卡片。")

        vault = config.vault_path()
        # 不给 kind 时沿用旧卡的类型：取代默认是「同一件事变了」，
        # 类型跟着变会让卡片悄悄换目录（目录是路径的一部分）。
        result = capture.supersede_card(
            vault,
            old["rel_path"],
            title=title,
            body=body,
            kind=str(args.get("kind") or old["kind"] or capture.DEFAULT_KIND),
            tags=parse_tags(args.get("tags")),
            source=str(args.get("source") or self.client_name),
        )
        if not result["ok"]:
            return _err(f"未取代：{result['reason']}")

        # **两张卡都要重新索引**：只索引新卡的话，旧卡在索引里仍是「有效」，
        # 默认检索照样返回它，而这个工具返回的是成功。
        indexed, warning = False, ""
        try:
            store.init(self.conn)
            importer.sync_one(self.conn, vault, result["new_path"])
            importer.sync_one(self.conn, vault, result["old_path"])
            store.commit(self.conn)
            indexed = True
        except Exception as exc:
            warning = (f"两张卡都已落盘，但索引失败：{exc}。"
                       f"可调用 memory_reindex 补建。")

        payload = {"old_id": old_id, **result, "indexed": indexed,
                   "note": ("旧卡文件仍在磁盘上、只是标了失效；"
                            "以后要查当时的事实用 memory_search(as_of=<日期>)。")}
        if warning:
            payload["warning"] = warning
        return _ok(json.dumps(payload, ensure_ascii=False, indent=2))

    def memory_delete(self, args: dict) -> dict:
        """软删一张卡（P3-02）。与 CLI 的 ``delete`` 同源 —— 可恢复，不清统计。"""
        try:
            card_id = int(args.get("id"))
        except (TypeError, ValueError):
            return _err("缺少有效的 id 参数（数字）。")

        if self._index_missing():
            return _err(f"索引不存在：{config.db_path()}")

        row = store.get_card(self.conn, card_id)
        if row is None:
            return _err(f"找不到 id={card_id} 的卡片。")

        vault = config.vault_path()
        result = capture.delete_card(vault, row["rel_path"])
        if not result["ok"]:
            return _err(f"未删除：{result['reason']}")

        # 索引里精确摘掉这一行，但**保留 card_stats**（软删可恢复）。
        # 真删（CLI 的 delete --purge）才清统计。
        indexed, warning = False, ""
        try:
            store.init(self.conn)
            store.delete_cards(self.conn, [row["rel_path"]], keep_stats=True)
            store.commit(self.conn)
            indexed = True
        except Exception as exc:
            warning = (f"文件已移入回收站（{result['trash_path']}），但索引未更新：{exc}。"
                       f"可调用 memory_reindex 补建。")

        summary = capture.trash_summary(vault)
        payload = {
            "id": card_id,
            **result,
            "indexed": indexed,
            "trash_count": summary["count"],
            "note": ("这是**软删**：文件在回收站里，未销毁，恢复后读取次数接得上。"
                     "彻底删除需要人在命令行执行 delete --purge。"),
        }
        if warning:
            payload["warning"] = warning
        if summary["count"] >= capture.TRASH_REMIND_THRESHOLD:
            payload["reminder"] = (
                f"回收站已积累 {summary['count']} 张卡。回收站不会自动清理，"
                f"确认不再需要后请让人在命令行执行："
                f"python memory.py delete --purge --older-than 30d"
            )
        return _ok(json.dumps(payload, ensure_ascii=False, indent=2))

    def memory_stats(self, args: dict) -> dict:
        if self._index_missing():
            return _err(f"索引不存在：{config.db_path()}")
        s = store.stats(self.conn)
        payload = {
            "total": s["total"],
            "chars": s["chars"],
            "by_kind": dict(s["by_kind"]),
            "by_source": dict(s["by_source"]),
            "by_status": dict(s["by_status"]),
            "recent": [{"path": p, "updated": u} for p, u in s["latest"]],
            # 访问统计来自独立表，index --rebuild 不会清它。
            # 注意口径：它记的是「被 memory_get 取过全文的次数」，
            # 不是「被写入的次数」，也不是「被检索召回的次数」。
            "most_accessed": store.access_stats(self.conn, limit=5),
            # 回收站单独报：它是**软删**的落点，不自动清理 —— 提醒归提醒，动手要人来。
            "trash": capture.trash_summary(config.vault_path()),
            "db": str(config.db_path()),
            "vault": str(config.vault_path()),
        }
        return _ok(json.dumps(payload, ensure_ascii=False, indent=2))

    def memory_reindex(self, args: dict) -> dict:
        vault = config.vault_path()
        if not vault.is_dir():
            return _err(f"记忆源目录不存在：{vault}")

        rebuild = bool(args.get("rebuild"))
        conn = self.conn
        store.init(conn)
        counts = importer.sync(conn, vault, rebuild=rebuild)
        payload = {
            "mode": "rebuild" if rebuild else "incremental",
            "vault": str(vault),
            "total": store.count_cards(conn),
            **counts,
        }
        return _ok(json.dumps(payload, ensure_ascii=False, indent=2))

    # ---------------------------------------------------------- 分发

    def call(self, name: str, args: dict) -> dict:
        fn = getattr(self, name, None)
        if fn is None or name not in {t["name"] for t in TOOLS}:
            return _err(f"未知工具：{name}")
        try:
            return fn(args or {})
        except Exception as exc:  # 工具内部异常不应中断服务
            return _err(f"工具执行失败：{exc}")


def _force_utf8_streams() -> None:
    """把 stdio 三个流都钉成 UTF-8。

    为什么三个都要：Windows 上 ``sys.stdin`` 默认按**区域编码**（中文系统是
    cp936）解码，而协议里的中文卡片标题与正文是 UTF-8 字节 —— 不解码对，
    轻则乱码入库，重则 ``UnicodeEncodeError: surrogates not allowed`` 直接把
    服务打挂。只改 stdout/stderr 是不够的：写出去的干净，读进来的已经烂了。

    ``errors="surrogateescape"`` 是防弹衣：万一真的来了非法字节，
    宁可留下替换字符，也不让整个进程崩掉。
    """
    for stream, kwargs in (
        (sys.stdin, {"errors": "surrogateescape"}),
        (sys.stdout, {"errors": "surrogateescape"}),
        (sys.stderr, {"errors": "surrogateescape"}),
    ):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", **kwargs)
        except Exception:  # 流被重定向成非文本对象时保持原样
            pass


def serve() -> int:
    """stdio 主循环。

    **客户端提前关掉管道是正常收场，不是错误。** 编辑器 / agent 宿主随时可能
    在会话结束后关闭 stdin/stdout；此时写响应会抛 ``BrokenPipeError``，
    读循环也会拿到 EOF 或同样的异常。原来的写法会让服务在退出时打出一整段
    Python 栈 —— 栈本身没什么害处，但宿主通常把 stderr 当错误信号，
    于是「用户正常关掉了窗口」被记成一次服务崩溃，真正的问题反而被淹没。

    所以这两类情况都当作正常结束，退出码 0。
    """
    _force_utf8_streams()
    server = MemoryServer()
    write = sys.stdout.write
    flush = sys.stdout.flush

    class _ClientGone(Exception):
        """客户端已关闭管道 —— 内部信号，用来跳出循环，不是错误。"""

    def send(msg: dict) -> None:
        try:
            write(json.dumps(msg, ensure_ascii=False) + "\n")
            flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            # ValueError/OSError 覆盖「写到已关闭的文本流」在 CPython 上的各种表现
            # （底层是 EBADF 或 "I/O operation on closed file"）。
            raise _ClientGone() from exc

    _log(f"ready  db={config.db_path()}  vault={config.vault_path()}")

    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                send({"jsonrpc": "2.0", "id": None,
                      "error": {"code": PARSE_ERROR, "message": "Parse error"}})
                continue

            if not isinstance(msg, dict):
                send({"jsonrpc": "2.0", "id": None,
                      "error": {"code": INVALID_REQUEST, "message": "Invalid Request"}})
                continue

            msg_id = msg.get("id")
            method = msg.get("method")
            params = msg.get("params") or {}

            # notification（无 id）不回包
            if msg_id is None:
                continue

            if method == "initialize":
                requested = str(params.get("protocolVersion") or "")
                negotiated = (
                    requested if requested in SUPPORTED_PROTOCOL_VERSIONS
                    else DEFAULT_PROTOCOL_VERSION
                )
                # 记录客户端身份，供 memory_capture 署名
                info = params.get("clientInfo") or {}
                name = str(info.get("name") or "").strip()
                ver = str(info.get("version") or "").strip()
                if name:
                    server.client_name = f"{name}@{ver}" if ver else name
                send({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                }})

            elif method == "ping":
                send({"jsonrpc": "2.0", "id": msg_id, "result": {}})

            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})

            elif method == "tools/call":
                name = str(params.get("name") or "")
                args = params.get("arguments") or {}
                if not isinstance(args, dict):
                    send({"jsonrpc": "2.0", "id": msg_id,
                          "error": {"code": INVALID_PARAMS,
                                    "message": "arguments must be an object"}})
                    continue
                send({"jsonrpc": "2.0", "id": msg_id,
                      "result": server.call(name, args)})

            else:
                send({"jsonrpc": "2.0", "id": msg_id,
                      "error": {"code": METHOD_NOT_FOUND,
                                "message": f"Method not found: {method}"}})

    except _ClientGone:
        _log("client closed the pipe, exiting")
        return 0

    _log("stdin closed, exiting")
    return 0
