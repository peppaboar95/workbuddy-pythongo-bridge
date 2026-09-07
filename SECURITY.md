# Security Policy

## 支持版本

| 版本 | 安全更新 |
| --- | --- |
| 0.3.x | 支持 |
| 0.2.x 及更早版本 | 不支持 |

## 报告漏洞

请不要通过公开 GitHub Issue 报告可能导致真实资金委托、授权绕过、密钥泄漏、消息伪造、重复报单或账户数据泄漏的问题。

请在仓库的 **Security → Advisories → Report a vulnerability** 页面向维护者提交私密报告：

https://github.com/peppaboar95/workbuddy-pythongo-bridge/security/advisories

维护者应在 3 个工作日内确认收到报告。仓库所有者必须在首次公开发布时启用 GitHub Private vulnerability reporting；如果页面没有私密入口，请不要公开披露细节。

报告请尽量包括版本、运行模式、最小复现步骤、预期与实际结果，以及已经脱敏的日志。不要提供密码、真实账号、密钥、Worker token、完整数据库或可识别个人身份的信息。

## 高优先级问题

- 未授权进入 `MANUAL_LIVE` 或 `LIMITED_AUTO`；
- 绕过逐笔批准、时间授权或结构化策略许可；
- 绕过账户、合约、动作、策略版本、额度、回撤或并发限制；
- `PRE_SUBMIT` 或 `SUBMIT_CALLED` 后重复调用报单接口；
- 篡改签名命令、Profile、保证金参考或 Adapter 配置后仍能报单；
- 熔断、暂停或撤销后仍能创建新委托；
- 通过 MCP 参数、日志、诊断包或仓库文件泄漏密钥与账户数据。

## 运行方责任

本项目不能替代期货公司权限控制、柜台隔离或人工监控。运行方必须默认保持 `OBSERVE_ONLY`，先在目标模拟柜台完成现场验证，保护本机密钥和运行数据，并对死信、未知结果、熔断、回撤和许可预算进行持续监控。

