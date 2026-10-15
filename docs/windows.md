# Windows 部署与迁移

## 首次部署

确认 `python --version` 为 3.11 或更高，Python 安装目录包含 `pythonw.exe`，`codex --version` 可运行，且 Codex 已使用 ChatGPT 登录。

在准备长期保留的项目目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\Enable.ps1
Invoke-RestMethod 'http://127.0.0.1:18761/health'
Get-ScheduledTask -TaskName 'CodexTransientRetry'
```

脚本注册当前用户的登录触发任务，使用 `pythonw.exe` 启动当前目录中的 `retry_proxy.py --serve`，检查健康端点后才修改 Codex 配置。最后完全退出并重新打开 Codex。

不要在任务仍引用该目录时删除或移动目录。健康端点中的 `version: 8` 是服务修订号；Python 项目发行版本为 `0.6.0`。

## 从旧目录迁移

先结束模型任务并完全退出 Codex，在外部 PowerShell 中操作：

1. 查看当前任务的 `Actions.Execute` 和 `Actions.Arguments`，定位旧安装目录。
2. 在旧安装目录运行 `Disable.ps1`，回滚配置并移除旧任务。
3. 在新项目目录运行 `Enable.ps1`，注册指向新目录的任务。
4. 检查 `/health` 和任务路径，再启动 Codex。

```powershell
(Get-ScheduledTask -TaskName 'CodexTransientRetry').Actions
```

旧、新目录的脚本都以安装路径核对计划任务归属。新目录的启用脚本会拒绝覆盖旧任务；卸载脚本在回滚配置前检查归属，拒绝从错误目录卸载另一份安装。

项目文件整理本身不迁移正在运行的服务。旧安装路径应保留到迁移完成。

## 手动运行及配置

用于开发或无计划任务的环境：

```powershell
python retry_proxy.py --serve
```

另外一个终端可调用配置入口：

```powershell
python retry_proxy.py --configure enable
python retry_proxy.py --configure disable
```

这些 Python 命令只处理对应的代理或配置职责，不创建或移除 Windows 计划任务。正常自动部署优先使用 PowerShell 脚本。

手动运行的网关应先停止，再运行 `Enable.ps1` 注册自动任务；脚本会拒绝在没有对应计划任务的手动网关上继续安装。

## 运行数据

配置目录优先使用 `CODEX_HOME`，否则为 `%USERPROFILE%\.codex`：

- `config.toml`：Codex 配置，包含有起止标记的代理管理块。
- `config.toml.retry-backup-*.bak`：原始配置备份。
- `config.toml.retry-upgrade-*.bak`：升级前备份。
- `transient-retry/config-state.json`：回滚所需的备份位置等状态。
- `transient-retry/proxy.log`：代理元数据日志，按大小轮换。

配置备份可能包含原配置中的秘密，不能上传项目仓库。启停脚本会验证当前 Codex 是否接受配置；用户编辑过管理块时会拒绝覆盖，需先核对内容。缺失配置文件时会创建空配置，回滚后保留这个空文件或后来添加的用户设置。

验证失败时，重复启用的已有配置保持原状，首次启用或升级恢复本次操作前的内容。验证期间有并发编辑时会拒绝覆盖。辅助进程及任务可能继续保留，排查完后可从对应安装目录运行 `Disable.ps1` 清理。

## 常见排查

健康检查失败时，先查 18761 端口、计划任务状态及日志。任务从其他目录安装时，按迁移流程操作。持续服务过载会在预算用尽后返回 `local_retry_limit_exceeded`，可保留其中的请求 ID 供排查。

本程序不负责配置 VPN 或选择节点。需要网络代理时，确认代理进程启动环境／系统设置能访问上游，且本机回环地址可直接访问。
