"""Thumbnails and lazy video transcoding, via ffmpeg.

Design constraint that shapes this whole module: labboard runs on machines that are
mid-training (ws-4 has an RTX 5090 to keep fed). The board is never allowed to slow a
run down, so every ffmpeg call is `nice -n 19`, thread-capped, and gated behind a small
global semaphore. Originals are never modified — derived files go to ~/.cache/labboard.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path

# Only two ffmpeg processes at a time, each on 2 threads: a hard ceiling on how much
# CPU the board can ever take from a training job.
_SEM = asyncio.Semaphore(2)
_FFMPEG_THREADS = "2"
_NICE = ["nice", "-n", "19"]

THUMB_WIDTH = 320
PROBE_TIMEOUT = 15
THUMB_TIMEOUT = 60
TRANSCODE_TIMEOUT = 1800  # 30 min ceiling for a long eval video

# Browsers reliably play H.264 only in 8-bit 4:2:0. h264/yuv444p is a real mjlab output
# and silently fails to render, which is exactly the case this module exists to catch.
PLAYABLE_VIDEO = {"h264", "vp8", "vp9", "av1"}
PLAYABLE_AUDIO = {"aac", "mp3", "opus", "vorbis", ""}
PLAYABLE_PIXFMT = {"yuv420p", "yuvj420p"}
PLAYABLE_CONTAINER = {".mp4", ".webm", ".m4v"}


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "labboard"


def _key(path: Path, tag: str) -> str:
    """Cache key bound to identity *and* content, so a rewritten file re-renders."""
    try:
        st = path.stat()
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        stamp = "missing"
    return hashlib.sha256(f"{path}|{stamp}|{tag}".encode()).hexdigest()[:24]


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


async def _run(cmd: list[str], timeout: int) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, b"", b"timeout"
    return proc.returncode or 0, out, err


async def probe(path: Path) -> dict:
    """ffprobe summary: {vcodec, acodec, pix_fmt, width, height, duration}."""
    code, out, _ = await _run(
        [
            *_NICE, "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,codec_name,pix_fmt,width,height",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1",
            str(path),
        ],
        PROBE_TIMEOUT,
    )
    if code != 0:
        return {}

    info: dict = {"vcodec": "", "acodec": "", "pix_fmt": "", "width": 0, "height": 0, "duration": 0.0}
    current = None
    for line in out.decode(errors="replace").splitlines():
        key, _, value = line.partition("=")
        if key == "codec_type":
            current = value
        elif key == "codec_name":
            info["vcodec" if current == "video" else "acodec"] = value
        elif key == "pix_fmt":
            info["pix_fmt"] = value
        elif key in ("width", "height"):
            info[key] = int(value) if value.isdigit() else 0
        elif key == "duration":
            try:
                info["duration"] = float(value)
            except ValueError:
                pass
    return info


def is_browser_playable(path: Path, info: dict) -> bool:
    if not info or not info.get("vcodec"):
        return False
    if path.suffix.lower() not in PLAYABLE_CONTAINER:
        return False
    if info["vcodec"] not in PLAYABLE_VIDEO:
        return False
    if info["vcodec"] == "h264" and info.get("pix_fmt") not in PLAYABLE_PIXFMT:
        return False  # e.g. yuv444p / 10-bit: decodes in ffmpeg, black frame in a browser
    return info.get("acodec", "") in PLAYABLE_AUDIO


async def thumbnail(path: Path, kind: str) -> Path | None:
    """Render (and cache) a thumbnail. Returns None if it cannot be made."""
    if kind not in ("image", "video") or not have_ffmpeg():
        return None

    out = cache_root() / "thumbs" / f"{_key(path, f'thumb{THUMB_WIDTH}')}.webp"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)

    scale = f"scale={THUMB_WIDTH}:-2:force_original_aspect_ratio=decrease"
    async with _SEM:
        if out.exists():  # another request rendered it while we waited
            return out
        tmp = out.with_suffix(".tmp.webp")

        if kind == "video":
            info = await probe(path)
            # Seek ~10% in: frame 0 of a rollout video is often an empty scene.
            seek = max(0.0, min(info.get("duration", 0.0) * 0.1, 5.0))
            cmd = [*_NICE, "ffmpeg", "-y", "-v", "error", "-threads", _FFMPEG_THREADS,
                   "-ss", f"{seek:.2f}", "-i", str(path), "-frames:v", "1",
                   "-vf", scale, str(tmp)]
        else:
            cmd = [*_NICE, "ffmpeg", "-y", "-v", "error", "-threads", _FFMPEG_THREADS,
                   "-i", str(path), "-frames:v", "1", "-vf", scale, str(tmp)]

        code, _, _ = await _run(cmd, THUMB_TIMEOUT)
        if code != 0 and kind == "video":
            # Seek past the end (very short clip) — retry from the first frame.
            cmd = [*_NICE, "ffmpeg", "-y", "-v", "error", "-threads", _FFMPEG_THREADS,
                   "-i", str(path), "-frames:v", "1", "-vf", scale, str(tmp)]
            code, _, _ = await _run(cmd, THUMB_TIMEOUT)

    if code != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(out)
    return out


async def transcoded(path: Path) -> Path | None:
    """Transcode to browser-safe H.264/AAC, cached. The original is never touched."""
    if not have_ffmpeg():
        return None

    out = cache_root() / "video" / f"{_key(path, 'h264')}.mp4"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)

    async with _SEM:
        if out.exists():
            return out
        tmp = out.with_suffix(".tmp.mp4")
        code, _, _ = await _run(
            [
                *_NICE, "ffmpeg", "-y", "-v", "error", "-threads", _FFMPEG_THREADS,
                "-i", str(path),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",  # x264 needs even dimensions
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",  # moov atom up front, so it streams
                str(tmp),
            ],
            TRANSCODE_TIMEOUT,
        )

    if code != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(out)
    return out


def cache_usage() -> tuple[int, int]:
    """(bytes, file count) of derived data — surfaced in the UI so it can't grow unnoticed."""
    root = cache_root()
    if not root.exists():
        return 0, 0
    total = count = 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
                count += 1
            except OSError:
                continue
    return total, count


def clear_cache() -> None:
    shutil.rmtree(cache_root(), ignore_errors=True)
