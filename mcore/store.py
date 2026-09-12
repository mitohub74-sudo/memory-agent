# -*- coding: utf-8 -*-
"""SQLite 存储层：schema 定义、连接管理、卡片读写。

设计原则
--------
1. **Markdown 是真相源**，本库只是【可重建的索引】。任何时候都可以
   删掉 .db 文件，用 ``memory.py index --rebuild`` 重建。
2. **schema 带版本号**，为将来迁移留路。
3. **cards.embedding 列已预留**（BLOB）。将来加向量检索时无需改表结构，
   只需新增一个 Searcher 实现并回填该列。

表结构
------
meta        键值对，存 schema_version 等元信息
cards       卡片主表，rel_path 作为业务主键
cards_fts   FTS5 全文索引，存 bigram 分词后的文本
card_stats  访问统计（index-side 辅助数据，重建索引**不清**）
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "MIGRATIONS",
    "BUSY_TIMEOUT_MS",
    "connect",
    "init",
    "upsert_card",
    "delete_cards",
    "get_card",
    "iter_cards",
    "count_cards",
    "stats",
    "record_access",
    "access_stats",
    "get_access_stat",
    "retry_on_locked",
    "is_locked_error",
    "commit",
]

# SQLite 的默认 busy 处理是「立刻失败」。多个进程同时写（例如两个 agent 会话
# 同时 capture）就会随机抛 "database is locked" —— 而且它是**间歇性**的，
# 手动重跑往往就好了，于是最容易被当成偶发噪音放过。
#
# 为什么是 5000ms：足够跨过一次正常写入的持锁时间（单卡写入是毫秒级），
# 又不至于让真正卡死的情况无限等下去。P3-01 的全量迁移也是在这个窗口内完成的。
BUSY_TIMEOUT_MS = 5000

# SQLite 的参数个数上限（``SQLITE_MAX_VARIABLE_NUMBER``）。3.32 起默认 32766，
# 但更早的版本是 999，而本项目的底线是 Python 3.10 自带的 SQLite —— 按 999 切分
# 才是安全的。批量化删除时用它算分块大小。
SQLITE_MAX_VARIABLES = 999

# 锁重试计划（秒）。**这不是随手取的退避**，而是针对一个具体机制的修正：
# ``busy_timeout`` 只在「等一个会自己释放的锁」时生效。而 ``_init_once`` 里
# 「先读 meta 拿 schema_version、再执行 DDL」是**读事务升级为写事务**；
# 一旦此时别的进程正持有写锁，SQLite 会**立即**返回 SQLITE_BUSY，
# **不调用 busy handler**（升级不能等待，否则就成了死锁）。
#
# 所以第一次重试必须等得比「另一个进程建完表」更久 —— 几十毫秒不够。
# 实测证据：原计划 [0.05, 0.1, 0.2] 在 2 个进程同时启动时**每轮必然有 1 个失败**；
# 把 busy_timeout 从 5 秒提到 30 秒**毫无改善** —— 这正好证明根本没走 busy handler，
# 而不是「锁等得不够久」。
#
# 每次重试都会重头跑 `_init_once`：如果对手已经把表建好，重试就直接走只读快路径返回。
_LOCK_BACKOFF_SECONDS: tuple[float, ...] = (0.3, 0.8, 2.0, 4.0)
_LOCK_RETRIES = len(_LOCK_BACKOFF_SECONDS)

SCHEMA_VERSION = 2

# 迁移表：MIGRATIONS[v] 是把 schema 从 v 升到 v+1 的 SQL 脚本。
# 每一步都必须**只做增量**（ADD COLUMN / CREATE TABLE IF NOT EXISTS），
# 因为它是跑在已有数据的库上的，而那个库不是可丢弃的临时产物。
MIGRATIONS: dict[int, str] = {
    # v1 → v2：卡片生命周期所需的列，以及访问统计表。
    1: """
ALTER TABLE cards ADD COLUMN priority   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cards ADD COLUMN ttl        TEXT    NOT NULL DEFAULT '';
ALTER TABLE cards ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS card_stats (
    rel_path      TEXT    PRIMARY KEY,
    access_count  INTEGER NOT NULL DEFAULT 0,
    last_accessed INTEGER NOT NULL DEFAULT 0
);
""",
}

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id           INTEGER PRIMARY KEY,
    rel_path     TEXT    NOT NULL UNIQUE,        -- 相对 vault 的路径，业务主键
    title        TEXT    NOT NULL DEFAULT '',
    kind         TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT '',
    source       TEXT    NOT NULL DEFAULT '',
    tags         TEXT    NOT NULL DEFAULT '[]',  -- JSON 数组
    created      TEXT    NOT NULL DEFAULT '',
    updated      TEXT    NOT NULL DEFAULT '',
    body         TEXT    NOT NULL DEFAULT '',    -- 去掉 frontmatter 后的正文
    content_hash TEXT    NOT NULL DEFAULT '',    -- 增量索引用
    priority     INTEGER NOT NULL DEFAULT 0,     -- 优先级（frontmatter: priority）
    ttl          TEXT    NOT NULL DEFAULT '',    -- 原始 TTL 写法（frontmatter: ttl）
    expires_at   INTEGER NOT NULL DEFAULT 0,     -- 解析后的过期时刻（unix 秒），0 = 永不过期
    embedding    BLOB                            -- 预留：向量检索
);

CREATE INDEX IF NOT EXISTS idx_cards_kind     ON cards(kind);
CREATE INDEX IF NOT EXISTS idx_cards_status   ON cards(status);
CREATE INDEX IF NOT EXISTS idx_cards_source   ON cards(source);
CREATE INDEX IF NOT EXISTS idx_cards_updated  ON cards(updated);
CREATE INDEX IF NOT EXISTS idx_cards_priority ON cards(priority);

CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
    title,
    body,
    tokenize='unicode61'
);

-- 访问统计刻意**不放进 cards**：cards 是可重建投影，index --rebuild 会清空它，
-- 统计一旦跟着走就等于「重建即丢」。用 rel_path 作主键而不是 id ——
-- id 是 rowid，重建后必然重排（这正是 bench 真值不能用 id 记录的同一个坑）。
CREATE TABLE IF NOT EXISTS card_stats (
    rel_path      TEXT    PRIMARY KEY,
    access_count  INTEGER NOT NULL DEFAULT 0,
    last_accessed INTEGER NOT NULL DEFAULT 0
);
"""


def current_journal_mode(conn: sqlite3.Connection) -> str:
    """读当前 journal_mode（只读查询）。"""
    row = conn.execute("PRAGMA journal_mode").fetchone()
    return str(row[0] if row is not None else "").lower()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）数据库连接。

    ``isolation_level=None`` 关掉 Python sqlite3 的**隐式事务控制**，改为完全显式。
    三个理由，缺一不可：

    1. 隐式模式下，``PRAGMA`` / ``CREATE`` 这类语句会偷偷开启一个事务，我们再去
       ``BEGIN IMMEDIATE`` 就会撞上 ``cannot start a transaction within a transaction``；
    2. 隐式模式会让「读一句、写一句」悄悄变成「读事务升级写事务」，
       也就是 WAL 下会返回 ``SQLITE_BUSY_SNAPSHOT`` 的那个坑（见 ``_init_once``）；
    3. 写入方究竟什么时候提交，变得取决于「上一次执行了什么语句」，
       而这类推理错误的表现是**间歇性**的锁失败，最难查。

    **``journal_mode`` 只在还不是 WAL 时才去切换** —— 这是本轮排查的根因所在。
    ``PRAGMA journal_mode=WAL`` 会**真的写库**（要拿独占锁，写入文件头）。多个进程
    同时启动时，输掉竞争的那个会在这里抛 ``database is locked``；而这个异常**不在**
    任何重试范围内（它发生在建连接阶段），于是表现为「一个进程查不到原因地索引失败」。

    实测：并发 2 个进程、每轮**必然**有 1 个失败，且失败点在 ``store.connect`` 而不是
    建表逻辑 —— 这就是那条线索的价值：把「必然是 1 个」当成重试不够久的证据会一直修错地方。
    WAL 是**持久属性**（写进文件头），所以建好之后每次连接只需一次只读 PRAGMA 确认。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    if current_journal_mode(conn) != "wal":
        # 首次切换仍可能撞上别的进程正在切，所以给它重试；失败也不致命 ——
        # WAL 只是更好的并发模式，退回默认模式功能完全可用，不该因此让 capture 失败。
        try:
            retry_on_locked(conn.execute, "PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
    conn.execute("PRAGMA synchronous=NORMAL")
    # 多个进程同时写时，不要在第一个锁上就失败；等一会儿再试。
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def is_locked_error(exc: BaseException) -> bool:
    """是不是「数据库被别的进程锁住」这一类错误。

    按**错误信息**判断而不是 ``sqlite3.OperationalError`` 类型：OperationalError
    覆盖了缺表、语法错、列不存在等一大堆问题，把它们也重试只会浪费时间并掩盖真错误。
    """
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def retry_on_locked(fn, *args, **kwargs):
    """执行可能撞上写锁的操作；只对锁错误重试，别的一律原样抛出。

    ``busy_timeout`` 已经让 SQLite 自己在内核层面等锁了，这一层是它等满之后的兜底。
    重试的是**整个调用**而不是重开连接：调用方传进来的 ``conn`` 仍然有效
    （SQLite 的锁失败不会让连接失效），重开连接反而会丢掉未提交的状态。

    退避时间见 ``_LOCK_BACKOFF_SECONDS`` 的注释 —— 那里解释了为什么第一次要等 0.3 秒。
    """
    for attempt in range(_LOCK_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if attempt >= _LOCK_RETRIES or not is_locked_error(exc):
                raise
            time.sleep(_LOCK_BACKOFF_SECONDS[attempt])
    raise AssertionError("不可达：重试循环必然返回或抛出")  # pragma: no cover


# 每个版本「应该长什么样」的检查清单（只列该版本**新增**的对象）。
# 用途只有一个：判断当前库是否已经符合期望，从而**决定要不要写**。
#
# 为什么不直接「每次都跑一遍 IF NOT EXISTS 的 DDL」：DDL 是**写操作**，要向
# SQLite 申请写锁。多个进程同时 capture 时，六个进程会在 init 阶段一起抢锁，
# 输的那些等满 busy_timeout 后失败 —— 实测并发 6 个进程约 40% 概率出现
# "database is locked"。而这些 DDL 在正常情况下**一行都不会改变任何东西**，
# 纯粹是无谓的写竞争。既然能只读判断，就不该用写来问。
_EXPECTED: dict[int, dict[str, object]] = {
    1: {
        "columns": {"cards": ("id", "rel_path", "title", "kind", "status", "source",
                              "tags", "created", "updated", "body", "content_hash",
                              "embedding")},
        "tables": ("cards", "cards_fts", "meta"),
    },
    2: {
        "columns": {"cards": ("priority", "ttl", "expires_at")},
        "tables": ("card_stats",),
        "indexes": ("idx_cards_priority",),
    },
}


def _expected_through(version: int) -> dict[str, set[str]]:
    """把 1..version 的期望合并成一份「必须存在」的清单。"""
    columns: dict[str, set[str]] = {}
    tables: set[str] = set()
    indexes: set[str] = set()
    for v in range(1, version + 1):
        spec = _EXPECTED.get(v, {})
        for table, cols in (spec.get("columns") or {}).items():  # type: ignore[union-attr]
            columns.setdefault(table, set()).update(cols)  # type: ignore[arg-type]
        tables.update(spec.get("tables") or ())  # type: ignore[arg-type]
        indexes.update(spec.get("indexes") or ())  # type: ignore[arg-type]
    return {"columns": columns, "tables": tables, "indexes": indexes}  # type: ignore[dict-item]


def _reopen(conn: sqlite3.Connection) -> sqlite3.Connection:
    """关掉旧连接、按同一路径重开一个，用于锁重试。"""
    try:
        path = _db_path_of(conn)
    except Exception:
        return conn
    try:
        conn.close()
    except Exception:
        pass
    return connect(path)


def _db_path_of(conn: sqlite3.Connection) -> str:
    """取连接的数据库文件路径（``PRAGMA database_list`` 是只读的）。"""
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main":
            return row["file"]
    raise RuntimeError("连接没有 main 数据库路径")


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    """当前库是否已完全符合 SCHEMA_VERSION 的期望（**全部只读查询**）。

    返回 True 时调用方可以放心跳过 DDL —— 因为期望清单是「按版本累积」的，
    低版本库里缺的列 / 表 / 索引都会在这里被发现，不会被误判成「已就绪」。
    """
    expected = _expected_through(SCHEMA_VERSION)

    existing_tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        )
    }
    if not set(expected["tables"]).issubset(existing_tables):
        return False

    existing_indexes = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    if not set(expected["indexes"]).issubset(existing_indexes):
        return False

    for table, cols in expected["columns"].items():  # type: ignore[union-attr]
        # PRAGMA table_info 是只读的；表不存在时返回空集合，正好落入「不符合」
        present = {
            r["name"] for r in conn.execute(f"PRAGMA table_info({table})")
        }
        if not cols.issubset(present):
            return False
    return True


def commit(conn: sqlite3.Connection) -> None:
    """提交事务，撞锁时重试。

    **每个写路径都必须用它，而不是裸 ``conn.commit()``。**
    提交本身要向 SQLite 申请写锁；多进程并发写时，输掉竞争的就是在提交这一步
    失败 —— 实测确认过：并发 6 个 CLI ``capture``，去掉这一层后必然有一个返回
    ``indexed: false`` 并附带 ``database is locked`` 警告。
    """
    retry_on_locked(conn.commit)


def _read_schema_version(conn: sqlite3.Connection) -> int | None:
    """读 meta.schema_version。库还没建表（全新库）时返回 None。"""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # meta 表还不存在 → 全新库
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"索引库的 schema_version 损坏：{row['value']!r}。"
            f"请从 Markdown 真相源运行 index --rebuild 重建索引。"
        ) from exc


def _write_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(version),),
    )


def init(conn: sqlite3.Connection) -> None:
    """建表并把 schema 升到当前版本。幂等，且**自带写锁重试**。

    三种情况分开处理：

    - **全新库**（没有 meta 表，或没写过版本）→ 建表，写入当前版本
    - **库版本 < 代码版本** → 逐级跑 MIGRATIONS，跑完再补一遍 DDL
    - **库版本 > 代码版本** → **报错**。库比代码新，说明用旧代码打开了新库；
      继续跑就是拿旧结构去读新数据，而损坏是静默的。

    为什么重试放在这里而不是调用方：**DDL 也是写操作**。即使版本一致、什么
    都不用改，末尾那遍 ``executescript`` 仍要向 SQLite 申请写锁。多个进程同时
    ``capture`` 时，输掉竞争的那个会在 init 阶段就撞上 ``database is locked`` ——
    实测确认过（去掉重试后并发 6 个进程必然有一个失败）。

    实测教训（这一条值一次真实的排查）：**锁失败后不能只在同一个连接上重试**。
    WAL 模式下连接可能持有一个已经过期的读快照，此时 ``SQLITE_BUSY_SNAPSHOT``
    表示「这个快照**永远**无法升级成功」，同一连接上重试多少次都一样。
    所以撞锁时我们会**关掉连接重新打开**（拿到新快照），再重试。
    """
    for attempt in range(_LOCK_RETRIES + 1):
        try:
            return _init_once(conn)
        except sqlite3.OperationalError as exc:
            if attempt >= _LOCK_RETRIES or not is_locked_error(exc):
                raise
            time.sleep(_LOCK_BACKOFF_SECONDS[attempt])
            # 关键：换一个连接，丢掉可能已过期的读快照。
            conn = _reopen(conn)


def _exec_script(conn: sqlite3.Connection, script: str) -> None:
    """在**当前事务内**逐条执行脚本。

    不用 ``conn.executescript``：它会先隐式 COMMIT，把外层 ``BEGIN IMMEDIATE``
    刚拿到的写锁放掉 —— 那样就等于又回到了「读事务升级写事务」的老路。
    本项目的 DDL/迁移脚本里没有分号出现在字符串字面量中的情况（都是纯 DDL），
    按分号切分是安全的。
    """
    for stmt in script.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _init_once(conn: sqlite3.Connection) -> None:
    """在**一个 IMMEDIATE 写事务**里完成建表 / 迁移。

    为什么必须是 IMMEDIATE，而不是「先读后写」：

    WAL 模式下，连接先在读事务里读 ``meta.schema_version``，再想升级成写事务时，
    如果 WAL 已经被别的进程推进过，SQLite 会返回 ``SQLITE_BUSY_SNAPSHOT`` ——
    表示「这个读快照已经过期，**永远**无法升级成功」。关键点是：

    - 它**立刻**返回，不走 ``busy_timeout``（没有可等的释放）；
    - 在此连接上**重试永远不会成功**（快照已失效），除非关闭连接重开。

    实测印证过这两点：并发 2 个进程时**每轮必然有 1 个失败**，且把
    ``busy_timeout`` 从 5 秒提到 30 秒、把重试退避加到 7 秒，**都没有任何改善**。
    这也解释了为什么「一个进程一个工作区」的环境下从来没人见过它。

    ``BEGIN IMMEDIATE`` 一开始就申请写锁，于是等待交给 busy handler（能等、会成功），
    也不存在「读快照过期」这回事。建表与迁移本来就是写操作，用写事务做名正言顺。
    """
    conn.execute("BEGIN IMMEDIATE")
    current = _read_schema_version(conn)

    if current is None:
        _exec_script(conn, _DDL)
        _write_schema_version(conn, SCHEMA_VERSION)
        conn.commit()
        return

    if current > SCHEMA_VERSION:
        conn.rollback()
        raise RuntimeError(
            f"索引库的 schema 版本（{current}）高于本代码支持的版本（{SCHEMA_VERSION}）。"
            f"请升级 memory-agent，或改用匹配版本的代码。"
            f"继续运行会用旧结构读新库，可能静默损坏数据。"
        )

    if current < SCHEMA_VERSION:
        for v in range(current, SCHEMA_VERSION):
            script = MIGRATIONS.get(v)
            if script is None:
                conn.rollback()
                raise RuntimeError(f"缺少 v{v} → v{v + 1} 的迁移脚本")
            try:
                _exec_script(conn, script)
            except sqlite3.OperationalError as exc:
                conn.rollback()
                # 锁错误交给外层重试；别在这里误判成「结构不对」。
                if is_locked_error(exc):
                    raise
                raise RuntimeError(
                    f"执行 v{v} → v{v + 1} 迁移失败：{exc}。"
                    f"索引库可能不是 v{v} 结构（被人手动改过，或来自更早的实验版本）。"
                    f"它只是可重建的投影 —— 删掉它，再从 Markdown 真相源 index --rebuild 即可。"
                ) from exc
        _write_schema_version(conn, SCHEMA_VERSION)
        # 迁移只负责「补上这一步的差异」。跑完再走一遍 DDL，
        # 保证老库里缺的、与本次迁移无关的表也不会漏掉（DDL 全是 IF NOT EXISTS）。
        _exec_script(conn, _DDL)
        conn.commit()
        return

    # 版本一致：结构齐了就什么都不用做。这个判断必须在**已经拿到写锁之后**做，
    # 否则就成了「只读查一下 → 决定要不要写」的升级路径（见 init 的 docstring）。
    if _schema_is_current(conn):
        conn.commit()
        return
    _exec_script(conn, _DDL)
    conn.commit()


def _fts_sync(conn: sqlite3.Connection, card_id: int, title: str, body: str) -> None:
    """重建某张卡的 FTS 索引行。"""
    from .tokenize import to_index_text

    conn.execute("DELETE FROM cards_fts WHERE rowid = ?", (card_id,))
    conn.execute(
        "INSERT INTO cards_fts(rowid, title, body) VALUES (?, ?, ?)",
        (card_id, to_index_text(title), to_index_text(body)),
    )


def upsert_card(conn: sqlite3.Connection, card: dict) -> str:
    """写入或更新一张卡，并同步 FTS 索引。

    返回 ``"inserted"`` / ``"updated"`` / ``"unchanged"``。
    ``unchanged`` 表示 file_hash 未变，跳过写入 —— 这是增量索引的关键。

    注：入库的 ``card`` 字典用 ``file_hash`` 作为键（来源是 importer），
    而 DB 列名仍是历史命名 ``content_hash``，两者刻意不统一 ——
    改列名要迁移，收益为零。
    """
    rel = card["rel_path"]
    row = conn.execute(
        "SELECT id, content_hash FROM cards WHERE rel_path = ?", (rel,)
    ).fetchone()

    if row is not None and row["content_hash"] == card.get("file_hash", ""):
        return "unchanged"

    fields = (
        card.get("title", ""),
        card.get("kind", ""),
        card.get("status", ""),
        card.get("source", ""),
        json.dumps(card.get("tags", []), ensure_ascii=False),
        card.get("created", ""),
        card.get("updated", ""),
        card.get("body", ""),
        card.get("file_hash", ""),
        int(card.get("priority", 0) or 0),
        card.get("ttl", ""),
        int(card.get("expires_at", 0) or 0),
    )

    if row is not None:
        card_id = int(row["id"])
        conn.execute(
            """UPDATE cards
                  SET title=?, kind=?, status=?, source=?, tags=?,
                      created=?, updated=?, body=?, content_hash=?,
                      priority=?, ttl=?, expires_at=?
                WHERE id=?""",
            fields + (card_id,),
        )
        _fts_sync(conn, card_id, card.get("title", ""), card.get("body", ""))
        return "updated"

    cur = conn.execute(
        """INSERT INTO cards
               (rel_path, title, kind, status, source, tags,
                created, updated, body, content_hash,
                priority, ttl, expires_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rel,) + fields,
    )
    card_id = int(cur.lastrowid)
    _fts_sync(conn, card_id, card.get("title", ""), card.get("body", ""))
    return "inserted"


def delete_cards(conn: sqlite3.Connection, rel_paths: list[str]) -> int:
    """按路径批量删除卡片及其索引行。返回删除数量。

    **批量化**：原来是一张卡三条语句（SELECT id、删 FTS、删 cards），删除 N 张
    就是 3N 次往返。SQLite 的参数上限是 **999**（编译期 ``SQLITE_MAX_VARIABLE_NUMBER``，
    老版本更低），所以按块切分而不是一次性拼一整个 IN 列表 —— 超限会直接报错。

    **同时清 card_stats**：卡片真的离开 vault 之后，它的读取次数已经没有任何意义，
    留着就是「永远读不出来的幽灵行」（join cards 时消失，但确实占着表）。
    注意软删（P3-02 的 `.trash`）是**可恢复**的，那条路径不该走这个函数 ——
    统计要留到真正销毁（``delete --purge``）时才丢。
    """
    rels = [r for r in dict.fromkeys(rel_paths) if r]  # 去重且保序
    if not rels:
        return 0

    # 留出余量：每个 chunk 用 1 个参数查 id + 1 个参数查 stats。
    chunk_size = (SQLITE_MAX_VARIABLES - 1) // 2
    n = 0
    for start in range(0, len(rels), chunk_size):
        chunk = rels[start : start + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        ids = [
            int(r["id"]) for r in conn.execute(
                f"SELECT id FROM cards WHERE rel_path IN ({placeholders})", chunk
            )
        ]
        if not ids:
            continue
        id_ph = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM cards_fts WHERE rowid IN ({id_ph})", ids)
        conn.execute(f"DELETE FROM cards WHERE id IN ({id_ph})", ids)
        conn.execute(
            f"DELETE FROM card_stats WHERE rel_path IN ({placeholders})", chunk
        )
        n += len(ids)
    return n


def get_card(conn: sqlite3.Connection, card_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()


def iter_cards(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM cards ORDER BY rel_path")


def count_cards(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])


def _has_card_stats(conn: sqlite3.Connection) -> bool:
    """card_stats 是否已存在。

    存在的意义：**读路径不能在旧库上炸掉**。一个 v1 的老库（用户升级代码后
    还没跑过 index）里没有这张表，而 ``show`` / ``memory_get`` 会顺手记一次访问。
    为此让「看一眼卡片全文」失败，代价完全不成比例 —— 所以记不了就不记，
    统计随后由写入路径的迁移（``importer.sync_one`` → ``init``）补上。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='card_stats'"
    ).fetchone()
    return row is not None


def record_access(conn: sqlite3.Connection, rel_paths, now: int | None = None) -> int:
    """把若干张卡记一次访问。返回实际记账的卡片数。

    只对**当前存在于索引里**的路径记账 —— 否则一本「已删除卡片」的统计账
    会永久留在库里，且对外不可见（读的时候 join cards 就消失了），
    等于静默膨胀。

    ``rel_paths`` 允许重复：同一批里重复出现只加一次计数，
    避免调用方传了重复 id 就把计数刷高。
    """
    if isinstance(rel_paths, str):
        rel_paths = [rel_paths]
    unique = {p for p in rel_paths if p}
    if not unique:
        return 0
    if not _has_card_stats(conn):
        return 0

    ts = int(time.time()) if now is None else int(now)
    n = 0
    for rel in sorted(unique):
        exists = conn.execute(
            "SELECT 1 FROM cards WHERE rel_path = ?", (rel,)
        ).fetchone()
        if exists is None:
            continue
        conn.execute(
            """INSERT INTO card_stats(rel_path, access_count, last_accessed)
                    VALUES (?, 1, ?)
               ON CONFLICT(rel_path) DO UPDATE SET
                    access_count  = access_count + 1,
                    last_accessed = excluded.last_accessed""",
            (rel, ts),
        )
        n += 1
    conn.commit()
    return n


def access_stats(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """访问最多的卡片。``limit`` 被钳制在 [1, 100]。"""
    if not _has_card_stats(conn):
        return []
    limit = max(1, min(int(limit), 100))
    rows = conn.execute(
        """SELECT s.rel_path, s.access_count, s.last_accessed, c.title
             FROM card_stats s
             LEFT JOIN cards c ON c.rel_path = s.rel_path
            ORDER BY s.access_count DESC, s.rel_path
            LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "rel_path": r["rel_path"],
            "title": r["title"] or "",
            "access_count": int(r["access_count"]),
            "last_accessed": int(r["last_accessed"]),
        }
        for r in rows
    ]


def get_access_stat(conn: sqlite3.Connection, rel_path: str) -> dict | None:
    if not _has_card_stats(conn):
        return None
    row = conn.execute(
        "SELECT rel_path, access_count, last_accessed FROM card_stats WHERE rel_path = ?",
        (rel_path,),
    ).fetchone()
    if row is None:
        return None
    return {
        "rel_path": row["rel_path"],
        "access_count": int(row["access_count"]),
        "last_accessed": int(row["last_accessed"]),
    }


def stats(conn: sqlite3.Connection) -> dict:
    """汇总统计，供 CLI 的 stats 命令使用。"""

    def group(col: str) -> list[tuple[str, int]]:
        rows = conn.execute(
            f"SELECT {col} AS k, COUNT(*) AS n FROM cards "
            f"GROUP BY {col} ORDER BY n DESC"
        ).fetchall()
        return [(r["k"] or "(空)", int(r["n"])) for r in rows]

    total = count_cards(conn)
    body_bytes = int(
        conn.execute("SELECT COALESCE(SUM(LENGTH(body)), 0) FROM cards").fetchone()[0]
    )
    return {
        "total": total,
        "chars": body_bytes,
        "by_kind": group("kind"),
        "by_source": group("source"),
        "by_status": group("status"),
        "latest": [
            (r["rel_path"], r["updated"])
            for r in conn.execute(
                "SELECT rel_path, updated FROM cards ORDER BY updated DESC LIMIT 5"
            ).fetchall()
        ],
    }
