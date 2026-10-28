---
name: clash-proxy-switch
description: 检测本机已安装的 Clash 系代理客户端（Clash Verge Rev、FlClash、Clash for Windows、Clash Nyanpasu、Mihomo Party、mihomo 内核等），把其中一个启动并切换到全局（global）代理模式，并把「出网是否真的就绪、该用哪个代理地址」一并返回，让原任务可以继续。当任务出现出网失败（连接被重置 ECONNRESET、连接超时、目标站点不可达、TLS 握手失败），且本机可能装有 Clash 系客户端时使用；也用于查询当前代理模式、任务结束后恢复原模式。触发词：clash、全局代理、系统代理、科学上网、代理没开、切全局、开代理再试、proxy、connection reset、网络不通。
agent_created: true
---

# Clash 全局代理自动切换

## 这个 skill 解决什么

agent 执行任务时常遇到「某个请求出不去」的症状，而本机其实已经装了 Clash 系客户端，
只是没启动、或者停在规则（rule）模式。本 skill 把「探测 → 选一个实例 → 必要时启动 →
切到 global → 校验出网」压缩成一次调用，并把失败原因结构化返回，避免 agent 反复重试或卡死。

脚本：`scripts/clash_proxy.py`（仅标准库，Python 3.8+，无第三方依赖）。
所有子命令都只向 stdout 输出一段 JSON，退出码见下表。

## 运行形态（三者共用同一套逻辑）

| 你的运行时 | 接入方式 |
|---|---|
| 支持 Skill / Agent Skills 规范（Claude Code、WorkBuddy 等） | 直接读本文件，按下面的流程调用 |
| 只支持 MCP（Claude Desktop、Cursor、Cline 等） | 注册 `scripts/mcp_server.py`（stdio），工具名 `clash_proxy_*` |
| 纯命令行 / 任意语言 | 直接执行 `scripts/clash_proxy.py`，解析 stdout JSON |

平台与客户端支持矩阵见 `README.md`；macOS/Linux 代码路径已实现但未在真机验证。

## 何时用 / 何时不用

用：
- HTTP 请求报 `ECONNRESET`、`Connection timed out`、`SSL_ERROR`、`Remote end closed connection`
- 目标站点在其他网络可达、在当前网络不可达，且本机装有 Clash 系客户端
- 用户明确说「开代理」「切全局」「用 clash」

不用：
- DNS 解析就失败（`ENOTFOUND`）——通常是域名错了或本机 DNS 问题，不是代理问题
- 目标站点本身 5xx / 换直连也一样不通
- 用户明确要求「不要用代理 / 走直连」

## 决策流程（按顺序执行）

1. 直接调 `ensure`，**不要**先 `detect` 再 `ensure`，一次调用足够。
2. 读返回 JSON：
   - `ok: true` → 看 `egress`：
     - `auto_routed: true`（系统代理或 TUN 已开）→ 直接重试原请求一次，然后继续任务。
     - `auto_routed: false` → 本机流量**不会**自动走代理，必须把 `egress.proxy_url`
       作为 `HTTP_PROXY` / `HTTPS_PROXY` 用于后续请求，再重试原请求一次。
   - `ok: false` 且 `degraded: true` → **不要重试本工具**，按 `code` 处理（见下表），
     然后继续原任务（走直连）或把结论告知用户。
3. 任务结束（或不再需要代理）后调用 `off`（切回 rule）或 `restore`（回到 ensure 之前的状态）。
4. 同一任务内 `ensure` 最多调用 2 次；第二次仍失败就放弃代理路径。

## 子命令

| 命令 | 作用 | 何时用 |
|---|---|---|
| `ensure` | 探测 → 选实例 → 必要时启动 → 切 global → 校验；幂等 | 主入口，几乎只用这个 |
| `status` | 显示控制器、当前模式、混合端口 | 想知道「现在什么状态」 |
| `off --mode rule\|direct` | 切回规则/直连 | 任务结束收尾 |
| `restore` | 按状态文件回滚到 ensure 之前 | 需要精确还原 |
| `detect` | 只读扫描本机 Clash 类客户端，不启动、不改配置 | 排查「为什么没找到」 |
| `doctor` | 环境自检（客户端/控制器/模式/状态文件） | 交付前或异常排查 |

常用参数：
- `--client <key>`：强制指定客户端（`detect` 输出里的 `key` 或 exe 名）
- `--no-launch`：只复用已在运行的实例，不启动新进程（最安全）
- `--timeout N`：冷启动后等待控制器就绪的秒数，默认 60
- `--no-verify`：跳过出网校验，只做模式切换
- `--endpoint host:port` / `--secret`：手工指定控制器（其它方式的兜底）
- `--deep`：深扫安装目录、深扫 9000-9100 端口

调用示例：

```bash
python "<skill>/scripts/clash_proxy.py" ensure --verify
```

## 退出码 → agent 该做什么

| code | 含义 | agent 动作 |
|---|---|---|
| 0 | 成功（模式已是目标值也算成功） | 按 `egress` 决定重试方式，然后继续任务 |
| 10 | 本机没有 Clash 类客户端 | 不要重试；告知用户，继续原任务 |
| 11 | 全部候选启动失败 | 不要重试；告知用户，继续原任务 |
| 12 | 需要管理员权限 | 提示用户以管理员运行或手动启动，继续原任务 |
| 13 | 拿不到控制接口（未开外部控制/未运行） | 不要重试；告知用户具体缺失项，继续原任务 |
| 14 | 接口可用但设置 mode 失败 | 检查 secret；提示用户手动切全局 |
| 15 | 模式已 global 但隧道不通 | 与模式无关，是节点/订阅问题；告知用户，继续原任务 |
| 2 | 参数或内部错误 | 读 `message`，修正参数或放弃 |

任何非 0 且 `degraded: true` 都表示「代理这条路走不通，但任务本身可以继续」——
不要阻塞、不要循环重试、不要把退出当成任务失败。

## 检测范围（三层，逐层收窄）

| 层 | 覆盖内容 | 为什么要它 |
|---|---|---|
| 进程 | `Get-CimInstance Win32_Process` 取 PID/名称/可执行路径；`tasklist` 兜底 | 能识别「正在运行」的实例，并可复用 |
| 注册表 | HKCU/HKLM 的 Uninstall 项：DisplayName / InstallLocation / DisplayIcon / DisplayVersion | **唯一能覆盖非标准安装目录的途径**（只扫 Program Files 会漏掉装在自定义目录的客户端） |
| 目录 | 注册表安装目录的父级 + `%LOCALAPPDATA%`/`%APPDATA%`/Program Files 等默认根，按「名称含 clash / mihomo / nyanpasu」筛目录再找 exe | 覆盖绿色版、便携版 |

补充证据：各客户端的配置目录（读 `external-controller` / `secret` / `mixed-port` / `mode` / `tun.enable`）。
名称筛选规则：主判据是名称含 `clash`；同时认 `mihomo` / `verge` / `nyanpasu` 等已知别名。
`v2rayN` 这类不含 clash 的客户端不会被误选。

## 多实例选择策略

先聚合、再评分、再按分数降序：

1. **聚合**：按「安装目录」合并同一客户端的 GUI / 内核 / 卸载器 / 服务，
   否则 `verge-mihomo.exe`、`FlClashCore.exe` 会被当成三个不同客户端。
2. **评分**（越高越优先）：
   - 客户端优先级（Clash Verge Rev 10 > Nyanpasu 20 > Mihomo Party 25 > FlClash 30 > CFW 40 > 裸内核 60）
   - 已在运行 +20；可执行文件存在 +10；有配置文件 +8；**配置里声明了 external-controller +12**
   - 服务/helper 扣分，裸内核扣分
3. **运行期能力优先**：`ensure` 先尝试「复用已在运行且控制器可用的实例」（零副作用），
   失败才按分数顺序启动，最多启动 3 个候选（`MAX_LAUNCH_ATTEMPTS`）。
4. 平手时按 exe 路径字典序，保证结果可复现。
5. 可用 `--client` 强制指定，或 `--no-launch` 禁止启动。

## 控制器探测顺序（决定「能不能切」）

1. `--endpoint` 显式指定
2. 配置里声明的 TCP 控制器（如 `127.0.0.1:9097`）
3. **Windows 命名管道** `\\.\pipe\*mihomo*|*clash*|*verge*`（排除 `*service*`）
4. 客户端进程实际监听的端口（可命中随机端口）
5. 常见默认端口 `9090/9097/9091/...`
6. `--deep` 时扫 9000-9100

第 3 步不可省：实测 Clash Verge Rev 2.5.7 在服务模式下**只开命名管道、不绑 9097**，
配置里写的管道名还会过期（`sidecar-release` → 实际 `production`），所以必须枚举管道。
切换方式是向控制器 `PATCH /configs {"mode":"global"}`，再 `GET /configs` 回读校验。

## 失败处理

- **分层降级**：拿不到控制器 → 逐个换候选客户端（≤3 次）→ 全部失败则返回 `degraded`。
- **必须有超时**：控制器等待默认 60s，每个端口探测 0.4-2s，管道读写 4s；没有无限循环。
- **失败不污染系统**：`ensure` 不会修改系统代理、不会开启 TUN；
  `--kill-on-failure` 才关闭自己启动的进程；模式切换失败时客户端保持运行，不会留下半死状态。
- **状态可回滚**：成功后写 `~/.workbuddy/state/clash-proxy-state.json`（含原模式、控制器、混合端口）。
  幂等重跑不会把「上次 ensure 后的状态」当成原始状态，保证 `restore` 永远回到最初模式。
- **结构化失败**：返回 `code` / `message` / `attempts`（每步结果）/ `next_actions`，便于上层决策。

## 硬性约束

- 只操作本机回环控制器（`127.0.0.1` 或命名管道），不碰远端配置、不改订阅。
- 只改 `mode`，不改系统代理开关、不改 TUN、不改 DNS、不改规则集。
- 单实例原则：已有可用实例就复用，不重复启动第二个客户端（避免端口/TUN 冲突）。
- 不删除、不重装、不修改用户配置文件。
- 退出前若曾切换模式，提示用户可用 `off` / `restore` 还原。

## 参考

- `README.md` — 面向使用者的说明、安装方式、兼容性矩阵
- `references/clients.md` — 各客户端可执行文件、配置路径、控制器默认值与坑位（Win/mac/Linux）
- `references/design.md` — 设计取舍、失败分类法、实测证据与已知限制
- `scripts/mcp_server.py` — MCP stdio 适配层，供非 skill 运行时使用
- `scripts/mock_controller.py` — 伪造控制器，用于离线验证切换逻辑
