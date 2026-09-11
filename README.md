# memory-agent

面向大模型的本地记忆检索。把散落的 Markdown 记忆卡索引成可快速检索的 SQLite。

**主体服务对象是 agent，不是人。** 输出以机器可读为先，不做展示层。

---

## 架构

```
agent 会话 ──采集端──> Markdown vault ──索引端──> SQLite ──查询端──> agent
                          (真相源)                 (可重建)      (MCP)
```

本仓库实现**采集端**（`mcore/capture.py`）、**索引端**（`mcore/importer.py`）与
**查询端**（`mcore/mcp_server.py`）。

## 核心约束

1. **Markdown 是真相源**。SQLite 只是索引，删掉可用 `index --rebuild` 重建，数据零损失。
2. **代码与数据分离**。仓库内不存任何记忆，也不硬编码本机路径。
3. **零外部依赖**。仅用 Python 标准库。

---

## 路径

数据默认在 `~/.memory_agent`，与代码仓库完全分离：

```
~/.memory_agent/
├── memory.db      索引库
├── config.json    本机配置（不进仓库）
└── vault/         Markdown 卡片（默认位置）
```

解析优先级：CLI 参数 > 环境变量 > `config.json` > 默认值。

| | CLI | 环境变量 | config.json |
|---|---|---|---|
| 数据根 | — | `MEMORY_AGENT_HOME` | — |
| 索引库 | `--db` | `MEMORY_AGENT_DB` | `db` |
| vault | `--vault` | `MEMORY_AGENT_VAULT` | `vault` |

## 接口

```bash
python memory.py paths                # 当前生效路径
python memory.py index [--rebuild]    # 同步 / 重建索引
python memory.py search <query>       # 检索
python memory.py capture --title T --body B   # 写入一张卡片
python memory.py stats                # 统计
python memory.py show <id>            # 卡片全文
python memory.py mcp                  # 启动 MCP server
```

所有命令支持 `--json`。退出码：`0` 成功，`1` 无结果，`2` 环境错误。

`search` 输出字段：`id` `path` `title` `kind` `source` `score` `matched` `snippet`。

---

## 采集端

```bash
python memory.py capture --title "标题" --body "正文" [--kind knowledge] [--tags a,b]
# 正文也可从 stdin 读
```

写入一张 Markdown 卡片到 vault。frontmatter 与既有卡片格式一致，因此新旧卡片共存、
互相可检索。

### 蒸馏交给调用方，本模块不调 LLM

记忆的价值在于压缩。把整段会话原样倒进 vault 只会制造噪音，检索时反而更难找到重点。

所以 `capture.py` **不做任何 LLM 调用** —— 蒸馏由调用方完成：agent 本身就是 LLM，
让它先想清楚「什么值得记、怎么写以后才看得懂」，再交给这里落盘。

这样做的收益：**零额外成本、零 API key、内容不出网**。

### 行为

| 情况 | 结果 |
|---|---|
| 新卡片 | 写入，返回 `created` |
| 标题与正文都相同 | 跳过，返回 `unchanged`（幂等） |
| 正文 < 20 字符 | 拒绝，返回 `rejected` |

- 文件名由标题生成，中文原样保留（如 `nginx-站点根目录位置.md`）
- 重名自动加序号后缀（`-2`、`-3`），**不覆盖已有卡片**
- 原子写入（临时文件 + 改名），中途失败不会留下半截文件
- `kind` 决定归入哪个分类目录：

| kind | 目录 | kind | 目录 |
|---|---|---|---|
| `system` | `00-System` | `prompt` | `05-Prompts` |
| `project` | `02-Projects` | `business` | `06-Business` |
| `knowledge` | `03-Knowledge` | `tool` | `07-Tools` |
| `content` | `04-Content` | `mistake` | `08-Mistakes` |

---

## MCP 查询端

```bash
python memory.py mcp
```

stdio 传输，每行一条 JSON-RPC 2.0 消息。协议版本 `2025-06-18` / `2025-03-26` /
`2024-11-05`（按请求协商，未知版本回落到 `2024-11-05`）。

**stdout 是协议通道，日志一律走 stderr。** 往 stdout 多写一个字符就会破坏握手。

### 工具

| 工具 | 用途 |
|---|---|
| `memory_search` | 检索记忆，返回摘要 + id。参数 `query` `limit` `kind` `source` |
| `memory_get` | 用 id 取卡片全文 |
| `memory_capture` | **写入**一条知识。参数 `title` `body` `kind` `tags` |
| `memory_stats` | 库概览（总数、类型/来源分布、最近更新） |
| `memory_reindex` | 同步记忆源最新改动到索引，参数 `rebuild` |

工具描述是接口的一部分 —— LLM 靠它判断何时调用，写得含糊 agent 就不会用。

### 客户端配置

```json
{
  "mcpServers": {
    "memory-agent": {
      "command": "python",
      "args": ["/path/to/memory-agent/memory.py", "mcp"]
    }
  }
}
```

### 测试

```bash
python tests/test_mcp.py
```

覆盖握手、版本协商、工具列表、五个工具调用、错误码（-32700 / -32601 / isError）、
**采集端闭环**（写入 → 幂等 → 索引 → 检索到，隔离在临时 vault 中运行），
以及 **stdout 纯净性** —— 逐行校验输出全部是合法 JSON-RPC。

---

## 中文分词（关键设计）

**SQLite FTS5 的内置分词器都不能用于中文。** 本机实测（SQLite 3.53.1）：

| 查询词 | 字数 | `trigram` | `unicode61` | bigram 预分词 |
|---|---|---|---|---|
| 私钥 | 2 | 0 | 0 | **1** |
| 阿里云 | 3 | 1 | 1 | **1** |
| `ecs-prod` | 8 | 语法报错 | 语法报错 | **1** |

- `unicode61` 把连续中文当作**单个 token**（「登录使用私钥」是一个词），搜不到子串。
- `trigram` 要求查询**至少 3 字符**，中文词多为 2 字，因此大量漏召回。

**做法**：入库前把中文按 2 字滑窗切分（`阿里云` → `阿里 里云`），英文保留
`_ . / -` 使 `ecs-prod`、`id_ed25519` 这类标识符保持完整。见 `mcore/tokenize.py`。

另注：FTS5 的 `MATCH` 语法中 `-` 会被解析为列名过滤，含连字符的词必须加引号。
`tokenize.to_query_expr()` 已统一处理。

---

## 检索行为

- 默认 **AND**；若零召回则自动降级 **OR**，结果中 `matched` 字段标注实际模式。
- **BM25** 排序。SQLite 的 `bm25()` 返回负值，对外取负使「越大越相关」。
- 已知局限：关键词检索**要求字面一致**。这是固有特性，不是缺陷。

## 扩展

检索层抽象为 `mcore.search.Searcher` 协议。加向量检索时新增一个实现即可，
CLI 与调用方无需改动；`cards.embedding` 列已预留。

> 若用云端 API 计算嵌入向量，卡片内容会发送给模型厂商。**必须使用本地模型。**

---

## 环境

Python 3.10+（已在 3.13、3.14 验证）。需 SQLite 支持 FTS5，Python 自带版本均满足。

---

## 许可证

[MIT](LICENSE)
