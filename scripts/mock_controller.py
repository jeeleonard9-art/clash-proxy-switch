#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_controller.py — 伪造一个最小可用的 Clash 外部控制器，用于离线测试。

实现真实控制器里本工具会用到的三个端点：
    GET  /version   -> {"version": "...", "meta": true}
    GET  /configs   -> 含 mode / mixed-port 的配置对象
    PATCH /configs  -> 校验 Bearer secret 后改写 mode，返回 204

用法:
    python mock_controller.py --port 9099 --secret test --mode rule
"""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"mode": "rule", "mixed-port": 7899, "port": 7899, "socks-port": 7898,
         "log-level": "info", "allow-lan": False}


class Handler(BaseHTTPRequestHandler):
    secret = ""
    server_version = "mock-mihomo/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[mock] " + (fmt % args) + "\n")

    def _auth_ok(self):
        got = self.headers.get("Authorization", "")
        if not self.secret:
            return True
        return got == f"Bearer {self.secret}"

    def _send(self, code, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if not self._auth_ok():
            return self._send(401, {"message": "Unauthorized"})
        if self.path == "/version":
            return self._send(200, {"version": "v1.19.0-mock", "meta": True})
        if self.path == "/configs":
            return self._send(200, STATE)
        if self.path == "/proxies":
            return self._send(200, {"proxies": {"GLOBAL": {"type": "Selector", "now": "MOCK"}}})
        return self._send(404, {"message": "not found"})

    def do_PATCH(self):
        if not self._auth_ok():
            return self._send(401, {"message": "Unauthorized"})
        if self.path != "/configs":
            return self._send(404, {"message": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"message": "bad json"})
        for key in ("mode", "mixed-port", "port", "allow-lan", "log-level"):
            if key in payload:
                STATE[key] = payload[key]
        sys.stderr.write(f"[mock] mode -> {STATE['mode']}\n")
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9099)
    ap.add_argument("--secret", default="test")
    ap.add_argument("--mode", default="rule")
    args = ap.parse_args()
    STATE["mode"] = args.mode
    Handler.secret = args.secret
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    sys.stderr.write(f"[mock] listening 127.0.0.1:{args.port} secret={args.secret!r} mode={args.mode}\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
