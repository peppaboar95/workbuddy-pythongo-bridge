# WorkBuddy-PythonGO Bridge 0.3.8

发布日期：2026-09-12

0.3.8 是低延迟异步交易链路的正式修正版，包含 0.3.7 的全部功能，并修复英文区域设置 Windows 无法生成中文桌面快捷方式的问题。新版本不削弱双端风控或失败关闭边界。

## 主要变化

- `submit_trade_intent` 在交易命令可靠入队后返回 `async_status`；调用方通过 `get_trade_intent` 追踪原生报单、柜台回报和最终状态；
- Worker 与 Adapter 的命令/回报扫描改为 100ms 活跃、200ms 空闲的自适应周期；
- Adapter 启动时预订阅账户 `instrument_allowlist` 中的精确合约，并持续维护本地 Tick 快照；
- `request_sync(..., purpose="TRADE")` 只允许账户、持仓和行情同步，拒绝把 K 线放进报单热路径；
- 撤单与部分成交后撤单会聚合为明确终态，并为意图状态查询补充数据库索引；
- 桌面启动与状态 `.cmd` 改用 UTF-8，并在开头切换到代码页 65001；英文或中文区域设置的 Windows 都能保存中文提示与中文路径。

## 发布文件

- `workbuddy_pythongo_bridge-0.3.8-py3-none-any.whl`
- `workbuddy-pythongo-bridge-0.3.8.zip`
- `workbuddy-pythongo-bridge-0.3.8.zip.sha256`
- `SHA256SUMS.txt`

## 升级

1. 保留旧安装目录和稳定 runtime，不要删除本机配置、密钥或数据库；
2. 完整解压 0.3.8 ZIP，双击 `首次安装与配置.cmd`；
3. 让向导复用已有 runtime、重新生成桌面快捷方式并重新部署 PythonGO Adapter；
4. 核对每个账户的 `instrument_allowlist` 只包含实际使用的精确合约；
5. 完整退出并重启 WorkBuddy 与无限易，在 PythonGO 中启动 Adapter；只重启策略可能继续使用旧模块缓存；
6. 先在 `OBSERVE_ONLY` 检查 Adapter、账户、持仓和行情，再恢复原有交易模式。

升级不会仅因性能配置、快捷方式编码或 Adapter 源码变化而重置 Profile。若无限易、PythonGO、柜台、账号、交易映射或签名绑定证据发生变化，仍需按安全规则重新完成相应 P0 与签名。

## 已知边界

- `TICK_DISPATCH` 仍依赖目标合约的下一笔 Tick；停牌、闭市或无行情时不会伪造 Tick；
- Bridge 快速返回表示本地可靠入队，不表示交易所或柜台已经接受委托；必须继续读取 `get_trade_intent`；
- 100–200ms 是本地扫描目标，不是端到端成交时限；实际延迟仍受无限易事件循环、柜台、网络与市场状态影响；
- 行情预订阅在 Adapter 初始化时执行，修改白名单后必须完整重启无限易才能生效。

## 安全边界

- 交易提交前仍重新执行完整硬风控并核对 Preview 决策指纹；
- Worker 与 Adapter 仍双端校验账号、Profile、模式、许可、行情、资金、持仓和本地交易锁；
- `PRE_SUBMIT`、`SUBMIT_CALLED` 或未知结果仍禁止自动重发；
- runtime、投资者账号或指纹、密钥、Worker token、数据库、日志、队列、签名 Profile、授权与 P0 证据不进入源码或发布包。
