# Changelog

本项目的重要变化记录在此。版本号遵循 Semantic Versioning。

## 0.3.4 - 2026-09-08

### Fixed

- 日常 `refresh-margin-reference` 只校验、写入和签名参考数据，不再修改 Adapter 配置、重置签名 Profile 或触发熔断；
- 新增显式 `migrate-margin-policy --confirm MIGRATE-MARGIN-POLICY`，把旧配置迁移从日常启动路径中分离；
- 保证金策略实质变化时保留 Profile，但触发 Worker/Adapter 本地熔断、撤销授权并使未消费 Preview 失效；
- Worker 与 Adapter 通过保证金策略代次和哈希握手，配置或运行实例不同步时失败关闭；
- 桌面启动器遇到待迁移配置时停止启动并显示处理命令，不再把安全迁移伪装成普通刷新失败。

### Packaging

- 新增保证金刷新、显式迁移、Profile 保留和专用退出码的回归测试；
- 更新操作手册、设计文档、接口参考和 v0.3.4 发布资产。

## 0.3.3 - 2026-09-07

### Changed

- 从已验证可运行的 0.3.2 wheel 恢复标准 `src/` 源码结构；
- 移除 4 个未使用的导入，不改变交易、风控、队列或授权行为；
- Worker 的 HTTP 版本标识改为复用包版本，避免发布时重复维护；
- 安装器在同目录存在多个 wheel 时明确失败，避免误装旧版本；
- 发布脚本改为从 `pyproject.toml` 读取版本并在构建前清理同项目旧产物；
- 清理公开文档中的制作电脑绝对路径，补齐 GitHub 协作、安全和 CI 文件。

### Packaging

- 新增 wheel、安装 ZIP、独立 ZIP 哈希与总哈希的一键构建和内容校验；
- Release ZIP 明确排除源码、运行数据、密钥、账号、日志和签名 Profile；
- wheel 继续声明 `LicenseRef-Proprietary`，未改变许可范围。

## 0.3.2 - 2026-09-07

### Added

- 九期网保证金/手续费参考表按需更新与签名校验；
- 本机刷新时效硬门禁和源页面时间软告警；
- Worker 与 Adapter 基于每手保证金的双重开仓资金校验；
- 本地参考数据格式迁移时的失败关闭、Profile 重置和 P0 复验流程。

### Safety

- 默认模式保持 `OBSERVE_ONLY`；
- 本地参考缺失、过期、哈希不一致或验签失败时拒绝使用回退保证金；
- 第三方手续费数据不用于放宽风控。
