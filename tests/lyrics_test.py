"""LRC 歌词解析测试。

不依赖网络，纯逻辑测试：
    .venv\\Scripts\\python.exe tests\\lyrics_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import ensure_console_utf8  # noqa: E402
from lyrics import find_line_index, format_timestamp, parse_lrc, parse_lyrics  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    print(f"{'[通过]' if ok else '[失败]'} {label}" + (f"  —— {detail}" if detail else ""))
    return ok


# 一份典型的网易云歌词：带作词信息、间奏空行、副歌重复
LRC = """[00:00.000] 作词 : 张三
[00:00.000] 作曲 : 李四
[00:01.500] 第一句歌词
[00:05.250] 第二句歌词
[00:09.000]
[00:12.000] 副歌
[00:13.000] 副歌
[00:14.000] 结尾
"""

TLYRIC = """[00:01.500] Line one
[00:05.250] Line two
[00:12.010] Chorus
[00:13.000] Chorus
[00:14.000] End
"""


def main() -> int:
    ensure_console_utf8()
    print("=" * 66)
    print("  歌词解析测试")
    print("=" * 66)

    # ---------------------------------------------------------- 基本解析
    ly = parse_lrc(LRC)
    check("解析出全部行", len(ly) == 8, f"{len(ly)} 行")
    check("有时间戳，可以逐句同步", ly.synced)
    check("第一行文本正确", ly.lines[0].text == "作词 : 张三", repr(ly.lines[0].text))
    check("第一行时间正确", ly.lines[0].time == 0.0, repr(ly.lines[0].time))

    # 小数位数不定的时间戳要按位数补零：.5 = 500ms，.25 = 250ms
    check("毫秒按位数解析（.5 → 0.5s）", ly.lines[2].time == 1.5, repr(ly.lines[2].time))
    check("毫秒按位数解析（.25 → 0.25s）", ly.lines[3].time == 5.25, repr(ly.lines[3].time))

    # ---------------------------------------------------------- 间奏空行
    interlude = [ln for ln in ly.timed if ln.text == ""]
    check("间奏空行被保留（高亮能走过去）", len(interlude) == 1, f"{len(interlude)} 个空行")

    # ---------------------------------------------------------- 重复行不能定位错
    # 13.0 和 12.0 都是"副歌"，必须各自定位到自己那一句，不能都用第一条
    idx_12 = ly.index_at(12.0)
    idx_13 = ly.index_at(13.0)
    check("重复歌词行定位不串位", idx_12 != idx_13, f"12s->#{idx_12} 13s->#{idx_13}")
    check("12s 定位到第 6 行", ly.lines[idx_12].time == 12.0, repr(ly.lines[idx_12].time))
    check("13s 定位到第 7 行", ly.lines[idx_13].time == 13.0, repr(ly.lines[idx_13].time))

    # ---------------------------------------------------------- 边界
    # 0.0s 有两行同时间戳（作词/作曲），取其中任意一行都算对
    check("0.0s 命中第一句区间", ly.index_at(0.0) in (0, 1), repr(ly.index_at(0.0)))
    check("同时间戳取最后一行", ly.index_at(0.0) == 1, repr(ly.index_at(0.0)))
    check("1.5s 命中第一句歌词", ly.index_at(1.5) == 2, repr(ly.index_at(1.5)))
    after = parse_lrc("[00:10.000] 只有一句\n")
    check("还没到第一句时返回 -1", after.index_at(3.0) == -1, repr(after.index_at(3.0)))
    check("超过最后一句停最后一句", after.index_at(999.0) == after.index_at(10.5) == 0, "")
    check("空歌词不崩", len(parse_lrc("")) == 0 and not parse_lrc("").synced)
    check("find_line_index 与 index_at 一致", find_line_index(ly, 12.5) == ly.index_at(12.5))

    # ---------------------------------------------------------- 双语
    bi = parse_lyrics(LRC, TLYRIC)
    check("识别出双语", bi.has_translation)
    check("译文对齐到原文行", bi.lines[2].translation == "Line one", repr(bi.lines[2].translation))
    check("译文按容差匹配（12.010 vs 12.000）", bi.lines[5].translation == "Chorus", repr(bi.lines[5].translation))
    check("译文行定位正确", bi.lines[bi.index_at(12.5)].translation == "Chorus", "")
    check("没有译文的行是空串", bi.lines[0].translation == "", repr(bi.lines[0].translation))

    # ---------------------------------------------------------- 整体偏移
    shifted = parse_lrc("[offset:+500]\n[00:10.000] 偏移测试\n")
    check("offset 标签不被当成歌词", len(shifted) == 1, f"{len(shifted)} 行：{[l.text for l in shifted.lines]}")
    check("offset 标签生效（+500ms）", shifted.lines[0].time == 10.5, repr(shifted.lines[0].time))
    negative = parse_lrc("[offset:-1000]\n[00:10.000] 负偏移\n")
    check("负 offset 生效", negative.lines[0].time == 9.0, repr(negative.lines[0].time))
    meta = parse_lrc("[ti:歌名]\n[ar:歌手]\n[by:某人]\n[00:01.000] 真正的歌词\n")
    check("ti/ar/by 标签被过滤", len(meta) == 1, f"{len(meta)} 行：{[l.text for l in meta.lines]}")

    # ---------------------------------------------------------- 一行多时间戳
    multi = parse_lrc("[00:01.000][00:30.000] 副歌重复\n")
    check("一行多时间戳展开成多行", len(multi) == 2, f"{len(multi)} 行")
    check("展开后两句文本一致", multi.lines[0].text == multi.lines[1].text == "副歌重复", "")
    check("展开后时间正确", multi.times() == [1.0, 30.0], str(multi.times()))

    # ---------------------------------------------------------- 无时间戳歌词
    plain = parse_lrc("第一行\n第二行\n第三行\n")
    check("纯文本歌词也能解析", len(plain) == 3, f"{len(plain)} 行")
    check("纯文本歌词不做同步", not plain.synced, "")
    check("纯文本歌词保留文本", plain.lines[1].text == "第二行", repr(plain.lines[1].text))
    check("纯文本歌词 index_at 返回 -1", plain.index_at(5.0) == -1, "")

    # ---------------------------------------------------------- 重复去重
    dupe = parse_lrc("[00:01.000] 同一句\n[00:01.000] 同一句\n")
    check("完全重复的行被去掉", len(dupe) == 1, f"{len(dupe)} 行")

    # ---------------------------------------------------------- 时间戳格式
    check("format_timestamp 正确", format_timestamp(72.345) == "[01:12.345]", format_timestamp(72.345))

    # ---------------------------------------------------------- 性能（二分查找）
    import time as _time

    big = parse_lrc("".join(f"[{i // 60:02d}:{i % 60:02d}.000] 第 {i} 句\n" for i in range(3000)))
    started = _time.perf_counter()
    for i in range(2000):
        big.index_at(i * 1.3)
    cost = (_time.perf_counter() - started) * 1000
    check("3000 行歌词 2000 次定位够快（<100ms）", cost < 100, f"{cost:.1f}ms")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 66)
    print(f"  结果：{passed}/{total} 通过")
    for label, ok, _ in results:
        if not ok:
            print(f"    未通过：{label}")
    print("=" * 66)
    return 1 if passed != total else 0


if __name__ == "__main__":
    raise SystemExit(main())
