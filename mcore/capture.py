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
import os
import re
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["KIND_DIRS", "DEFAULT_KIND", "CONFLICT_POLICIES", "SECRET_PATTERNS",
           "write_card", "scan_secrets", "scan_slug", "card_fingerprint", "slugify"]

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


def _render(title: str, body: str, kind: str, tags: list[str], source: str,
            status: str, severity: str, reason: str, ts: str) -> str:
    fm = [
        "---",
        f"formatVersion: {FORMAT_VERSION}",
        f"kind: {kind}",
        f'title: "{title}"' if any(c in title for c in ':"') else f"title: {title}",
        f"tags: {_yaml_list(tags)}",
        f"created: {ts}",
        f"updated: {ts}",
        f"status: {status}",
        f"submittedBy: {source}",
        f"severity: {severity}",
        f"reason: {reason}",
        f"source: {source}",
        "---",
        "",
    ]
    return "\n".join(fm) + body.strip() + "\n"


def _unique_path(directory: Path, slug: str) -> Path:
    """避开重名：slug.md 被占用时依次尝试 slug-2.md、slug-3.md…"""
    path = directory / f"{slug}.md"
    n = 2
    while path.exists():
        path = directory / f"{slug}-{n}.md"
        n += 1
    return path


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
                   reason, _now_iso())
    # 指纹写进注释，供下次幂等比对（标记名保持 contentHash 不变）
    text = text.rstrip("\n") + f"\n\n<!-- contentHash: {fingerprint} -->\n"

    # 原子写入：先写临时文件再改名，避免中途失败留下半截文件
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
