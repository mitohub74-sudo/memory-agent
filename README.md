# memory-agent

**给 agent 用的记忆层**：把已有的记忆汇总成一份可快速检索的本地索引，同时给没有记忆系统的 agent 提供一个。

## 它解决什么

记忆散落在各处 —— 有的 agent 自带记忆系统（各自的 Markdown、JSON、数据库），有的完全没有，关掉会话就忘光。

memory-agent 做两件事：

1. **汇总** —— 把已有记忆读出来，建成一份本地 SQLite 索引。**原文件不动，只读。**
2. **提供** —— 给没有记忆系统的 agent 一套写入 + 检索能力，通过 MCP 或 CLI 接入。

Markdown 是给人看的。但当记忆成百上千条时，模型靠逐个读文件既找不准也找不快 —— 索引解决的正是这一步。汇总与检索全程在本机完成，内容不出网。

## 设计承诺

| 承诺 | 实现方式 |
|---|---|
| **原文件只读** | 索引端只读取源文件，从不修改、从不删除。真相始终在你自己手里 |
| **真相源与索引分离** | 原格式（Markdown 等）是真相源，SQLite 只是可重建的索引。删库可 `index --rebuild` 重建 |
| **存储区独立于项目** | 记忆在 `~/.memory_agent`（路径可任意指定），代码在仓库。项目更新或程序失误不威胁记忆；重新克隆项目后配好 `config.json` 即可继续访问 |

> ⚠️ **一条如实的例外：访问统计不可重建。**
> `cards`（卡片内容 + 全文索引 + priority/ttl）全部能从 Markdown 重新解析出来，
> 所以删库重建是无损的。但 `card_stats` 表记录的**读取次数与最后读取时间没有第二个来源** ——
> 它一旦丢失就是永久丢失。这也是 `index --rebuild` 会清空 `cards`/`cards_fts`
> 却**特意保留 `card_stats`** 的原因（判据不是「表名像不像索引」，而是
> **能否从真相源重新算出来**）。删掉 `.db` 文件这种损坏下，统计确实会丢。
> 卡片内容本身不受影响。
| **零运行依赖、内容不出网** | 仅用 Python 标准库；不做任何 LLM 调用，蒸馏交给调用方 agent |
| **面向 agent 而非人** | 输出以机器可读为先（`--json`），不做展示层 |

## 实现范围

**已实现**：单源 Markdown 的采集 / 索引 / 检索、MCP 查询端、CLI、检索质量基准。

**尚未实现**：多源整合（当前只接受一个 vault）、Markdown 以外的格式适配。

---

## 架构

```
agent 会话 ──采集端──> 记忆源（Markdown）──索引端──> SQLite ──查询端──> agent
                        真相源 · 只读            (可重建)      (MCP)
```

本仓库实现**采集端**（`mcore/capture.py`）、**索引端**（`mcore/importer.py`）与
**查询端**（`mcore/mcp_server.py`），外加**检索质量基准**（`bench/retrieval_quality.py`）。

---

## 路径

数据默认在 `~/.memory_agent`，与代码仓库完全分离：

```
~/.memory_agent/
├── memory.db           索引库
├── config.json         本机配置（不进仓库）
├── bench_queries.json  检索基准真值（含本机卡片路径，不进仓库）
└── vault/              Markdown 卡片（默认位置）
```

解析优先级：CLI 参数 > 环境变量 > `config.json` > 默认值。

| | CLI | 环境变量 | config.json |
|---|---|---|---|
| 数据根 | — | `MEMORY_AGENT_HOME` | — |
| 索引库 | `--db` | `MEMORY_AGENT_DB` | `db` |
| vault | `--vault` | `MEMORY_AGENT_VAULT` | `vault` |

## 安装

需要 Python 3.10+，运行期**不安装任何第三方依赖**：

```bash
pip install .
memory --help
```

开发时可安装测试与静态检查工具（只属于开发依赖，不进入运行路径）：

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
python -m ruff check .
```

即使不安装包，也保留从仓库直跑的路径：

```bash
python memory.py mcp
python tests/test_mcp.py
```

---

## 接口

```bash
python memory.py paths                # 当前生效路径
python memory.py index [--rebuild]    # 同步 / 重建索引
python memory.py search <query>       # 检索
python memory.py capture --title T --body B   # 写入一张卡片
python memory.py stats                # 统计
python memory.py show <id>            # 卡片全文（长卡默认分片，见下）
python memory.py mcp                  # 启动 MCP server
```

所有命令支持 `--json`。退出码：`0` 成功，`1` 无结果，`2` 环境错误，
`3` 标题撞车被拒绝（`--on-conflict reject`）。

### 长卡分片读取

卡片会长（蒸馏出来的会话卡常有数千字符），所以取全文的**默认带长度上限**：

```bash
python memory.py show 8                      # 默认最多 20000 字符
python memory.py show 8 --offset 20000       # 续读下一页
python memory.py show 8 --max-chars 500      # 自定义窗口
python memory.py show 8 --full               # 不分片，返回完整正文
```

返回值里有 `offset` / `length` / `returned` / `has_more` / `next_offset`，
文本末尾会写明「还有 N 字符未显示」以及续读要用的 `next_offset`。

**截断永远显式**：宁可多一行提示，也不静默砍掉后半段再当成全文返回 ——
那会让调用方以为卡片就这么短。MCP 的 `memory_get` 参数与语义完全一致
（`offset` / `max_chars` / `full`），两个入口共用 `mcore/readtext.py` 一份实现。
`max_chars=0` 等价于 `--full`。

`search -n` 的取值范围是 `[1, 20]`，越界会被钳制 —— SQLite 的 `LIMIT -1` 表示**无上限**，
不钳制就会把整张表倒出来。

`search` 输出字段：`id` `path` `title` `kind` `source` `score` `matched` `snippet`。

---

## 采集端

```bash
python memory.py capture --title "标题" --body "正文" [--kind knowledge] [--tags a,b]
# 正文也可从 stdin 读
```

写入一张 Markdown 卡片到 vault，**落盘后立刻索引本卡** —— 不需要再手动跑 `index`，
写完即可被 `search` 检索。frontmatter 与既有卡片格式一致，因此新旧卡片共存、互相可检索。

### 蒸馏交给调用方，本模块不调 LLM

记忆的价值在于压缩。把整段会话原样倒进 vault 只会制造噪音，检索时反而更难找到重点。

所以 `capture.py` **不做任何 LLM 调用** —— 蒸馏由调用方完成：agent 本身就是 LLM，
让它先想清楚「什么值得记、怎么写以后才看得懂」，再交给这里落盘。

这样做的收益：**零额外成本、零 API key、内容不出网**。

### 行为

| 情况 | 结果 |
|---|---|
| 新卡片 | 写入并索引本卡，返回 `created` + `indexed: true` |
| 标题与正文都相同 | 跳过，返回 `unchanged`（幂等） |
| 正文 < 20 字符 | 拒绝，返回 `rejected` |
| **标题相同、正文不同** | **不覆盖**已有卡；默认另存为 `-2`，并回传 `conflict: true` + `existing_path` + `collision_paths` |
| `--on-conflict reject` 下的标题撞车 | 不写盘，返回 `conflict`，退出码 **3** |
| 正文/标题含疑似凭据 | **仍然写入**，但回传 `secrets_found` + `warnings`（默认只告警） |
| `--reject-secrets` 下含疑似凭据 | 不写盘，返回 `rejected`，退出码 **1** |
| 索引失败 | 卡片仍在磁盘（真相源优先），返回 `indexed: false` + `warning` |

### 敏感内容：默认只告警，不阻断

正文与标题都会扫一遍常见凭据形态：私钥头（`-----BEGIN ... PRIVATE KEY-----`）、
AWS Access Key ID、GitHub / Slack token、Google API Key、OpenAI 风格 key、JWT、
以及 `password = xxx` 这类明文赋值。

**命中后默认照常写入**，只在结果里回传 `secrets_found` 与 `warnings`。
需要更严时加 `--reject-secrets`（MCP 的 `memory_capture` 传 `reject_secrets: true`）。

**为什么默认不拦**：本库的正当用途就包含渗透测试记录，而这类记录里天然会出现密钥、
连接串、凭据 —— 硬拒会把项目本身的用途一起拒掉。这是刻意的取舍，不是漏做。

两条实现上的约束，都是有意为之：

1. **告警不复述凭据**。只报「命中了哪一类 + 位置区间」，不回显原文 ——
   把密钥抄进告警里等于又写了一遍到返回值与日志里，反而扩大暴露面。
2. **宁可漏报，不要误报**。`~/.ssh/id_ed25519`、`密钥见 ~/.ssh/xxx`、
   `token: 见上一条记录` 这类引用式写法**不会**被标记；赋值式还要求值同时含字母与数字
   （`password: 已改成用密钥登录` 是说明文字，不是凭据）。误报多起来，告警会被所有人忽略，
   那比没有告警更坏。

> 这是**提醒**，不是安全边界。真要严格管控凭据请用专门的扫描工具，不要让「顺手的正则」
> 承担安全职责。

**为什么标题撞车要显式回传**：同标题不同正文过去会**静默**多出一个 `slug-2.md` ——
调用方只看到「写好了」，不知道库里已经有两张同标题的卡，此后检索会同时命中两张，
而没有任何信息能判断该信哪张。现在冲突会出现在返回值里，并且**永远不覆盖**已有卡片。

想表达「事实变了」不要用 `--on-conflict`：修正既有事实用 `update`，
事实已变而旧值仍需留存用 `supersede`（见 ROADMAP 阶段 3）。
`on_conflict` 只回答「标题撞了怎么办」这一个问题。

索引只作用于**刚写入的这一张卡**，不触发全量同步（全量是 O(语料) 的）。
索引失败**不回滚** Markdown —— 真相源优先，索引随时可用 `index --rebuild` 重建。

CLI 的 `capture` 与 MCP 的 `memory_capture` 行为一致：两个入口，同一种结果。

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
| `memory_search` | 检索记忆，返回摘要 + id。参数 `query` `limit`（上限 20）`kind` `source` |
| `memory_get` | 用 id 取卡片全文（长卡分片返回，见下） |
| `memory_capture` | **写入**一条知识并立即索引本卡。参数 `title` `body` `kind` `tags` |
| `memory_stats` | 库概览（总数、类型/来源分布、最近更新） |
| `memory_reindex` | 手动补建索引（增量或 `rebuild` 全量）。正常写入已自动索引，此工具用于索引丢失或外部改动后补建 |

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

注意前缀是**单向**的：搜 `sqlite` 能命中 `sqlite3`，反过来搜 `sqlite3` 命中不了
`sqlite`（`*` 只能匹配「以查询词为前缀的 term」）。

- **BM25** 排序。SQLite 的 `bm25()` 返回负值，对外取负使「越大越相关」。
- 已知局限（都是固有特性，不是缺陷）：
  - **要求字面一致**。`sqlite` 能靠前缀档命中 `sqlite3`，但 `userById` 搜不到
    `getUserById` —— 片段在词中间，前缀够不着。
  - **词汇鸿沟无解**。查询说「本地装了什么模型」，卡片写「本机 Ollama 已装模型清单」，
    字面零重叠，任何词法手段都救不回来。这是向量检索要解决的问题，见「扩展」。

> 曾给 OR 档加过「先按命中词数重排、再按 BM25」的逻辑，动机是怀疑 OR 档 BM25 失真。
> 基准实测**证伪并发现它是负优化**：唯一受影响的查询「本地装了什么模型」，目标卡从
> 第 4 名被推到第 9 名，封顶 P@5 由 66.7% 降到 55.6%，其余查询无变化。已移除。
> 不要再加回来，除非基准显示正收益。

---

## 检索质量基准

```bash
python memory.py bench                      # 人类可读
python memory.py bench --json               # 机器可读
python memory.py bench --save base.json     # 存基线
python memory.py bench --baseline base.json # 对比；任一指标退化则退出码 1
```

真值文件在**数据目录**（`~/.memory_agent/bench_queries.json`），不在仓库里 ——
它包含本机卡片路径。实现见 `bench/retrieval_quality.py`。

**三条硬规定**，每一条都对应一次踩过的坑：

1. **真值按 `rel_path` 记录，不按整数 id。** 用 id 记过一次，索引重建后 rowid 重排，
   真值全部错位（卡 12 从「本机 Ollama」变成「渗透测试复盘」），据此得出的结论是假的。
2. **真值是一组「可接受卡」，不是一张「标准卡」。** 只认一张会把「返回了另一个同样
   正确的答案」误判为失败。
3. **真值为空的查询不计入精确率。** 语料里没有这个词，返回空才是正确行为。

指标用**封顶精确率**：分母取 `min(K, 相关卡数)`。「相关卡只有 2 张，取满 top5 也填不满」
是相关卡用完了，不是返回了噪音 —— 不封顶的话这个区别看不出来。

**本机实测（49 张卡，19 条查询，2026-09-12）**：

| 组 | 查询类型 | 条数 | 首位可接受 | 封顶 P@5 | 噪音率@5 | 实际档位 |
|---|---|---|---|---|---|---|
| A | 关键词（`sqlite` / `阿里云` / `运维`…） | 11 | **100%** | **100%** | **0%** | 全部 `all`，从不降级 |
| B | 自然语言（「本地装了什么模型」…） | 4 | 50% | 66.7% | 33.3% | 全部 `any` |

结论：**关键词查询零噪音，且根本走不到 OR 档**；噪音只出现在自然语言查询里。
另有 4 条查询（`sqlite3`/`tokenize`/`bm25`/`mcp`）在语料里不存在，返回空是正确的。

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

在那之前，四档降级已经覆盖绝大多数场景，而向量的成本是实打实的：
本地模型文件、ONNX runtime 或 sqlite-vec 依赖、每次 `capture` 都要算嵌入、
重建索引显著变慢。接口已就位，**推迟的代价≈0**。

### 试过并否决的方案

**FTS5 `trigram` 分词器**（SQLite 3.34+ 内置，零依赖，看着像个便宜的中间档）。
本机实测否决，三条理由：

| 维度 | 实测结果 |
|---|---|
| 2 字中文词 | **全部 0 召回** —— `词云`/`分词`/`快照`/`运维`/`记忆` 一个都搜不到（trigram 要求查询 ≥ 3 字符） |
| 自然语言查询 | **全部返回空**，比现状更差 |
| 唯一优势 | 英文中段碎片（`2551` 命中 `ed25519`），但前缀档已覆盖 `ed2551` → `ed25519` 这类常见情况 |
| 索引体积 | 0.88x bigram，没有优势 |

对一个**中文优先**的记忆库，第一条就是致命的。

**同义词/别名表**（纯词法，零依赖）。用人工编写的别名表扩展查询后重测，
B 类 4 条查询**仍然 0/4**。词法路线到此为止 —— 而且那张表是「看过测试查询之后」
写的，属于对测试集过拟合，真实泛化只会更差。

### 使用上的缓解手段（比上向量便宜得多）

1. **工具描述里要求传实体名/标识符，不要传整句问句。** 实测关键词查询零噪音、
   首位 100% 正确，而自然语言查询噪音率 33% —— 消灭问句等于消灭噪音来源。
   这与项目「蒸馏交给调用方」的原则同源。
2. **让置信度显式化**：`Hit` 带 `matched` 档位，调用方看到 `any`/`any-prefix`
   就知道这批结果不可信，可改用关键词重试。

---

## 环境

Python 3.10+（已在 3.13、3.14 验证）。需 SQLite 支持 FTS5，Python 自带版本均满足。

---

## 许可证

[MIT](LICENSE)
