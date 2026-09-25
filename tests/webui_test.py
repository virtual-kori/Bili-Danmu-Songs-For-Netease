"""网页面板测试：真启动机器人（--stdin 模式），然后打它的 HTTP 接口。

用法（需要网易云 API 服务已经在跑）：
    .venv\\Scripts\\python.exe tests\\webui_test.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import ensure_console_utf8  # noqa: E402

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    print(f"{'[通过]' if ok else '[失败]'} {label}" + (f"  —— {detail}" if detail else ""))
    return ok


def http_get(path: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def http_post(path: str, payload: dict, timeout: float = 5.0) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    ensure_console_utf8()
    print("=" * 66)
    print("  网页面板测试")
    print("=" * 66)

    # ---------------------------------------------------------- 端口冲突检测
    # Windows 上如果给探测用的 socket 设了 SO_REUSEADDR，被占用的端口也会"绑定成功"，
    # 冲突检测就形同虚设（两个实例会抢同一个端口）。这里守住这个行为。
    import socket as _socket

    from webui import _port_usable

    probe_port = PORT + 50
    holder = _socket.socket()
    holder.bind(("127.0.0.1", probe_port))
    holder.listen(1)
    try:
        check("能识别出端口已被占用", not _port_usable("127.0.0.1", probe_port))
        check("能识别出端口空闲", _port_usable("127.0.0.1", probe_port + 1))
    finally:
        holder.close()

    proc = subprocess.Popen(
        [str(PYTHON), "danmaku_bot.py", "--stdin", "--no-browser", "--web-port", str(PORT)],
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
        # ---------------------------------------------------------- 等面板起来
        status_code, body = 0, ""
        for _ in range(40):
            status_code, body = http_get("/api/status")
            if status_code == 200:
                break
            time.sleep(0.5)

        check("面板能起来（/api/status 返回 200）", status_code == 200, f"HTTP {status_code}")

        if status_code != 200:
            print("\n机器人输出：")
            print("".join(chunks))
            return 1

        # ---------------------------------------------------------- 页面
        code, html = http_get("/")
        check("首页能打开", code == 200 and "<title>" in html, f"HTTP {code}，{len(html)} 字节")
        check("页面自带样式和脚本（不依赖外网 CDN）", "setInterval(refresh" in html and "http://" not in html.split("<script>")[0].replace("http://www.w3.org", ""))
        check("页面包含控制按钮", all(k in html for k in ("切歌", "暂停", "继续", "清空队列")))
        check("页面包含弹幕面板", "收到的弹幕" in html and "id=\"danmaku\"" in html)

        # ---------------------------------------------------------- 状态字段
        try:
            status = json.loads(body)
        except ValueError:
            status = {}
        expected_keys = {
            "connected", "room_id", "netease_ok", "backend", "volume",
            "paused", "now_playing", "queue", "stats", "logs", "danmaku",
        }
        missing = expected_keys - set(status)
        check("状态接口字段齐全", not missing, f"缺少 {missing}" if missing else f"backend={status.get('backend')} volume={status.get('volume')}")

        stats = status.get("stats") or {}
        dm_keys = {"danmaku_received", "danmaku_matched", "seconds_since_danmaku"}
        check("状态里有弹幕统计字段", dm_keys <= set(stats), f"stats={sorted(stats)}")
        check("弹幕列表是数组", isinstance(status.get("danmaku"), list), f"{len(status.get('danmaku') or [])} 条")

        # ---------------------------------------------------------- 控制指令
        code, body = http_post("/api/command", {"action": "volume", "value": 55})
        ok = code == 200 and json.loads(body).get("ok") is True
        check("调音量", ok, body.strip())

        code, _ = http_get("/api/status")
        time.sleep(0.3)
        _, body = http_get("/api/status")
        check("音量已生效（状态里是 55）", json.loads(body).get("volume") == 55, f"volume={json.loads(body).get('volume')}")

        code, body = http_post("/api/command", {"action": "clear"})
        check("清空队列", code == 200 and json.loads(body).get("ok") is True, body.strip())

        code, body = http_post("/api/command", {"action": "skip"})
        check("切歌", code == 200 and json.loads(body).get("ok") is True, body.strip())

        code, body = http_post("/api/command", {"action": "stop"})
        stop_ok = code == 200 and json.loads(body).get("ok") is True
        check("停止播放", stop_ok, body.strip())

        code, html2 = http_get("/")
        check("页面有「停止播放」按钮", "停止播放" in html2 and "cmd('stop')" in html2)

        # ---------------------------------------------------------- 随机播放开关
        code, body = http_post("/api/command", {"action": "autoplay", "value": True})
        check("打开空闲随机播放", json.loads(body).get("ok") is True, json.loads(body).get("message"))
        status_ap = json.loads(http_get("/api/status")[1])
        check("状态里 autoplay.enabled 为真", (status_ap.get("autoplay") or {}).get("enabled") is True)

        code, body = http_post("/api/command", {"action": "autoplay", "value": False})
        check("关闭空闲随机播放", json.loads(body).get("ok") is True, json.loads(body).get("message"))
        status_ap2 = json.loads(http_get("/api/status")[1])
        check("状态里 autoplay.enabled 为假", (status_ap2.get("autoplay") or {}).get("enabled") is False)
        check("页面有随机播放开关", 'id="autoPlay"' in html2 and "空闲随机播放" in html2)

        code, body = http_post("/api/command", {"action": "不存在的操作"})
        check("未知操作被拒绝", json.loads(body).get("ok") is False, body.strip())

        code, body = http_post("/api/command", {"action": "volume", "value": "abc"})
        check("非法参数不崩", code == 200 and json.loads(body).get("ok") is False, body.strip())

        # ---------------------------------------------------------- 日志
        code, body = http_get("/api/logs")
        logs = json.loads(body)
        check("日志接口有内容", code == 200 and isinstance(logs, list) and len(logs) > 0, f"{len(logs)} 条")

        status2 = json.loads(http_get("/api/status")[1])
        check("状态里也带日志（页面一次请求就够）", len(status2.get("logs", [])) > 0, f"{len(status2.get('logs', []))} 条")

        # ---------------------------------------------------------- 404
        code, _ = http_get("/api/nothing")
        check("未知路径返回 404", code == 404, f"HTTP {code}")

        # ---------------------------------------------------------- 点歌后队列可见
        if proc.stdin:
            proc.stdin.write("点歌 卡农\n")
            proc.stdin.flush()
        queued = False
        for _ in range(30):
            time.sleep(0.5)
            s = json.loads(http_get("/api/status")[1])
            if s.get("now_playing") or s.get("queue"):
                queued = True
                np = s.get("now_playing") or {}
                detail = f"当前播放={np.get('name')} 队列={len(s.get('queue', []))} 已播放={s.get('stats', {}).get('played')}"
                break
        check("点歌后状态里能看到播放/队列", queued, detail if queued else "30 次轮询内没出现")

        # ---------------------------------------------------------- 网页面板点歌
        code, body = http_post("/api/command", {"action": "search", "value": "晴天"})
        data = json.loads(body)
        songs = data.get("songs") or []
        check("搜索返回候选列表", code == 200 and data.get("ok") is True and songs, f"{len(songs)} 首")
        if songs:
            fields = {"id", "name", "artists", "duration", "duration_ms", "vip_only"}
            check("候选字段完整", fields <= set(songs[0]), str(songs[0])[:90])

        code, body = http_post("/api/command", {"action": "search", "value": "   "})
        check("空关键词被拒绝", json.loads(body).get("ok") is False, json.loads(body).get("message"))

        # 网易云是模糊匹配，随便编个乱码也能搜到东西，所以要动态找一个真的搜不到的词
        from _helpers import pick_unmatched_keyword

        missing = pick_unmatched_keyword()
        code, body = http_post("/api/command", {"action": "search", "value": missing})
        check(
            "搜不到时给出提示",
            json.loads(body).get("ok") is False,
            f"用 {missing!r} 测试 -> {json.loads(body).get('message')}",
        )

        # 按关键词直接点（面板上的「直接点第一首」）
        code, body = http_post("/api/command", {"action": "request", "value": {"keyword": "稻香"}})
        data = json.loads(body)
        check("面板按关键词点歌", code == 200 and data.get("ok") is True, data.get("message"))
        added_name = (data.get("song") or {}).get("name")

        code, body = http_post("/api/command", {"action": "request", "value": {"keyword": "稻香"}})
        check("重复点同一首被拒绝（和弹幕点歌同样规则）", json.loads(body).get("ok") is False, json.loads(body).get("message"))

        # 用搜索结果里的完整信息点歌（面板上的「点这首」）
        if songs:
            code, body = http_post("/api/command", {"action": "request", "value": songs[0]})
            data = json.loads(body)
            check("面板按搜索结果点歌", code == 200 and data.get("ok") is True, data.get("message"))

        code, body = http_post("/api/command", {"action": "request", "value": {}})
        check("点歌缺参数被拒绝", json.loads(body).get("ok") is False, json.loads(body).get("message"))

        status3 = json.loads(http_get("/api/status")[1])
        queue_names = [item["name"] for item in status3.get("queue", [])]
        now_name = (status3.get("now_playing") or {}).get("name")
        check(
            "面板点的歌真的进了队列",
            bool(added_name) and (added_name in queue_names or added_name == now_name),
            f"点的是《{added_name}》，队列={queue_names}，正在播={now_name}",
        )
        check(
            "面板点歌的点歌人标记为「网页面板」",
            any(item.get("requester") == "网页面板" for item in status3.get("queue", [])),
            str([item.get("requester") for item in status3.get("queue", [])]),
        )
    finally:
        if proc.stdin:
            try:
                proc.stdin.write("退出\n")
                proc.stdin.flush()
            except OSError:
                pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        thread.join(timeout=5)

        if not args.quiet:
            print("\n---------------- 机器人输出（尾部）----------------")
            print("".join(chunks[-25:]))
            print("---------------------------------------------------")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 66)
    print(f"  结果：{passed}/{total} 通过")
    for label, ok, _ in results:
        if not ok:
            print(f"    未通过：{label}")
    print("=" * 66)
    return 1 if passed != total else 0


if __name__ == "__main__":
    raise SystemExit(main())
