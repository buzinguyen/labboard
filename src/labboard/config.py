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


ARTIFACT = "artifact"
PROJECT = "project"
KINDS = (ARTIFACT, PROJECT)


@dataclass
class Pin:
    """A pinned directory, of one of two kinds.

    `artifact` — the original kind, and the default: its *bytes* are servable. Browse,
    download, thumbnail. This is where run outputs live.

    `project` — a repo root whose tickets labboard reads. Nothing under it is served;
    `safety.resolve()` refuses the pin outright, so pinning a project cannot expose its
    source tree. Only `docs/log/tasks/*.md` is read, via a constant path that never
    touches user input.

    Keeping these separate is what lets a project be pinned at all: an artifact pin
    grants the tailnet read access to a whole tree, which is fine for `~/artifacts` and
    completely wrong for a code checkout.
    """

    id: str
    path: str
    title: str
    tags: list[str] = field(default_factory=list)
    note: str = ""
    added: str = ""
    archived: bool = False
    kind: str = ARTIFACT
    # Stable slug joining this pin to the same project on other devices.
    project: str = ""
    # Exactly one device should claim `main` for a project: the one that owns its
    # tickets. Others run experiments for it and report back with receipts.
    main: bool = False

    @property
    def is_project(self) -> bool:
        return self.kind == PROJECT

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
            "archived": self.archived,
            "kind": self.kind,
            "project": self.project,
            "main": self.main,
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
            kind = str(raw.get("kind") or ARTIFACT)
            if kind not in KINDS:
                # Unrecognized kind means a config from a newer or foreign version.
                # Fail closed: PROJECT serves no bytes, so an unknown pin cannot
                # accidentally expose a tree we do not understand.
                kind = PROJECT
            pins.append(
                Pin(
                    id=raw["id"],
                    path=raw["path"],
                    title=raw.get("title") or Path(raw["path"]).name,
                    tags=list(raw.get("tags", [])),
                    note=raw.get("note", ""),
                    added=raw.get("added", ""),
                    archived=bool(raw.get("archived", False)),
                    kind=kind,
                    project=str(raw.get("project", "")),
                    main=bool(raw.get("main", False)),
                )
            )
        except KeyError:
            # A malformed entry must not take the whole board down.
            continue
    return pins


def project_pins() -> list[Pin]:
    return [p for p in load_pins() if p.is_project and not p.archived]


def artifact_pins() -> list[Pin]:
    return [p for p in load_pins() if not p.is_project and not p.archived]


def active_pins() -> list[Pin]:
    """Pins that should appear in the board. Archived ones are kept but hidden."""
    return [p for p in load_pins() if not p.archived]


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


def add_pin(
    path: Path,
    title: str = "",
    tags: list[str] | None = None,
    note: str = "",
    unarchive: bool = True,
    kind: str = ARTIFACT,
    project: str = "",
    main: bool = False,
) -> Pin:
    """Register a directory. Idempotent — re-adding an existing path updates it in place.

    `unarchive` decides what re-adding an archived pin means. A person typing
    `pin add` clearly wants it back, so that defaults to True; automated callers
    (`scan`) pass False, otherwise every scan would resurrect pins you archived
    on purpose.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown pin kind: {kind!r}")
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
        kind=kind,
        project=project or (resolved.name if kind == PROJECT else ""),
        main=main,
    )
    for i, existing in enumerate(pins):
        if existing.id == pin.id:
            pin.added = existing.added or pin.added  # keep the original registration time
            pin.archived = False if unarchive else existing.archived
            pins[i] = pin
            break
    else:
        pins.append(pin)
    save_pins(pins)
    return pin


def set_archived(pin_id: str, archived: bool) -> bool:
    """Hide (or restore) a pin without touching the directory or losing the entry."""
    pins = load_pins()
    for pin in pins:
        if pin.id == pin_id:
            pin.archived = archived
            save_pins(pins)
            return True
    return False


def remove_pin(pin_id: str) -> bool:
    """Delete the entry outright. Archiving is the reversible option."""
    pins = load_pins()
    kept = [p for p in pins if p.id != pin_id]
    if len(kept) == len(pins):
        return False
    save_pins(kept)
    return True
