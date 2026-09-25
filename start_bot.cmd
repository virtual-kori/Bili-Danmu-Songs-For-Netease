@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   B站弹幕点歌机器人
echo ============================================================
echo.

call "%~dp0_check_env.cmd"
if errorlevel 1 (
    pause
    exit /b 1
)

echo 提示：这个窗口需要一直开着。
echo       播放控制（切歌/暂停/音量）可以看浏览器面板，或在直播间发弹幕。
echo       按 Ctrl+C 可以停止。
echo.

"%PYTHON_EXE%" danmaku_bot.py %*
pause
