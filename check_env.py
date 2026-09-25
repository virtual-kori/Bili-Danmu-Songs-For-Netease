"""环境自检：把跑起来需要的每一样东西都验一遍。

用法：
    python check_env.py
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys

from common import ROOT, RUNTIME_NODE, bundled_runtime, load_config, setup_logging, venv_health

OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"


def _check(label: str, ok: bool, detail: str = "", warn_only: bool = False) -> bool:
    tag = OK if ok else (WARN if warn_only else FAIL)
    print(f"{tag} {label}" + (f" —— {detail}" if detail else ""))
    return ok or warn_only


def main() -> int:
    setup_logging()
    print("=" * 66)
    print("  B站弹幕点歌 · 环境自检")
    print("=" * 66)

    problems = 0

    # ---------------------------------------------------------- 1. Python 与依赖
    print("\n[1/6] Python 与依赖包")
    print(f"      Python {sys.version.split()[0]}  ({sys.executable})")
    if bundled_runtime():
        # 便携包用 embeddable 解释器，本来就不是 venv，别误导用户
        _check("运行在便携包内置运行时中", True, "")
    else:
        in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
        _check("运行在项目虚拟环境中", in_venv, "" if in_venv else "建议用 run.cmd 启动", warn_only=True)

    # 运行时不能跨电脑（venv 会记原机器的 Python 路径），这块单独查一下，报错才好懂
    venv_ok, venv_reason = venv_health()
    runtime_label = (
        "运行时在本机可用（便携包内置）" if bundled_runtime()
        else "虚拟环境在本机可用（没有跨电脑拷贝的问题）"
    )
    if not _check(runtime_label, venv_ok, venv_reason):
        problems += 1

    # 便携包自带 node 时优先认它，这样自检结果和实际运行时是一致的
    node = str(RUNTIME_NODE) if RUNTIME_NODE.is_file() else shutil.which("node")
    if node:
        try:
            node_version = subprocess.run(
                [node, "--version"], capture_output=True, text=True, timeout=15
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            node_version = ""
        label = "Node.js 可用（便携包内置）" if RUNTIME_NODE.is_file() else "Node.js 可用"
        _check(label, True, node_version or node)
    else:
        _check("Node.js 可用", False, "没找到 node，网易云 API 服务需要 Node.js 18+")
        problems += 1

    required = {
        "blivedm": "B站弹幕库",
        "aiohttp": "异步 HTTP（blivedm 依赖）",
        "brotli": "弹幕数据解压（blivedm 依赖）",
        "requests": "调用网易云 API",
        "qrcode": "终端显示登录二维码",
    }
    for module, desc in required.items():
        try:
            importlib.import_module(module)
            _check(f"{module} ({desc})", True)
        except ImportError:
            _check(f"{module} ({desc})", False, "缺失，请重新运行 install.cmd")
            problems += 1

    # ---------------------------------------------------------- 2. 网易云 API
    print("\n[2/6] 网易云 API 服务")
    config = load_config()
    api_base = config["netease"]["api_base"]

    from netease_api import NeteaseClient, NeteaseError

    client = NeteaseClient(base_url=api_base, cookie=config["netease"].get("cookie", ""))
    if client.ping():
        _check(f"服务可访问：{api_base}", True)
        try:
            songs = client.search("晴天", limit=1)
            _check("搜索接口正常", bool(songs), songs[0].display if songs else "没有返回结果")
        except NeteaseError as exc:
            _check("搜索接口正常", False, str(exc))
            problems += 1
    else:
        _check(f"服务可访问：{api_base}", False, "连不上，请先双击 start_netease_api.cmd")
        problems += 1

    # ---------------------------------------------------------- 3. 登录状态
    print("\n[3/6] 网易云登录状态（决定能不能放 VIP 歌曲）")
    if not config["netease"].get("cookie"):
        _check("已扫码登录", False, "还没登录，请运行 login_netease.cmd；未登录时 VIP 歌曲拿不到完整地址")
        problems += 1
    else:
        try:
            account = client.current_account()
        except NeteaseError as exc:
            account = None
            _check("Cookie 有效", False, str(exc))
            problems += 1
        if account:
            _check("Cookie 有效", True, f"{account['nickname']} (uid={account['uid']})")
            _check(
                "会员权益",
                bool(account["is_vip"]),
                "是 VIP，可播放会员歌曲" if account["is_vip"] else "非 VIP，会员歌曲只能试听",
                warn_only=not account["is_vip"],
            )
        elif config["netease"].get("cookie"):
            _check("Cookie 有效", False, "服务端显示未登录，请重新运行 login_netease.cmd")
            problems += 1

    # ---------------------------------------------------------- 4. 播放器
    print("\n[4/6] 播放后端")
    from player import AudioPlayer

    backends = AudioPlayer.describe_backends()
    for name, available in backends.items():
        if name == "null":
            continue
        note = {
            "mpv": "推荐：支持暂停/音量/秒切歌",
            "ffplay": "支持暂停（靠按键），音量需重启生效",
            "pygame": "先下载整首再播，支持暂停/音量",
            "wmp": "Windows 自带，无播放控制",
        }.get(name, "")
        _check(f"{name} {note}", available, "" if available else "未安装", warn_only=True)

    if not any(backends[n] for n in ("mpv", "ffplay", "pygame", "wmp")):
        _check("至少有一个可用播放器", False, "只能静音模拟播放")
        problems += 1
    else:
        chosen = AudioPlayer()
        _check("实际选用", True, chosen.backend_name)

    # ---------------------------------------------------------- 5. 直播间配置
    print("\n[5/6] B站直播间配置")
    room_id = int(config["bilibili"].get("room_id", 0) or 0)
    _check("已配置直播间号", room_id > 0, f"room_id={room_id}" if room_id else "请在 config.json 里填 bilibili.room_id")
    if room_id <= 0:
        problems += 1

    has_sess = bool(config["bilibili"].get("sessdata"))
    has_jct = bool(config["bilibili"].get("bili_jct"))
    _check(
        "B站 Cookie（用于发弹幕反馈）",
        has_sess and has_jct,
        "已配置" if (has_sess and has_jct) else "未配置：仍能收弹幕点歌，只是不会回发弹幕",
        warn_only=True,
    )

    # ---------------------------------------------------------- 6. 目录与文件
    print("\n[6/6] 项目文件")
    for name in ("config.json", "netease_api.py", "player.py", "song_queue.py", "danmaku_bot.py"):
        path = ROOT / name
        _check(name, path.is_file())
    api_dir = ROOT / "netease-api" / "serve.js"
    _check("netease-api/serve.js", api_dir.is_file())

    print("\n" + "=" * 66)
    if problems:
        print(f"发现 {problems} 个必须解决的问题，详见上面标 [FAIL] 的条目。")
    else:
        print("一切就绪，可以运行 start_bot.cmd 开始点歌了。")
    print("=" * 66)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
