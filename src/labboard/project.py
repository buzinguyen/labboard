"""Auto-pinning from `labboard.toml` manifests.

Pinning is otherwise something a person or an agent has to remember. A manifest moves
that into the repo: the project states where its outputs land, and `labboard scan`
registers them. Committed alongside the code, it stays correct as the project moves.

    # labboard.toml, at a project root
    title = "safe_mjlab_zoo"       # optional prefix for every pin from this file
    tags  = ["mjlab", "safety"]    # optional defaults, merged into each pin

    pins = ["logs", "outputs/eval"]          # shorthand

    [[pin]]                                   # or the long form, for per-pin detail
    path  = "logs/rsl_rl"
    title = "training runs"
    tags  = ["go2"]

Paths are relative to the manifest's own directory; absolute paths are rejected so a
manifest can never reach outside the project it describes.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "labboard.toml"
DEFAULT_MAX_DEPTH = 4

# Directories never worth descending into when hunting for manifests.
SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
        ".cache", "wandb", "checkpoints", ".tox", "dist", "build", ".next",
    }
)


class ManifestError(Exception):
    """A manifest exists but cannot be used. Reported per-file, never fatal to a scan."""


@dataclass
class PinSpec:
    path: Path
    title: str
    tags: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def exists(self) -> bool:
        try:
            return self.path.is_dir()
        except OSError:
            return False


def find_manifests(root: Path, max_depth: int = DEFAULT_MAX_DEPTH) -> list[Path]:
    """Locate `labboard.toml` files under `root`, breadth-first within a depth budget.

    Bounded rather than exhaustive: an unbounded walk of a home directory full of
    datasets and checkpoints is slow enough to feel broken.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    found: list[Path] = []
    frontier = [(root, 0)]
    while frontier:
        current, depth = frontier.pop(0)

        manifest = current / MANIFEST_NAME
        if manifest.is_file():
            found.append(manifest)
            # A manifest describes its whole project; don't recurse into it looking
            # for more. Nested manifests would fight over the same directories.
            continue

        if depth >= max_depth:
            continue
        try:
            for entry in sorted(current.iterdir()):
                if entry.is_dir() and not entry.name.startswith(".") \
                        and entry.name not in SKIP_DIRS:
                    frontier.append((entry, depth + 1))
        except OSError:
            continue  # unreadable directory — skip, don't abort the scan

    return found


def read_manifest(manifest: Path) -> list[PinSpec]:
    """Parse one manifest into pin specs. Raises `ManifestError` on bad input."""
    manifest = Path(manifest)
    base = manifest.parent

    try:
        with manifest.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"{manifest}: {exc}") from exc

    default_tags = [str(t) for t in data.get("tags", [])]
    prefix = str(data.get("title", "")).strip()

    raw: list[dict] = []
    for shorthand in data.get("pins", []):
        if not isinstance(shorthand, str):
            raise ManifestError(f"{manifest}: `pins` must be a list of strings")
        raw.append({"path": shorthand})
    for entry in data.get("pin", []):
        if not isinstance(entry, dict) or "path" not in entry:
            raise ManifestError(f"{manifest}: every [[pin]] needs a `path`")
        raw.append(entry)

    if not raw:
        raise ManifestError(f"{manifest}: declares no pins")

    specs: list[PinSpec] = []
    for entry in raw:
        rel = Path(str(entry["path"]))
        if rel.is_absolute():
            raise ManifestError(f"{manifest}: `{rel}` must be relative to the manifest")

        target = (base / rel).resolve()
        if not target.is_relative_to(base.resolve()):
            raise ManifestError(f"{manifest}: `{rel}` escapes the project directory")

        name = str(entry.get("title", "")).strip() or rel.name or target.name
        specs.append(
            PinSpec(
                path=target,
                title=f"{prefix} · {name}" if prefix else name,
                tags=sorted({*default_tags, *(str(t) for t in entry.get("tags", []))}),
                note=str(entry.get("note", "")),
            )
        )
    return specs


def collect(roots: list[Path], max_depth: int = DEFAULT_MAX_DEPTH) -> tuple[list[PinSpec], list[str]]:
    """Gather pin specs from every manifest under `roots`.

    Returns (specs, problems). A broken manifest is reported, never fatal — one bad
    file must not stop the rest of a scan.
    """
    specs: list[PinSpec] = []
    problems: list[str] = []
    seen: set[Path] = set()

    for root in roots:
        try:
            manifests = find_manifests(Path(root), max_depth)
        except (NotADirectoryError, OSError) as exc:
            problems.append(str(exc))
            continue

        for manifest in manifests:
            try:
                for spec in read_manifest(manifest):
                    if spec.path in seen:
                        continue
                    seen.add(spec.path)
                    specs.append(spec)
            except ManifestError as exc:
                problems.append(str(exc))

    return specs, problems
