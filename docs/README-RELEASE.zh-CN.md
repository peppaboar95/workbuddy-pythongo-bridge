# WorkBuddy-PythonGO Bridge 0.3.6 操作手册

这是 WorkBuddy 与无限易 PythonGO v2 之间的本机期货桥接。本文按实际操作顺序说明首次安装、观察模式验收、P0 Profile、模式切换和 `LIMITED_AUTO` 许可。

> 默认模式是 `OBSERVE_ONLY`。模式名称不能证明当前柜台是模拟还是实盘；所有非观察模式都可能向当前登录柜台提交真实报单。

## 1. 先理解四个组件

| 组件 | 在哪里运行 | 作用 |
| --- | --- | --- |
| WorkBuddy MCP | WorkBuddy | 提交结构化查询、同步、Preview、许可和交易意图 |
| Worker | Windows 本机 | 保存状态、执行风控、签名队列消息和维护许可 |
| PythonGO Adapter | 无限易 PythonGO | 最终检查账号、Profile、模式、行情和授权，并调用报撤单接口 |
| Local Console | Windows 本机 | 绑定账号、签名 Profile、切换模式、解除熔断和执行 P0 取证 |

必须同时区分两类状态：

- **运行模式**：`OBSERVE_ONLY`、`SIM_SIGNAL`、`MANUAL_LIVE`、`LIMITED_AUTO`；
- **交易保护状态**：独立于运行模式。`SETUP_LOCK` 表示交易尚未启用，不是事故；`ACCOUNT_CHANGE`、`POLICY_REVIEW` 与 `INCIDENT_HALT` 表示必须复核的强保护。任何保护开启时，普通新单都被拒绝，但健康的查询链路仍可使用。

`LIMITED_AUTO` 还分为两步：

1. 本机操作者把 Worker 和 Adapter 切换到 `LIMITED_AUTO`；
2. WorkBuddy 在 readiness 通过后签发限时、限合约、限动作、限额度的策略许可。

只有模式没有许可，仍然不能自动报单。

## 2. 本文命令使用的路径

发布包默认把运行目录固定到 `%LOCALAPPDATA%\WorkBuddyPythonGO\runtime`，因此换一个目录解压新版 ZIP 也不会生成第二套密钥和配置。下面的命令先读取这个默认位置：

```powershell
$BridgeRoot = Join-Path $env:LOCALAPPDATA "WorkBuddyPythonGO\runtime"
$BridgeConfig = Join-Path $BridgeRoot "config\bridge.json"
$ReadyDir = Join-Path $BridgeRoot "pythongo_ready\pythongo_futures_01"
```

如果直接使用源码仓库，也可以把第一行改为你实际创建的运行目录，例如：

```powershell
$BridgeRoot = "C:\WorkBuddyPythonGO\runtime"
```

本文优先使用 `python -m ...`，避免 Windows 用户级脚本目录没有加入 `PATH`。如果安装时使用的是 Python Launcher，也可以把 `python` 换成 `py -3`。

## 3. 首次安装与配置

### 3.1 发布包安装

1. 完整解压发布 ZIP 到普通用户可写目录；
2. 双击 `首次安装与配置.cmd`；
3. 阅读首页说明，按任意键继续；
4. 安装器检查 Python 3.10+，使用包内 wheel 安装 Bridge；
5. Bridge 使用 Python 标准库直接下载九期网电脑版保证金手续费表，不安装任何额外采集依赖；
6. 中文向导依次处理以下内容：
   - 从上次保存位置、旧安装包 runtime 或稳定默认位置中选择运行目录；
   - 自动发现常见 WorkBuddy MCP 配置，并询问是否合并；
   - 是否绑定 `main_futures` 投资者账号（这是账号而非密码，输入会明文显示，完整账号仅写入本机 Adapter 配置）；
   - 自动发现无限易 `pyStrategy\self_strategy` 目录，由用户选定后安全部署 Adapter；
   - 是否创建桌面启动和状态入口；保证金数据按需更新已合并到启动入口。

方括号中的值是默认值，直接按 Enter 即可采用。向导不会启动 Worker、无限易或 WorkBuddy，不会签名 Profile，也不会开放交易。

首次绑定后，状态显示“交易尚未启用”，查询无需先完成 P0，也无需解除这个保护。已绑定账号的运行目录重复运行向导时，向导会直接保留原账号指纹，不再询问重新绑定，因此不会因重复安装而作废 Profile 或触发事故熔断。只有目标投资者账号确实变化时，才按第 6.1 节使用 `bind-investor`。日常运行应使用“启动PythonGO桥接.cmd”，不要把“首次安装与配置.cmd”当作日常启动器。

安装窗口无论成功或失败都会停留在“按任意键关闭”，不会立即消失。失败时保留窗口中的第一条明确错误。

### 3.2 源码安装

```powershell
python -m pip install --user --upgrade --force-reinstall --no-deps --no-build-isolation .
python -m workbuddy_pythongo.desktop setup --root "$env:LOCALAPPDATA\WorkBuddyPythonGO\runtime"
```

保证金手续费日更只使用 Python 标准库，不需要另装采集包。

初始化可重复运行，默认不覆盖已有配置、密钥、Worker token 或 Profile。不要在已有运行环境上随意使用 `--force`。

### 3.3 初始化结果

```text
C:\WorkBuddyPythonGO\
  config\bridge.json
  data\secrets\message_keys.json
  data\secrets\worker.token
  data\reference\
    保证金手续费.csv
    保证金手续费.csv.meta.json
  data\queue\pythongo_futures_01\...
  data\pythongo_runtime\pythongo_futures_01\...
  pythongo_ready\pythongo_futures_01\
    WorkBuddyPythonGOAdapter.py
    pythongo_adapter.path
    pythongo_adapter.json
    pythongo_profile.json
  workbuddy.mcp.example.json
```

密钥、Worker token、真实投资者账号、运行数据库和签名 Profile 都是本机材料，不得跨电脑复用或加入发布包。

### 3.4 本地保证金与手续费日更

当无限易合约对象没有提供可信的多空保证金率时，Adapter 才查询本地九期网格式文件。数据优先级固定为：

1. 无限易当前合约的有效原生保证金率；
2. 当日签名的本地 `保证金手续费.csv`，开仓资金校验使用其中的“保证金-每手”；
3. 两者都不可用时保持保证金数据为空，开仓 Preview 继续以 `MARGIN_RATIO_MISSING` 失败关闭。

桌面 `启动PythonGO桥接.cmd` 每次启动会先静默执行一次 `--if-due` 检查；当天文件有效且数据未过期时不访问网络，否则使用 Python 标准库下载并解析九期网电脑版页面 `https://www.9qihuo.com/qihuoshouxufei` 的完整保证金手续费表。下载限制为 HTTPS、九期网域名、60 秒和 8 MiB，并要求六家期货交易所及至少 500 条有效合约记录。正常刷新或跳过时不显示 CSV 加载过程和结果 JSON，普通刷新失败只显示一行简短警告。安装或升级不再创建独立的保证金更新入口；发现旧入口时会改名为带时间戳的 `.bak` 备份。需要人工查看完整结果或强制刷新时仍可运行下面的管理命令。

命令行强制更新：

```powershell
python -m workbuddy_pythongo.manager --config $BridgeConfig refresh-margin-reference
```

也可导入 CARRY/RSI 混合增强策略生成的同格式文件，不访问网络：

```powershell
python -m workbuddy_pythongo.manager --config $BridgeConfig refresh-margin-reference `
  --source-csv "C:\Users\你的用户名\Desktop\量化交易\期货\保证金手续费.csv"
```

刷新器会校验必要列、交易所、完整合约代码、买/卖保证金比例、每手保证金和开仓/平昨/平今手续费列，读取源页面时间用于告警，剔除重复合约，然后原子写入 CSV；旁边的 `.meta.json` 绑定 CSV 的 SHA-256、来源、本机刷新时间和 HMAC 签名。日常刷新只更新这两个参考数据文件，**不会修改 Adapter 配置、Profile、运行模式、熔断或授权状态**。新 CSV 的哈希和数值会进入后续 Preview 风险指纹，因此旧 Preview 不能借用新数据提交。

`margin_reference_refresh_max_age_hours` 是本机刷新硬门禁，默认 36 小时；过期、签名失败、哈希不一致或精确合约不匹配时不会启用回退值。九期网页面的“手续费更新时间”只按独立的 `margin_reference_source_warn_age_hours` 生成软告警，默认 168 小时；该时间过旧、缺失或无法解析都不会单独阻止使用已签名且本机刷新有效的保证金数据。

v0.3.4 为保证金策略增加独立代次和哈希。v0.3.5 起，安装/升级向导会自动补齐不改变风险语义的跟踪字段，且不修改 Profile、模式或交易保护。若绕过向导，或检测到旧时效字段、路径、安全系数、验签要求等实质变化，刷新和 Worker 启动仍会以 `MARGIN_POLICY_MIGRATION_REQUIRED` 停止，并显示下面的复核命令：

```powershell
python -m workbuddy_pythongo.manager --config $BridgeConfig migrate-margin-policy `
  --confirm MIGRATE-MARGIN-POLICY
```

仅补齐代次/哈希属于跟踪字段迁移，不触发熔断。旧时效字段、Schema、参考文件路径、安全系数或验签要求发生实质变化时，迁移会触发 Worker 与 Adapter 本地熔断、撤销短时授权与自动许可，并使未消费 Preview 失效。两类迁移都保留原 Profile 及其签名，因为 Profile 证明的是目标客户端、账号、构建和交易映射，并不证明每日参考数据或本地保证金策略。迁移后需完整重启无限易，使 Adapter 加载相同的策略代次和哈希；实质变化还应复核新策略与开仓 Preview，再按正常流程解除熔断。不要仅因保证金策略迁移而重签 Profile。

本地表不是券商结算参数。用于开仓估算时，按参考策略的方式使用“保证金-每手 × 手数”，再乘默认 `1.25` 安全系数；买/卖保证金比例仍保留在风险指纹中供审计。CSV 中的开仓、平昨、平今手续费列会完整保留，实际手续费仍以无限易账户快照的 `commission` 为准，第三方数值不用于放宽任何风控。九期网数据不能替代券商或交易所正式通知。

## 4. 部署 PythonGO Adapter

首次配置向导会搜索常见无限易安装位置。选择正确的 `pyStrategy\self_strategy` 后，向导仅部署两个必要文件；同名旧文件先备份为 `.bak.<时间戳>`，旧的 `pythongo_adapter.json` 和 `pythongo_profile.json` 也只会改名保留，不会直接删除。若自动发现失败或当时跳过，再按下面步骤手工操作。

1. 完整退出无限易；
2. 仅把 `$ReadyDir` 中的两个部署文件复制到目标无限易 PythonGO 的 `pyStrategy\self_strategy`：
   - `WorkBuddyPythonGOAdapter.py`；
   - `pythongo_adapter.path`。
3. 把 `self_strategy` 中旧的 `pythongo_adapter.json` 和 `pythongo_profile.json` 改名为带时间戳的 `.bak` 备份；两个活动 JSON 只保留在 `$ReadyDir`；
4. `pythongo_adapter.path` 保存 ready 目录中 `pythongo_adapter.json` 的绝对路径，Adapter 由此继续定位 Profile、密钥和运行数据；
5. 启动无限易并登录目标账号；
6. 在 PythonGO 中加载并启动独立的 WorkBuddy Adapter 策略实例。

部分无限易构建会把策略源码复制到 `pyStrategy\demo` 后执行。Adapter 会先查执行目录中的定位文件，再回退到同级 `self_strategy\pythongo_adapter.path`。

配置、Profile 和模式变化时无需再复制 JSON，但 Adapter 只在初始化时加载它们，仍应完整退出并重启无限易。覆盖 Python 文件后只停止并重新运行策略可能继续使用旧模块缓存，源码更新同样必须完整重启。

升级到 v0.3.6 时优先重新运行 CMD 安装器；向导会自动处理无风险跟踪字段并部署新 Adapter。若结果显示 `material_change=true`，应保持 `POLICY_REVIEW` 保护，复核新策略和开仓 Preview 后再解除；Profile 保持原样，无需仅因这次迁移重新签名。只有无限易、PythonGO、柜台、账号、交易映射或 Profile 本身的绑定证据变化时，才重新执行相应 P0 并重签。

## 5. 第一次启动：只使用 OBSERVE_ONLY

首次应先启动 Worker，再启动无限易中的 Adapter。

### 5.1 使用桌面入口

双击 `启动PythonGO桥接.cmd`。启动器会先静默尝试更新当日签名保证金表，不显示 CSV 加载过程或成功结果；普通更新失败只显示一行警告，不会绕过风控。安装/升级向导会自动补齐无风险的保证金策略跟踪字段；如果用户跳过升级向导，或检测到实质策略变化，启动器仍会停止并提示复核，不会继续进入模式菜单。

```text
1. OBSERVE_ONLY
2. SIM_SIGNAL
3. MANUAL_LIVE
4. LIMITED_AUTO
```

直接按 Enter 或输入 `1`。保持 Worker 窗口打开。

### 5.2 使用命令行

```powershell
python -m workbuddy_pythongo.manager --config $BridgeConfig start
```

### 5.3 检查状态

启动无限易中的 Adapter 后，双击 `查看PythonGO桥接状态.cmd`，或运行：

```powershell
python -m workbuddy_pythongo.console --config $BridgeConfig status
python -m workbuddy_pythongo.manager --config $BridgeConfig doctor
```

观察模式下，未签名 Profile 可以显示 `UNVERIFIED`；此时查询、同步、Preview 和空跑闭环仍可使用，普通报单不能使用。

状态页分别显示连接、观察和交易就绪度。即使交易保护已经开启，只要 `observation_ready=true`，状态页会显示“查询状态：可用；仅阻止新的交易提交”，不会把整个 Adapter 标为故障。`trade_ready=false` 只表示当前不能推进新的交易，并不表示查询链路损坏。

注意两种 Profile 状态不是一回事：

- `doctor` 的 `profile_signature=通过`：ready 目录中的文件能通过静态签名和绑定校验；
- 心跳中的 `profile_status=VALID`：正在运行的无限易 Adapter 已经加载并接受该 Profile。

如果文件已签名但心跳仍是 `UNVERIFIED`，说明无限易仍在运行旧模块或未重新加载 ready 内容；确认定位文件后完整重启无限易，无需复制 JSON 或 Profile。

## 6. 需要交易时：完成 P0、签名 Profile 和解除交易保护

只使用查询时无需执行本章。非观察模式必须使用目标电脑、目标账号、目标无限易/PythonGO/柜台组合完成 P0。不能只把任意非空构建字符串写入 Profile 后签名。

### 6.1 绑定投资者账号

如果首次向导没有绑定，或目标账号发生变化，执行：

```powershell
python -m workbuddy_pythongo.console --config $BridgeConfig bind-investor main_futures --confirm BIND-ACCOUNT
```

账号会明文显示，方便核对；这是投资者账号，不是登录密码。首次绑定会：

- 更新本机账号指纹；
- 保持 Profile 未验证；
- 写入 `SETUP_LOCK`，明确表示“交易尚未启用”，数据库不记录事故熔断；
- 保持 `OBSERVE_ONLY`。

如果已有账号指纹真正变化，重绑定仍会重置旧 Profile，并开启 `ACCOUNT_CHANGE` 强保护；这属于必须复核的安全事件。

如果输入账号与 Worker 和 Adapter 中已批准的指纹完全一致，`bind-investor` 是幂等无操作：不重置 Profile、不改变模式、不开启本地熔断。只有账号指纹确实发生变化，或 Worker/Adapter 绑定已不一致需要修复时，才执行上述安全重绑定流程。

绑定后无需复制 Adapter 配置或 Profile；确认定位文件指向 ready 目录，并完整重启无限易。

### 6.2 完成 P0 证据

按照 [P0 核对清单](../examples/P0-CHECKLIST.zh-CN.md) 核对并记录：

- 无限易、PythonGO 和柜台的真实构建标识；
- Adapter 调度模式和 memo 容量；
- 订单状态原值到统一状态的映射；
- 方向、开平、投机、GFD 和限价字段的真实值；
- 平今、平昨、重启回放和回调关联能力。

Profile 映射键格式为：

```text
FUTURES:<ACTION>:LIMIT:GFD
```

只有现场实际验证过的动作才能写入 Profile 和后续自动许可。

### 6.3 P0 报撤与逐腿验证

这些命令只用于目标仿真柜台的 P0 取证，要求 `OBSERVE_ONLY + LOCAL_HALT`，不属于普通交易入口。

单次一手报撤探针：

```powershell
python -m workbuddy_pythongo.console --config $BridgeConfig p0-test-order main_futures CFFEX IF2609 --confirm AUTHORIZE-P0-ONE-LOT
```

若目标合约的一手名义金额高于账户常规单笔限额，可为这一次 P0 命令明确给出不超过 100 万元的隔离额度，例如：

```powershell
python -m workbuddy_pythongo.console --config $BridgeConfig p0-test-order main_futures EXCHANGE CONTRACT --isolated-notional-cap 900000 --confirm AUTHORIZE-P0-ONE-LOT
```

目标交易所、合约和额度都会进入最长 30 秒的一次性签名授权与命令哈希；Worker 和 Adapter 分别校验。隔离额度只属于该 P0 命令，不会提高普通模式的 `max_order_notional`。

逐腿成交验证：

```powershell
python -m workbuddy_pythongo.console --config $BridgeConfig p0-validation-leg main_futures EXCHANGE CONTRACT OPEN_LONG --confirm AUTHORIZE-P0-TRADE-LEG
python -m workbuddy_pythongo.console --config $BridgeConfig p0-validation-leg main_futures EXCHANGE CONTRACT CLOSE_TODAY_LONG --confirm AUTHORIZE-P0-TRADE-LEG
python -m workbuddy_pythongo.console --config $BridgeConfig p0-validation-leg main_futures EXCHANGE CONTRACT OPEN_SHORT --confirm AUTHORIZE-P0-TRADE-LEG
python -m workbuddy_pythongo.console --config $BridgeConfig p0-validation-leg main_futures EXCHANGE CONTRACT CLOSE_TODAY_SHORT --confirm AUTHORIZE-P0-TRADE-LEG
```

每腿都必须单独核对 ACK、Order、Trade、唯一 memo 和持仓变化。任何部分成交、未知结果、持仓不符或回报歧义都应停止下一腿并人工对账。

### 6.4 签名和加载 Profile

1. 停止 Worker 和 Adapter；
2. 复核 `pythongo_profile.json` 中所有证据；
3. 本机签名：

```powershell
python -m workbuddy_pythongo.console --config $BridgeConfig sign-profile main_futures --confirm VERIFIED-PROFILE
```

4. 签名后的 Profile 和更新了 `expected_*` 的 Adapter 配置继续保留在 ready 目录，无需复制；
5. 完整重启无限易，Adapter 通过定位文件加载新内容；
6. 使用 `OBSERVE_ONLY` 启动 Worker 和 Adapter；
7. 检查 `doctor` 的 Profile 三项校验通过，且新心跳为 `profile_status=VALID`。

### 6.5 解除熔断

只有 Profile、账号、模式和正在运行的 Adapter 都已复核后，才执行：

```powershell
python -m workbuddy_pythongo.console --config $BridgeConfig clear-halt --confirm CLEAR-HALT
```

解除熔断会强制回到 `OBSERVE_ONLY`，并撤销旧批准、人工会话和自动许可。重新加载 Adapter 后，再完成一次观察模式同步和对账。

## 7. 切换运行模式

### 7.1 桌面菜单的三个输入阶段

启动菜单有三个不同提示：

1. `请输入序号 [1]`：输入 `1`、`2`、`3`、`4`，或输入 `Q` 退出；
2. 受阻说明后的 `按Enter返回模式菜单；输入Q取消启动`：这里只接受 Enter 或 `Q`，在这里输入 `LIMITED_AUTO` 不是确认模式；
3. 门禁通过后的 `完整输入<模式名>`：这里才输入 `SIM_SIGNAL`、`MANUAL_LIVE` 或 `LIMITED_AUTO`。

如果菜单标注 `[暂不可用]`，必须先解决括号中的门禁原因，不能用确认词绕过。

### 7.2 正确切模顺序

1. 停止无限易中的 Adapter；
2. 在 Worker 窗口按 `Ctrl+C` 停止 Worker；
3. 双击启动入口并选择目标模式；
4. 对非观察模式完整输入相同的模式名；
5. 启动器更新数据库和 ready 目录中的 `pythongo_adapter.json`，然后启动 Worker；
6. 无需复制 JSON 或 Profile；完整重启无限易并启动 Adapter；
7. 检查 Worker 与 Adapter 模式相同。

Worker 已经运行时，桌面启动器只显示状态，不会再次切模。要切模必须先停止现有 Worker。

命令行切模示例：

```powershell
python -m workbuddy_pythongo.manager --config $BridgeConfig set-mode SIM_SIGNAL --confirm SIM_SIGNAL
python -m workbuddy_pythongo.manager --config $BridgeConfig set-mode MANUAL_LIVE --confirm MANUAL_LIVE
python -m workbuddy_pythongo.manager --config $BridgeConfig set-mode LIMITED_AUTO --confirm LIMITED_AUTO
```

非观察模式的 Worker 如果直接使用命令启动，还必须重复确认已保存模式。例如：

```powershell
python -m workbuddy_pythongo.worker --config $BridgeConfig --confirm-mode LIMITED_AUTO
```

### 7.3 四种模式的区别

| 模式 | 是否可能调用报单接口 | 额外条件 |
| --- | --- | --- |
| `OBSERVE_ONLY` | 普通流程不会 | 可查询、同步、Preview 和空跑 |
| `SIM_SIGNAL` | 会 | 已验证的目标模拟柜台和签名 Profile |
| `MANUAL_LIVE` | 会 | 每笔批准或 1–60 分钟人工会话 |
| `LIMITED_AUTO` | 会 | readiness 通过且存在有效结构化许可 |

## 8. 进入 LIMITED_AUTO

### 8.1 前置检查

进入前必须满足：

- Profile 文件签名、P0 绑定和构建绑定全部通过；
- 正在运行的 Adapter 心跳为 `profile_status=VALID`；
- Worker 与 Adapter 都是 `LIMITED_AUTO`；
- Worker 熔断和 Adapter 本地熔断均已解除；
- Adapter 心跳新鲜并声明 `limited_auto_protocol=1`；
- 资金和持仓快照新鲜；
- 没有待对账状态、`SUBMIT_UNKNOWN`、命令积压或死信；
- 自动许可中的动作是 Profile 已验证动作的子集；
- 策略提交的 `source.type/rule_set_id/rule_version` 与许可完全一致；
- 单笔、会话、并发、持仓和回撤预算不超过本机硬限制。

`bridge.json` 的 `instrument_allowlist` 建议在自动模式前显式填写精确交易所和合约。当前实现中空数组表示不限制合约；这不适合有限自动部署。

先读取实际硬限制，不要照抄其他电脑的额度：

```text
在 WorkBuddy 中调用：get_risk_limits(account_alias="main_futures")
```

### 8.2 切换模式并同步 Adapter

按第 7.2 节停止旧进程，在桌面菜单输入 `4`，然后在正式确认框中完整输入：

```text
LIMITED_AUTO
```

无需复制 `pythongo_adapter.json` 或 Profile；完整重启无限易并启动 Adapter，它会通过定位文件读取启动器刚更新的 ready 配置。

### 8.3 请求新鲜快照

在 WorkBuddy 中调用 `request_sync`，参数示例：

```json
{
  "account_alias": "main_futures",
  "scopes": ["ACCOUNT", "POSITION", "ORDER", "TRADE", "QUOTE"],
  "instruments": [
    {"exchange": "YOUR_EXCHANGE", "instrument_id": "YOUR_CONTRACT"}
  ]
}
```

把 `YOUR_EXCHANGE` 和 `YOUR_CONTRACT` 替换为本次许可的精确合约。

### 8.4 检查 readiness

调用：

```text
check_limited_auto_readiness(account_alias="main_futures")
```

只有以下结果才能继续：

```json
{
  "ready": true,
  "reasons": []
}
```

readiness 只检查并记录当前状态，不签发许可。

### 8.5 签发结构化许可

调用 `authorize_limited_auto`。下面是参数模板，不可直接照抄合约、策略版本、时间窗或额度：

```json
{
  "account_alias": "main_futures",
  "instruments": [
    {"exchange": "YOUR_EXCHANGE", "instrument_id": "YOUR_CONTRACT"}
  ],
  "actions": ["OPEN_LONG", "CLOSE_TODAY_LONG"],
  "source_type": "RULE_ENGINE",
  "rule_set_id": "YOUR_RULE_SET",
  "rule_version": "YOUR_RULE_VERSION",
  "max_order_notional": 100000,
  "max_order_volume": 1,
  "max_session_notional": 200000,
  "max_orders": 2,
  "max_concurrent_orders": 1,
  "max_instrument_position_notional": 200000,
  "max_account_drawdown": 1000,
  "minutes": 120,
  "min_order_interval_seconds": 60,
  "max_consecutive_failures": 1,
  "trading_windows": [
    {"start": "21:00", "end": "02:30"}
  ],
  "reason": "目标策略的有限自动验证",
  "confirm": "AUTHORIZE-LIMITED-AUTO-P1"
}
```

额度既要覆盖本次实际一手名义金额，又不能超过 `get_risk_limits` 返回的硬限制。如果一手名义金额已经超过 `max_order_notional`，应选择其他已验证合约或重新完成风险评审；P0 隔离额度不能用于普通自动许可。

许可最长 720 分钟，只支持 `FIXED_VOLUME`，不会自动续期。跨午夜夜盘窗口可以写为 `21:00` 到 `02:30`。

### 8.6 确认许可状态

调用：

```text
get_limited_auto_status(account_alias="main_futures")
```

开始提交策略意图前至少确认：

```text
status=ACTIVE
active=true
health_reasons=[]
remaining_orders>0
remaining_notional>0
```

每个自动交易意图仍必须执行 `request_sync → preview_trade → submit_trade_intent`，并使用与许可完全一致的策略来源和 `FIXED_VOLUME`。通过 `get_trade_intent`、`get_orders` 和 `get_trades` 跟踪回调事实。

### 8.7 暂停、恢复和撤销

- `resume_limited_auto` 使用确认词 `RESUME-LIMITED-AUTO-P1`；
- `revoke_limited_auto` 使用确认词 `REVOKE-LIMITED-AUTO-P1`；
- 连续失败达到上限后必须创建新许可，不能直接恢复；
- Worker 启动、正常退出、模式切换、熔断和解除熔断都会撤销旧许可并轮换代次。

因此，每次完整重启后都应重新同步、检查 readiness，再创建新许可。

## 9. 日常启动与停止

### 9.1 日常启动

1. 确认上次没有 `SUBMIT_UNKNOWN` 或未终态委托；
2. 启动 Worker；
3. 启动无限易并登录目标账号；
4. 启动 PythonGO Adapter；
5. 查看桥接状态；
6. 请求账户、持仓、委托和成交同步；
7. 完成必要对账；
8. 非观察模式重新完成相应授权。

### 9.2 正常停止

1. 停止产生新信号；
2. 撤销自动许可或人工会话；
3. 确认没有提交中、未知或活动子委托；
4. 完成最后一次同步和对账；
5. 停止 Adapter；
6. 在 Worker 窗口按 `Ctrl+C`。

### 9.3 紧急熔断

本机立即熔断：

```powershell
python -m workbuddy_pythongo.console --config $BridgeConfig halt --reason "operator emergency stop"
```

WorkBuddy 也可以调用 `halt_trading`。只有本机 Console 能解除熔断，解除时一定回到 `OBSERVE_ONLY`。

## 10. 常见问题与处理

### 每次启动都提示本地熔断

正常启动 Worker 只会撤销短时授权，不会重新开启耐久本地熔断。先查看状态中的熔断原因；如果是 `account binding changed by local console`，说明之前重复绑定了账号。确认账号、Profile 和 Adapter 后只需解除一次熔断，以后使用“启动PythonGO桥接.cmd”日常启动。已绑定的首次向导不再提供重复绑定入口。

### 启动器提示 `MARGIN_POLICY_MIGRATION_REQUIRED`

这是 v0.3.4 对旧保证金策略配置的显式升级门禁，不是 CSV 下载失败。按提示运行 `migrate-margin-policy --confirm MIGRATE-MARGIN-POLICY`，检查输出中的 `material_change`，然后完整重启无限易。迁移不会重置或重签 Profile；如果实质策略发生变化，本地熔断会保持开启，需复核策略和 Preview 后再解除。

### 菜单显示“本地熔断尚未解除”

不要在受阻提示处输入模式名称。按 Enter 返回菜单或输入 `Q` 退出。先完成 Profile、Adapter 和对账检查，再按第 6.5 节解除熔断。

### `doctor` 显示 `worker=OBSERVE_ONLY adapter=LIMITED_AUTO`

数据库模式和 ready 目录配置不一致。先恢复：

```powershell
python -m workbuddy_pythongo.manager --config $BridgeConfig set-mode OBSERVE_ONLY
```

然后完整重启无限易，Adapter 会通过定位文件读取新模式；启动后核对心跳。

### Profile 文件通过，但心跳仍是 `UNVERIFIED`

正在运行的 Adapter 没有重新加载 ready 目录中的新 Profile。确认 `pythongo_adapter.path` 指向当前 ready 配置，完整退出并重启无限易。不要只点击“重新运行策略”。

### Adapter 显示 `STOPPED` 或心跳陈旧

确认无限易已登录、PythonGO x64 正常、策略实例已启动，并确认 `self_strategy\pythongo_adapter.path` 存在且指向正确的 ready 配置。`self_strategy` 不应再保留 `pythongo_adapter.json` 或 `pythongo_profile.json`。

### readiness 返回 `AUTO_DEAD_LETTER_PRESENT`

`dead_letter` 中存在被拒绝的队列消息。停止 Worker 和 Adapter，保留原文件并逐条检查拒绝原因。修复版本、签名、Schema 或模式问题后，把已审阅的旧文件移动到活动队列以外的本机审计目录；不要直接删除证据。确认活动 `dead_letter` 为空后重新启动和同步。

### readiness 返回快照或对账错误

- `AUTO_ACCOUNT_SNAPSHOT_STALE`：重新同步资金；
- `AUTO_POSITION_SNAPSHOT_STALE`：重新同步持仓；
- `AUTO_RECONCILIATION_REQUIRED`：完成委托、成交和持仓人工对账；
- `AUTO_SUBMIT_UNKNOWN_PRESENT`：不能自动重发，必须人工确认柜台事实；
- `AUTO_ADAPTER_MODE_MISMATCH`：确认定位文件指向当前 ready 配置并完整重启无限易；
- `AUTO_PROFILE_INVALID`：核对签名 Profile 和正在运行的 Adapter；
- `AUTO_ADAPTER_PROTOCOL_MISMATCH`：无限易中仍在运行旧 Adapter 源码。
- `AUTO_MARGIN_POLICY_MISMATCH`：Worker 与当前 Adapter 加载的保证金策略代次或哈希不同，完整重启无限易并检查 ready 配置。

### 重启后许可消失

这是正常行为。Worker 重启会撤销旧许可。重新同步、检查 readiness，并在操作者再次确认范围后签发新许可。

## 11. WorkBuddy MCP

首次向导可以自动把 `workbuddy-pythongo` 合并进 WorkBuddy MCP 配置，并保留其他 MCP 服务。也可以参考运行目录中的：

```text
workbuddy.mcp.example.json
```

MCP 只访问本机回环 Worker，不持有 HMAC 密钥。WorkBuddy 可以查询、同步、Preview、签发有界交易许可、提交已通过风控的意图、撤销精确订单和触发熔断，但不能：

- 切换运行模式；
- 签名 Profile；
- 解除本地熔断；
- 绕过 Worker 或 Adapter 的最终检查。

当前提供 31 个 MCP 工具。常规交易顺序是：

```text
pythongo_health
→ request_sync
→ 读取资金/持仓/行情
→ preview_trade
→ 对应模式的人工或自动许可
→ submit_trade_intent
→ get_trade_intent / get_orders / get_trades
```

## 12. 支持范围和安全边界

- 当前只支持期货、投机、固定手数、GFD 固定限价；
- 不支持期权、任意 Python、裸原生接口、全撤、FAK/FOK、市价或无许可自动交易；
- 通用平仓拆出的平今和平昨分别占用一笔自动许可额度；
- PythonGO Tick 中的原生 `datetime` 会在签名和写队列前转换为带时区的 ISO 8601 字符串；
- 合约乘数读取 `InstrumentData.size`；保证金优先使用无限易原生有效比例，原生值缺失或非有限、非正、超过 100% 时才使用严格验签且未过期的本地九期网表；
- 本地回退按“保证金-每手 × 手数 × 默认 1.25 安全系数”估算；本地文件无匹配、过期、重复、损坏或验签失败时，开仓以 `MARGIN_RATIO_MISSING` 失败关闭；
- Profile 是本机、账号和目标构建的验收证据，不是可复制的通用映射模板；
- 无限易、PythonGO、柜台、账号、策略代码或枚举发生变化时，必须重新执行相应 P0 并重签 Profile。

完整架构、威胁模型和 P0/P1 验证范围见 [设计文档](DESIGN.zh-CN.md)。

## 13. 开发验证

```powershell
python -m pytest
python -m build --wheel --no-isolation --outdir dist
```

测试覆盖严格配置、队列签名与过期、今昨仓拆分、信号规则、重启对账、`OBSERVE_ONLY` 空跑闭环，以及 `LIMITED_AUTO` 许可预算、策略版本绑定、代次轮换、双端校验和异常暂停。
