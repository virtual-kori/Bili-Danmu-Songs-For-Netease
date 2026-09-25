@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  打包迁移：生成一份可以拷到别的 Windows 电脑上跑的干净副本
rem  虚拟环境不会被打包（它不能跨电脑，目标机器跑 install.cmd 重建）
rem ============================================================

call "%~dp0_check_env.cmd"
if errorlevel 1 (
    pause
    exit /b 1
)

".venv\Scripts\python.exe" make_portable.py %*
pause
