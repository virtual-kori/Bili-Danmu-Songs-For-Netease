"""点歌队列：线程安全的 FIFO，带去重与容量限制。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from netease_api import Song


@dataclass
class QueueItem:
    """队列里的一首歌。"""

    song: Song
    requester: str = "匿名"
    requester_uid: int = 0
    requested_at: float = field(default_factory=time.time)
    play_url: str = ""
    quality: str = ""
    note: str = ""

    @property
    def display(self) -> str:
        return self.song.display

    def short(self, index: int | None = None) -> str:
        prefix = f"{index}. " if index is not None else ""
        return f"{prefix}{self.song.display} [{self.song.duration_text}] 由 {self.requester} 点播"


class SongQueue:
    """点歌队列。

    所有公开方法都可以从多个线程安全调用。
    """

    def __init__(self, max_size: int = 20, dedupe: bool = True) -> None:
        self.max_size = max(1, max_size)
        self.dedupe = dedupe
        self._items: list[QueueItem] = []
        self._lock = threading.RLock()
        self._now_playing: QueueItem | None = None
        self._history: list[QueueItem] = []

    # ------------------------------------------------------------------ 写操作

    def add(self, item: QueueItem) -> tuple[bool, str]:
        """入队。返回 (是否成功, 说明文字)。"""
        with self._lock:
            if self.dedupe:
                if self._now_playing and self._now_playing.song.id == item.song.id:
                    return False, "这首歌正在播放中"
                for existing in self._items:
                    if existing.song.id == item.song.id:
                        return False, f"队列里已经有《{existing.song.name}》了"

            if len(self._items) >= self.max_size:
                return False, f"队列已满（上限 {self.max_size} 首）"

            self._items.append(item)
            return True, f"已加入队列，当前排在第 {len(self._items)} 位"

    def pop_next(self) -> QueueItem | None:
        """取出下一首，并标记为正在播放。"""
        with self._lock:
            if self._now_playing is not None:
                self._history.append(self._now_playing)
                if len(self._history) > 50:
                    self._history.pop(0)
            self._now_playing = self._items.pop(0) if self._items else None
            return self._now_playing

    def finish_current(self) -> None:
        """标记当前歌曲播放结束。"""
        with self._lock:
            if self._now_playing is not None:
                self._history.append(self._now_playing)
                if len(self._history) > 50:
                    self._history.pop(0)
            self._now_playing = None

    def clear(self) -> int:
        """清空等待队列（不影响正在播放的歌），返回清掉的条数。"""
        with self._lock:
            count = len(self._items)
            self._items.clear()
            return count

    def remove_at(self, index: int) -> QueueItem | None:
        """按下标（从 0 开始）移除队列中的歌。"""
        with self._lock:
            if 0 <= index < len(self._items):
                return self._items.pop(index)
            return None

    # ------------------------------------------------------------------ 读操作

    def snapshot(self) -> list[QueueItem]:
        with self._lock:
            return list(self._items)

    @property
    def now_playing(self) -> QueueItem | None:
        with self._lock:
            return self._now_playing

    @property
    def history(self) -> list[QueueItem]:
        with self._lock:
            return list(self._history)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def is_empty(self) -> bool:
        with self._lock:
            return not self._items and self._now_playing is None

    def describe(self, limit: int = 10) -> str:
        """生成一段适合打在控制台或弹幕里的队列描述。"""
        with self._lock:
            head = f"正在播放：{self._now_playing.display}" if self._now_playing else "当前没有在播放"

            if not self._items:
                return f"{head}；队列为空"
            shown = self._items[:limit]
            lines = [head, f"队列共 {len(self._items)} 首："]
            lines.extend(item.short(i + 1) for i, item in enumerate(shown))
            if len(self._items) > limit:
                lines.append(f"...还有 {len(self._items) - limit} 首")
            return "\n".join(lines)
