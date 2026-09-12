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

        hits = search.KeywordSearcher(self.conn).search(
            query,
            limit=limit,
            kind=args.get("kind") or None,
            source=args.get("source") or None,
        )

        if not hits:
            return _ok(f"记忆库中没有与「{query}」相关的内容。")

        # 标签来自 search.MODE_LABELS（唯一来源）—— 档位的语义定义在那边，
        # 名字就该跟着语义走，不要在这个文件里再写一份。
        mode = search.mode_label(hits[0].matched)
        lines = [f"命中 {len(hits)} 条  (query={query}, 匹配模式={mode})", ""]
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
        text = head + window["text"]
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

        tags = args.get("tags") or []
        if not isinstance(tags, list):
            tags = [t.strip() for t in str(tags).split(",") if t.strip()]

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
    """stdio 主循环。"""
    _force_utf8_streams()
    server = MemoryServer()
    write = sys.stdout.write
    flush = sys.stdout.flush

    def send(msg: dict) -> None:
        write(json.dumps(msg, ensure_ascii=False) + "\n")
        flush()

    _log(f"ready  db={config.db_path()}  vault={config.vault_path()}")

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

    _log("stdin closed, exiting")
    return 0
