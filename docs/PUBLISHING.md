# GitHub 发布准备

## 仓库信息

- 建议仓库名：`115Drive`
- 简介：`Windows 115 网盘双向同步工具，原生托盘与设置，支持暂停续传和重启恢复。`
- Topics：`115drive`、`windows`、`file-sync`、`rclone`、`openlist`、`wpf`、`python`
- Release 标题：`115Drive v0.2.0 — Windows 双向同步与断点续传`
- Tag：`v0.2.0`
- 本版建议勾选 **Set as a pre-release**，因为当前为单机验证的公开测试版。

## 应提交的内容

将源码包解压后，提交其根目录下的文件和文件夹，包括 `README.md`、`PRIVACY.md`、
许可证、`tests/`、`tray/`、`packaging/`、`third_party/`、`docs/` 和 `.github/`。
不要在运行过的程序目录执行 `git add .`。

仓库不含预置账号、令牌、Cookie、运行数据库、同步文件或日志。
不要提交安装包、便携包、自己的 `config.json` 或 `runtime/`。

```powershell
# 在全新解压的源码目录执行。先在 GitHub 创建空仓库，再填写真实仓库 URL。
git init -b main
git add .
git status --short
git commit -m "feat: release 115Drive v0.2.0"
git remote add origin https://github.com/OWNER/115Drive.git
git push -u origin main
git tag v0.2.0
git push origin v0.2.0
```

`OWNER` 是需要替换的占位符。提交前检查本机 Git 的作者姓名和邮箱，若不想公开个人邮箱，
使用 GitHub 设置中提供的 noreply 邮箱。

## 创建 Release

在仓库的 **Releases → Draft a new release** 中选择 `v0.2.0`。
将 [v0.2.0 发布说明](releases/v0.2.0.md) 全文粘贴到说明栏，再上传：

1. `115Drive-Setup-0.2.0-x64.exe`
2. `115Drive-0.2.0-windows-x64.zip`
3. `115Drive-0.2.0-source.zip`
4. `SHA256SUMS.txt`

源码应和二进制一同发布，保留 `third_party/` 内对应的修改版 OpenList 和前端源码、
构建入口及许可证。

安装包、便携包超过普通 Git 的单文件限制，应放在 Release 附件中。
GitHub 普通仓库拒绝超过 100 MiB 的文件；Release 单个附件上限为 2 GiB。
参见 [GitHub 大文件说明](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)
和 [Release 说明](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。

发布后重新下载四个附件，检查文件大小和 SHA-256；首次发布保持测试版标识。

## 复核本地文件

在装有构建依赖的 Windows 环境中：

```powershell
python packaging/verify-package.py path\to\clean-source
python packaging/verify-release.py path\to\release --work path\to\new-empty-check-folder
```

`verify-release.py` 会解包安装器，比较便携包逐文件内容，检查 ZIP CRC、源码清单、
第三方源码对应关系，并运行随包后台的诊断命令；不会登录账号或启动同步。

只有发布文件需要上传。交付用的整合 ZIP、工作目录和诊断日志不需要加入仓库或 Release。
