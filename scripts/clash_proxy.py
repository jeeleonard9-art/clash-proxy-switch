#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""clash_proxy.py - Detect Clash clients and switch selected instance to global proxy mode.

Enhanced for cross-platform compatibility, REST API retries/timeouts, atomic
state persistence, and expanded diagnostic checks.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

# Ensure UTF-8 encoding across standard streams
if hasattr(sys.stdin, "reconfigure"):
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

VERSION = "1.1.0"
STATE_DIR = Path.home() / ".workbuddy" / "state"
STATE_FILE = STATE_DIR / "clash-proxy-state.json"

DEFAULT_TIMEOUT = 5.0
MAX_RETRIES = 3
BACKOFF_FACTOR = 0.5

# Standard Exit Codes
EXIT_OK = 0
EXIT_ARGS = 2
EXIT_NOT_FOUND = 10
EXIT_LAUNCH_FAILED = 11
EXIT_ELEVATION = 12
EXIT_NO_CONTROLLER = 13
EXIT_MODE_FAILED = 14
EXIT_VERIFY_FAILED = 15
EXIT_NO_STATE = 17

# Proxy environment variable pairs (case-insensitive)
PROXY_ENV_KEYS = [
    ("HTTP_PROXY", "http_proxy"),
    ("HTTPS_PROXY", "https_proxy"),
    ("ALL_PROXY", "all_proxy"),
    ("NO_PROXY", "no_proxy"),
]

LOOPBACK_IP = "127.0.0.1"
COMMON_CONTROLLER_URLS = [
    f"http://{LOOPBACK_IP}:9090",
    f"http://{LOOPBACK_IP}:9097",
    f"http://{LOOPBACK_IP}:9091",
    "http://localhost:9090",
    "http://localhost:9097",
]

COMMON_PROXY_PORTS = [
    7890, 7897, 10809, 10808, 8888, 8080,
    7891, 1080, 1089
]


# REST API Exception Hierarchy
class ClashAPIError(Exception):
    """Base error for REST API operations."""
    pass


class ClashConnectionError(ClashAPIError):
    """Connection or network error."""
    pass


class ClashTimeoutError(ClashAPIError):
    """Request timeout."""
    pass


class ClashHTTPError(ClashAPIError):
    """HTTP status error."""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"HTTP {code}: {message}")


def http_request_with_retry(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    data: bytes | str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = MAX_RETRIES,
    backoff_factor: float = BACKOFF_FACTOR,
) -> tuple[int, str]:
    """Executes an HTTP request with exponential backoff retry and timeout control."""
    if isinstance(data, str):
        data = data.encode("utf-8")

    req_headers = headers or {}
    req = urlrequest.Request(url, headers=req_headers, method=method, data=data)

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status, body
        except urlerror.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
            if e.code in (502, 503, 504) and attempt < retries:
                time.sleep(backoff_factor * (2 ** (attempt - 1)))
                last_exc = ClashHTTPError(e.code, body)
                continue
            raise ClashHTTPError(e.code, body) from e
        except (urlerror.URLError, socket.timeout, TimeoutError, OSError) as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff_factor * (2 ** (attempt - 1)))
                continue
            if isinstance(e, (socket.timeout, TimeoutError)):
                raise ClashTimeoutError(f"Request timed out after {timeout}s (retries={retries})") from e
            raise ClashConnectionError(f"Connection error to {url}: {e}") from e

    if last_exc:
        raise ClashConnectionError(f"Failed after {retries} retries: {last_exc}") from last_exc
    raise ClashConnectionError("Unknown network failure")


def get_proxy_env_vars() -> dict[str, str]:
    """Retrieves all active proxy environment variables (checking both upper and lower case)."""
    res = {}
    for u_key, l_key in PROXY_ENV_KEYS:
        if u_key in os.environ:
            res[u_key] = os.environ[u_key]
        if l_key in os.environ:
            res[l_key] = os.environ[l_key]
    return res


def set_proxy_env_vars(
    http_url: str | None = None,
    https_url: str | None = None,
    all_url: str | None = None,
    no_proxy: str | None = None,
):
    """Sets proxy environment variables for both upper and lower case keys."""
    mapping = {
        ("HTTP_PROXY", "http_proxy"): http_url,
        ("HTTPS_PROXY", "https_proxy"): https_url,
        ("ALL_PROXY", "all_proxy"): all_url,
        ("NO_PROXY", "no_proxy"): no_proxy,
    }
    for (u_key, l_key), val in mapping.items():
        if val is not None:
            os.environ[u_key] = val
            os.environ[l_key] = val


def unset_proxy_env_vars():
    """Unsets all proxy environment variables (both upper and lower case)."""
    for u_key, l_key in PROXY_ENV_KEYS:
        os.environ.pop(u_key, None)
        os.environ.pop(l_key, None)


def auto_detect_controller() -> str | None:
    """Probes known controller endpoints to find an active Clash/Mihomo instance."""
    for url in COMMON_CONTROLLER_URLS:
        try:
            status, _ = http_request_with_retry(f"{url}/version", timeout=1.5, retries=1)
            if status == 200:
                return url
        except Exception:
            pass
    return None


def save_state_atomic(state: dict, file_path: Path = STATE_FILE):
    """Atomically writes state dictionary to file using temporary file replacement."""
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = file_path.with_suffix(f".tmp.{os.getpid()}")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, file_path)
    except Exception as e:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass
        raise OSError(f"Failed to save state atomically to {file_path}: {e}") from e


def load_state_safe(file_path: Path = STATE_FILE) -> tuple[dict, str]:
    """Safely loads state from file, handling corrupted JSON or missing files gracefully."""
    file_path = Path(file_path)
    if not file_path.exists():
        return {}, "State file does not exist"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("State content is not a JSON object")
        return data, "OK"
    except (json.JSONDecodeError, ValueError, OSError) as e:
        backup_path = file_path.with_suffix(".corrupted.bak")
        try:
            if file_path.exists():
                os.replace(file_path, backup_path)
        except Exception:
            pass
        sys.stderr.write(f"[Warning] Corrupted state file detected ({e}). Backed up to {backup_path}.\n")
        return {}, f"Corrupted ({e})"


def run_detect(args):
    """Detects active Clash instances on the system."""
    ctrl_url = auto_detect_controller()
    instances = []
    if ctrl_url:
        instances.append({
            "name": "AutoDetectedClash",
            "controller": ctrl_url,
            "status": "running"
        })
    out = {
        "status": "ok",
        "detected_instances": instances,
        "count": len(instances)
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return EXIT_OK


def run_status(args):
    """Checks status of current Clash controller."""
    ctrl_url = getattr(args, "controller", None) or auto_detect_controller()
    if not ctrl_url:
        print(json.dumps({"status": "error", "message": "No controller endpoint reachable"}, ensure_ascii=False))
        return EXIT_NO_CONTROLLER
    try:
        status_code, body = http_request_with_retry(f"{ctrl_url}/configs")
        cfg = json.loads(body)
        out = {
            "status": "ok",
            "controller": ctrl_url,
            "mode": cfg.get("mode", "unknown"),
            "port": cfg.get("port"),
            "socks_port": cfg.get("socks-port"),
            "mixed_port": cfg.get("mixed-port")
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return EXIT_OK
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Failed to get status from {ctrl_url}: {e}"}, ensure_ascii=False))
        return EXIT_NO_CONTROLLER


def run_ensure(args):
    """Ensures global mode on Clash controller and saves state."""
    ctrl_url = getattr(args, "controller", None) or auto_detect_controller()
    if not ctrl_url:
        print(json.dumps({"status": "error", "message": "No controller endpoint available"}, ensure_ascii=False))
        return EXIT_NO_CONTROLLER

    try:
        # Save previous state
        st_code, body = http_request_with_retry(f"{ctrl_url}/configs")
        prev_cfg = json.loads(body) if st_code == 200 else {}
        prev_mode = prev_cfg.get("mode", "rule")

        state = {
            "controller": ctrl_url,
            "previous_mode": prev_mode,
            "env_backup": get_proxy_env_vars(),
            "timestamp": time.time()
        }
        save_state_atomic(state)

        # Switch to global mode
        payload = json.dumps({"mode": "global"}).encode("utf-8")
        http_request_with_retry(
            f"{ctrl_url}/configs",
            method="PATCH",
            headers={"Content-Type": "application/json"},
            data=payload
        )

        out = {
            "status": "ok",
            "controller": ctrl_url,
            "mode": "global",
            "previous_mode": prev_mode,
            "state_saved": str(STATE_FILE)
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return EXIT_OK
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Failed to set global mode on {ctrl_url}: {e}"}, ensure_ascii=False))
        return EXIT_MODE_FAILED


def run_off(args):
    """Switches Clash mode to rule/direct."""
    ctrl_url = getattr(args, "controller", None) or auto_detect_controller()
    if not ctrl_url:
        print(json.dumps({"status": "error", "message": "No controller endpoint available"}, ensure_ascii=False))
        return EXIT_NO_CONTROLLER

    target_mode = getattr(args, "mode", "rule") or "rule"
    try:
        payload = json.dumps({"mode": target_mode}).encode("utf-8")
        http_request_with_retry(
            f"{ctrl_url}/configs",
            method="PATCH",
            headers={"Content-Type": "application/json"},
            data=payload
        )
        out = {
            "status": "ok",
            "controller": ctrl_url,
            "mode": target_mode
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return EXIT_OK
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Failed to set mode {target_mode}: {e}"}, ensure_ascii=False))
        return EXIT_MODE_FAILED


def run_restore(args):
    """Restores state from state file."""
    state, state_msg = load_state_safe(STATE_FILE)
    if not state:
        print(json.dumps({"status": "error", "message": f"No valid state file found ({state_msg})"}, ensure_ascii=False))
        return EXIT_NO_STATE

    ctrl_url = state.get("controller") or auto_detect_controller()
    prev_mode = state.get("previous_mode", "rule")
    if ctrl_url:
        try:
            payload = json.dumps({"mode": prev_mode}).encode("utf-8")
            http_request_with_retry(
                f"{ctrl_url}/configs",
                method="PATCH",
                headers={"Content-Type": "application/json"},
                data=payload
            )
        except Exception as e:
            sys.stderr.write(f"Warning: Failed restoring mode on controller: {e}\n")

    out = {
        "status": "ok",
        "restored_mode": prev_mode,
        "restored_controller": ctrl_url
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return EXIT_OK


def run_doctor(args):
    """Comprehensive diagnostic checks."""
    results = {
        "status": "ok",
        "version": VERSION,
        "timestamp": time.time(),
        "checks": {}
    }

    # 1. Controller Connectivity
    ctrl_url = getattr(args, "controller", None) or auto_detect_controller()
    if ctrl_url:
        try:
            st, body = http_request_with_retry(f"{ctrl_url}/version", timeout=3.0, retries=2)
            ver_info = json.loads(body) if st == 200 else {}
            results["checks"]["controller"] = {
                "status": "ok",
                "url": ctrl_url,
                "version": ver_info.get("version", "unknown")
            }
        except Exception as e:
            results["checks"]["controller"] = {
                "status": "error",
                "url": ctrl_url,
                "error": str(e)
            }
            results["status"] = "degraded"
    else:
        results["checks"]["controller"] = {
            "status": "warning",
            "message": "No active controller auto-detected or specified"
        }

    # 2. Proxy Ports Reachability
    open_ports = []
    for port in COMMON_PROXY_PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            s.connect((LOOPBACK_IP, port))
            open_ports.append(port)
            s.close()
        except Exception:
            pass
    results["checks"]["proxy_ports"] = {
        "status": "ok" if open_ports else "warning",
        "open_ports": open_ports,
        "checked_ports": COMMON_PROXY_PORTS
    }

    # 3. Environment Variables Consistency
    env_vars = get_proxy_env_vars()
    results["checks"]["env_vars"] = {
        "status": "ok",
        "active_env_vars": env_vars,
        "count": len(env_vars)
    }

    # 4. State File Health
    state_data, state_health = load_state_safe(STATE_FILE)
    results["checks"]["state_file"] = {
        "status": "ok" if "OK" in state_health else "warning",
        "path": str(STATE_FILE),
        "health": state_health,
        "state_keys": list(state_data.keys()) if state_data else []
    }

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return EXIT_OK


def main():
    parser = argparse.ArgumentParser(description="Clash proxy switch management tool")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # detect
    p_detect = subparsers.add_parser("detect", help="Detect Clash instances")
    p_detect.set_defaults(func=run_detect)

    # status
    p_status = subparsers.add_parser("status", help="Get controller status")
    p_status.add_argument("--controller", help="Controller REST API URL")
    p_status.set_defaults(func=run_status)

    # ensure
    p_ensure = subparsers.add_parser("ensure", help="Ensure global mode and save state")
    p_ensure.add_argument("--controller", help="Controller REST API URL")
    p_ensure.set_defaults(func=run_ensure)

    # off
    p_off = subparsers.add_parser("off", help="Turn off global mode (rule/direct)")
    p_off.add_argument("--controller", help="Controller REST API URL")
    p_off.add_argument("--mode", default="rule", help="Mode to set (rule/direct)")
    p_off.set_defaults(func=run_off)

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore previous state")
    p_restore.set_defaults(func=run_restore)

    # doctor
    p_doctor = subparsers.add_parser("doctor", help="Run diagnostic checks")
    p_doctor.add_argument("--controller", help="Controller REST API URL")
    p_doctor.set_defaults(func=run_doctor)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
