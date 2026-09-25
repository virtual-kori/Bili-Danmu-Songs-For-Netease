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
rem
rem  便携包（有 runtime\ 目录）和开发环境（有 .venv）都能用，
rem  具体用哪个解释器由 _check_env.cmd 决定。
rem ============================================================

rem 环境检查会设置 PYTHON_EXE；如果套了 setlocal，这个变量在本文件里依然可用
call "%~dp0_check_env.cmd"
if errorlevel 1 (
    pause
    exit /b 1
)

rem 便携包：让内置解释器能 import 项目里的模块（embeddable 版默认不含脚本目录），
rem 同时把依赖目录加进去。控制台编码也一起设成 UTF-8，否则中文日志会乱码。
if exist "%~dp0runtime\python\python.exe" (
    set "PYTHONPATH=%~dp0;%~dp0runtime\python\Lib\site-packages"
    set "PYTHONIOENCODING=utf-8"
    set "PYTHONUTF8=1"
    rem 不要让内置 node 的 npm 去写用户目录，缓存留包内
    if exist "%~dp0runtime\node\node.exe" set "PATH=%~dp0runtime\node;%PATH%"
)

"%PYTHON_EXE%" run.py %*
set CODE=%errorlevel%

if not "%CODE%"=="0" (
    echo.
    echo [i] 退出代码 %CODE%，上面应该有具体原因。
)
pause
exit /b %CODE%
