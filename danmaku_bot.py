"""B站弹幕点歌机器人。

链路：B站直播弹幕 -> 解析"点歌 XXX" -> 网易云搜索 -> 取播放地址 -> 本地播放。

线程模型：
    主线程            asyncio 事件循环，跑 blivedm 收弹幕；只做解析和入队，绝不阻塞
    解析线程(resolver) 处理点歌请求：搜索、校验时长、入队
    播放线程(player)  从队列取歌、解析播放地址、调用播放器阻塞播放

启动：
    python danmaku_bot.py               正常监听直播间
    python danmaku_bot.py --stdin       从键盘输入模拟弹幕，用来测试整条链路
    python danmaku_bot.py --room 12345  临时指定直播间号
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import queue
import random
import re
import sys
import threading
import time
from collections import deque
from typing import Any

import aiohttp
import blivedm
import requests

from common import load_config, save_config, setup_logging
from netease_api import QUALITY_LEVELS, NeteaseClient, NeteaseError, Song
from player import AudioPlayer, AudioPrefetcher
from roomcode import describe_input_help, parse_room_code
from song_queue import QueueItem, SongQueue
from webui import ControlPanel

# --------------------------------------------------------------------- 指令解析

HELP_TEXT = """点歌指令一览：
  点歌 <歌名>      点一首歌（所有人可用）
  队列 / 歌单      查看当前队列
  停止 / 关闭音乐   立即停止播放并清空待播队列（管理员）
  切歌 / 下一首    跳过当前歌曲（管理员）
  暂停 / 继续      暂停或继续播放（管理员）
  音量 <0-100>     设置音量，也支持 音量+ / 音量-（管理员）
  清空             只清空等待队列，当前这首继续放完（管理员）
  帮助             显示这条信息"""

# 空闲随机播放进来的歌，点歌人显示成这个，方便和观众点的区分
AUTOPLAY_REQUESTER = "自动播放"

CONTROL_ALIASES: dict[str, str] = {
    "切歌": "skip",
    "下一首": "skip",
    "下一曲": "skip",
    "跳过": "skip",
    "暂停": "pause",
    "继续": "resume",
    "播放": "resume",
    "停止": "stop",
    "停止播放": "stop",
    "停": "stop",
    "关闭音乐": "stop",
    "关掉音乐": "stop",
    "关音乐": "stop",
    "别放了": "stop",
    "队列": "queue",
    "歌单": "queue",
    "点歌列表": "queue",
    "清空": "clear",
    "清空队列": "clear",
    "帮助": "help",
    "点歌帮助": "help",
    "状态": "status",
}

_VOLUME_RE = re.compile(r"^音量\s*([+-])?\s*(\d{1,3})?$")


class Command:
    """一条解析出来的指令。"""

    __slots__ = ("kind", "arg", "uname", "uid")

    def __init__(self, kind: str, arg: str = "", uname: str = "", uid: int = 0) -> None:
        self.kind = kind
        self.arg = arg
        self.uname = uname
        self.uid = uid

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Command({self.kind!r}, {self.arg!r}, {self.uname!r})"


def build_request_regex(prefixes: list[str]) -> re.Pattern[str]:
    """把配置里的前缀编译成正则。

    允许 "点歌晴雯" / "点歌 晴天" / "点歌:晴天" / "点歌：晴天" / "点歌，晴天" 等写法。
    """
    cleaned = [re.escape(p.strip()) for p in prefixes if p and p.strip()]
    # 长前缀放前面，避免 "点歌" 抢先匹配 "!点歌"
    cleaned.sort(key=len, reverse=True)
    if not cleaned:
        cleaned = [re.escape("点歌")]
    alternation = "|".join(cleaned)
    # 前缀后允许空白和常见分隔符
    return re.compile(rf"^\s*(?:{alternation})\s*[:：,，、\-–—]?\s*(.+?)\s*$", re.IGNORECASE)


def parse_command(text: str, request_re: re.Pattern[str], uname: str = "", uid: int = 0) -> Command | None:
    """把一条弹幕解析成 Command，认不出来就返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None

    # 控制在最前面判断，避免 "点歌 帮助" 之类被误判
    key = raw.strip("!！/／ ").strip()
    if key in CONTROL_ALIASES:
        return Command(CONTROL_ALIASES[key], "", uname, uid)

    volume_match = _VOLUME_RE.match(key)
    if volume_match:
        sign, number = volume_match.groups()
        if number:
            arg = ("-" if sign == "-" else "") + number
        elif sign == "+":
            arg = "+10"
        elif sign == "-":
            arg = "-10"
        else:
            return Command("volume_query", "", uname, uid)
        return Command("volume", arg, uname, uid)

    match = request_re.match(raw)
    if match:
        song_name = match.group(1).strip()
        if song_name:
            return Command("request", song_name, uname, uid)
    return None


# ----------------------------------------------------------------- 弹幕回发（可选）


class BiliDanmakuSender:
    """可选的弹幕回发，让观众看到"已加入队列"之类的反馈。

    需要 SESSDATA + bili_jct（B站 Cookie 里的两个字段）。
    """

    SEND_URL = "https://api.live.bilibili.com/msg/send"

    def __init__(self, sessdata: str, bili_jct: str, buvid3: str = "", min_interval: float = 3.0) -> None:
        self.enabled = bool(sessdata and bili_jct)
        self._cookies = {
            k: v
            for k, v in (("SESSDATA", sessdata), ("bili_jct", bili_jct), ("buvid3", buvid3))
            if v
        }
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
                "Referer": "https://live.bilibili.com/",
            }
        )
        self._min_interval = min_interval
        self._last_sent = 0.0
        self._lock = threading.Lock()

    def send(self, room_id: int, text: str) -> bool:
        """发一条弹幕，失败只返回 False 不抛异常。"""
        if not self.enabled or not text:
            return False
        with self._lock:
            if time.time() - self._last_sent < self._min_interval:
                return False
            self._last_sent = time.time()
        try:
            resp = self._session.post(
                self.SEND_URL,
                data={
                    "bubble": "0",
                    "msg": text[:20],  # B站弹幕长度限制
                    "color": "16777215",
                    "mode": "1",
                    "fontsize": "25",
                    "rnd": str(int(time.time())),
                    "roomid": str(room_id),
                    "csrf": self._cookies.get("bili_jct", ""),
                    "csrf_token": self._cookies.get("bili_jct", ""),
                },
                cookies=self._cookies,
                timeout=10,
            )
            payload = resp.json()
        except (requests.RequestException, ValueError):
            return False
        return payload.get("code") == 0


# ------------------------------------------------------------------------- 机器人


class SongBot:
    """把弹幕、搜索、队列、播放串起来。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.log = setup_logging()

        netease_conf = config["netease"]
        # 搜索和取播放地址分别用独立客户端，避免 requests.Session 跨线程争用
        self.search_client = NeteaseClient(
            base_url=netease_conf["api_base"],
            cookie=netease_conf.get("cookie", ""),
            level=netease_conf.get("level", "exhigh"),
        )
        self.play_client = NeteaseClient(
            base_url=netease_conf["api_base"],
            cookie=netease_conf.get("cookie", ""),
            level=netease_conf.get("level", "exhigh"),
        )
        # 预下载线程单独用一份客户端，避免和上面两个抢 Session
        self.prefetch_client = NeteaseClient(
            base_url=netease_conf["api_base"],
            cookie=netease_conf.get("cookie", ""),
            level=netease_conf.get("level", "exhigh"),
        )

        request_conf = config["request"]
        player_conf = config["player"]
        autoplay_conf = config.get("autoplay") or {}
        self.request_re = build_request_regex(request_conf["prefixes"])
        self.max_queue = int(request_conf["max_queue"])
        self.cooldown_sec = float(request_conf["user_cooldown_sec"])
        self.max_duration_sec = float(request_conf["max_duration_sec"])
        self.admin_uids = {int(u) for u in request_conf.get("admin_uids", []) if str(u).strip()}
        self.allow_everyone_control = bool(request_conf.get("allow_everyone_control", False))

        # 空闲自动随机播放（没人点歌时填充音乐）
        self.autoplay_enabled = bool(autoplay_conf.get("enabled", False))
        self.autoplay_source = str(autoplay_conf.get("source", "auto")).lower()
        self.autoplay_keywords = [
            str(k).strip() for k in (autoplay_conf.get("keywords") or []) if str(k).strip()
        ] or ["轻音乐", "纯音乐"]
        self.autoplay_avoid_repeat = max(0, int(autoplay_conf.get("avoid_repeat", 40)))
        self.autoplay_retry_delay = max(1.0, float(autoplay_conf.get("retry_delay_sec", 5)))
        self._autoplay_history: deque[int] = deque(maxlen=max(1, self.autoplay_avoid_repeat))
        self.autoplay_picked = 0

        self.queue = SongQueue(max_size=self.max_queue)
        self.player = AudioPlayer(
            backend=player_conf.get("backend", "auto"),
            volume=int(player_conf.get("volume", 70)),
            cache_dir=player_conf.get("cache_dir", "cache"),
            log=lambda m: self.log.info("[播放] %s", m),
        )

        # 只有"必须先有完整文件"的播放器才需要预下载才有意义
        self.prefetch_enabled = bool(player_conf.get("prefetch", True)) and self.player.backend.needs_local_file
        self.prefetcher = AudioPrefetcher(
            cache_dir=player_conf.get("cache_dir", "cache"),
            log=lambda m: self.log.info("[预下载] %s", m),
        )
        self._prefetch_resolving: set[int] = set()
        self._prefetch_lock = threading.Lock()

        self._cooldowns: dict[int, float] = {}
        self._request_queue: queue.Queue[Command] = queue.Queue()
        self._running = threading.Event()
        self._running.set()
        self.room_id = 0
        self.room_owner_uid = 0

        # 给网页面板看的运行状态
        self.connected = False
        self.paused = False
        self.current_started_at: float | None = None
        self.played_count = 0
        self.request_count = 0
        self.netease_account: str = ""
        self.netease_ok = False

        # 弹幕统计：用来回答"到底有没有收到弹幕"
        self.danmaku_received = 0
        self.danmaku_matched = 0
        self.last_danmaku_at: float | None = None
        self.connected_since: float | None = None
        self.recent_danmaku: deque[dict[str, Any]] = deque(maxlen=60)
        self.log_all_danmaku = bool(request_conf.get("log_all_danmaku", False))

        self.panel: ControlPanel | None = None

        self.replier = BiliDanmakuSender(
            sessdata=config["bilibili"].get("sessdata", ""),
            bili_jct=config["bilibili"].get("bili_jct", ""),
            buvid3=config["bilibili"].get("buvid3", ""),
        )

    # ------------------------------------------------------------ 状态快照

    def status_snapshot(self) -> dict[str, Any]:
        """汇总一份当前状态，给网页面板用。"""
        now = self.queue.now_playing
        elapsed = 0.0
        if now is not None and self.current_started_at is not None and not self.paused:
            elapsed = max(0.0, time.time() - self.current_started_at)

        now_playing = None
        if now is not None:
            now_playing = {
                "name": now.song.name,
                "artists": now.song.artists,
                "duration_sec": now.song.duration_ms / 1000,
                "requester": now.requester,
                "quality": now.quality or "",
            }

        return {
            "connected": self.connected,
            "room_id": self.room_id,
            "netease_ok": self.netease_ok,
            "netease_account": self.netease_account,
            "backend": self.player.backend_name,
            "supports_pause": self.player.supports_pause,
            "volume": self.player.volume,
            "paused": self.paused,
            "now_playing": now_playing,
            "now_elapsed": elapsed,
            "queue": [
                {
                    "name": item.song.name,
                    "artists": item.song.artists,
                    "duration": item.song.duration_text,
                    "requester": item.requester,
                }
                for item in self.queue.snapshot()
            ],
            "stats": {
                "played": self.played_count,
                "requests": self.request_count,
                "danmaku_received": self.danmaku_received,
                "danmaku_matched": self.danmaku_matched,
                "autoplay_picked": self.autoplay_picked,
                "seconds_since_danmaku": (
                    round(time.time() - self.last_danmaku_at, 1) if self.last_danmaku_at else None
                ),
            },
            "autoplay": {
                "enabled": self.autoplay_enabled,
                "source": self.autoplay_source,
                "keywords": list(self.autoplay_keywords),
            },
            "danmaku": list(self.recent_danmaku),
            "logs": [],
        }

    def note_danmaku(self, uname: str, text: str, matched: bool) -> None:
        """记录一条收到的弹幕（不管是不是点歌指令）。

        主程序只对点歌指令有反应，但用户在排查"到底有没有收到弹幕"时，
        需要知道原始弹幕的量。所以这里单独统计一份，网页面板能看到。
        """
        self.danmaku_received += 1
        self.last_danmaku_at = time.time()
        if matched:
            self.danmaku_matched += 1
        self.recent_danmaku.append(
            {
                "time": time.strftime("%H:%M:%S"),
                "uname": uname,
                "text": text[:120],
                "matched": matched,
            }
        )

    # ------------------------------------------------------------ 权限与冷却

    def is_admin(self, uid: int) -> bool:
        if self.allow_everyone_control:
            return True
        if uid and uid == self.room_owner_uid:
            return True
        return uid in self.admin_uids

    def _check_cooldown(self, uid: int) -> float:
        """返回还需要等待的秒数，0 表示可以点歌。"""
        if self.cooldown_sec <= 0:
            return 0.0
        last = self._cooldowns.get(uid, 0.0)
        remain = self.cooldown_sec - (time.time() - last)
        return max(0.0, remain)

    def _mark_cooldown(self, uid: int) -> None:
        self._cooldowns[uid] = time.time()

    # ------------------------------------------------------------ 指令分发

    def handle_danmaku(self, text: str, uname: str, uid: int) -> None:
        """收到一条弹幕。这里必须尽快返回，耗时的活交给其它线程。

        注意：没登录 B站（没配 sessdata）时，B站会把别人的昵称藏起来，
        uname 会是空的（会收到一条 LOG_IN_NOTICE 说明这件事），
        所以这里兜底成"用户<uid>"，免得点歌记录一片空白。
        """
        uname = (uname or "").strip() or (f"用户{uid}" if uid else "匿名")
        raw_text = (text or "").strip()
        command = parse_command(text, self.request_re, uname, uid)

        # 不管是不是点歌指令都记一笔，这样"到底有没有收到弹幕"一目了然
        self.note_danmaku(uname, raw_text, matched=command is not None)

        if command is None:
            # 默认不刷屏；想看到每一条弹幕就用 --verbose（或配置 log_all_danmaku）
            if self.log_all_danmaku:
                self.log.info("弹幕 %s：%s", uname, raw_text)
            return

        self.log.info("弹幕指令 %s(%s)：%s", uname, uid, raw_text)
        if command.kind == "request":
            self._request_queue.put(command)
        else:
            # 控制类指令都很快，直接在当前线程处理
            try:
                self._run_control(command)
            except Exception as exc:  # noqa: BLE001 - 不能让一条指令搞崩收弹幕
                self.log.exception("处理指令失败：%s", exc)

    def _reply(self, text: str) -> None:
        """把反馈发回直播间（未配置 Cookie 时只打日志）。"""
        if self.replier.enabled and self.room_id:
            self.replier.send(self.room_id, text)

    def stop_playback(self) -> int:
        """立刻停止播放，并把待播队列也清空。返回清掉的条数。

        和「清空」的区别：清空只是不再往下放、当前这首放完；
        停止是马上闭嘴。弹幕指令「停止 / 关闭音乐」和面板上的按钮都走这里。

        注意：这里会【顺手关掉空闲随机播放】。否则刚停完 2 秒，
        随机播放又接上一首，"停止"就白按了。
        """
        self.player.skip()
        cleared = self.queue.clear()
        if self.autoplay_enabled:
            self.set_autoplay(False)
            self.log.info("（空闲随机播放已一并关闭，需要的话在面板上重新打开）")
        return cleared

    def _run_control(self, command: Command) -> None:
        kind = command.kind

        if kind == "help":
            for line in HELP_TEXT.splitlines():
                self.log.info("%s", line)
            self._reply("点歌/队列/切歌/暂停/音量，详见控制台")
            return

        if kind == "queue":
            self.log.info("队列查询 by %s\n%s", command.uname, self.queue.describe())
            return

        if kind == "status":
            self.log.info(
                "状态：正在播放=%s 队列=%d首 音量=%d 播放器=%s",
                self.queue.now_playing.display if self.queue.now_playing else "无",
                len(self.queue),
                self.player.volume,
                self.player.backend_name,
            )
            return

        # 以下都是需要权限的
        if not self.is_admin(command.uid):
            self.log.info("忽略 %s 的 %s：没有权限", command.uname, kind)
            self._reply("只有房管可以操作哦～")
            return

        if kind == "skip":
            self.player.skip()
            self.log.info("%s 触发了切歌", command.uname)
            self._reply("已切歌")
        elif kind == "stop":
            cleared = self.stop_playback()
            self.log.info("%s 停止了播放（顺带清掉 %d 首待播）", command.uname, cleared)
            self._reply("已停止播放")
        elif kind == "pause":
            ok = self.player.pause()
            self.log.info("暂停：%s", "成功" if ok else "当前播放器不支持")
            self._reply("已暂停" if ok else "当前播放器不支持暂停")
        elif kind == "resume":
            ok = self.player.resume()
            self.log.info("继续：%s", "成功" if ok else "当前播放器不支持")
            self._reply("继续播放" if ok else "当前播放器不支持继续")
        elif kind == "clear":
            count = self.queue.clear()
            self.log.info("%s 清空了队列（%d 首）", command.uname, count)
            self._reply(f"已清空 {count} 首")
        elif kind == "volume":
            self._apply_volume(command)

    def _apply_volume(self, command: Command) -> None:
        arg = command.arg
        if arg.startswith("+"):
            value = self.player.volume_up(int(arg[1:]))
        elif arg.startswith("-"):
            value = self.player.volume_down(int(arg[1:]))
        else:
            value = int(arg)
            self.player.set_volume(value)
            value = self.player.volume
        self.log.info("音量 -> %d", value)
        self._reply(f"音量 {value}")

    # ------------------------------------------------------------ 点歌处理线程

    def _resolver_loop(self) -> None:
        """处理点歌请求：搜索 -> 查重 -> 入队。"""
        while self._running.is_set():
            try:
                command = self._request_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle_request(command)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("处理点歌请求出错：%s", exc)
            finally:
                self._request_queue.task_done()

    def _handle_request(self, command: Command) -> None:
        keyword = command.arg
        uid = command.uid

        remain = self._check_cooldown(uid)
        if remain > 0:
            self.log.info("%s 点歌太快，还需等 %.0f 秒", command.uname, remain)
            self._reply(f"{command.uname} 点歌太频繁，{remain:.0f}秒后再试")
            return

        songs, error = self.search_songs(keyword, limit=5)
        if error:
            self.log.error("搜索失败：%s", error)
            self._reply("搜索失败，稍后再试")
            return
        if not songs:
            self.log.info("没搜到：%s", keyword)
            self._reply(f"没找到《{keyword}》")
            return

        song = self.pick_song(keyword, songs)
        self.log.info(
            "命中：%s [%s] id=%s fee=%s",
            song.display,
            song.duration_text,
            song.id,
            song.fee,
        )

        ok, message = self.request_song(song, requester=command.uname, uid=uid)
        self.log.info("入队结果：%s（%s）", message, "成功" if ok else "失败")
        if ok:
            self._mark_cooldown(uid)
            self._reply(f"已点《{song.name}》")
        else:
            self._reply(message)

    # ------------------------------------------------- 给网页面板用的点歌接口

    def search_songs(self, keyword: str, limit: int = 8) -> tuple[list[Song], str]:
        """搜索歌曲。返回 (结果列表, 错误说明)；出错时列表为空。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return [], "关键词是空的"
        try:
            return self.search_client.search(keyword, limit=limit), ""
        except NeteaseError as exc:
            return [], str(exc)

    def request_song(
        self,
        song: Song,
        requester: str = "网页面板",
        uid: int = 0,
        bypass_cooldown: bool = True,
    ) -> tuple[bool, str]:
        """把一首已经确定好的歌加入队列。

        弹幕点歌和网页面板点歌共用这里，所以查重、队列上限、时长上限的规则完全一致。
        网页面板是主播自己操作的，默认绕过冷却（那是给观众防刷用的）。
        """
        if self.max_duration_sec > 0 and song.duration_ms / 1000 > self.max_duration_sec:
            return False, f"《{song.name}》时长 {song.duration_text}，超过上限 {self.max_duration_sec / 60:.0f} 分钟"

        item = QueueItem(song=song, requester=requester, requester_uid=uid)
        ok, message = self.queue.add(item)
        if ok:
            self.request_count += 1
            if not bypass_cooldown:
                self._mark_cooldown(uid)
        return ok, message

    @staticmethod
    def pick_song(keyword: str, songs: list[Song]) -> Song:
        """在多条搜索结果里挑一条：优先名字完全对得上的，否则用第一条。

        弹幕点歌和网页面板点歌共用，保证两边"点歌 晴天"选中的是同一首。
        """
        normalized = re.sub(r"[\s\-_()（）\[\]【】]", "", keyword).lower()
        for song in songs:
            if re.sub(r"[\s\-_()（）\[\]【】]", "", song.name).lower() == normalized:
                return song
        return songs[0]

    # ------------------------------------------------------------ 播放线程

    def _player_loop(self) -> None:
        """不断从队列取歌并播放；空闲时按需自动补一首随机歌。"""
        while self._running.is_set():
            if self.queue.now_playing is None and len(self.queue) == 0:
                if self.autoplay_enabled:
                    if not self._autoplay_fill():
                        # 取不到歌就缓一下，别死循环打接口
                        time.sleep(self.autoplay_retry_delay)
                    continue
                time.sleep(0.3)
                continue

            item = self.queue.pop_next()
            if item is None:
                time.sleep(0.3)
                continue

            try:
                self._play_item(item)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("播放出错：%s", exc)
            finally:
                self.queue.finish_current()

    # -------------------------------------------------------- 空闲随机播放

    def set_autoplay(self, enabled: bool) -> None:
        """开关空闲随机播放。"""
        self.autoplay_enabled = bool(enabled)
        self.log.info("空闲随机播放：%s", "已开启" if self.autoplay_enabled else "已关闭")

    def next_random_song(self) -> Song | None:
        """按配置的来源取一首随机歌，尽量避开最近放过的。"""
        candidates: list[Song] = []

        if self.autoplay_source in ("auto", "fm"):
            candidates = self._random_from_fm()
        if not candidates and self.autoplay_source in ("auto", "recommend"):
            candidates = self._random_from_recommend()
        if not candidates:
            candidates = self._random_from_keywords()

        if not candidates:
            return None

        recent = set(self._autoplay_history)
        fresh = [s for s in candidates if s.id not in recent]
        pool = fresh or candidates  # 全都听过就算了，总比不播强
        return random.choice(pool)

    def _random_from_fm(self) -> list[Song]:
        """私人FM。多调几次攒一点候选，一次只有 1~3 首。"""
        collected: list[Song] = []
        for _ in range(2):
            try:
                collected.extend(self.play_client.personal_fm())
            except NeteaseError as exc:
                self.log.debug("私人FM 取歌失败：%s", exc)
                break
            if len(collected) >= 4:
                break
        return collected

    def _random_from_recommend(self) -> list[Song]:
        try:
            return self.play_client.recommend_songs()
        except NeteaseError as exc:
            self.log.debug("每日推荐取歌失败：%s", exc)
            return []

    def _random_from_keywords(self) -> list[Song]:
        """兜底：从配置的关键词里随机挑一个搜，再从前面若干条里随机取。"""
        keyword = random.choice(self.autoplay_keywords)
        try:
            songs = self.play_client.search(keyword, limit=20)
        except NeteaseError as exc:
            self.log.debug("关键词随机取歌失败：%s", exc)
            return []
        return songs[:20]

    def _autoplay_fill(self) -> bool:
        """空闲时补一首歌进队列。成功返回 True。"""
        song = self.next_random_song()
        if song is None:
            self.log.warning("空闲随机播放：没能取到歌曲（检查一下网易云 API 服务）")
            return False

        ok, message = self.queue.add(QueueItem(song=song, requester=AUTOPLAY_REQUESTER, requester_uid=0))
        if ok:
            self._autoplay_history.append(song.id)
            self.autoplay_picked += 1
            self.log.info("空闲随机播放：《%s》 - %s", song.name, song.artists)
        else:
            self.log.debug("随机补歌没进队列：%s", message)
        return ok

    def _play_item(self, item: QueueItem) -> None:
        song = item.song
        self.log.info("=" * 58)
        self.log.info("开始播放：%s", song.display)
        source = "空闲随机" if item.requester == AUTOPLAY_REQUESTER else f"点播：{item.requester}"
        self.log.info("            %s  %s", song.duration_text, source)

        if not self._running.is_set():
            return

        try:
            play_url = self.play_client.song_url(song.id)
        except NeteaseError as exc:
            self.log.error("取播放地址失败：%s", exc)
            self._reply(f"《{song.name}》取地址失败，跳过")
            return

        if not play_url.ok:
            self.log.error("无法播放《%s》：%s", song.name, play_url.reason)
            self._reply(f"《{song.name}》{play_url.reason}")
            # 如果是音质太高导致拿不到地址，尝试降级重试
            fallback = self._try_lower_quality(song)
            if not fallback:
                return
            play_url = fallback

        url = NeteaseClient.upgrade_url_to_https(play_url.url or "")
        item.play_url = url
        item.quality = play_url.level
        self.log.info(
            "音质：%s  码率：%dkbps  大小：%.1fMB",
            play_url.level or "未知",
            play_url.br // 1000,
            play_url.size / 1024 / 1024,
        )

        # 拿走这首歌预下载好的文件（如果有），同时开始为下一首做准备
        cached = self.prefetcher.take(song.id) if self.prefetch_enabled else None
        self._spawn_prefetch_next()

        self.current_started_at = time.time()
        self.paused = False
        try:
            finished = self.player.play(url, duration_hint=song.duration_ms / 1000, cached_path=cached)
        finally:
            self.current_started_at = None
            self.played_count += 1

        if finished:
            self.log.info("播放结束：%s", song.display)
        else:
            self.log.info("播放被打断：%s", song.display)

    def _try_lower_quality(self, song: Song):
        """按音质从高到低再试几次，能拿到哪个算哪个。

        只在配置音质之下逐级降级——往上试（比如 exhigh 失败去试 hires）没有意义，
        那需要更高的会员等级。
        """
        configured = self.play_client.level
        try:
            index = QUALITY_LEVELS.index(configured)
        except ValueError:
            index = len(QUALITY_LEVELS) - 1

        for level in reversed(QUALITY_LEVELS[:index]):
            self.play_client.level = level
            try:
                result = self.play_client.song_url(song.id)
            except NeteaseError:
                continue
            if result.ok:
                self.log.info("从 %s 降级到 %s 后成功取到播放地址", configured, level)
                return result
        return None

    # ------------------------------------------------------------ 预下载下一首

    def _spawn_prefetch_next(self) -> None:
        """在后台把队列里下一首的音频先下好。

        当前后端不需要本地文件（比如 mpv 能直接流播）时自动跳过。
        """
        if not self.prefetch_enabled:
            return
        pending = self.queue.snapshot()
        if not pending:
            return

        item = pending[0]
        song_id = item.song.id
        with self._prefetch_lock:
            if song_id in self._prefetch_resolving:
                return
            self._prefetch_resolving.add(song_id)

        def work() -> None:
            try:
                result = self.prefetch_client.song_url(song_id)
                if result.ok:
                    url = NeteaseClient.upgrade_url_to_https(result.url or "")
                    self.prefetcher.submit(song_id, url)
            except NeteaseError as exc:
                self.log.debug("预下载取地址失败：%s", exc)
            except Exception as exc:  # noqa: BLE001 - 预下载永远不该影响主流程
                self.log.debug("预下载出错：%s", exc)
            finally:
                with self._prefetch_lock:
                    self._prefetch_resolving.discard(song_id)

        threading.Thread(target=work, name=f"prefetch-resolve-{song_id}", daemon=True).start()

    # ------------------------------------------------------------ 生命周期

    def start_workers(self) -> None:
        targets = (
            (self._resolver_loop, "resolver"),
            (self._player_loop, "player"),
            (self._danmaku_watchdog, "danmaku-watchdog"),
        )
        for target, name in targets:
            threading.Thread(target=target, name=name, daemon=True).start()
        self.log.info("工作线程已启动（解析 + 播放 + 弹幕监视）")

    def _danmaku_watchdog(self) -> None:
        """连上了却长时间收不到弹幕时，主动提示一下。

        "没有弹幕"和"功能坏了"看起来一模一样，不提示的话用户根本分不清。
        """
        warned = False
        while self._running.is_set():
            time.sleep(5)
            if self.danmaku_received > 0:
                return  # 已经收到过，没必要再盯着
            if not self.connected or self.connected_since is None:
                warned = False
                continue
            if warned:
                continue
            elapsed = time.time() - self.connected_since
            if elapsed < 60:
                continue
            warned = True
            self.log.warning("=" * 58)
            self.log.warning("已连接 %d 秒，但一条弹幕都没收到。", int(elapsed))
            self.log.warning("如果直播间当前【没在开播】，这是正常的——没开播就没有弹幕流。")
            self.log.warning("如果正在直播，请确认：")
            self.log.warning("  1. 房间号对不对（当前用的是 %s）", self.room_id)
            self.log.warning("  2. 直播间是不是真的有人在发弹幕")
            self.log.warning("  3. 跑一下完整诊断：")
            self.log.warning("     .venv\\Scripts\\python.exe tests\\diagnose_danmaku.py --room %s", self.room_id)
            self.log.warning("=" * 58)

    def stop(self) -> None:
        self._running.clear()
        with contextlib.suppress(Exception):
            self.player.skip()


# ------------------------------------------------------------------- blivedm 适配

# blivedm 0.1.1 用 xlive/web-room/v1 下的 getInfoByRoom / getDanmuInfo 做房间初始化，
# 但这两个接口现在会被 B站风控直接拒掉（返回 code=-352），连房间 1 这种官方房间也一样，
# 结果是 init_room() 永远失败、机器人连不上任何直播间。
#
# 老的 room/v1 接口不受影响（实测 code=0，且不需要任何 Cookie），返回的信息也完全够用：
#   room/v1/Room/room_init  -> 真实房间号、短号、主播 uid
#   room/v1/Danmu/getConf   -> 弹幕服务器列表 + 连接用 token
# 所以这里改用它们，并在失败时自动回退到 blivedm 原本的实现。
BILI_ROOM_INIT_URL = "https://api.live.bilibili.com/room/v1/Room/room_init"
BILI_DANMU_CONF_URL = "https://api.live.bilibili.com/room/v1/Danmu/getConf"
BILI_FINGER_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

logger = logging.getLogger("blivedm")

# 这些 cmd 对点歌没有任何用，但 blivedm 遇到不认识的 cmd 会打一条 WARNING，
# 而且把整个 command（常常是几百字符的 base64）都打出来，刷屏刷得没法看。
# 登记成"已知可忽略"，让 blivedm 静默跳过。（blivedm 自带的 IGNORED_CMDS 没覆盖这些新版本 cmd）
NOISY_CMDS = (
    "LOG_IN_NOTICE",       # "未登录无法查看他人昵称"
    "INTERACT_WORD_V2",    # 进房互动
    "LIKE_INFO_V3_UPDATE",  # 点赞数变化
    "LIKE_INFO_V3_CLICK",
    "WATCHED_CHANGE",      # 看过人数变化
    "DM_INTERACTION",      # 弹幕互动提示
    "ONLINE_RANK_V3",
    "ENTRY_EFFECT_V2",
    "STOP_LIVE_ROOM_LIST_V2",
    "POPULARITY_RED_POCKET_NEW",
    "POPULARITY_RED_POCKET_START",
    "POPULARITY_RED_POCKET_WINNER_LIST",
    "RANK_CHANGED",
    "REVENUE_RANK_CHANGED",
    "PLAY_TOGETHER",
    "VOICE_JOIN_ROOM_COUNT_INFO",
    "VOICE_JOIN_LIST",
    "GUARD_BUY_V2",
)

with contextlib.suppress(AttributeError):
    for _cmd in NOISY_CMDS:
        blivedm.BaseHandler._CMD_CALLBACK_DICT.setdefault(_cmd, None)  # noqa: SLF001


async def fetch_buvid_cookies() -> dict[str, str]:
    """取一对 buvid3/buvid4。

    这是 B站的公开指纹接口，不需要登录。带上它能让请求更像正常浏览器、
    降低被风控的概率。纯属锦上添花，失败就返回空字典，绝不影响主流程。
    """
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        # 括号式多上下文管理器要 Python 3.10+，这里为兼容 3.9 保持嵌套
        async with aiohttp.ClientSession(timeout=timeout) as session:  # noqa: SIM117
            async with session.get(BILI_FINGER_SPI_URL, headers={"User-Agent": BILI_UA}) as res:
                data = await res.json()
    except Exception:  # noqa: BLE001 - 拿不到就算了
        return {}

    if data.get("code") != 0:
        return {}
    payload = data.get("data") or {}
    return {key: payload[src] for key, src in (("buvid3", "b_3"), ("buvid4", "b_4")) if payload.get(src)}


class ResilientBLiveClient(blivedm.BLiveClient):
    """改用不受风控影响的老接口做初始化的 blivedm 客户端。

    只重写初始化方法和鉴权包，心跳、断线重连、消息解析全部沿用 blivedm 原实现。
    """

    buvid = ""
    """鉴权包里带上的 buvid3。实测带不带都能收到弹幕，但带上更像正常 Web 客户端，
    也没有任何副作用，所以默认补上（由 run_with_danmaku 从指纹接口取）。"""

    async def _send_auth(self) -> None:
        params = {
            "uid": self._uid,
            "roomid": self._room_id,
            "protover": 3,
            "platform": "web",
            "type": 2,
        }
        if self._host_server_token is not None:
            params["key"] = self._host_server_token
        if self.buvid:
            params["buvid"] = self.buvid
        await self._websocket.send_bytes(self._make_packet(params, blivedm.client.Operation.AUTH))

    async def _init_room_id_and_owner(self) -> bool:
        try:
            async with self._session.get(
                BILI_ROOM_INIT_URL,
                params={"id": self._tmp_room_id},
                headers={"User-Agent": BILI_UA, "Referer": "https://live.bilibili.com/"},
                ssl=self._ssl,
            ) as res:
                if res.status != 200:
                    logger.warning("room_init HTTP %s，回退到官方接口", res.status)
                else:
                    data = await res.json()
                    if data.get("code") != 0:
                        logger.warning("room_init 返回 code=%s，回退到官方接口", data.get("code"))
                    else:
                        info = data.get("data") or {}
                        self._room_id = info.get("room_id") or self._tmp_room_id
                        self._room_short_id = info.get("short_id") or 0
                        self._room_owner_uid = info.get("uid") or 0
                        return True
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("room_init 请求失败（%s），回退到官方接口", exc)

        return await super()._init_room_id_and_owner()

    async def _init_host_server(self) -> bool:
        try:
            async with self._session.get(
                BILI_DANMU_CONF_URL,
                params={"room_id": self._room_id, "platform": "pc", "player": "web"},
                headers={"User-Agent": BILI_UA, "Referer": "https://live.bilibili.com/"},
                ssl=self._ssl,
            ) as res:
                if res.status != 200:
                    logger.warning("getConf HTTP %s，回退到官方接口", res.status)
                else:
                    data = await res.json()
                    if data.get("code") != 0:
                        logger.warning("getConf 返回 code=%s，回退到官方接口", data.get("code"))
                    else:
                        payload = data.get("data") or {}
                        hosts = payload.get("host_server_list") or payload.get("server_list")
                        token = payload.get("token")
                        if hosts and token:
                            self._host_server_list = hosts
                            self._host_server_token = token
                            return True
                        logger.warning("getConf 没返回完整服务器信息，回退到官方接口")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("getConf 请求失败（%s），回退到官方接口", exc)

        return await super()._init_host_server()

    async def init_room(self) -> bool:
        ok = await super().init_room()
        if not ok:
            logger.error(
                "直播间 %s 初始化失败。常见原因：房间号不对、房间不存在、"
                "或者网络访问 B站接口受限。",
                self._tmp_room_id,
            )
        return ok


class BotHandler(blivedm.BaseHandler):
    """把 blivedm 的弹幕消息转交给 SongBot。"""

    def __init__(self, bot: SongBot) -> None:
        self.bot = bot

    async def _on_danmaku(self, client, message) -> None:
        try:
            self.bot.handle_danmaku(message.msg, message.uname, message.uid)
        except Exception as exc:  # noqa: BLE001 - 单条弹幕出错不能断连接
            self.bot.log.exception("处理弹幕出错：%s", exc)


# ------------------------------------------------------------------------ 入口


def _resolve_cli_room(text: str, log) -> int:
    """把命令行/键盘输入的直播间信息变成数字房间号，失败返回 0。"""
    result = parse_room_code(text)
    if result.ok:
        log.info("识别到直播间号 %s（%s）", result.room_id, result.reason)
        return int(result.room_id)
    log.error("没能从「%s」里识别出直播间号：%s", text, result.reason)
    for line in describe_input_help().splitlines():
        log.error("%s", line)
    return 0


def _resolve_config_room(config: dict[str, Any], log) -> int:
    """读 config.json 里的 room_id。

    它可能是数字，也可能是用户图省事直接粘的直播间网址（甚至 b23.tv 短链）。
    解析成功后顺手把配置归一成纯数字，以后就不必再解析了。
    """
    raw = config["bilibili"].get("room_id", 0)

    if isinstance(raw, int):
        return raw if raw > 0 else 0

    text = str(raw or "").strip()
    if not text:
        return 0

    result = parse_room_code(text)
    if not result.ok:
        log.warning("config.json 里的 bilibili.room_id 识别不了：%s", result.reason)
        return 0

    config["bilibili"]["room_id"] = int(result.room_id)
    if text != str(result.room_id):
        log.info("config.json 里的直播间网址已识别为房间号 %s（%s）", result.room_id, result.reason)
        save_config(config)
    return int(result.room_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="B站弹幕点歌机器人（网易云）")
    parser.add_argument(
        "--room",
        type=str,
        help="直播间号或直播间网址，覆盖 config.json 里的设置（网址和 b23.tv 短链都认）",
    )
    parser.add_argument("--stdin", action="store_true", help="不开弹幕连接，从键盘读指令（测试用）")
    parser.add_argument("--backend", help="临时指定播放后端：mpv/ffplay/pygame/wmp/null")
    parser.add_argument("--volume", type=int, help="临时指定音量 0-100")
    parser.add_argument("--list-backends", action="store_true", help="列出可用播放后端后退出")
    parser.add_argument("--no-web", action="store_true", help="不启动网页面板")
    parser.add_argument("--no-browser", action="store_true", help="启动网页面板，但不自动打开浏览器")
    parser.add_argument("--web-port", type=int, help="网页面板端口，默认 8765")
    parser.add_argument("--verbose", action="store_true", help="把收到的每一条弹幕都打出来（排查用）")
    parser.add_argument("--autoplay", action="store_true", help="开启空闲随机播放（覆盖配置）")
    parser.add_argument("--no-autoplay", action="store_true", help="关闭空闲随机播放（覆盖配置）")
    return parser


def _check_prerequisites(bot: SongBot, config: dict[str, Any]) -> bool:
    """启动前做基本检查，返回是否可以继续。"""
    log = bot.log
    ok = True

    if not bot.search_client.ping():
        log.error("连不上网易云 API 服务（%s）", config["netease"]["api_base"])
        log.error("请先双击 start_netease_api.cmd 启动它")
        ok = False
    else:
        bot.netease_ok = True
        log.info("网易云 API 服务正常：%s", config["netease"]["api_base"])

        if not config["netease"].get("cookie"):
            log.warning("还没扫码登录网易云，VIP 歌曲只能拿到试听片段")
            log.warning("建议先运行：python qr_login.py")
        else:
            try:
                account = bot.search_client.current_account()
            except NeteaseError as exc:
                log.warning("检查登录状态失败：%s", exc)
                account = None
            if account:
                bot.netease_account = str(account["nickname"])
                if account["is_vip"]:
                    bot.netease_account += "（VIP）"
                log.info(
                    "网易云账号：%s（VIP：%s）",
                    account["nickname"],
                    "是" if account["is_vip"] else "否",
                )
            else:
                log.warning("网易云 Cookie 好像失效了，建议重新运行 python qr_login.py")
    return ok


async def run_with_danmaku(bot: SongBot, room_id: int, reconnect_delay: float) -> None:
    """连接直播间并持续监听，掉线自动重建客户端。"""
    log = bot.log
    session = None

    while bot._running.is_set():
        session = None
        client = None
        try:
            bili_conf = bot.config["bilibili"]
            headers = {"User-Agent": BILI_UA, "Referer": "https://live.bilibili.com/"}

            cookies: dict[str, str] = {}
            if bili_conf.get("sessdata"):
                cookies["SESSDATA"] = bili_conf["sessdata"]
            if bili_conf.get("bili_jct"):
                cookies["bili_jct"] = bili_conf["bili_jct"]
            if bili_conf.get("buvid3"):
                cookies["buvid3"] = bili_conf["buvid3"]
            else:
                # 没配就自动取一个，取不到也不影响（老接口本来就不需要 Cookie）
                cookies.update(await fetch_buvid_cookies())

            session = aiohttp.ClientSession(
                headers=headers,
                cookies=cookies,
                timeout=aiohttp.ClientTimeout(total=10),
            )
            client = ResilientBLiveClient(
                room_id,
                session=session,
                uid=int(bili_conf.get("uid", 0) or 0),
            )
            # 鉴权包里也带上 buvid，和真实 Web 客户端一致
            client.buvid = cookies.get("buvid3", "")
            client.add_handler(BotHandler(bot))

            # 先把房间信息拿到手再开播，这样"连上了没"是确定的，而不是先宣称成功
            if await client.init_room():
                bot.room_id = client.room_id
                bot.room_owner_uid = client.room_owner_uid
                bot.connected = True
                bot.connected_since = time.time()
                log.info(
                    "已连接直播间 %s（真实房间号 %s，主播 uid %s，弹幕服务器 %d 个）",
                    room_id,
                    client.room_id,
                    client.room_owner_uid,
                    len(client._host_server_list),
                )
                client.start()
                await client.join()
            else:
                bot.connected = False
                log.warning("直播间初始化失败，%s 秒后重试", reconnect_delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络问题就重连
            log.error("弹幕连接出错：%s", exc)
        finally:
            bot.connected = False
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.stop_and_close()
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.close()

        if not bot._running.is_set():
            break
        log.warning("%.0f 秒后重连…", reconnect_delay)
        try:
            await asyncio.sleep(reconnect_delay)
        except asyncio.CancelledError:
            break


async def run_stdin(bot: SongBot) -> None:
    """从键盘输入模拟弹幕，用于在没有直播间的情况下测试整条链路。"""
    log = bot.log
    log.info("=" * 58)
    log.info("键盘模拟模式：直接输入弹幕内容回车，例如「点歌 晴天」")
    log.info("输入「退出」结束。")
    log.info("=" * 58)

    loop = asyncio.get_running_loop()
    while bot._running.is_set():
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        text = line.strip()
        if text in ("退出", "exit", "quit"):
            break
        if text:
            bot.handle_danmaku(text, "测试观众", 10001)


def main() -> int:
    args = build_parser().parse_args()
    log = setup_logging()

    if args.list_backends:
        print("播放后端可用性：")
        for name, available in AudioPlayer.describe_backends().items():
            print(f"  {'[可用]' if available else '[不可用]'} {name}")
        return 0

    config = load_config()
    if args.backend:
        config["player"]["backend"] = args.backend
    if args.volume is not None:
        config["player"]["volume"] = args.volume

    room_id = _resolve_config_room(config, log)
    if args.room is not None:
        room_id = _resolve_cli_room(args.room, log)
        if room_id <= 0:
            return 1

    if not args.stdin and room_id <= 0:
        # 没配就在这儿问一句，省得用户去翻配置文件
        if sys.stdin is not None:
            log.info("-" * 62)
            log.info("还没配置直播间号。")
            for line in describe_input_help().splitlines():
                log.info("%s", line)
            log.info("-" * 62)
            try:
                # 不强制要求是终端：管道喂一行进来也能用（方便脚本化）
                entered = input("请输入直播间号或网址（直接回车退出）：").strip()
            except (EOFError, KeyboardInterrupt, OSError, ValueError):
                entered = ""
                log.info("（没有读到输入）")
            if entered:
                room_id = _resolve_cli_room(entered, log)
                if room_id > 0:
                    config["bilibili"]["room_id"] = room_id
                    save_config(config)
                    log.info("已把房间号 %s 写入 config.json，下次启动就不用再输了。", room_id)
        if room_id <= 0:
            log.error("没有可用的直播间号，无法开始。")
            log.error("可以编辑 config.json 里的 bilibili.room_id（填网址也行），")
            log.error("或者启动时带上：run.cmd --room https://live.bilibili.com/你的房间号")
            return 1

    bot = SongBot(config)
    if args.verbose:
        bot.log_all_danmaku = True
        log.info("已开启 --verbose：收到的每一条弹幕都会打印出来（不点歌也能看到）")
    if args.autoplay:
        bot.autoplay_enabled = True
    if args.no_autoplay:
        bot.autoplay_enabled = False
    if bot.autoplay_enabled:
        log.info(
            "空闲随机播放：已开启（来源 %s，避开最近 %d 首）",
            bot.autoplay_source,
            bot.autoplay_avoid_repeat,
        )
    else:
        log.info("空闲随机播放：关闭（面板上可随时打开，或加 --autoplay）")
    log.info("播放后端：%s（暂停能力：%s）", bot.player.backend_name, "支持" if bot.player.supports_pause else "不支持")
    if bot.replier.enabled:
        log.info("B站弹幕反馈（回发弹幕）：开启")
    else:
        log.info("B站弹幕反馈（回发弹幕）：关闭（未配置 SESSDATA/bili_jct）")
        log.warning("没配 B站 Cookie 时，B站会隐藏观众昵称，")
        log.warning("点歌记录里的点歌人会显示成「用户<uid>」。想显示真名就填 sessdata。")

    if not _check_prerequisites(bot, config):
        return 1

    bot.start_workers()

    # 网页面板：控制台窗口容易被挡住或没打开，用浏览器看状态更省心
    web_conf = config.get("web") or {}
    if web_conf.get("enabled", True) and not args.no_web:
        panel = ControlPanel(
            bot,
            host=str(web_conf.get("host", "127.0.0.1")),
            port=int(args.web_port or web_conf.get("port", 8765)),
            log=lambda m: log.info("[面板] %s", m),
        )
        open_browser = bool(web_conf.get("open_browser", True)) and not args.no_browser
        if panel.start(open_browser=open_browser):
            bot.panel = panel
            log.info("浏览器里可以看状态和控制播放；不想要这个面板就加 --no-web")
    elif args.no_web:
        log.info("已按 --no-web 关闭网页面板")

    try:
        if args.stdin:
            asyncio.run(run_stdin(bot))
        else:
            asyncio.run(run_with_danmaku(bot, room_id, float(config["bilibili"].get("reconnect_delay", 10))))
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，正在退出…")
    finally:
        bot.stop()
        if bot.panel is not None:
            bot.panel.stop()

    log.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
