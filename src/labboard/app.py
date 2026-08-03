"""HTTP routes.

Every route that touches disk goes through `_target()`, which is the single call site
of the path guard. Nothing here builds a filesystem path from user input any other way.

The service is read-only: there is no upload, delete, move, or write route. The only
mutating endpoints manage pins, which are entries in a local config file.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import __version__, board, browse, config, media, organize, render, tailnet, tasks
from .safety import AccessDenied, PinUnavailable, resolve

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["human_size"] = browse.human_size
TEMPLATES.env.filters["human_age"] = browse.human_age

HOSTNAME = socket.gethostname()


def create_app(self_port: int = 8765) -> FastAPI:
    app = FastAPI(title="labboard", docs_url=None, redoc_url=None)

    # ---- helpers ---------------------------------------------------------------

    def _pin(pin_id: str) -> config.Pin:
        pin = config.get_pin(pin_id)
        if pin is None:
            raise HTTPException(404, "no such pin")
        return pin

    def _target(pin_id: str, rel: str) -> tuple[config.Pin, Path]:
        """The one and only place a request path becomes a filesystem path."""
        pin = _pin(pin_id)
        try:
            return pin, resolve(pin, rel)
        except AccessDenied:
            raise HTTPException(403, "outside this pin")
        except PinUnavailable:
            raise HTTPException(410, f"pinned directory is gone: {pin.path}")

    def _page(request: Request, template: str, **extra):
        """Render with the globals every template expects."""
        context = {"hostname": HOSTNAME, "version": __version__}
        context.update(extra)
        return TEMPLATES.TemplateResponse(request, template, context)

    # ---- pins ------------------------------------------------------------------

    # How many pins before a flat list stops being scannable and the tree wins.
    TREE_THRESHOLD = 8
    # A pin whose directory changed within this window counts as "recent".
    RECENT_WINDOW = 24 * 3600

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, view: str = "", sort: str = "activity"):
        every = config.load_pins()
        pins = [p for p in every if not p.archived]
        archived = [p for p in every if p.archived]
        cache_bytes, cache_files = media.cache_usage()

        # "What changed since yesterday" is the question a big pin list is bad at
        # answering, so answer it up front.
        cutoff = time.time() - RECENT_WINDOW
        recent = sorted(
            (p for p in pins if p.activity >= cutoff), key=lambda p: -p.activity
        )

        # Adaptive default: a tree around three pins is just noise, but a flat list of
        # two hundred is unusable. Explicit ?view= always wins.
        if view not in ("flat", "tree", "tag"):
            view = "tree" if len(pins) > TREE_THRESHOLD else "flat"

        if sort == "title":
            ordered = sorted(pins, key=lambda p: p.title.lower())
        elif sort == "path":
            ordered = sorted(pins, key=lambda p: p.path.lower())
        else:
            sort = "activity"
            ordered = sorted(pins, key=lambda p: -p.activity)

        return _page(
            request,
            "index.html",
            pins=ordered,
            view=view,
            sort=sort,
            recent=recent,
            archived=sorted(archived, key=lambda p: p.title.lower()),
            tree=organize.pin_tree(pins),
            tag_groups=organize.group_by_tag(pins),
            missing=[p for p in pins if not p.exists],
            cache_size=browse.human_size(cache_bytes),
            cache_files=cache_files,
            have_ffmpeg=media.have_ffmpeg(),
        )

    @app.post("/pins")
    def add_pin(path: str = Form(...), title: str = Form(""), tags: str = Form("")):
        try:
            pin = config.add_pin(
                Path(path.strip()),
                title=title.strip(),
                tags=[t.strip() for t in tags.split(",") if t.strip()],
            )
        except (NotADirectoryError, OSError) as exc:
            raise HTTPException(400, str(exc))
        return RedirectResponse(f"/b/{pin.id}", status_code=303)

    @app.post("/pins/{pin_id}/archive")
    def archive_pin(pin_id: str):
        """Hide a pin without losing it — the reversible default in the UI."""
        config.set_archived(pin_id, True)
        return RedirectResponse("/", status_code=303)

    @app.post("/pins/{pin_id}/restore")
    def restore_pin(pin_id: str):
        config.set_archived(pin_id, False)
        return RedirectResponse("/", status_code=303)

    @app.post("/pins/{pin_id}/delete")
    def delete_pin(pin_id: str):
        """Permanent. Only offered from the Archived section."""
        config.remove_pin(pin_id)
        return RedirectResponse("/", status_code=303)

    # ---- browse ----------------------------------------------------------------

    @app.get("/b/{pin_id}", response_class=HTMLResponse)
    @app.get("/b/{pin_id}/{rel:path}", response_class=HTMLResponse)
    async def view(request: Request, pin_id: str, rel: str = ""):
        pin, target = _target(pin_id, rel)
        show_hidden = request.query_params.get("hidden") == "1"

        if not target.exists():
            raise HTTPException(404, "not found")

        if target.is_dir():
            entries = browse.list_dir(pin.root, target, show_hidden)
            report = browse.find_report(entries)
            report_html = None
            if report is not None:
                try:
                    text = (pin.root / report.rel).read_text(errors="replace")
                    report_html = render.render_markdown(text, pin.id, report.rel)
                except OSError:
                    report_html = None
            return _page(
                request,
                "browse.html",
                pin=pin,
                rel=rel,
                entries=entries,
                crumbs=browse.breadcrumbs(rel),
                report=report,
                report_html=report_html,
                show_hidden=show_hidden,
                parent=str(Path(rel).parent) if rel and str(Path(rel).parent) != "." else "",
            )

        return await _file_view(request, pin, target, rel)

    async def _file_view(request: Request, pin: config.Pin, target: Path, rel: str):
        kind = browse.classify(target)
        stat = target.stat()
        ctx = dict(
            pin=pin,
            rel=rel,
            kind=kind,
            name=target.name,
            size=browse.human_size(stat.st_size),
            age=browse.human_age(stat.st_mtime),
            crumbs=browse.breadcrumbs(rel),
            parent=str(Path(rel).parent) if str(Path(rel).parent) != "." else "",
            body=None,
            table=None,
            truncated=False,
            video_info=None,
            needs_transcode=False,
        )

        if kind == "markdown":
            ctx["body"] = render.render_markdown(target.read_text(errors="replace"), pin.id, rel)
        elif kind == "table" and stat.st_size <= browse.MAX_INLINE_TEXT:
            header, rows, truncated = render.read_table(target.read_text(errors="replace"))
            ctx["table"] = (header, rows)
            ctx["truncated"] = truncated
        elif kind == "text" and stat.st_size <= browse.MAX_INLINE_TEXT:
            ctx["body"] = render.render_code(target.read_text(errors="replace"), target.name)
        elif kind == "video":
            info = await media.probe(target)
            ctx["video_info"] = info
            ctx["needs_transcode"] = not media.is_browser_playable(target, info)
        elif kind in ("text", "table"):
            ctx["truncated"] = True  # too large to inline; download link only

        return _page(request, "view.html", **ctx)

    # ---- bytes -----------------------------------------------------------------

    @app.get("/raw/{pin_id}/{rel:path}")
    def raw(pin_id: str, rel: str, download: int = 0):
        _, target = _target(pin_id, rel)
        if not target.is_file():
            raise HTTPException(404, "not a file")
        # Starlette's FileResponse handles conditional + Range requests, which is what
        # makes video scrubbing work without buffering the whole file.
        return FileResponse(
            target,
            filename=target.name if download else None,
            content_disposition_type="attachment" if download else "inline",
        )

    @app.get("/nested-media/{pin_id}", response_class=HTMLResponse)
    @app.get("/nested-media/{pin_id}/{rel:path}", response_class=HTMLResponse)
    def nested_media(request: Request, pin_id: str, rel: str = ""):
        """Fragment listing media buried below this directory.

        Served separately and fetched after page load: walking a deep run tree can take
        a moment, and the directory listing must not wait on it.
        """
        pin, target = _target(pin_id, rel)
        if not target.is_dir():
            raise HTTPException(404, "not a directory")

        entries, truncated = browse.find_nested_media(pin.root, target)
        return _page(
            request,
            "_nested_media.html",
            pin=pin,
            entries=entries,
            truncated=truncated,
            limit=browse.NESTED_LIMIT,
        )

    @app.get("/thumb/{pin_id}/{rel:path}")
    async def thumb(pin_id: str, rel: str):
        _, target = _target(pin_id, rel)
        if not target.is_file():
            raise HTTPException(404, "not a file")
        out = await media.thumbnail(target, browse.classify(target))
        if out is None:
            raise HTTPException(415, "no thumbnail")
        return FileResponse(out, media_type="image/webp", headers={"Cache-Control": "max-age=86400"})

    @app.get("/video/{pin_id}/{rel:path}")
    async def video(pin_id: str, rel: str):
        """Serve a browser-playable version, transcoding lazily only when needed."""
        _, target = _target(pin_id, rel)
        if not target.is_file():
            raise HTTPException(404, "not a file")

        info = await media.probe(target)
        if media.is_browser_playable(target, info):
            return FileResponse(target)

        out = await media.transcoded(target)
        if out is None:
            raise HTTPException(415, "cannot transcode; download the original instead")
        return FileResponse(out, media_type="video/mp4")

    # ---- portal ----------------------------------------------------------------

    def _node_payload() -> dict:
        """This node's own view, built in-process.

        Metadata only — never file contents. Ticket *bodies* are metadata too: they are
        the prose an agent wrote about a run, not the run's output. They travel; the
        artifacts they describe never do.
        """
        return {
            "hostname": HOSTNAME,
            "version": __version__,
            "pins": [{**p.to_dict(), "exists": p.exists} for p in config.load_pins()],
            "projects": [v.to_dict() for v in board.local_projects()],
        }

    @app.get("/api/node")
    def api_node():
        """What the portal aggregates."""
        return JSONResponse(_node_payload())

    @app.get("/portal", response_class=HTMLResponse)
    async def portal(request: Request):
        nodes = await tailnet.gather(self_port=self_port, local=_node_payload())
        return _page(request, "portal.html", nodes=nodes)

    # ---- projects & tickets ----------------------------------------------------

    @app.get("/projects", response_class=HTMLResponse)
    async def projects(request: Request):
        """The dashboard: every project across every device, and what it is working on."""
        nodes = await tailnet.gather(self_port=self_port, local=_node_payload())
        rollups = board.rollup(nodes)
        return _page(
            request,
            "projects.html",
            rollups=rollups,
            totals=board.totals(rollups),
            unreachable=[n for n in nodes if not n.reachable and n.online],
            render_ticket=render.render_ticket,
            stale_days=board.STALE_DAYS,
        )

    @app.get("/board", response_class=HTMLResponse)
    async def ticket_board(request: Request, status: str = "", project: str = ""):
        """Every ticket, flat — the drill-down from a project card."""
        nodes = await tailnet.gather(self_port=self_port, local=_node_payload())
        rollups = board.rollup(nodes)

        if project:
            rollups = [r for r in rollups if r.slug == project]

        rows = [(r, t) for r in rollups for t in r.tickets]
        if status == "live":
            rows = [(r, t) for r, t in rows if t.is_live]
        elif status in tasks.STATUSES:
            rows = [(r, t) for r, t in rows if t.status == status]
        rows.sort(key=lambda rt: (rt[1].order, rt[0].slug.lower(), rt[1].id))

        return _page(
            request,
            "board.html",
            rows=rows,
            rollups=rollups,
            status=status,
            project=project,
            statuses=tasks.STATUSES,
            render_ticket=render.render_ticket,
        )

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "hostname": HOSTNAME}

    @app.get("/static/pygments.css")
    def pygments_css():
        return Response(render.pygments_css(), media_type="text/css")

    return app
