@echo off
setlocal
chcp 65001 >nul

:: 清除代理 (修复某些环境下请求失败的问题)
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=

:: 检查虚拟环境是否存在
if not exist "venv" (
    echo [错误] 未找到虚拟环境。请先运行 install.bat。
    pause
    exit /b 1
)

:: 加载配置
if exist "config.bat" (
    call config.bat
) else (
    echo [错误] 未找到 config.bat。
    echo 请先运行 install.bat 进行初始化配置。
    pause
    exit /b 1
)

:: 激活虚拟环境并运行
call venv\Scripts\activate
echo 正在启动 涨停狙击手(商业版)...
python run_desktop.py

pause
