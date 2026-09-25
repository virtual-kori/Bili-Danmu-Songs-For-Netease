@echo off
rem ============================================================
rem  启动前的环境检查（被 run.cmd / start_bot.cmd 等 call 调用）
rem  退出码 0 = 可以启动；非 0 = 别往下走
rem
rem  这里要拦住的最典型情况：整套文件夹是从别的电脑拷过来的，
rem  而 .venv 是不可移植的 —— 直接跑会报一堆看不懂的错。
rem ============================================================

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo.
    echo [!] 还没安装依赖，请先双击 install.cmd
    echo.
    exit /b 1
)

"%~dp0.venv\Scripts\python.exe" "%~dp0bootstrap.py" %QDGJ_BOOTSTRAP_ARGS%
if errorlevel 1 exit /b 1
exit /b 0
