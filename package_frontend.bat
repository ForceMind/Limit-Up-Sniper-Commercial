@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo 前端分离打包工具（Windows 一键）
echo ============================================================
echo.
echo 说明：
echo - 直接回车：使用默认自动识别 API 逻辑
echo - 输入后端地址：固定分离部署时的后端 API 地址
echo   示例：https://api.your-domain.com
echo - 后台目录：管理后台目录名（默认：admin）
echo - 管理员API前缀：默认自动推断（示例：/api/admin-panel）
echo.

set "PY_CMD="
if exist ".venv\Scripts\python.exe" (
    set "PY_CMD=.venv\Scripts\python.exe"
) else (
    set "PY_CMD=python"
)

"%PY_CMD%" --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 或创建 .venv。
    pause
    exit /b 1
)

set /p API_BASE=请输入后端 API 地址（留空使用默认）： 
set /p ADMIN_PATH=请输入后台目录名（默认：admin）： 
set /p ADMIN_API_PREFIX=请输入管理员 API 前缀（留空自动推断）： 
if "%ADMIN_PATH%"=="" set "ADMIN_PATH=admin"

set "CMD_ARGS=--admin-path "%ADMIN_PATH%""
if not "%API_BASE%"=="" set "CMD_ARGS=!CMD_ARGS! --api-base "%API_BASE%""
if not "%ADMIN_API_PREFIX%"=="" set "CMD_ARGS=!CMD_ARGS! --admin-api-prefix "%ADMIN_API_PREFIX%""

echo.
echo [执行] 开始打包...
echo [参数] %CMD_ARGS%
"%PY_CMD%" scripts\package_frontend.py %CMD_ARGS%

if errorlevel 1 (
    echo.
    echo [失败] 打包失败，请检查输入参数与脚本输出日志。
    pause
    exit /b 1
)

echo.
echo [完成] 打包成功。
echo - 目录：dist\frontend_split
echo - 压缩包：dist\frontend_split_package-版本号.zip（具体以脚本输出为准）
echo - 后台入口：/%ADMIN_PATH%/index.html
echo.
pause
