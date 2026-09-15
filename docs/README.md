# 文档导航

按使用目标选择文档。安装、启动、交易启用和排障以操作手册为入口；设计文档用于理解实现，版本说明用于查看发布时的变化。

## 按目标阅读

| 需要做什么 | 阅读入口 |
| --- | --- |
| 第一次安装并使用查询 | [安装](README-RELEASE.zh-CN.md#install) → [部署 Adapter](README-RELEASE.zh-CN.md#adapter) → [启动观察模式](README-RELEASE.zh-CN.md#observe) |
| 准备启用交易 | [现场验证、签名与保护解除](README-RELEASE.zh-CN.md#p0) → [选择运行模式](README-RELEASE.zh-CN.md#modes)；现场记录使用 [P0 核对清单](../examples/P0-CHECKLIST.zh-CN.md) |
| 使用策略自动交易 | [LIMITED_AUTO 前置检查、同步和许可](README-RELEASE.zh-CN.md#limited-auto) |
| 日常启动、停止或排障 | [日常操作](README-RELEASE.zh-CN.md#daily) · [常见问题](README-RELEASE.zh-CN.md#faq) |
| 开发调用或理解实现 | [Python 接口参考（离线 HTML）](python-api-reference.html) · [设计与安全边界](DESIGN.zh-CN.md) |
| 核对发布包 | [安装包说明与文件清单](RELEASE-MANIFEST.zh-CN.md) |

只查询时，无需先完成 P0、签名 Profile 或解除首次安装保护。准备交易时，先运行观察模式并检查连接，再按手册完成验证和启用。

## 文档职责

- [操作手册](README-RELEASE.zh-CN.md)：按实际操作顺序说明安装、配置、模式切换、授权和故障处理。
- [设计文档](DESIGN.zh-CN.md)：解释组件职责、信任边界、队列、状态机与风险模型。
- [接口参考](python-api-reference.html)：查询 Bridge 方法的参数、响应、错误码与模块 API。
- [发布包说明](RELEASE-MANIFEST.zh-CN.md)：核对安装前准备、包内文件和不包含的运行材料。
- 版本说明：记录对应版本发布时的变化、升级注意事项与已知边界，不替代当前操作手册。

## 版本记录

| 版本 | 发布日期 | 主要主题 |
| --- | --- | --- |
| [0.3.9](RELEASE-v0.3.9.md) | 2026-09-13 | 队列恢复、自适应风控和保护状态 |
| [0.3.8](RELEASE-v0.3.8.md) | 2026-09-12 | 低延迟链路修正、中文桌面入口 |
| [0.3.7](RELEASE-v0.3.7.md) | 2026-09-12 | 交易热路径与异步状态 |
| [0.3.6](RELEASE-v0.3.6.md) | 2026-09-11 | 查询与交易启用分离、P0/P1 修复 |
| [0.3.5](RELEASE-v0.3.5.md) | 2026-09-11 | Windows 配置与状态体验 |
| [0.3.4](RELEASE-v0.3.4.md) | 2026-09-08 | 保证金参考刷新与策略迁移分离 |
| [0.3.3](RELEASE-v0.3.3.md) | 2026-09-07 | 首次 GitHub 发布整理 |

完整变更条目见 [CHANGELOG](../CHANGELOG.md)。文档沿用现有文件名，便于从源码仓库和安装包定位同一份说明。
