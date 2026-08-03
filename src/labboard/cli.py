"""Command line interface.

`labboard pin add` is the agent-facing half: a session that just produced artifacts
registers the directory itself, so the board fills in without manual curation. It
writes only to this machine's own pins.toml — no network, no credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, media, project, tasks
from .browse import human_size

DEFAULT_PORT = 8765


def _cmd_serve(args) -> int:
    import uvicorn

    if args.reload:
        # The reloader needs an import string, so pass the port via the environment.
        import os

        os.environ["LABBOARD_PORT"] = str(args.port)
        uvicorn.run(
            "labboard.__main__:app", host=args.host, port=args.port, reload=True, factory=False
        )
    else:
        from .app import create_app

        uvicorn.run(create_app(self_port=args.port), host=args.host, port=args.port)
    return 0


def _cmd_pin_add(args) -> int:
    path = Path(args.path).expanduser()
    is_project = args.project is not None
    kind = config.PROJECT if is_project else config.ARTIFACT
    slug = (args.project or "") if is_project else (args.belongs_to or "")
    try:
        pin = config.add_pin(
            path,
            title=args.title or "",
            tags=[t.strip() for t in (args.tags or "").split(",") if t.strip()],
            note=args.note or "",
            kind=kind,
            project=slug,
            main=bool(args.main),
        )
    except (NotADirectoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    label = "project" if pin.is_project else "pinned"
    print(f"{label} {pin.id}  {pin.title}\n        {pin.path}")
    if pin.is_project:
        role = "main — this device owns its tickets" if pin.main else "facilitator"
        print(f"        project {pin.project} · {role}")
        tdir = tasks.tasks_dir(pin.root)
        if not tdir.is_dir():
            print(f"        note: {tdir} does not exist yet — tickets go there")
    return 0


def _cmd_pin_list(args) -> int:
    pins = config.load_pins()
    if not args.all:
        pins = [p for p in pins if not p.archived]

    if args.json:
        print(json.dumps([{**p.to_dict(), "exists": p.exists} for p in pins], indent=2))
        return 0
    if not pins:
        print("no pins")
        return 0

    width = max(len(p.title) for p in pins)
    for pin in pins:
        flag = "-" if pin.archived else (" " if pin.exists else "!")
        kind = "P" if pin.is_project else " "
        print(f"{flag}{kind} {pin.id}  {pin.title:<{width}}  {pin.path}")

    notes = []
    if any(not p.exists for p in pins):
        notes.append("! = pinned directory is missing or unreadable")
    if any(p.is_project for p in pins):
        notes.append("P = project pin (tickets only; no files are served)")
    if args.all and any(p.archived for p in pins):
        notes.append("- = archived")
    if notes:
        print("\n" + "\n".join(notes), file=sys.stderr)
    return 0


def _resolve_id(target: str) -> str | None:
    """Accept either a pin id or a path — agents often only know the path."""
    if any(p.id == target for p in config.load_pins()):
        return target
    candidate = Path(target).expanduser()
    if candidate.exists():
        pin_id = config.make_id(candidate.resolve())
        if any(p.id == pin_id for p in config.load_pins()):
            return pin_id
    return None


def _cmd_pin_archive(args) -> int:
    pin_id = _resolve_id(args.id_or_path)
    if pin_id is None or not config.set_archived(pin_id, not args.restore):
        print(f"error: no pin matching {args.id_or_path!r}", file=sys.stderr)
        return 1
    print(f"{'restored' if args.restore else 'archived'} {pin_id}")
    return 0


def _cmd_scan(args) -> int:
    specs, problems = project.collect([Path(r) for r in args.roots], max_depth=args.depth)

    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)

    if not specs:
        print(f"no {project.MANIFEST_NAME} manifests found")
        return 0 if not problems else 1

    added = skipped = 0
    for spec in specs:
        if not spec.exists:
            print(f"  skip  {spec.path}  (declared but does not exist)")
            skipped += 1
            continue
        label = f"project {spec.project}" if spec.kind == "project" else spec.title
        if args.dry_run:
            print(f"  would pin  {label}  {spec.path}")
        else:
            # unarchive=False: a scan must not resurrect pins deliberately archived.
            config.add_pin(spec.path, title=spec.title, tags=spec.tags, note=spec.note,
                           unarchive=False, kind=spec.kind, project=spec.project,
                           main=spec.main)
            print(f"  pinned  {label}  {spec.path}")
        added += 1

    verb = "would pin" if args.dry_run else "pinned"
    print(f"\n{verb} {added}, skipped {skipped}")
    return 0


def _cmd_pin_rm(args) -> int:
    target = args.id_or_path
    if config.remove_pin(target):
        print(f"removed {target}")
        return 0

    # Accept a path as well as an id — friendlier for agents that only know the path.
    candidate = Path(target).expanduser()
    if candidate.exists() and config.remove_pin(config.make_id(candidate.resolve())):
        print(f"removed pin for {candidate.resolve()}")
        return 0

    print(f"error: no pin matching {target!r}", file=sys.stderr)
    return 1


def _cmd_tasks(args) -> int:
    """This machine's tickets. Local only — no network, no other devices."""
    from . import board, lint

    views = [v for v in board.local_projects() if not args.project or v.slug == args.project]
    if args.json:
        print(json.dumps([v.to_dict() for v in views], indent=2))
        return 0
    if not views:
        print("no project pins on this machine"
              if not args.project else f"no project pinned as {args.project!r}")
        return 1

    errors = warnings = 0

    for view in views:
        role = "main" if view.main else "facilitator"
        print(f"{view.slug}  ({role})  {view.path}")
        if not view.tickets and not view.receipts:
            print(f"    no tickets — write one to {tasks.tasks_dir(Path(view.path))}")
        for ticket in view.tickets:
            runs = f"  runs {', '.join(ticket.runs)}" if ticket.runs else ""
            print(f"    {ticket.status:<8} {ticket.id:<8} {ticket.title}{runs}")
        for receipt in view.receipts:
            print(f"    outbox   {receipt.id:<8} {receipt.title}  → {receipt.task or '?'}")

        n_err, n_warn = lint.summarize(view.problems)
        errors += n_err
        warnings += n_warn

        if args.check and view.problems:
            print()
            for problem in view.problems:
                mark = "ERROR" if problem.level == lint.ERROR else "warn "
                where = f"{problem.where}: " if problem.where else ""
                # stdout, not stderr: under --check these lines ARE the output, and
                # splitting streams interleaves them arbitrarily with the listing.
                print(f"    {mark}  {where}{problem.message}")
        elif view.problems:
            print(f"    ({n_err} error{'' if n_err == 1 else 's'}, "
                  f"{n_warn} warning{'' if n_warn == 1 else 's'} — run with --check)")
        print()

    if not args.check:
        return 0

    if errors or warnings:
        print(f"{errors} error(s), {warnings} warning(s)")
    else:
        print("all tickets well-formed")
    # Errors fail the command so a hook or a session-end check can act on it;
    # warnings are advisory and must not block finishing work.
    return 1 if errors else 0


def _cmd_inbox(args) -> int:
    """Results other devices reported for a project, so the main device can fold them in.

    This is the whole cross-device handoff: the facilitator wrote a receipt to its own
    disk, and this pulls that text over HTTP. No files move, and nothing is written on
    either side — the agent reading this output is what updates the ticket.
    """
    import asyncio

    from . import board, tailnet

    nodes = asyncio.run(tailnet.gather(self_port=args.port))
    rollups = [r for r in board.rollup(nodes) if not args.project or r.slug == args.project]

    if not rollups:
        known = ", ".join(sorted(r.slug for r in board.rollup(nodes))) or "none"
        print(f"error: no project {args.project!r} on the tailnet (known: {known})",
              file=sys.stderr)
        return 1

    pending = [(r, rec) for r in rollups for rec in (r.receipts if args.all else r.pending)]

    if args.json:
        print(json.dumps(
            [{**rec.to_dict(), "project": r.slug, "node": rec.node} for r, rec in pending],
            indent=2,
        ))
        return 0

    if not pending:
        print("nothing waiting" + ("" if args.all else " (use --all to include acknowledged)"))
        return 0

    for entry, rec in pending:
        print(f"--- {entry.slug} · {rec.id} · from {rec.node}"
              f"{' · ' + rec.run if rec.run else ''}"
              f"{' → ' + rec.task if rec.task else ''}")
        if rec.title:
            print(f"    {rec.title}")
        for link in rec.artifact_links:
            print(f"    artifacts: {link['label']}" + (f"  {link['url']}" if link.get("url")
                  else "  (not under any pin — not viewable)"))
        if rec.body.strip():
            for line in rec.body.strip().splitlines():
                print(f"    {line}")
        print()

    print(f"{len(pending)} waiting. Fold each into its ticket, then add its id to the "
          f"ticket's `acked:` list so it stops showing as pending.", file=sys.stderr)
    return 0


def _cmd_cache(args) -> int:
    if args.clear:
        media.clear_cache()
        print("cache cleared")
        return 0
    size, count = media.cache_usage()
    print(f"{human_size(size)} across {count} files at {media.cache_root()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="labboard", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web service")
    serve.add_argument("--host", default="127.0.0.1",
                       help="bind address (default 127.0.0.1; expose via `tailscale serve`)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--reload", action="store_true", help="auto-reload for development")
    serve.set_defaults(func=_cmd_serve)

    pin = sub.add_parser("pin", help="manage pinned directories")
    pin_sub = pin.add_subparsers(dest="pin_command", required=True)

    add = pin_sub.add_parser("add", help="register a directory (idempotent)")
    add.add_argument("path")
    add.add_argument("--title", help="display name (defaults to the directory name)")
    add.add_argument("--tags", help="comma-separated")
    add.add_argument("--note", help="one-line description")
    add.add_argument(
        "--project", metavar="SLUG", nargs="?", const="",
        help="register a PROJECT pin: read tickets from docs/log/tasks, serve no files. "
             "SLUG joins this repo to itself on other devices (default: directory name)",
    )
    add.add_argument(
        "--main", action="store_true",
        help="this device owns the project's tickets (exactly one device should)",
    )
    add.add_argument(
        "--belongs-to", metavar="SLUG", dest="belongs_to",
        help="artifact pins: which project's outputs these are, so the dashboard "
             "can show them beside its tickets",
    )
    add.set_defaults(func=_cmd_pin_add)

    listing = pin_sub.add_parser("list", help="list pins")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--all", action="store_true", help="include archived pins")
    listing.set_defaults(func=_cmd_pin_list)

    archive = pin_sub.add_parser("archive", help="hide a pin without deleting it")
    archive.add_argument("id_or_path")
    archive.set_defaults(func=_cmd_pin_archive, restore=False)

    restore = pin_sub.add_parser("restore", help="bring an archived pin back")
    restore.add_argument("id_or_path")
    restore.set_defaults(func=_cmd_pin_archive, restore=True)

    remove = pin_sub.add_parser("rm", help="delete a pin permanently (prefer `archive`)")
    remove.add_argument("id_or_path")
    remove.set_defaults(func=_cmd_pin_rm)

    scan = sub.add_parser(
        "scan",
        help=f"register pins declared in {project.MANIFEST_NAME} manifests",
        description=project.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scan.add_argument("roots", nargs="+", help="directories to search for manifests")
    scan.add_argument("--depth", type=int, default=project.DEFAULT_MAX_DEPTH,
                      help=f"how deep to search (default {project.DEFAULT_MAX_DEPTH})")
    scan.add_argument("--dry-run", action="store_true", help="show what would be pinned")
    scan.set_defaults(func=_cmd_scan)

    tasks_cmd = sub.add_parser(
        "tasks",
        help="this machine's tickets (local; no network)",
        description=tasks.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    tasks_cmd.add_argument("project", nargs="?", help="limit to one project slug")
    tasks_cmd.add_argument("--json", action="store_true")
    tasks_cmd.add_argument(
        "--check", action="store_true",
        help="validate every ticket and list what is wrong; exits non-zero on errors. "
             "Run this before ending a session that touched a ticket.",
    )
    tasks_cmd.set_defaults(func=_cmd_tasks)

    inbox = sub.add_parser(
        "inbox",
        help="results other devices reported for a project, to fold into its tickets",
    )
    inbox.add_argument("project", nargs="?", help="project slug (default: all projects)")
    inbox.add_argument("--all", action="store_true",
                       help="include receipts already acknowledged via `acked:`")
    inbox.add_argument("--json", action="store_true")
    inbox.add_argument("--port", type=int, default=DEFAULT_PORT,
                       help="local labboard port, for querying this node without TLS")
    inbox.set_defaults(func=_cmd_inbox)

    cache = sub.add_parser("cache", help="inspect or clear derived thumbnails/transcodes")
    cache.add_argument("--clear", action="store_true")
    cache.set_defaults(func=_cmd_cache)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
