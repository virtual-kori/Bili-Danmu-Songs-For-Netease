"""一键运行：拉起网易云 API 服务，等它就绪，再启动弹幕点歌机器人。

把原来「先开一个窗口跑 API，再开一个窗口跑机器人」合成一步，
退出时（含 Ctrl+C）自动把 API 服务一起收掉，不会留后台进程。

用法（一般直接双击 run.cmd）：
    run.cmd                     正常点歌
    run.cmd --room 1234567      临时指定直播间号
    run.cmd --stdin             键盘模拟点歌，不连直播间
    run.cmd --list-backends     看有哪些播放器可用
    run.cmd --keep-api          退出时保留 API 服务

如果 3000 端口上已经有 API 服务在跑，会直接复用，不会重复启动。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from common import ROOT, ensure_console_utf8, load_config, setup_logging

print("本程序由B站：半遥狐 开发开源")

VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
API_DIR = ROOT / "netease-api"
SERVE_JS = API_DIR / "serve.js"

# 启动时等 API 服务就绪的上限。首次启动要注册匿名 token + 拉公钥，会慢一些。
STARTUP_TIMEOUT = 90.0


def _ensure_venv() -> None:
    """如果被系统 Python 启动了，就换成项目虚拟环境重新执行。"""
    if not VENV_PYTHON.is_file():
        return
    with contextlib.suppress(OSError, ValueError):
        if Path(sys.executable).resolve() == VENV_PYTHON.resolve():
            return
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])


def api_healthy(base_url: str, timeout: float = 2.0) -> bool:
    """API 服务是否已经能正常应答。"""
    import requests

    try:
        resp = requests.get(
            f"{base_url}/search",
            params={"keywords": "test", "type": 1, "limit": 1},
            timeout=timeout,
        )
        return resp.status_code == 200
    except Exception:  # noqa: BLE001 - 没起来就是各种连接错误
        return False


class ApiServer:
    """网易云 API 服务的子进程包装。

    服务端每处理一个请求都会打一行日志，全打到控制台会把机器人的输出淹没，
    所以这里静默收集，只在它意外退出时把最近的日志倒出来方便排查。
    """

    def __init__(self, log) -> None:
        self.log = log
        self.process: subprocess.Popen | None = None
        self.buffer: deque[str] = deque(maxlen=200)
        self.owned = False
        self.port = 3000
        self._stop_watch = threading.Event()
        self._watch_thread: threading.Thread | None = None

    def start(self, port: int) -> None:
        node = shutil.which("node")
        if not node:
            raise RuntimeError("没找到 node，请先安装 Node.js（https://nodejs.org/）")

        self.port = port
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        env = dict(os.environ)
        env["PORT"] = str(port)

        self.process = subprocess.Popen(
            [node, str(SERVE_JS)],
            cwd=str(API_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=creationflags,
        )
        # 关键：把 API 服务也放进"父死子死"的作业对象。
        # 否则机器人被强杀（关窗口、任务管理器、崩溃）时，node 会变成孤儿进程
        # 继续占着 3000 端口，下次启动还会撞车。
        try:
            from player import assign_to_our_job

            assign_to_our_job(self.process)
        except Exception:  # noqa: BLE001 - 兜底失败不影响正常流程
            pass

        self.owned = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            self.buffer.append(line.rstrip())

    def wait_ready(self, base_url: str, timeout: float = STARTUP_TIMEOUT) -> tuple[bool, str]:
        """轮询直到服务可用；服务提前挂掉就立刻返回失败。"""
        deadline = time.time() + timeout
        reported = 0
        while time.time() < deadline:
            if self.process is not None and self.process.poll() is not None:
                return False, f"API 服务启动后立刻退出了（退出码 {self.process.returncode}）"
            if api_healthy(base_url):
                return True, ""
            waited = int(timeout - (deadline - time.time()))
            if waited and waited // 10 > reported:
                reported = waited // 10
                self.log.info("  还在启动中… 已等待 %d 秒", waited)
            time.sleep(0.4)
        return False, f"等待 {timeout:.0f} 秒后 API 服务仍无响应"

    def dump_tail(self, lines: int = 30) -> None:
        if not self.buffer:
            return
        self.log.error("API 服务最近的输出：")
        for line in list(self.buffer)[-lines:]:
            print(f"    {line}")

    def stop(self) -> None:
        self._stop_watch.set()
        self.terminate()

    def terminate(self) -> None:
        """只停进程，不停看护线程（看护重启时要用）。"""
        if not self.owned or self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.process.wait(timeout=5)

    # ------------------------------------------------------------ 服务看护

    def start_watch(self, base_url: str) -> None:
        """起一个后台线程盯着服务，挂了就自动拉起来。

        为什么要这个：机器人本身能容忍 API 服务短暂不可用（搜索失败会打日志继续跑），
        但用户看到的只是"搜不到歌"，很难联想到是服务挂了。自动重启省事得多。
        """
        self._watch_thread = threading.Thread(
            target=self._watch, args=(base_url,), name="api-watchdog", daemon=True
        )
        self._watch_thread.start()

    def _watch(self, base_url: str) -> None:
        interval = 20.0
        misses = 0
        while not self._stop_watch.wait(interval):
            if api_healthy(base_url):
                misses = 0
                continue

            misses += 1
            if not self.owned:
                # 别人起的服务（比如你自己开的 start_netease_api.cmd）。
                # 连续几次都不通，就自己拉一个起来，别让机器人一直瘸着。
                if misses >= 3:
                    self.log.warning("API 服务连续 %d 次无响应，尝试自己启动一个…", misses)
                    if self._restart(base_url):
                        misses = 0
                continue

            code = self.process.returncode if self.process is not None else "?"
            if self.process is not None and self.process.poll() is None:
                self.log.warning("API 服务没有响应，正在重启…")
            else:
                self.log.warning("API 服务已退出（退出码 %s），正在重启…", code)
            self.dump_tail(15)
            if self._restart(base_url):
                misses = 0

    def _restart(self, base_url: str) -> bool:
        self.terminate()
        self.buffer.clear()
        try:
            self.start(self.port)
        except (OSError, RuntimeError) as exc:
            self.log.error("重启 API 服务失败：%s", exc)
            return False
        ok, reason = self.wait_ready(base_url, timeout=60)
        if ok:
            self.log.info("API 服务已恢复：%s", base_url)
            return True
        self.log.error("重启后仍不可用：%s", reason)
        return False


def _split_args(argv: list[str]) -> tuple[bool, list[str]]:
    """把 run.py 自己的参数挑出来，其余原样透传给 danmaku_bot。"""
    keep_api = "--keep-api" in argv
    forwarded = [a for a in argv if a != "--keep-api"]
    return keep_api, forwarded


def main() -> int:
    ensure_console_utf8()
    log = setup_logging()

    _ensure_venv()

    if not VENV_PYTHON.is_file():
        log.error("还没装依赖：找不到 %s", VENV_PYTHON)
        log.error("请先双击 install.cmd")
        return 1
    if not SERVE_JS.is_file():
        log.error("找不到 API 服务启动器：%s", SERVE_JS)
        log.error("请先双击 install.cmd")
        return 1

    keep_api, forwarded = _split_args(sys.argv[1:])

    config = load_config()
    base_url = config["netease"]["api_base"].rstrip("/")
    port = 3000
    with contextlib.suppress(ValueError):
        port = int(base_url.rsplit(":", 1)[-1])

    print("=" * 62)
    print("  B站弹幕点歌机器人 · 一键运行")
    print("=" * 62)

    server = ApiServer(log)

    # 1) API 服务
    if api_healthy(base_url):
        log.info("检测到 API 服务已在运行，直接复用：%s", base_url)
    else:
        log.info("正在启动网易云 API 服务（端口 %d）…", port)
        try:
            server.port = port
            server.start(port)
        except (OSError, RuntimeError) as exc:
            log.error("%s", exc)
            return 1
        ok, reason = server.wait_ready(base_url)
        if not ok:
            log.error("%s", reason)
            server.dump_tail()
            server.terminate()
            return 1
        log.info("API 服务已就绪：%s", base_url)

    # 起了看护线程：服务中途挂掉会自动重启，不用你盯着
    server.start_watch(base_url)

    if not config["netease"].get("cookie"):
        log.warning("还没扫码登录网易云，VIP 歌曲只能拿到试听片段")
        log.warning("建议先双击 login_netease.cmd 扫一次码")

    # 2) 机器人（参数原样透传）
    log.info("正在启动机器人…（这个窗口关掉/按 Ctrl+C 就全部停止）")
    print("-" * 62)

    sys.argv = [str(ROOT / "danmaku_bot.py"), *forwarded]
    exit_code = 1
    try:
        from danmaku_bot import main as bot_main

        exit_code = bot_main()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，正在退出…")
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - 启动失败要给出可读原因
        log.exception("机器人启动失败：%s", exc)
        exit_code = 1

    # 3) 收尾
    if keep_api and server.owned:
        log.info("按 --keep-api 要求，保留 API 服务（端口 %d）", port)
    else:
        if server.owned:
            log.info("正在停止 API 服务…")
        server.stop()

    print("=" * 62)
    print("  已全部退出" if exit_code == 0 else f"  退出（代码 {exit_code}）")
    print("=" * 62)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
