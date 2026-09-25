"""被强杀的"受害者"进程（供 orphan_test.py 调用，不单独使用）。

spawn 一个长时间运行的子进程，报告它的 PID，然后傻等。
父进程会强杀本进程，再检查那个子进程有没有被一起带走。

    _orphan_victim.py              走 BaseBackend._spawn（播放器路径）
    _orphan_victim.py --no-job     直接 Popen（模拟修复前的行为）
    _orphan_victim.py --api        走 run.py 的 ApiServer（网易云 API 服务路径）

注意：真正的逻辑放在 main() 里，不要在模块级别直接执行 ——
否则任何 import 这个文件的工具（测试收集器等）都会被 sleep 卡住。
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 用 ping 假装"长时间运行的播放器"
PING_CMD = ["cmd", "/c", "ping -n 120 127.0.0.1 >nul"]

# 测试用的端口，避开真正的 3000
TEST_API_PORT = 3199


def main() -> None:
    sys.path.insert(0, str(ROOT))

    if "--api" in sys.argv:
        # 网易云 API 服务这条路径：run.py 里起的 node
        from run import ApiServer

        server = ApiServer(lambda _msg: None)
        server.start(TEST_API_PORT)
        assert server.process is not None
        print(f"CHILD={server.process.pid}", flush=True)
        time.sleep(300)
        return

    if "--no-job" in sys.argv:
        child = subprocess.Popen(PING_CMD, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        from player import BaseBackend

        backend = BaseBackend()
        child = backend._spawn(PING_CMD)

    print(f"CHILD={child.pid}", flush=True)
    time.sleep(300)


if __name__ == "__main__":
    main()
