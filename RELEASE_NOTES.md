近舟 0.2.0：新增其他 Mihomo/Clash 客户端连接入口。

- 新增“客户端连接”：控制地址、密钥环境变量、只读连接测试和 Selector 代理组选择，可接入 Clash Verge Rev、Clash Party（原 Mihomo Party）等本机 Mihomo/Clash 客户端。
- 保存新连接时先停止后台并暂停自动优选，保留测速范围、用户配置与原客户端回滚记录。
- 节点显示和手动请求绑定连接归属，避免把旧客户端同名节点套用到新连接。
- 控制接口禁止跟随 HTTP 重定向，认证密钥仍只从环境变量读取。

接入要求客户端开启本机 HTTP 控制 API，以及可用的 HTTP 或 mixed 代理端口；不支持仅提供其它管理协议的工具。已通过隔离控制器和界面逻辑回归，未对所有客户端版本逐一实机验证。

安装包用户可以在“版本与更新”中检查并安装，或下载 `NearRoute-0.2.0-windows.zip`。手动覆盖安装前请退出旧版，保留自己的 `config.json` 和 `state/`。首次安装需要带 tkinter 的 Python 3.10+，运行 `install.ps1` 完成设置。

Git 源码检出目录支持版本提醒与 Release 下载页，使用 `git pull --ff-only` 更新源码。安装包目录支持应用内安装。
