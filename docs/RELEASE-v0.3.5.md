# WorkBuddy-PythonGO Bridge 0.3.5

发布日期：2026-09-11

0.3.5 是面向普通 Windows 用户的配置与状态体验改进版本。发布包继续使用原有的 `首次安装与配置.cmd` 一键安装流程，并从 ZIP 内的 wheel 安装 Bridge。

## 变化

- Windows 发布包保留原有 CMD 安装入口和 Python 检测顺序，用户完整解压后双击即可；
- 投资者账号使用普通文本输入，向导明确提示它不是密码，并允许用户直接核对输入内容；
- 安装/升级向导自动补齐无风险的保证金策略代次和哈希，不修改 Profile、不触发交易保护；
- 实质保证金策略变化仍不会被自动应用，必须进入显式复核流程；
- 健康接口新增 `connected`、`observation_ready`、`trade_ready` 和 `trade_protection`，状态页不再把“查询正常但交易受保护”显示成整个 Adapter 故障。

## 安装资产

- `workbuddy_pythongo_bridge-0.3.5-py3-none-any.whl`
- `workbuddy-pythongo-bridge-0.3.5.zip`
- `workbuddy-pythongo-bridge-0.3.5.zip.sha256`
- `SHA256SUMS.txt`

普通用户校验 ZIP 后完整解压，双击 `首次安装与配置.cmd`。安装器检查 Python 3.10+、安装或更新包内 wheel，并启动中文配置向导；它不会自动启动无限易、Worker 或 WorkBuddy，也不会签名 Profile 或开放交易。

## 从 0.3.4 升级

1. 停止 Worker 和 Adapter，并备份运行目录；
2. 完整解压 0.3.5 ZIP，双击 `首次安装与配置.cmd`；
3. 安装器更新包内 wheel，并复用已有 runtime、密钥、token、账号绑定和 Profile；
4. 如果旧配置只缺少保证金策略跟踪字段，向导会安全补齐；如果检测到实质策略变化，只显示待复核，不自动修改；
5. 使用新生成的桌面入口启动 Worker，完整重启无限易以加载新的 Adapter 源码；
6. 查看状态，确认 `observation_ready=true`。交易保护开启时仍可进行查询，不要因此重新签名 Profile。

## 已知边界

- 电脑必须提供 Python 3.10 或更高版本及可用的 pip；
- 无限易 PythonGO Adapter 仍需按操作手册部署到策略目录；
- 查询观察路径可以开箱使用，任何可能触发真实报单的模式仍需目标环境验证和明确授权；
- 软件测试不能替代目标无限易、PythonGO、柜台和账户的现场验收。
