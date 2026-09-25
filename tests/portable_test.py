"""可移植性检查：保证这套东西能整个搬到别的 Windows 电脑上跑。

主要盯三件最容易出问题的事：
  1. 源码里有没有写死本机的绝对路径（换台机器就找不到文件）
  2. .venv 是不是从别的电脑拷过来的（虚拟环境不可移植）
  3. 打包/启动要用的文件是不是都在

用法：
    .venv\\Scripts\\python.exe tests\\portable_test.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import venv_health  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    print(f"{'[通过]' if ok else '[失败]'} {label}" + (f"  —— {detail}" if detail else ""))
    return ok


# 这些目录里的东西是第三方/生成物，不需要查
SKIP_DIRS = {".venv", "node_modules", ".npm-cache", ".pip-cache", ".ruff_cache", ".tmp", ".dev", "cache", "tools", "__pycache__"}

# 会被拷到别的机器上执行的源码/脚本
SCAN_SUFFIXES = {".py", ".cmd", ".bat", ".js", ".json", ".toml", ".md", ".npmrc"}

# 本机绝对路径的特征
ABS_PATTERNS = [
    (re.compile(r"C:\\Users\\", re.I), r"C:\Users\ 开头的绝对路径"),
    (re.compile(r"C:/Users/", re.I), "C:/Users/ 开头的绝对路径"),
    (re.compile(r"DESKTOP-[A-Z0-9]+", re.I), "本机计算机名"),
]

# 这些地方出现系统路径是合理的（找系统装的 mpv/ffmpeg 的兜底路径）
ALLOW_IN = {"player.py"}
SYSTEM_PATH_OK = re.compile(r"C:[/\\]Program Files|C:[/\\]ffmpeg")


def iter_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        # 这个文件本身要写着"C:\Users\"这类特征串才能做检测，别自己举报自己
        if rel.as_posix() == "tests/portable_test.py":
            continue
        if path.suffix.lower() not in SCAN_SUFFIXES and path.name != ".npmrc":
            continue
        yield path


def test_no_absolute_paths() -> None:
    print("\n=== 1. 没有写死的本机绝对路径 ===")
    offenders: list[str] = []
    for path in iter_files():
        rel = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, label in ABS_PATTERNS:
                if not pattern.search(line):
                    continue
                if rel in ALLOW_IN and SYSTEM_PATH_OK.search(line):
                    continue
                offenders.append(f"{rel}:{lineno} 含{label} -> {line.strip()[:70]}")

    check(
        "源码/脚本里没有本机绝对路径",
        not offenders,
        "；".join(offenders[:4]) if offenders else f"扫了 {len(list(iter_files()))} 个文件",
    )


def test_npmrc_portable() -> None:
    print("\n=== 2. npm 配置可移植 ===")
    npmrc = ROOT / ".npmrc"
    if not npmrc.is_file():
        check(".npmrc 不存在（用默认值，也没问题）", True)
        return
    text = npmrc.read_text(encoding="utf-8", errors="replace")
    has_abs = bool(re.search(r"C:\\", text) or re.search(r"C:/", text))
    check(".npmrc 里没有写死盘符路径", not has_abs, text.replace("\n", " | ").strip())


def test_venv_portable() -> None:
    print("\n=== 3. 虚拟环境可移植性 ===")
    ok, reason = venv_health()
    check("当前虚拟环境在本机可用", ok, reason)
    if ok:
        cfg = ROOT / ".venv" / "pyvenv.cfg"
        check(".venv/pyvenv.cfg 存在（记录了基础 Python 位置）", cfg.is_file())


def test_stale_venv_detection() -> None:
    """模拟"虚拟环境是从别的电脑拷过来的"，确认能被识别出来。

    pyvenv.cfg 只在解释器启动时读一次，运行中临时改掉是安全的。
    """
    print("\n=== 4b. 跨电脑虚拟环境的识别 ===")
    cfg = ROOT / ".venv" / "pyvenv.cfg"
    if not cfg.is_file():
        check("能识别出跨电脑拷贝的虚拟环境", True, "（没有 pyvenv.cfg，跳过）")
        return

    original = cfg.read_text(encoding="utf-8", errors="replace")
    fake = "\n".join(
        "home = C:\\Users\\SomeOtherPC\\AppData\\Local\\Programs\\Python\\Python313"
        if line.lower().startswith("home")
        else line
        for line in original.splitlines()
    )
    try:
        cfg.write_text(fake, encoding="utf-8")
        ok, reason = venv_health()
        check(
            "能识别出跨电脑拷贝的虚拟环境",
            not ok and "别的电脑" in reason,
            reason if not ok else "居然认为是可用的",
        )
    finally:
        cfg.write_text(original, encoding="utf-8")

    ok_after, reason_after = venv_health()
    check("还原后恢复正常", ok_after, reason_after)


def test_required_files() -> None:
    print("\n=== 4. 打包/启动需要的文件齐全 ===")
    needed = [
        "install.cmd",
        "run.cmd",
        "run.bat",
        "_check_env.cmd",
        "bootstrap.py",
        "make_portable.py",
        "make_portable.cmd",
        "check_env.cmd",
        "check_env.py",
        "get_mpv.cmd",
        "login_netease.cmd",
        "open_panel.cmd",
        "open_panel.py",
        "start_bot.cmd",
        "start_netease_api.cmd",
        "danmaku_bot.py",
        "common.py",
        "netease_api.py",
        "player.py",
        "song_queue.py",
        "roomcode.py",
        "qr_login.py",
        "get_mpv.py",
        "webui.py",
        "run.py",
        "使用说明.txt",
        "netease-api/serve.js",
        "netease-api/package.json",
    ]
    # config.json 不在必查列表里：可迁移副本故意不带它，首次运行会自动生成
    missing = [name for name in needed if not (ROOT / name).exists()]
    check("必需文件都在", not missing, f"缺少 {missing}" if missing else f"{len(needed)} 个")


def test_no_source_cache() -> None:
    print("\n=== 5. 源码目录里没有测试缓存 ===")
    junk = []
    for name in ("tests/playable.txt", "login_qrcode.png"):
        if (ROOT / name).exists():
            junk.append(name)
    for path in ROOT.rglob("__pycache__"):
        parts = path.relative_to(ROOT).parts
        # 第三方环境和打包产物里的不算（打包时也会排除）
        if any(part in SKIP_DIRS for part in parts):
            continue
        junk.append(path.relative_to(ROOT).as_posix())
    check("没有测试缓存/生成物混在源码里", not junk, "；".join(junk[:5]) if junk else "干净")


def test_all_scripts_use_relative_paths() -> None:
    print("\n=== 6. 批处理脚本都用相对路径 ===")
    bad = []
    for path in ROOT.glob("*.cmd"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("@echo off"):
            bad.append(f"{path.name} 没有以 @echo off 开头")
        if "%~dp0" not in text and "cd /d" not in text:
            bad.append(f"{path.name} 没有切到脚本所在目录")
    check("所有 .cmd 都以 @echo off 开头并切到自身目录", not bad, "；".join(bad[:4]) if bad else "")

    # CRLF：LF 换行的批处理会被 cmd.exe 拆错行（踩过一次）
    wrong_eol = []
    for path in list(ROOT.glob("*.cmd")) + list(ROOT.glob("*.bat")):
        data = path.read_bytes()
        lone_lf = sum(1 for i, b in enumerate(data) if b == 10 and (i == 0 or data[i - 1] != 13))
        if lone_lf:
            wrong_eol.append(f"{path.name}({lone_lf} 个裸 LF)")
    check("批处理都是 CRLF 换行", not wrong_eol, "；".join(wrong_eol) if wrong_eol else "")


def main() -> int:
    print("=" * 70)
    print("  可移植性检查")
    print("=" * 70)

    test_no_absolute_paths()
    test_npmrc_portable()
    test_venv_portable()
    test_stale_venv_detection()
    test_required_files()
    test_no_source_cache()
    test_all_scripts_use_relative_paths()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 70)
    print(f"  结果：{passed}/{total} 通过")
    for label, ok, _ in results:
        if not ok:
            print(f"    未通过：{label}")
    print("=" * 70)
    return 1 if passed != total else 0


if __name__ == "__main__":
    raise SystemExit(main())
