#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_server.py — 把 clash_proxy.py 包成一个 MCP stdio server。

用途：没有 skill 机制、只支持 MCP 的 agent（Claude Desktop / Cursor / Cline / Continue 等）
可以通过它调用同一套逻辑。skill 与 MCP 共用 `clash_proxy.py`，不存在两套实现。

手工测试：
    echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python mcp_server.py

协议：JSON-RPC 2.0，按行分隔，stdout 只允许出现协议消息（日志一律走 stderr）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SERVER_NAME = "clash-proxy-switch"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2024-11-05"
CLI = Path(__file__).resolve().with_name("clash_proxy.py")
CALL_TIMEOUT = 180


def log(msg):
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


TOOLS = [
    {
        "name": "clash_proxy_ensure",
        "description": (
            "确保本机 Clash 类客户端处于全局代理模式：探测已安装客户端 → 必要时启动 → "
            "PATCH 控制器 mode=global → 回读校验 → 可选验证出口。幂等，可重复调用。"
            "返回 JSON 含 ok/code/egress；egress.auto_routed=false 时表示系统流量不会自动走代理，"
            "需把 egress.proxy_url 作为 HTTP_PROXY 使用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["global", "rule", "direct"],
                         "default": "global", "description": "目标模式"},
                "client": {"type": "string",
                           "description": "强制指定客户端 key 或可执行文件名，多实例时使用"},
                "no_launch": {"type": "boolean", "default": False,
                              "description": "只复用已运行的实例，不启动新进程"},
                "timeout": {"type": "integer", "default": 60,
                            "description": "冷启动后等待控制器就绪的秒数"},
                "verify": {"type": "boolean", "default": True,
                           "description": "是否验证代理出口连通性"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "clash_proxy_status",
        "description": "查看本机 Clash 控制器的传输方式（tcp/pipe）、当前模式与混合端口，做任何决定前可先查。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "clash_proxy_off",
        "description": "把代理模式切回 rule（规则）或 direct（直连），用于任务结束收尾。",
        "inputSchema": {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["rule", "direct"], "default": "rule"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "clash_proxy_restore",
        "description": "按状态文件回滚到上一次 ensure 之前的模式，可选关闭由 ensure 启动的客户端。",
        "inputSchema": {
            "type": "object",
            "properties": {"close": {"type": "boolean", "default": False}},
            "additionalProperties": False,
        },
    },
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def run_cli(args):
    """调用 CLI 并返回 (exitcode, stdout, stderr)。"""
    try:
        proc = subprocess.run([sys.executable, str(CLI)] + args,
                              capture_output=True, timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时（{CALL_TIMEOUT}s）"
    except OSError as exc:
        return 127, "", f"无法执行 {CLI}: {exc}"
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def handle_tool_call(name, arguments):
    arguments = arguments or {}
    if name == "clash_proxy_ensure":
        args = ["ensure", "--mode", str(arguments.get("mode") or "global"),
                "--timeout", str(int(arguments.get("timeout") or 60))]
        if arguments.get("client"):
            args += ["--client", str(arguments["client"])]
        if arguments.get("no_launch"):
            args.append("--no-launch")
        args.append("--verify" if arguments.get("verify", True) else "--no-verify")
    elif name == "clash_proxy_status":
        args = ["status"]
    elif name == "clash_proxy_off":
        args = ["off", "--mode", str(arguments.get("mode") or "rule")]
    elif name == "clash_proxy_restore":
        args = ["restore"] + (["--close"] if arguments.get("close") else [])
    else:
        return {"content": [{"type": "text", "text": f"未知工具: {name}"}], "isError": True}

    code, out, err = run_cli(args)
    if code == 124 or code == 127:
        return {"content": [{"type": "text", "text": err or "执行失败"}], "isError": True}
    text = out.strip() or json.dumps({"ok": False, "code": code, "message": err.strip()[:400]},
                                     ensure_ascii=False)
    # 只有「参数/内部错误」算工具调用失败；10-15 是业务降级，交给模型读 JSON 决策
    return {"content": [{"type": "text", "text": text}], "isError": code == 2}


def dispatch(method, params):
    if method == "initialize":
        requested = (params or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        return {
            "protocolVersion": requested,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = params or {}
        name = params.get("name")
        if name not in TOOL_NAMES:
            raise ValueError(f"未知工具: {name}")
        return handle_tool_call(name, params.get("arguments"))
    if method == "ping":
        return {}
    raise ValueError(f"未实现的方法: {method}")


def main():
    # stdout 是 MCP 协议通道，必须强制 UTF-8：
    # 英文 Windows 的默认管道编码是 cp1252，写中文（工具返回的 message 全是中文）
    # 会抛 UnicodeEncodeError，整个 server 直接挂掉。
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            log(f"非法 JSON，已忽略: {line[:120]}")
            continue
        msg_id = msg.get("id")
        method = msg.get("method")
        if method is None:
            continue
        # 通知（无 id）不需要回包
        if msg_id is None:
            log(f"notification: {method}")
            continue
        try:
            result = dispatch(method, msg.get("params"))
            payload = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except ValueError as exc:
            payload = {"jsonrpc": "2.0", "id": msg_id,
                       "error": {"code": -32601, "message": str(exc)}}
        except Exception as exc:  # 任何异常都必须回包，否则客户端会一直等
            log(f"dispatch error: {exc}")
            payload = {"jsonrpc": "2.0", "id": msg_id,
                       "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    if not CLI.is_file():
        log(f"找不到 CLI: {CLI}")
        sys.exit(1)
    if os.environ.get("CLASH_MCP_SELFTEST"):
        print(json.dumps({"cli": str(CLI), "tools": sorted(TOOL_NAMES)}, ensure_ascii=False))
        sys.exit(0)
    main()
