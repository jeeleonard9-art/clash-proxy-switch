#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clash_proxy.py — 检测本机 Clash 类客户端，并把选中的实例切换到「全局代理」模式。

子命令:
    detect   只读扫描本机 Clash 类客户端（进程 / 注册表 / 安装目录 / 配置目录）
    status   探测当前 Clash 控制器与代理模式
    ensure   探测 -> 选实例 -> 必要时启动 -> 切全局 -> 校验（幂等，失败可降级）
    off      把模式切回 rule/direct
    restore  按状态文件回滚到 ensure 之前的状态
    doctor   环境自检

仅依赖 Python 标准库（3.8+）。机器可解析输出统一为单段 JSON（stdout）。
详细设计取舍见 ../references/design.md
"""

from __future__ import annotations

import argparse
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

try:
    import winreg  # type: ignore
except ImportError:  # 非 Windows
    winreg = None

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

VERSION = "1.0.0"
SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = Path.home() / ".workbuddy" / "state"
STATE_FILE = STATE_DIR / "clash-proxy-state.json"

# ---------------------------------------------------------------- 退出码
EXIT_OK = 0
EXIT_ARGS = 2
EXIT_NOT_FOUND = 10          # 本机没有任何 clash 类客户端
EXIT_LAUNCH_FAILED = 11      # 全部候选都启动失败
EXIT_ELEVATION = 12          # 需要管理员权限
EXIT_NO_CONTROLLER = 13      # 客户端在跑，但拿不到控制接口
EXIT_MODE_FAILED = 14        # 控制接口可用，但设置 mode 失败
EXIT_VERIFY_FAILED = 15      # mode 已设，但代理出口验证不通过
EXIT_NO_STATE = 17           # restore 时没有状态文件

CONTROLLER_TIMEOUT_DEFAULT = 60   # 冷启动 GUI 到控制器就绪的等待上限（秒）
MAX_LAUNCH_ATTEMPTS = 3           # 最多尝试启动几个不同的客户端
VERIFY_URL = "http://www.gstatic.com/generate_204"

# ---------------------------------------------------------------- 客户端矩阵
# patterns 用于把「进程名 / 目录名 / 注册表 DisplayName」归类到某个客户端；
# 顺序即匹配优先级（越靠前越具体）。priority 越小越优先被选中。
# platforms 决定该条在哪个系统上参与匹配，避免 mac/Linux 上跑 Windows 专属探测。
# config_dirs 用统一模板：%APPDATA% 在 Windows 取环境变量、macOS 映射到
# ~/Library/Application Support、Linux 映射到 ~/.config（见 expand()）。
CLIENT_SPECS = [
    {
        "key": "clash_verge_rev",
        "label": "Clash Verge Rev",
        "platforms": ["win", "mac", "linux"],
        "patterns": [r"clash[-_. ]?verge"],
        "exe_names": ["clash-verge.exe", "clash-verge", "Clash Verge"],
        "config_dirs": [
            r"%APPDATA%/io.github.clash-verge-rev.clash-verge-rev",
            r"~/.config/io.github.clash-verge-rev.clash-verge-rev",
            r"~/.local/share/io.github.clash-verge-rev.clash-verge-rev",
        ],
        "controller_ports": [9097],
        "priority": 10,
    },
    {
        "key": "clash_nyanpasu",
        "label": "Clash Nyanpasu",
        "platforms": ["win", "mac", "linux"],
        "patterns": [r"nyanpasu"],
        "exe_names": ["Clash Nyanpasu.exe", "clash-nyanpasu.exe", "clash-nyanpasu"],
        "config_dirs": [
            r"%APPDATA%/moe.elaina.clash.nyanpasu",
            r"%APPDATA%/clash-nyanpasu",
            r"~/.config/clash-nyanpasu",
        ],
        "controller_ports": [9090],
        "priority": 20,
    },
    {
        "key": "clashx_meta",
        "label": "ClashX Meta / ClashX Pro (macOS)",
        "platforms": ["mac"],
        "patterns": [r"clashx[-_. ]?meta", r"clashx", r"clashx pro"],
        "exe_names": ["ClashX Meta", "ClashX Pro", "ClashX"],
        "config_dirs": [r"~/.config/clash", r"~/.config/clashx"],
        "controller_ports": [9090],
        "priority": 25,
    },
    {
        "key": "mihomo_party",
        "label": "Mihomo Party",
        "platforms": ["win", "mac", "linux"],
        "patterns": [r"mihomo[-_. ]?party", r"party\.mihomo"],
        "exe_names": ["Mihomo Party.exe", "mihomo-party.exe", "Mihomo Party"],
        "config_dirs": [
            r"%APPDATA%/mihomo-party",
            r"%APPDATA%/party.mihomo.app",
            r"~/.config/mihomo-party",
        ],
        "controller_ports": [9090],
        "priority": 28,
    },
    {
        "key": "flclash",
        "label": "FlClash",
        "platforms": ["win", "mac", "linux"],
        "patterns": [r"flclash"],
        "exe_names": ["FlClash.exe", "FlClash"],
        "config_dirs": [r"%APPDATA%/com.follow.clash", r"~/.config/com.follow.clash",
                        r"~/.local/share/com.follow.clash"],
        "controller_ports": [],
        "priority": 30,
        "note": "默认 external-controller 为空，仅靠进程端口探测可能拿不到控制接口",
    },
    {
        "key": "clash_for_windows",
        "label": "Clash for Windows",
        "platforms": ["win"],
        "patterns": [r"clash\s?for\s?windows", r"clash-win64", r"^clash\.exe$"],
        "exe_names": ["Clash for Windows.exe", "clash.exe", "Clash.exe"],
        "config_dirs": [r"%USERPROFILE%/.config/clash", r"%APPDATA%/clash"],
        "controller_ports": [9090],
        "priority": 40,
    },
    {
        "key": "mihomo_core",
        "label": "Mihomo / Clash.Meta core",
        "platforms": ["win", "mac", "linux"],
        "patterns": [r"mihomo", r"clash[-_.]?meta", r"^clash$"],
        "exe_names": ["mihomo.exe", "clash-meta.exe", "verge-mihomo.exe", "mihomo", "clash-meta"],
        "config_dirs": [
            r"~/.config/mihomo",
            r"%APPDATA%/mihomo",
            r"~/.config/clash",
            r"/etc/mihomo",
            r"/etc/clash",
        ],
        "controller_ports": [9090],
        "priority": 60,
        "kind": "core",
    },
    {
        "key": "unknown_clash",
        "label": "Unidentified Clash-family client",
        "platforms": ["win", "mac", "linux"],
        "patterns": [r"clash"],
        "exe_names": [],
        "config_dirs": [],
        "controller_ports": [9090],
        "priority": 90,
    },
]

def platform_key():
    return "win" if IS_WIN else ("mac" if IS_MAC else "linux")


ACTIVE_SPECS = [s for s in CLIENT_SPECS if platform_key() in s.get("platforms", ["win", "mac", "linux"])]

FALLBACK_PORTS = [9090, 9097, 9091, 9093, 9095, 9099, 6170, 19090]
SERVICE_HINTS = ("service", "helper", "svc")
CORE_HINTS = ("core", "mihomo.exe", "verge-mihomo.exe", "clash-meta.exe")
# 配置目录模板里的环境变量占位符：%VAR% / $VAR / ${VAR}
_ENV_TOKEN_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
# 系统目录扫描时的排除项，避免把噪声当安装目录
DIR_SKIP = {"common", "windows", "windowsapps", "microsoft", "google", "mozilla"}


# ---------------------------------------------------------------- 基础工具
def log(msg: str) -> None:
    if os.environ.get("CLASH_PROXY_DEBUG"):
        print(f"[clash-proxy] {msg}", file=sys.stderr)


def run_text(cmd, timeout=15):
    """跑命令并返回 (returncode, stdout)。Windows 中文环境下逐级尝试解码。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"run_text failed: {cmd} -> {exc}")
        return 127, ""
    raw = p.stdout or b""
    for enc in ("utf-8", "mbcs" if IS_WIN else "latin-1", "gbk", "latin-1"):
        try:
            return p.returncode, raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return p.returncode, raw.decode("utf-8", "replace")


def expand(path: str) -> Path:
    """跨平台展开配置目录模板。

    Python 自带的 expandvars 在 POSIX 上不认识 %APPDATA%，会让配置目录静默失效；
    这里自己解析 %VAR% / $VAR / ${VAR}，并把 Windows 语义的环境变量映射到 mac/Linux 的
    实际位置（macOS 的应用数据在 ~/Library/Application Support，Linux 在 ~/.config）。
    """
    pk = platform_key()
    aliases = {
        "APPDATA": {"mac": "~/Library/Application Support", "linux": "~/.config"},
        "LOCALAPPDATA": {"mac": "~/Library/Caches", "linux": "~/.local/share"},
        "USERPROFILE": {"mac": "~", "linux": "~"},
        "PROGRAMFILES": {"mac": "/Applications", "linux": "/opt"},
        "PROGRAMDATA": {"mac": "/Library/Application Support", "linux": "/etc"},
    }
    out = path
    for _ in range(4):
        before = out

        def repl(m):
            name = (m.group(1) or m.group(2) or m.group(3)).upper()
            return (os.environ.get(name)
                    or aliases.get(name, {}).get(pk)
                    or m.group(0))

        out = _ENV_TOKEN_RE.sub(repl, out)
        if out.startswith("~"):
            out = str(Path.home()) + out[1:]
        if out == before:
            break
    return Path(os.path.normpath(out))


def classify(text: str):
    """把任意字符串归类到当前平台有效的客户端规格；匹配不到返回 None。"""
    low = (text or "").lower()
    for spec in ACTIVE_SPECS:
        for pat in spec["patterns"]:
            if re.search(pat, low):
                return spec
    return None


def is_service_like(name: str) -> bool:
    return any(h in name.lower() for h in SERVICE_HINTS)


def is_core_like(name: str) -> bool:
    return any(h in name.lower() for h in CORE_HINTS)


# ---------------------------------------------------------------- 探测：进程
def scan_processes():
    """返回 [{pid, name, exe}]；exe 可能为 None（权限不足）。"""
    procs = []
    if IS_WIN:
        code, out = run_text([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,ExecutablePath "
            "| ConvertTo-Json -Compress",
        ], timeout=25)
        if code == 0 and out.strip():
            try:
                data = json.loads(out)
                if isinstance(data, dict):
                    data = [data]
                for row in data:
                    procs.append({
                        "pid": int(row.get("ProcessId") or 0),
                        "name": row.get("Name") or "",
                        "exe": row.get("ExecutablePath") or None,
                    })
                return procs
            except (ValueError, TypeError) as exc:
                log(f"CIM json parse failed: {exc}")
        code, out = run_text(["tasklist", "/fo", "csv", "/nh"], timeout=20)
        for line in out.splitlines():
            parts = [c.strip('"') for c in line.split('","')]
            if len(parts) >= 2 and parts[0]:
                procs.append({"pid": int(parts[1]) if parts[1].isdigit() else 0,
                              "name": parts[0], "exe": None})
        return procs
    # POSIX：comm 在 Linux 上会被截断到 15 字符，改用 args 首个参数取 basename，
    # 同时能拿到绝对路径（等价于 Windows 的 ExecutablePath）。
    code, out = run_text(["ps", "-eo", "pid=,args="], timeout=10)
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, args = line.partition(" ")
        args = args.strip()
        if not pid_str.isdigit() or not args:
            continue
        argv0 = args.split()[0]
        name = os.path.basename(argv0)
        exe = argv0 if argv0.startswith("/") else None
        procs.append({"pid": int(pid_str), "name": name, "exe": exe})
    return procs


# ---------------------------------------------------------------- 探测：注册表
def scan_registry():
    """Windows 卸载表 -> [{name, location, icon, version}]。"""
    if not winreg:
        return []
    roots = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    found = []
    for hive, sub in roots:
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                child = winreg.OpenKey(key, winreg.EnumKey(key, i))
                row = {}
                for val in ("DisplayName", "InstallLocation", "DisplayIcon", "DisplayVersion"):
                    try:
                        row[val] = winreg.QueryValueEx(child, val)[0]
                    except OSError:
                        row[val] = ""
                row["DisplayName"] = str(row["DisplayName"])
                row["InstallLocation"] = str(row["InstallLocation"]).strip('" ')
                row["DisplayIcon"] = str(row["DisplayIcon"]).strip('" ')
                if row["DisplayName"] and classify(row["DisplayName"] + " " + row["InstallLocation"]):
                    found.append({
                        "name": row["DisplayName"],
                        "location": row["InstallLocation"],
                        "icon": row["DisplayIcon"],
                        "version": str(row["DisplayVersion"]),
                    })
            except OSError:
                continue
        try:
            key.Close()
        except Exception:
            pass
    return found


# ---------------------------------------------------------------- 探测：目录
def default_scan_roots():
    roots = []
    if IS_WIN:
        for env in ("LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"):
            val = os.environ.get(env)
            if val:
                roots.append(Path(val) / "Programs" if env in ("LOCALAPPDATA", "APPDATA") else Path(val))
        home = Path.home()
        roots += [home / "Desktop", home / "Downloads", home / "AppData" / "Local"]
    elif IS_MAC:
        roots += [Path("/Applications"), Path.home() / "Applications",
                  Path.home() / "Library" / "Application Support"]
    else:
        roots += [Path("/opt"), Path("/usr/local"), Path("/usr/bin"), Path("/usr/sbin"),
                  Path.home() / ".local" / "bin"]
    return [r for r in roots if r.exists()]


def _clash_like(name: str) -> bool:
    low = name.lower()
    return any(k in low for k in ("clash", "mihomo", "verge", "nyanpasu"))


# 名字里带这些的不是「已安装的客户端」，而是安装包/更新器/卸载器
NOISE_HINTS = ("installer", "setup", "unins", "uninstall", "updater", "-update", "portable")


def _is_noise_exe(path) -> bool:
    name = Path(path).name.lower()
    if any(h in name for h in NOISE_HINTS):
        return True
    # 临时目录里的可执行文件一律不算安装（下载完没清的安装包）
    return bool(re.search(r"[\\/](temp|tmp|\.tmp)[\\/]", str(path).lower()))


def _is_executable(p: Path) -> bool:
    if not p.is_file():
        return False
    if IS_WIN:
        return p.suffix.lower() == ".exe"
    return os.access(p, os.X_OK)


def scan_dirs(roots, max_depth=2):
    """在给定根目录下找 Clash 类可执行文件。

    三种形态都要覆盖：
      - Windows：``<目录>/*.exe``
      - macOS：``*.app/Contents/MacOS/<binary>``（.app 是目录，不能只看一层）
      - Linux：``/usr/bin/mihomo`` 这类直接躺在根目录下的可执行文件
    """
    hits = []

    def add(p: Path):
        s = str(p)
        if s not in hits:
            hits.append(s)

    for root in roots:
        if not root or not root.exists():
            continue
        stack = [(root, 0)]
        while stack:
            cur, depth = stack.pop()
            if depth > max_depth:
                continue
            try:
                entries = list(os.scandir(cur))
            except (OSError, PermissionError):
                continue
            for ent in entries:
                p = Path(ent.path)
                low = ent.name.lower()
                try:
                    if ent.is_dir(follow_symlinks=False):
                        if low in DIR_SKIP or low.startswith("$"):
                            continue
                        if low.endswith(".app"):
                            macos = p / "Contents" / "MacOS"
                            if macos.is_dir():
                                for f in macos.iterdir():
                                    if _is_executable(f) and _clash_like(f.name):
                                        add(f)
                            continue
                        if _clash_like(low):
                            try:
                                children = list(p.iterdir())
                            except OSError:
                                children = []
                            for f in children:
                                if _is_executable(f) and _clash_like(f.name):
                                    add(f)
                            stack.append((p, depth + 1))
                        else:
                            stack.append((p, depth + 1))
                    elif _is_executable(p) and _clash_like(ent.name):
                        add(p)
                except OSError:
                    continue
    return hits


# ---------------------------------------------------------------- 配置解析
def find_config_file(config_dirs):
    """在候选配置目录里挑出最可能是运行时配置的文件。"""
    best = None
    for d in config_dirs:
        d = expand(d)
        if not d.exists():
            continue
        for cand in ("config.yaml", "clash-verge.yaml", "runtime.yaml"):
            p = d / cand
            if p.is_file():
                return p
        for p in sorted(d.glob("*.yaml"), key=lambda x: -x.stat().st_mtime):
            if p.is_file() and p.stat().st_size < 4 * 1024 * 1024:
                best = best or p
            if best:
                break
    return best


def parse_clash_config(path):
    """只读顶层标量键（含 tun.enable），避免依赖 PyYAML。"""
    info = {}
    if not path or not Path(path).is_file():
        return info
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return info
    section = None
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        top = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if top:
            key, val = top.group(1), top.group(2).split("#")[0].strip().strip("\"'")
            section = key if not val else None
            if key in ("external-controller", "secret", "mixed-port", "port", "socks-port", "mode"):
                info[key] = val
            continue
        nested = re.match(r"^\s+([A-Za-z0-9_-]+):\s*(.*)$", line)
        if nested and section == "tun" and nested.group(1) == "enable":
            info["tun-enable"] = nested.group(2).split("#")[0].strip().strip("\"'")
    return info


def system_proxy_state():
    """读取 Windows 系统代理开关，判断 agent 自身流量是否会自动走代理。"""
    if not (IS_WIN and winreg):
        return {"enabled": None, "server": ""}
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enabled = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
        except OSError:
            enabled = False
        try:
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0])
        except OSError:
            server = ""
        return {"enabled": enabled, "server": server}
    except OSError as exc:
        log(f"read system proxy failed: {exc}")
        return {"enabled": None, "server": ""}


def config_dir_of(spec):
    return [expand(p) for p in spec.get("config_dirs", [])]


# ---------------------------------------------------------------- 控制器 API
def http_json(host, port, path, secret=None, method="GET", body=None, timeout=2.0):
    url = f"http://{host}:{port}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None)


def port_is_open(host, port, timeout=0.4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_controller(host, port, secrets):
    """GET /version 判定是否 Clash 控制器；返回 {endpoint, secret, version, meta} 或 None。"""
    if not port_is_open(host, port):
        return None
    tried = []
    for secret in secrets:
        try:
            status, payload = http_json(host, port, "/version", secret=secret, timeout=1.5)
            if status == 200 and isinstance(payload, dict) and "version" in payload:
                return {
                    "transport": "tcp",
                    "endpoint": f"{host}:{port}",
                    "secret": secret or "",
                    "version": str(payload.get("version")),
                    "meta": bool(payload.get("meta")),
                }
        except urlerror.HTTPError as exc:
            tried.append(exc.code)
        except (urlerror.URLError, OSError, ValueError) as exc:
            log(f"probe {host}:{port} secret={'(set)' if secret else '(none)'} -> {exc}")
            break
    log(f"port {port} not a clash controller (codes={tried})")
    return None


# ------------------------------------------------- 命名管道传输（Windows）
# 不少 GUI（Clash Verge Rev 服务模式、FlClash）不监听 TCP，只在
# \\.\pipe\<name> 上跑同一套 REST API；配置里写的管道名还会随版本/模式变化，
# 因此必须枚举 \\.\pipe\ 而不能只信配置。
PIPE_ROOT = r"\\.\pipe"


def list_clash_pipes():
    if not IS_WIN:
        return []
    try:
        names = os.listdir(PIPE_ROOT)
    except OSError as exc:
        log(f"list pipe failed: {exc}")
        return []
    hits = [n for n in names
            if any(k in n.lower() for k in ("mihomo", "clash", "verge"))
            and "service" not in n.lower()]
    return hits


def pipe_request(name, method, path, secret=None, body=None, timeout=4.0):
    """在命名管道上收发一条 HTTP/1.1 请求，返回 (status, json) 或 None。"""
    full = PIPE_ROOT + "\\" + name
    payload = b""
    headers = ["Host: localhost", "Accept: application/json", "Connection: close"]
    if secret:
        headers.append(f"Authorization: Bearer {secret}")
    if body is not None:
        payload = json.dumps(body).encode()
        headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {len(payload)}")
    request = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n"
    try:
        handle = open(full, "r+b", buffering=0)
    except OSError as exc:
        log(f"pipe open failed {name}: {exc}")
        return None
    chunks = []
    try:
        handle.write(request.encode() + payload)
        deadline = time.time() + timeout
        while time.time() < deadline:
            blob = b"".join(chunks)
            head, sep, rest = blob.partition(b"\r\n\r\n")
            if sep:
                m = re.search(rb"content-length:\s*(\d+)", head, re.I)
                if m and len(rest) >= int(m.group(1)):
                    break
            piece = handle.read(65536)
            if not piece:
                break
            chunks.append(piece)
            if len(b"".join(chunks)) > 4 * 1024 * 1024:
                break
    except OSError as exc:
        log(f"pipe rw failed {name}: {exc}")
    finally:
        try:
            handle.close()
        except OSError:
            pass
    blob = b"".join(chunks)
    head, sep, rest = blob.partition(b"\r\n\r\n")
    if not sep:
        return None
    try:
        status = int(head.split(b" ")[1])
    except (IndexError, ValueError):
        return None
    try:
        data = json.loads(rest) if rest.strip() else None
    except ValueError:
        data = None
    return status, data


def probe_pipe_controller(name, secrets):
    for secret in secrets:
        res = pipe_request(name, "GET", "/version", secret=secret, timeout=2.0)
        if res and res[0] == 200 and isinstance(res[1], dict) and "version" in res[1]:
            return {
                "transport": "pipe",
                "pipe": name,
                "endpoint": f"pipe://{name}",
                "secret": secret or "",
                "version": str(res[1].get("version")),
                "meta": bool(res[1].get("meta")),
            }
    return None


def ctrl_request(ctrl, method, path, body=None, timeout=4.0):
    """按控制器声明的传输方式发请求，屏蔽 TCP / 命名管道差异。"""
    if ctrl.get("transport") == "pipe":
        return pipe_request(ctrl["pipe"], method, path,
                            secret=ctrl.get("secret"), body=body, timeout=timeout)
    host, _, port = ctrl["endpoint"].rpartition(":")
    return http_json(host or "127.0.0.1", int(port), path,
                     secret=ctrl.get("secret"), method=method, body=body, timeout=timeout)



def listening_ports_of_pids(pids):
    """把进程 PID 集合映射到它监听的 TCP 端口。"""
    if not pids:
        return []
    pids = {int(p) for p in pids if p}
    ports = []
    if IS_WIN:
        code, out = run_text(["netstat", "-ano", "-p", "TCP"], timeout=20)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3].upper() == "LISTENING":
                try:
                    pid = int(parts[4])
                except ValueError:
                    continue
                if pid in pids:
                    local = parts[1]
                    port = local.rsplit(":", 1)[-1]
                    if port.isdigit():
                        ports.append(int(port))
    elif IS_MAC:
        # macOS 没有 ss；lsof -F 输出 p<pid> / n<addr:port> 交替成对，按当前 pid 归属
        code, out = run_text(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"], timeout=15)
        cur_pid = None
        for line in out.splitlines():
            if line.startswith("p"):
                cur_pid = int(line[1:]) if line[1:].isdigit() else None
            elif line.startswith("n") and cur_pid in pids:
                port = line.rsplit(":", 1)[-1].strip()
                if port.isdigit():
                    ports.append(int(port))
    else:
        # Linux：ss -p 需要 root 才能看到他人进程，能读到就利用，读不到退化为默认端口
        code, out = run_text(["ss", "-ltnp"], timeout=10)
        for line in out.splitlines():
            for pid in pids:
                if f"pid={pid}," in line:
                    m = re.search(r":(\d+)\s", line)
                    if m:
                        ports.append(int(m.group(1)))
    # 去重并保持顺序，跳过明显不是控制器的端口
    seen, ordered = set(), []
    for p in ports:
        if p not in seen and p > 0:
            seen.add(p)
            ordered.append(p)
    return ordered


def discover_controller(installs, deep=False, extra_endpoint=None, extra_secret=None):
    """探测顺序：显式指定 -> 配置声明的 TCP -> 命名管道 -> 进程实占端口 -> 默认口 -> 深扫。

    把命名管道放在「TCP 失败之后、端口猜测之前」，是因为「配置里写了端口但实际
    没绑定」是真实存在的部署形态（Clash Verge Rev 服务模式），此时管道是唯一入口。

    ``extra_endpoint``（--endpoint）是**严格模式**：一旦显式指定，就只认它，
    探测失败直接返回 None，绝不静默改用别的客户端——否则用户以为在操作 A，
    实际被改了 B，属于不可接受的副作用。
    """
    def secrets_of(inst=None):
        cand = [extra_secret]
        if inst:
            cand.append((inst.get("config_info") or {}).get("secret"))
        cand.append("")
        return list(dict.fromkeys([s for s in cand if s is not None]))

    if extra_endpoint:
        if extra_endpoint.startswith("pipe:"):
            return probe_pipe_controller(extra_endpoint[5:], secrets_of())
        host, _, port = extra_endpoint.rpartition(":")
        if not port.isdigit():
            log(f"非法 --endpoint: {extra_endpoint}")
            return None
        return probe_controller(host or "127.0.0.1", int(port), secrets_of())

    # 1) 配置声明的 TCP 控制器
    for inst in installs:
        ec = (inst.get("config_info") or {}).get("external-controller", "")
        if ec:
            host, _, port = ec.rpartition(":")
            if port.isdigit():
                hit = probe_controller(host or "127.0.0.1", int(port), secrets_of(inst))
                if hit:
                    return hit

    # 2) 命名管道（Windows 独有，Verge 服务模式 / FlClash 的实际入口）
    for inst in installs:
        for name in list_clash_pipes():
            hit = probe_pipe_controller(name, secrets_of(inst))
            if hit:
                return hit

    # 3) 进程实占端口（可命中随机端口）
    for inst in installs:
        for port in listening_ports_of_pids(inst.get("pids") or []):
            hit = probe_controller("127.0.0.1", int(port), secrets_of(inst))
            if hit:
                return hit

    # 4) 常见默认口
    for inst in installs:
        for port in inst.get("controller_ports") or []:
            hit = probe_controller("127.0.0.1", int(port), secrets_of(inst))
            if hit:
                return hit
    for port in FALLBACK_PORTS:
        hit = probe_controller("127.0.0.1", int(port), secrets_of())
        if hit:
            return hit

    # 5) 深扫
    if deep:
        for port in range(9000, 9101):
            hit = probe_controller("127.0.0.1", port, secrets_of())
            if hit:
                return hit
    return None


def get_mode(ctrl):
    try:
        res = ctrl_request(ctrl, "GET", "/configs", timeout=4)
        if res and res[0] == 200 and isinstance(res[1], dict):
            return res[1]
    except (urlerror.URLError, OSError, ValueError) as exc:
        log(f"get_mode failed: {exc}")
    return None


def set_mode(ctrl, mode):
    last = None
    for attempt in range(2):
        try:
            res = ctrl_request(ctrl, "PATCH", "/configs", body={"mode": mode}, timeout=6)
            if res and res[0] in (200, 204):
                return True, None
            last = f"HTTP {res[0]}" if res else "控制器无响应"
            if res and res[0] in (401, 403):
                last += "（secret 不匹配，检查客户端设置里的密钥）"
                break
        except urlerror.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code in (401, 403):
                last += "（secret 不匹配，检查客户端设置里的密钥）"
                break
        except (urlerror.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(0.4)
    return False, last


def verify_egress(mixed_port, timeout=6):
    """通过混合端口访问一个 204 探测点，验证隧道确实通了。"""
    if not mixed_port:
        return None
    proxy = f"http://127.0.0.1:{mixed_port}"
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        with opener.open(VERIFY_URL, timeout=timeout) as resp:
            return {"ok": resp.status in (200, 204, 301, 302), "status": resp.status, "via": proxy}
    except urlerror.HTTPError as exc:
        return {"ok": exc.code in (200, 204), "status": exc.code, "via": proxy}
    except (urlerror.URLError, OSError) as exc:
        return {"ok": False, "status": None, "via": proxy, "error": str(exc)[:160]}


# ---------------------------------------------------------------- 汇总探测
def collect_installs(scan_roots=None, deep=False):
    """合并四路证据 -> 候选列表（按 exe 路径去重）。"""
    procs = scan_processes()
    clash_procs = [p for p in procs if classify(p["name"])]

    evidence = {}  # 归一化 key -> install

    def key_of(path):
        return (path or "").lower().replace("/", "\\")

    for p in clash_procs:
        spec = classify(p["name"])
        if not spec:
            continue
        # 用 exe 路径优先归类，避免 verge-mihomo.exe 被当成独立客户端
        spec = classify(p["exe"]) or spec if p["exe"] else spec
        k = key_of(p["exe"]) or f"proc:{p['name'].lower()}"
        inst = evidence.setdefault(k, {
            "exe": p["exe"], "spec": spec, "pids": [], "sources": [], "proc_names": [],
        })
        inst["pids"].append(p["pid"])
        inst["proc_names"].append(p["name"])
        if "process" not in inst["sources"]:
            inst["sources"].append("process")

    for row in scan_registry():
        spec = classify(row["name"] + " " + row["location"])
        if not spec:
            continue
        exe = None
        icon = row["icon"]
        if icon.lower().endswith(".exe"):
            exe = icon
        elif row["location"]:
            exe = pick_exe_in_dir(Path(row["location"]))
        k = key_of(exe) or f"reg:{row['name'].lower()}"
        inst = evidence.setdefault(k, {
            "exe": exe, "spec": spec, "pids": [], "sources": [], "proc_names": [],
            "version": row["version"],
        })
        inst["version"] = inst.get("version") or row["version"]
        inst["display_name"] = row["name"]
        if "registry" not in inst["sources"]:
            inst["sources"].append("registry")

    roots = list(scan_roots or [])
    for row in scan_registry():
        if row["location"] and Path(row["location"]).exists():
            roots.append(Path(row["location"]).parent)
    roots += default_scan_roots()
    for exe_path in scan_dirs(roots, max_depth=2 if deep else 1):
        spec = classify(exe_path)
        if not spec:
            continue
        if is_service_like(Path(exe_path).name) or _is_noise_exe(exe_path):
            continue
        k = key_of(exe_path)
        inst = evidence.setdefault(k, {
            "exe": exe_path, "spec": spec, "pids": [], "sources": [], "proc_names": [],
        })
        if "install_dir" not in inst["sources"]:
            inst["sources"].append("install_dir")

    # 补齐配置与评分
    raw = []
    for inst in evidence.values():
        spec = inst["spec"]
        exe_path = Path(inst["exe"]) if inst["exe"] else None
        inst["exe_exists"] = bool(exe_path and exe_path.is_file())
        cfg_path = find_config_file(spec.get("config_dirs", []))
        inst["config_file"] = str(cfg_path) if cfg_path else None
        inst["config_info"] = parse_clash_config(cfg_path) if cfg_path else {}
        inst["controller_ports"] = [int(p) for p in
                                    ([inst["config_info"]["external-controller"].rsplit(":", 1)[-1]]
                                     if inst["config_info"].get("external-controller", "").rsplit(":", 1)[-1].isdigit()
                                     else [])] or list(spec.get("controller_ports", []))
        inst["key"] = spec["key"]
        inst["label"] = spec["label"]
        inst["priority"] = spec.get("priority", 90)
        inst["running"] = bool(inst["pids"])
        inst["kind"] = kind_of(inst["exe"], inst["proc_names"])
        if not inst["exe_exists"] and not inst["running"]:
            continue
        raw.append(inst)

    installs = collapse_installs(raw)
    for inst in installs:
        inst["score"] = score_install(inst)
    installs.sort(key=lambda x: (-x["score"], (x["exe"] or "").lower()))
    return installs


def kind_of(exe, proc_names):
    """区分 GUI 客户端 / 裸内核 / 后台服务，决定能否作为启动与切换目标。"""
    names = [n.lower() for n in (proc_names or []) if n]
    if exe:
        names.append(Path(exe).name.lower())
    if not names:
        return "unknown"
    if any(is_service_like(n) for n in names):
        return "service"
    for n in names:
        if "party" in n:            # Mihomo Party 是 GUI，不是裸内核
            return "gui"
        if re.search(r"(mihomo|clash[-_.]?meta|core)\b.*\.exe$", n) or re.search(r"^(mihomo|clash)$", n):
            return "core"
    return "gui"


KIND_RANK = {"gui": 0, "unknown": 1, "core": 2, "service": 3}


def collapse_installs(raw):
    """同一客户端目录下的 gui + core + service 合并为一个候选。

    否则 verge-mihomo.exe / FlClashCore.exe / *HelperService.exe 会被当成
    三个「客户端」，导致启动次数与评分全部失真。
    """
    groups = {}
    orphans = []
    for inst in raw:
        if inst.get("exe"):
            # 按「安装目录」聚合：同一目录下的 GUI / 内核 / 卸载器属于同一个客户端，
            # 若按 (客户端key, 目录) 聚合，verge-mihomo.exe 会自成一家，产生幽灵实例。
            gkey = str(Path(inst["exe"]).parent).lower()
            groups.setdefault(gkey, []).append(inst)
        else:
            orphans.append(inst)

    collapsed = []
    by_group = {}
    for gkey, members in groups.items():
        members.sort(key=lambda m: (
            KIND_RANK.get(m["kind"], 9),
            m.get("priority", 90),
            len(Path(m["exe"]).name),
        ))
        rep = dict(members[0])
        rep["pids"] = sorted({p for m in members for p in m.get("pids", [])})
        rep["sources"] = sorted({s for m in members for s in m.get("sources", [])})
        rep["running"] = bool(rep["pids"])
        rep["variants"] = [str(m["exe"]) for m in members[1:]]
        if not rep.get("version"):
            rep["version"] = next((m.get("version") for m in members if m.get("version")), None)
        collapsed.append(rep)
        by_group[gkey] = rep

    def find_host(inst):
        """给「只有进程、拿不到路径」的实例找归属客户端。"""
        names = {n.lower() for n in inst.get("proc_names", []) if n}
        # a) 某个安装目录里存在同名可执行文件 -> 就是这个客户端
        for gkey, rep in by_group.items():
            base = {Path(rep["exe"]).name.lower()} | {Path(v).name.lower() for v in rep["variants"]}
            if names & base:
                return rep
        # b) 客户端类型一致
        same = [c for c in collapsed if c["key"] == inst["key"]]
        if same:
            return same[0]
        # c) 全机只有一个客户端时，孤进程只能属于它
        if len(collapsed) == 1:
            return collapsed[0]
        return None

    # 只有进程没有 exe 的（服务态内核、权限受限的 helper）并入归属客户端，不单列
    for inst in orphans:
        host = find_host(inst)
        if host is None:
            continue
        if inst["kind"] == "service":
            # 服务只是特权 helper，不代表 GUI/内核在跑，不能计入 running
            host["helper_pids"] = sorted(set(host.get("helper_pids", [])) | set(inst.get("pids", [])))
        else:
            host["pids"] = sorted(set(host.get("pids", [])) | set(inst.get("pids", [])))
            host["running"] = True
        host["sources"] = sorted(set(host.get("sources", [])) | set(inst.get("sources", [])))
    return collapsed


def pick_exe_in_dir(d: Path):
    """在安装目录里挑一个最像主程序的 exe。"""
    if not d.exists():
        return None
    best, best_score = None, -1
    for f in d.iterdir() if d.is_dir() else []:
        if not f.is_file() or f.suffix.lower() != ".exe":
            continue
        low = f.name.lower()
        if is_service_like(low) or "unins" in low:
            continue
        score = 0
        if "clash" in low or "mihomo" in low or "verge" in low:
            score += 5
        if is_core_like(low):
            score -= 3
        if score > best_score:
            best, best_score = f, score
    return str(best) if best else None


def score_install(inst):
    """评分：可用性(控制器已知/在跑) > 客户端优先级 > 路径可靠性。"""
    score = 0
    score += 40 - inst["priority"] * 0.3          # 优先级（越靠前分越高）
    if inst["running"]:
        score += 20
    if inst["exe_exists"]:
        score += 10
    if inst.get("config_file"):
        score += 8
    if inst.get("config_info", {}).get("external-controller"):
        score += 12                               # 声明了控制器 = 大概率可 API 切换
    if inst.get("config_info", {}).get("secret"):
        score += 2
    if inst["kind"] == "core":
        score -= 6                                # 裸核心没有 GUI，优先级降低
    if inst["kind"] == "service":
        score -= 40
    if "registry" in inst["sources"]:
        score += 4
    return round(score, 2)


# ---------------------------------------------------------------- 启动
def launch_client(inst, timeout=CONTROLLER_TIMEOUT_DEFAULT):
    """启动客户端（分离进程），返回 {ok, pid, error, code}。"""
    exe = inst.get("exe")
    if not exe or not Path(exe).is_file():
        return {"ok": False, "error": f"可执行文件不存在: {exe}", "code": EXIT_LAUNCH_FAILED}

    args = [exe]
    if inst["kind"] == "core":
        cfg = inst.get("config_file")
        if not cfg:
            return {"ok": False, "error": "核心程序需要 -d 指定配置目录，但未找到配置", "code": EXIT_LAUNCH_FAILED}
        args = [exe, "-d", str(Path(cfg).parent)]

    popen_kw = dict(close_fds=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if IS_WIN:
        popen_kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        popen_kw["start_new_session"] = True                 # 脱离会话，父进程退出后继续存活
        # macOS 的 .app 必须用 open 启动，直接跑 Contents/MacOS 里的二进制
        # 会拿不到 bundle 环境（图标、权限、LSUIElement 都会异常）。
        if IS_MAC:
            bundle = re.search(r"(.*?\.app)/Contents/", str(exe))
            if bundle:
                args = ["open", "-a", bundle.group(1)] + (["--args"] + args[1:] if len(args) > 1 else [])
                popen_kw["cwd"] = str(Path.home())
    try:
        proc = subprocess.Popen(args, cwd=popen_kw.pop("cwd", str(Path(exe).parent)), **popen_kw)
    except PermissionError as exc:
        return {"ok": False, "error": f"权限不足，可能需要管理员/sudo 权限: {exc}",
                "code": EXIT_ELEVATION}
    except OSError as exc:
        return {"ok": False, "error": f"启动失败: {exc}", "code": EXIT_LAUNCH_FAILED}
    log(f"launched {exe} pid={proc.pid}")
    return {"ok": True, "pid": proc.pid, "args": args}


def wait_for_controller(inst, timeout, extra_secret=None):
    """轮询等待控制器就绪（客户端启动后端口是异步监听的）。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        refreshed = refresh_pids(inst)
        last = discover_controller([refreshed], extra_secret=extra_secret)
        if last:
            return last
        time.sleep(1.2)
    return None


def refresh_pids(inst):
    """重新匹配该客户端的进程 PID（启动后 PID 会变）。"""
    if not inst.get("exe"):
        return inst
    procs = scan_processes()
    target = Path(inst["exe"]).name.lower()
    pids = [p["pid"] for p in procs
            if p["name"].lower() == target or
            (p["exe"] and Path(p["exe"]).name.lower() == target)]
    inst = dict(inst)
    inst["pids"] = pids or inst.get("pids", [])
    return inst


# ---------------------------------------------------------------- 状态文件
def read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- 输出
def emit(payload, code=EXIT_OK, pretty=False):
    payload.setdefault("ok", code == EXIT_OK)
    payload.setdefault("code", code)
    payload["tool"] = f"clash-proxy-switch/{VERSION}"
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None))
    sys.exit(code)


def public_install(inst):
    """给 agent 看的精简字段，去掉内部对象。"""
    return {
        "key": inst["key"],
        "label": inst["label"],
        "exe": inst.get("exe"),
        "version": inst.get("version"),
        "running": inst["running"],
        "pids": inst["pids"],
        "helper_pids": inst.get("helper_pids", []),
        "kind": inst["kind"],
        "sources": inst["sources"],
        "config_file": inst.get("config_file"),
        "declared_controller": inst.get("config_info", {}).get("external-controller") or None,
        "score": inst["score"],
    }


# ---------------------------------------------------------------- 子命令
def cmd_detect(args):
    installs = collect_installs(scan_roots=[expand(p) for p in (args.roots or "").split(",") if p],
                                deep=args.deep)
    data = [public_install(i) for i in installs]
    if not data:
        emit({
            "ok": False, "code": EXIT_NOT_FOUND,
            "message": "未发现任何 Clash 类客户端（进程/注册表/安装目录均无匹配）",
            "installs": [],
            "next_actions": [
                "确认已安装 Clash 类客户端；若装在非标准目录，用 --roots 指定安装根目录",
                "用 --deep 展开更深层目录扫描",
            ],
        }, EXIT_NOT_FOUND, args.pretty)
    emit({
        "ok": True, "code": EXIT_OK,
        "message": f"发现 {len(data)} 个 Clash 类客户端",
        "installs": data,
        "scan_scope": {
            "process": True, "registry": bool(winreg), "install_dirs": True,
            "roots": [str(r) for r in default_scan_roots()],
            "deep": args.deep,
        },
    }, EXIT_OK, args.pretty)


def cmd_status(args):
    installs = collect_installs(deep=args.deep)
    ctrl = discover_controller(installs, deep=args.deep,
                               extra_endpoint=args.endpoint, extra_secret=args.secret)
    if not ctrl:
        emit({
            "ok": False, "code": EXIT_NO_CONTROLLER,
            "message": "未发现可用的 Clash 控制器（外部控制接口未开启，或客户端未运行）",
            "installs": [public_install(i) for i in installs],
            "next_actions": [
                "若客户端未运行，执行 ensure 可自动启动",
                "若客户端已在运行，检查设置中的『外部控制 / External Controller』是否开启并记录端口与 secret",
            ],
        }, EXIT_NO_CONTROLLER, args.pretty)
    cfg = get_mode(ctrl) or {}
    emit({
        "ok": True, "code": EXIT_OK,
        "message": f"控制器可用，当前模式 {cfg.get('mode', 'unknown')}",
        "controller": ctrl,
        "mode": cfg.get("mode"),
        "mixed_port": cfg.get("mixed-port") or cfg.get("port"),
        "installs": [public_install(i) for i in installs],
    }, EXIT_OK, args.pretty)


def cmd_ensure(args):
    target_mode = args.mode
    installs = collect_installs(deep=args.deep)
    if not installs:
        emit({
            "ok": False, "code": EXIT_NOT_FOUND, "degraded": True,
            "message": "未发现任何 Clash 类客户端，无法开启代理；请继续原任务或改用直连方案",
            "next_actions": ["告知用户本机未安装 Clash 类客户端", "不要反复重试本工具"],
        }, EXIT_NOT_FOUND, args.pretty)

    if args.client:
        filtered = [i for i in installs if i["key"] == args.client or
                    Path(i.get("exe") or "").name.lower() == args.client.lower()]
        if not filtered:
            emit({
                "ok": False, "code": EXIT_ARGS, "degraded": True,
                "message": f"指定的客户端不存在: {args.client}",
                "available": [i["key"] for i in installs],
            }, EXIT_ARGS, args.pretty)
        installs = filtered

    attempts = []
    ctrl = None
    chosen = None

    # 阶段 1：已有实例且控制器可用 -> 直接复用（最高优先级，零副作用）
    ctrl = discover_controller(installs, deep=args.deep, extra_endpoint=args.endpoint,
                               extra_secret=args.secret)
    if ctrl:
        chosen = next((i for i in installs if i["running"]), installs[0])
        attempts.append({"stage": "reuse", "client": chosen["label"], "endpoint": ctrl["endpoint"],
                         "result": "ok"})

    # 阶段 2：逐个尝试启动候选
    launched = None
    if not ctrl and not args.no_launch:
        for inst in installs[:MAX_LAUNCH_ATTEMPTS]:
            if not inst.get("exe") or not inst["exe_exists"]:
                attempts.append({"stage": "launch", "client": inst["label"],
                                 "result": "skipped", "reason": "无可执行文件"})
                continue
            res = launch_client(inst)
            attempts.append({"stage": "launch", "client": inst["label"], "exe": inst.get("exe"),
                             "result": "ok" if res["ok"] else "fail",
                             "pid": res.get("pid"), "reason": res.get("error")})
            if not res["ok"]:
                if res["code"] == EXIT_ELEVATION and len(installs) == 1:
                    emit({
                        "ok": False, "code": EXIT_ELEVATION, "degraded": True,
                        "message": res["error"], "attempts": attempts,
                        "next_actions": ["以管理员权限重试，或手动启动该客户端后重试 ensure"],
                    }, EXIT_ELEVATION, args.pretty)
                continue
            waited = wait_for_controller(inst, args.timeout, extra_secret=args.secret)
            if waited:
                ctrl, chosen, launched = waited, inst, res
                attempts.append({"stage": "wait_controller", "client": inst["label"],
                                 "result": "ok", "endpoint": waited["endpoint"]})
                break
            attempts.append({"stage": "wait_controller", "client": inst["label"],
                             "result": "timeout", "waited_s": args.timeout})
            if args.kill_on_failure and res.get("pid"):
                terminate(res["pid"])

    if not ctrl:
        launched_any = any(a.get("stage") == "launch" and a.get("result") in ("ok", "fail") for a in attempts)
        timed_out = any(a.get("result") == "timeout" for a in attempts)
        if not launched_any:
            code = EXIT_NO_CONTROLLER
        elif timed_out:
            code = EXIT_NO_CONTROLLER
        else:
            code = EXIT_LAUNCH_FAILED
        emit({
            "ok": False, "code": code, "degraded": True,
            "message": "无法获得 Clash 控制接口，代理未开启；可继续原任务（走直连）或由用户手动处理",
            "attempts": attempts,
            "report": build_report(installs),
            "next_actions": [
                "继续原任务，不要阻塞在代理上",
                "向用户说明：客户端需在设置中开启『外部控制 External Controller』并记录端口/secret",
                "用户手动打开客户端后可再次调用 ensure",
            ],
        }, code, args.pretty)

    cfg = get_mode(ctrl) or {}
    previous_mode = cfg.get("mode")
    already = previous_mode == target_mode

    # 幂等重跑时不要把「上一次 ensure 之后的状态」当成原始状态，
    # 否则 restore 永远回不到用户最初的选择。
    stash = read_state() or {}
    stash_ok = (stash.get("controller") or {}).get("endpoint") == ctrl["endpoint"]
    origin_mode = stash.get("previous_mode") if (stash_ok and stash.get("previous_mode")) else previous_mode

    if not already:
        ok, err = set_mode(ctrl, target_mode)
        if not ok:
            emit({
                "ok": False, "code": EXIT_MODE_FAILED, "degraded": True,
                "message": f"控制器可用但切换模式失败: {err}",
                "attempts": attempts, "controller": ctrl, "previous_mode": previous_mode,
                "next_actions": ["检查 secret 是否与客户端配置一致", "在客户端界面上手动切到全局模式"],
            }, EXIT_MODE_FAILED, args.pretty)

    cfg_after = get_mode(ctrl) or {}
    mode_now = cfg_after.get("mode")

    if mode_now != target_mode:
        emit({
            "ok": False, "code": EXIT_MODE_FAILED, "degraded": True,
            "message": f"模式写入后校验不一致：期望 {target_mode}，实际 {mode_now}",
            "attempts": attempts, "controller": ctrl,
        }, EXIT_MODE_FAILED, args.pretty)

    mixed_port = cfg_after.get("mixed-port") or cfg_after.get("port")
    verify = verify_egress(mixed_port) if args.verify else None

    # 「切到 global」不等于「agent 的请求会自动走代理」：
    # 客户端通常需要额外开启系统代理或 TUN。这里如实汇报，并给出可显式使用的代理地址。
    sysproxy = system_proxy_state()
    config_info = dict(chosen.get("config_info") or {})
    tun_on = str(config_info.get("tun-enable", "")).lower() == "true"
    proxy_url = f"http://127.0.0.1:{mixed_port}" if mixed_port else None
    egress_ready = bool(sysproxy.get("enabled")) or tun_on or bool(verify and verify.get("ok"))
    auto_routed = bool(sysproxy.get("enabled")) or tun_on
    egress = {
        "ready": egress_ready,
        "auto_routed": auto_routed,
        "requires_explicit_proxy": not auto_routed,
        "proxy_url": proxy_url,
        "system_proxy": sysproxy,
        "tun": tun_on,
        "tunnel_verified": None if verify is None else bool(verify.get("ok")),
    }
    if not auto_routed:
        egress["note"] = ("代理内核已就绪，但系统代理与 TUN 均未开启，本机普通请求不会自动走代理；"
                          "请在任务中显式使用 proxy_url 作为 HTTP_PROXY/HTTPS_PROXY")

    state = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "client_key": chosen["key"],
        "client_label": chosen["label"],
        "exe": chosen.get("exe"),
        "controller": ctrl,
        "previous_mode": origin_mode,
        "previous_mode_this_run": previous_mode,
        "applied_mode": mode_now,
        "mixed_port": mixed_port,
        "launched_by_us": bool(launched),
        "launched_pid": (launched or {}).get("pid"),
    }
    write_state(state)

    payload = {
        "ok": True, "code": EXIT_OK,
        "message": f"已切换到 {mode_now} 模式" + ("（原本已是该模式）" if already else ""),
        "client": chosen["label"],
        "controller": {k: ctrl[k] for k in ("endpoint", "version", "meta")},
        "previous_mode": origin_mode,
        "mode_before_this_run": previous_mode,
        "mode": mode_now,
        "mixed_port": mixed_port,
        "egress": egress,
        "launched_now": bool(launched),
        "verify": verify,
        "attempts": attempts,
        "state_file": str(STATE_FILE),
        "next_actions": [
            "重试之前失败的网络请求一次",
            "任务结束后调用 off（或 restore）恢复原代理模式",
        ],
    }
    if verify is not None and not verify["ok"]:
        payload["ok"] = False
        payload["code"] = EXIT_VERIFY_FAILED
        payload["degraded"] = True
        payload["message"] = "模式已是 global，但代理出口验证未通过（可能是节点/订阅不可用，而非模式问题）"
        payload["next_actions"] = [
            "把 verify 结果告知用户：模式已切换，但节点不通",
            "继续原任务（不要反复重试切换模式）",
        ]
        emit(payload, EXIT_VERIFY_FAILED, args.pretty)
    emit(payload, EXIT_OK, args.pretty)


def build_report(installs):
    return [{"client": i["label"], "exe": i.get("exe"), "running": i["running"],
             "declared_controller": i.get("config_info", {}).get("external-controller") or None,
             "hint": next((s.get("note") for s in ACTIVE_SPECS if s["key"] == i["key"] and s.get("note")), None)}
            for i in installs]


def terminate(pid):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, 15)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"terminate {pid} failed: {exc}")
        return False


def cmd_off(args):
    ctrl = discover_controller(collect_installs(), extra_endpoint=args.endpoint,
                               extra_secret=args.secret)
    if not ctrl:
        emit({"ok": False, "code": EXIT_NO_CONTROLLER,
              "message": "找不到控制器，无法切换模式"}, EXIT_NO_CONTROLLER, args.pretty)
    ok, err = set_mode(ctrl, args.mode)
    if not ok:
        emit({"ok": False, "code": EXIT_MODE_FAILED, "message": f"切换失败: {err}"},
             EXIT_MODE_FAILED, args.pretty)
    emit({"ok": True, "code": EXIT_OK, "message": f"已切换到 {args.mode}", "controller": ctrl},
         EXIT_OK, args.pretty)


def cmd_restore(args):
    state = read_state()
    if not state:
        emit({"ok": False, "code": EXIT_NO_STATE,
              "message": f"没有状态文件 {STATE_FILE}，无法回滚"}, EXIT_NO_STATE, args.pretty)
    ctrl = state.get("controller") or {}
    target = state.get("previous_mode") or "rule"
    if ctrl.get("endpoint"):
        ok, err = set_mode(ctrl, target)
    else:
        ok, err = False, "状态文件缺少控制器信息"
    closed = False
    if args.close and state.get("launched_by_us") and state.get("launched_pid"):
        closed = terminate(int(state["launched_pid"]))
    if not ok:
        emit({"ok": False, "code": EXIT_MODE_FAILED,
              "message": f"恢复模式失败: {err}", "target_mode": target},
             EXIT_MODE_FAILED, args.pretty)
    emit({"ok": True, "code": EXIT_OK,
          "message": f"已恢复到 {target} 模式" + ("，并关闭由本工具启动的客户端" if closed else ""),
          "restored_mode": target, "client_closed": closed}, EXIT_OK, args.pretty)


def cmd_doctor(args):
    checks = []
    checks.append({"item": "python", "ok": True, "detail": platform.python_version()})
    checks.append({"item": "platform", "ok": True, "detail": f"{platform.system()} {platform.release()}"})
    installs = collect_installs(deep=True)
    checks.append({"item": "clients_found", "ok": bool(installs),
                   "detail": [i["label"] for i in installs]})
    ctrl = discover_controller(installs, deep=True)
    checks.append({"item": "controller", "ok": bool(ctrl), "detail": ctrl})
    if ctrl:
        cfg = get_mode(ctrl) or {}
        checks.append({"item": "mode", "ok": bool(cfg.get("mode")), "detail": cfg.get("mode")})
    checks.append({"item": "state_file", "ok": bool(read_state()), "detail": str(STATE_FILE)})
    ok = all(c["ok"] for c in checks if c["item"] != "state_file")
    emit({"ok": ok, "code": EXIT_OK if ok else EXIT_NO_CONTROLLER,
          "message": "自检完成", "checks": checks,
          "installs": [public_install(i) for i in installs]}, EXIT_OK, args.pretty)


# ---------------------------------------------------------------- CLI
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="JSON 缩进输出，便于人读")

    p = argparse.ArgumentParser(prog="clash_proxy", description="Clash 全局代理自动切换器",
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="只读探测本机 Clash 类客户端", parents=[common])
    d.add_argument("--deep", action="store_true")
    d.add_argument("--roots", help="额外扫描根目录，逗号分隔")
    d.set_defaults(func=cmd_detect)

    s = sub.add_parser("status", help="显示控制器与当前模式", parents=[common])
    s.add_argument("--deep", action="store_true")
    s.add_argument("--endpoint")
    s.add_argument("--secret")
    s.set_defaults(func=cmd_status)

    e = sub.add_parser("ensure", help="确保已切到全局模式（幂等）", parents=[common])
    e.add_argument("--mode", default="global", choices=["global", "rule", "direct"])
    e.add_argument("--client", help="强制指定客户端 key 或 exe 名")
    e.add_argument("--timeout", type=int, default=CONTROLLER_TIMEOUT_DEFAULT)
    e.add_argument("--no-launch", action="store_true", help="只复用已运行的实例，不启动新进程")
    e.add_argument("--verify", dest="verify", action="store_true", help="切换后验证代理出口（默认开启）")
    e.add_argument("--no-verify", dest="verify", action="store_false", help="跳过出口验证")
    e.add_argument("--deep", action="store_true")
    e.add_argument("--kill-on-failure", action="store_true")
    e.add_argument("--endpoint")
    e.add_argument("--secret")
    e.set_defaults(func=cmd_ensure, verify=True)

    o = sub.add_parser("off", help="切回 rule/direct", parents=[common])
    o.add_argument("--mode", default="rule", choices=["rule", "direct"])
    o.add_argument("--endpoint")
    o.add_argument("--secret")
    o.set_defaults(func=cmd_off)

    r = sub.add_parser("restore", help="按状态文件回滚", parents=[common])
    r.add_argument("--close", action="store_true", help="同时关闭由本工具启动的客户端")
    r.set_defaults(func=cmd_restore)

    doc = sub.add_parser("doctor", help="环境自检", parents=[common])
    doc.set_defaults(func=cmd_doctor)
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        emit({"ok": False, "code": EXIT_ARGS, "message": "被中断"}, EXIT_ARGS, args.pretty)
    except Exception as exc:  # 兜底：任何异常都必须返回结构化结果，避免 agent 卡死
        emit({"ok": False, "code": EXIT_ARGS, "message": f"内部错误: {type(exc).__name__}: {exc}"},
             EXIT_ARGS, getattr(args, "pretty", False))


if __name__ == "__main__":
    main()
