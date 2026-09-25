"""把项目打包成一个干净的、可以搬到别的 Windows 电脑上跑的副本。

关键点：**虚拟环境（.venv）不能跨电脑**。
`.venv/pyvenv.cfg` 里记着创建它的那台机器上 Python 的绝对路径，
拷到别的电脑后那个路径不存在，.venv 就废了。所以打包时一律排除，
让目标机器跑一次 install.cmd 在本机重建。

可以一起带走的东西（都是可移植的）：
    netease-api/node_modules   纯 JS，带走就不用再 npm install（省几分钟）
    tools/                     mpv 便携版 + 7zr，都是免安装的

用法：
    .venv\\Scripts\\python.exe make_portable.py               生成文件夹
    .venv\\Scripts\\python.exe make_portable.py --zip         顺便压成 zip
    .venv\\Scripts\\python.exe make_portable.py --out D:\\x    指定输出位置
    .venv\\Scripts\\python.exe make_portable.py --no-node-modules
    .venv\\Scripts\\python.exe make_portable.py --with-config  连 config.json 一起带（含登录 Cookie，慎用）
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 这些一律不带：本机专用的环境、缓存、凭据、开发脚手架
EXCLUDE_NAMES = {
    ".venv",
    ".npm-cache",
    ".pip-cache",
    ".ruff_cache",
    ".tmp",
    ".dev",
    "cache",
    "__pycache__",
    ".git",
    "login_qrcode.png",
}

# 打包出来的文件夹名字
DEFAULT_NAME = "qdgj-portable"

# 这些必须存在，缺了说明打包出来的东西是残的
# 注意 config.json 不在这里：默认故意不带它（里面可能有登录 Cookie），首次运行会自动生成
REQUIRED = (
    "install.cmd",
    "run.cmd",
    "run.bat",
    "_check_env.cmd",
    "bootstrap.py",
    "danmaku_bot.py",
    "common.py",
    "使用说明.txt",
    "netease-api/serve.js",
    "netease-api/package.json",
)


def _ignore_factory(with_config: bool, with_node_modules: bool):
    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped: set[str] = set()
        for name in names:
            if name in EXCLUDE_NAMES or name.endswith((".pyc", ".pyo")):
                skipped.add(name)
        if not with_config and Path(directory).resolve() == ROOT:
            skipped.add("config.json")
        if not with_node_modules and Path(directory).name == "netease-api":
            skipped.add("node_modules")
        return skipped

    return ignore


def human(path: Path) -> str:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return f"{total / 1024 / 1024:.1f} MB"


def human_bytes(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB"


# ------------------------------------------------------------------ 自解压相关

SEVEN_ZR = ROOT / "tools" / "7zr.exe"
SEVEN_SFX = ROOT / "tools" / "7z.sfx"
ZIP_INSTALLER_VERSIONS = ("2603", "2501", "2408")


def _download(url: str, target: Path, log=print) -> bool:
    import requests

    try:
        resp = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log(f"    下载失败：{exc}")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(resp.content)
    return True


def ensure_tools(log=print) -> bool:
    """准备好 7zr.exe 和 7z.sfx（自解压外壳）。

    7zr.exe 我们本来就用来解压 mpv；7z.sfx 是自解压模块，
    z 官方只把它放在安装包里，所以先下安装包再用 7zr 从里面掏出来。
    """
    if not SEVEN_ZR.is_file():
        log("  正在获取 7zr.exe …")
        if not _download("https://www.7-zip.org/a/7zr.exe", SEVEN_ZR, log):
            return False

    if SEVEN_SFX.is_file():
        return True

    log("  正在获取自解压模块 7z.sfx（从 7-Zip 安装包里提取）…")
    work = ROOT / ".tmp" / "sfxsrc"
    work.mkdir(parents=True, exist_ok=True)

    installer = None
    for version in ZIP_INSTALLER_VERSIONS:
        candidate = work / f"7z{version}-x64.exe"
        if not candidate.is_file():
            url = f"https://www.7-zip.org/a/7z{version}-x64.exe"
            log(f"    下载 {url}")
            if not _download(url, candidate, log):
                continue
        installer = candidate
        break

    if installer is None:
        log("    [!] 没能下载到 7-Zip 安装包，无法制作自解压 exe")
        return False

    import subprocess

    out = work / "out"
    result = subprocess.run(
        [str(SEVEN_ZR), "x", str(installer), f"-o{out}", "-y"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    found = next(out.rglob("7z.sfx"), None)
    if result.returncode != 0 or found is None:
        log("    [!] 从安装包里没找到 7z.sfx")
        log((result.stdout or result.stderr or "")[-300:])
        return False

    shutil.copy2(found, SEVEN_SFX)
    log(f"    已就位：{SEVEN_SFX}（{human_bytes(SEVEN_SFX.stat().st_size)}）")
    return True


def build_7z(source_dir: Path, archive: Path, log=print) -> bool:
    """把 source_dir 压成 .7z，压缩包内带一层同名顶层目录。"""
    import subprocess

    if archive.exists():
        archive.unlink()

    log(f"  正在压缩 .7z（{human(source_dir)}）…")
    started = time.time()
    result = subprocess.run(
        [str(SEVEN_ZR), "a", str(archive), source_dir.name, "-mx=7", "-bso0", "-bsp0"],
        cwd=str(source_dir.parent),
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0 or not archive.is_file():
        log(f"    [!] 压缩失败：{(result.stdout or result.stderr or '')[-300:]}")
        return False
    log(f"    完成，用时 {time.time() - started:.1f} 秒，{human_bytes(archive.stat().st_size)}")
    return True


def build_sfx(payload_7z: Path, target_exe: Path, log=print) -> bool:
    """把 7z.sfx 外壳和 .7z 数据拼成一个自解压 exe。

    直接在 Python 里按字节拼接，比 cmd 的 `copy /b` 稳（不受中文路径/编码影响）。
    """
    if not SEVEN_SFX.is_file():
        log("    [!] 缺少 7z.sfx")
        return False

    log("  正在生成自解压 exe …")
    with open(target_exe, "wb") as out:
        with open(SEVEN_SFX, "rb") as shell:
            shutil.copyfileobj(shell, out)
        with open(payload_7z, "rb") as payload:
            shutil.copyfileobj(payload, out)
    log(f"    完成：{target_exe.name}（{human_bytes(target_exe.stat().st_size)}）")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="打包成可迁移副本")
    parser.add_argument("--out", help="输出目录，默认在项目旁边（也就是桌面）")
    parser.add_argument("--name", default=DEFAULT_NAME, help=f"文件夹名，默认 {DEFAULT_NAME}")
    parser.add_argument("--zip", action="store_true", help="顺便压成 zip")
    parser.add_argument("--sfx", action="store_true", help="制作自解压 exe（双击即解压）")
    parser.add_argument("--no-folder", action="store_true", help="打完包后删掉中间文件夹，只留压缩包")
    parser.add_argument("--force", action="store_true", help="目标已存在时先删掉")
    parser.add_argument("--no-node-modules", action="store_true", help="不带 node_modules（目标机器需能上网）")
    parser.add_argument("--with-config", action="store_true", help="连 config.json 一起带（含登录 Cookie）")
    args = parser.parse_args()

    with contextlib.suppress(AttributeError, ValueError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    out_dir = Path(args.out).expanduser().resolve() if args.out else ROOT.parent
    target = out_dir / args.name

    print("=" * 62)
    print("  打包可迁移副本")
    print("=" * 62)
    print(f"  源目录：{ROOT}")
    print(f"  目标：  {target}")
    print()

    if target.exists():
        if not args.force:
            print(f"[!] 目标已存在：{target}")
            print("    加 --force 可以先删掉再打包。")
            return 1
        print("  目标已存在，按 --force 删除重建…")
        shutil.rmtree(target, ignore_errors=True)

    with_node_modules = not args.no_node_modules

    print("  复制文件中…")
    started = time.time()
    shutil.copytree(
        ROOT,
        target,
        ignore=_ignore_factory(args.with_config, with_node_modules),
        dirs_exist_ok=False,
    )
    print(f"  完成，用时 {time.time() - started:.1f} 秒")

    # 检查完整性
    print()
    needed = list(REQUIRED) + (["config.json"] if args.with_config else [])
    missing = [name for name in needed if not (target / name).exists()]
    if missing:
        print("[!] 打包结果不完整，缺少：")
        for name in missing:
            print(f"    {name}")
        return 1

    if not args.with_config:
        print("  说明：没带 config.json（首次运行时自动生成默认配置）")

    print(f"  体积：{human(target)}")
    if with_node_modules:
        print("  已带 node_modules，目标机器不用再 npm install")
    else:
        print("  没带 node_modules，目标机器需要能上网跑 npm install")

    produced: list[tuple[str, int]] = []

    # ------------------------------------------------------------ zip
    if args.zip:
        print()
        print("  正在压 zip …")
        started = time.time()
        archive = shutil.make_archive(str(target), "zip", root_dir=out_dir, base_dir=args.name)
        size = Path(archive).stat().st_size
        produced.append((archive, size))
        print(f"    完成，用时 {time.time() - started:.1f} 秒，{human_bytes(size)}")

    # ------------------------------------------------------------ 自解压 exe
    if args.sfx:
        print()
        print("  制作自解压 exe …")
        if not ensure_tools():
            print("    [!] 自解压外壳没准备好，跳过。")
        else:
            payload = ROOT / ".tmp" / f"{args.name}.7z"
            payload.parent.mkdir(parents=True, exist_ok=True)
            if build_7z(target, payload):
                exe = out_dir / f"{args.name}.exe"
                if build_sfx(payload, exe):
                    produced.append((str(exe), exe.stat().st_size))
                payload.unlink(missing_ok=True)

    # ------------------------------------------------------------ 收尾
    if args.no_folder and (args.zip or args.sfx):
        shutil.rmtree(target, ignore_errors=True)
        print()
        print(f"  已按 --no-folder 删掉中间文件夹：{target}")

    print()
    print("=" * 62)
    if produced:
        print("  生成的文件：")
        for path, size in produced:
            print(f"    {Path(path).name}  （{human_bytes(size)}）")
        print(f"    位置：{out_dir}")
        print()
    print("  在目标电脑上：")
    print("    1. 装好 Python 3.9+ 和 Node.js 18+")
    print("    2. 解压（自解压 exe 双击即可）到任意目录，路径有中文/空格都没关系")
    print("    3. 双击 install.cmd（会在这台机器上重建虚拟环境）")
    print("    4. 双击 run.cmd 开跑")
    print()
    print("  注意：虚拟环境不能跨电脑，所以目标机器必须跑一次 install.cmd。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
