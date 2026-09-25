"""启动前的环境自检。

给 .cmd 启动脚本调用，专门回答一个问题：**这台机器上的环境现在能不能跑？**

最需要提前发现的是"虚拟环境是从别的电脑拷过来的"这种情况 ——
`.venv` 是不可移植的，`pyvenv.cfg` 里记着创建它的那台机器上 Python 的绝对路径。
拷到别的电脑后那个路径不存在，python.exe 要么起不来、要么跑偏，
用户看到的往往是一堆莫名其妙的报错。这里直接给出"请重新运行 install.cmd"。

    .venv\\Scripts\\python.exe bootstrap.py      # 退出码 0 = 可以跑
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
from pathlib import Path

# 别让 pygame 在自检时打印欢迎语
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

# 便携包用的是 embeddable 解释器（isolated 模式），sys.path 不含脚本所在目录，
# 所以得自己把项目根目录塞进去，否则 `import common` 会失败。
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VENV = ROOT / ".venv"
VENV_PYTHON = VENV / "Scripts" / "python.exe"
PYVENV_CFG = VENV / "pyvenv.cfg"
RUNTIME_NODE = ROOT / "runtime" / "node" / "node.exe"

# 跑起来必须有的第三方包
REQUIRED_MODULES = ("blivedm", "aiohttp", "brotli", "requests", "qrcode")

MIN_PYTHON = (3, 9)

OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"

problems: list[str] = []
warnings: list[str] = []


def say(tag: str, message: str) -> None:
    print(f"{tag} {message}")


def check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        problems.append(
            f"Python 版本太低（当前 {sys.version.split()[0]}，需要 "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+）"
        )
        say(FAIL, f"Python {sys.version.split()[0]}（需要 {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+）")
    else:
        say(OK, f"Python {sys.version.split()[0]}")


def read_pyvenv_home() -> str:
    """读出 pyvenv.cfg 里记录的、创建这个虚拟环境时用的基础 Python 路径。"""
    try:
        for line in PYVENV_CFG.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "home":
                return value.strip()
    except OSError:
        pass
    return ""


def check_venv() -> None:
    """确认 Python 运行环境可用。

    便携包（自带 runtime/python）和开发时的 .venv 都算通过；
    重点是要拦住"从别的电脑拷来的 .venv"。
    """
    from common import RUNTIME_PYTHON, bundled_runtime, venv_health

    if bundled_runtime():
        say(OK, f"使用便携包内置运行时（{RUNTIME_PYTHON}）")
        return

    if not VENV_PYTHON.is_file():
        problems.append("还没建虚拟环境")
        say(FAIL, f"找不到 {VENV_PYTHON}")
        return

    ok, reason = venv_health()
    if not ok:
        problems.append(reason)
        say(FAIL, reason)
        home = read_pyvenv_home()
        if home:
            say(FAIL, f"  pyvenv.cfg 里记录的 Python 是 {home}")
        return

    say(OK, f"虚拟环境可用（{VENV}）")


def check_dependencies() -> None:
    missing = []
    for name in REQUIRED_MODULES:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        problems.append(f"缺少依赖包：{'、'.join(missing)}")
        say(FAIL, f"缺少依赖包：{'、'.join(missing)}")
    else:
        say(OK, f"依赖包齐全（{'、'.join(REQUIRED_MODULES)}）")


def check_optional_packages() -> None:
    """可选包：缺了有降级方案，只提示不拦。"""
    notes = []
    try:
        import pygame  # noqa: F401

        notes.append("pygame")
    except ImportError:
        warnings.append("没装 pygame；没有 mpv 时会退化成 WMP 后端（不能暂停/调音量）")
    if notes:
        say(OK, f"可选播放后端：{'、'.join(notes)}")


def check_node() -> None:
    # 便携包自带 node 时，目标机器不用装 Node.js
    if RUNTIME_NODE.is_file():
        say(OK, f"使用便携包内置 Node.js（{RUNTIME_NODE}）")
    else:
        node = shutil.which("node")
        if not node:
            problems.append("没找到 Node.js")
            say(FAIL, "没找到 node（网易云 API 服务需要它，装一下 Node.js 18+）")
            return

        import subprocess

        version = ""
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            version = subprocess.run(
                [node, "--version"], capture_output=True, text=True, timeout=15
            ).stdout.strip()
        say(OK, f"Node.js {version or '(版本未知)'}")

    modules = ROOT / "netease-api" / "node_modules"
    if modules.is_dir():
        say(OK, "网易云 API 服务端依赖已安装")
    else:
        problems.append("还没装 node_modules")
        say(FAIL, "netease-api\\node_modules 不存在（运行 install.cmd 会装）")


def check_file_layout() -> None:
    # 注意这里【不包含 config.json】：可迁移副本是故意不带它的（里面可能有登录 Cookie），
    # 首次运行时由 load_config() 自动生成。所以它缺失是正常的，不能算问题。
    needed = [
        "danmaku_bot.py",
        "common.py",
        "run.py",
        "webui.py",
        "roomcode.py",
        "netease-api/serve.js",
    ]
    missing = [name for name in needed if not (ROOT / name).exists()]
    if missing:
        problems.append(f"文件不完整：{'、'.join(missing)}")
        say(FAIL, f"缺少文件：{'、'.join(missing)}")
    else:
        say(OK, "项目文件完整")

    tools = ROOT / "tools" / "mpv" / "mpv.exe"
    if tools.is_file():
        say(OK, "mpv 便携版已就位")
    else:
        warnings.append("没有 mpv（可运行 get_mpv.cmd 下载，或退回 pygame/WMP 后端）")


def check_config_readable() -> None:
    path = ROOT / "config.json"
    if not path.is_file():
        # 首次运行时 load_config() 会自动生成默认配置 —— 顺便把这条路径也验了
        try:
            from common import load_config

            load_config()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"config.json 生成失败：{exc}")
            say(FAIL, f"config.json 生成失败：{exc}")
            return
        say(OK, "config.json 不存在，已自动生成默认配置")
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"config.json 读不了：{exc}")
        say(FAIL, f"config.json 解析失败：{exc}")
        return
    if not isinstance(data, dict):
        problems.append("config.json 顶层不是对象")
        say(FAIL, "config.json 格式不对")
    else:
        say(OK, "config.json 可读")


def main() -> int:
    with contextlib.suppress(AttributeError, ValueError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    quiet = "--quiet" in sys.argv or "-q" in sys.argv

    # 先把结果收集起来：一切正常时只输出一行，免得每次启动都刷一屏
    import io
    from contextlib import redirect_stdout

    sink = io.StringIO()
    with redirect_stdout(sink):
        check_python_version()
        check_venv()
        if not problems:  # 环境本身不成立的话，后面的检查没意义
            check_dependencies()
            check_optional_packages()
            check_node()
            check_file_layout()
            check_config_readable()
    report = sink.getvalue()

    if problems:
        if not quiet:
            print("=" * 62)
            print("  启动前环境自检")
            print("=" * 62)
            print(report.rstrip())
            print("-" * 62)
            for item in warnings:
                print(f"{WARN} {item}")
            print()
            print("发现以下问题，无法启动：")
            for item in problems:
                print(f"  · {item}")
            print()
            print("解决办法：双击 install.cmd 重新安装一遍。")
            print("（虚拟环境不能跨电脑拷贝，换了机器必须在本机重建）")
            print("=" * 62)
        else:
            print("[FAIL] 环境有问题：" + "；".join(problems))
            print("       双击 install.cmd 重新安装，或双击 check_env.cmd 看详情。")
        return 1

    if verbose:
        print("=" * 62)
        print("  启动前环境自检")
        print("=" * 62)
        print(report.rstrip())
        for item in warnings:
            print(f"{WARN} {item}")
        print(f"{OK} 环境没问题，可以启动")
        print("=" * 62)
    else:
        py = sys.version.split()[0]
        node = ""
        for line in report.splitlines():
            if line.startswith(f"{OK} Node.js"):
                node = line[len(OK) + 1 :]
        extra = f"，{node}" if node else ""
        print(f"{OK} 环境检查通过（Python {py}{extra}）")
        for item in warnings:
            print(f"{WARN} {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
