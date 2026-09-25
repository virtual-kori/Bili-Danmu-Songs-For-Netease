"""打一个「自带运行时」的便携包：在**全新电脑**上解压即可运行。

和 make_portable.py 的区别（那个需要目标机器先装 Python 和 Node.js）：

    make_portable.py         只带代码 + node_modules + mpv，目标机器要装 Python/Node
    本脚本（--bundle）        额外把 Python 解释器、Node 解释器和全部依赖打包进去

打出来的目录结构：

    B站弹幕点歌机器人便携版/
    ├── run.cmd                     ← 目标机器上双击这个
    ├── runtime/
    │   ├── python/                 Python 3.13 embeddable + 依赖（约 90MB）
    │   └── node/node.exe           Node.js 便携版（约 80MB）
    ├── tools/mpv/mpv.exe           播放器（可选，约 120MB）
    ├── netease-api/node_modules/   网易云 API 服务端依赖
    ├── 使用说明（便携版）.txt
    └── ...项目源码

关于"轻量"：Python 用的是官方 embeddable 包（10.5MB，只有解释器和标准库），
不是完整安装版；Node 只取 node.exe 和 npm，删掉了文档和头文件。
这两项都是运行时必需品，没法再压。

用法：
    .venv\\Scripts\\python.exe make_portable_bundle.py
    .venv\\Scripts\\python.exe make_portable_bundle.py --zip       顺便压成 zip
    .venv\\Scripts\\python.exe make_portable_bundle.py --no-mpv   不带 mpv（省 120MB）
    .venv\\Scripts\\python.exe make_portable_bundle.py --out D:\\x --force
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

# 项目根目录要在 sys.path 里，这样 embeddable 解释器也能 import common
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import VERSION  # noqa: E402
from make_portable import (  # noqa: E402
    EXCLUDE_NAMES,
    REQUIRED,
    _ignore_factory,
    human,
    human_bytes,
)

DEFAULT_NAME = "B站弹幕点歌机器人便携版"

# 运行时下载源。Python 版本必须和本机解释器的 minor 版本一致，
# 否则 pip 装下来的 C 扩展 wheel 是给另一个 ABI 编的，导不进去。
PYTHON_SERIES = "3.13"
PYTHON_EMBED_URL = "https://www.python.org/ftp/python/{ver}/python-{ver}-embed-amd64.zip"
NODE_VERSION = "v22.14.0"
NODE_URL = "https://nodejs.org/dist/{ver}/node-{ver}-win-x64.zip"

# 必须是纯 Python 或已有 win_amd64 wheel 的包，否则装不进 embeddable
REQUIRED_PACKAGES = ("aiohttp", "brotli", "requests", "qrcode", "pillow", "pygame")
NO_DEPS_PACKAGES = ("blivedm",)

# 跑起来必须 import 得动的模块
SMOKE_MODULES = ("blivedm", "aiohttp", "brotli", "requests", "qrcode", "PIL", "pygame")


def log(message: str = "") -> None:
    print(message, flush=True)


def step(index: int, total: int, message: str) -> None:
    log(f"[{index}/{total}] {message}")


def download(url: str, target: Path) -> None:
    """下载到指定文件，带一个简单的进度提示。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp, open(target, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total and done % (4 * 1024 * 1024) < 256 * 1024:
                log(f"      {done * 100 // total}%（{human_bytes(done)} / {human_bytes(total)}）")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw
    )


# ------------------------------------------------------------------ 运行时

def resolve_python_version(log_) -> str:
    """挑一个和本机 minor 版本一致的 Python embeddable 版本号。"""
    local = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if local.startswith(PYTHON_SERIES):
        return local
    # 本机不是 3.13 就退回到该系列已知可用的版本
    log(f"  本机 Python 是 {local}，与打包用的 {PYTHON_SERIES} 系列不一致，"
        f"将使用 {PYTHON_SERIES} 的 embeddable 包")
    return f"{PYTHON_SERIES}.0"


def build_python(target: Path, cache: Path) -> bool:
    version = resolve_python_version(log)
    url = PYTHON_EMBED_URL.format(ver=version)
    archive = cache / f"python-{version}-embed-amd64.zip"

    if not archive.is_file():
        log(f"  下载 Python {version} embeddable（约 10.5MB）…")
        try:
            download(url, archive)
        except Exception as exc:  # noqa: BLE001
            log(f"  [!] 下载失败：{exc}")
            log(f"      可以手动下载后放到 {archive}")
            return False
    else:
        log(f"  使用已缓存的 {archive.name}")

    pydir = target / "runtime" / "python"
    pydir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(pydir)

    # 让解释器能 import 依赖和项目自己的模块。
    #
    # 关键点：只要存在 ._pth 文件，Python 就进入 isolated 模式，**会完全忽略
    # PYTHONPATH**（实测如此）。所以项目根目录必须在这里写成相对路径，
    # 不能用环境变量。路径按 python.exe 所在目录解析：runtime\python\..\.. = 包根目录。
    pth_files = list(pydir.glob("python*._pth"))
    if not pth_files:
        log("  [!] 没找到 python*._pth，无法配置模块搜索路径")
        return False
    pth_files[0].write_text(
        f"python{sys.version_info.major}{sys.version_info.minor}.zip\n"
        ".\n"
        "Lib\\site-packages\n"
        "..\\..\n"
        "import site\n",
        encoding="utf-8",
    )
    log(f"  已配置 {pth_files[0].name}（含项目根目录相对路径）")
    return True


def install_python_deps(target: Path) -> bool:
    """把依赖装进便携包的 site-packages。

    用本机 pip 的 --target 装，而不是让 embeddable 自带的 pip 装 ——
    embeddable 包默认没有 pip，省掉一次 get-pip 引导，也少一层出错可能。
    """
    sp = target / "runtime" / "python" / "Lib" / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)

    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-warn-script-location", "--target", str(sp)]

    log(f"  安装：{' '.join(REQUIRED_PACKAGES)}")
    got = run([*base, *REQUIRED_PACKAGES])
    if got.returncode != 0:
        log(f"  [!] 安装失败：{(got.stderr or got.stdout or '')[-800:]}")
        return False

    log(f"  安装：{' '.join(NO_DEPS_PACKAGES)}（--no-deps，绕开写死的 brotli）")
    got = run([*base, "--no-deps", *NO_DEPS_PACKAGES])
    if got.returncode != 0:
        log(f"  [!] 安装失败：{(got.stderr or got.stdout or '')[-800:]}")
        return False

    # pip 会留下 dist-info 和缓存，清掉省体积
    for junk in sp.glob("**/__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)
    return True


def build_node(target: Path, cache: Path) -> bool:
    """下载 Node 便携版，只保留 node.exe / npm / npx 等必需文件。"""
    url = NODE_URL.format(ver=NODE_VERSION)
    archive = cache / f"node-{NODE_VERSION}-win-x64.zip"

    if not archive.is_file():
        log(f"  下载 Node.js {NODE_VERSION}（约 33MB）…")
        try:
            download(url, archive)
        except Exception as exc:  # noqa: BLE001
            log(f"  [!] 下载失败：{exc}")
            return False
    else:
        log(f"  使用已缓存的 {archive.name}")

    node_dir = target / "runtime" / "node"
    node_dir.mkdir(parents=True, exist_ok=True)

    top = f"node-{NODE_VERSION}-win-x64/"
    # 只要运行服务用得上的东西：node.exe、npm、npx、以及 npm 自己的运行时
    keep_prefixes = ("node.exe", "npm", "npx", "node_modules/npm", "node_modules/.bin")
    keep_suffixes = (".exe", ".cmd", ".ps1", ".json", ".js", ".icns")

    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.startswith(top):
                continue
            rel = info.filename[len(top):]
            if not rel:
                continue
            if not rel.startswith(keep_prefixes):
                continue
            if not rel.endswith(keep_suffixes):
                continue
            out = node_dir / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)

    if not (node_dir / "node.exe").is_file():
        log("  [!] 没提取到 node.exe")
        return False
    log(f"  已提取 node.exe 等文件（{human(node_dir)}）")
    return True


# ------------------------------------------------------------------ 校验

def verify_python(target: Path) -> bool:
    """用便携包自己的解释器导一遍依赖和项目模块 —— 这是最关键的一步。"""
    py = target / "runtime" / "python" / "python.exe"
    if not py.is_file():
        log("  [!] 便携包里没有 python.exe")
        return False

    # PYTHONPATH 对 isolated 模式的解释器无效（见 build_python 里的说明），
    # 项目根目录是靠 ._pth 里的相对路径生效的。这里只设编码相关变量。
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        # 自检时不要弹 pygame 的欢迎语，免得刷屏
        "PYGAME_HIDE_SUPPORT_PROMPT": "1",
    }
    code = (
        "import " + ", ".join(SMOKE_MODULES) + "\n"
        "import lyrics, common, netease_api, webui, lyric_overlay, player, danmaku_bot\n"
        "from common import bundled_runtime\n"
        "assert bundled_runtime(), 'bundled_runtime() 应为 True'\n"
        "print('OK', len(" + repr(SMOKE_MODULES) + "), 'deps + project modules')\n"
    )
    got = subprocess.run(
        [str(py), "-c", code], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env={**os.environ, **env},
    )
    if got.returncode != 0:
        log(f"  [!] 便携包内解释器跑不起来：{(got.stderr or '')[-1200:]}")
        return False
    log(f"  {got.stdout.strip()}")
    return True


def verify_node(target: Path) -> bool:
    node = target / "runtime" / "node" / "node.exe"
    if not node.is_file():
        log("  [!] 便携包里没有 node.exe")
        return False
    got = run([str(node), "--version"])
    if got.returncode != 0:
        log(f"  [!] node 跑不起来：{(got.stderr or '')[-400:]}")
        return False
    log(f"  Node {got.stdout.strip()}")
    return True


# ------------------------------------------------------------------ 说明文件

PORTABLE_README = """\
{name}
{line}
版本 {version}

这是一份「自带运行时」的便携包：目标电脑**不需要**装 Python，也**不需要**装 Node.js。

怎么用（只有两步）
{line}

  1. 把整个文件夹解压到任意位置（路径有中文、空格都没关系）

  2. 双击  run.cmd

     首次运行会问你要直播间号 —— 直接粘直播间网址也行，例如
     https://live.bilibili.com/1234567

     想放会员歌曲：先双击 login_netease.cmd，用手机网易云扫码。
     不小心关掉了浏览器页面：双击 open_panel.cmd。

出问题
{line}

    双击 check_env.cmd        自检，会指出哪里不对
    双击 _check_env.cmd       快速检查（启动脚本内部用的就是它）

    控制台窗口要一直开着。关掉窗口 = 停止运行。

歌词显示在直播画面上（OBS）
{line}

    控制面板里有一栏「歌词叠加层」，点「复制地址」，然后：
    OBS → 来源 → 加号 → 浏览器 → 把地址粘进去，宽高设成画布大小。

    歌词是透明背景，不用做抠像。字体、颜色、位置都能在面板上直接改。

这个文件夹里都是什么
{line}

    run.cmd                 启动（双击这个）
    runtime\\python\\        Python 解释器和全部依赖（包内自带，别删）
    runtime\\node\\          Node.js（网易云 API 服务要用，别删）
    tools\\mpv\\             播放器（可选；没有会自动退回 pygame）
    netease-api\\            网易云 API 服务的代码和依赖
    config.json             你的配置和登录状态（首次运行自动生成）
    README.md               完整文档：指令表、配置项、排错

    注意：不要单独把 runtime 目录拷到别处，它和 run.cmd 是配套的。

{line}
详细说明见 README.md
"""


def write_readme(target: Path) -> None:
    text = PORTABLE_README.format(name=DEFAULT_NAME, version=VERSION, line="=" * 64)
    (target / "使用说明（便携版）.txt").write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ 主流程

def main() -> int:
    parser = argparse.ArgumentParser(description="打自带运行时的便携包")
    parser.add_argument("--out", help="输出目录，默认项目旁边")
    parser.add_argument("--name", default=DEFAULT_NAME, help=f"文件夹名，默认 {DEFAULT_NAME}")
    parser.add_argument("--zip", action="store_true", help="顺便压成 zip")
    parser.add_argument("--no-mpv", action="store_true", help="不带 mpv（省约 120MB）")
    parser.add_argument("--no-node-modules", action="store_true", help="不带 node_modules")
    parser.add_argument("--with-config", action="store_true", help="连 config.json 一起带（含登录凭据，慎用）")
    parser.add_argument("--force", action="store_true", help="目标已存在时先删掉")
    parser.add_argument("--skip-verify", action="store_true", help="跳过打包后的自检")
    args = parser.parse_args()

    with contextlib.suppress(AttributeError, ValueError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    out_dir = Path(args.out).expanduser().resolve() if args.out else ROOT.parent
    target = out_dir / args.name
    cache = ROOT / ".tmp" / "runtime-cache"

    TOTAL = 8
    log("=" * 64)
    log("  打包便携版（自带 Python + Node，目标电脑免安装）")
    log("=" * 64)
    log(f"  版本：  {VERSION}")
    log(f"  源目录：{ROOT}")
    log(f"  目标：  {target}")
    log()

    if target.exists():
        if not args.force:
            log(f"[!] 目标已存在：{target}")
            log("    加 --force 可以先删掉再打包。")
            return 1
        log("  目标已存在，按 --force 删除重建…")
        shutil.rmtree(target, ignore_errors=True)

    # ---------------------------------------------------------- 1. 复制源码
    step(1, TOTAL, "复制项目文件…")
    with_node_modules = not args.no_node_modules

    def ignore(directory: str, names: list[str]) -> set[str]:
        # 先走 make_portable 的规则（.venv/.git/缓存等一律不带）
        skipped = _ignore_factory(args.with_config, with_node_modules)(directory, names)
        # 便携包自己生成 runtime，源目录里的 runtime 是上一次的原型，不该带过去
        if Path(directory).resolve() == ROOT and "runtime" in names:
            skipped.add("runtime")
        # 不带 mpv 时把 tools/mpv 剔掉
        if args.no_mpv and Path(directory).resolve() == ROOT / "tools" and "mpv" in names:
            skipped.add("mpv")
        return skipped

    shutil.copytree(ROOT, target, ignore=ignore, dirs_exist_ok=False)
    log(f"  完成（{human(target)}）")

    # ---------------------------------------------------------- 2. Python 运行时
    step(2, TOTAL, "准备 Python 运行时（embeddable + 依赖）…")
    if not build_python(target, cache):
        return 1
    if not install_python_deps(target):
        return 1
    log(f"  Python 运行时就绪（{human(target / 'runtime' / 'python')}）")

    # ---------------------------------------------------------- 3. Node 运行时
    step(3, TOTAL, "准备 Node.js 运行时…")
    if not build_node(target, cache):
        return 1

    # ---------------------------------------------------------- 4. 播放器 / 依赖检查
    step(4, TOTAL, "检查播放器与 API 服务依赖…")
    has_mpv = (target / "tools" / "mpv" / "mpv.exe").is_file()
    has_modules = (target / "netease-api" / "node_modules").is_dir()
    log(f"  mpv：{'已带' if has_mpv else '未带（会自动退回 pygame 后端）'}")
    log(f"  netease-api 依赖：{'已带' if has_modules else '未带（首次运行需要联网 npm install）'}")

    # ---------------------------------------------------------- 5. 说明文件
    step(5, TOTAL, "生成便携版说明…")
    write_readme(target)
    log("  已生成 使用说明（便携版）.txt")

    # ---------------------------------------------------------- 6. 完整性
    step(6, TOTAL, "校验文件完整性…")
    needed = list(REQUIRED) + [
        "runtime/python/python.exe",
        "runtime/node/node.exe",
        "使用说明（便携版）.txt",
    ]
    missing = [name for name in needed if not (target / name).exists()]
    if missing:
        log("[!] 便携包不完整，缺少：")
        for name in missing:
            log(f"    {name}")
        return 1
    log(f"  必需文件齐全（共 {len(needed)} 项）")

    # ---------------------------------------------------------- 7. 实跑自检
    step(7, TOTAL, "用便携包自己的运行时实跑自检…")
    if args.skip_verify:
        log("  已按 --skip-verify 跳过")
    else:
        if not verify_python(target):
            return 1
        if not verify_node(target):
            return 1

    # ---------------------------------------------------------- 8. 打包
    step(8, TOTAL, "收尾…")
    total_size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    log(f"  便携包体积：{human_bytes(total_size)}")

    produced: list[tuple[str, int]] = []
    if args.zip:
        log("  正在压 zip（这一步比较久）…")
        archive = shutil.make_archive(str(target), "zip", root_dir=out_dir, base_dir=args.name)
        size = Path(archive).stat().st_size
        produced.append((archive, size))
        log(f"    完成：{Path(archive).name}（{human_bytes(size)}）")

    log()
    log("=" * 64)
    log(f"  便携包已生成：{target}")
    if produced:
        for path, size in produced:
            log(f"    {Path(path).name}  （{human_bytes(size)}）")
    log()
    log("  目标电脑上只需要：")
    log("    1. 解压（zip 或直接拷文件夹）")
    log("    2. 双击 run.cmd")
    log()
    log("  Python 和 Node.js 都已打包在内，目标机器不用安装任何东西。")
    log("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
