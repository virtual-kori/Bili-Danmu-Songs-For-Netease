"""公共工具：路径、配置读写、日志。"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

# 项目版本号。改这个值时请同步更新 CHANGELOG.md，并给仓库打上对应的 tag
# （V.0.1.1 对应 tag v0.1.1）。
VERSION = "V.0.1.1"
VERSION_TAG = "v0.1.1"

DEFAULT_CONFIG: dict[str, Any] = {
    "bilibili": {
        "room_id": 0,
        "sessdata": "",
        "bili_jct": "",
        "buvid3": "",
        "uid": 0,
        "reconnect_delay": 10,
    },
    "netease": {
        "api_base": "http://127.0.0.1:3000",
        "cookie": "",
        "level": "exhigh",
    },
    "player": {
        "backend": "auto",
        "volume": 70,
        "cache_dir": "cache",
        "prefetch": True,
    },
    "request": {
        "prefixes": ["点歌", "!点歌", "/点歌"],
        "max_queue": 20,
        "user_cooldown_sec": 30,
        "max_duration_sec": 600,
        "admin_uids": [],
        "allow_everyone_control": False,
        "log_all_danmaku": False,
    },
    "web": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
    },
    "lyric": {
        # 总开关：关掉后不再请求歌词接口，叠加层会显示"歌词未开启"
        "enabled": True,
        # 叠加层默认字体。留空则用系统默认字体栈
        "font_family": "",
        # 字号 / 行高 / 颜色，都是叠加层的默认值，面板里能改
        "font_size": 44,
        "line_height": 1.35,
        "color": "#ffffff",
        "active_color": "#7cc4ff",
        "translation_color": "#c9d4e6",
        # 逐句高亮的缩放倍数（当前句放大一点，更像卡拉OK）
        "active_scale": 1.06,
        # 叠加层显示方式：scroll 滚动列表 / focus 只显示当前句和邻居 / single 只有当前句
        "mode": "scroll",
        # 叠加层文字对齐：left / center / right
        "align": "center",
        # 竖屏时歌词显示在画面的什么位置：top / center / bottom
        "anchor": "bottom",
        # 用户自定义 CSS，追加在内置样式之后，优先级最高
        "css": "",
    },
    "autoplay": {
        "enabled": False,
        "source": "auto",
        "keywords": ["轻音乐", "纯音乐", "华语流行", "钢琴曲", "民谣"],
        "avoid_repeat": 40,
        "retry_delay_sec": 5,
    },
}

LOG_FORMAT = "%(asctime)s %(levelname)-5s %(message)s"
LOG_DATEFMT = "%H:%M:%S"

# 控制台日志的内存副本，给网页面板看（控制台窗口可能被挡住或没打开）
LOG_BUFFER: deque[dict[str, str]] = deque(maxlen=400)


class BufferLogHandler(logging.Handler):
    """把日志顺手存一份到内存，供 /api/logs 读取。"""

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            LOG_BUFFER.append(
                {
                    "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                    "level": record.levelname,
                    "msg": record.getMessage(),
                }
            )


def recent_logs(limit: int = 200) -> list[dict[str, str]]:
    """取最近的日志（新的在后）。"""
    if limit <= 0:
        return list(LOG_BUFFER)
    return list(LOG_BUFFER)[-limit:]


def ensure_console_utf8() -> None:
    """让 Windows 控制台能正常输出中文和 emoji。"""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def setup_logging(level: int = logging.INFO, name: str = "qdgj") -> logging.Logger:
    """配置并返回一个控制台 logger。

    顺便把 blivedm 自己的 logger 也接过来。它默认没有 handler，消息会走 Python 的
    last-resort handler，在控制台上吐出一大段裸堆栈（init_room 失败时尤其吓人）。
    """
    ensure_console_utf8()
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
        logger.addHandler(handler)

        # 同一份日志也存进内存，网页面板要显示
        buffer_handler = BufferLogHandler()
        logger.addHandler(buffer_handler)

        blivedm_logger = logging.getLogger("blivedm")
        blivedm_logger.handlers.clear()
        blivedm_logger.addHandler(handler)
        blivedm_logger.addHandler(buffer_handler)
        blivedm_logger.propagate = False

    logger.setLevel(level)
    logger.propagate = False
    logging.getLogger("blivedm").setLevel(level)
    return logger


def get_logger(name: str = "qdgj") -> logging.Logger:
    return logging.getLogger(name)


VENV_DIR = ROOT / ".venv"


def venv_health() -> tuple[bool, str]:
    """检查项目的 .venv 在这台机器上到底能不能用。

    虚拟环境是【不可移植】的：`.venv/pyvenv.cfg` 里记着创建它时那台机器上
    Python 的绝对路径。把整个文件夹拷到另一台电脑后，那个路径不存在，
    venv 就废了 —— 而且报错往往很难懂。这里提前把它识别出来，
    好让启动脚本给出"重跑 install.cmd"这种能照着做的提示。

    返回 (是否可用, 说明)。可用的说明是空串。
    """
    python_exe = VENV_DIR / "Scripts" / "python.exe"
    if not python_exe.is_file():
        return False, "还没建虚拟环境"

    with contextlib.suppress(OSError, ValueError):
        if Path(sys.prefix).resolve() != VENV_DIR.resolve():
            return False, f"当前不是用项目的虚拟环境在跑（sys.prefix={sys.prefix}）"

    cfg = VENV_DIR / "pyvenv.cfg"
    home = ""
    with contextlib.suppress(OSError):
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "home":
                home = value.strip().strip('"')
                break

    if home and not Path(home).exists():
        return (
            False,
            f"虚拟环境是在别的电脑上创建的（它依赖的 {home} 在本机不存在），请重新运行 install.cmd",
        )

    return True, ""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """把 override 合进 base 的副本，嵌套 dict 递归合并。"""
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    """读取 config.json，缺字段用默认值补齐；文件不存在则创建。"""
    path = Path(path)
    raw: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logging.getLogger("qdgj").warning("config.json 解析失败（%s），改用默认配置", exc)
            raw = {}
    else:
        save_config(DEFAULT_CONFIG, path)

    merged = deep_merge(DEFAULT_CONFIG, raw if isinstance(raw, dict) else {})
    if not path.is_file():
        save_config(merged, path)
    return merged


def save_config(config: dict[str, Any], path: Path | str = CONFIG_PATH) -> None:
    """原子地写回 config.json。"""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------ 小工具


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GB"


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
