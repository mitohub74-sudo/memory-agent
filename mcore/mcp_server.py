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

from . import capture, config, importer, search, store
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
                "query": {
                    "type": "string",
                    "description": "检索词。可以是关键词，也可以是描述性短语",
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

        _MODE_LABEL = {
            "all": "AND 精确",
            "all-prefix": "AND 前缀",
            "any": "OR（已放宽）",
            "any-prefix": "OR + 前缀（已放宽）",
        }
        mode = _MODE_LABEL.get(hits[0].matched, hits[0].matched)
        lines = [f"命中 {len(hits)} 条  (query={query}, 匹配模式={mode})", ""]
        for i, h in enumerate(hits, 1):
            lines.append(f"[{i}] id={h.card_id}  {h.title}")
            lines.append(f"    {h.rel_path}  kind={h.kind}  source={h.source}")
            if h.snippet:
                lines.append(f"    {h.snippet}")
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

        head = (
            f"# {row['title']}\n"
            f"路径：{row['rel_path']}\n"
            f"类型：{row['kind']}　来源：{row['source']}　状态：{row['status']}\n"
            f"标签：{row['tags']}\n"
            f"更新：{row['updated']}　读取次数：{access_count}\n"
            f"{'-' * 60}\n"
        )
        return _ok(head + row["body"])

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
        )

        if not result["ok"]:
            return _err(f"未写入：{result['reason']}")

        # 落盘成功后立刻索引**本卡**（单文件），不走全量 sync —— 那是 O(语料)。
        # 索引失败不回滚 Markdown：真相源优先，索引随时可用 index --rebuild 重建。
        indexed, warning = False, ""
        try:
            store.init(self.conn)
            importer.sync_one(self.conn, vault, result["path"])
            self.conn.commit()
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


def serve() -> int:
    """stdio 主循环。"""
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
