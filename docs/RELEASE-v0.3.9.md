# WorkBuddy-PythonGO Bridge 0.3.9

发布日期：2026-09-13

0.3.9 是面向开箱使用、队列恢复和自适应风控的正式版本。它保持 31 个 Bridge 方法和现有安装方式兼容，不降低 Worker 与 Adapter 的双端校验边界。

本文记录本版本发布时的变化；当前安装、交易启用和排障步骤请阅读[操作手册](README-RELEASE.zh-CN.md)。

## 主要变化

- Adapter 重启后恢复崩溃遗留的 `.json.processing-*` 命令，并继续使用执行日记区分安全重试与 `SUBMIT_UNKNOWN`；
- `TICK_DISPATCH` 只由命令绑定的精确交易所和目标合约 Tick 触发，其他合约行情不会误触发；
- 熔断和 `PAUSE_NEW_OPEN` 阻止风险增加型开仓，但保留精确撤单，以及由有效 Profile、新鲜行情、新鲜目标持仓和可平量共同证明的严格减仓；
- Preview 遇到陈旧资金、持仓或行情时，先发起一次不包含 K 线的交易热路径同步，再决定是否拒绝；
- 单笔名义金额、单笔保证金、总保证金和日内亏损使用绝对值与账户权益比例双上限；价格偏离同时受百分比和 Tick 数限制；
- 新 runtime 可选择“小账户保守、人工均衡、自动策略”三套风控预设；升级已有 runtime 不会静默覆盖原参数；
- 状态输出区分 `READY`、`PAUSE_NEW_OPEN` 与 `RISK_REDUCING_ONLY`，并单独报告当前是否具备严格减仓条件。

## 发布文件

- `workbuddy_pythongo_bridge-0.3.9-py3-none-any.whl`
- `workbuddy-pythongo-bridge-0.3.9.zip`
- `workbuddy-pythongo-bridge-0.3.9.zip.sha256`
- `SHA256SUMS.txt`

## 升级

1. 保留已有稳定 runtime，不要删除配置、密钥、数据库或 Profile；
2. 完整解压 0.3.9 ZIP，双击 `首次安装与配置.cmd`；
3. 让向导复用已有 runtime 并重新部署 PythonGO Adapter；已有风控参数不会被预设覆盖；
4. 完整退出并重启无限易，使新 Adapter 源码与配置生效；
5. 先在 `OBSERVE_ONLY` 检查 Adapter、目标合约行情、账户和持仓同步；
6. 非观察模式恢复前，核对状态中的保护级别、Profile、保证金策略和待对账委托。

仅升级本版本不会重置或要求重签 Profile。只有无限易、PythonGO、柜台、账号、交易映射或 Profile 绑定证据实际变化时，才需要重新完成相应 P0 与签名。

## 已知边界

- `TICK_DISPATCH` 必须等待目标合约的下一笔 Tick；闭市、停牌或无行情时不会使用其他合约 Tick 代替；
- 恢复的 `PRE_SUBMIT` 执行日记会进入 `SUBMIT_UNKNOWN`，不会自动重复报单，必须人工对账；
- 严格减仓仍要求 Profile 有效、目标行情与持仓新鲜、平今/平昨映射精确且可平量充足；保护状态不是绕过这些门禁的通道；
- 主动同步有有限等待时间，Adapter 离线或同步后数据仍陈旧时继续失败关闭；
- 风控预设是安全起点，不代表适合具体账户或合约，启用交易前仍需人工复核。

## 安全边界

- 交易提交前继续重算完整硬风控并核对 Preview 决策指纹；
- Worker 与 Adapter 继续双端校验账号、模式、签名、Profile、行情、资金、持仓和许可；
- 撤单只针对 Bridge 已记录且仍处于活动状态的精确委托；
- runtime、投资者账号或指纹、密钥、Worker token、数据库、日志、队列、签名 Profile、授权与 P0 证据不进入源码或发布包。
