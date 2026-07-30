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

from . import config, media
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
    if args.json:
        print(json.dumps([{**p.to_dict(), "exists": p.exists} for p in pins], indent=2))
        return 0
    if not pins:
        print("no pins")
        return 0
    width = max(len(p.title) for p in pins)
    for pin in pins:
        flag = " " if pin.exists else "!"
        print(f"{flag} {pin.id}  {pin.title:<{width}}  {pin.path}")
    if any(not p.exists for p in pins):
        print("\n! = pinned directory is missing or unreadable", file=sys.stderr)
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
    listing.set_defaults(func=_cmd_pin_list)

    remove = pin_sub.add_parser("rm", help="remove a pin (leaves the directory alone)")
    remove.add_argument("id_or_path")
    remove.set_defaults(func=_cmd_pin_rm)

    cache = sub.add_parser("cache", help="inspect or clear derived thumbnails/transcodes")
    cache.add_argument("--clear", action="store_true")
    cache.set_defaults(func=_cmd_cache)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
