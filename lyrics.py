"""LRC 歌词解析与同步。

网易云 `/lyric` 接口给回来的是 LRC 文本，长这样：

    [00:00.000] 作词 : 张三
    [00:12.340] 第一句歌词
    [00:15.900] 第二句歌词
    [00:15.900]

这个模块把它解析成带时间戳的行，并支持：
    * 中英双语 —— 译文（tlyric）按时间戳对齐到原文行上
    * 按播放进度定位当前该显示哪一句
    * 容错 —— 没时间戳的纯文本歌词也能显示（不做逐句同步）

只依赖标准库，方便单独测试。
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

__all__ = [
    "LyricLine",
    "Lyrics",
    "parse_lrc",
    "parse_lyrics",
    "find_line_index",
    "format_timestamp",
]

# [mm:ss.fff] / [mm:ss:ff] / [mm:ss]，时间戳可以有多个
_TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")

# 译文和原文的时间戳往往差几毫秒，配对时给一点容差（秒）
_TRANSLATION_TOLERANCE = 0.35


@dataclass(frozen=True)
class LyricLine:
    """一行歌词。

    time 为 None 表示这行没有时间戳（纯文本歌词），无法参与逐句同步。
    """

    time: float | None
    text: str
    translation: str = ""

    @property
    def timed(self) -> bool:
        return self.time is not None

    def to_json(self) -> dict[str, object]:
        return {"time": self.time, "text": self.text, "translation": self.translation}


# [offset:+500] / [ti:歌名] / [ar:歌手] 这类标签行，不是歌词，要丢掉
_META_TAG = re.compile(r"^\[(?:offset|ti|ar|al|by|length|re|ve|kana):.*\]$", re.IGNORECASE)


@dataclass
class Lyrics:
    """一首歌的完整歌词。"""

    lines: list[LyricLine] = field(default_factory=list)
    # 只有带时间戳的行，按时间升序 —— 同步时查这个，避免每次过滤
    timed: list[LyricLine] = field(default_factory=list)
    # timed[i] 对应 lines 里的下标。不能用 lines.index()：重复的歌词行会定位到错的那一句
    timed_index: list[int] = field(default_factory=list)
    has_translation: bool = False
    # 缓存 timed 的时间戳，避免每次查找都重建列表（面板每秒都在轮询）
    _times: list[float] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self._times:
            self._times = [ln.time for ln in self.timed if ln.time is not None]

    def __bool__(self) -> bool:
        return bool(self.lines)

    def __len__(self) -> int:
        return len(self.lines)

    @property
    def synced(self) -> bool:
        """有没有时间戳可以逐句同步。"""
        return bool(self.timed)

    def index_at(self, elapsed: float) -> int:
        """当前时刻该高亮第几行（在 lines 里的下标），还没到第一句返回 -1。"""
        times = self._times
        if not times:
            return -1
        pos = bisect.bisect_right(times, elapsed) - 1
        if pos < 0:
            return -1
        return self.timed_index[pos]

    def times(self) -> list[float]:
        """带时间戳行的时间点（升序），返回的是内部缓存，调用方不要改。"""
        return self._times

    def to_json(self) -> dict[str, object]:
        return {
            "lines": [ln.to_json() for ln in self.lines],
            "synced": self.synced,
            "has_translation": self.has_translation,
        }


def _parse_offset(text: str) -> float:
    """读 [offset:+500] 这类整体偏移标签（单位毫秒）。"""
    match = re.search(r"\[offset:\s*([+-]?\d+)\s*\]", text)
    if not match:
        return 0.0
    try:
        return int(match.group(1)) / 1000.0
    except ValueError:
        return 0.0


def _to_seconds(minutes: str, seconds: str, fraction: str) -> float:
    """把时间戳三段转成秒。

    小数部分的位数是不定的：`.5` 是 500ms，`.34` 是 340ms，`.345` 是 345ms，
    所以按位数补零，不能直接当整数用。
    """
    total = int(minutes) * 60 + int(seconds)
    if not fraction:
        return float(total)
    return total + int(fraction) / (10 ** len(fraction))


def _parse_entries(lrc: str) -> list[tuple[float, str]]:
    """把 LRC 解析成 (时间, 文本) 列表，按时间升序、去重。

    一行有多个时间戳标签时会展开成多条（副歌重复用得上）。
    """
    entries: list[tuple[float, str]] = []
    for raw in lrc.splitlines():
        line = raw.strip()
        if not line:
            continue

        matches = list(_TIME_TAG.finditer(line))

        if not matches:
            # 没有时间戳：可能是元数据标签行，也可能是纯文本歌词
            if _META_TAG.match(line):
                continue
            if line:
                entries.append((-1.0, line))
            continue

        # 文本取最后一个时间戳标签之后的内容
        text = line[matches[-1].end() :].strip()

        for match in matches:
            try:
                moment = _to_seconds(match.group(1), match.group(2), match.group(3) or "")
            except ValueError:
                continue
            entries.append((moment, text))

    # 稳定排序，让同时刻的重复行保持原顺序
    entries.sort(key=lambda item: item[0])

    # 同一时间戳 + 同一文本的重复行去掉（有些歌词源会重复贴一遍）
    deduped: list[tuple[float, str]] = []
    for moment, text in entries:
        if deduped and deduped[-1] == (moment, text):
            continue
        deduped.append((moment, text))
    return deduped


def _merge_translations(
    base: list[tuple[float, str]],
    tlyric: str,
    tolerance: float = _TRANSLATION_TOLERANCE,
) -> dict[int, str]:
    """把译文按时间戳对到原文行的下标上。

    译文行数一般和原文一致，但时间戳可能差几毫秒，所以用最近的原文行配对；
    差得太远（超过 tolerance）就当对不上，忽略掉，免得错位。
    """
    if not tlyric.strip() or not base:
        return {}

    trans = _parse_entries(tlyric)
    if not trans:
        return {}

    timed_index: list[tuple[float, int]] = [
        (moment, idx) for idx, (moment, _) in enumerate(base) if moment >= 0
    ]
    if not timed_index:
        return {}

    moments = [item[0] for item in timed_index]
    result: dict[int, str] = {}
    for moment, text in trans:
        if moment < 0 or not text:
            continue
        # 二分找到插入点，再比较左右两边哪个更近
        pos = bisect.bisect_left(moments, moment)
        best_idx = None
        best_gap = None
        for candidate in (pos - 1, pos):
            if 0 <= candidate < len(timed_index):
                gap = abs(moments[candidate] - moment)
                if best_gap is None or gap < best_gap:
                    best_gap = gap
                    best_idx = timed_index[candidate][1]
        if best_idx is not None and best_gap is not None and best_gap <= tolerance:
            # 同一行已经有译文就不覆盖（先来的优先）
            result.setdefault(best_idx, text)
    return result


def parse_lyrics(lrc: str, tlyric: str = "", tolerance: float = _TRANSLATION_TOLERANCE) -> Lyrics:
    """解析原文 + 译文，返回合并后的 Lyrics。"""
    base = _parse_entries(lrc or "")
    if not base:
        # 原文是空的，但译文有内容时退而用译文
        base = _parse_entries(tlyric or "")
        tlyric = ""

    offset = _parse_offset(lrc or "")
    translations = _merge_translations(base, tlyric or "", tolerance=tolerance)

    lines: list[LyricLine] = []
    for idx, (moment, text) in enumerate(base):
        moment = None if moment < 0 else round(moment + offset, 3)
        lines.append(LyricLine(time=moment, text=text, translation=translations.get(idx, "")))

    # 加了 offset 之后顺序可能变化，按时间重排，并记住每行在 lines 里的原始下标
    ordered = sorted(range(len(lines)), key=lambda i: lines[i].time if lines[i].timed else 0.0)
    timed = [lines[i] for i in ordered if lines[i].timed]
    timed_index = [i for i in ordered if lines[i].timed]

    return Lyrics(
        lines=lines,
        timed=timed,
        timed_index=timed_index,
        has_translation=any(ln.translation for ln in lines),
    )


def parse_lrc(lrc: str) -> Lyrics:
    """只解析原文歌词（不带头译文）。"""
    return parse_lyrics(lrc, "")


def find_line_index(lyrics: Lyrics, elapsed: float) -> int:
    """按播放进度找当前行在 lines 里的下标，还没到第一句返回 -1。"""
    return lyrics.index_at(elapsed)


def format_timestamp(seconds: float) -> str:
    """秒数转成 LRC 时间戳 [mm:ss.fff]，导出歌词时用。"""
    seconds = max(0.0, seconds)
    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    return f"[{minutes:02d}:{rest:06.3f}]"
