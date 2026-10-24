# Codex Transient Retry

[English](README.md) | **简体中文**

[![CI](https://github.com/shenmuegit/codex-transient-retry/actions/workflows/ci.yml/badge.svg)](https://github.com/shenmuegit/codex-transient-retry/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![Windows](https://img.shields.io/badge/Platform-Windows-blue)](docs/windows.md)
[![MIT License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

为发送到 Codex 网关的所有请求增加本地自动重试，包括模型回复、远程压缩、图片和模型列表，缓解容量不足、临时服务端错误和响应中断。适用于使用 ChatGPT 登录的 Windows Codex 客户端，复用现有登录状态与模型选择，运行时仅依赖 Python 标准库。

**每次模型响应完整接收后才交给客户端，文字和工具调用会延后显示。** 本项目是非官方辅助工具，依赖 Codex 后端接口。

## 目录

- [功能与适用范围](#功能与适用范围)
- [快速开始](#快速开始)
- [停止并回滚](#停止并回滚)
- [工作方式](#工作方式)
- [重试策略与支持路由](#重试策略与支持路由)
- [配置与运行数据](#配置与运行数据)
- [排查问题](#排查问题)
- [开发与测试](#开发与测试)
- [相关文档](#相关文档)
- [许可证](#许可证)

## 功能与适用范围

本工具面向个人本机使用，主要处理以下情况：

- 所有转发路径和 HTTP 方法都对 HTTP 错误、显式 JSON／SSE 失败及连接中断自动重试
- 完整缓冲单次响应，丢弃失败尝试中的部分输出，避免提前执行其中的本机工具调用
- 使用 Windows 计划任务在登录时启动，备份配置并支持关闭与回滚
- 图片及新增后端接口统一经过重试循环，无路由白名单

持续故障仍可能耗尽重试。错误码不决定是否重试；身份验证失败、额度不足和无效请求也使用相同策略。覆盖范围是发送到已配置网关的请求；桌面登录、更新及使用独立连接的第三方插件不会自动改道。

## 快速开始

启用前准备以下环境：

- Windows，且当前账户可以创建计划任务
- Git 和 Python 3.11+，Python 安装目录包含 `pythonw.exe`
- 已使用 ChatGPT 登录的 Codex，`python` 和 `codex` 均可从 PowerShell 调用
- 可访问 OpenAI 服务的网络；需要出站代理时，使用现有环境或系统代理设置

如果已经从其他目录部署本工具，先按 [Windows 部署与迁移](docs/windows.md#从旧目录迁移) 操作。

### 1. 获取源码并检查命令

将仓库克隆到准备长期保留的目录，然后确认 Python 和 Codex 命令可用：

```powershell
git clone https://github.com/shenmuegit/codex-transient-retry.git
Set-Location codex-transient-retry
python --version
codex --version
```

计划任务直接引用该目录中的源文件。安装期间及启用后不要移动或删除目录；运行代理无需执行 `pip install`。

### 2. 启用代理

先结束正在进行的模型任务并完全退出 Codex，再在项目目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\Enable.ps1
```

脚本创建 `CodexTransientRetry` 登录任务，确认代理启动后备份并修改 Codex 配置。保留内置 `openai` provider、ChatGPT 登录和模型选择，仅将 `openai_base_url` 指向本地网关，以兼容 Dot。客户端原生重试仍然启用，可能叠加代理尝试次数。执行策略只作用于当前 PowerShell 进程，不修改系统持久策略。

### 3. 确认状态并重新打开 Codex

检查健康端点和计划任务：

```powershell
Invoke-RestMethod 'http://127.0.0.1:18761/health'
Get-ScheduledTask -TaskName 'CodexTransientRetry'
```

健康检查应返回 `service: codex-transient-retry` 和 `version: 12`。这里的 `version` 是服务修订号，项目发行版本为 `0.6.0`。确认后重新打开 Codex，使新连接使用本地代理。

## 停止并回滚

先结束模型任务并退出 Codex，再从原安装目录运行关闭脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\Disable.ps1
```

脚本移除本工具管理的配置块及计划任务，并请求代理退出；其他配置和历史备份保留。随后重新打开 Codex，加载恢复后的连接配置。

如果管理块经过手动编辑，脚本会拒绝覆盖。跨目录安装、升级及校验失败的处理方式见 [Windows 部署与迁移](docs/windows.md)。

## 工作方式

Codex 将请求发送到本地代理。所有转发请求都保留原有鉴权头，完整接收响应并检查结果，再决定返回或重试：

```mermaid
flowchart TD
    C[Codex 客户端] --> P[本地代理 127.0.0.1:18761]
    P --> U[OpenAI Codex 服务]
    U --> B[完整缓冲并检查响应]
    B -->|成功或不可重试| C
    B -->|失败尝试| R{还有重试预算}
    R -->|有| W[丢弃本次缓冲并等待]
    W --> P
    R -->|耗尽| E[返回最后一次上游结果]
    E --> C
```

上游站点固定为 `https://chatgpt.com`，原始请求路径与查询参数保持不变。出站连接可使用环境或系统配置的网络代理；本工具不配置代理软件或选择节点。

缓冲单位是一次 HTTP 请求。失败尝试的部分文字与本机工具调用不会提前下发；服务端已执行的操作无法回滚，后端请求也可能执行多次。协议解析及连接升级细节见 [实现原理](docs/architecture.md)。

## 重试策略与支持路由

所有转发请求采用相同策略，不设置路径、方法、鉴权、Origin 或错误码白名单／黑名单。

| 设置 | 行为 |
| --- | --- |
| 每次进入代理的请求 | 最多尝试 4 次：首次请求加 3 次重试 |
| 退避间隔 | 30、60、120 秒；有效的 `Retry-After` 可以延长等待 |
| 失败尝试 | 所有大于等于 400 的 HTTP 状态、显式 JSON／SSE 错误、连接或响应中断 |
| 重试耗尽 | 返回最后一次上游状态、响应头和正文；没有上游响应的连接故障返回 HTTP 502 |
| 模型列表 | 每次重新获取，无缓存及专属超时 |
| 重定向 | 将原始状态和 `Location` 返回客户端 |
| WebSocket 及其他 HTTP 升级 | 握手失败可重试；收到 HTTP 101 后双向转发字节 |
| `GET /health` | 返回服务修订号、进程及重试参数 |

这是一轮代理请求的预算，不是整个客户端操作的上限：代理耗尽后，Codex 仍可能重新请求。内置 provider ID 不能通过重新定义来关闭这些原生重试。Dot 云端主会话使用自己的连接，不经过这个本机网关。

代理不再附加请求／响应大小上限、I/O 超时、重试调度总时限、并发配额或内存配额。监听地址仍为本机回环，上游仍为配置的 ChatGPT 来源；上游鉴权、额度和客户端超时由它们各自处理。

业务正文保持不变，包括 gzip 压缩响应字节及调用方的 `Accept-Encoding`。读取 HTTP 长度／分块边界是为了保存可重放的请求：分块上传解码后按内容长度重放，trailer 字段转为请求头；逐跳连接头由两段 HTTP 连接分别处理。这些属于协议处理，不用于决定请求是否获准。普通 HTTP 转发并非逐字节 TCP 隧道。

普通 HTTP 响应仍完整缓冲，避免把失败尝试中的本机工具调用提前交付。升级连接开始双向转发后，已交付的字节无法透明重放；原生 WebSocket 消息错误由客户端处理，不提供消息级缓冲重试。

图片及其他写入请求保留调用方的幂等键。上游已完成但响应丢失时，重试可能重复执行或计费。无上限缓冲可能占用大量内存；静默连接可能持续等待，直到客户端取消或连接关闭。升级 Codex 后需重新验收原生连接兼容性。

## 配置与运行数据

Codex 配置仍位于 `%USERPROFILE%\.codex\config.toml`，也可通过 `CODEX_HOME` 指定。日志、回滚状态和备份默认保存在 `retry_proxy.py` 所在目录的 `local-diagnostics/transient-retry/` 下，可用 `CODEX_RETRY_RUNTIME_DIR` 指定其他运行目录。下表路径相对于运行目录：

| 路径 | 用途 |
| --- | --- |
| `config.toml.retry-backup-*.bak` | 原始配置备份 |
| `config.toml.retry-upgrade-*.bak` | 升级前配置备份 |
| `config-state.json` | 配置回滚状态 |
| `proxy.log` | 代理元数据日志，最多保留 3 个约 2 MiB 的文件 |

升级时读取旧 Codex 配置目录中的回滚状态，将原始备份复制到运行目录；旧状态、备份和日志保留原状。

日志不记录凭据、提示词或回复正文。配置备份可能包含敏感设置，应保留在本机；仓库不包含登录状态、个人配置、备份、运行日志或代理规则文件。

## 排查问题

先检查健康端点，再根据现象查看任务、日志或配置：

| 现象 | 排查方向 |
| --- | --- |
| 健康检查连接失败 | 检查计划任务状态、18761 端口占用及 `proxy.log` |
| 提示已有其他安装的计划任务 | 从旧安装目录关闭工具，再按迁移流程启用新目录 |
| 发送请求后暂时没有文字输出 | 单次响应会完整缓冲；查看日志区分正常等待与故障重试 |
| 重试后仍返回上游错误 | 查看日志中的 `exhausted` 及原始上游错误／请求 ID |
| 代理启动但无法访问上游 | 检查代理进程使用的环境／系统代理设置，并确保回环地址可直接访问 |

更多操作见 [Windows 排查说明](docs/windows.md#常见排查)。反馈问题可提交 [GitHub Issue](https://github.com/shenmuegit/codex-transient-retry/issues)，附上版本和脱敏错误信息，保留登录文件与配置备份在本机。

## 开发与测试

在项目目录创建虚拟环境，安装开发依赖后执行检查、测试和构建：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m build
```

测试使用本机模拟服务和临时配置，无需 OpenAI 凭据。Windows 脚本测试模拟计划任务及网络操作；CI 覆盖 Windows Python 3.11／3.13、Ubuntu Python 3.11，以及格式、构建和安装后的命令入口。

Python 包提供 `codex-transient-retry` 命令。安装后可用 `codex-transient-retry --serve` 手动运行；Windows 自动启停仍使用源码目录中的 PowerShell 脚本，手动部署步骤见 [Windows 部署文档](docs/windows.md#手动运行及配置)。

## 相关文档

根据需要查看以下文档：

| 文档 | 内容 |
| --- | --- |
| [实现原理](docs/architecture.md) | 响应缓冲、统一重试及协议边界 |
| [Windows 部署与迁移](docs/windows.md) | 首次部署、旧目录迁移、手动运行、回滚和排查 |
| [验证与兼容性基线](docs/validation.md) | 已验证版本、验证方式及未验证范围 |
| [更新记录](CHANGELOG.md) | 项目版本变更 |

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
