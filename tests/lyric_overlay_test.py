"""歌词叠加层测试：起一个真的 ControlPanel（用假 bot），打它的 HTTP 接口。

不需要网易云 API，也不需要机器人依赖：
    .venv\\Scripts\\python.exe tests\\lyric_overlay_test.py

会临时创建 config.json 并在结束时复原，不会留下垃圾。
"""

from __future__ import annotations

import json
import re
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# webui 会 import netease_api，而后者依赖 requests。
# 这个测试只测面板和叠加层的 HTTP 层，不需要真的发请求，
# 所以塞一个最小桩进去，让测试在没有装依赖的环境也能跑。
if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ModuleNotFoundError:
        stub = types.ModuleType("requests")

        class _Session:
            headers: dict = {}

            def get(self, *a, **k):
                raise RuntimeError("测试不应发起真实网络请求")

        stub.Session = _Session  # type: ignore[attr-defined]
        stub.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("测试不应发起真实网络请求"))
        sys.modules["requests"] = stub

from common import CONFIG_PATH, VERSION, ensure_console_utf8  # noqa: E402
from lyrics import parse_lyrics  # noqa: E402
from webui import ControlPanel, sanitize_lyric_settings  # noqa: E402

PORT = 8807
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


# 一份带翻译的歌词，模拟机器人抓到的结果
LRC = "[00:00.000] 第一句\n[00:05.000] 第二句\n[00:10.000] 第三句\n"
TLYRIC = "[00:00.000] Line one\n[00:05.000] Line two\n[00:10.000] Line three\n"


class FakeBot:
    """够 ControlPanel 用的假机器人。"""

    def __init__(self) -> None:
        self.paused = False
        self.config = {"lyric": {"enabled": True}}
        self.lyric_enabled = True
        self._elapsed = 5.5
        self._lyrics = parse_lyrics(LRC, TLYRIC)

        outer = self

        class Player:
            volume = 70
            backend_name = "mpv"
            supports_pause = True

            def skip(self): pass
            def pause(self): return True
            def resume(self): return True
            def set_volume(self, v): self.volume = v

        class Queue:
            def clear(self): return 0
            def snapshot(self): return []

        self.player = Player()
        self.queue = Queue()
        self._outer = outer

    def status_snapshot(self):
        return {
            "connected": True, "room_id": 123, "netease_ok": True, "netease_account": "测试",
            "backend": "mpv", "paused": False, "volume": 70,
            "now_playing": {"name": "测试歌曲", "artists": "测试歌手", "duration_sec": 200,
                            "requester": "观众", "quality": "极高"},
            "now_elapsed": self._elapsed,
            "queue": [],
            "stats": {"played": 1},
            "danmaku": [],
            "lyric": {
                "enabled": self.lyric_enabled,
                "song_id": 999,
                "lines": [ln.to_json() for ln in self._lyrics.lines],
                "synced": self._lyrics.synced,
                "has_translation": self._lyrics.has_translation,
                "count": len(self._lyrics.lines),
                "fetching": False,
                "have": True,
                "error": "",
            },
            "logs": [],
        }


def main() -> int:
    ensure_console_utf8()
    print("=" * 66)
    print("  歌词叠加层测试")
    print("=" * 66)

    # config.json 会被测试改写，先备份
    backup = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.is_file() else None
    existed = CONFIG_PATH.is_file()

    panel = ControlPanel(FakeBot(), host="127.0.0.1", port=PORT, log=lambda m: None)
    url = panel.start(open_browser=False)
    if not url:
        print("面板起不来，测试没法继续")
        return 1

    try:
        # ------------------------------------------------------ 叠加层页面
        code, html = http_get("/overlay")
        check("叠加层页面能打开", code == 200 and "<title>" in html, f"HTTP {code}，{len(html)} 字节")
        check("叠加层背景透明", "background:transparent" in html.replace(" ", ""), "")
        check("页面不依赖外部 CDN", "http://" not in html.replace("http://www.w3.org", ""), "")
        check("有歌词容器", 'id="lines"' in html and 'id="stage"' in html, "")
        check("支持逐句高亮", ".ln.active" in html, "")
        check("支持译文显示", ".tr" in html and "translation" in html, "")
        check("字体可自定义", "--font-family" in html, "")
        check("位置可自定义", "--anchor" in html, "")
        check("有自定义 CSS 注入点", "userCssEl" in html, "")

        # ------------------------------------------------------ 叠加层接口
        code, body = http_get("/api/overlay")
        data = json.loads(body)
        check("叠加层接口返回 200", code == 200, f"HTTP {code}")
        check("接口字段齐全", {"playing", "elapsed", "lyric", "config"} <= set(data), str(sorted(data)))
        check("正在播放标记正确", data.get("playing") is True, "")
        check("播放进度正确", abs((data.get("elapsed") or 0) - 5.5) < 0.01, str(data.get("elapsed")))
        lyric = data.get("lyric") or {}
        check("歌词行数正确", lyric.get("count") == 3, str(lyric.get("count")))
        check("标记为可同步", lyric.get("synced") is True, "")
        check("标记为双语", lyric.get("has_translation") is True, "")
        check("译文已对齐", (lyric.get("lines") or [{}])[0].get("translation") == "Line one",
              str((lyric.get("lines") or [{}])[0]))
        check("配置随接口下发", isinstance(data.get("config"), dict) and "font_size" in data["config"], "")

        # ------------------------------------------------------ 面板页面
        code, panel_html = http_get("/")
        check("控制面板能打开", code == 200 and "<title>" in panel_html, f"HTTP {code}")
        check("面板有歌词区", 'id="lyricBox"' in panel_html, "")
        check("面板有叠加层地址", 'id="ovUrl"' in panel_html and "/overlay" in panel_html, "")
        check("面板有样式设置表单", 'id="lyFont"' in panel_html and 'id="lyCss"' in panel_html, "")
        check("面板有显示方式选择", 'id="lyMode"' in panel_html and "focus" in panel_html, "")
        check("面板字段与接口对得上", 'id="lySize"' in panel_html and 'id="lyActive"' in panel_html, "")

        # ------------------------------------------------------ 版本号
        check("面板显示版本号", VERSION in panel_html, f"页面里没找到 {VERSION}")
        check("版本占位符已被替换", "{{VERSION}}" not in panel_html, "占位符没被替换掉")
        check("叠加层接口也带版本", data.get("version") == VERSION, str(data.get("version")))
        status_now = json.loads(http_get("/api/status")[1])
        check("状态接口也带版本", status_now.get("version") == VERSION, str(status_now.get("version")))

        # ------------------------------------------------------ 面板不能抢页面滚动
        # scrollIntoView 会连整个页面一起滚，导致每唱一句页面就被拉回歌词卡片。
        # 面板必须只动歌词框自己的 scrollTop。
        # 注意只匹配真正的调用，注释里提到这个名字是允许的（正好用来解释为什么不这么做）。
        call = re.compile(r"\.scrollIntoView\s*\(")
        check("面板歌词高亮不会滚动整个页面", not call.search(panel_html),
              "面板里调用了 scrollIntoView，会抢走页面滚动")
        check("面板只在歌词框内部滚动", "box.scrollTo" in panel_html and "box.scrollTop" in panel_html, "")
        check("歌词框是可滚动容器", "overflow:auto" in panel_html.replace(" ", "")
              and "nowlyric" in panel_html, "")
        # 弹幕和日志本来就只用容器自己的 scrollTop，顺便一起守住
        check("弹幕与日志也只滚自己的容器",
              panel_html.count("scrollTop") >= 3, f"scrollTop 出现 {panel_html.count('scrollTop')} 次")

        # 叠加层同样不能抢滚动（它是 OBS 里的独立页面，更不能动）
        check("叠加层也不用 scrollIntoView", not call.search(html), "叠加层里调用了 scrollIntoView")

        # ------------------------------------------------------ 保存设置
        code, body = http_post("/api/settings", {
            "font_family": "思源黑体", "font_size": 72, "color": "#ffe066",
            "active_color": "#ff5c8a", "mode": "focus", "anchor": "top",
            "align": "left", "active_scale": 1.2, "css": ".ln.active{ text-shadow:0 0 20px red; }",
        })
        result = json.loads(body)
        check("保存设置返回成功", code == 200 and result.get("ok") is True, str(result.get("message")))
        saved = result.get("setting") or {}
        check("字号已保存", saved.get("font_size") == 72, str(saved.get("font_size")))
        check("模式已保存", saved.get("mode") == "focus", str(saved.get("mode")))
        check("CSS 已保存", "text-shadow" in str(saved.get("css")), "")

        # 真的写进 config.json 了吗
        written = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        check("已写进 config.json", (written.get("lyric") or {}).get("font_size") == 72,
              str((written.get("lyric") or {}).get("font_size")))
        check("写入时保留了其它配置段", "bilibili" in written and "netease" in written,
              str(sorted(written.keys())))

        # 面板重新读到的应该是新值
        code, body = http_get("/api/status")
        status = json.loads(body)
        check("状态接口带 lyric_settings", "lyric_settings" in status, "")
        check("状态里的设置是新值", (status.get("lyric_settings") or {}).get("font_size") == 72,
              str((status.get("lyric_settings") or {}).get("font_size")))

        # 叠加层接口也要跟着变
        code, body = http_get("/api/overlay")
        cfg = (json.loads(body).get("config") or {})
        check("叠加层拿到新设置", cfg.get("font_size") == 72 and cfg.get("mode") == "focus", str(cfg.get("mode")))

        # ------------------------------------------------------ 非法输入被挡住
        code, body = http_post("/api/settings", {"font_size": 99999, "color": "javascript:alert(1)",
                                                 "mode": "'; DROP TABLE", "align": "嗯"})
        result = json.loads(body)
        hard = result.get("setting") or {}
        check("超大字号被夹到上限", hard.get("font_size") == 300, str(hard.get("font_size")))
        check("非法颜色被退回默认", hard.get("color") == "#ffffff", str(hard.get("color")))
        check("非法模式被退回默认", hard.get("mode") == "scroll", str(hard.get("mode")))
        check("非法对齐被退回默认", hard.get("align") == "center", str(hard.get("align")))

        # ------------------------------------------------------ 纯函数
        sane = sanitize_lyric_settings({"font_size": "abc", "css": "x" * 99999})
        check("非数字字号退回默认", sane.get("font_size") == 44, str(sane.get("font_size")))
        check("超长 CSS 被截断", len(str(sane.get("css"))) <= 20000, str(len(str(sane.get("css")))))
        check("未知字段不会被写进去", "evil" not in sanitize_lyric_settings({"evil": 1}), "")
        check("开关能关掉", sanitize_lyric_settings({"enabled": False}).get("enabled") is False, "")

        # ------------------------------------------------------ 404
        code, _ = http_get("/api/overlay-nothing")
        check("未知路径仍返回 404", code == 404, f"HTTP {code}")
    finally:
        panel.stop()
        # 复原 config.json
        if backup is None:
            CONFIG_PATH.unlink(missing_ok=True)
        else:
            CONFIG_PATH.write_text(backup, encoding="utf-8")

    restored = "不存在（已清理）" if not existed else "已复原"
    check("config.json 已复原", (not existed and not CONFIG_PATH.exists()) or existed, restored)

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
