"""Project tickets — the shared representation between Buzi and the agents.

A *ticket* is one markdown file with frontmatter, living in the project repo at
`docs/log/tasks/`. That location is deliberate: it is the directory the `project-log`
skill already owns, it is already in `.git/info/exclude`, so planning notes can never
reach a shared remote, and it puts a ticket next to the `experiments.md` rows that
answer it.

    ---
    id: T007
    title: Does halving action scale fix the loiter optimum?
    status: active                      # open | active | blocked | done | dropped
    tags: [go2, reward]
    runs: [E014, E015]                  # rows in experiments.md
    artifacts: [~/artifacts/go2/E014]   # resolved against this node's artifact pins
    updated: 2026-08-03
    ---
    ## Question
    ...

One file per ticket, so an agent updating a status writes a single file and can never
clobber a sibling's edit.

**labboard never writes here.** Tickets are written by agents and by Buzi; this module
only reads. That keeps the service read-only against project repos, which is the whole
reason a project pin can be granted without granting access to the source tree.

Receipts
--------
A device that is not a project's main device still runs experiments for it. It reports
back by dropping a *receipt* in `docs/log/tasks/outbox/` — same file shape, `kind:
receipt`, naming the ticket it belongs to. Receipts travel to the main device the same
way everything else in labboard travels: not at all. The board reads them over HTTP
from each node, and `labboard inbox <project>` hands them to the main device's agent so
it can fold the result into the ticket and close it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

# Where tickets live inside a project pin. A constant, never user input — which is why
# nothing in this module needs to go through the path guard.
TASKS_REL = ("docs", "log", "tasks")
OUTBOX_NAME = "outbox"

# Lifecycle. `open` is queued, `active` is the one being worked, `blocked` is waiting on
# something external, and the last two are terminal.
STATUSES = ("open", "active", "blocked", "done", "dropped")
LIVE_STATUSES = ("open", "active", "blocked")
CLOSED_STATUSES = ("done", "dropped")

# Display order: what needs attention first.
STATUS_ORDER = {s: i for i, s in enumerate(("active", "blocked", "open", "done", "dropped"))}

# Bounds. A project with a thousand ticket files is a bug, not a workload, and a
# multi-megabyte "ticket" is something else that wandered into the directory.
MAX_TASKS = 500
MAX_TASK_BYTES = 256 * 1024
# How much ticket body crosses the wire to the portal. Bodies are prose; this is
# generous for a real ticket and stops one runaway file from bloating every board load.
MAX_BODY_CHARS = 8000

CLOSED_WINDOW_DAYS = 30


# ---- frontmatter -------------------------------------------------------------------
#
# A deliberately small parser rather than a YAML dependency. The schema is fixed and
# agent-written, so the useful failure mode is "this key was ignored", not "the file
# half-parsed into something surprising". Supports scalars, `[a, b]` inline lists, and
# `- item` block lists. Nothing nested.


def _scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_block(lines: list[str]) -> dict:
    meta: dict = {}
    key: str | None = None
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Continuation of a block list started by a bare `key:` on a previous line.
        if stripped.startswith("- ") and key is not None and isinstance(meta.get(key), list):
            meta[key].append(_scalar(stripped[2:]))
            continue

        if ":" not in raw:
            continue
        name, _, value = raw.partition(":")
        key = name.strip()
        value = value.strip()

        if not value:
            # Either an empty scalar or the head of a block list; treat as a list and
            # let the accessors coerce an empty one back to "".
            meta[key] = []
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            meta[key] = [_scalar(p) for p in inner.split(",") if p.strip()] if inner else []
        else:
            meta[key] = _scalar(value)
    return meta


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split `---` frontmatter from the markdown body.

    A file with no frontmatter, or an unterminated block, is all body — a malformed
    ticket should still be readable rather than vanishing from the board.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text

    return _parse_block(lines[1:end]), "\n".join(lines[end + 1:]).lstrip("\n")


def _as_str(meta: dict, key: str, default: str = "") -> str:
    value = meta.get(key, default)
    if isinstance(value, list):
        return value[0] if value else default
    return str(value)


def _as_list(meta: dict, key: str) -> list[str]:
    value = meta.get(key, [])
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()] if value else []


def days_since(stamp: str) -> float | None:
    """Whole days since an ISO date. Tolerates a full timestamp; None if unparseable."""
    if not stamp:
        return None
    try:
        when = date.fromisoformat(stamp[:10])
    except ValueError:
        return None
    return (datetime.now(timezone.utc).date() - when).days


# ---- tickets -----------------------------------------------------------------------


@dataclass
class Task:
    id: str
    title: str
    status: str
    project: str = ""
    tags: list[str] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    # Receipt ids this ticket has absorbed. Absent means "none yet", which makes
    # every receipt read as pending — the safe direction to be wrong in.
    acked: list[str] = field(default_factory=list)
    updated: str = ""
    closed: str = ""
    body: str = ""
    filename: str = ""
    kind: str = "task"
    # Receipts only: which ticket and which experiment this result belongs to.
    task: str = ""
    run: str = ""
    # Filled in by the aggregator, not by the file.
    node: str = ""
    node_url: str = ""
    artifact_links: list[dict] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED_STATUSES

    @property
    def age_days(self) -> float | None:
        return days_since(self.updated)

    @property
    def order(self) -> int:
        return STATUS_ORDER.get(self.status, len(STATUS_ORDER))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "project": self.project,
            "tags": self.tags,
            "runs": self.runs,
            "artifacts": self.artifacts,
            "acked": self.acked,
            "updated": self.updated,
            "closed": self.closed,
            "body": self.body[:MAX_BODY_CHARS],
            "filename": self.filename,
            "kind": self.kind,
            "task": self.task,
            "run": self.run,
            "artifact_links": self.artifact_links,
        }


def from_dict(raw: dict) -> Task:
    """Rebuild a Task from an `/api/node` payload. Unknown keys are ignored."""
    return Task(
        id=str(raw.get("id", "")),
        title=str(raw.get("title", "")),
        status=str(raw.get("status", "open")),
        project=str(raw.get("project", "")),
        tags=[str(t) for t in raw.get("tags", [])],
        runs=[str(r) for r in raw.get("runs", [])],
        artifacts=[str(a) for a in raw.get("artifacts", [])],
        acked=[str(a) for a in raw.get("acked", [])],
        updated=str(raw.get("updated", "")),
        closed=str(raw.get("closed", "")),
        body=str(raw.get("body", "")),
        filename=str(raw.get("filename", "")),
        kind=str(raw.get("kind", "task")),
        task=str(raw.get("task", "")),
        run=str(raw.get("run", "")),
        artifact_links=[d for d in raw.get("artifact_links", []) if isinstance(d, dict)],
    )


def _parse_task(path: Path, default_project: str, kind: str) -> Task | None:
    try:
        if path.stat().st_size > MAX_TASK_BYTES:
            return None
        text = path.read_text(errors="replace")
    except OSError:
        return None

    meta, body = parse_frontmatter(text)

    status = _as_str(meta, "status", "open").lower()
    if status not in STATUSES:
        status = "open"

    # A receipt records a finished run by definition; anything else is a ticket.
    declared_kind = _as_str(meta, "kind", "").lower()
    if declared_kind in ("task", "ticket", "receipt"):
        kind = "receipt" if declared_kind == "receipt" else "task"

    return Task(
        id=_as_str(meta, "id") or path.stem,
        title=_as_str(meta, "title") or path.stem,
        status=status,
        project=_as_str(meta, "project") or default_project,
        tags=_as_list(meta, "tags"),
        runs=_as_list(meta, "runs") or _as_list(meta, "run"),
        artifacts=_as_list(meta, "artifacts"),
        acked=_as_list(meta, "acked"),
        updated=_as_str(meta, "updated"),
        closed=_as_str(meta, "closed"),
        body=body,
        filename=path.name,
        kind=kind,
        task=_as_str(meta, "task"),
        run=_as_str(meta, "run"),
    )


def _safe_children(directory: Path, root: Path) -> list[Path]:
    """Markdown files directly in `directory`, refusing anything that escapes `root`.

    The directory path is a constant, but its *contents* are not: `docs/log/tasks` could
    itself be a symlink, or hold links pointing outside the repo. Cheap to check, and it
    keeps this module's posture consistent with the rest of labboard.
    """
    try:
        real_root = root.resolve()
        real_dir = directory.resolve()
    except OSError:
        return []
    if real_dir != real_root and not real_dir.is_relative_to(real_root):
        return []
    if not real_dir.is_dir():
        return []

    out: list[Path] = []
    try:
        for entry in sorted(real_dir.iterdir()):
            if len(out) >= MAX_TASKS:
                break
            if entry.name.startswith(".") or entry.suffix.lower() != ".md":
                continue
            try:
                if not entry.is_file() or not entry.resolve().is_relative_to(real_root):
                    continue
            except OSError:
                continue
            out.append(entry)
    except OSError:
        return []
    return out


def tasks_dir(root: Path) -> Path:
    return root.joinpath(*TASKS_REL)


def load_tasks(root: Path, project: str = "") -> tuple[list[Task], list[Task]]:
    """Read one project's tickets and receipts. Returns (tickets, receipts)."""
    base = tasks_dir(root)

    tickets = [
        t for t in (_parse_task(p, project, "task") for p in _safe_children(base, root)) if t
    ]
    receipts = [
        t
        for t in (
            _parse_task(p, project, "receipt")
            for p in _safe_children(base / OUTBOX_NAME, root)
        )
        if t
    ]

    tickets.sort(key=lambda t: (t.order, t.id))
    receipts.sort(key=lambda t: (t.updated, t.id), reverse=True)
    return tickets, receipts


# ---- linking results back --------------------------------------------------------


def artifact_url(raw: str, pins: list) -> str | None:
    """Map a path from a ticket's `artifacts:` onto a browse URL, if a pin covers it.

    Returns a path relative to the owning node (`/b/<pin>/<rel>`); the caller prefixes
    the node's base URL. Unpinned paths return None and render as plain text — a
    result that was never pinned genuinely is not viewable, and saying so is better
    than emitting a link that 404s.
    """
    try:
        target = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None

    best: tuple[int, str] | None = None
    for pin in pins:
        if getattr(pin, "kind", "artifact") != "artifact" or getattr(pin, "archived", False):
            continue
        try:
            root = pin.root
        except OSError:
            continue
        if target != root and not target.is_relative_to(root):
            continue
        rel = "" if target == root else str(target.relative_to(root))
        url = f"/b/{pin.id}/{rel}" if rel else f"/b/{pin.id}"
        # Most specific pin wins, so a run dir nested inside a pinned output root
        # links through the pin that actually contains it.
        depth = len(root.parts)
        if best is None or depth > best[0]:
            best = (depth, url)
    return best[1] if best else None


def attach_links(task: Task, pins: list) -> Task:
    task.artifact_links = [
        {"label": raw, "url": artifact_url(raw, pins)} for raw in task.artifacts
    ]
    return task
