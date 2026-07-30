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

Step order matters: `ffmpeg` must exist before the install, and the tailscale *operator*
must be set before `tailscale serve` — without it, serve blocks forever with no output
rather than erroring.

```bash
# 1. prerequisites (need a real terminal for the sudo password)
sudo apt update && sudo apt install -y ffmpeg
sudo tailscale set --operator=$USER
loginctl enable-linger $USER

# 2. install
git clone https://github.com/buzinguyen/labboard.git ~/labboard
cd ~/labboard && bash systemd/install.sh

# 3. add the short-name route: http://<node>/
tailscale serve --bg --http=80 http://127.0.0.1:8765

# 4. pin your output roots
labboard pin add <project-output-root> --title "..." --tags <project>
```

Step 2 installs a systemd **user** service, enables linger so it survives logout on
headless boxes, symlinks `labboard` into `~/.local/bin`, and exposes it with
`tailscale serve` — a real HTTPS certificate, reachable only from the tailnet. Nothing is
published to the public internet; Funnel is never enabled.

Requires `uv` and, for thumbnails and transcoding, `ffmpeg`. Clone over **HTTPS** — the
repo is public, and GitHub SSH auth often fails without a TTY-loaded agent.

Verify with `systemctl --user status labboard` and
`curl -s http://127.0.0.1:8765/healthz`. The first HTTPS request to a new node takes
~15s while Let's Encrypt provisions the certificate; warm requests are ~20ms.

### Updating

`uv sync` deletes the site-packages the running process is using, so a restart is not
optional — skipping it surfaces later as a 500 on `/portal` with an SSL error:

```bash
cd ~/labboard && git pull && uv sync && systemctl --user restart labboard
```

### Troubleshooting

| Symptom | Cause |
|---|---|
| `tailscale serve` hangs with no output | operator not set — run step 1 |
| `systemctl --user`: `Failed to connect to bus` over SSH | `export XDG_RUNTIME_DIR=/run/user/$(id -u)` |
| `Too many authentication failures` on SSH | agent offers too many keys; set `IdentitiesOnly yes` in `~/.ssh/config` |
| No thumbnails, videos won't play | `ffmpeg` missing |
| 500 on `/portal` after an update | service not restarted after `uv sync` |

## Use

```bash
labboard pin add ~/runs/go2_tunnel_ra/2026-07-30_11-02-14 --title "go2 tunnel RA" --tags mjlab,go2
labboard pin list [--all]     # --all includes archived
labboard pin archive <id|path>   # hide it, reversibly — the usual way to tidy up
labboard pin restore <id|path>
labboard pin rm <id|path>        # permanent; prefer archive
labboard scan <dir>...           # register pins declared in labboard.toml manifests
labboard cache [--clear]         # how much derived data has accumulated
```

Then open `https://<node>.tail8b90f5.ts.net` for that machine, or `/portal` on any node
for everything at once.

### Finding things once there are many pins

The Pins page offers three views. **Tree** groups pins by their longest shared path
prefix, collapsing single-child chains, so `~/proj/logs/run1` and `~/proj/logs/run2` sit
together under one `~/proj/logs` group. **Tags** groups by tag. **List** is flat, sorted
by recent activity, name, or path. A search box filters title, path and tags at once, and
opens whichever collapsed groups contain matches. The default is tree above eight pins.

A **Recent activity** section sits at the top: pins whose directory changed in the last 24
hours. With a large board this is usually the only part you need.

Inside a directory, the listing filters by name, modification time and size, and the
columns sort. Size filters never hide folders — navigation is the reason you are there.

### Declaring pins in the repo

Rather than remembering to pin each project, drop a `labboard.toml` at its root:

```toml
title = "safe_mjlab_zoo"        # optional prefix for every pin from this file
tags  = ["mjlab", "safety"]     # optional defaults, merged into each pin

pins = ["logs/rsl_rl"]          # shorthand

[[pin]]                          # or the long form
path  = "outputs/eval"
title = "eval sweeps"
tags  = ["go2"]
```

Then `labboard scan ~/projects` registers everything declared beneath that root. Paths are
relative to the manifest and may not escape its directory. The search skips `.git`,
`wandb`, `checkpoints`, `node_modules` and friends, stops at four levels deep by default
(`--depth`), and does not descend past a manifest it has already found. A broken manifest
is reported and skipped, never fatal to the scan. `--dry-run` shows what would happen.

Scanning is idempotent and deliberately **will not un-archive** a pin you archived — only
an explicit `pin add` does that.

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
