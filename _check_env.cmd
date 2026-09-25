@echo off
rem ============================================================
rem  启动前的环境检查（被 run.cmd / start_bot.cmd 等 call 调用）
rem  退出码 0 = 可以启动；非 0 = 别往下走
rem
rem  做完三件事：
rem    1. 选中要用的 Python 解释器 -> PYTHON_EXE
rem    2. 便携包（有 runtime\ 目录）时设好 PYTHONPATH / 编码 / node 路径
rem    3. 跑 bootstrap.py 做实际自检
rem
rem  两种情况都支持：
rem    a. 便携包：runtime\python\python.exe 是包内自带的解释器，
rem       目标电脑不需要装 Python，路径是相对的，拷到哪台机器都能用。
rem    b. 开发环境：.venv（不可跨电脑，从别处拷来的必须重建）
rem ============================================================

set "PYTHON_EXE="
if exist "%~dp0runtime\python\python.exe" (
    set "PYTHON_EXE=%~dp0runtime\python\python.exe"
    rem 说明：项目根目录不是靠 PYTHONPATH 生效的 —— embeddable 解释器一旦有 ._pth
    rem 文件就进入 isolated 模式，会忽略 PYTHONPATH。真正起作用的是打包时写进
    rem python*._pth 里的相对路径（..\..）。这里设 PYTHONPATH 只是给可能存在的
    rem 其它解释器留个兼容，无害。
    set "PYTHONPATH=%~dp0;%~dp0runtime\python\Lib\site-packages"
    set "PYTHONIOENCODING=utf-8"
    set "PYTHONUTF8=1"
    if exist "%~dp0runtime\node\node.exe" set "PATH=%~dp0runtime\node;%PATH%"
    set "PYGAME_HIDE_SUPPORT_PROMPT=1"
) else if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
)

if not defined PYTHON_EXE (
    echo.
    echo [!] 找不到 Python 运行环境。
    echo.
    echo     如果是便携包：runtime 目录不完整，请重新解压整个文件夹。
    echo     如果是源码：请先双击 install.cmd 安装依赖。
    echo.
    exit /b 1
)

rem 便携包自带的解释器不可能"跨电脑失效"，跳过虚拟环境校验
if exist "%~dp0runtime\python\python.exe" goto run_bootstrap

"%PYTHON_EXE%" -c "import sys; sys.exit(0 if sys.prefix.lower().endswith('.venv') else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [!] 虚拟环境不可用（可能是从别的电脑拷过来的）。
    echo     请重新双击 install.cmd 在本机重建。
    echo.
    exit /b 1
)

:run_bootstrap
"%PYTHON_EXE%" "%~dp0bootstrap.py" %QDGJ_BOOTSTRAP_ARGS%
if errorlevel 1 exit /b 1
exit /b 0
