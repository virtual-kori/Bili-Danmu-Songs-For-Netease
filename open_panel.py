"""重新打开浏览器控制台。

面板本身是机器人进程提供的：机器人在跑，面板就在跑。
关掉浏览器标签页【不会】停掉任何东西，重新打开这个网址就行了 ——
这个脚本就是帮你做这件事。

用法：
    .venv\\Scripts\\python.exe open_panel.py             打开浏览器
    .venv\\Scripts\\python.exe open_panel.py --check-only 只检查在不在，不打开浏览器
"""

from __future__ import annotations

import contextlib
import sys
import urllib.error
import urllib.request
import webbrowser

from common import load_config

TIMEOUT = 4.0


def panel_url() -> str:
    config = load_config()
    web = config.get("web") or {}
    host = str(web.get("host", "127.0.0.1"))
    port = int(web.get("port", 8765))
    return f"http://{host}:{port}/"


def panel_alive(url: str) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/status", timeout=TIMEOUT) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def main() -> int:
    with contextlib.suppress(AttributeError, ValueError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    url = panel_url()
    check_only = "--check-only" in sys.argv

    if not panel_alive(url):
        print(f"[X] 面板没在运行：{url}")
        print()
        print("    面板是机器人进程提供的，机器人没跑的时候它就是打不开的。")
        print("    双击 run.cmd 把机器人启动起来，面板会自动跟着打开。")
        print()
        print("    顺带一提：只是关掉浏览器标签页【不会】停掉机器人。")
        print("    面板服务会一直在，重新打开上面那个网址就行 ——")
        print("    建议把它收藏到书签栏，以后就不用找这个脚本了。")
        return 1

    print(f"[OK] 面板在运行：{url}")
    if check_only:
        return 0

    try:
        webbrowser.open(url)
        print("     已尝试打开浏览器；没弹出来的话手动访问上面的地址。")
    except Exception as exc:  # noqa: BLE001
        print(f"     自动打开浏览器失败（{exc}），手动访问上面的地址即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
