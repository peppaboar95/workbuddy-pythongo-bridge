# WorkBuddy-PythonGO Bridge 0.3.3 发布包说明

本发布包用于在另一台 Windows 电脑上安装 WorkBuddy-PythonGO Bridge。安装过程使用包内 wheel，不需要复制开发源码，也不会携带制作电脑的运行数据。

## 安装前准备

- Windows 10/11；
- Python 3.10 或更高版本；
- Tencent WorkBuddy；
- 目标无限易客户端及 PythonGO v2；
- 目标电脑上的完整投资者账号，以及在目标无限易/PythonGO/柜台组合上完成 P0 的条件。

## 安装

1. 校验 ZIP 同目录的 `.zip.sha256`，并可使用包内 `SHA256SUMS.txt` 校验解压文件；
2. 将 ZIP 完整解压到普通用户可写的独立目录；
3. 双击 `首次安装与配置.cmd`；
4. 在中文向导中选择运行目录、WorkBuddy MCP 合并、投资者账号绑定和桌面入口；
5. 按向导最后一页，仅将 `pythongo_ready` 中的 `WorkBuddyPythonGOAdapter.py` 和 `pythongo_adapter.path` 部署到目标无限易 PythonGO 策略目录；
6. 首次只使用 `OBSERVE_ONLY`，完成目标环境 P0、Profile 复核和本机签名后再考虑其他模式。

安装脚本不会自动启动 Worker、无限易、PythonGO Adapter 或 WorkBuddy，不会签名 Profile，也不会开放模拟、人工或自动交易。

## 包内文件

- `首次安装与配置.cmd`：Windows 安装入口；
- `workbuddy_pythongo_bridge-0.3.3-py3-none-any.whl`：Python 安装包；
- `LICENSE`：专有许可条款；
- `README-RELEASE.zh-CN.md`：完整使用说明；
- `workbuddy-pythongo-bridge-design.md`：设计、安全边界和运行模型；
- `RELEASE-MANIFEST.zh-CN.md`：本说明；
- `RELEASE-v0.3.3.md`：本版本变化、升级说明和已知边界；
- `examples/`：P0 清单和观察模式交易请求示例；
- `workbuddy.mcp.example.json`、`workbuddy.mcp.example.README.md`：手工排障时使用的 MCP 示例；
- `SHA256SUMS.txt`：包内文件的 SHA-256 清单。

## 明确不包含

发布包不包含源码树、`runtime`、投资者账号或指纹、本机密钥、Worker token、数据库、WAL/SHM、日志、消息队列、执行日记、签名 Profile、LIVE/P1 授权或现场 P0 证据。这些内容必须在目标电脑本机生成和验证，不能跨机器复用。
