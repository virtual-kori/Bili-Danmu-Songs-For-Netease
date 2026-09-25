"""网易云音乐扫码登录。

流程：申请 unikey -> 生成二维码 -> 轮询扫码状态 -> 成功后把 Cookie 存进 config.json。
拿到带 VIP 权益的 Cookie 之后，取播放地址就能拿到完整高音质歌曲。

用法：
    python qr_login.py
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path

from common import CONFIG_PATH, ROOT, load_config, save_config, setup_logging
from netease_api import NeteaseClient, NeteaseError

QR_PNG = ROOT / "login_qrcode.png"

# /login/qr/check 的状态码
STATUS_EXPIRED = 800
STATUS_WAITING = 801
STATUS_SCANNED = 802
STATUS_CONFIRMED = 803


def _save_png(data_url: str) -> Path | None:
    """把 data:image/png;base64,... 存成文件。"""
    if not data_url:
        return None
    payload = data_url.split(",", 1)[1] if "," in data_url else data_url
    try:
        QR_PNG.write_bytes(base64.b64decode(payload))
    except (ValueError, OSError):
        return None
    return QR_PNG


def _print_ascii_qr(text: str) -> bool:
    """在终端里直接画出二维码，省得切窗口。"""
    try:
        import qrcode
    except ImportError:
        return False
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return True
    except Exception:  # noqa: BLE001 - 终端不支持就静默跳过
        return False


def _open_image(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}"')
    except OSError:
        pass


def login(timeout_sec: int = 300, open_image: bool = True) -> str | None:
    """跑完整个扫码流程，成功返回 Cookie 字符串，失败/超时返回 None。"""
    log = setup_logging()
    config = load_config()
    client = NeteaseClient(base_url=config["netease"]["api_base"])

    log.info("正在向本地 API 服务申请二维码…")
    try:
        key = client.qr_key()
        qr = client.qr_create(key, qrimg=True)
    except NeteaseError as exc:
        log.error("%s", exc)
        log.error("请先启动网易云 API 服务：双击 start_netease_api.cmd")
        return None

    log.info("=" * 62)
    log.info("请用【手机网易云音乐 App】扫码登录（我 -> 右上角扫一扫）")
    log.info("=" * 62)

    if not _print_ascii_qr(qr["qrurl"]):
        log.info("（终端画不出二维码，请看弹出的图片）")

    png = _save_png(qr["qrimg"])
    if png:
        log.info("二维码图片：%s", png)
        if open_image:
            _open_image(png)

    deadline = time.time() + timeout_sec
    last_status = None
    scanned_notice = False

    while time.time() < deadline:
        time.sleep(2)
        try:
            result = client.qr_check(key)
        except NeteaseError as exc:
            log.warning("查询扫码状态出错：%s", exc)
            continue

        code = result.get("code")
        if code == STATUS_EXPIRED:
            log.error("二维码已过期，请重新运行本脚本")
            return None
        if code == STATUS_WAITING:
            if last_status != code:
                log.info("等待扫码…")
        elif code == STATUS_SCANNED:
            if not scanned_notice:
                log.info("已扫码，请在手机上点确认")
                scanned_notice = True
        elif code == STATUS_CONFIRMED:
            cookie = client.cookie_from_qr_response(result)
            if not cookie:
                log.error("登录成功但没拿到 Cookie，响应：%s", result)
                return None
            log.info("登录成功，Cookie 已获取（%d 字符）", len(cookie))
            return cookie
        else:
            message = result.get("message") or result.get("msg") or ""
            log.debug("未处理的状态码 %s %s", code, message)

        last_status = code

    log.error("等待扫码超时（%d 秒）", timeout_sec)
    return None


def persist_cookie(cookie: str) -> None:
    """把 Cookie 写进 config.json，并顺手验证账号信息。"""
    log = setup_logging()
    config = load_config()
    config["netease"]["cookie"] = cookie
    save_config(config)
    log.info("Cookie 已写入 %s", CONFIG_PATH)

    client = NeteaseClient(base_url=config["netease"]["api_base"], cookie=cookie)
    try:
        account = client.current_account()
    except NeteaseError as exc:
        log.warning("验证登录状态失败：%s", exc)
        return

    if not account:
        log.warning("服务端说当前仍未登录，Cookie 可能已失效，建议重跑一次")
        return

    log.info("-" * 62)
    log.info("账号：%s (uid=%s)", account["nickname"], account["uid"])
    log.info("VIP ：%s", "是（可播放 VIP 歌曲）" if account["is_vip"] else "否（VIP 歌曲只能试听）")
    log.info("-" * 62)


def main() -> int:
    parser = argparse.ArgumentParser(description="网易云音乐扫码登录")
    parser.add_argument("--timeout", type=int, default=300, help="等待扫码的秒数，默认 300")
    parser.add_argument("--no-open", action="store_true", help="不要自动弹出二维码图片窗口")
    args = parser.parse_args()

    cookie = login(timeout_sec=args.timeout, open_image=not args.no_open)
    if not cookie:
        return 1
    persist_cookie(cookie)

    log = setup_logging()
    log.info("")
    log.info("下一步：运行 python danmaku_bot.py 开始监听弹幕点歌")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
