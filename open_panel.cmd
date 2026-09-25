@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  重新打开浏览器控制台（面板）
rem
rem  关掉浏览器标签页不会停掉机器人 —— 面板服务一直在跑。
rem  这个脚本只是帮你把那个网址重新打开。
rem ============================================================

call "%~dp0_check_env.cmd"
if errorlevel 1 (
    pause
    exit /b 1
)

"%PYTHON_EXE%" open_panel.py %*
if errorlevel 1 pause
exit /b 0
