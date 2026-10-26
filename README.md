# RiverNexus 自动签到工具

面向 Windows 10/11 的轻量级 Tkinter 桌面程序，用于登录 RiverNexus、查询签到信息并执行签到。程序提供手动签到、定时签到、随机时间调整、运行状态和日志查看功能；定时计划使用北京时间。

> 本项目为个人自动化工具。网站接口或页面变更可能影响程序运行，请遵守目标网站的使用规则。

## 运行要求

- Windows 10 或 Windows 11
- Python 3.10 或更高版本（运行源码时）
- 网络可访问 RiverNexus

程序使用 Python 标准库，不需要额外第三方 Python 包。

## 运行源码

```powershell
python main.py
```

首次运行会在程序目录创建本地配置和运行数据。请在软件中填写账号密码；凭据保存在本地 `config.ini`，不应分享或提交到版本库。程序还会生成签到状态、日志、截图及运行所需的数据目录。

## 打包为 Windows EXE

安装 PyInstaller 后执行：

```powershell
python -m pip install pyinstaller
python -m PyInstaller --clean --noconfirm --onefile --windowed --name RiverNexus main.py
```

生成文件位于 `dist/RiverNexus.exe`。使用打包版时无需另行安装 Python；首次运行会在 EXE 所在目录创建本地配置和运行数据。请勿公开分享含有个人运行数据的目录。

## 项目文件

- `main.py`：GUI、接口请求、定时任务及日志实现
- `requirements.txt`：依赖说明（仅使用 Python 标准库）
- `README.md`：项目说明
- `config.ini`、`state.json`：首次运行后生成的本地配置和签到状态，不纳入版本控制
- `logs/`、`screenshots/`：本机日志和截图，不纳入版本控制

## 隐私提示

账号密码以明文保存在本机 `config.ini` 中，请妥善保护该文件。提交代码前确认没有将配置、状态、日志、截图、浏览器/运行数据或个人信息加入 Git。
