# GitHub 发布检查清单

## 仓库设置

- [ ] 确认仓库所有者与 URL 为 `peppaboar95/workbuddy-pythongo-bridge`；
- [ ] 确认专有许可符合预期；如果要改为开源许可，先由权利人明确决定并单独修改；
- [ ] 启用 GitHub Private vulnerability reporting；
- [ ] 将默认分支设为 `main`，启用分支保护和必需 CI；
- [ ] 检查 CODEOWNERS 中的账号。

## 内容检查

- [ ] `git status --short --ignored` 中没有运行数据、密钥、token、真实账号或签名 Profile；
- [ ] 搜索本机绝对路径、邮箱、账号、IP、密钥片段和未脱敏日志；
- [ ] `README.md`、`CHANGELOG.md`、`pyproject.toml`、`src/.../__init__.py` 和发布说明版本一致；
- [ ] 操作手册、P0 清单和示例不包含制作电脑专有数据。

## 验证

```powershell
python -m unittest discover -s tests -v
python -m ruff check src tests
cmd /c "首次安装与配置.cmd --syntax-check"
.\tools\build_release.ps1
```

- [ ] 在干净临时目录安装构建出的 wheel 并核对包元数据与 `__version__`；
- [ ] 解压 Release ZIP，确认安装器、wheel、文档、示例、LICENSE 和包内哈希齐全；
- [ ] 校验 ZIP 独立哈希和 `SHA256SUMS.txt`；
- [ ] 在 Windows 10/11 的 Python 3.10+ 环境完成一次首次安装向导冒烟测试。

## 发布

- [ ] 提交源码，不提交 `dist/`；
- [ ] 创建带签名或受保护的 `v0.3.3` 标签；
- [ ] GitHub Release 使用 `docs/RELEASE-v0.3.3.md`；
- [ ] 上传 wheel、ZIP、`.zip.sha256` 和 `SHA256SUMS.txt`；
- [ ] 发布后从 GitHub 重新下载资产并复核哈希。

