"""探测：未登录状态下哪些搜索结果能真正拿到播放地址。

用来在没有网易云 Cookie 的情况下挑一首可播放的歌做端到端测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import ensure_console_utf8  # noqa: E402
from netease_api import QUALITY_LEVELS, NeteaseClient, NeteaseError  # noqa: E402

KEYWORDS = [
    "纯音乐", "轻音乐 放松", "白噪音", "钢琴曲", "Lo-fi", "民谣",
    "晴天", "起风了", "海阔天空", "卡农", "天空之城", "菊次郎的夏天",
]


def main() -> int:
    ensure_console_utf8()
    client = NeteaseClient()
    if not client.ping():
        print("API 服务没起来")
        return 1

    playable: list[tuple[str, int, str, str]] = []
    for keyword in KEYWORDS:
        try:
            songs = client.search(keyword, limit=4)
        except NeteaseError as exc:
            print(f"  {keyword}: 搜索失败 {exc}")
            continue

        for song in songs:
            found = None
            for level in reversed(QUALITY_LEVELS):
                client.level = level
                try:
                    result = client.song_url(song.id)
                except NeteaseError:
                    continue
                if result.ok:
                    found = (level, result.level, result.url or "")
                    break
            if found:
                level, label, url = found
                playable.append((song.display, song.id, label or level, url))
                print(f"  [可播] {song.display}  id={song.id}  音质={label or level}")
                break
        else:
            print(f"  [全不可播] {keyword}（前 4 条都拿不到地址）")

    print()
    print(f"共找到 {len(playable)} 首未登录即可播放的歌")
    for display, song_id, label, url in playable[:5]:
        print(f"  {display}  id={song_id}  {label}")
        print(f"    {url[:110]}")
    if playable:
        # 缓存写到 .tmp/ 而不是 tests/ —— tests/ 里只放源码，打包时保持干净
        cache_dir = Path(__file__).resolve().parent.parent / ".tmp"
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_dir / "playable.txt", "w", encoding="utf-8") as handle:
            for display, song_id, label, _url in playable:
                handle.write(f"{song_id}\t{display}\t{label}\n")
        print(f"（结果已缓存到 {cache_dir / 'playable.txt'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
