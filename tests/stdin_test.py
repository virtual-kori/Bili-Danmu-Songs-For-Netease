"""机器人主循环的集成测试（不需要直播间）。

用 --stdin 模式把 danmaku_bot.py 整个跑起来，按时间喂几条"弹幕"，
然后检查输出里该出现的日志都出现了。会真的出声，但很快。

用法：
    .venv\\Scripts\\python.exe tests\\stdin_test.py
    .venv\\Scripts\\python.exe tests\\stdin_test.py --quiet   不打印机器人原始输出
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import CONFIG_PATH, ensure_console_utf8  # noqa: E402

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
FAKE_UID = 10001


def pick_unmatched_keyword() -> str:
    """找一个确实搜不到任何结果的词（实现见 _helpers，网页面板测试也用同一个）。"""
    from _helpers import pick_unmatched_keyword as impl

    return impl()


def run_bot(script: list[tuple[str, float]]) -> str:
    """启动机器人，按 (内容, 之后等待秒数) 依次喂入，返回全部输出。"""
    proc = subprocess.Popen(
        [str(PYTHON), "danmaku_bot.py", "--stdin"],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    chunks: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            chunks.append(line)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    try:
        for text, wait in script:
            assert proc.stdin is not None
            proc.stdin.write(text + "\n")
            proc.stdin.flush()
            time.sleep(wait)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass

    try:
        proc.wait(timeout=40)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    thread.join(timeout=5)
    return "".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true", help="不打印机器人原始输出")
    args = parser.parse_args()

    ensure_console_utf8()
    print("=" * 66)
    print("  机器人主循环集成测试（--stdin 模式）")
    print("=" * 66)

    original = CONFIG_PATH.read_text(encoding="utf-8")
    config = json.loads(original)
    # 让模拟观众拥有控制权限，才能把音量/切歌这些分支也走一遍
    config["request"]["admin_uids"] = [FAKE_UID]
    # 关掉冷却：脚本里几条点歌都是同一个 uid，否则会被"点歌太快"拦住，
    # 就测不到"搜不到"这条分支了（冷却逻辑在 smoke_test 里单独覆盖）
    config["request"]["user_cooldown_sec"] = 0
    config["player"].setdefault("backend", "mpv")
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已临时把 admin_uids 设为 [{FAKE_UID}]、关闭点歌冷却（测试结束会还原）\n")

    try:
        missing = pick_unmatched_keyword()
        print(f"用于测试「搜不到」的关键词：{missing}\n")
        script = [
            ("点歌 卡农", 12.0),          # 搜索 -> 取地址 -> 开始播放
            ("队列", 2.0),
            ("音量 40", 2.0),
            ("切歌", 3.0),
            (f"点歌 {missing}", 5.0),     # 搜不到的分支
            ("你好呀", 1.0),              # 无关弹幕，应被忽略
            ("退出", 3.0),
        ]
        output = run_bot(script)
    finally:
        CONFIG_PATH.write_text(original, encoding="utf-8")
        print("config.json 已还原\n")

    if not args.quiet:
        print("---------------- 机器人输出 ----------------")
        print(output)
        print("--------------------------------------------")

    expectations = [
        ("收到并识别点歌指令", "弹幕指令 测试观众(10001)：点歌 卡农"),
        ("搜索命中歌曲", "命中："),
        ("加入队列", "入队结果：已加入队列"),
        ("开始播放", "开始播放："),
        ("打印播放地址音质", "音质："),
        ("响应队列查询", "队列查询 by 测试观众"),
        ("执行音量指令", "音量 -> 40"),
        ("执行切歌", "触发了切歌"),
        ("搜不到时给出提示", f"没搜到：{missing}"),
        ("正常退出", "已退出"),
    ]
    ignored_ok = "弹幕指令 测试观众(10001)：你好呀" not in output

    failed: list[str] = []
    for label, needle in expectations:
        ok = needle in output
        print(f"{'[通过]' if ok else '[失败]'} {label}" + ("" if ok else f"  —— 输出里没找到 {needle!r}"))
        if not ok:
            failed.append(label)

    print(f"{'[通过]' if ignored_ok else '[失败]'} 无关弹幕被忽略")
    if not ignored_ok:
        failed.append("无关弹幕被忽略")

    total = len(expectations) + 1
    print("\n" + "=" * 66)
    print(f"  结果：{total - len(failed)}/{total} 通过")
    if failed:
        for label in failed:
            print(f"    未通过：{label}")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
