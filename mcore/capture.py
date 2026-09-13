# -*- coding: utf-8 -*-
"""采集端：把知识写成 Markdown 卡片，落进 vault。

为什么由 agent 来蒸馏
--------------------
记忆的价值在于压缩。把整段会话原样倒进 vault，只会制造噪音，检索时反而
更难找到重点。本模块**不做 LLM 调用** —— 蒸馏交给调用方：agent 本身就是
LLM，让它先想清楚「什么值得记、怎么写得让人以后能看懂」，再交给这里落盘。

这样零额外成本、零 API key、数据不出网。

格式兼容
--------
frontmatter 与 vault 中既有卡片完全一致，因此新旧卡片可以共存、互相检索。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .util import parse_tags

__all__ = ["KIND_DIRS", "DEFAULT_KIND", "CONFLICT_POLICIES", "SECRET_PATTERNS",
           "TRASH_DIRNAME", "TRASH_REMIND_THRESHOLD",
           "write_card", "supersede_card", "update_card", "update_frontmatter",
           "delete_card", "restore_card", "purge_card", "purge_trash",
           "list_trash", "trash_summary",
           "scan_secrets", "scan_slug", "card_fingerprint", "slugify"]

# kind -> 分类目录（与既有 vault 结构一致）
KIND_DIRS: dict[str, str] = {
    "system": "00-System",
    "project": "02-Projects",
    "knowledge": "03-Knowledge",
    "content": "04-Content",
    "prompt": "05-Prompts",
    "business": "06-Business",
    "tool": "07-Tools",
    "mistake": "08-Mistakes",
}
DEFAULT_KIND = "knowledge"
FORMAT_VERSION = 1

# 正文最小长度：太短的多半是碎片，不值得占一个卡片位
MIN_BODY_CHARS = 20
# 文件名长度上限（Windows 限制 255，留出后缀与扩展名余量）
MAX_SLUG_CHARS = 60

# 标题撞车（slug 相同）但内容不同时的处理策略。
# 默认 suffix：保持既有行为不变 —— 不覆盖任何已有卡片。
CONFLICT_POLICIES = ("suffix", "reject")

# ---------------------------------------------------------------- 敏感内容扫描
#
# **这是在提醒，不是在拦截。** 默认只把命中结果回传给调用方，绝不阻断写入。
#
# 为什么默认不硬拒：本库的用途之一就是存渗透测试记录，而这类记录里天然会出现
# 密钥、凭据、连接串 —— 硬拒等于把项目的正当用途一起拒掉（ROADMAP §9 第 10 条）。
# 需要更严的场合由调用方显式开 ``--reject-secrets``。
#
# 每条只报「命中了哪一类」，**不回显命中的原文**：把密钥抄进告警里，
# 等于把它又写了一遍到日志 / 返回值里，反而扩大了暴露面。
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("私钥头", re.compile(r"-{5}BEGIN [A-Z ]*PRIVATE KEY-{5}")),
    ("SSH 私钥文件体", re.compile(r"-{5}BEGIN OPENSSH PRIVATE KEY-{5}")),
    ("AWS Access Key ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Slack Token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("OpenAI 风格 Key", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    # 赋值式凭据：键名**紧跟**冒号或等号，且值要同时含字母与数字。
    #
    # 两条约束都是实测加上的：
    #
    # 1. 「紧跟」——早期写成 `\bprivate[_-]?key\b\s*[:=]`，``\b`` 允许把键名截短成
    #    `private`，于是散文「private key 指的是概念」被判成凭据；
    # 2. 「值含字母**且**含数字」——``password: 已改成用密钥登录`` 这种说明性文字
    #    长度足够、看着也像赋值，但它没有数字。真实凭据（``hunter2hunter2``、
    #    ``aBcD1234``）几乎都混合字母与数字。
    #
    # 误报一旦多起来，告警会被所有人忽略 —— 那比没有告警更坏，所以宁可漏一点。
    # 这条局限写在这里，不要靠加长正则去追：真正要严的场合应该开 ``reject_secrets``
    # 或改用专门的凭据扫描工具，而不是让「顺手的正则」承担安全职责。
    ("疑似明文凭据赋值",
     re.compile(r"(?i)\b(?:password|passwd|pwd|api[_-]?key|apikey|secret|"
                r"access[_-]?token|auth[_-]?token|private[_-]?key|client[_-]?secret)"
                r"(?![A-Za-z])[`\"']?\s*[:=]\s*[\"']?"
                r"(?=[^\s\"'`,]{8,})(?=[^\s\"'`,]*[A-Za-z])(?=[^\s\"'`,]*\d)"
                r"([^\s\"'`,]+)")),
)


def scan_secrets(text: str) -> list[dict]:
    """扫出疑似凭据，返回 ``[{"kind", "span"}...]``（**不含命中原文**）。

    ``span`` 是命中位置区间，用于让人自己回到原文确认 —— 我们只指出「这里有东西」，
    不替调用方把凭据复述一遍。
    """
    text = text or ""
    found: list[dict] = []
    for kind, pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            found.append({"kind": kind, "span": [match.start(), match.end()]})
    return found


def _secret_warnings(findings: list[dict]) -> list[str]:
    """把命中结果转成给人看的告警文案（刻意不含凭据内容）。"""
    return [
        f"检测到疑似凭据（{f['kind']}，位置 {f['span'][0]}-{f['span'][1]}）。"
        f"记忆库会长期保留这条内容，且会被检索召回 —— 若非必要请改成引用方式"
        f"（例如「密钥见 ~/.ssh/xxx」而不是贴上密钥本身）。"
        for f in findings
    ]

# 文件名安全化：保留中文、字母数字、连字符；其余折叠为连字符
_UNSAFE = re.compile(r"[^\w\u4e00-\u9fff-]+", re.UNICODE)
_DASHES = re.compile(r"-{2,}")

# Windows 保留设备名。**不能用作文件名的词干** —— 在这些名字后面加扩展名也不行：
# ``CON.md`` 在 Windows 上依然打不开，因为设备名是在遇到 ``.`` 之前就已经匹配完了。
#
# 项目要跨平台，而这些卡片文件名直接由标题生成，用户完全可能写一张
# 标题为「NUL」「COM1」「con」的卡 —— 那时落盘会失败或产生一个打不开的文件，
# 而且失败发生在**写入阶段**，离「标题起得不对」这个真正原因很远。
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

# 保留名清单本身就是判定依据，不再需要额外的正则。


def _avoid_reserved(slug: str) -> str:
    """slug 若是 Windows 保留设备名，包一层下划线把它变成普通名字。

    ``con`` → ``_con_``；``my-con`` → ``my-_con_``。

    为什么用「包裹」而不是「加前缀」：加前缀会把 ``my-con`` 变成 ``xmy-con``，
    读起来像另一个词；包裹保持可读性，且一眼能看出这里被人为改动过。

    只判**整段**是不是保留名（不判「以保留名结尾」）：
    真正会出问题的是文件名的词干，而 ``my-con.md`` 的词干是 ``my-con``，
    它不是设备名 —— 把它改掉只会让用户莫名其妙。判定放在 ``slugify`` 的最后，
    因为保留名可能是截断或去连字符之后才浮现的（超长标题截断后末尾恰好是 ``con``）。
    """
    if slug.upper() in _RESERVED_NAMES:
        return f"_{slug}_"
    return slug


def slugify(title: str) -> str:
    """把标题转成安全的文件名片段。中文原样保留。

    最后一步处理 **Windows 保留设备名**（``CON`` / ``NUL`` / ``COM1``…）：
    它们在 Windows 上无法作为文件名，而卡片标题完全可能就叫「NUL」。
    判定必须在截断与去连字符**之后**做 —— 保留名可能是前几步才浮现的。
    """
    s = (title or "").strip().lower()
    s = _UNSAFE.sub("-", s)
    s = _DASHES.sub("-", s).strip("-")
    if len(s) > MAX_SLUG_CHARS:
        s = s[:MAX_SLUG_CHARS].rstrip("-")
    s = _avoid_reserved(s or "untitled")
    return s


def _now_iso() -> str:
    """与既有卡片一致的 ISO 时间戳（毫秒 + Z）。

    **只取一次 ``now()``**。原实现调了两次（秒用一次、毫秒用一次），
    两次之间可能跨过整秒边界 —— 于是会出现 ``...:59.000Z`` 这种自相矛盾的时间戳：
    秒还是 59，而毫秒已经取了下一秒的 000。概率极低但确实会发生，
    而时间戳是卡片排序与「最近更新」的依据，错了不会报错、只会让顺序变得诡异。

    ``datetime`` 是导入的对象（不是模块），所以这里没法用 frozen-time 之类的
    库来测；测试改为断言「毫秒与秒来自同一时刻」的实现形态（见 tests）。
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _yaml_list(items: list[str]) -> str:
    """内联数组，元素含逗号或方括号时加引号。"""
    out = []
    for it in items:
        s = str(it).strip()
        if not s:
            continue
        out.append(f'"{s}"' if any(c in s for c in ',[]"') else s)
    return "[" + ", ".join(out) + "]"


def _yaml_scalar(value: str) -> str:
    """渲染一个 frontmatter 标量。

    含冒号或引号时加引号（否则值里的 ``:`` 会把这一行变成嵌套结构）。
    空值写成 ``""`` 而不是留空 —— ``key: `` 这种写法末尾挂着一个看不见的空格，
    而且与「字段不存在」在肉眼上难以区分。

    ``update_card`` 与 ``_render`` 共用它：两处各写一份引号规则，迟早在某个
    含冒号的标题上分叉，而分叉出来的两份 frontmatter 都「看起来正常」。
    """
    text = str(value)
    if not text:
        return '""'
    return f'"{text}"' if any(c in text for c in ':"') else text


def _render(title: str, body: str, kind: str, tags: list[str], source: str,
            status: str, severity: str, reason: str, ts: str,
            *, supersedes: str = "", invalid_at: str = "",
            superseded_by: str = "") -> str:
    """渲染一张卡的完整文本（frontmatter + 正文）。

    取代相关的三个字段**默认不写** —— 只有在真的用到时才出现在 frontmatter 里。
    理由：字段一多，人读卡时就要在一堆空值里找有用的那几行；而「没写」与
    「写了空串」对读卡的人不是一回事。

    注意 ``created`` 与 ``updated`` 都由 ``ts`` 决定（同一次调用的毫秒一致），
    这是 P4-09 的结果：原来取两次时钟，跨整秒边界会产生自相矛盾的时间戳。
    """
    fm = [
        "---",
        f"formatVersion: {FORMAT_VERSION}",
        f"kind: {kind}",
        f"title: {_yaml_scalar(title)}",
        f"tags: {_yaml_list(tags)}",
        f"created: {ts}",
        f"updated: {ts}",
        f"status: {status}",
        f"submittedBy: {source}",
        f"severity: {severity}",
        f"reason: {reason}",
        f"source: {source}",
    ]
    # 取代关系写进 frontmatter（真相源）—— 删掉 memory.db 之后关系必须还在。
    if supersedes:
        fm.append(f"supersedes: {supersedes}")
    if invalid_at:
        fm.append(f"invalid_at: {invalid_at}")
    if superseded_by:
        fm.append(f"superseded_by: {superseded_by}")
    fm += ["---", ""]
    return "\n".join(fm) + body.strip() + "\n"


def _split_card_text(text: str) -> tuple[list[str], str] | None:
    """把卡片文本切成 ``(frontmatter 行, 正文)``；没有 frontmatter 段时返回 None。

    行列表**含首尾两条 ``---``**，所以行列表最后一项的下标就是结束分隔符的下标。
    与 :func:`update_frontmatter` 里的判定同一套规则（剥 BOM、找第二条 ``---``）——
    两处各写一遍的话，迟早有一处忘了剥 BOM，于是同一个文件在一处能改、在另一处
    「没有 frontmatter」，而两边都不报错。
    """
    stripped = text.lstrip("\ufeff")
    lines = stripped.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[: i + 1], "\n".join(lines[i + 1 :])
    return None


def _apply_frontmatter_updates(lines: list[str], end: int, updates: dict) -> list[str]:
    """逐行替换 ``key: ...``，缺失的键追加在结束分隔符之前。返回新的行列表。

    **只动给定的键。** 未知字段（别的工具加的、旧实验字段）原样留在原位置 ——
    整份重渲染会把它们抹掉，而「抹掉别人写的东西」不可逆且不报错。
    新键追加时保持原有键的相对顺序，让 diff 只体现真正改动的行。
    """
    remaining = dict(updates)
    out: list[str] = []
    for i, line in enumerate(lines):
        if i == end:
            # 结束分隔符之前，把没找到的键补上（保持原有键的相对顺序）
            for key, value in remaining.items():
                out.append(f"{key}: {value}")
            remaining.clear()
            out.append(line)
            continue
        if 0 < i < end and ":" in line:
            key = line.split(":", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}: {remaining.pop(key)}")
                continue
        out.append(line)
    return out


# 幂等比对用的指纹标记（frontmatter 之后、正文末尾的 HTML 注释）。
#
# 抽成常量是因为**改卡片时必须把它一起重算**：`_matches_fingerprint` 的第一条判据
# 就是「文件里有 contentHash: <新指纹>」。只改正文不重算标记的后果是静默的 ——
# 卡片内容变了，但去重判据还指着旧值，于是同一份内容再写一次会被判定为「新内容」，
# 库里悄悄多出一张重复卡。
_HASH_COMMENT_RE = re.compile(r"<!--\s*contentHash:\s*[0-9a-fA-F]+\s*-->")


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件再改名，避免中途失败留下半截卡片。

    采集端原有的三处写盘（``write_card`` / ``update_frontmatter`` / 本次新增的
    ``update_card``）共用它。三处各写一遍的话，总有一处会忘记「失败时清掉临时文件」，
    而 ``.md.tmp`` 残留会被下一次 ``rglob("*.md")`` 忽略（扩展名不是 .md），
    所以这种疏漏**不会报错**，只会悄悄在 vault 里堆垃圾文件。
    """
    tmp = path.with_suffix(".md.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def update_frontmatter(path: Path, updates: dict) -> bool:
    """就地改写一个已存在卡片的 frontmatter 字段（原子写）。返回是否真的改了。

    **只替换给定的键，其余行原样保留。** 这一点是刻意的：卡片可能是人手工写的、
    可能有本项目不知道的字段（别的工具加的、旧的实验字段），重新渲染整份 frontmatter
    会把它们抹掉 —— 而「抹掉别人写的东西」是不可逆的损失，且不会报错。

    做法是逐行替换 ``key: ...``，缺失的键追加在结束分隔符之前。正文与
    ``<!-- contentHash: ...>`` 注释一并原样保留：前者是卡片内容，后者是幂等比对用的标记，
    动了它下次重复写入就会多出一张卡。

    找不到 frontmatter（文件不以 ``---`` 开头）时返回 False 且**不写盘** ——
    宁可让调用方看到「没改成功」，也不要往一个格式不明的文件里硬塞字段。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False

    # 剥掉可能存在的 BOM 再判断，否则第一行是 "\ufeff---" 而匹配不上
    stripped = text.lstrip("\ufeff")
    lines = stripped.split("\n")
    if not lines or lines[0].strip() != "---":
        return False

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return False

    new_text = "\n".join(_apply_frontmatter_updates(lines, end, updates))
    if new_text == stripped:
        return False  # 没有任何变化，不必写盘（也避免无谓地改动 mtime）

    _atomic_write_text(path, new_text)
    return True


def update_card(
    vault: str | Path,
    rel_path: str,
    *,
    title: str | None = None,
    body: str | None = None,
    kind: str | None = None,
    tags: list[str] | None = None,
    priority: int | None = None,
    ttl: str | None = None,
    source: str | None = None,
) -> dict:
    """原地修改一张已存在的卡片（P3-02）。返回 ``{"ok", "changed", ...}``。

    与 :func:`supersede_card` 的分工 —— 这是调用方最容易选错的地方，所以写清楚：

    - **事实本身就写错了**（错别字、错误的路径、漏了参数）→ 用 ``update_card``，
      因为没有「当时是对的」这回事，历史没有价值；
    - **事实变了**（服务迁了地址、端口换了）→ 用 ``supersede_card``，
      因为「当时是多少」以后还要能回答。

    **只改传进来的字段。** 其余 frontmatter 行、未知字段、正文一字不动 ——
    卡片可能是人手工写的、可能带别的工具加的字段，整份重渲染会把它们抹掉。

    几条刻意的不支持 / 约定：

    1. **拒绝改 ``kind``**。``kind`` 决定卡片所在目录，改它等于移动文件；
       而 ``rel_path`` 是取代关系与 ``card_stats`` 的锚点（``id`` 是 rowid，
       ``index --rebuild`` 后会重排，不能当锚点）。想换类型就 supersede 出新卡再删旧卡。
       这是**故意的不支持**：静默忽略 ``kind`` 比报错更坏 —— 调用方会以为改成了。
    2. **改标题不动文件**。``title`` 只改 frontmatter 里的那一行，文件名保持不变。
       理由同上：路径是锚点。代价是文件名与标题可能不一致，这一点会写在返回值里。

       **已知代价（如实记录，不假装没有）**：``write_card`` 的重复判定是按
       **文件名的 slug** 找同族文件的（``scan_slug``）。改了标题而文件名没改之后，
       再用**新标题** capture 同一份内容，会落到另一个 slug 空间里、
       因而产生一张新卡。要避免它，就别用新标题去重写同一张卡 ——
       改标题的场景本就少见，而「为了去重而重命名文件」会打断取代关系与统计。
    3. **``updated`` 会被刷新，``created`` 保持不变。** 只有当确实有字段变了才写盘 ——
       没有变化时不刷新时间戳、不改 mtime，并如实回传 ``changed: []``。
    4. **正文长度门槛与 :func:`write_card` 一致**（``MIN_BODY_CHARS``）。
       两处门槛不同的后果是「同一份内容换个入口就能进来」，而两边都返回成功。
    5. **指纹（``contentHash``）会按新标题 + 新正文重算**，否则「重复写入判定」
       会拿着旧指纹去比，同一份内容会被当成新内容重复建卡。
    6. 文件**没有 frontmatter 段**时拒绝改动（不硬塞字段）—— 与
       :func:`update_frontmatter` 同一取向。
    """
    vault = Path(vault)
    path = (vault / rel_path).resolve()
    # 防越界：rel_path 来自调用方，不允许指到 vault 之外
    try:
        rel = path.relative_to(vault.resolve()).as_posix()
    except ValueError:
        return {"ok": False, "changed": [],
                "reason": f"路径不在 vault 内：{rel_path}"}

    if not path.is_file():
        return {"ok": False, "changed": [], "reason": f"卡片不存在：{rel_path}"}

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "changed": [], "reason": f"读不了卡片（{rel}）：{exc}"}

    parts = _split_card_text(text)
    if parts is None:
        return {"ok": False, "changed": [],
                "reason": (f"{rel} 没有 frontmatter 段，无法原地改字段 —— "
                           f"硬塞字段会造出一种半成品格式（开头几行像元数据但没有分隔符，"
                           f"而索引端会把它们整段当成正文）。请先补上 --- 段，或删掉重建。")}
    fm_lines, body_text = parts

    # 读现值用 importer 的解析器，**不在这里再写一份**：本项目已经因为
    # 「同一个意思、几处各写」吃过多次亏（tags 三份、档位标签两份）。
    # 局部导入是为了不把 capture 与 importer 绑成模块级依赖（capture 是纯文件操作，
    # 它不该在导入期就把索引端拖进来）。
    from .importer import parse_frontmatter

    meta, _ = parse_frontmatter(text)
    current_title = str(meta.get("title") or "").strip()
    current_kind = str(meta.get("kind") or DEFAULT_KIND).strip().lower()
    current_tags = parse_tags(meta.get("tags", []))
    current_source = str(meta.get("source") or "").strip()
    current_ttl = str(meta.get("ttl", "") or "").strip()
    try:
        current_priority = int(str(meta.get("priority", "") or "0").strip())
    except (TypeError, ValueError):
        # 与 importer._parse_priority 同口径：非法值当 0。这里只用来判断「有没有变」，
        # 不当校验 —— 一张卡写了 priority: 高 不该让 update 失败。
        current_priority = 0

    # 幂等标记从正文里摘出来单独看：它会被重算，不能混进「正文是否变化」的比较。
    marker = _HASH_COMMENT_RE.search(body_text)
    if marker:
        body_text = body_text[: marker.start()] + body_text[marker.end() :]
    current_body = body_text.strip()

    if kind is not None:
        wanted = str(kind or "").strip().lower()
        if wanted and wanted != current_kind:
            return {"ok": False, "changed": [], "rel_path": rel,
                    "kind_current": current_kind, "kind_requested": wanted,
                    "reason": (f"不支持改类型（kind）：{current_kind} → {wanted}。"
                               f"kind 决定卡片所在目录，改它等于移动文件，"
                               f"而路径是取代关系与读取统计的锚点。"
                               f"若确实要换类型：先用 supersede 写一张新类型的新卡，"
                               f"再 delete 旧卡。")}

    updates: dict[str, str] = {}
    changed: list[str] = []
    new_title, new_body = current_title, current_body

    if title is not None:
        candidate = str(title).strip()
        if not candidate:
            return {"ok": False, "changed": [], "rel_path": rel,
                    "reason": "标题不能改成空值"}
        if candidate != current_title:
            new_title = candidate
            updates["title"] = _yaml_scalar(candidate)
            changed.append("title")

    if tags is not None:
        candidate_tags = parse_tags(tags)
        if candidate_tags != current_tags:
            updates["tags"] = _yaml_list(candidate_tags)
            changed.append("tags")

    if priority is not None:
        try:
            candidate_priority = int(priority)
        except (TypeError, ValueError):
            return {"ok": False, "changed": [], "rel_path": rel,
                    "reason": f"priority 必须是整数，收到：{priority!r}"}
        if candidate_priority != current_priority:
            updates["priority"] = str(candidate_priority)
            changed.append("priority")

    if ttl is not None:
        candidate_ttl = str(ttl).strip()
        if candidate_ttl != current_ttl:
            updates["ttl"] = _yaml_scalar(candidate_ttl)
            changed.append("ttl")

    if source is not None:
        candidate_source = str(source).strip()
        if candidate_source != current_source:
            updates["source"] = _yaml_scalar(candidate_source)
            changed.append("source")

    if body is not None:
        candidate_body = str(body).strip()
        if len(candidate_body) < MIN_BODY_CHARS:
            return {"ok": False, "changed": [], "rel_path": rel,
                    "reason": (f"正文过短（{len(candidate_body)} < {MIN_BODY_CHARS} 字符），"
                               f"不值得占一张卡 —— 与 capture 是同一道门槛，"
                               f"否则同一份内容换个入口就能进来。")}
        if candidate_body != current_body:
            new_body = candidate_body
            changed.append("body")

    if not changed:
        # 不写盘、不刷时间戳。返回 ok=true 但明确 changed 为空 ——
        # 「没变化」不是失败，也绝不能谎称改过了。
        return {"ok": True, "changed": [], "rel_path": rel, "path": str(path),
                "title": current_title,
                "reason": "给定的值与卡片现值相同，未改动（updated 也未刷新）"}

    updates["updated"] = _now_iso()
    new_lines = _apply_frontmatter_updates(fm_lines, len(fm_lines) - 1, updates)
    fingerprint = card_fingerprint(new_title, new_body)
    new_text = ("\n".join(new_lines) + "\n" + new_body
                + f"\n\n<!-- contentHash: {fingerprint} -->\n")
    _atomic_write_text(path, new_text)

    result = {
        "ok": True,
        "changed": changed,
        "rel_path": rel,
        "path": str(path),
        "title": new_title,
        "updated": updates["updated"],
        "reason": f"已更新 {rel}（{', '.join(changed)}）",
    }
    if "title" in changed:
        # 文件名与标题不一致是刻意的（路径是锚点），但必须说出来 ——
        # 否则调用方按标题去找文件名会找不到，还以为卡片丢了。
        result["note"] = (f"标题已改，但文件名保持不变（{path.name}）—— "
                          f"移动文件会让取代关系与读取统计对不上。")
    return result


def supersede_card(
    vault: str | Path,
    old_rel_path: str,
    *,
    title: str,
    body: str,
    kind: str = DEFAULT_KIND,
    tags: list[str] | None = None,
    source: str = "agent",
) -> dict:
    """写一张新卡，并让旧卡失效（取代语义）。

    ``old_rel_path`` 是旧卡相对 vault 的路径（``03-Knowledge/xxx.md``）——
    **用路径而不是 id**：id 是 rowid，``index --rebuild`` 后会重排，关系会错位。
    而且这个函数是纯文件操作，它根本不想依赖索引是否存在。

    三步，顺序不可换：

    1. **先写新卡**（带 ``supersedes`` 指向旧卡）。若这步失败，什么都没变；
    2. **再给旧卡打失效标记**（``invalid_at`` + ``superseded_by``）。
       若这步失败，结果是「两张都有效」—— 比反过来的「两张都失效、新卡还不存在」好得多，
       因为前者只是查询时多给一条，后者是用户刚写的内容凭空消失；
    3. 由调用方索引两张卡（本函数不做索引 —— 它不该知道索引的存在）。

    **旧卡文件永远保留，只加标记。** 这是取代与删除的根本区别，也是「事后能回答
    『当时是多少』」的唯一依据。

    返回 ``{"ok", "new_path", "old_path", "invalid_at", "reason"}``。
    """
    vault = Path(vault)
    old_path = (vault / old_rel_path).resolve()
    # 防越界：rel_path 来自调用方，不允许指到 vault 之外
    try:
        old_rel = old_path.relative_to(vault.resolve()).as_posix()
    except ValueError:
        return {"ok": False, "reason": f"路径不在 vault 内：{old_rel_path}"}

    if not old_path.is_file():
        return {"ok": False, "reason": f"要取代的卡片不存在：{old_rel}"}

    old_text = old_path.read_text(encoding="utf-8")
    if "invalid_at:" in old_text:
        # 已经被取代过 —— 不允许链式覆盖。
        # 否则「A 被 B 取代、B 又被 C 取代」时，A 的 superseded_by 会指向谁就开始
        # 取决于操作顺序，而历史链会静默断掉。让调用方显式处理这种情况。
        return {"ok": False,
                "reason": f"这张卡已被取代过（{old_rel}），请改取代当前有效的那张"}

    # 1) 写新卡
    new = write_card(
        vault, title=title, body=body, kind=kind, tags=tags, source=source,
        reason="取代旧卡", supersedes=old_rel,
    )
    if not new.get("ok"):
        return {"ok": False, "reason": f"新卡未写入：{new.get('reason')}"}

    new_rel = Path(new["path"]).resolve().relative_to(vault.resolve()).as_posix()
    invalid_at = _now_iso()

    # 2) 给旧卡打标记（只改这两个键，其余行与正文原样保留）
    changed = update_frontmatter(old_path, {
        "invalid_at": invalid_at,
        "superseded_by": new_rel,
    })
    if not changed:
        # 新卡已经写进去了，但旧卡没标上 —— 结果是「两张都有效」。
        # 不回滚新卡：真相源优先，用户刚写的内容不该因为收尾失败就消失。
        # 但必须如实报出来，并给出可执行的修法。
        return {
            "ok": False,
            "new_path": str(Path(new["path"])),
            "old_path": str(old_path),
            "reason": (f"新卡已写入，但旧卡（{old_rel}）的 frontmatter 未能改写 —— "
                       f"可能是文件缺少 frontmatter 段。两张卡现在都有效，"
                       f"请手工在旧卡里加 invalid_at 与 superseded_by，或删掉新卡重来。"),
        }

    return {
        "ok": True,
        "new_path": str(Path(new["path"])),
        "old_path": str(old_path),
        "new_rel": new_rel,
        "old_rel": old_rel,
        "invalid_at": invalid_at,
        "reason": f"已用新卡取代 {old_rel}",
    }


def _normalize_rel(rel_path: str) -> str:
    """规范化调用方给的相对路径：剥掉开头的 ``./`` 与**误加的** ``.trash/`` 前缀。

    **不能用 ``lstrip("./")``**：它按「字符集合」剥，会把 ``.trash/x`` 剥成
    ``trash/x`` —— 前缀里的 ``.`` 和 ``t``… 都被当成要剥的字符。这不是假想，
    是实测踩到的（``delete --restore .trash/xxx`` 报「回收站里没有 trash/xxx」）。
    这里逐前缀比较，只剥真正匹配的那一个。
    """
    rel = Path(str(rel_path)).as_posix()
    while rel.startswith("./"):
        rel = rel[2:]
    prefix = f"{TRASH_DIRNAME}/"
    while rel.startswith(prefix):
        rel = rel[len(prefix):]
    return rel


def _resolve_in_trash(vault: Path, rel_path: str) -> tuple[Path | None, str, str]:
    """把 ``rel_path`` 解析成回收站内的绝对路径。返回 ``(路径, 规范化后的 rel, 错误说明)``。

    **必须限界，而且是硬性要求**：``Path('C:/Windows/notepad.exe')`` 是绝对路径，
    而 ``pathlib`` 在拼接时会让绝对路径**整个取代**左边 —— ``base / 绝对路径``
    得到的就是那个绝对路径。于是 ``purge_card`` 会去删一个 vault 之外的文件。
    所以这里解析后必须回验「它仍在 ``.trash`` 之内」，越界一律拒绝。

    同一条检查顺带挡住 ``../../`` 这类穿越（``.trash/../../x`` 解析后不在 ``.trash`` 内）。
    """
    rel = _normalize_rel(rel_path)
    if not rel:
        return None, "", "路径为空"
    root = (vault / TRASH_DIRNAME).resolve()
    try:
        candidate = (root / rel).resolve()
        candidate.relative_to(root)
    except (ValueError, OSError):
        return None, rel, f"路径不在回收站内：{rel_path}"
    return candidate, rel, ""


def _unique_path(directory: Path, slug: str) -> Path:
    """避开重名：slug.md 被占用时依次尝试 slug-2.md、slug-3.md…"""
    path = directory / f"{slug}.md"
    n = 2
    while path.exists():
        path = directory / f"{slug}-{n}.md"
        n += 1
    return path


# ---------------------------------------------------------------- 软删与回收站
#
# 删除是**软删**：文件移到 ``<vault>/.trash/<原相对路径>``，随时可移回。
# 真删只发生在 ``purge``，且**只对已经在 .trash 里的文件生效** ——
# 这条约束是「`--purge` 不可能删掉一张活着的卡」的机制保证，不是靠调用方自觉。
#
# 为什么要按原相对路径存放：恢复时不需要任何额外记录就能算回原位；
# 一张卡被手工放进 .trash 也能被恢复（哪怕没有清单记录）。
TRASH_DIRNAME = ".trash"
_TRASH_MANIFEST = ".manifest.json"


def _trash_root(vault: Path) -> Path:
    return vault / TRASH_DIRNAME


def _is_inside_trash(rel: str) -> bool:
    """判断一个 rel_path 是不是已经位于回收站内。

    只认**第一段**恰好等于 ``.trash``：形近目录（``.trash-old``）不算 ——
    模糊匹配会让「.trash-old 里的卡」被判成已删除，然后 `restore` 去找一个
    并不存在的文件。``importer._SKIP_DIRS`` 对同一件事是同一种判法。
    """
    parts = Path(rel).parts
    return bool(parts) and parts[0] == TRASH_DIRNAME


def _read_trash_manifest(vault: Path) -> dict:
    """读回收站清单 ``{rel_path: {"deleted_at", "trash_path"}}``。

    清单**只为一件事存在**：知道「这张卡是什么时候被删的」。
    文件的 mtime 在移动后仍是原卡片的写入时间（不能用它当删除时间），
    所以「删除超过了 N 天才清理」这个筛选必须有独立的记录。

    读不了 / 损坏就返回空字典，**不抛错**：清单不是真相源，丢了只是让
    ``--older-than`` 失去依据 —— 那时我们**跳过并如实报告**，绝不猜。
    """
    try:
        raw = (_trash_root(vault) / _TRASH_MANIFEST).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_trash_manifest(vault: Path, data: dict) -> None:
    """写回收站清单（原子写）。它不是真相源，写失败不该让删除失败。"""
    root = _trash_root(vault)
    root.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(root / _TRASH_MANIFEST,
                       json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def list_trash(vault: str | Path) -> list[dict]:
    """列出回收站里的卡片。

    每条返回 ``{"rel_path", "trash_path", "deleted_at", "size"}``；
    ``deleted_at`` 为空串表示**清单里没有它的记录**（例如手工放进来的文件）——
    调用方必须能区分「不知道何时删的」与「很久以前删的」，
    否则 ``--older-than`` 会把来路不明的文件当成很久以前删的而被清掉。
    """
    vault = Path(vault)
    root = _trash_root(vault)
    if not root.is_dir():
        return []

    manifest = _read_trash_manifest(vault)
    by_trash_path = {
        str(rec.get("trash_path", "")): (rel, rec)
        for rel, rec in manifest.items() if isinstance(rec, dict)
    }

    out: list[dict] = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        trash_rel = path.relative_to(vault).as_posix()
        rel, rec = by_trash_path.get(trash_rel, (trash_rel[len(TRASH_DIRNAME) + 1:], {}))
        out.append({
            "rel_path": rel,
            "trash_path": trash_rel,
            "deleted_at": str(rec.get("deleted_at", "") or ""),
            "size": path.stat().st_size,
        })
    return out


def delete_card(vault: str | Path, rel_path: str) -> dict:
    """软删一张卡：移到 ``<vault>/.trash/<原相对路径>``。返回删除结果。

    **它是可逆的**，所以：

    - **不清读取统计**（``card_stats``）。软删能恢复，统计跟着清掉就是清掉一份
      没有第二个来源的数据。真正的销毁在 :func:`purge_card`。
    - **不删任何内容**，只是搬走文件。

    为什么是「移动」而不是「打个删除标记留在原地」：留在原地的卡片仍在 vault 里，
    索引端要么得学会读标记（于是删除语义渗进索引端），要么就会继续检索到它。
    移到 vault 之外的子目录、再把该目录排除出索引，删除语义只需要一个地方知道。

    返回 ``{"ok", "rel_path", "trash_path", "deleted_at", "path", "reason"}``。
    """
    vault = Path(vault)
    path = (vault / rel_path).resolve()
    try:
        rel = path.relative_to(vault.resolve()).as_posix()
    except ValueError:
        return {"ok": False, "reason": f"路径不在 vault 内：{rel_path}"}

    if _is_inside_trash(rel):
        # 重复软删会把 .trash 里的文件再套一层 .trash。
        return {"ok": False, "rel_path": rel,
                "reason": (f"{rel} 已经在回收站里了。"
                           f"要彻底删除请用 delete --purge {rel}。")}

    if not path.is_file():
        return {"ok": False, "rel_path": rel, "reason": f"卡片不存在：{rel}"}

    trash_rel = f"{TRASH_DIRNAME}/{rel}"
    dest = vault / trash_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # 同名冲突是真实存在的：删掉一张 → 恢复 → 又写一张同名卡 → 再删。
        # 直接覆盖会**静默销毁**回收站里那一份 —— 而回收站的全部意义就是它还在。
        dest = _unique_path(dest.parent, dest.stem)
        trash_rel = dest.relative_to(vault).as_posix()

    # 同一卷内 os.replace 是原子的：中途失败不会留下「两处都没有」的状态。
    # .trash 是 vault 的子目录，因此必然同卷。
    os.replace(path, dest)

    deleted_at = _now_iso()
    manifest = _read_trash_manifest(vault)
    manifest[rel] = {"deleted_at": deleted_at, "trash_path": trash_rel}
    try:
        _write_trash_manifest(vault, manifest)
    except OSError:
        # 清单写不进去不该让「已经搬走的文件」看起来像没删成功。
        # 代价是这张卡的删除时间未知 —— 由 list_trash 如实报成空。
        pass

    return {
        "ok": True,
        "rel_path": rel,
        "trash_path": trash_rel,
        "deleted_at": deleted_at,
        "path": str(dest),
        "reason": f"已软删到 {trash_rel}（可恢复：delete --restore {rel}）",
    }


def restore_card(vault: str | Path, rel_path: str, *,
                 force: bool = False) -> dict:
    """把回收站里的卡移回原位。返回恢复结果。

    ``rel_path`` 给的是**原相对路径**（``03-Knowledge/xxx.md``），不是 ``.trash/...``
    那种全路径 —— 恢复的语义是「回到它原本待的地方」，让调用方自己拼目标路径
    等于把「它原本在哪」这件事交给调用方记。

    目标位置已存在同名卡时**默认拒绝**（覆盖是不可逆的丢失）。``force=True`` 才继续，
    做法是**先把占位的那张也移进回收站**再恢复 —— 于是「强制恢复」也不销毁任何东西。

    恢复之后**统计照旧**（软删时没清），读取次数会接上原来的计数。
    """
    vault = Path(vault)
    src, rel, why = _resolve_in_trash(vault, rel_path)
    if src is None:
        return {"ok": False, "rel_path": rel or str(rel_path), "reason": why}
    if not src.is_file():
        # 回退到清单：同名冲突时实际落点带 -2 后缀，路径不再是 .trash/<rel>。
        # 清单里的值同样要限界 —— 它是磁盘上的文件，可能被人手工改过。
        rec = _read_trash_manifest(vault).get(rel) or {}
        fallback, _, _ = _resolve_in_trash(vault, str(rec.get("trash_path", "")))
        if fallback is not None and fallback.is_file():
            src = fallback
        else:
            return {"ok": False, "rel_path": rel,
                    "reason": (f"回收站里没有 {rel}。"
                               f"可用 python memory.py stats 查看回收站张数，"
                               f"或 delete --purge --all 清空。")}

    dest = vault / rel
    displaced = ""
    if dest.exists():
        if not force:
            return {"ok": False, "rel_path": rel,
                    "reason": (f"目标位置已有一张卡（{rel}），拒绝覆盖 —— 覆盖是不可逆的丢失。"
                               f"确认要用回收站里这一张顶替它，请加 --force："
                               f"（占位的那张也会先被移进回收站，不会丢）")}
        # 强制恢复也不销毁：占位的那张先移进回收站
        trash_rel = f"{TRASH_DIRNAME}/{rel}"
        keep = vault / trash_rel
        keep.parent.mkdir(parents=True, exist_ok=True)
        if keep.exists():
            keep = _unique_path(keep.parent, keep.stem)
        os.replace(dest, keep)
        displaced = keep.relative_to(vault).as_posix()

    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)

    manifest = _read_trash_manifest(vault)
    if rel in manifest:
        manifest.pop(rel, None)
        try:
            _write_trash_manifest(vault, manifest)
        except OSError:
            pass

    result = {
        "ok": True,
        "rel_path": rel,
        "path": str(dest),
        "reason": f"已从回收站恢复到 {rel}",
    }
    if displaced:
        result["displaced"] = displaced
        result["note"] = (f"原占位的卡片已移进回收站：{displaced}（没有销毁任何内容）")
    return result


def purge_card(vault: str | Path, rel_path: str) -> dict:
    """彻底删除回收站里的一张卡（不可恢复）。返回删除结果。

    **只对已经在 ``.trash`` 里的文件生效。** 传一个活着的卡路径进来会被拒绝 ——
    这不是靠调用方自觉，而是这个函数唯一能删除的位置就是回收站。
    """
    vault = Path(vault)
    src, rel, why = _resolve_in_trash(vault, rel_path)
    if src is None:
        return {"ok": False, "rel_path": rel or str(rel_path), "reason": why}
    if not src.is_file():
        rec = _read_trash_manifest(vault).get(rel) or {}
        fallback, _, _ = _resolve_in_trash(vault, str(rec.get("trash_path", "")))
        if fallback is not None and fallback.is_file():
            src = fallback
        else:
            live = vault / rel
            if live.is_file():
                return {"ok": False, "rel_path": rel,
                        "reason": (f"{rel} 是一张活着的卡片，不是回收站里的 —— "
                                   f"purge 只能彻底删除回收站里的内容。"
                                   f"要软删它请先 delete {rel}。")}
            return {"ok": False, "rel_path": rel,
                    "reason": f"回收站里没有 {rel}"}

    size = src.stat().st_size
    src.unlink()
    _prune_empty_dirs(src.parent, vault / TRASH_DIRNAME)

    manifest = _read_trash_manifest(vault)
    if rel in manifest:
        manifest.pop(rel, None)
        try:
            _write_trash_manifest(vault, manifest)
        except OSError:
            pass

    return {"ok": True, "rel_path": rel, "purged_bytes": size,
            "reason": f"已彻底删除 {rel}（不可恢复）"}


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    """删完后把空目录往上收掉，但**不碰 stop 本身**。

    回收站留着空目录不会出错，但会让人以为「里面还有东西」；
    而 ``.trash`` 本身要留下 —— 它是这个功能的落点。
    """
    current = start
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _parse_trash_time(raw: str) -> float | None:
    """把清单里的 ``deleted_at``（``2026-09-13T00:48:58.402Z``）解析成 unix 秒。

    解析不了返回 ``None``：调用方据此把这张卡**跳过并报出来**，而不是当成
    「很久以前删的」。猜测会直接导致文件被删掉 —— 那是不可逆的，
    所以这里的取向是「宁可不清，也不误清」。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, fmt).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def purge_trash(vault: str | Path, *, older_seconds: int | None = None,
                everything: bool = False, now_ts: float | None = None) -> dict:
    """批量彻底删除回收站内容。返回 ``{"ok", "purged", "skipped", ...}``。

    **必须显式给一个筛选条件**（``everything=True`` 或 ``older_seconds``）：
    没有条件就等于「清空回收站」，而那种操作不该由一个手滑的命令触发。

    ``deleted_at`` 缺失或解析不了的条目在 ``older_seconds`` 模式下会被**跳过**，
    并在 ``skipped`` 里说明原因。``everything=True`` 则连它们一起删 ——
    「清空」是明确的意图，不需要再拿时间当判据。
    """
    vault = Path(vault)
    if not everything and older_seconds is None:
        return {"ok": False, "purged": [], "skipped": [],
                "reason": "批量彻底删除必须给条件：--older-than <时长> 或 --all"}

    now = time.time() if now_ts is None else now_ts
    purged: list[dict] = []
    skipped: list[dict] = []

    for entry in list_trash(vault):
        if not everything:
            stamp = _parse_trash_time(entry["deleted_at"])
            if stamp is None:
                skipped.append({**entry,
                                "skip_reason": "清单里没有这张卡的删除时间，无法判断是否超期"})
                continue
            if now - stamp < int(older_seconds or 0):
                continue  # 还没到期，安静留着

        res = purge_card(vault, entry["rel_path"])
        if res["ok"]:
            purged.append(entry)
        else:
            skipped.append({**entry, "skip_reason": res["reason"]})

    return {"ok": True, "purged": purged, "skipped": skipped,
            "count": len(purged),
            "reason": f"已彻底删除 {len(purged)} 张回收站卡片"}


def trash_summary(vault: str | Path) -> dict:
    """回收站概览，供 ``delete`` 与 ``stats`` 提醒用。

    A3 的决定是「**不自动清理**，攒到一定量提醒用户清理」，所以这里只数、不动手。
    """
    entries = list_trash(vault)
    unknown = sum(1 for e in entries if not e["deleted_at"])
    return {"count": len(entries), "unknown_deleted_at": unknown,
            "bytes": sum(e["size"] for e in entries)}


# 攒到多少张就该提醒 —— 只是提示阈值，不影响任何行为（不自动清理）。
TRASH_REMIND_THRESHOLD = 10


def card_fingerprint(title: str, body: str) -> str:
    """卡片指纹：由「标题 + 正文」算出。

    与 importer 的 ``file_hash``（整份文件文本，含 frontmatter）**不是一回事**，
    所以名字必须区分开 —— 它们数值不同、用途也不同。
    """
    return hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()[:16]


def _matches_fingerprint(text: str, title: str, fingerprint: str) -> bool:
    """一个已存在的文件是否与「标题 + 正文指纹」一致。

    两条判据都认：

    1. 文件里的 ``contentHash`` 标记 —— 采集端写出的卡片都带；
    2. 用**同一个标题**按正文重算指纹 —— 兼容没有标记的旧卡 / 手工写的卡。

    判据 2 必须用**本次写入的标题**，而不是从文件里再解析一次标题：
    原始实现就是这么算的，换掉会让既有卡片的幂等匹配静默失效。
    """
    if f"contentHash: {fingerprint}" in text:
        return True
    body_part = text.split("---", 2)[-1].strip()
    return hashlib.sha256(
        f"{title}\n{body_part}".encode("utf-8")
    ).hexdigest()[:16] == fingerprint


def scan_slug(directory: Path, slug: str, title: str, fingerprint: str) -> dict:
    """扫描同 slug 的卡片，返回 ``{"same": Path|None, "collisions": [Path, ...]}``。

    - ``same``：内容与本次写入**完全相同**的已有卡片（幂等命中，跳过写入）；
    - ``collisions``：同 slug 但内容不同的卡片 —— 也就是「标题撞车」。

    **不能看到第一个内容不同的就停下**：既有卡片可能已经是 ``slug-2.md``
    这种带序号的名字，真正的幂等命中排在它后面。提前退出会把「重复写入」
    退化成「每次生成一个新序号」，而那是静默的重复累积。

    目录不存在时直接返回空结果（首次写入的常见情况，不该抛错）。
    """
    if not directory.is_dir():
        return {"same": None, "collisions": []}

    collisions: list[Path] = []
    for existing in sorted(directory.glob(f"{slug}*.md")):
        try:
            text = existing.read_text(encoding="utf-8")
        except Exception:
            continue  # 读不了的文件既不算命中也不算撞车，跳过
        if _matches_fingerprint(text, title, fingerprint):
            return {"same": existing, "collisions": collisions}
        collisions.append(existing)
    return {"same": None, "collisions": collisions}


def write_card(
    vault: str | Path,
    *,
    title: str,
    body: str,
    kind: str = DEFAULT_KIND,
    tags: list[str] | None = None,
    source: str = "agent",
    status: str = "approved",
    severity: str = "info",
    reason: str = "agent 主动沉淀",
    on_conflict: str = "suffix",
    reject_secrets: bool = False,
    supersedes: str = "",
) -> dict:
    """写入一张卡片。

    返回 ``{"ok", "action", "path", "reason"}``。
    ``action`` 为 ``created`` / ``unchanged`` / ``rejected`` / ``conflict``。

    幂等：标题与正文都相同的卡片重复写入时不会产生副本。

    **标题撞车（同 slug、不同正文）会显式回传**，不再静默产生 ``slug-2.md``：

    - ``on_conflict="suffix"``（默认，保持向后兼容）：仍写入带序号的新文件，
      但结果里带 ``conflict: true`` + ``existing_path`` + ``collision_count``；
    - ``on_conflict="reject"``：不写盘，返回 ``action="conflict"`` 与 ``ok: False``。

    ``existing_path`` 的语义统一为「**slug 基名对应的那张卡**」（``slug.md``），
    无论它是新写出的还是早就存在的 —— 调用方要的是「我在跟谁撞车」，而基名是
    唯一确定的答案。被撞的实际文件清单在 ``collision_paths`` 里。

    **敏感内容默认只告警**：命中 ``SECRET_PATTERNS`` 时结果里带
    ``secrets_found`` 与 ``warnings``，但**照常写入**。``reject_secrets=True``
    才改成拒绝。为什么默认不拦见 ``SECRET_PATTERNS`` 上方的说明。
    """
    vault = Path(vault)
    title = (title or "").strip()
    body = (body or "").strip()

    if not title:
        return {"ok": False, "action": "rejected", "reason": "标题不能为空"}
    if len(body) < MIN_BODY_CHARS:
        return {"ok": False, "action": "rejected",
                "reason": f"正文过短（{len(body)} < {MIN_BODY_CHARS} 字符），不值得建卡"}

    policy = str(on_conflict or "suffix").strip().lower()
    if policy not in CONFLICT_POLICIES:
        return {"ok": False, "action": "rejected",
                "reason": f"未知的 on_conflict 策略：{on_conflict!r}，"
                          f"可选 {' / '.join(CONFLICT_POLICIES)}"}

    # 敏感内容扫描在写盘之前做，但**默认不因此拒绝** —— 只把结果带回去。
    # 标题也扫：有些人会把密钥塞在标题里。
    findings = scan_secrets(f"{title}\n{body}")
    if findings and reject_secrets:
        return {
            "ok": False,
            "action": "rejected",
            "secrets_found": [f["kind"] for f in findings],
            "reason": (f"正文含疑似凭据（{'、'.join(f['kind'] for f in findings)}），"
                       f"且已开启 reject_secrets，故拒绝写入。"
                       f"请改为引用方式（如「密钥见 ~/.ssh/xxx」）。"),
        }

    kind = (kind or DEFAULT_KIND).strip().lower()
    if kind not in KIND_DIRS:
        kind = DEFAULT_KIND
    directory = vault / KIND_DIRS[kind]

    slug = slugify(title)
    fingerprint = card_fingerprint(title, body)

    # 幂等检查：同 slug 且指纹一致 -> 视为重复
    # 注意：写进文件的标记字符串仍是 ``contentHash``，不随变量改名而变 ——
    # 改了它，磁盘上既有卡片的标记就再也匹配不上。
    scan = scan_slug(directory, slug, title, fingerprint)
    if scan["same"] is not None:
        result = {"ok": True, "action": "unchanged",
                  "path": str(scan["same"]), "reason": "内容相同，已存在"}
        if findings:
            result["secrets_found"] = [f["kind"] for f in findings]
            result["warnings"] = _secret_warnings(findings)
        return result

    collisions: list[Path] = scan["collisions"]
    base_path = directory / f"{slug}.md"

    if collisions and policy == "reject":
        result = {
            "ok": False,
            "action": "conflict",
            "path": "",
            "existing_path": str(base_path),
            "collision_paths": [str(p) for p in collisions],
            "reason": (f"标题撞车：{KIND_DIRS[kind]}/{slug}.md 已存在但内容不同（"
                       f"共 {len(collisions)} 张同标题卡）。"
                       f"若想改动既有事实用 update；若事实已变、旧卡仍需留存用 supersede。"),
        }
        if findings:
            result["secrets_found"] = [f["kind"] for f in findings]
            result["warnings"] = _secret_warnings(findings)
        return result

    directory.mkdir(parents=True, exist_ok=True)
    path = _unique_path(directory, slug)

    text = _render(title, body, kind, tags or [], source, status, severity,
                   reason, _now_iso(), supersedes=supersedes)
    # 指纹写进注释，供下次幂等比对（标记名保持 contentHash 不变）
    text = text.rstrip("\n") + f"\n\n<!-- contentHash: {fingerprint} -->\n"

    # 原子写入：先写临时文件再改名，避免中途失败留下半截文件
    _atomic_write_text(path, text)

    result = {"ok": True, "action": "created", "path": str(path),
              "reason": f"已写入 {KIND_DIRS[kind]}/"}
    if collisions:
        result["conflict"] = True
        result["existing_path"] = str(base_path)
        result["collision_count"] = len(collisions)
        result["collision_paths"] = [str(p) for p in collisions]
        result["note"] = (f"标题与已有 {len(collisions)} 张卡相同但内容不同，"
                          f"已另存为 {path.name}（未覆盖任何卡片）。")
    if findings:
        # 告警与「已写入」并存：卡片确实写进去了，同时把风险说出来。
        # 不合并进 note，是因为 note 已被撞车占用；两个字段各自独立更清楚。
        result["secrets_found"] = [f["kind"] for f in findings]
        result["warnings"] = _secret_warnings(findings)
    return result
