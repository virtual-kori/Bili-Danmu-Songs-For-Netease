@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [!] Python 环境不存在，请先运行 install.cmd
    pause
    exit /b 1
)

".venv\Scripts\python.exe" check_env.py
pause
