"""Cross-device project rollup.

Each node publishes its own projects and tickets at `/api/node`. This module merges
those payloads by project slug into the view the dashboard renders.

The merge encodes one rule: **a project's tickets come from its main device.** Other
devices run experiments for it and publish *receipts*, which show up against the ticket
they name. Nothing is copied and no device writes to another — the aggregation is
read-only and happens per page load, exactly like the pin portal it sits beside.

When the rule is violated — no device claims a project, or two do, or three tickets are
active at once — the rollup carries a warning rather than silently picking a winner.
Those are the situations where a wrong guess would quietly mislead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config, lint, tasks

# A project whose active ticket has not moved in this long is worth flagging.
STALE_DAYS = 7


@dataclass
class ProjectView:
    """One project as seen on a single node."""

    slug: str
    title: str
    main: bool
    pin_id: str
    path: str
    tickets: list[tasks.Task] = field(default_factory=list)
    receipts: list[tasks.Task] = field(default_factory=list)
    problems: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title,
            "main": self.main,
            "pin_id": self.pin_id,
            "path": self.path,
            "tickets": [t.to_dict() for t in self.tickets],
            "receipts": [t.to_dict() for t in self.receipts],
            "problems": [p.to_dict() for p in self.problems],
        }


def local_projects() -> list[ProjectView]:
    """Read every project pin on this machine, with its tickets and receipts."""
    artifacts = config.artifact_pins()
    views: list[ProjectView] = []

    for pin in config.project_pins():
        slug = pin.project or pin.root.name
        tickets, receipts = tasks.load_tasks(pin.root, slug)
        for task in (*tickets, *receipts):
            tasks.attach_links(task, artifacts)
        views.append(
            ProjectView(
                # Checked here rather than at render time: only this node has the pins
                # and the disk needed to tell whether an artifact is actually reachable.
                problems=lint.check(tickets, receipts, artifacts),
                slug=slug,
                title=pin.title or slug,
                main=pin.main,
                pin_id=pin.id,
                path=pin.path,
                tickets=tickets,
                receipts=receipts,
            )
        )

    views.sort(key=lambda v: v.slug.lower())
    return views


@dataclass
class Rollup:
    """One project, merged across every device that carries it."""

    slug: str
    title: str = ""
    main_node: str = ""
    main_url: str = ""
    nodes: list[str] = field(default_factory=list)
    tickets: list[tasks.Task] = field(default_factory=list)
    receipts: list[tasks.Task] = field(default_factory=list)
    artifact_pins: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    problems: list = field(default_factory=list)

    # ---- the one ticket in flight ----

    @property
    def active(self) -> list[tasks.Task]:
        return [t for t in self.tickets if t.status == "active"]

    @property
    def current(self) -> tasks.Task | None:
        """The single ticket being worked. None if the project has none."""
        return self.active[0] if self.active else None

    # ---- liveness ----

    @property
    def counts(self) -> dict:
        out = {s: 0 for s in tasks.STATUSES}
        for ticket in self.tickets:
            out[ticket.status] = out.get(ticket.status, 0) + 1
        return out

    @property
    def live_count(self) -> int:
        return sum(1 for t in self.tickets if t.is_live)

    @property
    def closed_recently(self) -> int:
        """Tickets closed in the last 30 days — needs `closed:` on the ticket."""
        n = 0
        for ticket in self.tickets:
            if not ticket.is_closed:
                continue
            days = tasks.days_since(ticket.closed or ticket.updated)
            if days is not None and days <= tasks.CLOSED_WINDOW_DAYS:
                n += 1
        return n

    @property
    def stale_days(self) -> float | None:
        """Days since the current ticket last moved. None if there is no current one."""
        return self.current.age_days if self.current else None

    @property
    def is_stale(self) -> bool:
        days = self.stale_days
        return days is not None and days >= STALE_DAYS

    @property
    def pending(self) -> list[tasks.Task]:
        """Receipts the main device has not acknowledged yet.

        A ticket acknowledges a receipt by listing its id under `acked:`. Tickets that
        never use `acked:` show every receipt as pending — over-reporting a result is
        the right way to be wrong here, since the alternative is a finished run quietly
        never reaching the ticket.
        """
        acked = {a for t in self.tickets for a in t.acked}
        return [r for r in self.receipts if r.id not in acked]

    @property
    def errors(self) -> list:
        return [p for p in self.problems if p.level == lint.ERROR]

    @property
    def health(self) -> str:
        """Coarse state for the dashboard: what, if anything, needs attention."""
        if self.warnings or self.errors:
            return "warn"
        if self.pending:
            return "pending"
        if any(t.status == "blocked" for t in self.tickets):
            return "blocked"
        if self.is_stale:
            return "stale"
        if self.current:
            return "active"
        return "idle"


def _ticket_sort(task: tasks.Task) -> tuple:
    return (task.order, task.id)


def rollup(nodes: list) -> list[Rollup]:
    """Merge every reachable node's projects into one list, keyed by slug."""
    merged: dict[str, Rollup] = {}
    main_claims: dict[str, list[str]] = {}

    for node in nodes:
        if not node.reachable:
            continue
        for raw in node.projects:
            slug = str(raw.get("slug", "")).strip()
            if not slug:
                continue
            entry = merged.setdefault(slug, Rollup(slug=slug))
            if node.name not in entry.nodes:
                entry.nodes.append(node.name)

            is_main = bool(raw.get("main"))
            if is_main:
                main_claims.setdefault(slug, []).append(node.name)
                entry.main_node = node.name
                entry.main_url = node.url("")
                entry.title = str(raw.get("title") or slug)

            # Tickets are authoritative only on the main device. A facilitator's
            # checkout may hold a stale copy of docs/log/tasks; showing both would
            # invent disagreements that do not matter.
            if is_main:
                entry.tickets = [
                    _locate(tasks.from_dict(t), node) for t in raw.get("tickets", [])
                ]
                entry.problems = [lint.from_dict(p) for p in raw.get("problems", [])]

            # Receipts come from wherever the run happened — that is the point of them.
            entry.receipts.extend(
                _locate(tasks.from_dict(r), node) for r in raw.get("receipts", [])
            )

        # Artifact pins carrying a project slug render beside that project's tickets.
        for pin in node.pins:
            slug = str(pin.get("project", "")).strip()
            if not slug or pin.get("kind") == config.PROJECT or pin.get("archived"):
                continue
            entry = merged.setdefault(slug, Rollup(slug=slug))
            entry.artifact_pins.append(
                {
                    "title": pin.get("title", ""),
                    "node": node.name,
                    "url": node.url(f"/b/{pin.get('id', '')}"),
                    "exists": pin.get("exists", True),
                }
            )

    for slug, entry in merged.items():
        claims = main_claims.get(slug, [])
        if not claims:
            entry.warnings.append(
                "no device claims this project as main, so no tickets are authoritative "
                "— set `main = true` in its labboard.toml on the device that owns it"
            )
            entry.title = entry.title or slug
        elif len(claims) > 1:
            entry.warnings.append(
                f"{len(claims)} devices claim main ({', '.join(sorted(claims))}) — "
                "tickets shown are from the last one polled; clear `main` on the rest"
            )

        if len(entry.active) > 1:
            names = ", ".join(t.id for t in entry.active)
            entry.warnings.append(
                f"{len(entry.active)} tickets are active ({names}) — one project, "
                "one ticket in flight"
            )

        entry.tickets.sort(key=_ticket_sort)
        entry.receipts.sort(key=lambda r: (r.updated, r.id), reverse=True)

    # Projects needing attention first, then alphabetically.
    order = {"warn": 0, "pending": 1, "blocked": 2, "stale": 3, "active": 4, "idle": 5}
    return sorted(
        merged.values(), key=lambda r: (order.get(r.health, 9), r.slug.lower())
    )


def _locate(task: tasks.Task, node) -> tasks.Task:
    """Stamp a task with the node it came from, and absolutize its artifact links."""
    task.node = node.name
    task.node_url = node.url("")
    task.artifact_links = [
        {**link, "url": node.url(link["url"]) if link.get("url") else None}
        for link in task.artifact_links
    ]
    task.refs = [
        {**ref, "url": node.url(ref["url"])} for ref in task.refs if ref.get("url")
    ]
    return task


def totals(rollups: list[Rollup]) -> dict:
    """Headline numbers across every project — the top strip of the dashboard."""
    counts = {s: 0 for s in tasks.STATUSES}
    for entry in rollups:
        for status, n in entry.counts.items():
            counts[status] = counts.get(status, 0) + n
    return {
        "projects": len(rollups),
        "counts": counts,
        "live": sum(entry.live_count for entry in rollups),
        "closed_recently": sum(entry.closed_recently for entry in rollups),
        "pending": sum(len(entry.pending) for entry in rollups),
        "stale": sum(1 for entry in rollups if entry.is_stale),
        "needs_attention": sum(1 for entry in rollups if entry.warnings or entry.errors),
        "problems": sum(len(entry.problems) for entry in rollups),
    }
