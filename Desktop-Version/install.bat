@echo off
setlocal
chcp 65001 >nul

echo =========================================
echo   涨停狙击手 Windows 安装程序
echo =========================================

:: 1. 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未安装 Python 或未将其添加到环境变量。
    echo 请从 https://www.python.org/ 安装 Python 3.8+
    pause
    exit /b 1
)

:: 2. 创建虚拟环境
if not exist "venv" (
    echo [1/3] 正在创建虚拟环境...
    python -m venv venv
) else (
    echo [1/3] 虚拟环境已存在。
)

:: 3. 安装依赖
echo [2/3] 正在安装依赖项...
call venv\Scripts\activate
pip install --upgrade pip
pip install -r ..\requirements.txt

:: 4. 设置 API 密钥
echo [3/3] 正在配置 API 密钥...
if exist "config.bat" (
    echo config.bat 已存在，跳过配置。
    goto :API_DONE
)

set /p API_KEY="请输入您的 Deepseek API 密钥: "
echo set DEEPSEEK_API_KEY=%API_KEY%> config.bat
echo 配置已保存到 config.bat

:API_DONE
echo.
echo =========================================
echo   安装完成！
echo =========================================
echo 要启动应用程序，请运行: run.bat
echo.
pause
