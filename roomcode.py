"""把用户给的直播间信息统一变成直播间号。

支持这些写法（都能识别出同一个房间号）：

    1934302095
    https://live.bilibili.com/1934302095
    https://live.bilibili.com/1934302095?broadcast_type=0&is_room_feed=1
    live.bilibili.com/1934302095
    https://live.bilibili.com/blanc/1934302095
    https://live.bilibili.com/h5/1934302095
    https://live.bilibili.com/p/html5/1934302095
    https://b23.tv/AbCdEf                  （手机 App 分享出来的短链，会联网展开）
    【哔哩哔哩】我的直播间 https://live.bilibili.com/1934302095?share_source=copy_web
    直播间号：1934302095
    １９３４３０２０９５                    （全角数字）

不支持的写法会给出具体原因，而不是闷声失败。
"""

from __future__ import annotations

import re

# B站直播间号的合理范围：真实房间号目前最多 10 位
MIN_ROOM_ID = 1
MAX_ROOM_ID = 99_999_999_999

# 全角数字转半角
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# 直播间地址：live.bilibili.com/ 后面可以有 0~N 段路径，最后一段是数字
#   /1934302095            （标准）
#   /blanc/1934302095      （活动/白名单页）
#   /h5/1934302095         （手机页）
#   /p/html5/1934302095    （旧手机页）
_LIVE_URL_RE = re.compile(r"live\.bilibili\.com/(?:[A-Za-z0-9_-]+/)*?(\d{3,12})", re.I)

# 房间号写在查询参数里的情况，比较少见但见过
_QUERY_ROOM_RE = re.compile(r"[?&](?:room_id|roomid)=(\d{3,12})", re.I)

# 其他形态的 B站直播地址，比如 m.bilibili.com/live/1934302095
_LIVE_ALT_RE = re.compile(r"bilibili\.com/(?:[A-Za-z0-9_-]+/)*live/(?:[A-Za-z0-9_-]+/)*(\d{3,12})", re.I)

# 手机 App 分享出来的短链（需要联网跟着跳转才能拿到真实地址）
_SHORT_URL_RE = re.compile(
    r"(?:https?://)?(?:b23\.tv|bili2233\.cn|b23\.wtf)/[A-Za-z0-9._-]+",
    re.I,
)

# 用户空间地址，容易被误当成直播间地址
_SPACE_URL_RE = re.compile(r"space\.bilibili\.com/(\d+)", re.I)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class RoomCodeResult:
    """解析结果。"""

    __slots__ = ("room_id", "reason", "ok")

    def __init__(self, room_id: int | None, reason: str) -> None:
        self.room_id = room_id
        self.reason = reason
        self.ok = room_id is not None

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"RoomCodeResult({self.room_id}, {self.reason!r})"


def _valid(value: int) -> bool:
    return MIN_ROOM_ID <= value <= MAX_ROOM_ID


def _digits(text: str) -> list[str]:
    """找出文本里所有像房间号的数字串。

    注意要排除跟在字母后面的数字（比如 buvid、token），否则分享文案里的杂七杂八
    会被当成房间号。
    """
    found = []
    for match in re.finditer(r"(?<![A-Za-z0-9])(\d{3,12})(?![0-9])", text):
        value = int(match.group(1))
        if _valid(value):
            found.append(match.group(1))
    return found


def resolve_short_link(url: str, timeout: float = 8.0) -> str | None:
    """展开 b23.tv 之类的短链，返回最终地址。

    先看 HTTP 跳转；有些短链是返回一个带 JS 跳转的 HTML 页，
    所以拿不到跳转时再去正文里找 live.bilibili.com 链接。
    """
    import requests

    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url

    try:
        resp = requests.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": _UA},
        )
    except requests.RequestException:
        return None

    final = str(resp.url or "")
    if "live.bilibili.com" in final:
        return final

    # 跟着跳转没到直播间，就在正文里翻一下
    with_body = re.search(r"https?://live\.bilibili\.com/[^\s\"'<>\\]+", resp.text or "")
    if with_body:
        return with_body.group(0)
    return final or None


def parse_room_code(text: str, resolve_short: bool = True) -> RoomCodeResult:
    """从任意用户输入里提取直播间号。

    resolve_short=False 时不联网，遇到短链会直接说"需要联网展开"。
    """
    raw = (text or "").strip()
    if not raw:
        return RoomCodeResult(None, "输入是空的")

    normalized = raw.translate(_FULLWIDTH_DIGITS)
    # 去掉千分位逗号，但别动 URL 里的东西：只在纯数字场景处理
    bare = normalized.replace(" ", "").replace(",", "").replace("，", "")

    # 1) 纯数字（最常见的填法）
    if bare.isdigit():
        value = int(bare)
        if _valid(value):
            return RoomCodeResult(value, "纯数字")
        return RoomCodeResult(None, f"{bare} 不在合理的直播间号范围内")

    # 2) 用户空间地址：明确说清楚，不然会被兜底逻辑当成房间号
    space = _SPACE_URL_RE.search(normalized)
    if space:
        return RoomCodeResult(
            None,
            f"这是用户空间地址（uid={space.group(1)}），不是直播间地址。"
            "直播间网址长这样：https://live.bilibili.com/1234567",
        )

    # 3) 标准直播间地址
    for pattern, reason in (
        (_LIVE_URL_RE, "从直播间网址提取"),
        (_LIVE_ALT_RE, "从直播间网址提取"),
    ):
        match = pattern.search(normalized)
        if match:
            value = int(match.group(1))
            if _valid(value):
                return RoomCodeResult(value, reason)

    query = _QUERY_ROOM_RE.search(normalized)
    if query:
        value = int(query.group(1))
        if _valid(value):
            return RoomCodeResult(value, "从网址参数里提取")

    # 4) 短链：联网展开后再解析
    short = _SHORT_URL_RE.search(normalized)
    if short:
        if not resolve_short:
            return RoomCodeResult(None, "看起来是 b23.tv 短链，需要联网展开")
        final = resolve_short_link(short.group(0))
        if not final:
            return RoomCodeResult(None, "短链展开失败，可能是网络问题；可以把网址在浏览器里打开后复制完整地址")
        if _SHORT_URL_RE.search(final) and "live.bilibili.com" not in final:
            # 还停在短链域名上，说明压根没跳转（多半是短链失效或过期了）
            return RoomCodeResult(None, f"这个短链没有跳转到直播间，可能已失效：{final[:80]}")
        nested = _LIVE_URL_RE.search(final) or _LIVE_ALT_RE.search(final) or _QUERY_ROOM_RE.search(final)
        if nested:
            value = int(nested.group(1))
            if _valid(value):
                return RoomCodeResult(value, "从短链展开后提取")
        return RoomCodeResult(None, f"短链展开了，但目标不是直播间地址：{final[:80]}")

    # 5) 兜底：整段文本里只有一个像房间号的数字
    candidates = _digits(normalized)
    if len(candidates) == 1:
        return RoomCodeResult(int(candidates[0]), "从文本里提取到唯一的数字")
    if len(candidates) > 1:
        preview = "、".join(candidates[:5])
        return RoomCodeResult(None, f"文本里有多个数字（{preview}），分不清哪个是房间号，请只粘直播间网址或房间号")

    return RoomCodeResult(None, "没找到直播间号")


def describe_input_help() -> str:
    """给用户看的输入说明。"""
    return (
        "可以填纯房间号，也可以直接粘直播间网址，例如：\n"
        "  1934302095\n"
        "  https://live.bilibili.com/1934302095\n"
        "  https://live.bilibili.com/1934302095?broadcast_type=0\n"
        "  手机 App 分享出来的 b23.tv 短链也认"
    )


if __name__ == "__main__":
    import sys

    from common import ensure_console_utf8

    ensure_console_utf8()
    samples = sys.argv[1:] or [
        "1934302095",
        "https://live.bilibili.com/1934302095",
        "https://live.bilibili.com/1934302095?broadcast_type=0&is_room_feed=1",
        "live.bilibili.com/1934302095",
        "https://live.bilibili.com/blanc/1934302095",
        "https://live.bilibili.com/h5/1934302095",
        "https://live.bilibili.com/p/html5/1934302095",
        "【哔哩哔哩】我的直播间 https://live.bilibili.com/1934302095?share_source=copy_web",
        "直播间号：1934302095",
        "１９３４３０２０９５",
        "https://space.bilibili.com/1234567",
        "https://live.bilibili.com/",
        "hello world",
    ]
    for item in samples:
        result = parse_room_code(item)
        mark = "OK  " if result.ok else "FAIL"
        print(f"[{mark}] {item!r:70} -> {result.room_id}  ({result.reason})")
