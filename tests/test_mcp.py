#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP server 协议级测试。

不需要任何测试框架，直接运行：

    python tests/test_mcp.py

重点验证三件事：
  1. 协议合规：握手、tools/list、tools/call、错误码
  2. **stdout 纯净**：每一行都必须是合法 JSON-RPC，混入一个字符握手就废了
  3. 工具确实能查到真实数据
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PY = ROOT / "memory.py"

_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed.append(name)
        print(f"  FAIL  {name}" + (f"  — {detail}" if detail else ""))


class Client:
    """极简 MCP 客户端：逐行 JSON-RPC over stdio。"""

    def __init__(self, env: dict | None = None) -> None:
        import os
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        self.proc = subprocess.Popen(
            [sys.executable, str(MEMORY_PY), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            bufsize=1,
            env=full_env,
        )
        self.next_id = 1
        self.stdout_lines: list[str] = []

    def _write(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        m = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            m["params"] = params
        self._write(m)

    def request(self, method: str, params: dict | None = None) -> dict:
        """发请求并读到对应 id 的响应。"""
        rid = self.next_id
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"服务提前退出（等待 {method} 的响应时 EOF）")
            line = line.rstrip("\n")
            self.stdout_lines.append(line)
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"stdout 出现非 JSON 内容：{line!r} ({exc})")
            if obj.get("id") == rid:
                return obj

    def raw(self, text: str) -> dict:
        """发送原始字符串（用于测试畸形输入）。"""
        rid = self.next_id
        self.next_id += 1
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("EOF")
            line = line.rstrip("\n")
            self.stdout_lines.append(line)
            obj = json.loads(line)
            if obj.get("id") == rid or obj.get("id") is None:
                return obj

    def close(self) -> str:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        return self.proc.stderr.read()


def main() -> int:
    print("MCP server 协议测试")
    print("=" * 62)

    c = Client()
    try:
        # ---------------------------------------------------- 握手
        print("\n[1] 初始化握手")
        r = c.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        })
        res = r.get("result", {})
        check("initialize 返回 result", "result" in r, str(r)[:200])
        check("协商到请求的协议版本", res.get("protocolVersion") == "2025-06-18",
              res.get("protocolVersion"))
        check("声明 tools 能力", "tools" in res.get("capabilities", {}))
        check("返回 serverInfo", res.get("serverInfo", {}).get("name") == "memory-agent",
              str(res.get("serverInfo")))
        c.notify("notifications/initialized")

        # ---------------------------------------------------- 版本降级
        print("\n[2] 协议版本协商（不支持的版本应回落到默认）")
        r2 = c.request("initialize", {"protocolVersion": "1999-01-01",
                                      "capabilities": {}, "clientInfo": {}})
        check("未知版本回落到 2024-11-05",
              r2.get("result", {}).get("protocolVersion") == "2024-11-05",
              r2.get("result", {}).get("protocolVersion"))

        # ---------------------------------------------------- 工具列表
        print("\n[3] tools/list")
        r3 = c.request("tools/list")
        tools = r3.get("result", {}).get("tools", [])
        names = [t["name"] for t in tools]
        check("返回 5 个工具", len(tools) == 5, str(names))
        for want in ("memory_search", "memory_get", "memory_capture",
                     "memory_stats", "memory_reindex"):
            check(f"包含 {want}", want in names)
        check("每个工具都有 inputSchema",
              all("inputSchema" in t and "description" in t for t in tools))

        # ---------------------------------------------------- 检索
        print("\n[4] tools/call → memory_search")
        r4 = c.request("tools/call", {"name": "memory_search",
                                      "arguments": {"query": "密钥", "limit": 3}})
        content = r4.get("result", {}).get("content", [])
        text = content[0]["text"] if content else ""
        check("返回 content 数组", bool(content))
        check("content type 为 text", content and content[0].get("type") == "text")
        check("检索到内容", "命中" in text and "命中 0" not in text, text[:120])
        check("未标记为错误", not r4.get("result", {}).get("isError"))

        # 从结果里抠出 id，供下一步用
        import re
        m = re.search(r"id=(\d+)", text)
        card_id = int(m.group(1)) if m else None

        print("\n[5] tools/call → memory_get")
        if card_id is None:
            check("能从检索结果解析出 id", False, text[:120])
        else:
            r5 = c.request("tools/call", {"name": "memory_get",
                                          "arguments": {"id": card_id}})
            t5 = r5.get("result", {}).get("content", [{}])[0].get("text", "")
            check(f"取回 id={card_id} 的全文", len(t5) > 100, t5[:80])

        print("\n[6] tools/call → memory_stats")
        r6 = c.request("tools/call", {"name": "memory_stats", "arguments": {}})
        t6 = r6.get("result", {}).get("content", [{}])[0].get("text", "")
        try:
            stats = json.loads(t6)
            check("stats 返回合法 JSON", True)
            check("stats 含 total 字段", "total" in stats, str(stats)[:120])
            check("卡片数 > 0", stats.get("total", 0) > 0, str(stats.get("total")))
        except json.JSONDecodeError:
            check("stats 返回合法 JSON", False, t6[:120])

        # ---------------------------------------------------- 错误处理
        print("\n[7] 错误处理")
        r7 = c.request("tools/call", {"name": "no_such_tool", "arguments": {}})
        check("未知工具返回 isError",
              r7.get("result", {}).get("isError") is True, str(r7)[:150])

        r8 = c.request("tools/call", {"name": "memory_search", "arguments": {}})
        check("缺 query 参数返回 isError",
              r8.get("result", {}).get("isError") is True, str(r8)[:150])

        r9 = c.request("nonexistent/method")
        check("未知方法返回 -32601",
              r9.get("error", {}).get("code") == -32601, str(r9)[:150])

        r10 = c.raw("{ this is not json")
        check("畸形 JSON 返回 -32700",
              r10.get("error", {}).get("code") == -32700, str(r10)[:150])

        r11 = c.request("ping")
        check("ping 返回空 result", r11.get("result") == {}, str(r11)[:120])

        # ---------------------------------------------------- 采集端
        print("\n[8] 采集端闭环（隔离到临时 vault，不碰真实记忆）")
        import tempfile
        tmp = tempfile.mkdtemp(prefix="memory-agent-test-")
        c2 = Client(env={
            "MEMORY_AGENT_VAULT": str(Path(tmp) / "vault"),
            "MEMORY_AGENT_DB": str(Path(tmp) / "memory.db"),
        })
        try:
            c2.request("initialize", {"protocolVersion": "2025-06-18",
                                      "capabilities": {},
                                      "clientInfo": {"name": "test-agent", "version": "2.0"}})
            c2.notify("notifications/initialized")

            title = "测试卡：示例服务器登录方式"
            body = ("示例主机使用密钥登录，密钥位于 ~/.ssh/id_ed25519_example，"
                    "SSH 别名为 example-host。修改 web 服务配置后需执行 reload 才生效。")

            r = c2.request("tools/call", {"name": "memory_capture",
                                          "arguments": {"title": title, "body": body,
                                                        "kind": "knowledge",
                                                        "tags": ["测试", "ecs"]}})
            t = r.get("result", {}).get("content", [{}])[0].get("text", "")
            check("capture 返回结果", bool(t), t[:120])
            try:
                out = json.loads(t)
                check("首次写入 action=created", out.get("action") == "created", str(out))
                check("返回写入路径", bool(out.get("path")), str(out))
            except json.JSONDecodeError:
                check("capture 返回合法 JSON", False, t[:120])

            # 幂等：同样内容再写一次
            r = c2.request("tools/call", {"name": "memory_capture",
                                          "arguments": {"title": title, "body": body}})
            t = r.get("result", {}).get("content", [{}])[0].get("text", "")
            try:
                out = json.loads(t)
                check("重复写入 action=unchanged（幂等）",
                      out.get("action") == "unchanged", str(out))
            except json.JSONDecodeError:
                check("重复写入返回合法 JSON", False, t[:120])

            # 过短正文应被拒绝
            r = c2.request("tools/call", {"name": "memory_capture",
                                          "arguments": {"title": "太短", "body": "嗯"}})
            check("过短正文被拒绝",
                  r.get("result", {}).get("isError") is True, str(r)[:120])

            # 建索引后应能检索到
            c2.request("tools/call", {"name": "memory_reindex", "arguments": {}})
            r = c2.request("tools/call", {"name": "memory_search",
                                          "arguments": {"query": "密钥"}})
            t = r.get("result", {}).get("content", [{}])[0].get("text", "")
            check("写入的卡片可被检索到", "命中 1" in t or "命中 2" in t, t[:150])

            # 中文标题应生成可读文件名
            files = list((Path(tmp) / "vault").rglob("*.md"))
            check("落盘 1 个卡片文件", len(files) == 1, str([f.name for f in files]))
            check("归入 03-Knowledge 目录",
                  files and "03-Knowledge" in str(files[0]), str(files))
            check("中文标题保留在文件名中",
                  files and "登录" in files[0].name, files[0].name if files else "")
        finally:
            c2.close()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

        # ---------------------------------------------------- 检索档位
        print("\n[9] 检索档位：四档降级与前缀匹配")
        tmp3 = tempfile.mkdtemp(prefix="memory-agent-tier-")
        c3 = Client(env={
            "MEMORY_AGENT_VAULT": str(Path(tmp3) / "vault"),
            "MEMORY_AGENT_DB": str(Path(tmp3) / "memory.db"),
        })
        try:
            c3.request("initialize", {"protocolVersion": "2025-06-18",
                                      "capabilities": {},
                                      "clientInfo": {"name": "test-agent", "version": "3.0"}})
            c3.notify("notifications/initialized")

            c3.request("tools/call", {"name": "memory_capture",
                                      "arguments": {
                                          "title": "档位测试卡",
                                          "body": ("调用 getUserById 获取用户，再用 tokenizeText "
                                                   "处理，依赖 sqlite3 和 requests 库。"),
                                          "kind": "knowledge"}})
            # 第二张卡只含 requests —— 用来验证 AND 档成功时不会降级到 OR
            c3.request("tools/call", {"name": "memory_capture",
                                      "arguments": {
                                          "title": "只含 requests 的卡",
                                          "body": "这张卡里只有 requests 这一个关键词，用于区分档位。",
                                          "kind": "knowledge"}})
            c3.request("tools/call", {"name": "memory_reindex", "arguments": {}})

            def hits(query: str) -> str:
                r = c3.request("tools/call", {"name": "memory_search",
                                              "arguments": {"query": query}})
                return r.get("result", {}).get("content", [{}])[0].get("text", "")

            check("整词命中走 AND 精确档", "AND 精确" in hits("sqlite3"))
            check("前缀档：sqlite 命中 sqlite3", "命中 1" in hits("sqlite"))
            check("前缀档标注为 AND 前缀", "AND 前缀" in hits("sqlite"))
            check("前缀档：request 命中 requests（两张卡都含）", "命中 2" in hits("request"))
            check("前缀档：request 标注为 AND 前缀", "AND 前缀" in hits("request"))
            check("前缀档：tokenize 命中 tokenizeText", "命中 1" in hits("tokenize"))
            check("单字不加前缀，不放大噪音", "没有与" in hits("a"))
            check("词中片段仍搜不到（已知局限，非缺陷）", "没有与" in hits("userById"))

            # 档位优先级：两个词都能被 AND 满足时，不许降级到 OR。
            # 若降级，第二张卡（只含 requests）也会被召回，命中数会变成 2。
            both = hits("sqlite3 requests")
            check("AND 档可满足时不降级到 OR（命中 1 而非 2）", "命中 1" in both)
            check("AND 档可满足时标注 AND 精确", "AND 精确" in both)

            # AND 无法满足时才降级：加入语料里不存在的词，AND 必然失败
            loose = hits("sqlite3 requests zzznotexist")
            check("AND 无法满足时降级到 OR", "OR" in loose)
            check("降级后召回更宽（命中 2）", "命中 2" in loose)
        finally:
            c3.close()
            shutil.rmtree(tmp3, ignore_errors=True)

        # ---------------------------------------------------- stdout 纯净
        print("\n[10] stdout 纯净性（最关键）")
        bad = []
        for line in c.stdout_lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0":
                    bad.append(line)
            except json.JSONDecodeError:
                bad.append(line)
        check(f"全部 {len(c.stdout_lines)} 行均为合法 JSON-RPC", not bad,
              str(bad[:3]))

        stderr = c.close()
        check("stderr 有日志输出（不污染 stdout）", len(stderr) > 0,
              repr(stderr[:80]))

    except Exception as exc:
        check(f"测试过程中未抛异常", False, str(exc))
        c.proc.kill()

    print("\n" + "=" * 62)
    total = _passed + len(_failed)
    print(f"结果：{_passed}/{total} 通过")
    if _failed:
        print("失败项：")
        for f in _failed:
            print(f"  - {f}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
