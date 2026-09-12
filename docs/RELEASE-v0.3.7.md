# WorkBuddy-PythonGO Bridge 0.3.7

发布日期：2026-09-12

0.3.7 是交易热路径的低延迟与异步状态版本。它不削弱双端风控或失败关闭边界：Bridge 在命令可靠写入本地队列后立即返回，调用方通过 `get_trade_intent` 继续追踪原生报单、柜台回报和最终状态。

## 主要变化

- `submit_trade_intent` 在交易命令可靠入队后返回 `async_status`，不再同步等待无限易下一次扫描或柜台回报；
- 异步状态明确区分 Bridge 已接收、命令已入队、原生报单函数已返回、已看到柜台订单证据和最终状态；建议以 150ms 为起点轮询 `get_trade_intent`；
- Worker 与 Adapter 的命令/回报扫描改为 100ms 活跃、200ms 空闲的自适应周期；对账、租约和维护任务仍按低频运行；
- Adapter 启动时预订阅 `bridge.json` 中账户 `instrument_allowlist` 的精确合约，并持续维护本地 Tick 快照；
- `request_sync` 新增 `purpose="TRADE"`；交易同步只允许账户、持仓和行情，明确拒绝把 K 线放进报单热路径；普通同步继续使用 `purpose="GENERAL"`；
- 单子单撤单、部分成交后撤单等情况会聚合为明确终态，异步轮询能够正常结束；
- 为命令确认、回报和订单的意图查询补充数据库索引。

## 发布文件

- `workbuddy_pythongo_bridge-0.3.7-py3-none-any.whl`
- `workbuddy-pythongo-bridge-0.3.7.zip`
- `workbuddy-pythongo-bridge-0.3.7.zip.sha256`
- `SHA256SUMS.txt`

## 升级

1. 保留旧安装目录和稳定 runtime，不要删除本机配置、密钥或数据库；
2. 完整解压 0.3.7 ZIP，双击 `首次安装与配置.cmd`；
3. 让向导复用已有 runtime，并重新部署 PythonGO Adapter；
4. 核对每个账户的 `instrument_allowlist` 只包含实际使用的精确合约；
5. 完整退出并重启 WorkBuddy 与无限易，在 PythonGO 中启动 Adapter；只重启策略可能继续使用旧模块缓存；
6. 先在 `OBSERVE_ONLY` 检查 Adapter、账户、持仓和行情，再恢复原有交易模式。

升级不会仅因性能配置或 Adapter 源码变化而重置 Profile。若无限易、PythonGO、柜台、账号、交易映射或签名绑定证据发生变化，仍需按安全规则重新完成相应 P0 与签名。

## 已知边界

- `TICK_DISPATCH` 仍依赖目标合约的下一笔 Tick；预订阅可降低首次请求等待，但停牌、闭市或无行情时不会伪造 Tick；
- Bridge 的快速返回表示本地可靠入队，不表示交易所或柜台已经接受委托；必须继续读取 `get_trade_intent`；
- 100–200ms 是本地扫描目标，不是端到端成交时限；实际延迟仍受无限易事件循环、柜台、网络与市场状态影响；
- 行情预订阅在 Adapter 初始化时执行，修改白名单后必须完整重启无限易才能生效。

## 安全边界

- 交易提交前仍重新执行完整硬风控并核对 Preview 决策指纹；
- Worker 与 Adapter 仍双端校验账号、Profile、模式、许可、行情、资金、持仓和本地交易锁；
- `PRE_SUBMIT`、`SUBMIT_CALLED` 或未知结果仍禁止自动重发；
- runtime、投资者账号或指纹、密钥、Worker token、数据库、日志、队列、签名 Profile、授权与 P0 证据不进入源码或发布包。
