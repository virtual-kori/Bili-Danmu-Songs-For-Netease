@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  一键运行：自动启动网易云 API 服务 + 弹幕点歌机器人
rem  参数会原样传给机器人，例如：
rem     run.cmd --room 1234567
rem     run.cmd --room https://live.bilibili.com/1234567
rem     run.cmd --stdin
rem     run.cmd --autoplay
rem ============================================================

call "%~dp0_check_env.cmd"
if errorlevel 1 (
    pause
    exit /b 1
)

".venv\Scripts\python.exe" run.py %*
set CODE=%errorlevel%

if not "%CODE%"=="0" (
    echo.
    echo [i] 退出代码 %CODE%，上面应该有具体原因。
)
pause
exit /b %CODE%
