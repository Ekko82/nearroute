近舟 0.2.26：补齐 WebSocket 断连和 Windows 连接重置的恢复保护。

- 配置 Codex 日志信号后，现在也能识别重试记录中的 `websocket closed by server before response.completed` 和 Windows `os error 10054`，避免漏掉已有的 60 秒恢复窗口。
- 恢复期间暂缓后台测速和普通自动换点，继续检查线路；连续健康失败仍允许故障切换。旧错误不会反复延长窗口，恢复后补测一次。
- 仅识别对应传输重试字段，避免把认证、限流或服务错误正文中引用的相同文字误计为连接故障。保持只读统计时间与数量，不保存错误正文。

这次更新补齐近舟的恢复保护。WebSocket 服务端关闭、节点或网络重置的实际原因仍需在报错电脑上验证。

安装包用户可以在“版本与更新”中检查并安装，或下载 `NearRoute-0.2.26-windows.zip`。手动覆盖安装前请退出旧版，保留自己的 `config.json` 和 `state/`。首次安装需要带 tkinter 的 Python 3.10+，运行 `install.ps1` 完成设置。

Git 源码检出目录支持版本提醒与 Release 下载页，使用 `git pull --ff-only` 更新源码。安装包目录支持应用内安装。
