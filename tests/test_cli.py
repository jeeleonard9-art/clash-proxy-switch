#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨平台测试：不依赖真实 Clash 客户端，用 mock_controller.py 伪造控制器。

在 Windows / macOS / Linux 上都能跑：
    python -m unittest discover -s tests -v

覆盖四类风险：
1. 路径解析（%APPDATA% 在不同系统必须映射到不同真实目录）
2. 客户端规格的平台过滤（mac 才有 ClashX，win 才有 CFW）
3. 实例聚合（同一目录下的 GUI/内核/服务不能算成三个客户端）
4. 命令行全链路（探测→切换→回读→回滚）与退出码语义
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import clash_proxy as cp  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def run_cli(args, timeout=120):
    """跑 CLI，返回 (exitcode, parsed_json_or_None, raw_stdout, stderr)。"""
    proc = subprocess.run([sys.executable, str(SCRIPTS / "clash_proxy.py")] + args,
                          capture_output=True, timeout=timeout)
    out = proc.stdout.decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(out) if out else None
    except ValueError:
        parsed = None
    return proc.returncode, parsed, out, proc.stderr.decode("utf-8", "replace")


class TestPathResolution(unittest.TestCase):
    """%APPDATA% 这类 Windows 语义必须能在三平台上落到真实目录。"""

    def test_appdata_points_to_real_dir(self):
        p = cp.expand("%APPDATA%/io.github.clash-verge-rev.clash-verge-rev")
        self.assertNotIn("%APPDATA%", str(p), "占位符没有被展开")
        self.assertIn("clash-verge-rev", str(p))
        if sys.platform == "darwin":
            self.assertIn("Application Support", str(p))
        elif os.name != "nt":
            self.assertIn(".config", str(p))

    def test_home_token(self):
        p = cp.expand("~/.config/mihomo")
        self.assertTrue(str(p).startswith(str(Path.home())), f"~ 未展开: {p}")

    def test_no_leftover_placeholders_in_specs(self):
        for spec in cp.ACTIVE_SPECS:
            for d in spec.get("config_dirs", []):
                expanded = str(cp.expand(d))
                self.assertNotIn("%", expanded, f"{spec['key']} 的配置目录残留占位符: {expanded}")


class TestPlatformFiltering(unittest.TestCase):
    def test_platform_specific_clients(self):
        keys = {s["key"] for s in cp.ACTIVE_SPECS}
        if sys.platform == "darwin":
            self.assertIn("clashx_meta", keys, "macOS 上应启用 ClashX")
            self.assertNotIn("clash_for_windows", keys, "macOS 不该出现 CFW")
        elif os.name == "nt":
            self.assertIn("clash_for_windows", keys)
            self.assertNotIn("clashx_meta", keys)
        else:
            self.assertNotIn("clashx_meta", keys)
            self.assertNotIn("clash_for_windows", keys)

    def test_classify(self):
        self.assertEqual(cp.classify("Clash Verge Rev")["key"], "clash_verge_rev")
        self.assertEqual(cp.classify("E:/apps/FlClash/FlClash.exe")["key"], "flclash")
        self.assertEqual(cp.classify("verge-mihomo.exe")["key"], "mihomo_core")
        self.assertIsNone(cp.classify("v2rayN"), "非 Clash 系客户端不应被匹配")


class TestAggregation(unittest.TestCase):
    """同一安装目录下的 GUI / 内核 / 服务必须聚成一个候选。"""

    def _inst(self, exe, kind, priority, key="clash_verge_rev", pids=None):
        return {"key": key, "label": key, "exe": exe, "kind": kind, "priority": priority,
                "pids": pids or [], "sources": ["install_dir"], "proc_names": [],
                "version": None}

    def test_same_dir_collapses_to_one(self):
        raw = [
            self._inst("/opt/clash-verge/clash-verge", "gui", 10),
            self._inst("/opt/clash-verge/verge-mihomo", "core", 60, key="mihomo_core"),
            self._inst("/opt/clash-verge/verge-mihomo-alpha", "core", 60, key="mihomo_core"),
        ]
        out = cp.collapse_installs(raw)
        self.assertEqual(len(out), 1, f"应聚成 1 个候选，实际 {len(out)}")
        self.assertEqual(out[0]["kind"], "gui", "代表应是 GUI 而不是内核")
        self.assertEqual(len(out[0]["variants"]), 2)

    def test_service_is_not_running(self):
        raw = [
            self._inst("/opt/x/clash-verge", "gui", 10),
            {"key": "clash_verge_rev", "exe": None, "kind": "service", "priority": 10,
             "pids": [999], "sources": ["process"], "proc_names": ["clash-verge-service"]},
        ]
        out = cp.collapse_installs(raw)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["running"], "helper 服务在跑不代表客户端在跑")
        self.assertIn(999, out[0]["helper_pids"])

    def test_noise_paths_rejected(self):
        self.assertTrue(cp._is_noise_exe("/tmp/Clash Verge-2.5.8-installer.exe"))
        self.assertTrue(cp._is_noise_exe("C:/Users/x/AppData/Local/Temp/clash-setup.exe"))
        self.assertTrue(cp._is_noise_exe("/opt/app/unins000.exe"))
        self.assertFalse(cp._is_noise_exe("/opt/clash-verge/clash-verge"))


class TestCliAgainstMock(unittest.TestCase):
    """用 mock 控制器跑完整状态机，验证退出码与幂等性。"""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.mock = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "mock_controller.py"),
             "--port", str(cls.port), "--secret", "test", "--mode", "rule"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port(cls.port):
            cls.mock.kill()
            raise unittest.SkipTest("mock 控制器未能在 10s 内启动")

    @classmethod
    def tearDownClass(cls):
        cls.mock.terminate()
        try:
            cls.mock.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.mock.kill()

    def setUp(self):
        cp.STATE_FILE.unlink(missing_ok=True)

    def _endpoint(self):
        return ["--endpoint", f"127.0.0.1:{self.port}", "--secret", "test"]

    def test_1_status(self):
        code, data, raw, err = run_cli(["status"] + self._endpoint())
        self.assertEqual(code, 0, f"status 失败: {raw} {err}")
        self.assertTrue(data["ok"])
        self.assertEqual(data["mode"], "rule")
        self.assertEqual(data["controller"]["transport"], "tcp")

    def test_2_ensure_idempotent(self):
        code, data, raw, err = run_cli(["ensure", "--no-launch", "--no-verify"] + self._endpoint())
        self.assertEqual(code, 0, f"ensure 失败: {raw} {err}")
        self.assertEqual(data["mode"], "global")
        self.assertEqual(data["previous_mode"], "rule", "首次 ensure 应记录原始模式")

        # 幂等重跑不得把 origin 污染成 global
        code, data2, raw2, err2 = run_cli(["ensure", "--no-launch", "--no-verify"] + self._endpoint())
        self.assertEqual(code, 0, f"二次 ensure 失败: {raw2} {err2}")
        self.assertEqual(data2["previous_mode"], "rule", "origin 被污染，restore 将失效")
        self.assertEqual(data2["mode_before_this_run"], "global")

    def test_3_off_and_restore(self):
        # 先把 mock 置回 rule，否则 origin 无从验证（setUp 已清空状态文件）
        run_cli(["off", "--mode", "rule"] + self._endpoint())
        code, data, raw, err = run_cli(["ensure", "--no-launch", "--no-verify"] + self._endpoint())
        self.assertEqual(code, 0, f"ensure 失败: {raw} {err}")
        self.assertEqual(data["previous_mode"], "rule")

        code, data, _, err = run_cli(["restore"])
        self.assertEqual(code, 0, f"restore 失败: {err}")
        self.assertEqual(data["restored_mode"], "rule")

        # 不只看返回文案，回读确认 mock 真的回到了 rule
        code, st, _, _ = run_cli(["status"] + self._endpoint())
        self.assertEqual(st["mode"], "rule", "restore 后实际模式未回滚")

    def test_4_wrong_secret_rejected(self):
        """鉴权失败必须在探测层就判负，不能误当成可用控制器。"""
        self.assertIsNone(cp.probe_controller("127.0.0.1", self.port, ["WRONG"]))
        self.assertIsNotNone(cp.probe_controller("127.0.0.1", self.port, ["test"]),
                             "正确 secret 应能探测到控制器")

    def test_5_explicit_endpoint_is_strict(self):
        """--endpoint 是严格模式：指错了就直接失败，不许顺延改别的客户端。"""
        code, data, raw, err = run_cli(["ensure", "--no-launch", "--no-verify", "--timeout", "3",
                                        "--endpoint", "127.0.0.1:1"])
        self.assertEqual(code, cp.EXIT_NO_CONTROLLER, f"显式端点不可用应为 13: {raw} {err}")
        self.assertTrue(data.get("degraded"), "失败必须标记 degraded")
        self.assertIsNone(data.get("controller"), "严格模式下不该绑定到其它控制器")


    def test_6_ensure_works_without_any_local_client(self):
        """干净机器（没装 Clash）上，--endpoint 必须照常工作。

        CI 暴露的回归：cmd_ensure 会因为「探测不到本地客户端」直接退出 10，
        根本没走到显式传入的控制器地址。
        """
        import argparse
        import contextlib
        import io

        orig = cp.collect_installs
        cp.collect_installs = lambda **kw: []
        try:
            ns = argparse.Namespace(mode="global", client=None, timeout=5, no_launch=True,
                                    verify=False, deep=False,
                                    endpoint=f"127.0.0.1:{self.port}", secret="test",
                                    kill_on_failure=False, pretty=False)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    cp.cmd_ensure(ns)
            self.assertEqual(ctx.exception.code, 0,
                             f"无本地客户端时 ensure --endpoint 应成功: {buf.getvalue()[:300]}")
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["mode"], "global")
            self.assertEqual(payload["client"], "explicit --endpoint")
        finally:
            cp.collect_installs = orig


class TestZeroClientEnvironment(unittest.TestCase):
    """干净 runner 上没有任何客户端时，必须优雅降级而不是崩。"""

    def test_detect_returns_json(self):
        code, data, raw, err = run_cli(["detect"], timeout=180)
        self.assertIn(code, (0, cp.EXIT_NOT_FOUND), f"detect 异常退出 {code}: {err[:300]}")
        self.assertIsNotNone(data, f"detect 未输出合法 JSON: {raw[:300]}")
        self.assertIn("installs", data)

    def test_doctor_runs(self):
        code, data, raw, err = run_cli(["doctor"], timeout=300)
        self.assertEqual(code, 0, f"doctor 失败: {err[:300]}")
        self.assertIn("checks", data)

    def test_missing_state_file(self):
        cp.STATE_FILE.unlink(missing_ok=True)
        code, data, raw, err = run_cli(["restore"])
        self.assertEqual(code, cp.EXIT_NO_STATE, f"无状态文件应为 17: {raw} {err}")


class TestMcpServer(unittest.TestCase):
    """MCP stdio 协议：握手、列工具、调用、错误回传。"""

    def _session(self, messages):
        payload = "".join(json.dumps(m) + "\n" for m in messages).encode()
        proc = subprocess.run([sys.executable, str(SCRIPTS / "mcp_server.py")],
                              input=payload, capture_output=True, timeout=180)
        lines = [ln for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
        return [json.loads(ln) for ln in lines]

    def test_handshake_and_tools(self):
        res = self._session([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual(len(res), 2, "通知不应产生回包")
        self.assertIn("serverInfo", res[0]["result"])
        names = {t["name"] for t in res[1]["result"]["tools"]}
        self.assertEqual(names, {"clash_proxy_ensure", "clash_proxy_status",
                                 "clash_proxy_off", "clash_proxy_restore"})

    def test_unknown_tool_returns_error(self):
        res = self._session([
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "not_a_tool", "arguments": {}}},
        ])
        self.assertEqual(len(res), 1)
        self.assertIn("error", res[0])
        self.assertEqual(res[0]["id"], 7)

    def test_status_tool_degrades_without_client(self):
        res = self._session([
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": "clash_proxy_status", "arguments": {}}},
        ])
        result = res[0]["result"]
        self.assertIn("content", result)
        # 干净 runner 上没有控制器，应返回结构化降级 JSON 而不是崩
        payload = json.loads(result["content"][0]["text"])
        self.assertIn("code", payload)


class TestNonHttpPort(unittest.TestCase):
    """现实里存在「端口讲的是别的协议」：FlClash 的私有 JSON-RPC 控制口就会这样。

    探测必须判负而不是抛异常，否则一条 status 就能把整个 CLI 打挂。
    """

    def _fake_jsonrpc_port(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break
                try:
                    conn.recv(2048)
                    conn.sendall(b'{"method":"getTraffic","data":false,"id":1}\r\n')
                except OSError:
                    pass
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass

        threading.Thread(target=serve, daemon=True).start()
        return srv, port, stop

    def test_probe_returns_none(self):
        srv, port, stop = self._fake_jsonrpc_port()
        try:
            self.assertIsNone(cp.probe_controller("127.0.0.1", port, [""]),
                              "非 HTTP 对端应判负而不是抛异常")
            self.assertIsNone(cp.probe_controller("127.0.0.1", port, ["secret"]))
        finally:
            stop.set()
            srv.close()

    def test_cli_degrades_not_crashes(self):
        srv, port, stop = self._fake_jsonrpc_port()
        try:
            code, data, raw, err = run_cli(["status", "--endpoint", f"127.0.0.1:{port}"])
            self.assertEqual(code, cp.EXIT_NO_CONTROLLER,
                             f"应降级为 13 而不是内部错误(2): {raw} {err[:200]}")
            self.assertFalse(data["ok"])
        finally:
            stop.set()
            srv.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
