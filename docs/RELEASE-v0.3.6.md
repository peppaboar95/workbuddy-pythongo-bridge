# WorkBuddy-PythonGO Bridge 0.3.6

发布日期：2026-09-11

0.3.6 是面向开箱使用的 P0/P1 修复版本。它不降低交易安全门槛，而是把“查询可用”和“交易已启用”明确分开：普通用户完成安装、账号绑定和 Adapter 启动后即可使用查询；只有决定启用交易时，才需要执行 P0、签名 Profile 和解除交易保护。

## 主要变化

- `首次安装与配置.cmd` 继续使用普通 wheel 安装到当前用户的默认 Python 环境，所有安装与启动提示改为中文；
- runtime 默认固定到 `%LOCALAPPDATA%\WorkBuddyPythonGO\runtime`，升级时优先读取本机保存位置，也能发现旧 ZIP 旁的 runtime；
- 向导自动发现常见 WorkBuddy MCP 配置和无限易 `pyStrategy\self_strategy` 目录；部署前必须由用户选择目标；
- 自动部署只写入 `WorkBuddyPythonGOAdapter.py` 与 `pythongo_adapter.path`，覆盖旧文件前生成时间戳备份，并把旧 JSON 改名保留；
- 首次绑定使用 `SETUP_LOCK`，显示为“交易尚未启用”，不会再被描述为故障或事故熔断；已有账号真正发生变化时仍启用强制 `ACCOUNT_CHANGE` 保护；
- P0 的目标合约与单次隔离额度改为动态签名绑定，不再依赖源码内的某个固定合约；上限仍为 100 万元，Adapter 与 Worker 保留双重风控；
- `status` 与 `doctor` 增加中文原因、建议动作和 Worker 启动提示；
- Windows CI 覆盖 Python 3.10、3.11、3.12、3.13、3.14，以及含空格和中文字符的安装路径。

## 发布文件

- `workbuddy_pythongo_bridge-0.3.6-py3-none-any.whl`
- `workbuddy-pythongo-bridge-0.3.6.zip`
- `workbuddy-pythongo-bridge-0.3.6.zip.sha256`
- `SHA256SUMS.txt`

## 升级

1. 保留旧安装目录，不要删除其中可能存在的 runtime；
2. 完整解压 0.3.6 ZIP，双击 `首次安装与配置.cmd`；
3. 向导会优先复用本机保存的 runtime；若只在旧 ZIP 中发现 runtime，也会默认选择旧目录；
4. 核对向导发现的 WorkBuddy MCP 文件与无限易策略目录后再确认；
5. 完整重启 WorkBuddy 和无限易，在 PythonGO 中启动 Adapter，再使用桌面状态入口检查查询链路。

仅补齐保证金策略跟踪字段会自动完成，并保留 Profile 与交易状态。若检测到保证金策略实质变化，查询仍可继续，交易保持 `POLICY_REVIEW` 保护，按屏幕给出的 `migrate-margin-policy` 命令复核。

## 安全边界

- 首次配置不启动 Worker、无限易或 WorkBuddy，不签名 Profile，不开放交易；
- `SETUP_LOCK` 仍通过 Adapter 本地签名交易锁阻止普通报单，但不会把健康的查询链路标成事故；
- P0 命令仍只能从本机 Console 触发，必须处于 `OBSERVE_ONLY`，Adapter 必须报告本地交易锁；每个命令都有短 TTL、单次授权、精确合约、精确额度和命令哈希；
- 已有账号指纹变化、保证金策略实质变化、未知提交结果等真实风险事件仍失败关闭。
