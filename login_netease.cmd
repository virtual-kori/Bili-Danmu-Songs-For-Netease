@echo off
chcp 65001 >nul
cd /d "%~dp0"

call "%~dp0_check_env.cmd"
if errorlevel 1 (
    pause
    exit /b 1
)

rem 扫码登录要用到网易云 API 服务，没起的话提示一下
"%PYTHON_EXE%" -c "import sys; sys.path.insert(0,'.'); from netease_api import NeteaseClient; sys.exit(0 if NeteaseClient().ping() else 1)" >nul 2>&1
if errorlevel 1 (
    echo [!] 网易云 API 服务没在运行，请先双击 start_netease_api.cmd
    echo     ^(或者直接双击 run.cmd，它会自己把服务带起来^)
    echo.
    pause
    exit /b 1
)

"%PYTHON_EXE%" qr_login.py
pause
