# WorkBuddy-PythonGO Bridge

WorkBuddy/MCP 与无限易 PythonGO v2 之间的本机、失败关闭型期货交易桥接器。

> 📖 **在线阅读接口文档**：<https://peppaboar95.github.io/workbuddy-pythongo-bridge/python-api-reference.html>
> （31 个 Bridge 方法的参数、响应、错误码、配额与模块 API，侧栏导航 + 深色代码卡）

> **重要风险提示**
>
> 本项目能够触发模拟或真实资金账户的委托，不构成投资建议，也不保证盈利。默认模式为
> `OBSERVE_ONLY`。任何非观察模式都必须先在目标无限易、PythonGO、柜台和账户组合上完成
> P0 现场验收；模式名称本身不能证明当前连接的是模拟柜台。

## 当前版本

- 版本：0.3.6
- Python：Worker 需要 3.10 或更高版本
- 客户端：无限易 PythonGO v2
- MCP 工具：31 个
- 网络边界：Worker 仅绑定 `127.0.0.1`
- 默认状态：`OBSERVE_ONLY`，交易关闭

0.3.6 完成开箱体验的 P0/P1 修复：CMD 安装器使用稳定的用户运行目录并自动复用旧环境；向导可发现并部署无限易 Adapter、发现常见 MCP 配置；首次绑定显示为“交易尚未启用”而不是事故熔断；查询不再依赖 P0；P0 合约和单次额度由签名命令动态绑定。

## 安全模型

- `preview_trade` 与 `submit_trade_intent` 两阶段提交；
- 提交时重新执行完整硬风控并比较决策指纹；
- Worker 与 PythonGO Adapter 双端检查账号、模式、签名、时效和授权；
- `PRE_SUBMIT`、`SUBMIT_CALLED` 或未知结果不会自动重发；
- 熔断只能由本机 Console 解除，解除后强制回到 `OBSERVE_ONLY`；
- `LIMITED_AUTO` 需要限时、限合约、限动作、限策略版本和限额度的结构化许可；
- 密钥、真实投资者账号、数据库、队列、日志和签名 Profile 不进入源码或 Release。

四种运行模式均与独立熔断状态并存：

| 模式 | 是否可能调用报单接口 | 额外条件 |
| --- | --- | --- |
| `OBSERVE_ONLY` | 普通流程不会 | 默认模式，可查询、同步、Preview 和空跑 |
| `SIM_SIGNAL` | 会 | 仅用于已经现场确认的模拟柜台 |
| `MANUAL_LIVE` | 会 | 逐笔批准或 1–60 分钟本机会话 |
| `LIMITED_AUTO` | 会 | readiness 通过并存在有效结构化许可 |

## 架构

```text
WorkBuddy MCP
    │ 本机 HTTP + Bearer token
    ▼
Worker ── SQLite / 签名文件队列 ── PythonGO Adapter
                                      │
                                      ▼
                                无限易交易接口
```

Worker 负责状态、预览、风控、许可、审计和队列；内嵌 Adapter 负责账户与 Profile 复核、行情和账户快照、最终风控以及报撤单调用。两侧任一状态不可信时都应拒绝新开仓。

## 安装

普通用户应从 GitHub Releases 下载同一版本的 ZIP 与哈希文件：

- `workbuddy-pythongo-bridge-0.3.6.zip`
- `workbuddy-pythongo-bridge-0.3.6.zip.sha256`
- `SHA256SUMS.txt`

校验后完整解压 ZIP，双击 `首次安装与配置.cmd`。安装器会检查 Python 3.10+、把包内 wheel 安装到当前用户的默认 Python 环境，并启动全中文向导。运行数据默认保存在 `%LOCALAPPDATA%\WorkBuddyPythonGO\runtime`，换一个解压目录升级也会复用原环境；向导会尝试发现 WorkBuddy MCP 配置和无限易策略目录，并在用户选定后安全部署。它不会启动无限易、Worker 或 WorkBuddy，也不会签名 Profile 或开放交易。投资者账号会明文显示，便于输入时核对；它不是登录密码。

完整安装、P0、模式切换、日常运行和故障处理请阅读 [操作手册](docs/README-RELEASE.zh-CN.md)。

## 从源码开发

```powershell
git clone https://github.com/peppaboar95/workbuddy-pythongo-bridge.git
cd workbuddy-pythongo-bridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m unittest discover -s tests -v
python -m ruff check src tests
```

本地初始化默认写入单独的 `runtime` 目录：

```powershell
python -m workbuddy_pythongo.desktop setup --root .\runtime
```

不要提交 `runtime`、密钥、Worker token、真实账号、签名 Profile、数据库、日志、队列或现场证据；`.gitignore` 已覆盖常见路径，但提交前仍须人工检查。

## 构建 Release

在 Windows PowerShell 7 中运行：

```powershell
.\tools\build_release.ps1
```

脚本从 `pyproject.toml` 读取版本，构建 wheel、安装 ZIP、ZIP 独立哈希和总哈希清单，并校验发布包必需文件。结果位于 `dist\`。

## 文档

- [Python 接口参考（单文件 HTML）](https://peppaboar95.github.io/workbuddy-pythongo-bridge/python-api-reference.html) — 31 个 Bridge 方法的参数、响应、错误码、配额与模块 API
- [中文操作手册](docs/README-RELEASE.zh-CN.md)
- [设计与安全边界](docs/DESIGN.zh-CN.md)
- [0.3.6 发布说明](docs/RELEASE-v0.3.6.md)
- [P0 核对清单](examples/P0-CHECKLIST.zh-CN.md)

## 许可

本项目使用专有许可。公开可见不等于授予复制、修改或再发布权；具体条款见 [LICENSE](LICENSE)。
