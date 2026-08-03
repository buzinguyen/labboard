"""Ticket validation.

labboard cannot stop an agent from writing a bad ticket — it never writes to a project
repo, and it has no hook into an agent's session. What it can do is make a bad ticket
*visible*: the same checks run from `labboard tasks --check`, which an agent runs before
it finishes, and on the project page, where Buzi sees anything the agent skipped.

So the enforcement is: convention in the skill, a command that fails loudly, and a board
that shows the gap. Deliberately not a schema that refuses to load the file — a ticket
with a missing `updated:` is still worth reading, and dropping it would hide the very
problem being reported.

Errors are things an agent got wrong and can fix from the ticket alone. Warnings are
things that need a judgement call, usually about whether a result is actually reachable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import tasks

ERROR = "error"
WARN = "warn"

# `## Results`, `### results`, `## Done when` — the sections the convention asks for.
def _has_heading(body: str, name: str) -> bool:
    return re.search(rf"^\s*#{{1,4}}\s*{name}", body or "", re.MULTILINE | re.IGNORECASE) is not None


@dataclass
class Problem:
    level: str
    where: str
    message: str

    def to_dict(self) -> dict:
        return {"level": self.level, "where": self.where, "message": self.message}


def from_dict(raw: dict) -> Problem:
    return Problem(
        level=str(raw.get("level", WARN)),
        where=str(raw.get("where", "")),
        message=str(raw.get("message", "")),
    )


def check(tickets: list, receipts: list, pins: list | None = None) -> list[Problem]:
    """Validate one project's tickets. Returns problems, most severe first."""
    problems: list[Problem] = []
    pins = pins or []

    def err(where: str, message: str) -> None:
        problems.append(Problem(ERROR, where, message))

    def warn(where: str, message: str) -> None:
        problems.append(Problem(WARN, where, message))

    # ---- project-wide ----

    active = [t for t in tickets if t.status == "active"]
    if len(active) > 1:
        err(
            ", ".join(t.id for t in active),
            f"{len(active)} tickets are active — one project works one ticket at a time; "
            "put the rest back to `open`",
        )

    seen: dict[str, str] = {}
    for ticket in tickets:
        if ticket.id in seen:
            err(ticket.id, f"duplicate id, also used by {seen[ticket.id]}")
        else:
            seen[ticket.id] = ticket.filename

    if tickets and not active and any(t.is_live for t in tickets):
        warn("", "no ticket is active — promote one from `open` so the board shows work")

    # ---- per ticket ----

    for ticket in tickets:
        where = ticket.id

        if ticket.status not in tasks.STATUSES:
            err(where, f"unknown status {ticket.status!r}")

        if not ticket.updated:
            err(where, "no `updated:` — the board cannot tell whether this has gone quiet")
        elif tasks.days_since(ticket.updated) is None:
            err(where, f"`updated: {ticket.updated}` is not an ISO date (YYYY-MM-DD)")
        elif tasks.days_since(ticket.updated) < 0:
            err(where, f"`updated: {ticket.updated}` is in the future")

        if ticket.is_closed and not ticket.closed:
            err(where, f"status is `{ticket.status}` but `closed:` is empty")
        if ticket.closed and not ticket.is_closed:
            warn(where, f"has `closed: {ticket.closed}` but status is `{ticket.status}`")

        if ticket.filename and not ticket.filename.startswith(ticket.id):
            warn(where, f"id does not match filename {ticket.filename}")

        if not (ticket.body or "").strip():
            warn(where, "empty body — state the question and what would settle it")
        else:
            if ticket.is_live and not _has_heading(ticket.body, "Done when"):
                warn(where, "no `## Done when` — without it nobody can tell when to close it")
            if ticket.status == "done" and not _has_heading(ticket.body, "Results"):
                warn(where, "closed as done with no `## Results` section")

        if ticket.status == "done" and not ticket.runs:
            warn(where, "done with no `runs:` — which experiment answered it?")

        for raw, link in zip(ticket.artifacts, ticket.artifact_links or []):
            if not link.get("url"):
                warn(
                    where,
                    f"`{raw}` is not under any artifact pin, so the result is not viewable "
                    "from the board — pin its output root",
                )
            elif not Path(raw).expanduser().exists():
                warn(where, f"`{raw}` is listed in `artifacts:` but does not exist on disk")

    # ---- receipts ----

    known = {t.id for t in tickets}
    for receipt in receipts:
        where = receipt.id
        if not receipt.task:
            err(where, "receipt names no `task:` — it cannot be folded into anything")
        elif receipt.task not in known:
            err(where, f"receipt targets `{receipt.task}`, which is not a ticket here")
        if not receipt.run:
            warn(where, "receipt names no `run:` — which experiment produced this?")

    problems.sort(key=lambda p: (p.level != ERROR, p.where))
    return problems


def summarize(problems: list[Problem]) -> tuple[int, int]:
    """(errors, warnings)."""
    return (
        sum(1 for p in problems if p.level == ERROR),
        sum(1 for p in problems if p.level == WARN),
    )
