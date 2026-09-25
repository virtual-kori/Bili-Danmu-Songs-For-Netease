"""B站接口排查工具。

机器人连不上直播间（比如 `init_room() failed`）时跑这个，它会逐项告诉你哪一层断了。

用法：
    .venv\\Scripts\\python.exe tests\\diagnose_bilibili.py
    .venv\\Scripts\\python.exe tests\\diagnose_bilibili.py --room 你的房间号
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402

from common import load_config, setup_logging  # noqa: E402
from danmaku_bot import (  # noqa: E402
    BILI_DANMU_CONF_URL,
    BILI_FINGER_SPI_URL,
    BILI_ROOM_INIT_URL,
    BILI_UA,
    ResilientBLiveClient,
    fetch_buvid_cookies,
)

# blivedm 原版用的两个接口（会被风控返回 -352）
XLIVE_INFO = "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom"
XLIVE_DANMU = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"

HEADERS = {"User-Agent": BILI_UA, "Referer": "https://live.bilibili.com/"}
findings: list[tuple[str, bool, str]] = []


def record(label: str, ok: bool, detail: str = "") -> bool:
    findings.append((label, ok, detail))
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {label}" + (f"  —— {detail}" if detail else ""))
    return ok


async def get_json(session: aiohttp.ClientSession, url: str, params: dict) -> dict:
    try:
        async with session.get(url, params=params, headers=HEADERS) as res:
            return await res.json()
    except Exception as exc:  # noqa: BLE001
        return {"code": f"请求异常 {type(exc).__name__}"}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--room", type=str, help="直播间号或直播间网址，默认读 config.json")
    args = parser.parse_args()

    setup_logging()

    # 房间号可能是纯数字，也可能是粘进来的直播间网址
    from roomcode import parse_room_code

    if args.room:
        parsed = parse_room_code(args.room)
        if not parsed.ok:
            print(f"没能从「{args.room}」里识别出直播间号：{parsed.reason}")
            return 1
        room_id = int(parsed.room_id)
        print(f"已从输入识别出房间号：{room_id}（{parsed.reason}）")
    else:
        raw = load_config()["bilibili"].get("room_id", 0)
        parsed = parse_room_code(str(raw))
        room_id = int(parsed.room_id) if parsed.ok else 0

    print("=" * 74)
    print("  B站接口排查")
    print("=" * 74)
    print(f"  直播间号：{room_id or '（没配置）'}")
    print()

    timeout = aiohttp.ClientTimeout(total=20)

    # 没配房间号就先说清楚——不然下面每项都会失败，看着像是接口坏了
    if room_id <= 0:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            print("[1] 指纹接口（buvid，公开接口，不需要登录）")
            async with session.get(BILI_FINGER_SPI_URL, headers=HEADERS) as res:
                spi = await res.json()
            has_buvid = spi.get("code") == 0 and (spi.get("data") or {}).get("b_3")
            record("能取到 buvid3", bool(has_buvid), "网络正常" if has_buvid else "网络可能有问题")
        print()
        print("=" * 74)
        print("  还没配置直播间号，房间相关的检查没法做。")
        print()
        print("  房间号就是直播间网址 live.bilibili.com/ 后面那串数字，例如")
        print("  https://live.bilibili.com/1234567  ->  填 1234567")
        print()
        print("  两种填法：")
        print("    .venv\\Scripts\\python.exe tests\\diagnose_bilibili.py --room 1234567")
        print("    或者编辑 config.json，把 bilibili.room_id 改成你的房间号")
        print("=" * 74)
        return 1

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # ---------------------------------------------------------- 1
        print("[1] 指纹接口（buvid，公开接口，不需要登录）")
        async with session.get(BILI_FINGER_SPI_URL, headers=HEADERS) as res:
            spi = await res.json()
        has_buvid = spi.get("code") == 0 and (spi.get("data") or {}).get("b_3")
        record("能取到 buvid3", bool(has_buvid), "拿不到也能用，只是更容易被风控" if not has_buvid else "")

        # ---------------------------------------------------------- 2
        print()
        print("[2] 老接口 room/v1（本项目实际使用的）")
        legacy_init = await get_json(session, BILI_ROOM_INIT_URL, {"id": room_id})
        record("room_init 可用", legacy_init.get("code") == 0, f"code={legacy_init.get('code')}")
        info = legacy_init.get("data") or {}
        if legacy_init.get("code") == 0:
            print(f"         真实房间号={info.get('room_id')}  短号={info.get('short_id')}  "
                  f"主播uid={info.get('uid')}  开播状态={info.get('live_status')}")
            if info.get("live_status") == 0:
                print("         （房间当前未开播，不影响连接弹幕服务器）")

        real_room = info.get("room_id") or room_id
        legacy_conf = await get_json(
            session, BILI_DANMU_CONF_URL, {"room_id": real_room, "platform": "pc", "player": "web"}
        )
        conf_ok = legacy_conf.get("code") == 0 and bool(
            (legacy_conf.get("data") or {}).get("token")
        )
        record("Danmu/getConf 可用（能拿到 token 和服务器列表）", conf_ok, f"code={legacy_conf.get('code')}")

        # ---------------------------------------------------------- 3
        print()
        print("[3] 新接口 xlive/web-room/v1（blivedm 原版用的，被风控，仅作对照）")
        x1 = await get_json(session, XLIVE_INFO, {"room_id": real_room})
        x2 = await get_json(session, XLIVE_DANMU, {"id": real_room})
        print(f"         getInfoByRoom    code={x1.get('code')}")
        print(f"         getDanmuInfo     code={x2.get('code')}")
        if x1.get("code") == -352 or x2.get("code") == -352:
            print("         -352 = B站风控拦截。这两个接口失败【属于预期】，")
            print("         本项目已经改用上面的老接口绕开它们，不影响使用。")
        else:
            print("         这两个接口居然是通的（B站风控策略可能变了）。")
            print("         不影响使用，本项目仍走老接口。")

        # ---------------------------------------------------------- 4
        print()
        print("[4] 用修好的客户端跑一次 init_room()")
        cookies = await fetch_buvid_cookies()
        async with aiohttp.ClientSession(
            headers=HEADERS, cookies=cookies, timeout=timeout
        ) as sess:
            client = ResilientBLiveClient(room_id, session=sess)
            try:
                ok = await client.init_room()
                record(
                    "init_room()",
                    ok,
                    f"真实房间号={client.room_id}  服务器={len(client._host_server_list)}个  "
                    f"token={'有' if client._host_server_token else '无'}",
                )
            finally:
                await client.close()

    # ---------------------------------------------------------- 结论
    print()
    print("=" * 74)
    failed = [label for label, ok, _ in findings if not ok]
    if not failed:
        print("  结论：一切正常，机器人应该能连上直播间。")
    else:
        print("  结论：以下项目没通过：")
        for label in failed:
            print(f"    - {label}")
        print()
        if not conf_ok:
            print("  老接口都不通，通常是网络问题（代理 / 防火墙 / DNS）。")
            print("  试试：用浏览器打开 https://live.bilibili.com/ 看能不能访问。")
        else:
            print("  老接口是通的，但某个环节仍有问题，把上面输出发出来看看。")
    print("=" * 74)
    print()
    print("  想进一步验证 WebSocket 能不能真连上：")
    print(f"    .venv\\Scripts\\python.exe tests\\live_connect_test.py --room {room_id}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
