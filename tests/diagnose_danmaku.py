"""诊断：到底能不能收到弹幕（DANMU_MSG）。

之前的 live_connect_test 只证明了"收到心跳"，但心跳是服务端对心跳包的固定回复，
**不能证明弹幕在下发**。这个脚本专门统计各类消息的数量。

对比两种鉴权包：
    A. blivedm 原版（不含 buvid）
    B. 补上 buvid（新版 B站 Web 客户端会带）

用法：
    .venv\\Scripts\\python.exe tests\\diagnose_danmaku.py
    .venv\\Scripts\\python.exe tests\\diagnose_danmaku.py --wait 40
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
import blivedm  # noqa: E402

from danmaku_bot import BILI_UA, ResilientBLiveClient, fetch_buvid_cookies  # noqa: E402

HEADERS = {"User-Agent": BILI_UA, "Referer": "https://live.bilibili.com/"}

# 找开播房间：列人气榜的接口基本都被风控了（-352），
# 所以改成批量查一批知名房间的开播状态，挑 live_status==1 的。
AREA_APIS = [
    ("room/v1/Room/get_info", None),
]

CANDIDATE_ROOMS = [
    6, 5050, 7734200, 21743919, 21452505, 1017, 22637261, 5471298,
    92613, 21396545, 6163933, 5440, 94815, 12235923,
]


class CountingHandler(blivedm.BaseHandler):
    """只统计，不做别的。"""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.samples: list[str] = []
        self.first_danmaku_at: float | None = None
        self.unknown_cmds: Counter[str] = Counter()

    def _bump(self, name: str, sample: str = "") -> None:
        self.counts[name] += 1
        if sample and len(self.samples) < 6:
            self.samples.append(sample)

    async def handle(self, client, command: dict) -> None:
        cmd = str(command.get("cmd", "")).split(":")[0]
        self.unknown_cmds[cmd] += 1
        await super().handle(client, command)

    async def _on_heartbeat(self, client, message) -> None:
        self.counts["心跳"] += 1

    async def _on_danmaku(self, client, message) -> None:
        if self.first_danmaku_at is None:
            self.first_danmaku_at = time.time()
        self._bump("弹幕", f"{message.uname or '(未登录看不到昵称)'}: {message.msg}")

    async def _on_gift(self, client, message) -> None:
        self._bump("礼物", f"{message.uname} 送了 {message.gift_name}")

    async def _on_buy_guard(self, client, message) -> None:
        self._bump("上舰")

    async def _on_super_chat(self, client, message) -> None:
        self._bump("醒目留言")


class BuvidAuthClient(ResilientBLiveClient):
    """鉴权包里补上 buvid 的版本。"""

    buvid = ""

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


async def find_live_rooms(session: aiohttp.ClientSession, want: int = 6) -> list[tuple[int, str, str]]:
    """批量查一批知名房间，挑出正在开播的，返回 [(room_id, 标题, 主播)]。"""
    found: list[tuple[int, str, str]] = []
    for room_id in CANDIDATE_ROOMS:
        try:
            async with session.get(
                "https://api.live.bilibili.com/room/v1/Room/get_info",
                params={"room_id": room_id},
                headers=HEADERS,
            ) as resp:
                data = await resp.json()
        except Exception:  # noqa: BLE001
            continue
        if data.get("code") != 0:
            continue
        info = data.get("data") or {}
        if info.get("live_status") != 1:
            continue
        found.append((int(info.get("room_id") or room_id), str(info.get("title") or "")[:34], ""))
        if len(found) >= want:
            break
    return found


async def probe_room(room_id: int, title: str, wait: float, use_buvid: bool, buvid: str, tag: str = "") -> Counter[str]:
    label = tag or ("B. 补 buvid" if use_buvid else "A. 原版（无 buvid）")
    prefix = f"  [{label}]"

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
        cls = BuvidAuthClient if use_buvid else ResilientBLiveClient
        client = cls(room_id, session=session)
        if use_buvid:
            client.buvid = buvid
        handler = CountingHandler()
        client.add_handler(handler)

        if not await client.init_room():
            print(f"{prefix} init_room 失败")
            await client.close()
            return handler.counts

        client.start()
        started = time.time()
        while time.time() - started < wait:
            await asyncio.sleep(0.5)
            if handler.first_danmaku_at is not None and handler.counts["弹幕"] >= 8:
                break
        await client.stop_and_close()

    elapsed = time.time() - started
    summary = "  ".join(f"{k}={v}" for k, v in handler.counts.items()) or "什么都没有"
    print(f"{prefix} 房间 {room_id}，监听 {elapsed:.0f} 秒：{summary}")
    if handler.first_danmaku_at:
        print(f"{prefix}   首条弹幕在 {handler.first_danmaku_at - started:.1f} 秒")
    for sample in handler.samples[:3]:
        print(f"{prefix}   · {sample}")
    return handler.counts


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", type=float, default=25.0, help="每个房间监听多少秒")
    parser.add_argument("--rooms", type=int, default=3, help="测几个房间")
    parser.add_argument("--room", type=str, help="直接指定房间号，跳过自动查找")
    args = parser.parse_args()

    from roomcode import parse_room_code

    print("=" * 74)
    print("  弹幕捕捉诊断")
    print("=" * 74)

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
        buvid_map = await fetch_buvid_cookies()
        buvid = buvid_map.get("buvid3", "")
        print(f"buvid3：{buvid or '（没拿到）'}")

        if args.room:
            parsed = parse_room_code(args.room)
            if not parsed.ok:
                print(f"房间号识别失败：{parsed.reason}")
                return 1
            rooms = [(int(parsed.room_id), "(指定)", "")]
        else:
            print("\n正在查找正在开播的房间…")
            rooms = await find_live_rooms(session, want=args.rooms)
            if not rooms:
                print("没找到开播房间，请用 --room 手动指定一个正在直播的房间")
                return 1
            for room_id, title, uname in rooms:
                print(f"  {room_id}  {title}  by {uname}")

    print("\n" + "=" * 74)
    print("  开始监听（两种鉴权【同时】连同一个房间，这样才可比）")
    print("=" * 74)

    # 必须并行跑：房间的弹幕密度一直在变，先后跑的话两组拿到的样本
    # 根本不是一个时间窗，比出来的差异全是噪声（我就被这个坑过一次）。
    verdict: dict[str, int] = {"A": 0, "B": 0}
    for room_id, title, _ in rooms:
        print(f"\n  房间 {room_id} {title!r}")
        counts_a, counts_b = await asyncio.gather(
            probe_room(room_id, title, args.wait, use_buvid=False, buvid=buvid, tag="A 原版"),
            probe_room(room_id, title, args.wait, use_buvid=True, buvid=buvid, tag="B 补buvid"),
        )
        verdict["A"] += counts_a.get("弹幕", 0)
        verdict["B"] += counts_b.get("弹幕", 0)

    count_a, count_b = verdict["A"], verdict["B"]

    print("\n" + "=" * 74)
    print(f"  同一时间窗内收到的弹幕：原版鉴权 {count_a} 条，补 buvid {count_b} 条")
    print()

    total = count_a + count_b
    if total > 0:
        print("  => 结论：弹幕捕捉正常，机器人能收到弹幕。")
        print("     如果主程序里看不到弹幕，多半是房间当前没开播（没开播就没有弹幕流），")
        print("     或者你只盯着控制台——非点歌弹幕默认是不打印的，")
        print("     加 --verbose 或看网页面板的「收到的弹幕」卡片。")
        if count_a == 0 or count_b == 0:
            print("     （两组条数差异较大只是噪声，样本太小，别据此下结论）")
        ok = True
    else:
        print("  => 结论：两种鉴权都收不到弹幕。")
        print("     先确认这个房间真的在开播、真的有人在发弹幕；")
        print("     如果确实有弹幕却收不到，再查网络 / 代理 / 防火墙。")
        ok = False

    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
