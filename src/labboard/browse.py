"""Directory listing and file-kind classification.

Everything here assumes the path already came out of `safety.resolve()`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .safety import is_denied

# Extension → kind. Kind drives both the icon and which inline viewer is used.
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".m4v", ".gif"}
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a"}
MARKDOWN_EXT = {".md", ".markdown"}
PDF_EXT = {".pdf"}
TABLE_EXT = {".csv", ".tsv"}
TEXT_EXT = {
    ".txt", ".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".sh", ".bash", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".js", ".ts",
    ".xml", ".html", ".css", ".sql", ".rst", ".tex", ".make", ".mk", ".dockerfile",
}
# Big and never viewable — listed, but the UI offers download only and never a thumbnail.
OPAQUE_EXT = {
    ".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".pkl", ".pickle", ".npz", ".npy",
    ".h5", ".hdf5", ".bin", ".pb", ".tfevents", ".zip", ".tar", ".gz", ".xz", ".7z",
}

# Files worth surfacing at the top of a run directory.
README_NAMES = ("report.md", "readme.md", "summary.md", "results.md", "notes.md")

MAX_INLINE_TEXT = 2 * 1024 * 1024  # 2 MB — beyond this, offer download instead


def classify(path: Path, is_dir: bool = False) -> str:
    if is_dir:
        return "dir"
    ext = path.suffix.lower()
    name = path.name.lower()
    if ext in MARKDOWN_EXT:
        return "markdown"
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in PDF_EXT:
        return "pdf"
    if ext in TABLE_EXT:
        return "table"
    if ext in OPAQUE_EXT or name.startswith("events.out.tfevents"):
        return "opaque"
    if ext in TEXT_EXT or name in ("makefile", "dockerfile", "license", "readme"):
        return "text"
    return "binary"


def human_size(n: int) -> str:
    step = 1024.0
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"


def human_age(ts: float) -> str:
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 86400 * 30:
        return f"{seconds // 86400}d ago"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Entry:
    name: str
    rel: str  # path relative to the pin root, for building URLs
    is_dir: bool
    kind: str
    size: int
    mtime: float

    @property
    def size_h(self) -> str:
        # A directory has no meaningful size. An em-dash reads as "not applicable";
        # an empty cell just looks like the column failed to render.
        return "—" if self.is_dir else human_size(self.size)

    @property
    def age_h(self) -> str:
        return human_age(self.mtime)

    @property
    def iso(self) -> str:
        return datetime.fromtimestamp(self.mtime, timezone.utc).isoformat(timespec="seconds")

    @property
    def thumbable(self) -> bool:
        return self.kind in ("image", "video")


def list_dir(root: Path, target: Path, show_hidden: bool = False) -> list[Entry]:
    """List `target`, newest-first, directories grouped before files.

    Newest-first is the point of the whole app: the thing you want is almost always
    the thing that just finished.
    """
    entries: list[Entry] = []
    with os.scandir(target) as it:
        for de in it:
            if is_denied(de.name):
                continue
            if not show_hidden and de.name.startswith("."):
                continue
            try:
                is_dir = de.is_dir()
                st = de.stat()
            except OSError:
                continue  # broken symlink, vanished mid-scan, permission denied
            path = Path(de.path)
            entries.append(
                Entry(
                    name=de.name,
                    rel=str(path.relative_to(root)),
                    is_dir=is_dir,
                    kind=classify(path, is_dir),
                    size=0 if is_dir else st.st_size,
                    mtime=st.st_mtime,
                )
            )

    entries.sort(key=lambda e: (not e.is_dir, -e.mtime))
    return entries


# Bounds for the nested-media walk. A run tree can hold thousands of frames; the panel
# is for jumping to results, not for enumerating every PNG.
NESTED_LIMIT = 400
NESTED_DEPTH = 8

# Directories with no plausible media, skipped to keep the walk cheap. `wandb` is
# deliberately absent — wandb/run-*/files/media/videos is exactly where eval clips live.
NESTED_SKIP = frozenset({"node_modules", "__pycache__", ".venv", "venv", "site-packages"})


def find_nested_media(
    root: Path, start: Path, limit: int = NESTED_LIMIT, max_depth: int = NESTED_DEPTH
) -> tuple[list[Entry], bool]:
    """Image/video files *below* `start`, newest first.

    Files directly in `start` are excluded — those already render in the page's own
    gallery, and the point of this panel is reaching media buried further down.

    Returns (entries, truncated). Symlinked directories are not followed, and any file
    resolving outside the pin is dropped, so this cannot walk out of the pinned tree.
    """
    found: list[Entry] = []
    truncated = False
    start_depth = len(start.parts)

    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        here = Path(dirpath)
        depth = len(here.parts) - start_depth

        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".") and d not in NESTED_SKIP and not is_denied(d)
            )

        if here == start:
            continue  # handled by the current directory's own gallery

        for name in filenames:
            if name.startswith(".") or is_denied(name):
                continue
            path = here / name
            if classify(path) not in ("image", "video"):
                continue
            try:
                # A symlinked file could point outside the pin; drop it rather than
                # advertise something /raw would refuse to serve.
                if not path.resolve().is_relative_to(root):
                    continue
                st = path.stat()
            except OSError:
                continue

            found.append(
                Entry(
                    name=name,
                    rel=str(path.relative_to(root)),
                    is_dir=False,
                    kind=classify(path),
                    size=st.st_size,
                    mtime=st.st_mtime,
                )
            )
            if len(found) >= limit:
                truncated = True
                break
        if truncated:
            break

    found.sort(key=lambda e: -e.mtime)
    return found, truncated


def find_report(entries: list[Entry]) -> Entry | None:
    """The markdown file to render at the top of a directory, if any."""
    by_name = {e.name.lower(): e for e in entries if not e.is_dir}
    for candidate in README_NAMES:
        if candidate in by_name:
            return by_name[candidate]
    return None


def breadcrumbs(rel: str) -> list[tuple[str, str]]:
    """[(label, rel_path)] for each ancestor of `rel`, excluding the pin root itself."""
    if not rel:
        return []
    parts = Path(rel).parts
    return [(part, str(Path(*parts[: i + 1]))) for i, part in enumerate(parts)]
