# 参与开发

请先阅读 README 中的同步语义和限制，尤其是双向删除、首次合并和冲突处理。

## 复现与验证

使用独立测试文件夹和单独的测试网盘子目录。不要用唯一副本的数据调试同步。
修改同步行为时验证新增、修改、删除、暂停、中断恢复及文件内容校验，
不要仅根据退出码或界面状态判断文件传输成功。

```powershell
python -m pip install -r requirements-build.txt
python -c "from pathlib import Path; from desktop_service import defaults; defaults(Path.cwd())"
python -m unittest discover -s tests -p "test_*.py"
```

原生界面需在 Windows 桌面会话中运行 `tray/build.ps1 -SelfTest`；
云端测试需要本人的授权会话，且正式同步必须暂停。CI 不包含真实账号或云端测试。

## 提交

说明具体问题、修改后的行为和实际完成的验证。同步历史、分片缓存和用户文件的清理必须
限定到可以证明归本工具所有的对象，不自动重置双向同步基线。
保留第三方许可证；修改 OpenList 时一并更新对应源码包和构建说明。

提交前检查 Git 暂存内容，运行 `packaging/verify-package.py` 检查干净导出目录。
不要将自己的凭据作为测试数据，也不要把真实运行日志加入测试 fixture。
