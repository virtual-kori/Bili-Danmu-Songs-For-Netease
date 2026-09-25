"""端到端冒烟测试：不连直播间，把「解析指令 -> 搜索 -> 取地址 -> 播放 -> 队列」跑一遍。

用法（先确保 API 服务已启动）：
    .venv\\Scripts\\python.exe tests\\smoke_test.py
    .venv\\Scripts\\python.exe tests\\smoke_test.py --no-audio   只测逻辑不放出声音
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contextlib

from common import DEFAULT_CONFIG, ensure_console_utf8  # noqa: E402
from danmaku_bot import build_request_regex, parse_command  # noqa: E402
from netease_api import NeteaseClient, NeteaseError, Song  # noqa: E402
from player import AudioPlayer  # noqa: E402
from song_queue import QueueItem, SongQueue  # noqa: E402

PASS = "[通过]"
FAIL = "[失败]"
results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    print(f"{PASS if ok else FAIL} {label}" + (f"  —— {detail}" if detail else ""))
    return ok


# ------------------------------------------------------------------ 1. 指令解析


def test_parsing() -> None:
    print("\n=== 1. 指令解析 ===")
    request_re = build_request_regex(DEFAULT_CONFIG["request"]["prefixes"])

    cases = [
        ("点歌 晴天", "request", "晴天"),
        ("点歌晴天", "request", "晴天"),
        ("点歌:晴天", "request", "晴天"),
        ("点歌：晴天", "request", "晴天"),
        ("点歌，晴天", "request", "晴天"),
        ("!点歌 起风了", "request", "起风了"),
        ("/点歌 卡农", "request", "卡农"),
        ("点歌   稻香   ", "request", "稻香"),
        ("切歌", "skip", ""),
        ("下一首", "skip", ""),
        ("暂停", "pause", ""),
        ("继续", "resume", ""),
        ("停止", "stop", ""),
        ("停止播放", "stop", ""),
        ("关闭音乐", "stop", ""),
        ("关掉音乐", "stop", ""),
        ("关音乐", "stop", ""),
        ("别放了", "stop", ""),
        ("队列", "queue", ""),
        ("歌单", "queue", ""),
        ("清空", "clear", ""),
        ("帮助", "help", ""),
        ("音量 50", "volume", "50"),
        ("音量50", "volume", "50"),
        ("音量+", "volume", "+10"),
        ("音量-", "volume", "-10"),
    ]
    for text, kind, arg in cases:
        command = parse_command(text, request_re, "测试", 1)
        ok = command is not None and command.kind == kind and command.arg == arg
        check(f"解析 {text!r} -> {kind}", ok, "" if ok else f"实际得到 {command!r}")

    for text in ("你好啊", "点歌", "随便说说", ""):
        command = parse_command(text, request_re, "测试", 1)
        check(f"忽略无关弹幕 {text!r}", command is None, "" if command is None else f"误判为 {command!r}")

    # 「点歌 停止」应该当成一首叫"停止"的歌，不能被停止指令抢走
    command = parse_command("点歌 停止", request_re, "测试", 1)
    check(
        "「点歌 停止」仍按点歌处理",
        command is not None and command.kind == "request" and command.arg == "停止",
        f"实际得到 {command!r}",
    )


def test_room_code() -> None:
    """直播间号识别：纯数字、各种网址形态、分享文案、全角数字、错误输入。"""
    print("\n=== 1b. 直播间号识别 ===")
    from roomcode import parse_room_code

    expected_id = 1934302095
    accept = [
        "1934302095",
        "  1934302095  ",
        "https://live.bilibili.com/1934302095",
        "http://live.bilibili.com/1934302095",
        "https://live.bilibili.com/1934302095?broadcast_type=0&is_room_feed=1",
        "https://live.bilibili.com/1934302095#/",
        "live.bilibili.com/1934302095",
        "www.live.bilibili.com/1934302095",
        "https://live.bilibili.com/blanc/1934302095",
        "https://live.bilibili.com/h5/1934302095",
        "https://live.bilibili.com/p/html5/1934302095",
        "https://m.bilibili.com/live/1934302095",
        "https://live.bilibili.com/?room_id=1934302095",
        "【哔哩哔哩】我的直播间 https://live.bilibili.com/1934302095?share_source=copy_web",
        "直播间号：1934302095",
        "房间 1934302095",
        "１９３４３０２０９５",
    ]
    for text in accept:
        result = parse_room_code(text, resolve_short=False)
        check(
            f"识别 {text[:52]!r}",
            result.ok and result.room_id == expected_id,
            f"-> {result.room_id}（{result.reason}）",
        )

    reject = [
        "",
        "   ",
        "hello world",
        "https://live.bilibili.com/",
        "https://live.bilibili.com/abc",
        "0",
        "https://space.bilibili.com/1234567",  # 用户空间，不是直播间
    ]
    for text in reject:
        result = parse_room_code(text, resolve_short=False)
        check(f"拒绝 {text[:40]!r}", not result.ok, f"原因：{result.reason}")

    # 用户空间地址要给专门的提示，不能只说"没找到"
    space = parse_room_code("https://space.bilibili.com/1234567", resolve_short=False)
    check("用户空间地址给出专门提示", "用户空间" in space.reason, space.reason)

    # 短链：不联网时要说清楚需要联网
    short = parse_room_code("https://b23.tv/AbCdEf", resolve_short=False)
    check("短链在离线时报明需要联网", not short.ok and "联网" in short.reason, short.reason)

    # 短链展开后的提取逻辑（打桩，不真的联网）
    import roomcode

    original = roomcode.resolve_short_link
    try:
        roomcode.resolve_short_link = lambda url, timeout=8.0: "https://live.bilibili.com/1934302095?share_source=copy_web"
        expanded = roomcode.parse_room_code("https://b23.tv/AbCdEf")
        check("短链展开后能提取房间号", expanded.ok and expanded.room_id == expected_id, f"-> {expanded.room_id}")

        roomcode.resolve_short_link = lambda url, timeout=8.0: "https://b23.tv/AbCdEf"
        stale = roomcode.parse_room_code("https://b23.tv/AbCdEf")
        check("失效短链说「已失效」而不是含糊的「不是直播间」", not stale.ok and "失效" in stale.reason, stale.reason)

        roomcode.resolve_short_link = lambda url, timeout=8.0: None
        failed = roomcode.parse_room_code("https://b23.tv/AbCdEf")
        check("短链展开失败有提示", not failed.ok and "失败" in failed.reason, failed.reason)
    finally:
        roomcode.resolve_short_link = original


# ------------------------------------------------------------------ 2. 队列逻辑


def test_queue() -> None:
    print("\n=== 2. 点歌队列 ===")
    song_a = Song(id=1, name="A", artists="X", duration_ms=180000)
    song_b = Song(id=2, name="B", artists="Y", duration_ms=200000)

    q = SongQueue(max_size=3)
    ok, msg = q.add(QueueItem(song=song_a, requester="甲"))
    check("入队成功", ok and len(q) == 1, msg)

    ok, msg = q.add(QueueItem(song=song_a, requester="乙"))
    check("重复歌曲被拒绝", not ok and len(q) == 1, msg)

    q.add(QueueItem(song=song_b, requester="丙"))
    first = q.pop_next()
    check("取出顺序为 FIFO", first is not None and first.song.id == 1)
    check("正在播放已标记", q.now_playing is not None and q.now_playing.song.id == 1)

    ok, msg = q.add(QueueItem(song=Song(id=1, name="A", artists="X"), requester="丁"))
    check("正在播放的歌不能重复点", not ok, msg)

    q.clear()
    check("清空队列", len(q) == 0 and q.now_playing is not None)
    q.finish_current()
    check("播完后状态归零", q.now_playing is None and q.is_empty)

    q2 = SongQueue(max_size=2)
    q2.add(QueueItem(song=Song(id=10, name="X", artists="Z")))
    q2.add(QueueItem(song=Song(id=11, name="Y", artists="Z")))
    ok, msg = q2.add(QueueItem(song=Song(id=12, name="Z", artists="Z")))
    check("队列上限生效", not ok, msg)


# ------------------------------------------------------------------ 3. 指令权限


def test_permissions() -> None:
    """权限判断：房主 / admin_uids / allow_everyone_control / 普通观众。"""
    print("\n=== 3. 指令权限 ===")
    from danmaku_bot import SongBot

    base = {
        "bilibili": {"room_id": 1},
        "netease": {"api_base": "http://127.0.0.1:3000", "cookie": "", "level": "exhigh"},
        "player": {"backend": "null", "volume": 0, "prefetch": False},
        "request": {
            "prefixes": ["点歌"],
            "max_queue": 3,
            "user_cooldown_sec": 0,
            "max_duration_sec": 0,
            "admin_uids": [555],
            "allow_everyone_control": False,
        },
    }

    bot = SongBot(base)
    bot.room_owner_uid = 999

    check("房主有控制权限", bot.is_admin(999))
    check("admin_uids 里的 uid 有权限", bot.is_admin(555))
    check("普通观众没有权限", not bot.is_admin(12345))
    check("uid=0（匿名）没有权限", not bot.is_admin(0))

    everyone = {**base, "request": {**base["request"], "allow_everyone_control": True}}
    bot2 = SongBot(everyone)
    bot2.room_owner_uid = 999
    check("allow_everyone_control 打开后人人可控制", bot2.is_admin(12345) and bot2.is_admin(0))

    bot._cooldowns.clear()
    check("首次点歌无冷却", bot._check_cooldown(777) == 0.0)
    bot.cooldown_sec = 30
    bot._mark_cooldown(777)
    remaining = bot._check_cooldown(777)
    check("点歌后进入冷却", 0 < remaining <= 30, f"剩余 {remaining:.1f}s")
    check("冷却按人隔离", bot._check_cooldown(778) == 0.0)

    # 昵称兜底：未登录 B站时 B站会把观众昵称藏起来，uname 是空的
    request_queue = bot._request_queue
    while not request_queue.empty():
        request_queue.get_nowait()

    bot.handle_danmaku("点歌 测试", "", 4242)
    command = request_queue.get_nowait()
    check("昵称空时兜底成「用户<uid>」", command.uname == "用户4242", command.uname)

    bot.handle_danmaku("点歌 测试", "   ", 0)
    command = request_queue.get_nowait()
    check("昵称和 uid 都为空时兜底成「匿名」", command.uname == "匿名", command.uname)

    bot.handle_danmaku("点歌 测试", "真实昵称", 1)
    command = request_queue.get_nowait()
    check("有昵称时正常使用", command.uname == "真实昵称", command.uname)

    # 停止播放：既停当前，也清掉待播（和「清空」的区别就在这里）
    from song_queue import QueueItem

    bot.queue.clear()
    bot.queue.finish_current()
    bot.queue.add(QueueItem(song=Song(id=901, name="正在播", artists="X"), requester="甲"))
    bot.queue.pop_next()  # 让它变成"正在播放"
    bot.queue.add(QueueItem(song=Song(id=902, name="待播", artists="X"), requester="乙"))
    check("停止前：有 1 首正在播 + 1 首待播", bot.queue.now_playing is not None and len(bot.queue) == 1)

    cleared = bot.stop_playback()
    check(
        "停止播放会清空待播队列",
        cleared == 1 and len(bot.queue) == 0,
        f"清掉 {cleared} 首，剩 {len(bot.queue)} 首",
    )

    # 「清空」只清待播，不动当前这首，两者行为要区分开
    bot.queue.finish_current()
    bot.queue.add(QueueItem(song=Song(id=903, name="正在播2", artists="X"), requester="甲"))
    bot.queue.pop_next()
    bot.queue.add(QueueItem(song=Song(id=904, name="待播2", artists="X"), requester="乙"))
    bot.queue.clear()
    check(
        "「清空」保留正在播放的那首",
        bot.queue.now_playing is not None and len(bot.queue) == 0,
        f"now_playing={bot.queue.now_playing.song.name if bot.queue.now_playing else None}",
    )
    bot.queue.finish_current()


# ------------------------------------------------------------------ 4. 网易云接口


def test_netease() -> tuple[list[Song], NeteaseClient] | None:
    print("\n=== 4. 网易云接口 ===")
    client = NeteaseClient()
    if not check("API 服务可达", client.ping(), "请先启动 start_netease_api.cmd"):
        return None

    try:
        songs = client.search("晴天", limit=5)
    except NeteaseError as exc:
        check("搜索接口", False, str(exc))
        return None
    check("搜索接口", bool(songs), f"返回 {len(songs)} 条，第一条：{songs[0].display}" if songs else "无结果")
    if songs:
        check("歌曲字段完整", bool(songs[0].name and songs[0].artists and songs[0].id > 0), songs[0].display)

    detail_ok = True
    try:
        detail = client.song_detail([songs[0].id])
        detail_ok = bool(detail)
    except NeteaseError as exc:
        detail_ok = False
        print("   详情接口出错：", exc)
    check("歌曲详情接口", detail_ok)

    lyric_ok = True
    try:
        lyric = client.lyric(songs[0].id)
        lyric_ok = isinstance(lyric, str)
    except NeteaseError as exc:
        lyric_ok = False
        print("   歌词接口出错：", exc)
    check("歌词接口", lyric_ok)

    account = None
    with contextlib.suppress(NeteaseError):
        account = client.current_account()
    if account:
        check("登录状态", True, f"{account['nickname']}，VIP={account['is_vip']}")
    else:
        check("登录状态", True, "未登录（VIP 歌曲会拿不到完整地址，属正常）")

    return songs, client


# ------------------------------------------------------------- 4b. 空闲随机播放


def test_autoplay() -> None:
    """空闲随机播放：随机源可用、会避开最近放过的、能进队列、会被「停止」关掉。"""
    print("\n=== 4b. 空闲随机播放 ===")
    from common import DEFAULT_CONFIG, deep_merge
    from danmaku_bot import AUTOPLAY_REQUESTER, SongBot

    base = deep_merge(
        DEFAULT_CONFIG,
        {
            "netease": {"api_base": "http://127.0.0.1:3000", "cookie": "", "level": "exhigh"},
            "player": {"backend": "null", "volume": 0, "prefetch": False},
            "autoplay": {"enabled": False, "source": "auto", "avoid_repeat": 40},
        },
    )
    bot = SongBot(base)

    check("默认是关闭的", bot.autoplay_enabled is False)

    bot.set_autoplay(True)
    check("能打开", bot.autoplay_enabled is True)

    # 随机源：多取几次都应该有歌，且互不重复
    picked: list[int] = []
    for _ in range(5):
        song = bot.next_random_song()
        if song is None:
            break
        picked.append(song.id)
        bot._autoplay_history.append(song.id)
    check("能取到随机歌", len(picked) >= 3, f"取了 {len(picked)} 首")
    check("不会重复取同一首", len(picked) == len(set(picked)), f"{picked}")

    # 补歌：应该带着「自动播放」标记进队列
    bot.queue.clear()
    bot.queue.finish_current()
    bot._autoplay_history.clear()
    filled = bot._autoplay_fill()
    check("空闲时能补一首进队列", filled and len(bot.queue) == 1, f"队列 {len(bot.queue)} 首")
    check(
        "补进来的歌标记为「自动播放」",
        bot.queue.snapshot() and bot.queue.snapshot()[0].requester == AUTOPLAY_REQUESTER,
        AUTOPLAY_REQUESTER,
    )
    check("补歌计数增加", bot.autoplay_picked == 1, str(bot.autoplay_picked))

    # 观众点的歌应该排在随机歌前面（FIFO，谁先来谁先放）
    bot.request_song(Song(id=888001, name="观众点的", artists="X", duration_ms=1000), requester="观众甲")
    names = [i.song.name for i in bot.queue.snapshot()]
    check("观众点歌排在随机歌后面（不会插队）", names[-1] == "观众点的", str(names))

    # 「停止播放」必须把随机播放一起关掉，否则停完 2 秒又响
    bot.set_autoplay(True)
    bot.stop_playback()
    check("「停止播放」会一并关闭随机播放", bot.autoplay_enabled is False)
    check("「停止播放」清空了待播", len(bot.queue) == 0, f"剩 {len(bot.queue)} 首")

    # 关键词来源也要能用（不依赖登录）
    bot.autoplay_source = "keywords"
    bot.autoplay_keywords = ["轻音乐"]
    kw_song = bot.next_random_song()
    check("关键词来源可用", kw_song is not None, f"{kw_song.name} - {kw_song.artists}" if kw_song else "取不到")


# ------------------------------------------------------------------ 5. 播放地址


def _find_playable(client: NeteaseClient) -> tuple[Song, str, str] | None:
    """找一首能拿到播放地址的歌。

    优先用 .tmp/playable.txt（由 probe_free.py 生成的缓存，放在 .tmp 里不污染源码目录），
    没有就现场搜几个关键词试试。
    """
    cache = Path(__file__).resolve().parent.parent / ".tmp" / "playable.txt"
    candidates: list[Song] = []

    if cache.is_file():
        for line in cache.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].isdigit():
                candidates.append(Song(id=int(parts[0]), name=parts[1], artists="", duration_ms=180000))

    for keyword in ("纯音乐", "轻音乐", "白噪音"):
        with contextlib.suppress(NeteaseError):
            candidates.extend(client.search(keyword, limit=6))

    for song in candidates:
        for level in ("standard", "higher", "exhigh"):
            client.level = level
            try:
                result = client.song_url(song.id)
            except NeteaseError:
                continue
            if result.ok:
                return song, level, result.url or ""
    return None


def test_play_url(client: NeteaseClient) -> tuple[Song, str] | None:
    print("\n=== 5. 播放地址解析 ===")
    found = _find_playable(client)
    if not found:
        check("找到可播放歌曲", False, "当前未登录且没找到免费可播歌曲（登录后可跳过此项）")
        return None

    song, level, url = found
    check("找到可播放歌曲", True, f"{song.display} (id={song.id}) 音质={level}")
    check("地址是 http(s)", url.startswith("http"), url[:80])
    check("可以真正下载到音频数据", _probe_download(url))
    return song, url


def _probe_download(url: str) -> bool:
    import requests

    try:
        with requests.get(url, stream=True, timeout=20) as resp:
            if resp.status_code != 200:
                print(f"   HTTP {resp.status_code}")
                return False
            chunk = next(resp.iter_content(chunk_size=64 * 1024), b"")
            print(f"   首块 {len(chunk)} 字节，Content-Type={resp.headers.get('Content-Type')}")
            return len(chunk) > 0
    except requests.RequestException as exc:
        print("   下载出错：", exc)
        return False


# ------------------------------------------------------------------ 6. 播放器


def test_player(url: str, duration: float = 6.0, enabled: bool = True, backend: str | None = None) -> None:
    print("\n=== 6. 播放器 ===")
    if not enabled:
        check("播放器实测", True, "已用 --no-audio 跳过")
        return

    backends = AudioPlayer.describe_backends()
    check("至少一个后端可用", any(backends[n] for n in ("mpv", "ffplay", "pygame", "wmp")))

    player = AudioPlayer(volume=30, backend=backend) if backend else AudioPlayer(volume=30)
    print(f"   使用后端：{player.backend_name}  (支持暂停={player.supports_pause})")

    outcome: dict[str, object] = {}

    def run() -> None:
        started = time.time()
        result = player.play(url, duration_hint=duration)
        outcome["result"] = result
        outcome["elapsed"] = time.time() - started

    worker = threading.Thread(target=run, daemon=True)
    worker.start()

    # mpv 起播很快，给 5 秒就够；pygame 要先下载，多等一会儿
    settle = 5.0 if player.backend_name == "mpv" else max(duration + 10, 20)
    time.sleep(settle)

    started_ok = worker.is_alive() or float(outcome.get("elapsed", 0) or 0) > 1.0
    check("播放器能启动并播放", started_ok, f"等待 {settle:.0f}s 后状态：{'播放中' if worker.is_alive() else '已结束'}")

    if worker.is_alive() and player.backend_name == "mpv":
        paused = player.pause()
        check("暂停生效（需要 mpv IPC）", paused, "命名管道控制")
        time.sleep(1.0)
        check("继续生效（需要 mpv IPC）", player.resume(), "命名管道控制")
        before = player.volume
        changed = player.set_volume(before - 15)
        check(
            "实时调音量（需要 mpv IPC）",
            changed and player.volume == before - 15,
            f"{before} -> {player.volume}",
        )

    if worker.is_alive():
        print("   切歌")
        player.skip()
        worker.join(timeout=10)

    elapsed = float(outcome.get("elapsed", 0) or 0)
    if worker.is_alive():
        check("切歌能中断播放", False, "线程没停下来")
    else:
        check(
            "切歌被正确识别为「打断」而不是「播完」",
            outcome.get("result") is False,
            f"play() 返回 {outcome.get('result')}（期望 False），总耗时 {elapsed:.1f}s",
        )


def test_prefetch(url: str) -> None:
    """验证预下载：提交任务 -> 后台下完 -> take() 能拿到本地文件。"""
    print("\n=== 7. 预下载 ===")
    from player import AudioPrefetcher

    prefetcher = AudioPrefetcher(cache_dir="cache", log=lambda m: print(f"   {m}"))
    if not check("提交预下载任务", prefetcher.submit("smoketest", url)):
        return

    deadline = time.time() + 60
    path = None
    while time.time() < deadline:
        path = prefetcher.take("smoketest")
        if path:
            break
        time.sleep(0.5)

    if path is None:
        check("预下载完成并可取回", False, "60 秒内没下完")
        return
    size = path.stat().st_size
    check("预下载完成并可取回", size > 100_000, f"{path.name}  {size / 1024 / 1024:.1f}MB")
    path.unlink(missing_ok=True)
    check("取回后缓存被消费", prefetcher.take("smoketest") is None)


# ------------------------------------------------------------------ main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-audio", action="store_true", help="跳过真实播放")
    parser.add_argument("--backend", help="指定播放后端：mpv/ffplay/pygame/wmp/null")
    args = parser.parse_args()

    ensure_console_utf8()
    print("=" * 66)
    print("  端到端冒烟测试")
    print("=" * 66)

    test_parsing()
    test_room_code()
    test_queue()
    test_permissions()

    netease = test_netease()
    url = None
    if netease:
        songs, client = netease
        test_autoplay()
        found = test_play_url(client)
        if found:
            url = found[1]

    if url:
        test_player(url, enabled=not args.no_audio, backend=args.backend)
        test_prefetch(url)
    else:
        print("\n=== 6. 播放器 ===")
        check("播放器实测", True, "没有可播放地址，跳过真实播放；逻辑部分已验证")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 66)
    print(f"  结果：{passed}/{total} 通过")
    failed = [label for label, ok, _ in results if not ok]
    if failed:
        print("  未通过：")
        for label in failed:
            print(f"    - {label}")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
