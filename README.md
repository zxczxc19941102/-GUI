# RiverNexus 自动签到工具

Windows Tkinter 桌面程序，通过 RiverNexus JSON API 登录、读取福利统计并签到；北京时间在 00:00–12:00 内随机安排每日签到。

## 启动

```powershell
python main.py
```

使用 Python 标准库，无需额外第三方包。首次启动会在程序目录生成 `config.ini`（账号和密码默认留空）、`state.json` 与 `logs/`。在 GUI 填写账号密码后保存到本机配置文件。

## 网络

程序经由 `http://127.0.0.1:10808` 访问网站，请确保本地代理服务已启动且允许此端口。

## 打包无控制台 EXE

```powershell
python -m pip install pyinstaller
python -m PyInstaller --clean --noconfirm --onefile --windowed --name RiverNexus main.py
```

EXE 位于 `dist/RiverNexus.exe`。目标机器无需安装 Python，但必须可连接网络并启动上述代理。

## 隐私

账号密码保存在本地 `config.ini`。不要提交配置文件、状态、日志、构建目录或 EXE。`.gitignore` 已忽略这些文件。
