近舟 0.2.17：正确处理测速链接的 HTTP 临时响应。

- 自定义链接先返回 `103 Early Hints` 等临时响应时，继续读取最终状态，避免把正常线路误判为失败。HTTP、HTTPS 及 HTTPS 代理隧道均支持，不增加探测请求。
- 保留最终状态、响应完整性、同一连接复用与临时响应数量上限的检查；`101 Switching Protocols` 不会被跳过。

安装包用户可以在“版本与更新”中检查并安装，或下载 `NearRoute-0.2.17-windows.zip`。手动覆盖安装前请退出旧版，保留自己的 `config.json` 和 `state/`。首次安装需要带 tkinter 的 Python 3.10+，运行 `install.ps1` 完成设置。

Git 源码检出目录支持版本提醒与 Release 下载页，使用 `git pull --ff-only` 更新源码。安装包目录支持应用内安装。
