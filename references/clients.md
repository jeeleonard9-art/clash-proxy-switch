# 客户端矩阵

`scripts/clash_proxy.py` 里的 `CLIENT_SPECS` 就是这张表。新增客户端只改这一处，
`platforms` 字段决定该条在哪个系统上参与匹配。

## Windows（实测）

| 客户端 | 主程序 | 配置目录 | 控制器 | 实测发现 |
|---|---|---|---|---|
| Clash Verge Rev | `clash-verge.exe` | `%APPDATA%\io.github.clash-verge-rev.clash-verge-rev` | 声明 `127.0.0.1:9097`，实际走命名管道 | 服务模式下 **9097 不监听**；只能走 `\\.\pipe\verge-mihomo-production-<hash>`；配置里写的管道名（`sidecar-release-<hash>`）可能已过期 |
| FlClash | `FlClash.exe` | `%APPDATA%\com.follow.clash` | `external-controller: ""` | 默认不开 TCP 控制器，只能靠进程端口探测；探测不到则降级 |

关键细节：

- **Clash Verge Rev 服务模式**：内核 `verge-mihomo.exe` 由 `clash-verge-service.exe`（SYSTEM 权限）托管，
  以 Services 会话运行，`Win32_Process.ExecutablePath` 读不到，`taskkill` 也杀不掉（需管理员）。
  GUI 退出后内核可能继续存活 → `ensure` 会自然走「复用」路径，这是正确行为。
- **secret 可能是占位值**（如 `set-your-secret`），照抄即可，否则管道/TCP 返回 401。
- **`*HelperService.exe` / `*Core.exe`** 是特权 helper 与内核，不代表 GUI 在运行，不能计入 `running`。
- **v2rayN** 等不含 clash 关键字的客户端会被正确排除。

## macOS（代码已支持，未实测）

| 客户端 | 形态 | 配置目录 | 控制器 |
|---|---|---|---|
| ClashX Meta / ClashX Pro / ClashX | `.app` bundle | `~/.config/clash` | 9090 |
| Clash Verge Rev | `.app` bundle | `~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev` | 9097 |
| Mihomo Party | `.app` bundle | `~/Library/Application Support/mihomo-party` | 9090 |
| mihomo（brew 安装） | 命令行二进制 | `~/.config/mihomo`、`/etc/mihomo` | 9090 |

实现要点：`.app` 是目录，扫描必须进入 `Contents/MacOS/`；启动用 `open -a <bundle>`，
直接执行内部二进制会丢 bundle 环境（图标、权限、`LSUIElement`）。

## Linux（代码已支持，未实测）

| 客户端 | 形态 | 配置目录 | 控制器 |
|---|---|---|---|
| Clash Verge Rev | AppImage / deb | `~/.config/io.github.clash-verge-rev.clash-verge-rev` | 9097 |
| FlClash | AppImage | `~/.config/com.follow.clash` | 通常无 |
| mihomo / clash-meta | 系统二进制或 systemd 服务 | `~/.config/mihomo`、`/etc/mihomo` | 9090 |

注意：`ss -p` 非 root 看不到他人进程，端口探测可能失效，此时退化为默认端口列表；
若内核以 systemd 服务运行，`systemctl --user start mihomo` 比直接启动二进制更合适。

## 命名管道协议（Windows）

mihomo 的 `external-controller-pipe` 在同一管道上跑 HTTP/1.1，标准库即可收发：

```python
h = open(r"\\.\pipe\verge-mihomo-production-<hash>", "r+b", buffering=0)
h.write(b"GET /version HTTP/1.1\r\nHost: localhost\r\n"
        b"Authorization: Bearer <secret>\r\nConnection: close\r\n\r\n")
```

返回 `HTTP/1.1 200 OK` + `{"meta":true,"version":"..."}`。

- 枚举：`os.listdir(r"\\.\pipe")`，按 `mihomo|clash|verge` 过滤，**排除 `*service*`**
  （`clash-verge-service` 是 Verge 自己的 IPC，会返回 500）。
- 管道名不可从配置推断，必须枚举。
- Unix socket 控制器（`external-controller-unix`）尚未实现。

## 配置里值得读的键（顶层标量）

| 键 | 用途 |
|---|---|
| `external-controller` | 控制器地址，可能声明了但没绑定 |
| `secret` | 控制器鉴权 |
| `mixed-port` | 出口验证用的 HTTP 代理端口 |
| `mode` | 当前模式：`rule` / `global` / `direct` |
| `tun.enable` | 是否 TUN 接管，影响「流量是否自动走代理」 |

这些是**运行时配置**，GUI 退出或切换模式时会重写，不要当成唯一真相，以控制器回读为准。
