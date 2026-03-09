@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

echo =========================================
echo   涨停狙击手 更新程序 (Windows)
echo =========================================

REM 1. 备份数据
echo [1/3] 正在备份数据...
if exist "..\backend\data" (
    if exist "._data_backup" rd /s /q "._data_backup"
    xcopy /E /I /Q /Y "..\backend\data" "._data_backup" >nul
    echo 数据已备份到 ._data_backup
)

REM 2. Git 拉取
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
    echo  1. 强制更新 - 丢弃代码更改并保留数据 - 推荐
    echo  2. 取消
    echo.
    set /p CHOICE="请输入选项 1/2: "

    if "!CHOICE!"=="1" (
        echo.
        echo 正在强制更新...
        pushd ..
        for /f "tokens=*" %%i in ('git rev-parse --abbrev-ref HEAD') do set BRANCH=%%i
        git fetch --all
        git reset --hard origin/!BRANCH!
        popd

        REM 还原数据
        if exist "._data_backup" (
            echo 正在还原数据...
            xcopy /E /I /Q /Y "._data_backup" "..\backend\data" >nul
        )
    ) else (
        echo.
        echo 已取消。正在还原数据...
        if exist "._data_backup" (
            xcopy /E /I /Q /Y "._data_backup" "..\backend\data" >nul
            rd /s /q "._data_backup"
        )
        pause
        exit /b 1
    )
) else (
    REM 正常拉取成功后，仍还原数据以确保保留本地配置
    if exist "._data_backup" (
        echo 正在还原数据...
        xcopy /E /I /Q /Y "._data_backup" "..\backend\data" >nul
    )
)

REM 清理备份
if exist "._data_backup" rd /s /q "._data_backup"

REM 3. 更新依赖
echo [3/3] 正在更新依赖项...
if exist "venv" (
    call venv\Scripts\activate
    venv\Scripts\python.exe -m pip install --upgrade pip
    venv\Scripts\python.exe -m pip install -r ..\backend\requirements.txt -i https://pypi.org/simple --trusted-host pypi.org --trusted-host files.pythonhosted.org
    if !errorlevel! neq 0 (
        echo [警告] 官方 PyPI 安装失败，正在尝试清华镜像...
        venv\Scripts\python.exe -m pip install -r ..\backend\requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
    )
    if !errorlevel! neq 0 (
        echo [警告] 清华镜像安装失败，正在尝试阿里云镜像...
        venv\Scripts\python.exe -m pip install -r ..\backend\requirements.txt -i http://mirrors.cloud.aliyuncs.com/pypi/simple/ --trusted-host mirrors.cloud.aliyuncs.com
    )
    if !errorlevel! neq 0 (
        echo [错误] 依赖更新失败，请检查网络或代理设置后重试。
        pause
        exit /b 1
    )
) else (
    echo [警告] 未找到虚拟环境。
)

echo.
echo 更新完成！
pause
