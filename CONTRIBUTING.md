# Contributing

感谢你改进 WorkBuddy-PythonGO Bridge。因为本项目能够接入真实资金账户，变更必须保持失败关闭，并提供可复核的安全边界。

## 开发环境

需要 Windows 和 Python 3.10 或更高版本：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m unittest discover -s tests -v
python -m ruff check src tests
```

## 提交要求

- 为行为变化添加或更新测试；
- 默认模式必须保持 `OBSERVE_ONLY`；
- 不得从自然语言、未知枚举或缺失字段猜测交易语义；
- 不得让重试路径绕过 `PRE_SUBMIT`、`SUBMIT_CALLED` 或 `SUBMIT_UNKNOWN`；
- 任何授权变化都要说明账户、时间、合约、动作、策略版本、额度、撤销和熔断边界；
- PythonGO Adapter 变更必须在目标无限易/PythonGO 构建上重新完成现场验证；
- 更新面向用户的安装、升级和安全说明。

## 禁止提交

不要提交真实投资者账号、密码、密钥、Worker token、`bridge.json`、签名 Profile、数据库、WAL/SHM、日志、队列、执行日记、保证金本机签名副本或未脱敏的现场验收材料。

公开 Issue 和 Pull Request 也不能包含上述内容。安全问题请按照 [SECURITY.md](SECURITY.md) 使用 GitHub 私密漏洞报告。

## Pull Request

Pull Request 应说明：

1. 解决的问题和非目标；
2. 风险边界是否变化；
3. 测试命令及结果；
4. 对安装、数据库、Adapter、Profile 或配置的迁移影响；
5. 是否需要目标无限易/PythonGO/柜台现场复验。

