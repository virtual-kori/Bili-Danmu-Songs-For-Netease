"""把 mpv 便携版下载到 tools/mpv/（不需要管理员权限、不装到系统里）。

为什么推荐 mpv：它能直接流播网络音频，起播几乎瞬间，而且支持暂停/继续/实时调音量/
秒切歌。相比之下 pygame 后端要先整首下载再播，每首歌开头会等几秒。

用法：
    python get_mpv.py             下载最新的 x86_64 标准版
    python get_mpv.py --v3        下载 v3 版（需要较新的 CPU，起播更快一点）
    python get_mpv.py --list      只看有哪些版本，不下载
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from common import ROOT, ensure_console_utf8, human_bytes, setup_logging

TOOLS_DIR = ROOT / "tools"
MPV_DIR = TOOLS_DIR / "mpv"

# 两个活跃维护的第三方 mpv Windows 构建
REPOS = ("shinchiro/mpv-winbuild-cmake", "zhongfly/mpv-winbuild")
GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"


def _pick_asset(assets: list[dict], v3: bool = False) -> dict | None:
    """从发布产物里挑出 x86_64 的标准 mpv 包（排除 dev/debug/ffmpeg/aarch64/i686）。"""
    candidates = []
    for asset in assets:
        name: str = asset.get("name", "")
        if not name.endswith(".7z"):
            continue
        if not name.startswith("mpv-"):
            continue
        if any(bad in name for bad in ("dev-", "debug-", "aarch64", "i686")):
            continue
        if "x86_64" not in name:
            continue
        is_v3 = "x86_64-v3" in name
        if is_v3 != v3:
            continue
        candidates.append(asset)
    return candidates[0] if candidates else None


def _fetch_release(repo: str) -> dict:
    import requests

    resp = requests.get(
        GITHUB_API.format(repo=repo),
        timeout=30,
        headers={"User-Agent": "qdgj-mpv-downloader", "Accept": "application/vnd.github+json"},
    )
    resp.raise_for_status()
    return resp.json()


def _download(url: str, target: Path, log) -> bool:
    import requests

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last_report = 0
            with open(target, "wb") as handle:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if total and done - last_report > total // 10:
                        last_report = done
                        log.info("  下载中… %d%%（%s / %s）", done * 100 // total, human_bytes(done), human_bytes(total))
    except Exception as exc:  # noqa: BLE001
        log.error("下载失败：%s", exc)
        target.unlink(missing_ok=True)
        return False
    return True


SEVEN_ZR_URL = "https://www.7-zip.org/a/7zr.exe"
SEVEN_ZR = TOOLS_DIR / "7zr.exe"


def _ensure_7zr(log) -> str | None:
    """拿到一个能解 7z 的命令行工具。

    这些 mpv 构建用了 BCJ2/LZMA 过滤器，py7zr 和 Windows 自带的 bsdtar 都搞不定，
    所以优先用 7-Zip 官方独立版 7zr.exe（600KB，免安装，不下系统目录）。
    """
    import subprocess

    # 1) 系统里已经装了 7-Zip 就直接用
    for name in ("7z", "7za", "7zz"):
        found = shutil.which(name)
        if found:
            log.info("  使用系统已安装的 %s", found)
            return found

    # 2) 项目里已有的 7zr.exe
    if SEVEN_ZR.is_file():
        return str(SEVEN_ZR)

    # 3) 下载 7zr.exe
    log.info("  下载解压工具 7zr.exe（约 600KB）…")
    try:
        import requests

        resp = requests.get(SEVEN_ZR_URL, timeout=60)
        resp.raise_for_status()
        SEVEN_ZR.parent.mkdir(parents=True, exist_ok=True)
        SEVEN_ZR.write_bytes(resp.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("  7zr.exe 下载失败：%s", exc)
        return None

    try:
        subprocess.run([str(SEVEN_ZR)], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("  7zr.exe 不能运行：%s", exc)
        return None
    return str(SEVEN_ZR)


def _extract(archive: Path, dest: Path, log) -> bool:
    import subprocess

    dest.mkdir(parents=True, exist_ok=True)

    seven = _ensure_7zr(log)
    if seven:
        log.info("  解压到 %s …", dest)
        try:
            result = subprocess.run(
                [seven, "x", str(archive), f"-o{dest}", "-y"],
                capture_output=True,
                text=True,
                timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("  7zr 解压失败：%s", exc)
        else:
            if result.returncode == 0 and (dest / "mpv.exe").is_file():
                return True
            tail = (result.stdout or result.stderr or "").strip().splitlines()[-3:]
            log.warning("  7zr 解压未成功：%s", " / ".join(tail))

    # 后备：py7zr（对不含 BCJ2 的包有效）
    try:
        import py7zr
    except ImportError:
        log.error("  没有可用的 7z 解压工具。")
        log.error("  可以手动下载 7-Zip 解压 %s 到 %s", archive, dest)
        return False

    log.info("  改用 py7zr 解压…")
    try:
        with py7zr.SevenZipFile(archive, mode="r") as handle:
            handle.extractall(path=dest)
    except Exception as exc:  # noqa: BLE001
        log.error("  py7zr 解压失败：%s", exc)
        return False
    return True


def _verify(log) -> bool:
    exe = MPV_DIR / "mpv.exe"
    if not exe.is_file():
        log.error("解压后没找到 %s", exe)
        return False

    import subprocess

    try:
        result = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("mpv.exe 跑不起来：%s", exc)
        return False

    first_line = (result.stdout or result.stderr or "").splitlines()
    log.info("  mpv 版本：%s", first_line[0] if first_line else "(无输出)")
    return True


def main() -> int:
    ensure_console_utf8()
    log = setup_logging()

    parser = argparse.ArgumentParser(description="下载 mpv 便携版到 tools/mpv/")
    parser.add_argument("--v3", action="store_true", help="下载 x86_64-v3 版（需要较新 CPU）")
    parser.add_argument("--list", action="store_true", help="只列出可用版本")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = parser.parse_args()

    if (MPV_DIR / "mpv.exe").is_file() and not args.force and not args.list:
        log.info("已经装好了：%s", MPV_DIR / "mpv.exe")
        log.info("要重新下载请加 --force")
        _verify(log)
        return 0

    log.info("正在查询 mpv 最新版本…")
    release = None
    used_repo = ""
    for repo in REPOS:
        try:
            release = _fetch_release(repo)
            used_repo = repo
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("查询 %s 失败：%s", repo, exc)

    if not release:
        log.error("两个镜像都查不到，请检查网络")
        return 1

    tag = release.get("tag_name", "?")
    assets = release.get("assets", [])
    log.info("来源：%s  版本：%s（共 %d 个文件）", used_repo, tag, len(assets))

    if args.list:
        for asset in assets:
            print(f"  {asset['name']:60} {asset['size'] / 1024 / 1024:6.1f}MB")
        return 0

    asset = _pick_asset(assets, v3=args.v3)
    if not asset:
        log.error("没找到合适的 x86_64 包，用 --list 看看有哪些")
        return 1

    log.info("选中：%s（%s）", asset["name"], human_bytes(asset["size"]))

    download_dir = TOOLS_DIR / "_download"
    archive = download_dir / asset["name"]
    if archive.is_file() and not args.force:
        log.info("已有下载缓存，跳过下载")
    else:
        if not _download(asset["browser_download_url"], archive, log):
            return 1

    if MPV_DIR.exists() and args.force:
        shutil.rmtree(MPV_DIR, ignore_errors=True)

    if not _extract(archive, MPV_DIR, log):
        return 1

    if not _verify(log):
        return 1

    try:
        archive.unlink(missing_ok=True)
        download_dir.rmdir()
    except OSError:
        pass

    log.info("")
    log.info("完成！mpv 已就位于 %s", MPV_DIR)
    log.info("机器人在自动探测后端时会优先选它，无需改配置。")
    log.info("验证：.venv\\Scripts\\python.exe danmaku_bot.py --list-backends")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
