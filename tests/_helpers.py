"""测试用的共享小工具（下划线开头，不当作测试用例收集）。"""

from __future__ import annotations

import random
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pick_unmatched_keyword() -> str:
    """找一个确实搜不到任何结果的词。

    网易云搜索是模糊匹配：随手编个 "zzqqxx9f8e7d6c5b4a3" 照样能给你返回 8 条结果，
    所以"搜不到"这条分支不能靠人肉编词，必须真的问一次接口确认返回 0 条。
    """
    from netease_api import NeteaseClient, NeteaseError

    client = NeteaseClient()
    for _ in range(8):
        token = "".join(random.choices(string.ascii_lowercase + string.digits, k=22))
        try:
            if not client.search(token, limit=3):
                return token
        except NeteaseError:
            break
    return "asdkjfhaskjdfhqwertyuiopzxcv"
