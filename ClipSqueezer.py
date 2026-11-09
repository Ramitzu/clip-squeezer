#!/usr/bin/env python3
"""
Clip Squeezer
=============
Drag video clips in, pick a size, get files that actually fit.

Two-pass H.264 encoding with a calculated bitrate budget means the output
lands just under the target size instead of hoping for the best.

    python ClipSqueezer.py                 # normal windowed app
    python ClipSqueezer.py --cli a.mp4     # headless, for testing
    python ClipSqueezer.py --install-ffmpeg
"""

from __future__ import annotations

import json
import math
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "Clip Squeezer"
VERSION = "1.0.1"
USER_AGENT = f"ClipSqueezer/{VERSION}"

# Upload limits are quoted in decimal megabytes (10 MB = 10,000,000 bytes).
# Using 1024*1024 here let a "10 MB" output land ~13,000 bytes over Discord's
# limit, which is exactly the failure this app exists to prevent.
MB = 1_000_000
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEVNULL = "NUL" if os.name == "nt" else "/dev/null"

VIDEO_EXTS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".flv",
    ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp", ".ogv", ".divx",
}

# A stable release line first: nightly builds can change behaviour without
# warning. The nightly is only a fallback for when that line gets retired.
_FFMPEG_BASE = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
FFMPEG_URLS = (
    _FFMPEG_BASE + "ffmpeg-n9.0-latest-win64-gpl-9.0.zip",
    _FFMPEG_BASE + "ffmpeg-master-latest-win64-gpl.zip",
)

# Discord's free limit rose from 10 MB to 20 MB in August 2026.
TARGET_CHOICES = [
    (20, "20 MB  Free"),
    (25, "25 MB"),
    (50, "50 MB  Nitro Basic"),
    (100, "100 MB"),
    (500, "500 MB"),
    (1000, "1 GB  Nitro"),
]

# Common heights to step down through when the clip's own height won't fit.
HEIGHT_LADDER = (2160, 1440, 1080, 720, 540, 480, 360, 288, 240, 180)


# --------------------------------------------------------------------------
# palette / metrics
# --------------------------------------------------------------------------

BG = "#101218"
SURFACE = "#171A22"
SURFACE_2 = "#1F2330"
SURFACE_3 = "#252A38"
BORDER = "#2A3040"
TEXT = "#E8EBF2"
TEXT_DIM = "#98A1B3"
TEXT_FAINT = "#69718A"
ACCENT = "#5865F2"
ACCENT_HI = "#6E7AF5"
ACCENT_LO = "#4550C8"
ACCENT_SOFT = "#232849"
ACCENT_TEXT = "#C3C9FF"
GREEN = "#3BA55D"
YELLOW = "#E0A626"
RED = "#ED4245"
RED_SOFT = "#B93A3D"

_SCALE = 1.0


def S(value: float) -> int:
    """Scale a design pixel to the current display."""
    return max(1, int(round(value * _SCALE)))


def F(size: float, weight: str = "normal") -> tuple:
    """Font in device pixels (negative size), so DPI scaling is explicit."""
    return ("Segoe UI", -max(8, S(size)), weight)


def human_size(num_bytes: float) -> str:
    """Decimal units, matching how Explorer and Discord report file sizes."""
    if not num_bytes or num_bytes < 0:
        return "--"
    if num_bytes < 1000:
        return f"{int(num_bytes)} B"
    if num_bytes < MB:
        return f"{num_bytes / 1000:.0f} KB"
    if num_bytes < 1000 * MB:
        return f"{num_bytes / MB:.1f} MB"
    return f"{num_bytes / (1000 * MB):.2f} GB"


def human_time(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"0:{seconds:02d}"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


# --------------------------------------------------------------------------
# ffmpeg discovery / bootstrap
# --------------------------------------------------------------------------

_ffmpeg: Path | None = None
_ffprobe: Path | None = None


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _search_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("CLIP_SQUEEZER_FFMPEG_DIR")
    if env:
        dirs.append(Path(env))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs += [Path(meipass) / "bin", Path(meipass)]
    here = app_dir()
    dirs += [here / "bin", here]
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ClipSqueezer" / "bin"
    dirs.append(local)
    return dirs


def _find_tool(name: str) -> Path | None:
    exe = name + (".exe" if os.name == "nt" else "")
    for folder in _search_dirs():
        candidate = folder / exe
        if candidate.is_file():
            return candidate
    found = shutil.which(name)
    return Path(found) if found else None


def refresh_tools() -> tuple[Path | None, Path | None]:
    global _ffmpeg, _ffprobe
    _ffmpeg = _find_tool("ffmpeg")
    _ffprobe = _find_tool("ffprobe")
    return _ffmpeg, _ffprobe


def ffmpeg_bin() -> Path:
    if _ffmpeg and Path(_ffmpeg).is_file():
        return Path(_ffmpeg)
    refresh_tools()
    if _ffmpeg:
        return Path(_ffmpeg)
    raise CompressError("ffmpeg is not installed yet.")


def ffprobe_bin() -> Path:
    if _ffprobe and Path(_ffprobe).is_file():
        return Path(_ffprobe)
    refresh_tools()
    if _ffprobe:
        return Path(_ffprobe)
    raise CompressError("ffprobe is not installed yet.")


def set_tools(ffmpeg: Path, ffprobe: Path) -> None:
    global _ffmpeg, _ffprobe
    _ffmpeg, _ffprobe = Path(ffmpeg), Path(ffprobe)


def install_dir() -> Path:
    """Where a downloaded ffmpeg should live: next to the app if writable."""
    preferred = app_dir() / "bin"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
        return preferred
    except OSError:
        fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ClipSqueezer" / "bin"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _clear_stale_downloads() -> None:
    """A cancelled download shouldn't leave a 200 MB zip behind."""
    for folder in (app_dir() / "bin", Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ClipSqueezer" / "bin"):
        try:
            for stale in folder.glob("_download-*.zip"):
                stale.unlink(missing_ok=True)
            for stale in folder.glob("*.exe.part"):
                stale.unlink(missing_ok=True)
        except OSError:
            pass


def _open_ffmpeg_download():
    """Open the first build that still exists; a 404 means it was retired."""
    for index, url in enumerate(FFMPEG_URLS):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            return urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code != 404 or index == len(FFMPEG_URLS) - 1:
                raise


def download_ffmpeg(dest: Path, progress=None) -> tuple[Path, Path]:
    """Fetch a static Windows ffmpeg build and extract the two binaries."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / f"_download-{uuid.uuid4().hex[:8]}.zip"
    try:
        with _open_ffmpeg_download() as response, open(archive, "wb") as fh:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            while True:
                chunk = response.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                received += len(chunk)
                if progress and total:
                    progress("download", min(1.0, received / total))
        if progress:
            progress("extract", 0.0)
        extracted: dict[str, Path] = {}
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = Path(info.filename)
                if os.name == "nt":
                    if name.suffix.lower() != ".exe":
                        continue
                elif name.suffix:
                    continue
                stem = name.stem.lower()
                if stem in ("ffmpeg", "ffprobe"):
                    # Write beside the target, then swap in one rename: a
                    # half-downloaded exe must never look installed.
                    target = dest / name.name
                    temp = dest / (name.name + ".part")
                    with zf.open(info) as src, open(temp, "wb") as out:
                        shutil.copyfileobj(src, out)
                    temp.replace(target)
                    extracted[stem] = target
        if "ffmpeg" not in extracted or "ffprobe" not in extracted:
            raise CompressError("The downloaded archive did not contain ffmpeg.")
        return extracted["ffmpeg"], extracted["ffprobe"]
    finally:
        try:
            archive.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------

class CompressError(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 30.0
    vcodec: str = ""
    acodec: str = ""
    channels: int = 2
    bitrate_kbps: float = 0.0
    size: int = 0
    vstream_index: int = 0
    astream_index: int | None = None


def _ratio(text: str, default: float = 0.0) -> float:
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            return float(num) / float(den) if float(den) else default
        return float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def probe_media(path: Path, cancel=None) -> MediaInfo:
    cmd = [
        str(ffprobe_bin()), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    result = {"stdout": "", "stderr": "", "exc": None}

    def _reader() -> None:
        try:
            out, err = proc.communicate()
            result["stdout"], result["stderr"] = out, err
        except Exception as exc:  # pragma: no cover - defensive
            result["exc"] = exc

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()
    start = time.time()
    try:
        while reader_thread.is_alive():
            if cancel is not None and cancel.is_set():
                proc.kill()
                reader_thread.join(timeout=5)
                raise Cancelled()
            if time.time() - start > 30:
                proc.kill()
                reader_thread.join(timeout=5)
                raise CompressError("Reading this clip took too long.")
            time.sleep(0.05)
    except BaseException:
        proc.kill()
        reader_thread.join(timeout=5)
        raise

    if result["exc"] is not None:
        raise CompressError("Could not read this file.") from result["exc"]

    stdout, stderr = result["stdout"], result["stderr"]
    returncode = proc.returncode

    if returncode != 0:
        detail = (stderr or "").strip().splitlines()
        message = detail[-1] if detail else "Could not read this file."
        if ": " in message:
            message = message.split(": ", 1)[1]
        raise CompressError(friendly_error(message))

    try:
        data = json.loads(stdout or "{}")
    except ValueError as exc:
        raise CompressError("Could not read this file.") from exc
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and s.get("disposition", {}).get("attached_pic", 0) != 1), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise CompressError("That file has no video track.")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)

    # Rotated phone clips: use the upright dimensions.
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rotation = int(float(side["rotation"]))
            except (TypeError, ValueError):
                pass
    if not rotation:
        try:
            rotation = int(float((video.get("tags") or {}).get("rotate", 0)))
        except (TypeError, ValueError):
            rotation = 0
    if abs(rotation) % 180 == 90:
        width, height = height, width

    duration = _ratio(str((data.get("format") or {}).get("duration", "0")))
    if not duration:
        duration = _ratio(str(video.get("duration", "0")))

    fps = _ratio(str(video.get("avg_frame_rate", "0")))
    if not fps or fps > 240:
        fps = _ratio(str(video.get("r_frame_rate", "30")), 30.0)
    if not fps or not math.isfinite(fps):
        fps = 30.0

    bitrate = _ratio(str((data.get("format") or {}).get("bit_rate", "0"))) / 1000.0
    if not bitrate:
        bitrate = _ratio(str(video.get("bit_rate", "0"))) / 1000.0

    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    try:
        v_index = int(video.get("index", 0))
    except (TypeError, ValueError):
        v_index = 0
    a_index = None
    if audio is not None:
        try:
            a_index = int(audio.get("index"))
        except (TypeError, ValueError):
            a_index = None

    return MediaInfo(
        duration=max(duration, 0.0),
        width=width,
        height=height,
        fps=min(fps, 120.0),
        vcodec=str(video.get("codec_name") or ""),
        acodec=str((audio or {}).get("codec_name") or ""),
        channels=int((audio or {}).get("channels") or 2),
        bitrate_kbps=bitrate,
        size=size,
        vstream_index=v_index,
        astream_index=a_index,
    )


# --------------------------------------------------------------------------
# planning + encoding
# --------------------------------------------------------------------------

SPEED_PRESETS = {"fast": "veryfast", "balanced": "medium", "best": "slow"}


def _audio_kbps(total_kbps: float, channels: int) -> int:
    if total_kbps >= 1600:
        rate = 160
    elif total_kbps >= 1000:
        rate = 128
    elif total_kbps >= 480:
        rate = 96
    elif total_kbps >= 260:
        rate = 64
    else:
        rate = 48
    return min(rate, 64) if channels == 1 else rate


@dataclass
class EncodePlan:
    video_kbps: int
    audio_kbps: int
    height: int
    fps: float
    preset: str
    filters: str
    channels: int = 2


def plan_encode(info: MediaInfo, target_mb: float, speed: str = "balanced",
                factor: float = 1.0) -> EncodePlan:
    target_bytes = target_mb * MB
    duration = max(info.duration, 0.25)

    # 96% of the budget for the stream itself: container overhead + slack so
    # Discord never says "too big" by a few kilobytes.
    total_kbps = (target_bytes * 8.0 / duration / 1000.0) * 0.96 * factor

    audio_k = _audio_kbps(total_kbps, info.channels) if info.acodec else 0
    video_k = max(60.0, total_kbps - audio_k)

    # Never inflate a file that is already efficient.
    if info.bitrate_kbps:
        video_k = min(video_k, max(150.0, info.bitrate_kbps * 1.08))

    fps = info.fps or 30.0
    if fps > 60:
        fps = 60.0
    if video_k < 1500 and fps > 30.5:
        fps = 30.0
    if video_k < 260 and fps > 24.5:
        fps = 24.0

    aspect = (info.width / info.height) if info.width and info.height else 16 / 9
    # Cap the short side at 4K, so a 2160x3840 phone clip isn't cut down.
    ceiling = max(2, min(info.height or 1080, round(2160 / min(aspect, 1.0))))
    ceiling -= ceiling % 2
    bpp = 0.070  # bits per pixel per frame that H.264 holds up at
    height = None
    # Try the clip's own height first: 1920-tall phone clips and 900p captures
    # aren't on the ladder and shouldn't shrink when the budget has room.
    for candidate in (ceiling, *(h for h in HEIGHT_LADDER if h < ceiling)):
        w = candidate * aspect
        if w * candidate * fps * bpp / 1000.0 <= video_k:
            height = candidate
            break
    if height is None:
        height = 180 if ceiling >= 180 else max(2, ceiling)
    height -= height % 2

    filters = [f"scale=-2:{int(height)}:flags=lanczos"]
    if info.fps and info.fps > fps + 0.05:
        filters.append(f"fps={fps:g}")

    return EncodePlan(
        video_kbps=max(40, int(video_k)),
        audio_kbps=int(audio_k),
        height=int(height),
        fps=fps,
        preset=SPEED_PRESETS.get(speed, "medium"),
        filters=",".join(filters),
        channels=1 if info.channels == 1 else 2,
    )


def _parse_progress_time(key: str, value: str) -> float | None:
    try:
        if key in ("out_time_us", "out_time_ms"):
            return float(value) / 1_000_000.0
        if key == "out_time":
            hours, minutes, seconds = value.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return None
    return None


def _run_ffmpeg(cmd: list[str], duration: float, cancel: threading.Event,
                holder: dict, on_pct) -> None:
    errors: list[str] = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    holder["proc"] = proc
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            if cancel.is_set():
                break
            line = raw.strip()
            if not line:
                continue
            key, sep, value = line.partition("=")
            if sep and key == "progress" and value == "end":
                on_pct(1.0)
            elif sep and key in ("out_time_us", "out_time_ms", "out_time"):
                seconds = _parse_progress_time(key, value)
                if seconds is not None and duration > 0:
                    on_pct(max(0.0, min(0.999, seconds / duration)))
            elif not sep:
                errors.append(line)
                if len(errors) > 40:
                    errors.pop(0)
        # If we stopped reading because of a cancel, the child may still be
        # writing into the pipe - terminate before waiting so it can't block.
        if cancel.is_set() and proc.poll() is None:
            proc.terminate()
        try:
            code = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        holder.pop("proc", None)

    if cancel.is_set():
        raise Cancelled()
    if code != 0:
        tail = [e for e in errors if e.strip()][-6:]
        raise CompressError(friendly_error("\n".join(tail)))


def friendly_error(text: str) -> str:
    low = text.lower()
    if "no space left" in low:
        return "Not enough disk space."
    if "permission denied" in low or "access is denied" in low:
        return "Can't write there. Pick another save folder."
    if "invalid data found" in low or "moov atom not found" in low:
        return "This file can't be read as a video."
    if "no such file" in low:
        return "The file disappeared while working on it."
    if "unknown encoder" in low:
        return "This ffmpeg build is missing the H.264 encoder."
    if not text.strip():
        return "ffmpeg failed for an unknown reason."
    return text.strip().splitlines()[-1][:220]


def encode_to_size(src: Path, info: MediaInfo, settings: "Settings",
                   out_path: Path, on_pct, cancel: threading.Event,
                   holder: dict | None = None,
                   stage=None) -> tuple[Path, int]:
    target_bytes = settings.target_mb * MB
    holder = holder if holder is not None else {}
    workdir = Path(tempfile.mkdtemp(prefix="clipsqueezer-"))
    factor = 1.0
    try:
        for attempt in range(2):
            plan = plan_encode(info, settings.target_mb, settings.speed, factor)
            passlog = workdir / f"pass{attempt}"
            part = out_path.with_name(f"{out_path.stem}.{uuid.uuid4().hex[:6]}.part.mp4")
            if attempt == 0:
                p1_base, p1_span, p2_base, p2_span = 0.0, 42.0, 42.0, 48.0
            else:
                p1_base, p1_span, p2_base, p2_span = 90.0, 4.5, 94.5, 5.0

            video_args = [
                "-map", f"0:{info.vstream_index}",
                "-vf", plan.filters,
                "-c:v", "libx264",
                "-preset", plan.preset,
                "-b:v", f"{plan.video_kbps}k",
                "-maxrate", f"{int(plan.video_kbps * 1.5)}k",
                "-bufsize", f"{int(plan.video_kbps * 2)}k",
                "-profile:v", "high",
                "-pix_fmt", "yuv420p",
                "-sn",
                "-map_chapters", "-1",
                "-map_metadata", "-1",
                "-max_muxing_queue_size", "1024",
            ]
            head = [
                str(ffmpeg_bin()), "-hide_banner", "-nostdin", "-y",
                "-loglevel", "error", "-progress", "pipe:1",
                "-i", str(src),
            ]

            try:
                if stage:
                    stage(1)
                pass1 = head + video_args + [
                    "-pass:v", "1", "-passlogfile:v", str(passlog),
                    "-an", "-f", "null", DEVNULL,
                ]
                _run_ffmpeg(
                    pass1, info.duration, cancel, holder,
                    lambda p, b=p1_base, s=p1_span: on_pct(b + p * s),
                )

                if cancel.is_set():
                    raise Cancelled()

                audio_args = ["-an"]
                if info.acodec and plan.audio_kbps:
                    audio_map = (f"0:{info.astream_index}"
                                 if info.astream_index is not None
                                 else "0:a:0?")
                    audio_args = [
                        "-map", audio_map,
                        "-c:a", "aac",
                        "-b:a", f"{plan.audio_kbps}k",
                        "-ac", str(plan.channels),
                        "-ar", "48000",
                    ]

                if stage:
                    stage(2)
                pass2 = head + video_args + [
                    "-pass:v", "2", "-passlogfile:v", str(passlog),
                ] + audio_args + [
                    "-movflags", "+faststart",
                    "-metadata", f"comment={APP_NAME}",
                    str(part),
                ]
                _run_ffmpeg(
                    pass2, info.duration, cancel, holder,
                    lambda p, b=p2_base, s=p2_span: on_pct(b + p * s),
                )

                if not part.exists():
                    raise CompressError("Encoding produced no output file.")
                size = part.stat().st_size
            except BaseException:
                # Never leave a half-written .part file next to the clip.
                part.unlink(missing_ok=True)
                raise

            if size <= target_bytes:
                # Never destroy a file that appeared since output_path_for ran.
                target = out_path
                counter = 2
                while target.exists():
                    target = out_path.with_name(f"{out_path.stem} {counter}.mp4")
                    counter += 1
                part.replace(target)
                return target, size

            # Overshot: tighten the budget and try once more, unless the plan
            # is already at its minimum and a retry would make the same file.
            part.unlink(missing_ok=True)
            factor *= (target_bytes * 0.95) / size
            retry = plan_encode(info, settings.target_mb, settings.speed, factor)
            if attempt == 1 or retry == plan:
                raise CompressError(f"Over limit: {size / MB:.2f} MB")

        raise CompressError("Could not reach the target size.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# batch runner
# --------------------------------------------------------------------------

@dataclass
class Job:
    uid: int
    src: Path
    size: int = 0
    info: MediaInfo | None = None
    state: str = "queued"   # queued running done fits error canceled
    out: Path | None = None
    out_size: int = 0
    pct: float = 0.0
    note: str = ""
    probed: bool = False


@dataclass
class Settings:
    target_mb: float = 10.0
    speed: str = "balanced"
    out_dir: Path | None = None


def output_path_for(src: Path, out_dir: Path | None) -> Path:
    folder = out_dir if out_dir else src.parent
    base = src.stem
    while base.lower().endswith(" (discord)"):
        base = base[: -len(" (discord)")]
    stem = f"{base or src.stem} (discord)"
    candidate = folder / f"{stem}.mp4"
    counter = 2
    while candidate.exists():
        candidate = folder / f"{stem} {counter}.mp4"
        counter += 1
    return candidate


class BatchRunner:
    def __init__(self, emit):
        self.emit = emit
        self.cancel_event = threading.Event()
        self._holder: dict = {}
        self.thread: threading.Thread | None = None

    def start(self, jobs: list[Job], settings: Settings) -> None:
        self.cancel_event.clear()
        self.thread = threading.Thread(
            target=self._run, args=(jobs, settings), daemon=True
        )
        self.thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()
        proc = self._holder.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    # -- worker ------------------------------------------------------------
    def _run(self, jobs: list[Job], settings: Settings) -> None:
        total = len(jobs)
        done = 0
        saved = 0
        failures = 0

        for index, job in enumerate(jobs, start=1):
            if self.cancel_event.is_set():
                job.state = "canceled"
                self.emit(("job", job))
                continue

            job.state = "running"
            job.pct = 0.0
            job.note = ""
            self.emit(("job", job))
            self.emit(("status", f"Reading clip {index} of {total}..."))

            try:
                # Always read it again: the file may have changed since it was added.
                info = probe_media(job.src, self.cancel_event)
                job.info = info
                if info.size:
                    job.size = info.size
                if info.duration <= 0.1:
                    raise CompressError("Couldn't read the clip length.")

                if self.cancel_event.is_set():
                    job.state = "canceled"
                    self.emit(("job", job))
                    self.emit(("status", "Canceled."))
                    continue

                if job.size and job.size <= settings.target_mb * MB:
                    job.state = "fits"
                    self.emit(("job", job))
                    self.emit(("status", f"{job.src.name} already fits in "
                                          f"{settings.target_mb:g} MB."))
                    done += 1
                    continue

                out_dir = settings.out_dir
                if out_dir:
                    out_dir.mkdir(parents=True, exist_ok=True)
                out_path = output_path_for(job.src, out_dir)

                plan = plan_encode(info, settings.target_mb, settings.speed)

                def stage(pass_no: int, idx=index, p=plan) -> None:
                    self.emit(("status",
                               f"Encoding {idx} of {total} - pass {pass_no}/2 "
                               f"- {p.height}p @ {p.video_kbps} kbps"))

                def progress(pct: float, idx=index) -> None:
                    job.pct = pct
                    self.emit(("progress", job, pct))

                out, size = encode_to_size(
                    job.src, info, settings, out_path, progress,
                    self.cancel_event, self._holder, stage,
                )
                job.out = out
                job.out_size = size
                job.pct = 100.0
                self.emit(("progress", job, 100.0))
                job.state = "done"
                saved += max(0, job.size - size)
                done += 1
                self.emit(("job", job))
                self.emit(("status", f"Saved {out.name}"))

            except Cancelled:
                job.state = "canceled"
                self.emit(("job", job))
                self.emit(("status", "Canceled."))
            except CompressError as exc:
                job.state = "error"
                job.note = str(exc)
                failures += 1
                self.emit(("job", job))
                self.emit(("status", f"{job.src.name}: {job.note}"))
            except Exception as exc:  # keep the batch alive
                job.state = "error"
                job.note = friendly_error(str(exc))
                failures += 1
                self.emit(("job", job))
                self.emit(("status", f"{job.src.name}: {job.note}"))

            if self.cancel_event.is_set():
                break

        self.emit(("finished", {
            "done": done,
            "failed": failures,
            "saved": saved,
            "total": total,
            "canceled": self.cancel_event.is_set(),
        }))


def run_cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="ClipSqueezer", description="Shrink clips to a size limit.")
    parser.add_argument("files", nargs="*")
    parser.add_argument("--target", type=float, default=20.0, help="target size in MB (default 20)")
    parser.add_argument("--speed", choices=sorted(SPEED_PRESETS), default="balanced")
    parser.add_argument("--out", default=None, help="output folder")
    parser.add_argument("--install-ffmpeg", action="store_true")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.install_ffmpeg:
        tools = refresh_tools()
        if all(tools):
            print(f"ffmpeg already available: {tools[0]}")
            return 0
        dest = install_dir()
        print(f"Downloading ffmpeg into {dest} ...")
        last = [-1]

        def progress(stage: str, frac: float) -> None:
            pct = int(frac * 100)
            if pct != last[0]:
                last[0] = pct
                print(f"\r  {stage}: {pct}%", end="", flush=True)

        ffmpeg, ffprobe = download_ffmpeg(dest, progress)
        print(f"\nInstalled: {ffmpeg}")
        set_tools(ffmpeg, ffprobe)
        return 0

    if not args.files:
        parser.print_help()
        return 2

    if not all(refresh_tools()):
        print("ffmpeg/ffprobe is missing. Run with --install-ffmpeg first.")
        return 2

    if not math.isfinite(args.target) or args.target <= 0:
        print("--target must be a finite number greater than 0.")
        return 2

    out_dir = Path(args.out).expanduser().resolve() if args.out else None
    jobs: list[Job] = []
    missing = 0
    for index, name in enumerate(args.files):
        path = Path(name).expanduser()
        if not path.is_file():
            print(f"skip (not found): {path}")
            missing += 1
            continue
        jobs.append(Job(uid=index, src=path, size=path.stat().st_size))

    if not jobs:
        print("Nothing to do.")
        return 1

    settings = Settings(target_mb=args.target, speed=args.speed, out_dir=out_dir)

    def emit(event) -> None:
        kind = event[0]
        if kind == "job":
            job = event[1]
            print(f"[{job.src.name}] {job.state}"
                  + (f" - {job.note}" if job.note else ""))
        elif kind == "progress":
            job, pct = event[1], event[2]
            print(f"\r  {job.src.name}: {pct:5.1f}%", end="", flush=True)
        elif kind == "status":
            print(f"\r{event[1]:<78}", end="\r", flush=True)
        elif kind == "finished":
            print("\nDone.", event[1])

    runner = BatchRunner(emit)
    runner.start(jobs, settings)
    if runner.thread:
        try:
            runner.thread.join()
        except KeyboardInterrupt:
            runner.cancel()
            runner.thread.join()
    return 0 if (missing == 0 and all(j.state in ("done", "fits") for j in jobs)) else 1


# --------------------------------------------------------------------------
# UI primitives
# --------------------------------------------------------------------------

def round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=28, **kwargs)


class RoundedButton:
    """Canvas button with kind-based colours and proper hover states."""

    KINDS = {
        "primary": (ACCENT, "#FFFFFF", ACCENT_HI, ACCENT_LO, ACCENT),
        "secondary": (SURFACE_2, TEXT, SURFACE_3, "#161922", BORDER),
        "ghost": (BG, TEXT_DIM, SURFACE_2, SURFACE_2, BG),
        "danger": (RED_SOFT, "#FFFFFF", "#D1494C", "#93302F", RED_SOFT),
    }

    def __init__(self, master, text, command=None, kind="primary",
                 width=170, height=42, radius=11, bg=None, font=None):
        import tkinter as tk

        self.command = command
        self._kind = kind
        self._width = S(width)
        self._height = S(height)
        self._radius = S(radius)
        self._bg = bg or BG
        self._enabled = True
        self._hover = False
        self._pressed = False
        self._font = font or F(13, "bold")

        self.canvas = tk.Canvas(
            master, width=self._width, height=self._height, bg=self._bg,
            highlightthickness=0, bd=0, cursor="hand2",
        )
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self._text = text
        self._draw()

    # -- packing passthrough ----------------------------------------------
    def pack(self, **kwargs):
        self.canvas.pack(**kwargs)
        return self

    # -- state -------------------------------------------------------------
    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self._draw()

    def set_kind(self, kind: str) -> None:
        if kind != self._kind:
            self._kind = kind
            self._draw()

    def set_enabled(self, enabled: bool) -> None:
        if enabled != self._enabled:
            self._enabled = enabled
            self.canvas.configure(cursor="hand2" if enabled else "arrow")
            self._draw()

    # -- drawing -----------------------------------------------------------
    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        fill, fg, hover, press, outline = self.KINDS[self._kind]
        if not self._enabled:
            fill = SURFACE_2 if self._kind != "ghost" else self._bg
            fg = TEXT_FAINT
            outline = BORDER if self._kind != "ghost" else self._bg
        elif self._pressed:
            fill = press
            fg = "#D9DEF0"
        elif self._hover:
            fill = hover
        round_rect(
            c, 1, 1, self._width - 1, self._height - 1, self._radius,
            fill=fill, outline=outline, width=1 if outline != fill else 0,
        )
        c.create_text(
            self._width / 2, self._height / 2 + S(1), text=self._text,
            fill=fg, font=self._font,
        )

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self._draw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, _event=None) -> None:
        if not self._enabled:
            return
        self._pressed = True
        self._draw()

    def _on_release(self, _event=None) -> None:
        was = self._pressed
        self._pressed = False
        self._draw()
        if was and self._enabled and self.command:
            self.command()


class ChipGroup:
    """Segmented choice row drawn on a canvas."""

    def __init__(self, master, options, value=None, on_change=None,
                 height=34, bg=BG):
        import tkinter as tk
        import tkinter.font as tkfont

        self.options = [[v, label] for v, label in options]
        self.value = value if value is not None else (self.options[0][0] if self.options else None)
        self.on_change = on_change
        self._h = S(height)
        self._font = F(13)
        self._font_obj = tkfont.Font(font=self._font)
        self._boxes: list[tuple] = []
        self._hover = None
        self._bg = bg
        self._enabled = True

        self.canvas = tk.Canvas(
            master, height=self._h, bg=bg, highlightthickness=0, bd=0, width=10,
        )
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-1>", self._on_click)
        self._draw()

    def grid(self, **kwargs):
        self.canvas.grid(**kwargs)
        return self

    def _chip_width(self, label: str) -> int:
        return self._font_obj.measure(label) + S(30)

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._boxes.clear()
        y1, y2 = 2, self._h - 2
        x = 2
        for value, label in self.options:
            w = self._chip_width(label)
            selected = value == self.value
            hovered = value == self._hover and not selected
            fill = ACCENT_SOFT if selected else (SURFACE_3 if hovered else SURFACE_2)
            outline = ACCENT if selected else (BORDER if not hovered else "#3B4356")
            fg = ACCENT_TEXT if selected else (TEXT if hovered else TEXT_DIM)
            round_rect(c, x, y1, x + w, y2, S(9), fill=fill, outline=outline, width=1)
            c.create_text(x + w / 2, (y1 + y2) / 2 + S(1), text=label, fill=fg, font=self._font)
            self._boxes.append((value, x, y1, x + w, y2))
            x += w + S(8)
        c.configure(width=max(10, x))

    def _hit(self, x: int, y: int):
        for value, x1, y1, x2, y2 in self._boxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return value
        return None

    def _on_motion(self, event) -> None:
        if not self._enabled:
            return
        value = self._hit(event.x, event.y)
        if value != self._hover:
            self._hover = value
            self.canvas.configure(cursor="hand2" if value is not None else "arrow")
            self._draw()

    def _on_leave(self, _event=None) -> None:
        if self._hover is not None:
            self._hover = None
            self._draw()

    def _on_click(self, event) -> None:
        if not self._enabled:
            return
        value = self._hit(event.x, event.y)
        if value is None:
            return
        # "Custom..." must still open its dialog when it is already selected.
        if value == self.value and value != "custom":
            return
        self.select(value)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.canvas.configure(cursor="hand2" if enabled else "arrow")
        if not enabled:
            self._hover = None
        self._draw()

    def select(self, value, notify=True) -> None:
        if not getattr(self, "_enabled", True):
            return
        if value not in [v for v, _ in self.options]:
            return
        self.value = value
        self._draw()
        if notify and self.on_change:
            self.on_change(value)

    def relabel(self, value, label: str) -> None:
        for item in self.options:
            if item[0] == value:
                item[1] = label
        self._draw()


class DropZone:
    CARD_HEIGHT = 192

    def __init__(self, master, command):
        import tkinter as tk
        import tkinter.font as tkfont

        self.command = command
        self._active = False
        self._btn_hover = False
        self._btn_box = (0, 0, 0, 0)
        self._font_title = F(16, "bold")
        self._font_sub = F(13)
        self._font_hint = F(11)
        self._font_btn = F(13, "bold")
        self._f_title = tkfont.Font(font=self._font_title)
        self._f_sub = tkfont.Font(font=self._font_sub)
        self._f_hint = tkfont.Font(font=self._font_hint)
        self.canvas = tk.Canvas(
            master, height=S(self.CARD_HEIGHT), bg=BG, highlightthickness=0,
            bd=0, cursor="hand2",
        )
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.canvas.bind("<Button-1>", lambda e: self.command())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        self._draw()

    def pack(self, **kwargs):
        self.canvas.pack(**kwargs)
        return self

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        w = max(self.canvas.winfo_width(), S(320))
        h = S(self.CARD_HEIGHT)
        pad = S(2)
        active = self._active

        round_rect(
            c, pad, pad, w - pad, h - pad, S(16),
            fill=ACCENT_SOFT if active else SURFACE, outline="",
        )
        path = [
            pad + S(16), pad, w - pad - S(16), pad, w - pad, pad + S(16),
            w - pad, h - pad - S(16), w - pad - S(16), h - pad,
            pad + S(16), h - pad, pad, h - pad - S(16), pad, pad + S(16),
            pad + S(16), pad,
        ]
        c.create_line(
            path, smooth=True, width=1,
            fill=ACCENT if active else "#333A4D", dash=(6, 5),
        )

        title = "Drop it in" if active else "Drop your clips here"
        sub = "let go to add it" if active else "or click Choose clips below"
        hint = "MP4  -  MOV  -  MKV  -  WEBM  -  AVI"

        group_w = S(48) + S(20) + max(
            self._f_title.measure(title),
            self._f_sub.measure(sub),
            self._f_hint.measure(hint),
        )
        x = max(S(24), (w - group_w) / 2)
        cy = S(72)

        icon_x = x + S(24)
        c.create_oval(
            icon_x - S(25), cy - S(25), icon_x + S(25), cy + S(25),
            fill="#2B3168" if not active else "#343C86", outline="",
        )
        accent = "#AEB8FF"
        c.create_line(icon_x, cy - S(12), icon_x, cy + S(3), fill=accent,
                      width=max(2, S(4)), capstyle="round")
        c.create_polygon(
            icon_x - S(9), cy, icon_x + S(9), cy, icon_x, cy + S(12),
            fill=accent, outline="",
        )

        text_x = x + S(48) + S(20)
        c.create_text(text_x, cy - S(22), text=title, anchor="w",
                      fill=TEXT, font=self._font_title)
        c.create_text(text_x, cy + S(2), text=sub, anchor="w",
                      fill=TEXT_DIM, font=self._font_sub)
        c.create_text(text_x, cy + S(24), text=hint, anchor="w",
                      fill=TEXT_FAINT, font=self._font_hint)

        # The one obvious action: a real button shape inside the card.
        btn_w, btn_h = S(176), S(38)
        bx1 = (w - btn_w) / 2
        bx2 = bx1 + btn_w
        by1 = h - S(54)
        by2 = by1 + btn_h
        self._btn_box = (bx1, by1, bx2, by2)
        fill = ACCENT_HI if self._btn_hover else ACCENT
        round_rect(c, bx1, by1, bx2, by2, S(10), fill=fill, outline="")
        c.create_text(
            (bx1 + bx2) / 2, (by1 + by2) / 2 + S(1),
            text="Drop to add" if active else "Choose clips",
            fill="#FFFFFF", font=self._font_btn,
        )

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self._draw()

    def _on_motion(self, event) -> None:
        x1, y1, x2, y2 = self._btn_box
        inside = x1 <= event.x <= x2 and y1 <= event.y <= y2
        if inside != self._btn_hover:
            self._btn_hover = inside
            self._draw()

    def _on_enter(self, _event=None) -> None:
        self.canvas.configure(cursor="hand2")

    def _on_leave(self, _event=None) -> None:
        self._btn_hover = False
        self.set_active(False)
        self._draw()


class ProgressBar:
    def __init__(self, master, height=8):
        import tkinter as tk

        self._h = S(height)
        self._value = 0.0
        self.canvas = tk.Canvas(
            master, height=self._h, bg=BG, highlightthickness=0, bd=0,
        )
        self.canvas.bind("<Configure>", lambda e: self._draw())

    def pack(self, **kwargs):
        self.canvas.pack(**kwargs)
        return self

    def set(self, pct: float) -> None:
        pct = max(0.0, min(100.0, pct or 0.0))
        if abs(pct - self._value) > 0.4 or pct in (0.0, 100.0):
            self._value = pct
            self._draw()

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        w = max(self.canvas.winfo_width(), S(40))
        h = self._h
        round_rect(c, 0, 0, w, h, h / 2, fill=SURFACE_2, outline="")
        if self._value > 0:
            filled = max(h, w * self._value / 100.0)
            round_rect(c, 0, 0, filled, h, h / 2, fill=ACCENT, outline="")


class Pill:
    def __init__(self, master, text="", color=GREEN, bg=BG):
        import tkinter as tk
        import tkinter.font as tkfont

        self._text = text
        self._color = color
        self._font = F(11)
        self._font_obj = tkfont.Font(font=self._font)
        self._bg = bg
        self.canvas = tk.Canvas(master, height=S(26), width=10, bg=bg,
                                highlightthickness=0, bd=0)
        self._draw()

    def set(self, text: str, color: str) -> None:
        self._text = text
        self._color = color
        self._draw()

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        w = self._font_obj.measure(self._text) + S(30)
        h = S(26)
        c.configure(width=w)
        round_rect(c, 0, 0, w, h, h / 2, fill=SURFACE, outline=BORDER, width=1)
        c.create_oval(S(9), h / 2 - S(3), S(15), h / 2 + S(3),
                      fill=self._color, outline="")
        c.create_text(S(22), h / 2 + S(1), text=self._text, anchor="w",
                      fill=TEXT_DIM, font=self._font)


# --------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------

def ask_number(parent, title: str, prompt: str, initial: float,
               suffix: str = "MB") -> float | None:
    import tkinter as tk

    result: dict = {"value": None}
    win = tk.Toplevel(parent)
    win.withdraw()
    win.title(title)
    win.configure(bg=BG)
    win.resizable(False, False)
    win.transient(parent)

    body = tk.Frame(win, bg=BG)
    body.pack(fill="both", expand=True, padx=S(24), pady=S(20))
    tk.Label(body, text=prompt, bg=BG, fg=TEXT, font=F(13)).pack(anchor="w")

    entry_row = tk.Frame(body, bg=SURFACE_2, highlightthickness=1,
                         highlightbackground=BORDER)
    entry_row.pack(fill="x", pady=(S(12), S(18)))
    var = tk.StringVar(value=f"{initial:g}")
    entry = tk.Entry(
        entry_row, textvariable=var, bg=SURFACE_2, fg=TEXT, insertbackground=TEXT,
        relief="flat", font=F(15), bd=0, highlightthickness=0,
    )
    entry.pack(side="left", fill="x", expand=True, padx=(S(10), 0), pady=S(7))
    tk.Label(entry_row, text=suffix, bg=SURFACE_2, fg=TEXT_FAINT,
             font=F(12)).pack(side="right", padx=(S(4), S(10)))

    def commit(_event=None) -> None:
        try:
            value = float(var.get().strip().replace(",", "."))
        except ValueError:
            entry_row.configure(highlightbackground=RED)
            return
        if 0.5 <= value <= 20000:
            result["value"] = value
            win.destroy()
        else:
            entry_row.configure(highlightbackground=RED)

    buttons = tk.Frame(body, bg=BG)
    buttons.pack(fill="x")
    RoundedButton(buttons, "Cancel", win.destroy, kind="ghost",
                  width=88, height=34, radius=9).pack(side="right")
    RoundedButton(buttons, "Use size", commit, kind="primary",
                  width=104, height=34, radius=9).pack(side="right", padx=(0, S(8)))

    win.bind("<Return>", commit)
    win.bind("<Escape>", lambda e: win.destroy())
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
    win.geometry(f"+{max(0, x)}+{max(0, y)}")
    win.deiconify()
    win.grab_set()
    entry.focus_set()
    entry.select_range(0, "end")
    parent.wait_window(win)
    return result["value"]


class App:
    def __init__(self):
        import tkinter as tk

        self.tk = tk
        self.jobs: list[Job] = []
        self.uid = 0
        self.target_mb: float = 20.0
        self.out_dir: Path | None = None
        self.running = False
        self.started_at = 0.0
        self.events: queue.Queue = queue.Queue()
        self.runner = BatchRunner(self.events.put)
        self._downloading = False
        self._start_after_download = False
        self._closing = False
        self._run_uids: set[int] = set()

        refresh_tools()
        _clear_stale_downloads()

        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD
            self.dnd = DND_FILES
            root = TkinterDnD.Tk()
            self.has_dnd = True
        except Exception:
            self.dnd = None
            root = tk.Tk()
            self.has_dnd = False

        self.root = root
        root.title(APP_NAME)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._setup_style()
        self._build()
        self._place_window()
        self._wire_shortcuts()
        self._start_prober()

        if self.has_dnd:
            self._enable_dnd()

        if self._engine_ready():
            self.pill.set("engine ready", GREEN)
        else:
            self.pill.set("ffmpeg needed", YELLOW)
            self._say(text="One-time setup: press Squeeze and the app will fetch ffmpeg.")

        self._update_counts()
        self.root.after(60, self._poll)

    # -- construction ------------------------------------------------------
    def _setup_style(self) -> None:
        from tkinter import ttk

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except self.tk.TclError:
            pass
        style.configure(
            "Queue.Treeview",
            background=SURFACE, fieldbackground=SURFACE, foreground=TEXT,
            borderwidth=0, rowheight=S(33), font=F(13),
        )
        style.configure(
            "Queue.Treeview.Heading",
            background=SURFACE, foreground=TEXT_FAINT, borderwidth=0,
            relief="flat", font=F(11, "bold"), padding=(S(4), S(8)),
        )
        style.map(
            "Queue.Treeview.Heading",
            background=[("active", SURFACE)],
            relief=[("active", "flat")],
        )
        style.map(
            "Queue.Treeview",
            background=[("selected", "#262C42")],
            foreground=[("selected", TEXT)],
        )
        style.layout("Queue.Treeview", [
            ("Queue.Treeview.treearea", {"sticky": "nswe"}),
        ])
        style.configure(
            "Queue.Vertical.TScrollbar",
            background=SURFACE_3, troughcolor=SURFACE, bordercolor=SURFACE,
            arrowcolor=TEXT_FAINT, darkcolor=SURFACE_3, lightcolor=SURFACE_3,
            width=S(10), borderwidth=0, relief="flat",
        )
        style.map("Queue.Vertical.TScrollbar",
                  background=[("active", "#39415A")])

    def _build(self) -> None:
        tk = self.tk
        from tkinter import ttk

        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=S(26), pady=(S(20), S(16)))

        # ---- footer (packed first so it always stays visible) ------------
        footer = tk.Frame(outer, bg=BG)
        footer.pack(side="bottom", fill="x", pady=(S(14), 0))

        self.folder_btn = RoundedButton(
            footer, "Show in folder", self._show_last_output, kind="secondary",
            width=168, height=42,
        )
        self._folder_visible = False

        self.action_btn = RoundedButton(
            footer, "Squeeze clips", self._action, kind="primary",
            width=196, height=42,
        )
        self.action_btn.pack(side="right")

        # ---- progress ----------------------------------------------------
        progress = tk.Frame(outer, bg=BG)
        progress.pack(side="bottom", fill="x", pady=(S(14), 0))
        self.bar = ProgressBar(progress, height=7).pack(fill="x")

        meta = tk.Frame(outer, bg=BG)
        meta.pack(side="bottom", fill="x", pady=(S(7), 0))
        self.status = tk.Label(meta, text="Drag clips in, or press Ctrl+O to browse.",
                               bg=BG, fg=TEXT_DIM, font=F(12), anchor="w")
        self.status.pack(side="left")
        self.detail = tk.Label(meta, text="", bg=BG, fg=TEXT_FAINT, font=F(12),
                               anchor="e")
        self.detail.pack(side="right")

        # ---- header ------------------------------------------------------
        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")

        logo = tk.Canvas(header, width=S(42), height=S(42), bg=BG,
                         highlightthickness=0)
        logo.pack(side="left")
        round_rect(logo, 0, 0, S(42), S(42), S(12), fill=ACCENT, outline="")
        logo.create_line(S(21), S(11), S(21), S(25), fill="#FFFFFF",
                         width=max(2, S(3)), capstyle="round")
        logo.create_polygon(S(14), S(23), S(28), S(23), S(21), S(32),
                            fill="#FFFFFF", outline="")

        titles = tk.Frame(header, bg=BG)
        titles.pack(side="left", padx=(S(13), 0))
        tk.Label(titles, text=APP_NAME, bg=BG, fg=TEXT,
                 font=F(20, "bold")).pack(anchor="w")
        tk.Label(titles, text="Shrink clips until they fit the upload limit.",
                 bg=BG, fg=TEXT_DIM, font=F(12)).pack(anchor="w")

        self.pill = Pill(header, "checking", TEXT_FAINT)
        self.pill.canvas.pack(side="right", pady=(S(8), 0))

        # ---- drop zone ---------------------------------------------------
        self.dropzone = DropZone(outer, self._pick_files)
        self.dropzone.pack(fill="x", pady=(S(18), S(16)))

        # ---- file list ---------------------------------------------------
        listbar = tk.Frame(outer, bg=BG)
        listbar.pack(fill="x", pady=(0, S(8)))
        self.count_lbl = tk.Label(listbar, text="No clips yet", bg=BG, fg=TEXT,
                                  font=F(13, "bold"))
        self.count_lbl.pack(side="left")
        self.hint_lbl = tk.Label(
            listbar,
            text="double-click a finished clip to open it  -  Del removes",
            bg=BG, fg=TEXT_FAINT, font=F(11),
        )
        self.hint_lbl.pack(side="left", padx=(S(12), 0))
        RoundedButton(listbar, "Clear", self._clear, kind="ghost",
                      width=70, height=28, radius=8,
                      font=F(12)).pack(side="right")

        table = tk.Frame(outer, bg=SURFACE, highlightthickness=1,
                         highlightbackground=BORDER)
        table.pack(fill="both", expand=True)

        columns = ("size", "length", "result", "state")
        self.tree = ttk.Treeview(table, columns=columns, show="tree headings",
                                 style="Queue.Treeview", height=5,
                                 selectmode="extended")
        self.tree.heading("#0", text="CLIP", anchor="w")
        self.tree.heading("size", text="SIZE", anchor="e")
        self.tree.heading("length", text="LENGTH", anchor="e")
        self.tree.heading("result", text="RESULT", anchor="e")
        self.tree.heading("state", text="STATUS", anchor="w")
        self.tree.column("#0", width=S(330), minwidth=S(160), stretch=True,
                         anchor="w")
        self.tree.column("size", width=S(92), minwidth=S(70), stretch=False,
                         anchor="e")
        self.tree.column("length", width=S(80), minwidth=S(60), stretch=False,
                         anchor="e")
        self.tree.column("result", width=S(96), minwidth=S(70), stretch=False,
                         anchor="e")
        self.tree.column("state", width=S(150), minwidth=S(110), stretch=False,
                         anchor="w")

        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview,
                               style="Queue.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y", pady=S(2), padx=(0, S(2)))
        self.tree.pack(side="left", fill="both", expand=True, pady=S(2),
                       padx=(S(4), 0))

        self.tree.tag_configure("running", foreground="#AEB8FF")
        self.tree.tag_configure("done", foreground=TEXT)
        self.tree.tag_configure("fits", foreground=TEXT_FAINT)
        self.tree.tag_configure("error", foreground="#F07C7E")
        self.tree.tag_configure("canceled", foreground=TEXT_FAINT)

        self.tree.bind("<Double-1>", self._open_selected)
        self.tree.bind("<Delete>", lambda e: self._remove_selected())
        self.tree.bind("<BackSpace>", lambda e: self._remove_selected())

        # ---- options -----------------------------------------------------
        options = tk.Frame(outer, bg=BG)
        options.pack(fill="x", pady=(S(16), 0))
        options.columnconfigure(1, weight=1)

        def option_label(row: int, text: str) -> None:
            tk.Label(options, text=text, bg=BG, fg=TEXT_DIM, font=F(12),
                     anchor="w").grid(row=row, column=0, sticky="w",
                                      padx=(0, S(18)), pady=(0, S(8)))

        option_label(0, "Target size")
        self.target_chips = ChipGroup(
            options, TARGET_CHOICES + [("custom", "Custom...")],
            value=self.target_mb, on_change=self._on_target,
        ).grid(row=0, column=1, sticky="w", pady=(0, S(8)))

        option_label(1, "Speed")
        self.speed_chips = ChipGroup(
            options,
            [("fast", "Fast"), ("balanced", "Balanced"), ("best", "Best quality")],
            value="balanced", on_change=self._on_speed,
        ).grid(row=1, column=1, sticky="w", pady=(0, S(8)))

        option_label(2, "Save to")
        save_row = tk.Frame(options, bg=BG)
        save_row.grid(row=2, column=1, sticky="ew")
        self.save_lbl = tk.Label(save_row, text="Same folder as the clip", bg=BG,
                                 fg=TEXT, font=F(12), anchor="w")
        self.save_lbl.pack(side="left")
        self.save_btn = RoundedButton(save_row, "Change", self._choose_out_dir,
                                      kind="ghost", width=68, height=26,
                                      radius=7, font=F(11))
        self.save_btn.pack(side="left", padx=(S(8), 0))

        self._refresh_rows()

    def _place_window(self) -> None:
        root = self.root
        hidden = os.environ.get("CLIP_SQUEEZER_HIDDEN") == "1"
        if hidden:
            root.withdraw()
        root.update_idletasks()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        width = min(S(980), int(screen_w * 0.92))
        height = min(S(780), int(screen_h * 0.90))
        x = max(0, (screen_w - width) // 2)
        y = max(0, int((screen_h - height) * 0.42))
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.minsize(min(S(900), width), min(S(700), height))
        self._icon = self._make_icon()
        root.iconphoto(True, self._icon)

    def _make_icon(self):
        image = self.tk.PhotoImage(width=64, height=64)
        radius = 16
        for y in range(64):
            if y < radius:
                dx = int(radius - math.sqrt(max(0.0, radius ** 2 - (radius - 1 - y) ** 2)))
            elif y > 63 - radius:
                dx = int(radius - math.sqrt(max(0.0, radius ** 2 - (y - (63 - radius)) ** 2)))
            else:
                dx = 0
            image.put(ACCENT, (dx, y, 63 - dx, y + 1))
        image.put("#FFFFFF", (29, 15, 35, 37))
        for i in range(13):
            half = 11 - i
            image.put("#FFFFFF", (32 - half, 34 + i, 32 + half, 35 + i))
        return image

    def _wire_shortcuts(self) -> None:
        self.root.bind("<Control-o>", lambda e: self._pick_files())
        self.root.bind("<Control-O>", lambda e: self._pick_files())
        self.root.bind("<Return>", lambda e: self._action())

    # -- drag & drop -------------------------------------------------------
    def _enable_dnd(self) -> None:
        try:
            self.root.drop_target_register(self.dnd)
            self.root.dnd_bind("<<Drop>>", self._on_drop)
            self.root.dnd_bind("<<DropEnter>>", lambda e: self.dropzone.set_active(True))
            self.root.dnd_bind("<<DropLeave>>", lambda e: self.dropzone.set_active(False))
        except Exception:
            self.has_dnd = False

    def _on_drop(self, event) -> None:
        self.dropzone.set_active(False)
        try:
            paths = list(self.root.tk.splitlist(event.data))
        except Exception:
            paths = [str(event.data)]
        self._add_paths([Path(p) for p in paths])

    # -- engine ------------------------------------------------------------
    def _engine_ready(self) -> bool:
        return bool(_ffmpeg and _ffprobe and Path(_ffmpeg).is_file() and Path(_ffprobe).is_file())

    def _ensure_engine(self) -> bool:
        from tkinter import messagebox

        if self._engine_ready():
            return True
        if self._downloading:
            return False
        answer = messagebox.askyesno(
            "One-time setup",
            "Clip Squeezer needs ffmpeg to do the actual compressing.\n\n"
            "Download it now? It's a one-time ~200 MB download from the "
            "FFmpeg-Builds project on GitHub, and everything keeps working "
            "offline afterwards.",
            parent=self.root,
        )
        if answer:
            self._start_after_download = True
            self._download_engine()
        return False

    def _download_engine(self) -> None:
        if self._downloading:
            return
        self._downloading = True
        self.pill.set("downloading ffmpeg", YELLOW)
        self._say(text="Downloading ffmpeg - one time only, hang tight...")
        self.action_btn.set_enabled(False)
        self.bar.set(0)

        def worker() -> None:
            try:
                dest = install_dir()

                def progress(stage: str, frac: float) -> None:
                    self.events.put(("engine_progress", stage, frac))

                ffmpeg, ffprobe = download_ffmpeg(dest, progress)
                set_tools(ffmpeg, ffprobe)
                self.events.put(("engine_done",))
            except Exception as exc:
                self.events.put(("engine_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    # -- files -------------------------------------------------------------
    def _pick_files(self) -> None:
        from tkinter import filedialog

        types = [("Video clips", " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTS))),
                 ("All files", "*.*")]
        chosen = filedialog.askopenfilenames(parent=self.root, title="Choose clips",
                                             filetypes=types)
        if chosen:
            self._add_paths([Path(p) for p in chosen])

    def _add_paths(self, paths: list[Path]) -> None:
        existing = {str(job.src).lower() for job in self.jobs}
        added = 0
        skipped = 0
        for path in paths:
            candidates: list[Path] = []
            if path.is_dir():
                try:
                    candidates = sorted(
                        p for p in path.iterdir() if p.suffix.lower() in VIDEO_EXTS
                    )
                except OSError:
                    skipped += 1
                    continue
            elif path.suffix.lower() in VIDEO_EXTS:
                candidates = [path]
            else:
                skipped += 1
                continue
            for candidate in candidates:
                candidate = candidate.resolve()
                key = str(candidate).lower()
                if key in existing:
                    skipped += 1
                    continue
                try:
                    size = candidate.stat().st_size
                except OSError:
                    skipped += 1
                    continue
                existing.add(key)
                self.uid += 1
                job = Job(uid=self.uid, src=candidate, size=size)
                self.jobs.append(job)
                self._probe_queue.put(job)
                added += 1
            if path.is_dir() and not candidates:
                skipped += 1

        if added:
            self._refresh_rows()
            self._say(
                text=f"Added {added} clip{'s' if added != 1 else ''}."
                + (f" Skipped {skipped} unsupported or duplicate." if skipped else "")
            )
        elif skipped:
            self._say(text="Those files weren't videos, or are already in the list.")
        self._update_counts()

    def _refresh_rows(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for job in self.jobs:
            self.tree.insert("", "end", iid=str(job.uid), text=job.src.name,
                             values=self._row_values(job), tags=(job.state,))

    def _row_values(self, job: Job) -> tuple:
        size = human_size(job.size)
        if job.info:
            length = human_time(job.info.duration)
        else:
            length = "..." if not job.probed else "--"
        result = f"\u2192 {self.target_mb:g} MB"
        status = "Ready"

        if job.state == "running":
            result = f"\u2192 {self.target_mb:g} MB"
            status = f"Compressing {job.pct:.0f}%"
        elif job.state == "done":
            result = human_size(job.out_size)
            if self._result_path(job) is None:
                status = "Missing - retry"
                result = "--"
            else:
                saved = (1 - job.out_size / job.size) * 100 if job.size else 0
                status = f"Done - {saved:.0f}% smaller" if saved >= 1 else "Done"
        elif job.state == "fits":
            result = "no change"
            status = "Already fits"
        elif job.state == "error":
            result = "--"
            status = job.note or "Failed"
        elif job.state == "canceled":
            result = "--"
            status = "Canceled"
        return (size, length, result, status)

    def _refresh_row(self, job: Job) -> None:
        iid = str(job.uid)
        if self.tree.exists(iid):
            self.tree.item(iid, values=self._row_values(job), tags=(job.state,))

    def _say(self, text: str, color: str = TEXT_DIM) -> None:
        """Status line: one line, never long enough to push the layout around."""
        text = " ".join(str(text).split())
        if len(text) > 95:
            text = text[:92].rstrip() + "..."
        self.status.configure(text=text, fg=color)

    def _update_counts(self) -> None:
        total = len(self.jobs)
        if total == 0:
            self.count_lbl.configure(text="No clips yet")
        else:
            total_bytes = sum(j.size for j in self.jobs)
            self.count_lbl.configure(
                text=f"{total} clip{'s' if total != 1 else ''}   -   {human_size(total_bytes)}"
            )
        self._update_action()

    def _pending_jobs(self) -> list[Job]:
        pending = [j for j in self.jobs if j.state in ("queued", "error", "canceled")]
        # A finished job whose output disappeared is retryable from source.
        for j in self.jobs:
            if j.state == "done" and self._result_path(j) is None:
                pending.append(j)
        return pending

    def _result_path(self, job: Job) -> Path | None:
        """The file to reveal: the compressed output, or the original if it fit."""
        if job.out and job.out.exists():
            return job.out
        if job.state == "fits" and job.src.exists():
            return job.src
        return None

    def _has_results(self) -> bool:
        return any(self._result_path(j) for j in self.jobs)

    def _set_folder_visible(self, visible: bool) -> None:
        if visible == self._folder_visible:
            return
        self._folder_visible = visible
        if visible:
            self.folder_btn.pack(side="left")
        else:
            self.folder_btn.canvas.pack_forget()

    def _update_action(self) -> None:
        if self.running:
            self._set_folder_visible(False)
            self.target_chips.set_enabled(False)
            self.speed_chips.set_enabled(False)
            self.save_btn.set_enabled(False)
            self.action_btn.set_text("Cancel")
            self.action_btn.set_kind("danger")
            self.action_btn.set_enabled(True)
            return

        if self._downloading:
            self.action_btn.set_enabled(False)
            return

        pending = self._pending_jobs()
        has_results = self._has_results()
        self.target_chips.set_enabled(True)
        self.speed_chips.set_enabled(True)
        self.save_btn.set_enabled(True)
        # With more work queued, the finished file stays reachable on the left;
        # otherwise the big button itself becomes "Show in folder".
        self._set_folder_visible(bool(pending) and has_results)
        self.action_btn.set_kind("primary")
        if pending:
            self.action_btn.set_enabled(True)
            if len(pending) == 1:
                self.action_btn.set_text("Squeeze 1 clip")
            else:
                self.action_btn.set_text(f"Squeeze {len(pending)} clips")
        elif has_results:
            self.action_btn.set_enabled(True)
            self.action_btn.set_text("Show in folder")
        else:
            self.action_btn.set_enabled(False)
            self.action_btn.set_text("Squeeze clips")

    def _set_target_hint(self) -> None:
        if not self.jobs:
            return
        self._say(
            text=f"Target: {self.target_mb:g} MB. Files that already fit are left alone."
        )

    def _remove_selected(self) -> None:
        if self.running:
            return
        selection = set(self.tree.selection())
        if not selection:
            return
        self.jobs = [j for j in self.jobs if str(j.uid) not in selection]
        self._refresh_rows()
        self._update_counts()

    def _clear(self) -> None:
        if self.running:
            return
        self.jobs = []
        self._refresh_rows()
        self._update_counts()
        self.bar.set(0)
        self.detail.configure(text="")
        self._say(text="Drag clips in, or press Ctrl+O to browse.")

    # -- options -----------------------------------------------------------
    def _on_target(self, value) -> None:
        if self.running:
            return
        if value == "custom":
            current = self.target_mb
            chosen = ask_number(self.root, "Custom target size",
                                "Compress each clip to at most:", current)
            if chosen is None:
                # Put the chip back where it was; a cancelled dialog must not
                # leave the highlight on "10 MB" while the target is still 18.
                matching = [v for v, _ in TARGET_CHOICES
                            if abs(v - self.target_mb) < 1e-6]
                self.target_chips.select(matching[0] if matching else "custom",
                                         notify=False)
                return
            self.target_mb = float(chosen)
            self.target_chips.relabel("custom", f"{chosen:g} MB")
            self.target_chips.select("custom", notify=False)
        else:
            self.target_mb = float(value)
            self.target_chips.select(value, notify=False)
        for job in self.jobs:
            # A clip marked "already fits" must become work again if the target
            # just shrank below its size.
            if job.state == "fits" and job.size > self.target_mb * MB:
                job.state = "queued"
            if job.state == "done":
                rp = self._result_path(job)
                if rp is not None and rp.stat().st_size > self.target_mb * MB:
                    job.state = "queued"
                    job.out = None
                    job.out_size = 0
            if job.state in ("queued", "error", "canceled", "fits"):
                self._refresh_row(job)
        self._update_action()
        self._set_target_hint()

    def _on_speed(self, value) -> None:
        if self.running:
            return
        self._say(
            text={"fast": "Fast: quickest encode, slightly softer detail.",
                  "balanced": "Balanced: good detail at a sensible speed.",
                  "best": "Best quality: slowest, squeezes the most detail in."}[value]
        )

    def _choose_out_dir(self) -> None:
        if self.running:
            return
        from tkinter import filedialog

        folder = filedialog.askdirectory(parent=self.root,
                                         title="Where should finished clips go?")
        if not folder:
            return
        self.out_dir = Path(folder)
        self._show_out_dir()

    def _show_out_dir(self) -> None:
        if self.out_dir:
            text = self._shorten(self.out_dir)
            self.save_lbl.configure(text=text, fg=TEXT)
            self.save_btn.set_text("Reset")
            self.save_btn.command = self._reset_out_dir
        else:
            self.save_lbl.configure(text="Same folder as the clip", fg=TEXT)
            self.save_btn.set_text("Change")
            self.save_btn.command = self._choose_out_dir

    def _reset_out_dir(self) -> None:
        self.out_dir = None
        self._show_out_dir()

    def _shorten(self, path: Path, limit: int = 46) -> str:
        text = str(path)
        home = str(Path.home())
        if text.lower().startswith(home.lower()):
            text = "~" + text[len(home):]
        if len(text) <= limit:
            return text
        parts = Path(text).parts
        if len(parts) <= 3:
            return text[:limit - 3] + "..."
        return f"{parts[0]}...{os.sep}{parts[-2]}{os.sep}{parts[-1]}"

    # -- run ---------------------------------------------------------------
    def _action(self) -> None:
        if self.running:
            self._cancel()
        elif self._pending_jobs():
            self._start()
        elif self._has_results():
            self._show_last_output()

    def _start(self) -> None:
        pending = self._pending_jobs()
        if not pending:
            if self.jobs:
                self._say(text="Everything in the list is done. Add another clip.")
            return
        if not self._ensure_engine():
            return

        for job in pending:
            job.state = "queued"
            job.pct = 0.0
            job.note = ""
            job.out = None
            job.out_size = 0
            self._refresh_row(job)

        self.running = True
        self.started_at = time.time()
        self._run_uids = {j.uid for j in pending}
        self._update_action()
        self.action_btn.set_text("Cancel")
        self.bar.set(0)
        self._say(text="Starting...")
        self.detail.configure(text=f"0 of {len(pending)}")

        settings = Settings(target_mb=self.target_mb, speed=self.speed_chips.value,
                            out_dir=self.out_dir)
        self.runner = BatchRunner(self.events.put)
        self.runner.start(pending, settings)

    def _cancel(self) -> None:
        self.runner.cancel()
        self._say(text="Cancelling...")

    def _reveal(self, path: Path) -> None:
        """Open Explorer with this file highlighted."""
        try:
            subprocess.Popen(["explorer", "/select,", str(path)])
        except OSError:
            try:
                os.startfile(str(path.parent))
            except OSError:
                pass

    def _show_last_output(self) -> None:
        for job in reversed(self.jobs):
            path = self._result_path(job)
            if path:
                self._reveal(path)
                return
        self._say(text="That file isn't there anymore - it may have been moved.",
                  color=YELLOW)

    def _open_selected(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        job = next((j for j in self.jobs if str(j.uid) == selection[0]), None)
        if job:
            path = self._result_path(job)
            if path:
                self._reveal(path)
                return
            self._say(text=f"{job.src.name}: {job.note or job.state}")

    # -- probing -----------------------------------------------------------
    def _start_prober(self) -> None:
        self._probe_queue: queue.Queue = queue.Queue()
        self._probe_stop = threading.Event()
        thread = threading.Thread(target=self._prober, daemon=True)
        self._probe_thread = thread
        thread.start()
        self.root.bind("<F5>", lambda e: self._requeue_probes())

    def _prober(self) -> None:
        while not self._probe_stop.is_set():
            try:
                job = self._probe_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                return
            try:
                info = probe_media(job.src, self._probe_stop)
                job.info = info
                job.probed = True
                self.events.put(("probed", job))
            except Cancelled:
                job.probed = True
                return
            except Exception:
                job.probed = True
                self.events.put(("probe_failed", job))

    def _requeue_probes(self) -> None:
        for job in self.jobs:
            if not job.info:
                self._probe_queue.put(job)

    # -- event pump --------------------------------------------------------
    def _poll(self) -> None:
        if self._closing:
            return
        try:
            while True:
                event = self.events.get_nowait()
                self._handle(event)
        except queue.Empty:
            pass
        except Exception:
            import traceback

            try:
                traceback.print_exc()
            except Exception:
                pass
        if self.running:
            elapsed = time.time() - self.started_at
            self.detail.configure(text=f"{human_time(elapsed)} elapsed")
        self.root.after(60, self._poll)

    def _handle(self, event) -> None:
        kind = event[0]
        if kind == "progress":
            job, pct = event[1], event[2]
            job.pct = pct
            self._refresh_row(job)
            run_jobs = [j for j in self.jobs if j.uid in self._run_uids]
            done = sum(1 for j in run_jobs if j.state in ("done", "fits"))
            active = len([j for j in run_jobs if j.state in ("queued", "running")])
            total = done + active
            overall = ((done + pct / 100.0) / total * 100.0) if total else 0.0
            self.bar.set(overall)
        elif kind == "job":
            job = event[1]
            self._refresh_row(job)
            if job.state == "error":
                self._say(text=f"{job.src.name}: {job.note}")
        elif kind == "status":
            self._say(text=event[1])
        elif kind in ("probed", "probe_failed"):
            self._refresh_row(event[1])
        elif kind == "finished":
            self._finish(event[1])
        elif kind == "engine_progress":
            stage, frac = event[1], event[2]
            if stage == "extract":
                self._say(text="Unpacking ffmpeg...")
                self.pill.set("unpacking", YELLOW)
            else:
                self._say(text=f"Downloading ffmpeg... {frac * 100:.0f}%")
                self.pill.set(f"ffmpeg {frac * 100:.0f}%", YELLOW)
                self.bar.set(frac * 100)
        elif kind == "engine_done":
            self._downloading = False
            self._requeue_probes()
            self.pill.set("engine ready", GREEN)
            self._say(text="ffmpeg installed. Ready to squeeze.")
            self.bar.set(0)
            self._update_action()
            if self._start_after_download:
                self._start_after_download = False
                self.root.after(300, self._start)
        elif kind == "engine_error":
            self._downloading = False
            self.pill.set("ffmpeg missing", RED)
            self._say(text=f"Download failed: {event[1]}")
            self.bar.set(0)
            self._update_action()

    def _finish(self, summary: dict) -> None:
        self.running = False
        self._update_action()
        total = summary.get("total") or 1
        self.bar.set(summary["done"] / total * 100.0)
        fits = sum(1 for j in self.jobs if j.uid in self._run_uids and j.state == "fits")
        if summary["canceled"]:
            self._say(text="Canceled.", color=TEXT_DIM)
        elif summary["failed"] and not summary["done"]:
            self._say(text="Nothing could be compressed - check the list above.",
                      color=RED)
        elif summary["done"] and summary["done"] == fits and not summary["failed"]:
            self._say(text="Already under the limit - nothing to compress.",
                      color=GREEN)
        else:
            bits = [f"{summary['done']} clip{'s' if summary['done'] != 1 else ''} ready"]
            if summary["saved"]:
                bits.append(f"{human_size(summary['saved'])} smaller in total")
            if summary["failed"]:
                bits.append(f"{summary['failed']} failed")
            self._say(text="Done - " + "  -  ".join(bits) + ".", color=GREEN)
        self.detail.configure(text="")
        self._update_counts()

    # -- misc --------------------------------------------------------------
    def _on_close(self) -> None:
        from tkinter import messagebox

        if self.running:
            if not messagebox.askyesno("Still working",
                                       "A clip is still compressing. Quit anyway?",
                                       parent=self.root):
                return
            self._closing = True
            self.runner.cancel()
            thread = self.runner.thread
            if thread is not None:
                thread.join(timeout=5)
        # Stop any in-flight probe so its ffprobe child doesn't linger.
        if hasattr(self, "_probe_stop"):
            self._probe_stop.set()
        if hasattr(self, "_probe_thread"):
            self._probe_thread.join(timeout=5)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    global _SCALE

    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in ("--cli", "--install-ffmpeg", "--help", "-h"):
        if args[0] in ("--install-ffmpeg",):
            return run_cli(args)
        if args[0] in ("--help", "-h"):
            print(__doc__)
            return 0
        return run_cli(args[1:])

    enable_dpi_awareness()

    import tkinter as tk

    probe_root = tk.Tk()
    probe_root.withdraw()
    try:
        dpi = probe_root.winfo_fpixels("1i") / 96.0
    except Exception:
        dpi = 1.0
    probe_root.destroy()
    _SCALE = max(1.0, min(dpi, 1.35))

    try:
        App().run()
    except Exception as exc:  # last-resort guard so a bug isn't a silent exit
        import traceback

        log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ClipSqueezer"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        try:
            from tkinter import messagebox

            # Hidden mode is used for off-screen testing; never pop a dialog.
            if not os.environ.get("CLIP_SQUEEZER_HIDDEN"):
                messagebox.showerror(
                    APP_NAME,
                    f"{APP_NAME} hit a problem and has to close.\n\n{exc}\n\n"
                    f"Details were saved to {log_dir / 'error.log'}",
                )
        except Exception:
            try:
                print(traceback.format_exc())
            except Exception:
                pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
