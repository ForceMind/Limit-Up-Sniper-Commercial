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
    echo [警告] 未找到 config.bat。
    set /p API_KEY="请输入您的 Deepseek API 密钥 (按回车跳过): "
    if not "%API_KEY%"=="" (
        set DEEPSEEK_API_KEY=%API_KEY%
        echo set DEEPSEEK_API_KEY=%API_KEY%> config.bat
    )
)

:: 激活虚拟环境并运行
call venv\Scripts\activate
set PYTHONPATH=..;%PYTHONPATH%
echo 正在启动 涨停狙击手...
echo 访问地址: http://127.0.0.1:8000
python -m uvicorn app.main:app --reload --port 8000

pause
