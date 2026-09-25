@echo off
chcp 65001 >nul
cd /d "%~dp0"

call "%~dp0_check_env.cmd"
if errorlevel 1 (
    pause
    exit /b 1
)

echo 正在下载 mpv 便携版到 tools\mpv\（约 32MB，不需要管理员权限）
echo.
".venv\Scripts\python.exe" get_mpv.py %*
pause
