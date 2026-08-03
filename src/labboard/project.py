"""Auto-pinning from `labboard.toml` manifests.

Pinning is otherwise something a person or an agent has to remember. A manifest moves
that into the repo: the project states where its outputs land, and `labboard scan`
registers them. Committed alongside the code, it stays correct as the project moves.

    # labboard.toml, at a project root
    project = "mjlab-go2"          # slug joining this repo to itself on other devices
    main    = "ws-3"               # HOSTNAME of the device that owns the tickets

    title = "safe_mjlab_zoo"       # optional prefix for every pin from this file
    tags  = ["mjlab", "safety"]    # optional defaults, merged into each pin

    pins = ["logs", "outputs/eval"]          # shorthand

    [[pin]]                                   # or the long form, for per-pin detail
    path  = "logs/rsl_rl"
    title = "training runs"
    tags  = ["go2"]

`project` creates a *project pin* on the manifest's own directory — a pin that serves no
bytes and exists only so labboard can read `docs/log/tasks/`. `pins` create the usual
*artifact pins*. A manifest may declare either or both.

`main` is the one bit of cross-device coordination in the system, and it names a
*hostname* rather than being a boolean. The manifest is committed, so the same file is
read on every device that checks the repo out; a boolean would make all of them claim
ownership at once. Naming the host means one committed line stays correct everywhere —
the device it names owns the tickets, and the rest are facilitators that run experiments
and report back with receipts.

Paths are relative to the manifest's own directory; absolute paths are rejected so a
manifest can never reach outside the project it describes.
"""

from __future__ import annotations

import socket
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
    kind: str = "artifact"
    project: str = ""
    main: bool = False

    @property
    def exists(self) -> bool:
        try:
            return self.path.is_dir()
        except OSError:
            return False


@dataclass
class Manifest:
    """One parsed `labboard.toml`: its artifact pins, plus its project pin if declared."""

    pins: list[PinSpec] = field(default_factory=list)
    project: PinSpec | None = None

    @property
    def specs(self) -> list[PinSpec]:
        """Everything this manifest registers, project pin first."""
        return ([self.project] if self.project else []) + self.pins


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


def _is_main_here(declared, hostname: str) -> bool:
    """Does this manifest's `main` name the machine we are running on?

    A hostname is the committed, per-device-correct form. `true` is still honoured for a
    manifest that is never shared, and `false`/absent means facilitator.
    """
    if isinstance(declared, bool):
        return declared
    if not isinstance(declared, str) or not declared.strip():
        return False
    want = declared.strip().lower()
    have = hostname.lower()
    # Match the bare host as well as an FQDN on either side, so `main = "ws-3"` works
    # on a box whose hostname is `ws-3.local`.
    return want == have or want.split(".")[0] == have.split(".")[0]


def read_manifest(manifest: Path, hostname: str | None = None) -> Manifest:
    """Parse one manifest. Raises `ManifestError` on bad input."""
    manifest = Path(manifest)
    base = manifest.parent
    hostname = hostname if hostname is not None else socket.gethostname()

    try:
        with manifest.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"{manifest}: {exc}") from exc

    default_tags = [str(t) for t in data.get("tags", [])]
    prefix = str(data.get("title", "")).strip()

    slug = str(data.get("project", "")).strip()
    if slug and ("/" in slug or slug.startswith(".")):
        # The slug is a join key rendered into pages and passed to `labboard inbox`;
        # keep it a plain identifier rather than something path-shaped.
        raise ManifestError(f"{manifest}: `project` must be a plain slug, not a path")

    project_spec = None
    if slug:
        project_spec = PinSpec(
            path=base.resolve(),
            title=prefix or slug,
            tags=default_tags,
            kind="project",
            project=slug,
            main=_is_main_here(data.get("main"), hostname),
        )

    raw: list[dict] = []
    for shorthand in data.get("pins", []):
        if not isinstance(shorthand, str):
            raise ManifestError(f"{manifest}: `pins` must be a list of strings")
        raw.append({"path": shorthand})
    for entry in data.get("pin", []):
        if not isinstance(entry, dict) or "path" not in entry:
            raise ManifestError(f"{manifest}: every [[pin]] needs a `path`")
        raw.append(entry)

    if not raw and project_spec is None:
        raise ManifestError(f"{manifest}: declares neither `project` nor any pins")

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
                # An artifact pin declared by a project's manifest still belongs to
                # that project — that is what lets the dashboard show a project's
                # outputs alongside its tickets.
                project=slug,
            )
        )
    return Manifest(pins=specs, project=project_spec)


def collect(
    roots: list[Path],
    max_depth: int = DEFAULT_MAX_DEPTH,
    hostname: str | None = None,
) -> tuple[list[PinSpec], list[str]]:
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
                for spec in read_manifest(manifest, hostname).specs:
                    if spec.path in seen:
                        continue
                    seen.add(spec.path)
                    specs.append(spec)
            except ManifestError as exc:
                problems.append(str(exc))

    return specs, problems
