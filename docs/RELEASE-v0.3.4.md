# WorkBuddy-PythonGO Bridge 0.3.4

发布日期：2026-09-08

0.3.4 修复日常保证金参考刷新会隐式重置签名 Profile 的行为，并把参考数据更新与保证金风险策略迁移拆成两个明确流程。

## 变化

- `refresh-margin-reference` 现在只校验、原子写入并签名 CSV 与元数据；不修改 Adapter 配置、Profile、模式、熔断或授权；
- 新增显式迁移命令 `migrate-margin-policy --confirm MIGRATE-MARGIN-POLICY`；
- Adapter 配置增加保证金策略代次与哈希，心跳回传后由 Worker 双向核对；
- 仅补齐代次/哈希的旧配置迁移不触发熔断；实质策略变化会触发本地熔断、撤销短时和自动授权，并使未消费 Preview 失效；
- 所有保证金策略迁移均保持 Profile 及其签名字节不变；
- 桌面启动器遇到待迁移配置时显示明确指令并停止，不再把迁移混在静默日更中；
- 新增对应回归测试并更新操作手册、设计说明和接口参考。

## 安装资产

- `workbuddy_pythongo_bridge-0.3.4-py3-none-any.whl`
- `workbuddy-pythongo-bridge-0.3.4.zip`
- `workbuddy-pythongo-bridge-0.3.4.zip.sha256`
- `SHA256SUMS.txt`

普通用户下载 ZIP 与哈希文件，校验后完整解压，再双击 `首次安装与配置.cmd`。安装器不会启动无限易、Worker 或 WorkBuddy，也不会签名 Profile 或开放交易。

## 从 0.3.3 升级

1. 停止新的交易请求，确认没有提交中或 `SUBMIT_UNKNOWN` 意图，停止 Worker 与 Adapter，并备份运行目录；
2. 安装 0.3.4 wheel，重新运行首次配置向导以更新 ready 目录中的 Adapter 源码和桌面入口；
3. 运行：

   ```powershell
   python -m workbuddy_pythongo.manager --config $BridgeConfig migrate-margin-policy `
     --confirm MIGRATE-MARGIN-POLICY
   ```

4. 如果结果为 `material_change=false`，说明只补齐策略跟踪字段，不会触发熔断；如果为 `true`，保持本地熔断并复核策略和开仓 Preview；
5. 完整重启无限易，使 Adapter 加载新源码及相同的策略代次/哈希，然后运行 `doctor`；
6. 不要仅因这次迁移重新签名 Profile。只有客户端构建、柜台、账号、交易映射或 Profile 绑定证据变化时，才重新执行相应 P0 并签名。

## 已知边界

- 软件测试不能替代目标无限易、PythonGO、柜台和账户的 P0/P1 现场验收；
- `SIM_SIGNAL` 仍可能调用当前柜台报单接口；
- 本地保证金参考只在无限易缺少可信原生保证金率时使用，不能替代期货公司或交易所正式参数；
- 源码可见不改变项目的专有许可范围。
