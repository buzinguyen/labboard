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

There are two kinds:

| Kind | What it grants | Use it for |
|---|---|---|
| **artifact** (default) | the tree's *bytes* — browse, download, thumbnail | `~/artifacts`, run output roots |
| **project** | *nothing* — only `docs/log/tasks/*.md` is read | a code checkout, so its tickets show on the board |

The split exists because an artifact pin hands the whole tailnet read access to a tree,
which is right for `~/artifacts` and completely wrong for a repo. A project pin is
refused by the path guard outright: `/b/<project-pin>` is a 403 for every path, and the
tickets are read through a constant path that never touches user input.

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
labboard pin add . --project mjlab-go2 --main   # project pin: tickets, no files
labboard pin list [--all]     # --all includes archived; P marks project pins
labboard pin archive <id|path>   # hide it, reversibly — the usual way to tidy up
labboard pin restore <id|path>
labboard pin rm <id|path>        # permanent; prefer archive
labboard scan <dir>...           # register pins declared in labboard.toml manifests
labboard tasks [project]         # this machine's tickets (local, no network)
labboard inbox [project]         # results other devices reported, to fold into tickets
labboard cache [--clear]         # how much derived data has accumulated
```

Then open `https://<node>.tail8b90f5.ts.net` for that machine, or `/portal` on any node
for everything at once. Pages: **Projects** (`/projects`, the dashboard grid) → a project
(`/p/<slug>`, all its tickets) · **Board** (`/board`, every ticket flat) · **Pins** ·
**Tailnet**.

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

A directory whose media is buried further down — weights and configs at the top, eval
videos three levels in — shows an **In subfolders** panel beside the listing, linking
straight to every image and video below that point, newest first. It searches `wandb/`
too, since `wandb/run-*/files/media` is where eval clips usually land. The panel loads
after the page so a deep tree never delays the listing, hides itself when there is
nothing nested, and is bounded (400 files, 8 levels) so a directory of frame dumps
cannot stall it.

## Projects and tickets

Experiments outgrow a pile of directories: which question was this run answering, what
is still open, what finished while you were on another machine. labboard answers that
from the same federated metadata it already gathers — **no sync, no database, no server
holding state.**

A **ticket** is one markdown file in the project repo at `docs/log/tasks/`:

```markdown
---
id: T007
title: Does halving action scale fix the loiter optimum?
status: active            # open | active | blocked | done | dropped
tags: [go2, reward]
runs: [E014, E015]        # rows in docs/log/experiments.md
artifacts: [~/artifacts/go2/E014]
updated: 2026-08-03
---
## Question
## Done when
## Results
```

That location is deliberate: the `project-log` skill already owns it, it is already
git-ignored so planning notes never reach a shared remote, and it puts the ticket next to
the `E###` rows that answer it. One file per ticket, so two agents editing different
tickets can never clobber each other. **labboard only reads these** — agents and you
write them, which is what keeps the service read-only against your repos.

A ticket is a *question*; an `E###` row is *one run launched to answer it*. The ticket
links down to runs and out to artifacts; it never duplicates what `experiments.md`
already records.

### One main device per project

A project's tickets live on one device — the one that owns it. Other devices run
experiments for it and report back with a **receipt** dropped in
`docs/log/tasks/outbox/`:

```markdown
---
kind: receipt
project: mjlab-go2
task: T007
run: E015
updated: 2026-08-02
---
loiter_rate 0.31 → 0.04 at 18k steps. Clean tunnel traversal.
```

Receipts move the way everything in labboard moves — they don't. The board reads them
over HTTP from each node and shows them against the ticket they name; on the main device
`labboard inbox <project>` prints them so an agent can write the report, update the
ticket, and close it. Add the receipt's id to the ticket's `acked:` list and it stops
showing as pending.

### The dashboard

`/projects` is the landing page: a **grid** of cards, one per project, so a dozen
projects fit on one screen instead of scrolling. Each card shows its main device, a
preview of the single `active` ticket, results waiting to be folded in, ticket counts,
and how long the current ticket has been quiet. Violations of the one-ticket-one-project
rule are flagged rather than silently resolved — two devices claiming `main`, or three
tickets active at once, show as warnings on the card.

Clicking a card opens `/p/<slug>`: every ticket for that project, grouped by status with
bodies expanded for the active one, plus its receipts and artifact pins. The ticket
preview on the card deep-links to that ticket's anchor. `/board` remains the flat
cross-project table, filterable by status and project.

Each card also has a **private note box**. It lives in your browser's `localStorage` and
nowhere else: never uploaded, never written to disk, never read by an agent. It is for
your own intent and breadcrumbs, in whatever language you think in. When a note should
become part of the ticket, hit Copy and hand it to an agent — an explicit act, never
automatic. Notes are kept per project and remember which ticket they were written
against, so a note that predates the current ticket says so.

### Declaring pins in the repo

Rather than remembering to pin each project, drop a `labboard.toml` at its root:

```toml
project = "mjlab-go2"           # creates a PROJECT pin on this directory
main    = "ws-3"                # HOSTNAME of the device that owns the tickets

title = "safe_mjlab_zoo"        # optional prefix for every pin from this file
tags  = ["mjlab", "safety"]     # optional defaults, merged into each pin

pins = ["logs/rsl_rl"]          # shorthand — these become ARTIFACT pins

[[pin]]                          # or the long form
path  = "outputs/eval"
title = "eval sweeps"
tags  = ["go2"]
```

`main` names a hostname rather than being `true`, because the manifest is committed and
read on every device that checks the repo out — a boolean would make all of them claim
ownership at once. One committed line stays correct everywhere.

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

With tickets, the loop is:

```bash
labboard tasks                    # what is this project working on?
# ...run the experiment, register E### in docs/log/experiments.md as usual...

# on the project's MAIN device — update the ticket file directly:
#   fill ## Results, add the E### to runs:, bump updated:,
#   set status: done + closed: <date> when the question is answered

# on a FACILITATOR device — you do not own the tickets, so leave a receipt:
cat > docs/log/tasks/outbox/R-$(hostname)-e015.md <<'EOF'
---
kind: receipt
project: mjlab-go2
task: T007
run: E015
title: half action scale converged
updated: 2026-08-02
---
loiter_rate 0.31 → 0.04 at 18k steps.
EOF
```

Rules worth internalising: never write a ticket on a device that is not `main` for that
project — write a receipt instead. Never put results *files* in a ticket; put the path in
`artifacts:` and let labboard resolve it to a link. And never edit a note box — those are
Buzi's, and they are not on disk for you to find.

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

Project pins are refused by the guard for *every* path, so pinning a repo cannot expose
its source. Their tickets are read via a constant path that never sees user input, and
that reader still refuses a symlinked `docs/log/tasks` or a ticket linked in from outside
the repo.

```bash
uv run pytest        # the guard is covered adversarially; keep it that way
```

## Layout

| Path | Role |
|---|---|
| `src/labboard/safety.py` | path guard — the security boundary |
| `src/labboard/config.py` | pins.toml read/write; artifact vs project pins |
| `src/labboard/tasks.py` | ticket + receipt files, frontmatter, artifact links |
| `src/labboard/board.py` | cross-device project rollup and liveness stats |
| `src/labboard/browse.py` | directory listing, file-kind classification |
| `src/labboard/media.py` | ffmpeg thumbnails + lazy transcode |
| `src/labboard/render.py` | markdown / code / CSV rendering |
| `src/labboard/tailnet.py` | peer discovery for the portal |
| `src/labboard/app.py` | routes |
| `systemd/` | user unit + install script |
