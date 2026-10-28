# 设计说明与实测证据

## 为什么是 Skill 而不是 MCP

需求是「agent 在任务中途需要代理时自己拉起并切换」。这个动作是**一次性的、有明确退出码的本地操作**，
不需要常驻服务、不需要跨会话状态、不需要给模型暴露一堆新工具。

- Skill + 一个 CLI：零注册步骤，立即可用；JSON 输出直接喂给模型；失败有退出码语义。
- MCP：需要单独起进程、写 `mcp.json`、让用户在连接器页面点信任，且工具列表污染上下文。

若将来需要被其他客户端复用，把 `clash_proxy.py` 的各子命令包一层 stdio JSON-RPC 即可，
业务逻辑不需要改。

## 三个核心问题怎么解

### 1. 检测范围（进程 / 注册表 / 安装目录 / 配置）

只做进程扫描不够（没启动就找不到），只做目录扫描也不够（装在非标准路径）。
真实反例：客户端装在 `E:\Apps\` 这类自定义目录，既不在 Program Files 也不在 LOCALAPPDATA，
**只有注册表卸载项能命中**。所以四路证据互补：

| 证据源 | 解决的问题 | 局限 |
|---|---|---|
| 进程 | 是否在运行、PID | 拿不到路径时（服务态）需别的线索补全 |
| 注册表 Uninstall | 非标准安装目录、版本号 | 绿色版不注册 |
| 目录扫描 | 绿色版/便携版 | 需要知道扫描根；深扫慢 |
| 配置目录 | 控制端口、secret、当前模式 | 目录名随版本变化 |

目录扫描的根不是硬编码的 Program Files，而是**注册表 InstallLocation 的父级** +
默认根目录，这样 `E:\Apps` 这类自定义根也能覆盖到子目录里的其它客户端。

### 2. 多实例选择

现实里同一台机器常同时装 2-3 个 Clash 系客户端（实测环境里同时有 Verge、FlClash 和一个非 Clash 客户端）。
选择分两步：

**第一步是聚合**，否则计数就错了。同一安装目录下的 `clash-verge.exe`、`verge-mihomo.exe`、
`verge-mihomo-alpha.exe` 必须合成一个候选；`FlClash.exe` + `FlClashCore.exe` 同理。
主程序判定顺序：GUI > 未知 > 内核 > 服务（`KIND_RANK`），同等时取客户端优先级更高、文件名更短者。

**第二步是评分**：

```
score = (40 - priority*0.3) + 40*(声明了控制器) + 20*(在运行) + 10*(exe存在)
      + 8*(有配置文件) + 2*(有secret) + 4*(来自注册表) - 6*(裸内核) - 40*(服务)
```

排序后 `ensure` 的执行顺序是「先复用、后启动」：

1. 只要有一个实例的控制器可达，直接复用（零副作用，最快）。
2. 否则按分数启动，最多 3 个候选（`MAX_LAUNCH_ATTEMPTS`），每个都等控制器就绪。

这样既满足「多实例时选一个」，又保证不会同时拉起两个客户端（端口/TUN 会打架）。

### 3. 失败处理

失败被拆成 7 类可决策的退出码（10/11/12/13/14/15/2），而不是一个 "error"。
每类都带 `degraded: true`（除参数错误），语义是「代理走不通，任务可以继续」，
明确告诉 agent 不要阻塞、不要循环重试。

工程上的不变量：

- 所有等待都有上限（端口 0.4s / HTTP 2s / 管道 4s / 控制器就绪 60s）。
- 不修改系统代理、TUN、DNS、规则；只 `PATCH /configs {"mode"}`。
- 模式写入后必须 `GET /configs` 回读确认，避免「接口 204 但实际没生效」。
- 状态文件记录**原始模式**；幂等重跑用 stash 保住 origin，`restore` 才能回到真正最初的状态
  （这是实测中发现的 bug：第二次 `ensure` 会把 origin 覆盖成 global，导致 restore 失效）。

## 实测记录（Windows 11，2026-10-25）

| 场景 | 结果 |
|---|---|
| 四路探测 | 2 个客户端：Verge(93) > FlClash(45)；v2rayN 正确排除 |
| 冷启动 Verge（仅 TCP 探测） | 60s 超时 → code 13（此时 9097 未监听，属正确降级） |
| 加入管道探测后 | 2s 内命中 `pipe://verge-mihomo-production-<hash>`，version v1.19.32 |
| `off` global→rule | 通过管道 PATCH 成功，回读 rule |
| `ensure` rule→global | 成功；`verify` 经 `127.0.0.1:7897` 返回 204，隧道确实通 |
| 幂等重跑 | 返回「原本已是该模式」，origin 保持 rule |
| `restore` | 正确回到 rule |
| 出口验证失败路径（mock） | code 15 + `degraded`，提示「节点不通而非模式问题」 |
| 错误 secret | 不卡死，4s 内降级为 code 13 |
| 无控制器 | code 13（`--no-launch` 时不再误报 11） |

## 已知限制

- **切 global ≠ 自动走代理**。客户端通常还需开「系统代理」或 TUN。实测环境两者都是关的，
  所以 `ensure` 会返回 `egress.auto_routed: false` 和可显式使用的 `proxy_url`，
  而不是假装成功。agent 用这个地址设 `HTTP_PROXY` 才能真正让请求走代理。
- 服务态内核（SYSTEM 权限）无法被普通权限终止，`--kill-on-failure` 对它会失败，只记录日志。
- 未实现「修改客户端 GUI 设置」（如替用户打开系统代理）——那会改动用户网络环境，超出本工具边界。
- macOS/Linux 只做了基础分支（`ps` / `ss` / `~/.config/*`），未实测；命名管道是 Windows 独有。
- 首次冷启动若客户端弹窗（订阅更新、版本提示），控制器可能迟迟不出现，60s 后降级。

## 测试方法

```bash
# 离线验证切换逻辑，不碰真实代理
python scripts/mock_controller.py --port 9099 --secret test --mode rule &
python scripts/clash_proxy.py ensure --endpoint 127.0.0.1:9099 --secret test --no-launch --no-verify
python scripts/clash_proxy.py off --mode rule --endpoint 127.0.0.1:9099 --secret test
python scripts/clash_proxy.py restore
```

`mock_controller.py` 只实现 `/version`、`/configs` 的 GET 与 PATCH，行为与 mihomo 一致。
