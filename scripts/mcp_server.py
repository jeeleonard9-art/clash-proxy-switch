#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp_server.py - MCP stdio server wrapping clash_proxy.py CLI.

Ensures cross-platform stdio stability (UTF-8, line-buffered, logs to stderr)
and robust parameter validation with structured error handling.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Force UTF-8 reconfiguration on stdio standard streams
if hasattr(sys.stdin, "reconfigure"):
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

SERVER_NAME = "clash-proxy-switch"
SERVER_VERSION = "1.1.0"
DEFAULT_PROTOCOL = "2024-11-05"
CLI = Path(__file__).resolve().with_name("clash_proxy.py")
CALL_TIMEOUT = 180


def log_stderr(msg: str):
    """Logs strictly to sys.stderr to avoid corrupting stdout JSON-RPC framing."""
    sys.stderr.write(f"[{SERVER_NAME}] {msg}\n")
    sys.stderr.flush()


TOOLS = [
    {
        "name": "clash_detect",
        "description": "Detect running Clash client instances and endpoints.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "clash_status",
        "description": "Get current status of Clash controller.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "controller": {
                    "type": "string",
                    "description": "Controller REST API URL",
                }
            },
        },
    },
    {
        "name": "clash_ensure",
        "description": "Ensure global mode on Clash and backup state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "controller": {
                    "type": "string",
                    "description": "Controller REST API URL",
                }
            },
        },
    },
    {
        "name": "clash_off",
        "description": "Switch Clash proxy mode off (rule or direct).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "controller": {
                    "type": "string",
                    "description": "Controller REST API URL",
                },
                "mode": {
                    "type": "string",
                    "description": "Target mode (rule/direct)",
                    "default": "rule",
                },
            },
        },
    },
    {
        "name": "clash_restore",
        "description": "Restore Clash state from saved state file.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "clash_doctor",
        "description": "Run diagnostic checks on Clash controller & environment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "controller": {
                    "type": "string",
                    "description": "Controller REST API URL",
                }
            },
        },
    },
]


def run_cli_command(subcommand: str, args: dict | None = None) -> dict:
    """Executes clash_proxy.py subprocess safely with parameter validation."""
    args = args or {}
    if not isinstance(args, dict):
        return {
            "isError": True,
            "content": [
                {"type": "text", "text": "Validation Error: 'arguments' must be a JSON object"}
            ],
        }

    cmd = [sys.executable, str(CLI), subcommand]

    if "controller" in args and args["controller"]:
        if not isinstance(args["controller"], str):
            return {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": "Validation Error: 'controller' parameter must be a string",
                    }
                ],
            }
        cmd.extend(["--controller", str(args["controller"])])

    if subcommand == "off" and "mode" in args and args["mode"]:
        if not isinstance(args["mode"], str):
            return {
                "isError": True,
                "content": [
                    {"type": "text", "text": "Validation Error: 'mode' must be a string"}
                ],
            }
        cmd.extend(["--mode", str(args["mode"])])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=CALL_TIMEOUT,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if stderr:
            log_stderr(f"CLI stderr output: {stderr}")

        try:
            parsed = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError:
            parsed = {"raw_output": stdout, "raw_error": stderr}

        is_error = proc.returncode != 0
        return {
            "isError": is_error,
            "content": [{
                "type": "text",
                "text": json.dumps(
                    {
                        "exit_code": proc.returncode,
                        "result": parsed,
                        "stderr": stderr,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            }],
        }
    except subprocess.TimeoutExpired:
        return {
            "isError": True,
            "content": [{
                "type": "text",
                "text": f"Error: Subprocess command '{subcommand}' timed out after {CALL_TIMEOUT}s",
            }],
        }
    except Exception as e:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Execution Exception: {str(e)}"}],
        }


def send_response(response: dict):
    """Writes JSON-RPC response strictly to stdout with explicit flush."""
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def dispatch_request(req: dict):
    """Parses JSON-RPC request and dispatches responses cleanly."""
    if not isinstance(req, dict):
        send_response({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request: expected JSON object"},
        })
        return

    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params", {})

    if not method:
        send_response({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32600, "message": "Missing method in request"},
        })
        return

    if method == "initialize":
        send_response({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": DEFAULT_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        })
        return

    if method == "notifications/initialized":
        return

    if method == "tools/list":
        send_response({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        })
        return

    if method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})

        mapping = {
            "clash_detect": "detect",
            "clash_status": "status",
            "clash_ensure": "ensure",
            "clash_off": "off",
            "clash_restore": "restore",
            "clash_doctor": "doctor",
        }

        if tool_name not in mapping:
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": True,
                    "content": [
                        {"type": "text", "text": f"Unknown tool: '{tool_name}'"}
                    ],
                },
            })
            return

        result = run_cli_command(mapping[tool_name], tool_args)
        send_response({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result,
        })
        return

    send_response({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: '{method}'"},
    })


def main():
    log_stderr(f"Starting {SERVER_NAME} v{SERVER_VERSION} MCP Stdio Server...")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            dispatch_request(req)
        except json.JSONDecodeError as e:
            send_response({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {str(e)}"},
            })
        except Exception as e:
            log_stderr(f"Server loop error: {e}")


if __name__ == "__main__":
    main()
