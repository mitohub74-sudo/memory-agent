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

**做法**：入库前把中文按 2 字滑窗切分（`阿里云` → `阿里 里云`），英文整词小写化。
见 `mcore/tokenize.py`。

⚠️ **一个容易误解的点**：`cards_fts` 用的是 `tokenize='unicode61'`，所以预分词的
结果会被 FTS5 **再切一遍** —— `-` `_` `.` `/` 都是分隔符。用 `fts5vocab` 查实际
词表可以确认：`id_ed25519` 存进去是 `id` + `ed25519` 两个 token，不是一整串。
所以「保留 `_ . / -` 使标识符保持完整」这个说法**不成立** —— 它只是让查询侧把
整串转成相邻短语，因此整串仍能命中；但搜 `id` 同样会命中。

另注：FTS5 的 `MATCH` 语法中 `-` 会被解析为列名过滤，含连字符的词必须加引号。
`tokenize.to_query_expr()` 已统一处理。

---

## 检索行为

匹配档位从精确到宽松，**前一档零召回才降级** —— 保证精确匹配的既有行为不被
放宽匹配污染：

| 档位 | 含义 | `matched` |
|---|---|---|
| 1 | AND 精确：全部词整词命中 | `all` |
| 2 | AND 前缀：全部词前缀命中 | `all-prefix` |
| 3 | OR 精确：任一整词命中 | `any` |
| 4 | OR 前缀：任一前缀命中 | `any-prefix` |

**为什么需要前缀档**：FTS5 的 `MATCH` 是**整词**匹配，正文里写了 `sqlite3` 就搜不到
`sqlite`，写了 `requests` 就搜不到 `request`。代码类内容里这种后缀差异极常见。
前缀只对长度 ≥ 2 的词生效 —— 实测 `"a"*` 在 49 张卡的库里命中 45 张，单字前缀纯噪音。

**OR 档位会重排**：OR 下 BM25 会被长文档和高频词主导，出现「命中词更少却排更前」的
情况。所以 OR 档位多取候选，先按「命中了几个查询词」排序，再按 BM25，最后截断。

- **BM25** 排序。SQLite 的 `bm25()` 返回负值，对外取负使「越大越相关」。
- 已知局限（都是固有特性，不是缺陷）：
  - **要求字面一致**。`sqlite` 能靠前缀档命中 `sqlite3`，但 `userById` 搜不到
    `getUserById` —— 片段在词中间，前缀够不着。
  - **词汇鸿沟无解**。查询说「本地装了什么模型」，卡片写「本机 Ollama 已装模型清单」，
    字面零重叠，任何词法手段都救不回来。这是向量检索要解决的问题，见「扩展」。

## 扩展

检索层抽象为 `mcore.search.Searcher` 协议。加向量检索时新增一个实现即可，
CLI 与调用方无需改动；`cards.embedding` 列已预留。

> 若用云端 API 计算嵌入向量，卡片内容会发送给模型厂商。**必须使用本地模型。**
> 本库含服务器信息、凭据、渗透测试记录，内容不能出网。

**什么时候才值得上向量**（满足任一）：

- 卡片数 **> 1000**，关键词召回的候选池开始失控；
- 反复出现「明明记过但搜不到」，且措辞差异属于**同义/近义**而非词形差异
  （词形差异前缀档已经覆盖）；
- 需要**跨语言**检索（中文查询找英文卡片）。

在那之前，前缀档 + 覆盖数重排已经覆盖绝大多数场景，而向量的成本是实打实的：
本地模型文件、ONNX runtime 或 sqlite-vec 依赖、每次 `capture` 都要算嵌入、
重建索引显著变慢。接口已就位，**推迟的代价≈0**。

---

## 环境

Python 3.10+（已在 3.13、3.14 验证）。需 SQLite 支持 FTS5，Python 自带版本均满足。

---

## 许可证

[MIT](LICENSE)
