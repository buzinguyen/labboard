#!/usr/bin/env bash
# Install labboard as a systemd *user* service and expose it on the tailnet.
#
# Idempotent — safe to re-run after a `git pull`.
#
#   bash systemd/install.sh              # install + start + expose
#   PORT=9000 bash systemd/install.sh    # non-default port
#   bash systemd/install.sh --no-serve   # skip the `tailscale serve` step
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8765}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/labboard.service"
DO_SERVE=1
[[ "${1:-}" == "--no-serve" ]] && DO_SERVE=0

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v uv >/dev/null || die "uv not found — install it first: https://docs.astral.sh/uv/"
command -v ffmpeg >/dev/null || warn "ffmpeg not found — thumbnails and transcoding will be disabled."

say "Syncing dependencies in $REPO"
(cd "$REPO" && uv sync --quiet)

say "Creating config and cache directories"
# ReadWritePaths= requires these to exist before the unit starts.
mkdir -p "$HOME/.config/labboard" "$HOME/.cache/labboard"

say "Writing $UNIT"
mkdir -p "$UNIT_DIR"
sed -e "s|@REPO@|$REPO|g" -e "s|@PORT@|$PORT|g" \
    "$REPO/systemd/labboard.service.in" > "$UNIT"

# Agents are instructed (via the global CLAUDE.md) to call `labboard pin add`, so the
# command has to resolve without knowing where the repo was cloned.
say "Linking labboard into ~/.local/bin"
mkdir -p "$HOME/.local/bin"
ln -sf "$REPO/.venv/bin/labboard" "$HOME/.local/bin/labboard"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "~/.local/bin is not on PATH — add it so \`labboard\` resolves in new shells." ;;
esac

say "Enabling service"
systemctl --user daemon-reload
systemctl --user enable --now labboard.service

# Without lingering, the service dies when the last SSH session closes — which is
# exactly the case on the headless workstations this exists to serve.
if ! loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -q 'Linger=yes'; then
  say "Enabling linger (keeps the service alive after logout)"
  loginctl enable-linger "$USER" || warn "could not enable linger; the service will stop at logout"
fi

sleep 1
if ! systemctl --user is-active --quiet labboard.service; then
  warn "service is not active — recent logs:"
  journalctl --user -u labboard.service -n 20 --no-pager || true
  die "labboard failed to start"
fi
say "Service is running on http://127.0.0.1:$PORT"

if [[ "$DO_SERVE" == 1 ]]; then
  if ! command -v tailscale >/dev/null; then
    warn "tailscale not found — skipping. labboard is reachable on loopback only."
  else
    say "Exposing on the tailnet via tailscale serve"
    # `tailscale serve` blocks forever with no output when it lacks privileges rather
    # than erroring, so never run it unbounded — a hang here looks like a broken install.
    set +e
    timeout 30 tailscale serve --bg --https=443 "http://127.0.0.1:$PORT"
    rc=$?
    set -e

    if [[ $rc -eq 0 ]]; then
      DNS="$(tailscale status --json 2>/dev/null \
             | grep -o '"DNSName":"[^"]*"' | head -1 | cut -d'"' -f4 | sed 's/\.$//')"
      say "Available at https://${DNS:-<this-node>}"
    else
      [[ $rc -eq 124 ]] && warn "tailscale serve timed out — it almost always means missing privileges."
      warn "Could not expose on the tailnet. The service itself is running fine on loopback."
      warn ""
      warn "Grant the CLI persistent access (once per machine, needs your password):"
      warn "  sudo tailscale set --operator=\$USER"
      warn "then re-run:"
      warn "  tailscale serve --bg --https=443 http://127.0.0.1:$PORT"
      warn ""
      warn "Or do it as root without changing the operator:"
      warn "  sudo tailscale serve --bg --https=443 http://127.0.0.1:$PORT"
      warn ""
      warn "HTTPS certificates must also be enabled for the tailnet"
      warn "(admin console → DNS → HTTPS Certificates)."
    fi
  fi
fi

cat <<EOF

  status   systemctl --user status labboard
  logs     journalctl --user -u labboard -f
  restart  systemctl --user restart labboard
  pin      $REPO/.venv/bin/labboard pin add /path/to/results --title "..."
  remove   systemctl --user disable --now labboard && tailscale serve --https=443 off

EOF
