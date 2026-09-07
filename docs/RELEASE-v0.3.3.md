# WorkBuddy-PythonGO Bridge 0.3.3

发布日期：2026-09-07

0.3.3 是面向首次 GitHub 发布的整理版本，不改变 0.3.2 的交易、风控、签名队列、保证金参考或授权协议。

## 变化

- 恢复标准 `src/` 项目结构，源码、文档、示例、测试和构建工具分目录管理；
- 移除 `core.py`、`event_ingest.py` 和 `reconciliation.py` 中 4 个未使用导入；
- Worker HTTP 版本标识统一读取包版本；
- 安装器检测多个 wheel 并明确失败，避免同目录旧包导致误装；
- 新增 GitHub Actions、Issue/PR 模板、贡献说明、安全策略和发布检查清单；
- 新增可重复的 Release 构建脚本与 ZIP 内容校验；
- 删除公开操作手册中的制作电脑绝对路径。

## 安装资产

- `workbuddy_pythongo_bridge-0.3.3-py3-none-any.whl`
- `workbuddy-pythongo-bridge-0.3.3.zip`
- `workbuddy-pythongo-bridge-0.3.3.zip.sha256`
- `SHA256SUMS.txt`

普通用户下载 ZIP 与哈希文件，校验后完整解压，再双击 `首次安装与配置.cmd`。安装器不会启动无限易、Worker 或 WorkBuddy，也不会签名 Profile 或开放交易。

## 升级

从 0.3.2 升级不包含数据库或配置迁移。安装新 wheel 后仍应保持当前安全状态，并运行 `doctor` 检查版本、配置、Profile、账号绑定和 Adapter 心跳。不要因为本次仅为整理版本而跳过目标环境复核。

## 已知边界

- 软件测试不能替代目标无限易、PythonGO、柜台和账户的 P0/P1 现场验收；
- `SIM_SIGNAL` 仍可能调用当前柜台报单接口；
- 本地保证金参考只在无限易缺少可信原生保证金率时使用，不能替代期货公司或交易所正式参数；
- 源码可见不改变项目的专有许可范围。

