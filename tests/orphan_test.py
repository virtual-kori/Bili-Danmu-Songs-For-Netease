"""验证：机器人被【强杀】时，播放器子进程会不会变成孤儿。

这是一个真实踩过的坑：用户点了控制台窗口的 ×、或者用任务管理器结束进程，
Python 来不及做清理，mpv 就变成孤儿进程**一直放音乐**。
普通的 try/finally 拦不住 TerminateProcess，只有 Windows 作业对象能兜住。

对照两组：
  A. --no-job  直接 Popen（修复前的行为）—— 预期：子进程活下来（复现问题）
  B. 走 _spawn（加入作业对象）            —— 预期：子进程被一起杀掉（已修复）

用法：
    .venv\\Scripts\\python.exe tests\\orphan_test.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import ensure_console_utf8  # noqa: E402

PY = ROOT / ".venv" / "Scripts" / "python.exe"
VICTIM = ROOT / "tests" / "_orphan_victim.py"


def alive(pid: int) -> bool:
    """用 tasklist 查进程是否还活着（避免引入 psutil 依赖）。"""
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",
    ).stdout
    return str(pid) in out


def run_case(label: str, extra_args: list[str]) -> tuple[bool, bool]:
    """返回 (强杀前是否存活, 强杀后是否存活)。"""
    args = [str(PY), str(VICTIM)] + extra_args
    victim = subprocess.Popen(
        args,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )

    child_pid = None
    deadline = time.time() + 30
    while time.time() < deadline:
        line = victim.stdout.readline()
        if not line:
            break
        if line.strip().startswith("CHILD="):
            child_pid = int(line.strip().split("=", 1)[1])
            break

    print(f"\n--- {label} ---")
    if child_pid is None:
        print("  没能拿到子进程 PID")
        victim.kill()
        victim.wait(timeout=10)
        return False, False

    print(f"  受害者 PID={victim.pid}  子进程 PID={child_pid}")
    time.sleep(1.5)
    before = alive(child_pid)
    print(f"  强杀前子进程存活：{before}")

    victim.kill()  # TerminateProcess：Python 代码拦不住
    victim.wait(timeout=10)
    print(f"  已强杀受害者 PID={victim.pid}")

    time.sleep(3)
    after = alive(child_pid)
    print(f"  强杀后子进程存活：{after}")

    if after:
        print("  => 变成孤儿进程了，音乐会一直放下去")
        subprocess.run(["taskkill", "/F", "/PID", str(child_pid)], capture_output=True, text=True)
    else:
        print("  => 子进程已随之终止")

    return before, after


def main() -> int:
    ensure_console_utf8()
    print("=" * 66)
    print("  强杀实验：子进程会不会变成孤儿")
    print("=" * 66)

    print("\n[播放器子进程]")
    before_a, after_a = run_case("A. 修复前（直接 Popen，无作业对象）", ["--no-job"])
    before_b, after_b = run_case("B. 修复后（_spawn，加入作业对象）", [])

    print("\n[网易云 API 服务（run.py 起的 node）]")
    before_c, after_c = run_case("C. ApiServer 起的 node", ["--api"])

    print("\n" + "=" * 66)
    checks = [
        ("A 组能复现问题：播放器被强杀后变成孤儿", before_a and after_a),
        ("B 组已修复：播放器随父进程一起终止", before_b and not after_b),
        ("C 组已修复：API 服务随父进程一起终止", before_c and not after_c),
    ]
    failed = 0
    for label, ok in checks:
        print(f"{'[通过]' if ok else '[失败]'} {label}")
        failed += 0 if ok else 1
    print(f"\n结果：{len(checks) - failed}/{len(checks)} 通过")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
