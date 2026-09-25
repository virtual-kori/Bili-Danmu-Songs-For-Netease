@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  打包「自带运行时」的便携版：目标电脑免装 Python / Node.js
rem
rem  会把 Python 解释器、Node.js 和全部依赖一起打进去，体积较大
rem  （约 300MB，视有没有带 mpv 而定），换来的是解压即可运行。
rem
rem  常用参数：
rem    --zip        顺便压成 zip（推荐，方便拷贝）
rem    --no-mpv     不带 mpv，省约 120MB
rem    --force      目标文件夹已存在时先删掉
rem ============================================================

call "%~dp0_check_env.cmd"
if errorlevel 1 (
    pause
    exit /b 1
)

"%PYTHON_EXE%" make_portable_bundle.py %*
pause
