@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

echo ============================================================
echo   一键安装依赖
echo ============================================================
echo.

rem ---------------------------------------------------------- 0. 找 Python
set PYTHON=
where python >nul 2>nul && set PYTHON=python
if "%PYTHON%"=="" (
    where py >nul 2>nul && set PYTHON=py -3
)
if "%PYTHON%"=="" (
    echo [!] 没找到 Python。
    echo     请先安装 Python 3.9 或更高版本：https://www.python.org/downloads/
    echo     安装时记得勾选 "Add python.exe to PATH"。
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PYTHON% --version 2^>^&1') do echo   %%v

rem 版本太老直接拦下来（后面用到的语法和依赖都需要 3.9+）
%PYTHON% -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [!] Python 版本太低，需要 3.9 或更高。请升级后重试。
    pause
    exit /b 1
)

rem ---------------------------------------------------------- 0b. 找 Node
where node >nul 2>nul
if errorlevel 1 (
    echo [!] 没找到 Node.js。
    echo     请先安装 Node.js 18 或更高版本：https://nodejs.org/
    pause
    exit /b 1
)
for /f "delims=" %%v in ('node -v') do echo   Node.js %%v
echo.

rem ------------------------------------------- 1. 网易云 API 服务端依赖
echo [1/4] 安装网易云 API 服务端依赖...
if exist "netease-api\node_modules\@neteasecloudmusicapienhanced\api\package.json" (
    echo     已安装，跳过
) else (
    rem 缓存放在项目目录里，整套东西可以整体搬走，不依赖用户目录
    set "npm_config_cache=%~dp0.npm-cache"
    pushd netease-api
    call npm install
    set NPM_RESULT=%errorlevel%
    popd
    if not "%NPM_RESULT%"=="0" (
        echo [!] npm install 失败。检查一下网络，或换用 Node.js 官方安装包重装。
        pause
        exit /b 1
    )
)
echo.

rem ---------------------------------------------------------- 2. 虚拟环境
echo [2/4] 准备 Python 虚拟环境...
rem 虚拟环境不可移植：从别的电脑拷来的 .venv 里记着原机器的 Python 路径，
rem 在本机根本用不了，所以检测到无效就删掉重建。
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.prefix.lower().endswith('.venv') else 1)" >nul 2>&1
    if errorlevel 1 (
        echo     检测到无效的虚拟环境（可能是从别的电脑拷来的），正在重建...
        rmdir /s /q ".venv"
    )
)
if exist ".venv\Scripts\python.exe" (
    echo     已存在且可用，跳过
) else (
    %PYTHON% -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo [!] 虚拟环境创建失败。确认 Python 安装时勾选了 Add to PATH。
        pause
        exit /b 1
    )
)
echo.

rem ---------------------------------------------------------- 3. Python 依赖
echo [3/4] 安装 Python 依赖...
echo     说明：先装小写 brotli（有现成 wheel），再用 --no-deps 装 blivedm。
echo     blivedm 把 brotli==1.0.9 写死了，那个版本在新版 Python 上要源码编译且编不过。
set "SP=%~dp0.venv\Lib\site-packages"
%PYTHON% -m pip install --disable-pip-version-check --no-warn-script-location --target "%SP%" ^
    aiohttp brotli requests qrcode pillow pygame
if errorlevel 1 (
    echo [!] 依赖安装失败。检查一下网络；国内网络慢可以换镜像重试：
    echo     %PYTHON% -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --target "%SP%" aiohttp brotli requests qrcode pillow pygame
    pause
    exit /b 1
)
%PYTHON% -m pip install --disable-pip-version-check --no-warn-script-location --no-deps --target "%SP%" blivedm
echo.

rem ---------------------------------------------------------- 4. 播放器
echo [4/4] 播放器
if exist "tools\mpv\mpv.exe" (
    echo     已检测到 mpv，跳过
    goto after_mpv
)
echo     mpv 能直接流播网络音频、起播几乎瞬间，强烈建议装上（约 32MB，免管理员权限）。
set /p GETMPV="    现在下载 mpv 吗？[Y/n] "
if /i "%GETMPV%"=="n" goto after_mpv
".venv\Scripts\python.exe" get_mpv.py
echo     （如果下载失败也不影响使用，程序会自动退回 pygame 后端）

:after_mpv
echo.
echo ============================================================
echo   安装完成。接下来按顺序来：
echo     1. 双击 run.cmd               一键启动（API 服务 + 机器人）
echo     2. 首次运行会问你要直播间号，粘直播间网址也行
echo     3. 想放会员歌就先双击 login_netease.cmd 扫码
echo     4. 出问题就双击 check_env.cmd 自检
echo ============================================================
pause
