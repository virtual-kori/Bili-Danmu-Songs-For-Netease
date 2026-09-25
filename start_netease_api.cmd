@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   网易云 API 服务（本地）
echo ============================================================
echo.

if not exist "netease-api\node_modules" (
    echo [!] 还没安装依赖，请先运行 install.cmd
    pause
    exit /b 1
)

if "%PORT%"=="" set PORT=3000

echo 正在启动，监听 http://127.0.0.1:%PORT%
echo 这个窗口不要关，关了机器人就搜不到歌了。
echo 按 Ctrl+C 可以停止服务。
echo.

cd netease-api
node serve.js
pause
