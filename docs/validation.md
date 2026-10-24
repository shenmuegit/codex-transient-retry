# 验证与兼容性基线

发布前执行本地回归、静态检查、源码与 wheel 构建，以及安装后的命令入口验证。当前服务修订为 12，项目发行版本仍为 0.6.0；本次修改尚未发布。

## 自动回归

测试使用本机模拟上游和临时配置，无需 OpenAI 凭据，不调用真实生图服务。覆盖统一重试次数及退避、任意路径和常见／扩展方法、模型容量及服务端失败、远程压缩、断流、`Retry-After`、取消、gzip／JSON 损坏、非法 UTF-8、SSE BOM／换行、304 和完整缓冲。

修订 10 的转发回归覆盖缺失或非 Bearer 鉴权、网页 Origin、未知错误码及 HTTP 4xx／5xx、移除正文大小上限、分块上传及 trailer 重放、绝对形式的目标、重定向原样返回、gzip 编码与字节保留、每次获取模型列表、耗尽后保留上游错误，以及升级握手重试和 WebSocket 双向转发。

配置测试覆盖缺失文件、原 provider 的保存与恢复、旧头及旧 provider 块升级、幂等启用、原始／升级备份和回滚。Windows 脚本测试在独立 PowerShell 进程中模拟 Codex 校验、计划任务和网络操作，验证跨目录安装保护，以及首次启用、重复启用、升级和并发编辑时的校验失败处理；不会操作真实计划任务。

## 原生客户端与 CI

修订 8 曾在 Codex CLI／app-server `0.162.0-alpha.17.2` 和 Windows 桌面包 `26.1007.2314.0` 验证 HTTP 回退及本地终止错误。修订 10 已删除这两项行为，原来的原生连接结果不能作为本次兼容性结论。当前自动回归验证本地 HTTP 和升级转发，修订 12 对原生 OpenAI 身份和客户端叠加重试执行 CLI 故障注入回归；真实账户及原生 WebSocket 模型会话仍需相应环境验收。

CI 配置 Windows Python 3.11／3.13 和 Ubuntu Python 3.11 测试矩阵，另检查 Ruff、源码／wheel 构建，以及离开源码目录后安装的命令入口。Actions 固定到已核对的提交 SHA，只申请仓库内容读取权限。本机运行不代表远端 CI 已执行。

## 本地复现

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
python -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m build
python retry_proxy.py --help
```

长期并发负载、内存耗尽、真实 HTTPS／网络代理组合、不同操作系统及后续 Codex 版本需要相应环境验证。普通 HTTP 仍完整缓冲；升级后不做消息级重试。

## 修订 11 回归

新增正常 zlib／raw deflate、叠加内容编码、未知编码透传、显式压缩错误恢复、false 错误字段、深层 JSON／长整数／深层 SSE、多值请求头重放，以及修订 10 配置迁移和精确回滚。

## 修订 12 回归

配置迁移验证内置 OpenAI 身份、旧自定义 provider 块移除、provider 原始行及位置恢复、重复启用，以及迁移到项目运行目录后不在旧配置目录创建工作文件。

安装了 Codex CLI 时，自动回归还会用临时配置、虚拟凭据和本地模拟上游验证：首次失败后恢复执行 2 次 POST；连续 4 次 503 耗尽一轮代理预算后，原生客户端可以再次请求，在第 5 次 POST 恢复。该测试不承诺客户端总尝试次数上限。测试将代理等待设为 0；生产退避由独立回归验证。没有安装 Codex CLI 时，这两项原生测试会明确跳过。不会创建或使用真实 OpenAI API key，也不会调用真实生图服务。
