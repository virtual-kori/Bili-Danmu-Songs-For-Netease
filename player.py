"""音频播放后端。

支持的播放器（自动探测，优先级从高到低）：
    mpv       推荐。支持暂停/继续/音量/无缝切歌（通过命名管道 IPC 控制）
    ffplay    ffmpeg 自带，支持音量，暂停能力有限
    pygame    纯 Python 方案，先下载到本地缓存再播放，支持暂停/音量
    wmp       Windows 自带（WPF MediaPlayer），零安装，无播放控制
    null      不出声的模拟后端，用来测试整条点歌链路

所有后端都实现同一套接口，play() 是阻塞调用：
    正常播放完毕返回 True；被 stop() 打断返回 False。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

# 别让 pygame 每次导入都打印欢迎语
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

LogFunc = Callable[[str], None]


def _default_log(message: str) -> None:
    print(f"[player] {message}", flush=True)


# ------------------------------------------------------- 子进程"父死子死"兜底


def _build_kill_on_close_job():
    """创建一个 Windows 作业对象，句柄关闭时自动杀掉里面的所有进程。

    为什么要这个：播放器（mpv 等）是我们 spawn 出来的子进程。正常情况下退出时
    我们会主动 terminate 它们，但如果机器人被【强杀】——用户点了控制台窗口的 ×、
    任务管理器结束进程、或者程序崩溃 —— 子进程就变成孤儿，**音乐会一直放下去**。
    普通 Python 代码拦不住 TerminateProcess，只有作业对象能兜住。

    返回作业对象句柄；非 Windows 或创建失败返回 None（此时行为退回原来的样子）。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # 0x2000 = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.BasicLimitInformation.LimitFlags = 0x2000
        # 9 = JobObjectExtendedLimitInformation
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None

        return (kernel32, job)
    except Exception:  # noqa: BLE001 - 拿不到就算了，退回原行为
        return None


_JOB = _build_kill_on_close_job()


def assign_to_our_job(proc: subprocess.Popen) -> bool:
    """把子进程放进"父死子死"的作业对象。失败也不影响播放。"""
    if _JOB is None:
        return False
    try:
        kernel32, job = _JOB
        handle = getattr(proc, "_handle", None)
        if handle is None:
            return False
        return bool(kernel32.AssignProcessToJobObject(job, int(handle)))
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------- 后端实现


class BaseBackend:
    """播放后端基类。"""

    name = "base"
    supports_pause = False
    supports_volume = False
    needs_local_file = False

    def __init__(self, volume: int = 70, cache_dir: Path | None = None, log: LogFunc = _default_log):
        self.volume = max(0, min(100, int(volume)))
        self.cache_dir = cache_dir or Path("cache")
        self.log = log
        self._process: subprocess.Popen | None = None
        self._paused = False
        self._lock = threading.Lock()

    # ---- 生命周期

    @classmethod
    def available(cls) -> bool:
        """当前机器上这个后端能不能用。"""
        raise NotImplementedError

    def play(self, source: str, stop_event: threading.Event, cached_path: Path | None = None) -> bool:
        """阻塞播放 source（URL 或本地路径），返回是否正常播完。

        cached_path 是上游预下载好的本地文件，后端可以选择直接用（目前 pygame 用）。
        """
        raise NotImplementedError

    def stop(self) -> None:
        """立刻停止当前播放。"""
        with self._lock:
            proc = self._process
        if proc and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()

    def pause(self) -> bool:
        return False

    def resume(self) -> bool:
        return False

    def set_volume(self, volume: int) -> bool:
        self.volume = max(0, min(100, int(volume)))
        return False

    # ---- 工具

    def _spawn(self, args: list[str], **kwargs) -> subprocess.Popen:
        """启动子进程。

        默认把 stdout/stderr 丢掉，避免和父进程的管道纠缠；
        需要交互（例如 ffplay 的键盘控制）时由调用方传入 stdin=PIPE。

        进程创建后会被放进一个"父死子死"的 Windows 作业对象（见下方
        kill_children_with_us）。这一步很关键：否则机器人被强杀（关窗口、
        任务管理器、崩溃）时，mpv 会变成孤儿进程，音乐一直放下去。
        """
        creationflags = 0
        if os.name == "nt":
            # 不要弹出控制台窗口
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs.setdefault("stdout", subprocess.DEVNULL)
        kwargs.setdefault("stderr", subprocess.DEVNULL)
        proc = subprocess.Popen(args, creationflags=creationflags, **kwargs)
        assign_to_our_job(proc)
        with self._lock:
            self._process = proc
        return proc

    def _wait_process(self, proc: subprocess.Popen, stop_event: threading.Event) -> bool:
        """等待子进程结束。被 stop_event 打断时返回 False。"""
        while True:
            if stop_event.is_set():
                self.stop()
                return False
            try:
                proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                continue
        self._paused = False
        return proc.returncode in (0, None)

    def download(self, url: str, suffix: str = ".mp3", stop_event: threading.Event | None = None) -> Path | None:
        """把音频下载到本地缓存，返回文件路径。

        传入 stop_event 时，切歌可以中断下载（否则用户得等整首下载完才切得动）。
        """
        import requests

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        name = uuid.uuid4().hex[:12] + suffix
        target = self.cache_dir / name
        try:
            with requests.get(url, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                with open(target, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=128 * 1024):
                        if stop_event is not None and stop_event.is_set():
                            handle.close()
                            target.unlink(missing_ok=True)
                            return None
                        if chunk:
                            handle.write(chunk)
        except Exception as exc:  # noqa: BLE001 - 网络问题种类很多，统一降级
            self.log(f"下载失败：{exc}")
            target.unlink(missing_ok=True)
            return None
        return target


class MpvBackend(BaseBackend):
    """mpv 后端，通过 Windows 命名管道 IPC 做播放控制。"""

    name = "mpv"
    supports_pause = True
    supports_volume = True

    def __init__(self, exe: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exe = exe
        self._pipe_path = rf"\\.\pipe\qdgj-mpv-{uuid.uuid4().hex[:8]}"
        self._pipe = None

    @classmethod
    def find_exe(cls) -> str | None:
        """按优先级找一个可用的 mpv：工作区自带 -> PATH。"""
        local = Path(__file__).resolve().parent / "tools" / "mpv" / "mpv.exe"
        if local.is_file():
            return str(local)
        found = shutil.which("mpv")
        if found:
            return found
        for candidate in (
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "mpv" / "mpv.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "mpv.exe",
        ):
            if candidate.is_file():
                return str(candidate)
        return None

    @classmethod
    def available(cls) -> bool:
        return cls.find_exe() is not None

    # ---- IPC

    def _open_pipe(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                # 这个管道要一直开着收发 mpv 指令，不能交给 with 管理
                self._pipe = open(self._pipe_path, "r+b", buffering=0)  # noqa: SIM115
                return True
            except OSError:
                time.sleep(0.1)
        return False

    def _command(self, *command: object) -> bool:
        if self._pipe is None:
            return False
        try:
            payload = json.dumps({"command": list(command)}) + "\n"
            self._pipe.write(payload.encode("utf-8"))
            return True
        except (OSError, ValueError):
            return False

    def _close_pipe(self) -> None:
        if self._pipe is not None:
            with contextlib.suppress(OSError):
                self._pipe.close()
            self._pipe = None

    # ---- 播放

    def play(self, source: str, stop_event: threading.Event, cached_path: Path | None = None) -> bool:
        args = [
            self.exe,
            "--no-video",
            "--no-resume-playback",
            "--really-quiet",
            "--audio-display=no",
            "--force-window=no",
            "--cache=yes",
            f"--volume={self.volume}",
            f"--input-ipc-server={self._pipe_path}",
            source,
        ]
        try:
            proc = self._spawn(args)
        except OSError as exc:
            self.log(f"启动 mpv 失败：{exc}")
            return False

        if not self._open_pipe():
            self.log("mpv IPC 管道打不开，暂停/音量控制将不可用（切歌仍可用）")

        try:
            return self._wait_process(proc, stop_event)
        finally:
            self._close_pipe()

    def pause(self) -> bool:
        if self._command("set_property", "pause", True):
            self._paused = True
            return True
        return False

    def resume(self) -> bool:
        if self._command("set_property", "pause", False):
            self._paused = False
            return True
        return False

    def set_volume(self, volume: int) -> bool:
        super().set_volume(volume)
        return self._command("set_property", "volume", self.volume)


class FFplayBackend(BaseBackend):
    """ffplay 后端。"""

    name = "ffplay"
    supports_pause = True
    supports_volume = True

    def __init__(self, exe: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exe = exe

    @classmethod
    def find_exe(cls) -> str | None:
        found = shutil.which("ffplay")
        if found:
            return found
        for root in (
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")),
            Path("C:/ffmpeg"),
            Path("C:/ffmpeg/bin"),
        ):
            candidate = root / "ffplay.exe"
            if candidate.is_file():
                return str(candidate)
        return None

    @classmethod
    def available(cls) -> bool:
        return cls.find_exe() is not None

    def play(self, source: str, stop_event: threading.Event, cached_path: Path | None = None) -> bool:
        args = [
            self.exe,
            "-nodisp",
            "-autoexit",
            "-loglevel", "quiet",
            "-volume", str(self.volume),
            source,
        ]
        try:
            # ffplay 靠 stdin 上的按键做控制（p 暂停、q 退出）
            proc = self._spawn(args, stdin=subprocess.PIPE)
        except OSError as exc:
            self.log(f"启动 ffplay 失败：{exc}")
            return False
        return self._wait_process(proc, stop_event)

    def _send_key(self, key: bytes) -> bool:
        with self._lock:
            proc = self._process
        if proc and proc.stdin and proc.poll() is None:
            try:
                proc.stdin.write(key)
                proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False
        return False

    def pause(self) -> bool:
        if self._send_key(b"p"):
            self._paused = True
            return True
        return False

    def resume(self) -> bool:
        if self._send_key(b"p"):
            self._paused = False
            return True
        return False

    def set_volume(self, volume: int) -> bool:
        """ffplay 运行时改不了音量，只能记住下次生效。"""
        super().set_volume(volume)
        return True


class PygameBackend(BaseBackend):
    """pygame.mixer 后端：先下载整首再播，控制能力最全但起播有延迟。"""

    name = "pygame"
    supports_pause = True
    supports_volume = True
    needs_local_file = True

    _mixer = None

    @classmethod
    def available(cls) -> bool:
        try:
            import pygame  # noqa: F401
        except ImportError:
            return False
        return True

    @classmethod
    def _ensure_mixer(cls, log: LogFunc):
        if cls._mixer is not None:
            return cls._mixer
        import pygame

        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
        except Exception as exc:  # noqa: BLE001 - 没有声卡时会抛
            log(f"pygame 混音器初始化失败：{exc}")
            return None
        cls._mixer = pygame.mixer
        return cls._mixer

    def play(self, source: str, stop_event: threading.Event, cached_path: Path | None = None) -> bool:
        mixer = self._ensure_mixer(self.log)
        if mixer is None:
            return False

        is_remote = urlparse(source).scheme in ("http", "https")
        local_path: Path | None
        keep_file = False  # 预下载来的缓存文件播完不删，本地路径也不删

        if cached_path is not None and Path(cached_path).is_file():
            local_path = Path(cached_path)
            keep_file = True
            self.log("使用预下载好的音频，立即播放")
        elif is_remote:
            self.log("正在下载音频到本地缓存…")
            local_path = self.download(source, stop_event=stop_event)
            if local_path is None:
                # 下载被切歌打断和下载失败要区分开
                if stop_event.is_set():
                    self.log("下载被切歌打断")
                else:
                    self.log("下载失败，跳过这首")
                return False
        else:
            local_path = Path(source)
            keep_file = True

        try:
            mixer.music.load(str(local_path))
            mixer.music.set_volume(self.volume / 100.0)
            mixer.music.play()
        except Exception as exc:  # noqa: BLE001
            self.log(f"pygame 播放失败：{exc}")
            if not keep_file:
                local_path.unlink(missing_ok=True)
            return False

        try:
            while mixer.music.get_busy():
                if stop_event.is_set():
                    mixer.music.stop()
                    return False
                time.sleep(0.2)
            # 关键：skip() 会先把 mixer 停掉，导致 get_busy() 直接变 False，
            # 所以必须再确认一次 stop_event，否则切歌会被误判成"正常播完"。
            return not stop_event.is_set()
        finally:
            with contextlib.suppress(Exception):  # 老版本 pygame 没有 unload
                mixer.music.unload()
            if not keep_file:
                local_path.unlink(missing_ok=True)

    def stop(self) -> None:
        mixer = self._mixer
        if mixer is not None:
            with contextlib.suppress(Exception):
                mixer.music.stop()
        super().stop()

    def pause(self) -> bool:
        if self._mixer is None:
            return False
        self._mixer.music.pause()
        self._paused = True
        return True

    def resume(self) -> bool:
        if self._mixer is None:
            return False
        self._mixer.music.unpause()
        self._paused = False
        return True

    def set_volume(self, volume: int) -> bool:
        super().set_volume(volume)
        if self._mixer is not None:
            self._mixer.music.set_volume(self.volume / 100.0)
        return True


class WmpBackend(BaseBackend):
    """Windows 自带播放能力（PowerShell + WPF MediaPlayer），零依赖。"""

    name = "wmp"
    supports_pause = False
    supports_volume = False

    _SCRIPT = r"""
param([string]$Url, [double]$Volume)
Add-Type -AssemblyName PresentationCore
$player = New-Object System.Windows.Media.MediaPlayer
$player.Open([uri]$Url)
$player.Volume = $Volume
$player.Play()
$deadline = (Get-Date).AddSeconds(30)
while (-not $player.NaturalDuration.HasTimeSpan) {
    if ((Get-Date) -gt $deadline) { break }
    Start-Sleep -Milliseconds 200
}
if ($player.NaturalDuration.HasTimeSpan) {
    $seconds = [math]::Ceiling($player.NaturalDuration.TimeSpan.TotalSeconds) + 1
} else {
    $seconds = 600
}
Start-Sleep -Seconds $seconds
$player.Stop()
$player.Close()
"""

    @classmethod
    def available(cls) -> bool:
        if os.name != "nt":
            return False
        return shutil.which("powershell") is not None or shutil.which("pwsh") is not None

    def play(self, source: str, stop_event: threading.Event, cached_path: Path | None = None) -> bool:
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            return False
        args = [
            exe,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command", self._SCRIPT,
            "-Url", source,
            "-Volume", f"{self.volume / 100.0:.2f}",
        ]
        try:
            proc = self._spawn(args)
        except OSError as exc:
            self.log(f"启动 WMP 后端失败：{exc}")
            return False
        self.log("WMP 后端不支持暂停/音量/精确切歌，建议改用 mpv")
        return self._wait_process(proc, stop_event)


class NullBackend(BaseBackend):
    """不出声的模拟后端，用于测试整条链路（不装任何播放器也能跑）。"""

    name = "null"
    supports_pause = False
    supports_volume = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._duration_hint = 0.0

    @classmethod
    def available(cls) -> bool:
        return True

    def play(
        self,
        source: str,
        stop_event: threading.Event,
        cached_path: Path | None = None,
        duration_hint: float = 0.0,
    ) -> bool:
        seconds = duration_hint or 10.0
        self.log(f"[模拟播放] 时长 {seconds:.0f} 秒（不会真的出声）")
        deadline = time.time() + seconds
        while time.time() < deadline:
            if stop_event.is_set():
                return False
            time.sleep(0.2)
        return True


BACKEND_ORDER = ("mpv", "ffplay", "pygame", "wmp", "null")


class AudioPrefetcher:
    """后台预下载"下一首"。

    pygame 这类需要完整文件的播放器，如果等轮到才下载，每首歌开头都要空等几秒。
    这里在上一首还在播的时候就把它下好，轮到直接播。
    """

    def __init__(self, cache_dir: str | Path = "cache", log: LogFunc = _default_log) -> None:
        self.cache_dir = Path(cache_dir) / "prefetch"
        self.log = log
        self._lock = threading.Lock()
        self._ready: dict[str, Path] = {}
        self._inflight: set[str] = set()

    def submit(self, key: int | str, url: str) -> bool:
        """提交一个预下载任务；已在缓存或正在下载则跳过。"""
        if not url:
            return False
        token = str(key)
        with self._lock:
            if token in self._inflight or token in self._ready:
                return False
            self._inflight.add(token)
        threading.Thread(target=self._worker, args=(token, url), name=f"prefetch-{token}", daemon=True).start()
        return True

    def _worker(self, token: str, url: str) -> None:
        import requests

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.log(f"预下载目录创建失败：{exc}")
            with self._lock:
                self._inflight.discard(token)
            return

        target = self.cache_dir / f"{token}.mp3"
        partial = self.cache_dir / f"{token}.part"
        try:
            with requests.get(url, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                with open(partial, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=128 * 1024):
                        if chunk:
                            handle.write(chunk)
            partial.replace(target)
        except Exception as exc:  # noqa: BLE001 - 预下载失败无所谓，播放时会重新下
            self.log(f"预下载失败（不影响播放）：{exc}")
            partial.unlink(missing_ok=True)
            with self._lock:
                self._inflight.discard(token)
            return

        with self._lock:
            self._ready[token] = target
            self._inflight.discard(token)
        self.log(f"已预下载：{target.name}")

    def take(self, key: int | str) -> Path | None:
        """取出预下载好的文件；没有就返回 None（调用方自行下载）。"""
        token = str(key)
        with self._lock:
            path = self._ready.pop(token, None)
        if path is not None and path.is_file():
            return path
        return None

    def clear(self) -> int:
        """清掉所有预下载缓存，返回删除的文件数。"""
        with self._lock:
            paths = list(self._ready.values())
            self._ready.clear()
        count = 0
        for path in paths:
            try:
                path.unlink(missing_ok=True)
                count += 1
            except OSError:
                pass
        return count


# ------------------------------------------------------------------- 对外封装


class AudioPlayer:
    """对上层暴露的统一播放器。"""

    def __init__(
        self,
        backend: str = "auto",
        volume: int = 70,
        cache_dir: str | Path = "cache",
        log: LogFunc = _default_log,
    ) -> None:
        self.log = log
        self.volume = max(0, min(100, int(volume)))
        self.cache_dir = Path(cache_dir)
        self.backend = self._build(backend)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # ---- 后端选择

    def _build(self, backend: str) -> BaseBackend:
        backend = (backend or "auto").lower()

        def make(name: str) -> BaseBackend | None:
            if name == "mpv":
                exe = MpvBackend.find_exe()
                return MpvBackend(exe, volume=self.volume, cache_dir=self.cache_dir, log=self.log) if exe else None
            if name == "ffplay":
                exe = FFplayBackend.find_exe()
                return FFplayBackend(exe, volume=self.volume, cache_dir=self.cache_dir, log=self.log) if exe else None
            if name == "pygame":
                return PygameBackend(volume=self.volume, cache_dir=self.cache_dir, log=self.log)
            if name == "wmp":
                return WmpBackend(volume=self.volume, cache_dir=self.cache_dir, log=self.log)
            if name == "null":
                return NullBackend(volume=self.volume, cache_dir=self.cache_dir, log=self.log)
            return None

        if backend != "auto":
            chosen = make(backend)
            if chosen is not None:
                self.log(f"使用播放后端：{chosen.name}")
                return chosen
            self.log(f"指定的后端 '{backend}' 不可用，改为自动探测")

        for name in BACKEND_ORDER:
            candidate = make(name)
            if candidate is not None:
                if name == "null":
                    self.log("没有找到任何可用的播放器，暂时使用模拟后端（不会出声）")
                    self.log("想要真正出声：把 mpv.exe 放到 tools/mpv/ 目录，或安装 ffmpeg / 运行 pip install pygame")
                else:
                    self.log(f"使用播放后端：{candidate.name}")
                return candidate

        raise RuntimeError("连模拟后端都初始化失败了，这不该发生")

    @property
    def backend_name(self) -> str:
        return self.backend.name

    @property
    def supports_pause(self) -> bool:
        return self.backend.supports_pause

    # ---- 播放控制

    def play(self, source: str, duration_hint: float = 0.0, cached_path: Path | None = None) -> bool:
        """阻塞播放。返回 True 表示正常播完，False 表示被打断或失败。"""
        with self._lock:
            self._stop_event = threading.Event()
            stop_event = self._stop_event

        try:
            if isinstance(self.backend, NullBackend):
                return self.backend.play(source, stop_event, cached_path, duration_hint)
            return self.backend.play(source, stop_event, cached_path)
        except Exception as exc:  # noqa: BLE001 - 播放失败不应该拖垮整个程序
            self.log(f"播放出错：{exc}")
            return False

    def skip(self) -> None:
        """切歌：打断当前播放。"""
        with self._lock:
            self._stop_event.set()
        self.backend.stop()

    def pause(self) -> bool:
        return self.backend.pause()

    def resume(self) -> bool:
        return self.backend.resume()

    def set_volume(self, volume: int) -> bool:
        self.volume = max(0, min(100, int(volume)))
        return self.backend.set_volume(self.volume)

    def volume_up(self, step: int = 10) -> int:
        self.set_volume(self.volume + step)
        return self.volume

    def volume_down(self, step: int = 10) -> int:
        self.set_volume(self.volume - step)
        return self.volume

    @staticmethod
    def describe_backends() -> dict[str, bool]:
        """探测本机各后端的可用性，供环境自检使用。"""
        return {
            "mpv": MpvBackend.available(),
            "ffplay": FFplayBackend.available(),
            "pygame": PygameBackend.available(),
            "wmp": WmpBackend.available(),
            "null": True,
        }


if __name__ == "__main__":
    # 直接运行本文件可以做一次后端自检
    print("播放后端可用性：")
    for key, ok in AudioPlayer.describe_backends().items():
        print(f"  {'✓' if ok else '✗'} {key}")
    player = AudioPlayer()
    print(f"\n最终选用：{player.backend_name}")
    print(f"支持暂停：{player.supports_pause}")
    if len(sys.argv) > 1:
        print(f"试播：{sys.argv[1]}")
        player.play(sys.argv[1], duration_hint=8)
