"""SongBot 歌词抓取逻辑测试。

机器人依赖（aiohttp / blivedm / 播放器）在没装环境时也能跑：这里用桩模块把它们顶掉，
只测歌词抓取这一条链路（异步抓取、缓存、换歌丢弃、失败降级）。
"""

import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------- 桩依赖
if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ModuleNotFoundError:
        _stub("requests", Session=type("S", (), {"headers": {}}))

_stub("aiohttp", ClientSession=object)
_stub("blivedm", BaseHandler=object, BLiveClient=object)

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    print(f"{'[通过]' if ok else '[失败]'} {label}" + (f"  —— {detail}" if detail else ""))
    return ok


from danmaku_bot import SongBot  # noqa: E402
from common import DEFAULT_CONFIG, deep_merge, ensure_console_utf8  # noqa: E402
from netease_api import Song  # noqa: E402

LRC = "[00:00.000] 第一句\n[00:05.000] 第二句\n"
TLYRIC = "[00:00.000] Line one\n[00:05.000] Line two\n"


class FakeClient:
    """冒充 NeteaseClient，只实现歌词接口。"""

    def __init__(self, lrc=LRC, tlyric=TLYRIC, error=None, delay=0.0):
        self.lrc = lrc
        self.tlyric = tlyric
        self.error = error
        self.delay = delay
        self.calls = 0

    def lyric_full(self, song_id):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.lrc, self.tlyric


def make_bot(client, enabled=True):
    """造一个 SongBot，但跳过 __init__ 里那些要真环境的初始化。"""
    bot = SongBot.__new__(SongBot)
    import collections
    import threading

    bot.config = deep_merge(DEFAULT_CONFIG, {"lyric": {"enabled": enabled}})
    bot.log = types.SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None,
        warning=lambda *a, **k: None, error=lambda *a, **k: None,
    )
    bot.play_client = client
    bot.lyric_enabled = enabled
    bot._lyric_lock = threading.Lock()
    bot._lyric_cache = {}
    bot._lyric_lru = collections.deque(maxlen=80)
    bot._lyric_gen = 0
    bot._lyric_current_id = 0
    bot._lyric_current = None
    bot._lyric_fetching = False
    bot._lyric_error = ""
    bot._lyric_have = False
    return bot


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


SONG = Song(id=123, name="测试歌", artists="测试歌手", duration_ms=200000)


def main() -> int:
    ensure_console_utf8()
    print("=" * 66)
    print("  SongBot 歌词抓取测试")
    print("=" * 66)

    # ------------------------------------------------------ 正常抓取
    # 给桩客户端一点延迟，否则线程可能抢在断言之前就跑完了
    client = FakeClient(delay=0.15)
    bot = make_bot(client)
    bot._start_lyric_fetch(SONG)

    check("抓取中是 fetching 状态", bot.lyric_state()["fetching"] is True, "")
    check("抓取中已经记住 song_id", bot.lyric_state()["song_id"] == 123, "")

    ok = wait_for(lambda: not bot.lyric_state()["fetching"])
    state = bot.lyric_state()
    check("抓取能结束", ok, "")
    check("拿到歌词", state["have"] is True and state["count"] == 2, f"count={state['count']}")
    check("标记为可同步", state["synced"] is True, "")
    check("标记为双语", state["has_translation"] is True, "")
    check("译文已合并", state["lines"][0].get("translation") == "Line one", str(state["lines"][0]))
    check("没有错误", state["error"] == "", state["error"])

    # ------------------------------------------------------ 缓存命中
    bot._start_lyric_fetch(SONG)
    check("第二次是缓存命中（不重新请求）", client.calls == 1, f"调用 {client.calls} 次")
    check("缓存命中后立刻可用", bot.lyric_state()["count"] == 2, "")
    check("缓存命中不算 fetching", bot.lyric_state()["fetching"] is False, "")

    # ------------------------------------------------------ 没有歌词
    bot2 = make_bot(FakeClient(lrc="", tlyric=""))
    bot2._start_lyric_fetch(SONG)
    wait_for(lambda: not bot2.lyric_state()["fetching"])
    st2 = bot2.lyric_state()
    check("无歌词时 have 为假", st2["have"] is False and st2["count"] == 0, f"count={st2['count']}")
    check("无歌词不算错误", st2["error"] == "", st2["error"])

    # ------------------------------------------------------ 抓取失败
    bot3 = make_bot(FakeClient(error=RuntimeError("接口炸了")))
    bot3._start_lyric_fetch(SONG)
    wait_for(lambda: not bot3.lyric_state()["fetching"])
    st3 = bot3.lyric_state()
    check("失败时记录下来", "接口炸了" in st3["error"], st3["error"])
    check("失败时 have 为假", st3["have"] is False, "")
    check("失败不会留下半截歌词", st3["count"] == 0, f"count={st3['count']}")

    # ------------------------------------------------------ 开关
    bot4 = make_bot(FakeClient(), enabled=False)
    bot4._start_lyric_fetch(SONG)
    check("关掉歌词后不请求", bot4.play_client.calls == 0, f"调用 {bot4.play_client.calls} 次")
    check("关掉歌词后 state 里 enabled 为假", bot4.lyric_state()["enabled"] is False, "")

    # ------------------------------------------------------ 换歌时丢弃过期结果
    slow = FakeClient(delay=0.4)
    bot5 = make_bot(slow)
    bot5._start_lyric_fetch(SONG)
    other = Song(id=999, name="另一首", artists="别人", duration_ms=100000)
    bot5._start_lyric_fetch(other)  # 立刻换歌
    check("换歌后当前 id 是新的", bot5.lyric_state()["song_id"] == 999, str(bot5.lyric_state()["song_id"]))
    wait_for(lambda: not bot5.lyric_state()["fetching"], timeout=3)
    time.sleep(0.3)
    st5 = bot5.lyric_state()
    check("过期的旧结果被丢弃", st5["song_id"] == 999, f"song_id={st5['song_id']}")
    check("新歌歌词正常拿到", st5["count"] == 2, f"count={st5['count']}")

    # ------------------------------------------------------ 缓存上限
    bot6 = make_bot(FakeClient())
    for i in range(200):
        bot6._start_lyric_fetch(Song(id=i + 1, name=f"歌{i}", artists="x", duration_ms=1000))
        wait_for(lambda: not bot6.lyric_state()["fetching"], timeout=2)
    check("缓存不会无限增长", len(bot6._lyric_cache) <= 80, f"{len(bot6._lyric_cache)} 条")

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
