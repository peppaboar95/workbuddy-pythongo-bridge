# PythonGO P0 现场核对清单

完成者必须保留无限易/PythonGO/柜台版本、测试时间、模拟账号和原始回调证据。不要把完整账号、密码、token 或 HMAC 密钥写入清单。

- [ ] 固定无限易客户端构建标识。
- [ ] 固定 PythonGO v2 构建与 Python ABI。
- [ ] 固定券商/柜台构建和模拟/实盘属性。
- [ ] 验证 Adapter 实例、账户类型与投资者指纹绑定。
- [ ] 验证 `get_account_fund_data(investor)` 的资金、保证金、风险度字段和量纲。
- [ ] 验证 `get_all_position(simple=True/False)` 的层级、投机标志、今昨仓和冻结量。
- [ ] 验证合约 `price_tick`、`volume_multiple`、涨跌停和多空保证金率字段。
- [ ] 对每个拟开放动作验证 `order_direction`、`offset`、`order_type`、`hedgeflag`、`market=false` 的真实入参。
- [ ] 单独验证上期所/能源中心显式平昨；不能验证时保持能力关闭。
- [ ] 验证 `make_order_req()`：`-1` 为即时失败，正数仅为本地报单号。
- [ ] 验证 `cancel_order()`：`0` 仅代表撤单请求已发送，终态来自回调。
- [ ] 收集全部订单原始状态并填写 `order_status_map`；未知状态必须归一为 `UNKNOWN_BROKER_STATUS`。
- [ ] 验证 `on_order`、`on_trade`、`on_cancel`、`on_error` 字段和重复回调行为。
- [ ] 验证 memo 最大安全字节数、截断和 Order/Trade 回传稳定性。
- [ ] 验证 Adapter 后台线程是否允许报撤单；否则使用 `TICK_DISPATCH`。
- [ ] 验证日盘、夜盘、断线、重连、暂停和客户端退出行为。
- [ ] 验证重启后是否可靠回放当日委托/成交；未验证时填 `false`。
- [ ] 在 `OBSERVE_ONLY` 完成同步、Preview、Submit、空跑 ACK 和对账。
- [ ] 操作者复核 Profile 无占位符、绑定一致、未知能力关闭，再执行本机签名。
