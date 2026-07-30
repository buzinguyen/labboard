"""Pin storage.

A *pin* is `(absolute path, title)` on the machine running this service. It is both a
bookmark and the authorization scope: `safety.resolve()` refuses to serve anything that
does not live beneath a pinned root. Adding a pin is the only way to widen access.

Pins live on the node they point at (`~/.config/labboard/pins.toml`), never centrally.
That is what lets an agent register a directory with a purely local file write — no
cross-host call, no credentials, no keys anywhere in the system.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import tomli_w

CONFIG_ENV = "LABBOARD_CONFIG"


def config_path() -> Path:
    """Location of pins.toml. `LABBOARD_CONFIG` overrides (used by tests)."""
    if override := os.environ.get(CONFIG_ENV):
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "labboard" / "pins.toml"


def make_id(path: Path) -> str:
    """Short, stable id derived from the absolute path, so re-adding is idempotent."""
    return hashlib.sha256(str(path).encode()).hexdigest()[:10]


@dataclass
class Pin:
    id: str
    path: str
    title: str
    tags: list[str] = field(default_factory=list)
    note: str = ""
    added: str = ""

    @property
    def root(self) -> Path:
        """The pin's target, with symlinks resolved.

        Resolved here (once, at the boundary) so that every containment check in
        `safety` compares realpath against realpath.
        """
        return Path(self.path).expanduser().resolve()

    @property
    def exists(self) -> bool:
        try:
            return self.root.is_dir()
        except OSError:
            # Unreachable NFS mount, permission denied on a parent, etc.
            return False

    @property
    def activity(self) -> float:
        """mtime of the pinned directory — a cheap proxy for "a run landed here".

        A directory's mtime changes when entries are added or removed, so this
        surfaces the pin a new run just wrote into. O(1), unlike walking the tree.
        """
        try:
            return self.root.stat().st_mtime
        except OSError:
            return 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "tags": self.tags,
            "note": self.note,
            "added": self.added,
        }


def load_pins() -> list[Pin]:
    path = config_path()
    if not path.exists():
        return []
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    pins = []
    for raw in data.get("pins", []):
        try:
            pins.append(
                Pin(
                    id=raw["id"],
                    path=raw["path"],
                    title=raw.get("title") or Path(raw["path"]).name,
                    tags=list(raw.get("tags", [])),
                    note=raw.get("note", ""),
                    added=raw.get("added", ""),
                )
            )
        except KeyError:
            # A malformed entry must not take the whole board down.
            continue
    return pins


def save_pins(pins: list[Pin]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: a torn pins.toml would lock you out of every pin at once.
    tmp = path.with_suffix(".toml.tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump({"pins": [p.to_dict() for p in pins]}, fh)
    tmp.replace(path)


def get_pin(pin_id: str) -> Pin | None:
    return next((p for p in load_pins() if p.id == pin_id), None)


def add_pin(path: Path, title: str = "", tags: list[str] | None = None, note: str = "") -> Pin:
    """Register a directory. Idempotent — re-adding an existing path updates it in place."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise NotADirectoryError(f"not a directory: {resolved}")

    pins = load_pins()
    pin = Pin(
        id=make_id(resolved),
        path=str(resolved),
        title=title or resolved.name,
        tags=tags or [],
        note=note,
        added=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    for i, existing in enumerate(pins):
        if existing.id == pin.id:
            pin.added = existing.added or pin.added  # keep the original registration time
            pins[i] = pin
            break
    else:
        pins.append(pin)
    save_pins(pins)
    return pin


def remove_pin(pin_id: str) -> bool:
    pins = load_pins()
    kept = [p for p in pins if p.id != pin_id]
    if len(kept) == len(pins):
        return False
    save_pins(kept)
    return True
