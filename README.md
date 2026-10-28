# clash-proxy-switch

[![CI](https://github.com/jeeleonard9-art/clash-proxy-switch/actions/workflows/ci.yml/badge.svg)](https://github.com/jeeleonard9-art/clash-proxy-switch/actions/workflows/ci.yml)

让 agent 在需要时把本机 Clash 类客户端切到**全局代理**模式，用完再还原。

一个零依赖的 Python 脚本 + 一份 Skill 描述 + 一个 MCP 适配层，三种形态共用同一套逻辑。
适用于 AI agent 执行任务中途遇到出网失败、需要临时走代理的场景。

```
探测客户端  →  选一个实例  →  必要时启动  →  PATCH mode=global  →  回读校验  →  验证出口
```

## 它解决什么问题

agent 跑任务时经常卡在「某个请求出不去」，而本机其实装着 Clash，只是没启动或停在「规则」模式。
手工去点开客户端、切模式、再回来继续，对人类来说很烦，对 agent 来说根本做不到。

这个工具把这件事变成一次函数调用，并把结果结构化返回：成功、失败原因、以及**这次代理到底有没有真的生效**。

## ⚠️ 先读这一条：切到 global ≠ 请求会自动走代理

这是最容易踩的坑。Clash 的 `mode=global` 只决定**内核如何路由**，系统流量能否进入内核取决于另一组开关：

| 接管方式 | 谁能用上代理 | 本工具是否修改 |
|---|---|---|
| 系统代理（System Proxy） | 遵守系统代理设置的程序（浏览器、多数 HTTP 客户端） | ❌ 不改 |
| TUN 模式 | 全部流量，含不认代理设置的程序 | ❌ 不改 |
| 显式代理 `http://127.0.0.1:<mixed-port>` | 只有主动使用它的进程 | ❌ 不改，但会把地址告诉你 |

所以 `ensure` 成功后会返回：

```json
"egress": {
  "ready": true,
  "auto_routed": false,              // ← 系统代理和 TUN 都没开，普通请求不会自动走代理
  "requires_explicit_proxy": true,
  "proxy_url": "http://127.0.0.1:7897",
  "system_proxy": { "enabled": false, "server": "" },
  "tun": false,
  "tunnel_verified": true            // 走 proxy_url 实测能出去（HTTP 204）
}
```

`auto_routed: false` 时，调用方应该把 `egress.proxy_url` 设成 `HTTP_PROXY` / `HTTPS_PROXY` 再重试请求。
工具不会擅自改你的系统代理或 TUN —— 那属于改动用户网络环境，超出了它的职责边界。

## 兼容性

### 按运行方式

| 你的 agent 支持什么 | 能不能用 | 怎么接 |
|---|---|---|
| Skill 机制（Claude Code、WorkBuddy、兼容 Agent Skills 规范的运行时） | ✅ | 直接用 `SKILL.md`，模型自行决定何时调用 |
| MCP（Claude Desktop、Cursor、Cline、Continue、Windsurf 等） | ✅ | 注册 `scripts/mcp_server.py`（stdio） |
| 命令行 / 任何语言 | ✅ | 直接执行 `scripts/clash_proxy.py`，读 stdout 的 JSON |
| 只提供 HTTP 工具调用、且不能起本地进程 | ❌ | 需要自己包一层 HTTP 服务 |

`SKILL.md` 的 frontmatter 里有 `agent_created: true`，这是 WorkBuddy 的私有字段，
其他运行时通常会忽略未知字段；若你的运行时校验严格，删掉这一行即可。

### 按客户端 × 平台

| 客户端 | Windows | macOS | Linux | 控制器传输 |
|---|---|---|---|---|
| Clash Verge Rev | ✅ 实测 | ⚠️ 未实测 | ⚠️ 未实测 | TCP 9097 / Windows 命名管道 |
| FlClash | ✅ 实测（探测） | ⚠️ 未实测 | ⚠️ 未实测 | 通常无控制器，靠进程端口探测 |
| ClashX Meta / ClashX Pro | — | ⚠️ 未实测 | — | TCP 9090 |
| Mihomo Party | ⚠️ 未实测 | ⚠️ 未实测 | ⚠️ 未实测 | TCP 9090 |
| Clash Nyanpasu | ⚠️ 未实测 | ⚠️ 未实测 | ⚠️ 未实测 | TCP 9090 |
| Clash for Windows | ⚠️ 未实测 | — | — | TCP 9090 |
| mihomo / Clash.Meta 内核 | ⚠️ 未实测 | ⚠️ 未实测 | ⚠️ 未实测 | TCP 9090 |

**诚实说明**：只有 Windows 上「Clash Verge Rev + FlClash」是实机跑通的（测试记录见 `references/design.md`）。
macOS / Linux 的代码路径（`.app` bundle 启动、`ps`/`lsof` 解析、XDG 配置目录）都已实现，但**没有在真机验证过**。
如果你在 mac/Linux 上跑通了或跑挂了，欢迎提 issue 带上 `python scripts/clash_proxy.py doctor --pretty` 的输出。

### 平台要求

- Python 3.9+，**只用标准库**，无需 `pip install`（CI 覆盖 3.9 / 3.12）
- Windows 10/11、macOS 12+、主流 Linux 发行版
- 无需管理员权限运行本工具本身（但**启动**需要提权的客户端时会被拒绝，见退出码 12）

## CI 到底验证了什么

`CI` 工作流在 **ubuntu / macos / windows × Python 3.9 / 3.12** 六种组合上跑 22 个测试：

| 验证项 | 说明 |
|---|---|
| 路径解析 | `%APPDATA%` 在 mac 落到 `~/Library/Application Support`、在 Linux 落到 `~/.config` |
| 平台过滤 | macOS 才启用 ClashX、Windows 才启用 CFW，不会互相串台 |
| 实例聚合 | 同一目录下的 GUI/内核/服务算作一个客户端，服务态进程不计入「运行中」 |
| 噪声排除 | 安装包、更新器、`%TEMP%` 里的可执行文件不会被当成已安装客户端 |
| 状态机 | mock 控制器上跑 `ensure → off → restore`，含幂等性与回读校验 |
| 退出码 | 无客户端返回 10、显式端点失效返回 13、无状态文件返回 17 |
| MCP 协议 | 握手、`tools/list`、`tools/call`、未知工具报错 |

**CI 不能证明的**：它跑在没装任何 Clash 的干净 runner 上，用 mock 控制器代替真实客户端。
「macOS 上能不能真的发现并启动 ClashX」只有真机能回答——这就是矩阵里那些 ⚠️ 的含义，
CI 通过 ≠ 该平台上的真实客户端可用。

## 安装

### 方式一：作为 Skill

```bash
# Claude Code
git clone https://github.com/<you>/clash-proxy-switch.git ~/.claude/skills/clash-proxy-switch

# WorkBuddy
git clone https://github.com/<you>/clash-proxy-switch.git "%USERPROFILE%\.workbuddy\skills\clash-proxy-switch"
```

重启会话后生效。模型会根据 `description` 里的触发条件自行调用。

### 方式二：作为 MCP server

在客户端的 MCP 配置里加：

```json
{
  "mcpServers": {
    "clash-proxy-switch": {
      "command": "python3",
      "args": ["/绝对路径/clash-proxy-switch/scripts/mcp_server.py"]
    }
  }
}
```

Windows 把 `python3` 换成 `python`，路径用正斜杠或双反斜杠。
暴露 4 个工具：`clash_proxy_ensure` / `clash_proxy_status` / `clash_proxy_off` / `clash_proxy_restore`。

### 方式三：纯命令行

```bash
python3 scripts/clash_proxy.py ensure --pretty
```

## 用法

```bash
python3 scripts/clash_proxy.py ensure            # 主入口：确保处于 global 模式（幂等）
python3 scripts/clash_proxy.py status            # 当前模式 / 传输方式 / 混合端口
python3 scripts/clash_proxy.py off               # 切回 rule
python3 scripts/clash_proxy.py restore           # 按状态文件回到 ensure 之前
python3 scripts/clash_proxy.py detect            # 只读探测，不启动不改配置
python3 scripts/clash_proxy.py doctor            # 环境自检
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--client <key\|exe>` | 多实例时强制指定（`detect` 输出里的 `key`） |
| `--no-launch` | 只复用已在运行的实例，绝不启动新进程（最安全） |
| `--timeout N` | 冷启动后等待控制器就绪的秒数，默认 60 |
| `--no-verify` | 跳过出口验证 |
| `--endpoint host:port` / `--secret` | 手工指定控制器，绕过自动探测 |
| `--deep` | 深扫目录 + 扫 9000-9100 端口 |
| `--pretty` | JSON 缩进，方便人读 |

### 输出与退出码

成功和失败都只输出一段 JSON，字段固定包含 `ok` / `code` / `message`，
失败时带 `degraded`（表示「代理这条路不通，但原任务可以继续」）和 `next_actions`。

| code | 含义 | 建议动作 |
|---|---|---|
| 0 | 成功（已是目标模式也算） | 按 `egress` 决定是否需要显式代理 |
| 10 | 本机没有 Clash 类客户端 | 不要重试 |
| 11 | 候选全部启动失败 | 不要重试 |
| 12 | 需要管理员权限 | 提示用户手动启动 |
| 13 | 拿不到控制接口 | 提示开启「外部控制 / External Controller」 |
| 14 | 接口可用但设置 mode 失败 | 检查 secret |
| 15 | 模式已 global 但隧道不通 | 节点/订阅问题，与模式无关 |
| 2 | 参数或内部错误 | 修正参数 |

## 工作原理

三层探测互补，缺一层就会漏：

1. **进程**（`Win32_Process` / `ps`）→ 判断是否在运行
2. **注册表卸载表**（Windows）→ 覆盖**非标准安装目录**（只扫 Program Files 会漏掉装在 `D:\Apps\` 的情况）
3. **目录扫描**（`*.exe` / `*.app/Contents/MacOS/*` / PATH 下的可执行文件）→ 覆盖绿色版
4. 再叠加**配置目录**里读到的 `external-controller` / `secret` / `mixed-port` / `mode` / `tun.enable`

多实例选择：按安装目录把同一客户端的 GUI/内核/服务聚成一个候选，再按
「客户端优先级 + 是否在运行 + 是否声明了控制器」评分排序；执行时**先复用**、后启动，最多启 3 个。

控制器传输按 `TCP → Windows 命名管道 → 进程实占端口 → 默认端口` 依次尝试。
命名管道这步不可省：Clash Verge Rev 服务模式只开管道、不监听配置里写的 9097，
而且管道名每次安装都会变（`production` / `sidecar-release` ...），必须枚举 `\\.\pipe\`。

细节见 `references/design.md` 与 `references/clients.md`。

## 安全边界

- 只操作**本机回环**接口（`127.0.0.1`、命名管道），不连任何远程服务
- 只改 `mode`，**不改**系统代理、TUN、DNS、规则集、订阅
- 不下载任何东西，不写入客户端配置文件
- 幂等：重复调用无副作用；`restore` 可精确回到最初模式
- 默认不杀进程；只有显式 `--kill-on-failure` / `restore --close` 才动自己启动的进程

## 已知限制

- **切 global ≠ 自动走代理**，见上文。这是设计选择，不是 bug。
- 以 SYSTEM/root 服务方式运行的内核，普通权限无法终止（`--kill-on-failure` 对其无效）。
- macOS/Linux 未实测；`external-controller-unix`（Unix socket 控制器）尚未实现，目前只支持 TCP 与 Windows 命名管道。
- 首次冷启动若客户端弹订阅更新/版本提示，控制器可能迟迟不就绪，60s 后按 `code 13` 降级。
- 只认「名称含 clash / mihomo / verge / nyanpasu」的客户端；v2rayN、Shadowsocks 等不在范围内。

## 开发与测试

跑完整测试套件（22 个用例，不需要真实代理，也不需要装 Clash）：

```bash
python -m unittest discover -s tests -v
```

测试会自己拉起 `mock_controller.py`，覆盖路径解析、平台过滤、实例聚合、状态机与 MCP 协议。
换系统或改客户端矩阵后先跑它，比手动点客户端靠谱。

手工调试切换逻辑：

```bash
python3 scripts/mock_controller.py --port 9099 --secret test --mode rule &
python3 scripts/clash_proxy.py status  --endpoint 127.0.0.1:9099 --secret test
python3 scripts/clash_proxy.py ensure  --endpoint 127.0.0.1:9099 --secret test --no-launch --no-verify
python3 scripts/clash_proxy.py off --mode rule --endpoint 127.0.0.1:9099 --secret test
python3 scripts/clash_proxy.py restore
```

`mock_controller.py` 只实现 `/version` 与 `/configs` 的 GET/PATCH，行为与 mihomo 一致，用于跑通
「探测→切换→回读→回滚」全链路。

调试日志：设环境变量 `CLASH_PROXY_DEBUG=1`，会往 stderr 打印探测细节。

## License

MIT
