@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

echo =========================================
echo   涨停狙击手 更新程序 (Windows)
echo =========================================

:: 1. 备份数据
echo [1/3] 正在备份数据...
if exist "..\backend\data" (
    if exist "._data_backup" rd /s /q "._data_backup"
    xcopy /E /I /Q /Y "..\backend\data" "._data_backup" >nul
    echo 数据已备份到 ._data_backup
)

:: 2. Git 拉取
echo [2/3] 正在拉取最新代码...
git config --global --add safe.directory "*"
pushd ..
git pull
set GIT_EXIT_CODE=%errorlevel%
popd

if %GIT_EXIT_CODE% neq 0 (
    echo.
    echo [!] Git 拉取失败。可能是由于本地数据更改导致的冲突。
    echo.
    echo 选项:
    echo  1. 强制更新 (丢弃代码更改，保留数据) - 推荐
    echo  2. 取消
    echo.
    set /p CHOICE="请输入选项 (1/2): "
    
    if "!CHOICE!"=="1" (
        echo.
        echo 正在强制更新...
        pushd ..
        for /f "tokens=*" %%i in ('git rev-parse --abbrev-ref HEAD') do set BRANCH=%%i
        git fetch --all
        git reset --hard origin/!BRANCH!
        popd
        
        :: 还原数据
        if exist "._data_backup" (
            echo 正在还原数据...
            xcopy /E /I /Q /Y "._data_backup" "..\backend\data" >nul
        )
    ) else (
        echo.
        echo 已取消。正在还原数据...
        if exist "._data_backup" (
            xcopy /E /I /Q /Y "._data_backup" "..\data" >nul
            rd /s /q "._data_backup"
        )
        pause
        exit /b 1
    )
) else (
    :: 正常拉取成功，仍还原数据以确保保留本地配置（如果它们被覆盖了）
    if exist "._data_backup" (
        echo 正在还原数据...
        xcopy /E /I /Q /Y "._data_backup" "..\data" >nul
    )
)

:: 清理备份
if exist "._data_backup" rd /s /q "._data_backup"

:: 3. 更新依赖
echo [3/3] 正在更新依赖项...
if exist "venv" (
    call venv\Scripts\activate
    pip install -r ..\requirements.txt
) else (
    echo [警告] 未找到虚拟环境。
)

echo.
echo 更新完成！
pause
