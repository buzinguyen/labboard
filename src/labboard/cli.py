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

from . import config, media, project
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
    try:
        pin = config.add_pin(
            path,
            title=args.title or "",
            tags=[t.strip() for t in (args.tags or "").split(",") if t.strip()],
            note=args.note or "",
        )
    except NotADirectoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"pinned {pin.id}  {pin.title}\n        {pin.path}")
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
        print(f"{flag} {pin.id}  {pin.title:<{width}}  {pin.path}")

    notes = []
    if any(not p.exists for p in pins):
        notes.append("! = pinned directory is missing or unreadable")
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
        if args.dry_run:
            print(f"  would pin  {spec.title}  {spec.path}")
        else:
            # unarchive=False: a scan must not resurrect pins deliberately archived.
            config.add_pin(spec.path, title=spec.title, tags=spec.tags, note=spec.note,
                           unarchive=False)
            print(f"  pinned  {spec.title}  {spec.path}")
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

    cache = sub.add_parser("cache", help="inspect or clear derived thumbnails/transcodes")
    cache.add_argument("--clear", action="store_true")
    cache.set_defaults(func=_cmd_cache)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
