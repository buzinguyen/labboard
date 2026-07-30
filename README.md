# labboard

Webpage to view training artifacts across devices, over a Tailscale tailnet.

Replaces the "open a VS Code SSH session into the remote machine's output folder just to
watch one eval video" ritual with a URL that works from a laptop or a phone.

```
        ┌─ portal ─ fetches each node's pin list server-side ─┐
        │  one bookmark                                       │
 you ───┤                                                     │
        │  click a pin → the browser goes DIRECTLY to the node│
        └─► https://ws-4.tail8b90f5.ts.net/b/<pin>/… ─────────┘
```

## What it is not

**It never syncs, copies, or uploads a file between machines.** Every node serves only its
own disk. If a machine is off, its results are simply unavailable until it is back — that
is the intended trade, not a limitation to work around. Which machine holds what stays
visible, so you can tell at a glance where an experiment ran.

It is also **read-only**: no upload, delete, rename, or move. The systemd unit enforces
that at the OS level (`ProtectHome=read-only`), so the service cannot modify a training
output even if it were compromised.

And it does not track metrics — wandb already does that. labboard covers the gap wandb
leaves: files on disk that never made it into a run, like eval videos, matplotlib figures,
PDFs, and CSV dumps.

## Pins

A **pin** is `(absolute path, title)` on the machine running the service. It is both a
bookmark and the authorization scope: labboard serves *only* paths beneath a pinned root,
so adding a pin is the only way to widen access.

Pins live on the node they point at (`~/.config/labboard/pins.toml`), never centrally.
That is what lets an agent register a directory with a purely local file write — no
cross-host call and no credentials anywhere in the system — and what makes each node's
own URL a complete, working board even when the portal host is down.

## Install (per node)

```bash
git clone git@github.com:buzinguyen/labboard.git
cd labboard
bash systemd/install.sh
```

Installs a systemd **user** service, enables linger so it survives logout on headless
boxes, and exposes it with `tailscale serve` — which supplies a real HTTPS certificate and
keeps it reachable only from the tailnet. Nothing is published to the public internet;
Funnel is never enabled.

Requires `uv` and, for thumbnails and transcoding, `ffmpeg`.

## Use

```bash
labboard pin add ~/runs/go2_tunnel_ra/2026-07-30_11-02-14 --title "go2 tunnel RA" --tags mjlab,go2
labboard pin list
labboard pin rm <id|path>
labboard cache            # how much derived data has accumulated
labboard cache --clear
```

Then open `https://<node>.tail8b90f5.ts.net` for that machine, or `/portal` on any node
for everything at once.

### For agents

An agent that just produced artifacts should register them itself:

```bash
labboard pin add "$OUTPUT_DIR" --title "E014 half action scale" --tags mjlab,go2
```

Write a `REPORT.md` in that directory and it renders at the top of the listing, with
relative `![](figs/reward.png)` links resolved — so the report, its figures, and its videos
all arrive as one page.

## Viewers

Markdown reports (`REPORT.md`, `RESULTS.md`, …) · video with lazy transcoding · image
contact sheets · PDF · CSV/TSV tables · syntax-highlighted logs and configs. Checkpoints
and archives are listed but never previewed.

Videos that browsers cannot play — H.264 in `yuv444p` is a real MuJoCo/mjlab output — are
transcoded on demand into `~/.cache/labboard/` and served from there. The original is never
touched. All ffmpeg work runs `nice -n 19`, thread-capped, and gated behind a semaphore, so
the board can never steal CPU from a training run.

## Safety

`src/labboard/safety.py` is the entire boundary between the web and the filesystem, and
every disk-touching route goes through it. It handles `..` traversal, absolute-path
injection, symlink escape (including a symlink onto a deny-listed name), and NUL bytes.
Sensitive filenames — `.env`, `*.pem`, `id_*`, `.netrc`, `.ssh`, `.git` — are never listed
or served even inside a valid pin.

```bash
uv run pytest        # the guard is covered adversarially; keep it that way
```

## Layout

| Path | Role |
|---|---|
| `src/labboard/safety.py` | path guard — the security boundary |
| `src/labboard/config.py` | pins.toml read/write |
| `src/labboard/browse.py` | directory listing, file-kind classification |
| `src/labboard/media.py` | ffmpeg thumbnails + lazy transcode |
| `src/labboard/render.py` | markdown / code / CSV rendering |
| `src/labboard/tailnet.py` | peer discovery for the portal |
| `src/labboard/app.py` | routes |
| `systemd/` | user unit + install script |
