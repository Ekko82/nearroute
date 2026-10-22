# 开发指南

近舟运行于带 tkinter 的 Python 3.10+，运行和常规测试使用标准库。以下命令均在仓库根目录执行。

## 目录职责

```text
nearroute/
├─ dashboard.py          # 桌面窗口与用户操作入口
├─ optimizer.py          # 节点探测、优选及后台命令入口
├─ client_detection.py   # 本机控制接口发现与代理组判断
├─ updater.py            # 应用内更新与安装工作进程
├─ tray_icon.py          # Windows 系统托盘
├─ windows_app.py        # Windows 应用标识及窗口图标
├─ install.ps1           # 当前用户安装、快捷方式与登录启动
├─ launch.ps1            # 启动桌面窗口
├─ uninstall.ps1         # 撤销安装及按需恢复节点
├─ tests/                # 离线测试、隔离安装和托盘检查
├─ tools/                # 构建正式安装包与生成图标
│  └─ local/             # 个人开发脚本，Git 忽略
├─ assets/               # 随程序分发的图标及图标源文件
├─ docs/                 # 开发与维护说明
├─ .github/workflows/    # 测试与 Release 工作流
├─ config.example.json   # 可公开的默认配置
├─ config.json           # 本机配置，Git 忽略
├─ state/                # 本地状态、日志、备份和预览，Git 忽略
├─ dist/                 # 构建生成的正式更新资产，Git 忽略
├─ VERSION               # 当前正式版本号
├─ RELEASE_NOTES.md      # 本次更新说明
├─ README.md             # 安装、使用和故障诊断
└─ LICENSE
```

运行模块及三个 PowerShell 入口保留在根目录，安装快捷方式、后台命令和更新器继续使用同一路径。`tests/` 是独立的测试包；测试之间通过 `tests.*` 导入共用夹具。测试和开发工具不放入正式安装包。

`config.json` 和 `state/` 属于本机数据，不随目录整理迁移或删除，也不应提交到仓库。新增测试、工具和文档分别放入对应目录；个人诊断脚本放入已忽略的 `tools/local/`，临时结果留在 `state/`。这些本地目录无需出现在新的源码检出中。发布资产由工具生成到 `dist/`，不要手工将整个工作目录打包。

## 运行与测试

首次从源码运行，可复制样例配置后打开窗口：

```powershell
Copy-Item .\config.example.json .\config.json
python -B dashboard.py
```

已有 `config.json` 时直接运行窗口，保留本机设置。安装和后台命令见 [README](../README.md)。

运行全部测试，或仅运行一个模块：

```powershell
python -B -m unittest discover -v
python -B -m unittest tests.test_install_lifecycle -v
```

测试使用模拟控制器，不发送真实代理请求或切换节点。CI 在 Ubuntu 和 Windows 上测试 Python 3.10、3.12、3.13；没有图形显示时，Tk 窗口测试跳过。Windows 安装、单实例锁和登录启动集成在 Windows 上验证。

Windows 隔离安装检查使用临时目录、临时注册表键和模拟后台，不修改实际桌面、登录启动项或代理节点。CI 的 Windows/Python 3.12 作业也运行这项检查：

```powershell
$env:NEARROUTE_INSTALL_SMOKE = '1'
python -B -m unittest tests.test_install_lifecycle -v
Remove-Item Env:NEARROUTE_INSTALL_SMOKE
```

Windows 真实托盘检查需要可用的桌面会话和系统托盘，默认跳过，不包含在常规离线 CI 中。它只创建唯一命名的临时图标，检查图标尺寸、透明像素、唤回消息、重建及退出资源回收，不操作现有近舟窗口或重启资源管理器：

```powershell
$env:NEARROUTE_TRAY_SMOKE = '1'
python -B -m unittest tests.test_tray_icon.WindowsTraySmokeTests -v
Remove-Item Env:NEARROUTE_TRAY_SMOKE
```

## 构建与发布

`assets/` 保存图标源和生成文件。需要修改图标时，安装开发工具所需的 Pillow，再运行 `python -B tools/build_icon.py`；预览输出到已忽略的 `state/icon-preview/`。普通运行与测试直接使用仓库中的 ICO，无需 Pillow。

`tools/build_release.py` 按白名单生成 `dist/NearRoute-X.Y.Z-windows.zip` 和 `dist/nearroute-update.json`，不包含测试、本机配置和状态。检查本地构建：

```powershell
python -B tools/build_release.py
```

发布时先更新 `VERSION` 中的 `X.Y.Z` 和 `RELEASE_NOTES.md` 中的本次说明，运行测试并检查实际提交内容，然后提交并推送匹配的 `vX.Y.Z` 标签。构建工具的 `--tag vX.Y.Z` 可检查标签与 `VERSION` 是否一致。

Release 工作流通过 Windows / Ubuntu 的六组测试后，生成白名单 ZIP 和校验清单，上传完整草稿再发布。应用只跟随正式 Release，不跟随每次 `main` 提交。维护者可在 GitHub Release 中编辑面向用户的更新说明。
