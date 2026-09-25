"""真连一次直播间的弹幕服务器，验证 WebSocket 握手 + 鉴权 + 心跳。

只测连接，不收点歌指令，所以不需要房间在开播。

用法：
    .venv\\Scripts\\python.exe tests\\live_connect_test.py
    .venv\\Scripts\\python.exe tests\\live_connect_test.py --room 1934302095
    .venv\\Scripts\\python.exe tests\\live_connect_test.py --wait 20
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
import blivedm  # noqa: E402

from common import setup_logging  # noqa: E402
from danmaku_bot import BILI_UA, ResilientBLiveClient, fetch_buvid_cookies  # noqa: E402


class ProbeHandler(blivedm.BaseHandler):
    """记录收到什么消息。"""

    def __init__(self) -> None:
        self.heartbeats = 0
        self.danmaku = 0
        self.gifts = 0
        self.first_seen: float | None = None
        self.samples: list[str] = []

    def _touch(self) -> None:
        if self.first_seen is None:
            self.first_seen = time.time()

    async def _on_heartbeat(self, client, message) -> None:
        self._touch()
        self.heartbeats += 1
        if "心跳" not in self.samples:
            self.samples.append(f"心跳（人气值 {message.popularity}）")

    async def _on_danmaku(self, client, message) -> None:
        self._touch()
        self.danmaku += 1
        if len(self.samples) < 8:
            self.samples.append(f"弹幕 {message.uname}: {message.msg}")

    async def _on_gift(self, client, message) -> None:
        self._touch()
        self.gifts += 1
        if len(self.samples) < 8:
            self.samples.append(f"礼物 {message.uname} x{message.num} {message.gift_name}")


async def probe(room_id: int, wait: float) -> bool:
    print("=" * 70)
    print(f"连接直播间 {room_id}")
    print("=" * 70)

    cookies = await fetch_buvid_cookies()
    print(f"buvid：{'拿到' if cookies else '没拿到（不影响）'}")

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(
        headers={"User-Agent": BILI_UA, "Referer": "https://live.bilibili.com/"},
        cookies=cookies,
        timeout=timeout,
    ) as session:
        client = ResilientBLiveClient(room_id, session=session)
        handler = ProbeHandler()
        client.add_handler(handler)

        ok = await client.init_room()
        print(f"init_room()        -> {ok}")
        if not ok:
            print("  初始化就失败了，后面的不用测了")
            await client.close()
            return False

        print(f"真实房间号          -> {client.room_id}")
        print(f"短号                -> {client.room_short_id}")
        print(f"主播 uid            -> {client.room_owner_uid}")
        print(f"弹幕服务器          -> {len(client._host_server_list)} 个")
        token_desc = f"有，长度 {len(client._host_server_token)}" if client._host_server_token else "无"
        print(f"鉴权 token          -> {token_desc}")

        client.start()
        print(f"\n开始监听 {wait:.0f} 秒（这一步验证 WebSocket 握手和鉴权是否真的通过）…")
        started = time.time()
        while time.time() - started < wait:
            await asyncio.sleep(0.5)
            if handler.first_seen is not None:
                break

        await client.stop_and_close()

    print()
    if handler.first_seen is None:
        print("[失败] 连上了但一个消息都没收到 —— WebSocket 鉴权可能没过")
        return False

    print(f"[通过] 收到第一条消息耗时 {handler.first_seen - started:.1f} 秒")
    print(f"       心跳 {handler.heartbeats} 次，弹幕 {handler.danmaku} 条，礼物 {handler.gifts} 个")
    for sample in handler.samples:
        print(f"       · {sample}")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--room", type=str, default="1934302095", help="直播间号或直播间网址")
    parser.add_argument("--wait", type=float, default=15.0, help="最多等多少秒")
    args = parser.parse_args()

    setup_logging()
    # 这个测试只想看关键结论，把 blivedm 自己的日志压低
    import logging

    logging.getLogger("blivedm").setLevel(logging.ERROR)

    from roomcode import parse_room_code

    parsed = parse_room_code(args.room)
    if not parsed.ok:
        print(f"没能从「{args.room}」里识别出直播间号：{parsed.reason}")
        return 1

    ok = await probe(int(parsed.room_id), args.wait)
    print("=" * 70)
    print("  结论：整条弹幕链路" + ("通畅" if ok else "不通") )
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
