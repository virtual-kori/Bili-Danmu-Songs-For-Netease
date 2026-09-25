"""网易云音乐 API 客户端封装。

对接本地运行的 NeteaseCloudMusicApi / @neteasecloudmusicapienhanced/api 服务，
不需要任何开发者凭证，登录态（含 VIP 权益）通过扫码得到的 Cookie 提供。

对外主要能力：
    search()        搜索单曲
    song_url()      取播放地址（VIP Cookie 下可拿到无损/高码率完整歌曲）
    lyric()         取歌词
    song_detail()   取歌曲详情
    qr_*()          扫码登录
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import requests


class NeteaseError(RuntimeError):
    """调用网易云 API 失败。"""


@dataclass(frozen=True)
class Song:
    """一首歌的最小信息集合。"""

    id: int
    name: str
    artists: str
    album: str = ""
    duration_ms: int = 0
    fee: int = 0

    @property
    def duration_text(self) -> str:
        total = max(0, self.duration_ms // 1000)
        return f"{total // 60}:{total % 60:02d}"

    @property
    def display(self) -> str:
        return f"{self.name} - {self.artists}"

    @property
    def is_vip_only(self) -> bool:
        """fee==1 表示 VIP 歌曲，fee==0 免费，fee==4 购买专辑，fee==8 低音质免费。"""
        return self.fee == 1


@dataclass
class PlayUrl:
    """一次播放地址查询的结果。"""

    url: str | None
    level: str = ""
    br: int = 0
    size: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.url)


# 音质等级，从低到高。数值越大码率越高，需要会员等级也越高。
QUALITY_LEVELS = ("standard", "higher", "exhigh", "lossless", "hires")

_QUALITY_LABEL = {
    "standard": "标准",
    "higher": "较高",
    "exhigh": "极高",
    "lossless": "无损",
    "hires": "Hi-Res",
    "jyeffect": "沉浸环绕声",
    "sky": "沉浸环绕声",
    "jymaster": "超清母带",
}


class NeteaseClient:
    """本地网易云 API 服务的瘦客户端。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3000",
        cookie: str = "",
        level: str = "exhigh",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie or ""
        self.level = level if level in QUALITY_LEVELS else "exhigh"
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                )
            }
        )

    # ------------------------------------------------------------------ 底层

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发起一次 GET 请求，返回解析后的 JSON。

        服务端未启动、返回非 JSON（例如 HTML 错误页）都会抛 NeteaseError。
        """
        query: dict[str, Any] = dict(params or {})
        if self.cookie:
            query["cookie"] = self.cookie
        # 让服务端不要对响应做加密，方便直接解析 JSON
        query.setdefault("timestamp", int(time.time() * 1000))
        query.setdefault("realIP", "127.0.0.1")

        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, params=query, timeout=self.timeout)
        except requests.RequestException as exc:
            raise NeteaseError(
                f"无法连接网易云 API 服务 ({self.base_url})，请先启动它：{exc}"
            ) from exc

        if resp.status_code != 200:
            raise NeteaseError(f"{path} 返回 HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise NeteaseError(
                f"{path} 返回的不是 JSON，服务可能没起来。前 200 字符：{resp.text[:200]!r}"
            ) from exc

        if not isinstance(data, dict):
            raise NeteaseError(f"{path} 返回了意外的结构：{type(data).__name__}")

        code = data.get("code")
        if code is not None and code not in (200, 800, 801, 802, 803):
            message = data.get("message") or data.get("msg") or ""
            raise NeteaseError(f"{path} 失败：code={code} {message}".strip())
        return data

    def ping(self) -> bool:
        """检查本地 API 服务是否可用。"""
        try:
            self._get("/search", {"keywords": "test", "type": 1, "limit": 1})
            return True
        except NeteaseError:
            return False

    # ------------------------------------------------------------------ 搜索

    @staticmethod
    def _song_from_raw(raw: dict[str, Any]) -> Song:
        artists = raw.get("artists") or raw.get("ar") or []
        names = [a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name")]
        album_raw = raw.get("album") or raw.get("al") or {}
        album = album_raw.get("name", "") if isinstance(album_raw, dict) else ""
        duration = raw.get("duration") or raw.get("dt") or 0
        return Song(
            id=int(raw.get("id", 0)),
            name=str(raw.get("name", "")),
            artists=" / ".join(names) if names else "未知歌手",
            album=str(album or ""),
            duration_ms=int(duration or 0),
            fee=int(raw.get("fee", 0) or 0),
        )

    def search(self, keywords: str, limit: int = 10) -> list[Song]:
        """按关键字搜索单曲，返回候选列表（保持网易云的排序）。"""
        keywords = (keywords or "").strip()
        if not keywords:
            return []

        data = self._get(
            "/search",
            {"keywords": keywords, "type": 1, "limit": max(1, min(limit, 50)), "offset": 0},
        )
        result = data.get("result") or {}
        raw_songs: Iterable[dict[str, Any]] = result.get("songs") or []
        songs: list[Song] = []
        for raw in raw_songs:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            try:
                songs.append(self._song_from_raw(raw))
            except (TypeError, ValueError):
                continue
        return songs

    def song_detail(self, song_ids: int | Iterable[int]) -> list[Song]:
        """批量取歌曲详情（搜索接口偶尔缺字段时用来补全）。"""
        ids = [song_ids] if isinstance(song_ids, int) else list(song_ids)
        if not ids:
            return []
        data = self._get(
            "/song/detail",
            {"ids": ",".join(str(i) for i in ids)},
        )
        songs = data.get("songs") or []
        return [self._song_from_raw(s) for s in songs if isinstance(s, dict)]

    # ----------------------------------------------------- 随机播放用的推荐源

    def personal_fm(self) -> list[Song]:
        """私人FM：一次给 1~3 首。

        实测未登录也能拿到内容（只是没那么"懂你"），登录后是真正的个性化推荐。
        想连续随机播放就多调几次。
        """
        data = self._get("/personal_fm")
        raw = data.get("data") or []
        if not isinstance(raw, list):
            return []
        songs: list[Song] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                songs.append(self._song_from_raw(item))
            except (TypeError, ValueError):
                continue
        return songs

    def recommend_songs(self) -> list[Song]:
        """每日推荐（一次给一批，约 30 首）。未登录时也有内容。"""
        data = self._get("/recommend/songs")
        payload = data.get("data") or {}
        if not isinstance(payload, dict):
            return []
        raw = payload.get("dailySongs") or payload.get("recommend") or []
        songs: list[Song] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                songs.append(self._song_from_raw(item))
            except (TypeError, ValueError):
                continue
        return songs

    # --------------------------------------------------------------- 播放地址

    def song_url(self, song_id: int) -> PlayUrl:
        """取播放地址。

        优先用 /song/url/v1（可按 level 指定音质），失败则退回 /song/url。
        """
        data: dict[str, Any] = {}
        try:
            data = self._get("/song/url/v1", {"id": song_id, "level": self.level})
        except NeteaseError:
            data = self._get("/song/url", {"id": song_id, "br": 320000})

        entries = data.get("data") or []
        if not entries:
            return PlayUrl(url=None, reason="接口没有返回播放数据")

        entry = entries[0]
        if not isinstance(entry, dict):
            return PlayUrl(url=None, reason="播放数据格式异常")

        url = entry.get("url")
        if not url:
            free_trial = entry.get("freeTrialInfo")
            if free_trial:
                reason = "该歌曲只能试听片段（需要 VIP 或数字专辑）"
            else:
                reason = "拿不到播放地址，可能是版权限制、下架或需要登录"
            return PlayUrl(url=None, reason=reason)

        level = str(entry.get("level") or entry.get("encodeType") or "")
        return PlayUrl(
            url=str(url),
            level=_QUALITY_LABEL.get(level, level),
            br=int(entry.get("br") or 0),
            size=int(entry.get("size") or 0),
        )

    @staticmethod
    def upgrade_url_to_https(url: str) -> str:
        """网易云有时返回 http 地址，部分播放器会拒绝，统一换成 https。"""
        if url.startswith("http://"):
            return "https://" + url[len("http://") :]
        return url

    # ------------------------------------------------------------------- 歌词

    def lyric(self, song_id: int) -> str:
        """取 LRC 歌词文本，没有则返回空串。"""
        data = self._get("/lyric", {"id": song_id})
        lrc = (data.get("lrc") or {}).get("lyric") or ""
        return str(lrc)

    # --------------------------------------------------------------- 登录相关

    def login_status(self) -> dict[str, Any]:
        """查询当前 Cookie 的登录状态。"""
        data = self._get("/login/status")
        payload = data.get("data") or {}
        return payload if isinstance(payload, dict) else {}

    def current_account(self) -> dict[str, Any] | None:
        """返回当前登录账号信息（昵称、VIP 类型），未登录返回 None。"""
        payload = self.login_status()
        profile = payload.get("profile")
        if not profile:
            return None
        account = payload.get("account") or {}
        vip = profile.get("vipType", 0)
        return {
            "uid": profile.get("userId"),
            "nickname": profile.get("nickname", ""),
            "vip_type": vip,
            "is_vip": bool(vip) or bool(account.get("vipType")),
        }

    def qr_key(self) -> str:
        """申请一个扫码登录用的 unikey。"""
        data = self._get("/login/qr/key")
        key = ((data.get("data") or {}) if isinstance(data.get("data"), dict) else {}).get("unikey")
        if not key:
            raise NeteaseError("没能取到二维码 key，请确认 API 服务正常")
        return str(key)

    def qr_create(self, key: str, qrimg: bool = True) -> dict[str, str]:
        """用 unikey 生成二维码，返回 {'qrurl':..., 'qrimg': base64}。"""
        data = self._get("/login/qr/create", {"key": key, "qrimg": "true" if qrimg else "false"})
        payload = data.get("data") or {}
        if not isinstance(payload, dict):
            raise NeteaseError("二维码生成失败")
        return {"qrurl": str(payload.get("qrurl", "")), "qrimg": str(payload.get("qrimg", ""))}

    def qr_check(self, key: str) -> dict[str, Any]:
        """查询扫码状态。

        code: 800 过期 / 801 等待扫码 / 802 已扫码待确认 / 803 成功
        """
        data = self._get("/login/qr/check", {"key": key})
        return data

    @staticmethod
    def cookie_from_qr_response(data: dict[str, Any]) -> str:
        """从 /login/qr/check 的响应里提取 Set-Cookie 形式的登录串。"""
        raw = data.get("cookie")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()

        # 有些版本把 cookie 放在 body.cookie / data.cookie
        for container in (data.get("body"), data.get("data")):
            if isinstance(container, dict):
                inner = container.get("cookie")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()

        # 兜底：用 set-cookie 数组自己拼
        set_cookie = data.get("set-cookie") or data.get("setCookie")
        if isinstance(set_cookie, list):
            parts = []
            for item in set_cookie:
                if isinstance(item, str) and "=" in item:
                    parts.append(item.split(";", 1)[0].strip())
            if parts:
                return "; ".join(parts)
        return ""


def try_levels(client: NeteaseClient, song_id: int, levels: Iterable[str]) -> PlayUrl:
    """依次尝试多个音质等级，返回第一个成功的结果。

    用于 VIP 到期或某首歌没有无损时的优雅降级。
    """
    original = client.level
    last = PlayUrl(url=None, reason="没有可用音质")
    try:
        for level in levels:
            client.level = level
            try:
                result = client.song_url(song_id)
            except NeteaseError as exc:
                last = PlayUrl(url=None, reason=str(exc))
                continue
            if result.ok:
                return result
            last = result
        return last
    finally:
        client.level = original
