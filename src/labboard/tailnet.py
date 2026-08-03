"""Tailnet peer discovery for the portal view.

The portal fetches each node's pin list *server-side* and renders links that point
straight at the origin node. Two reasons it works this way:

  * no CORS dance between `*.tail8b90f5.ts.net` origins, and
  * file bytes never hop through the portal host — which matters because buzi-pc
    currently reaches some peers over a DERP relay, and double-hopping video through
    it would be needlessly slow.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
from dataclasses import dataclass, field

import httpx

PEER_TIMEOUT = 2.0  # one sleeping workstation must never stall the portal


@dataclass
class Node:
    name: str
    dns: str
    ip: str
    online: bool
    is_self: bool = False
    os: str = ""
    reachable: bool = False
    error: str = ""
    pins: list[dict] = field(default_factory=list)
    projects: list[dict] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"https://{self.dns}"

    def url(self, path: str) -> str:
        """A link to `path` on this node.

        Relative for ourselves — a link to our own machine always works, and does not
        depend on knowing our own DNS name or having a certificate. Absolute for peers,
        because the browser must go straight to the machine holding the data rather
        than routing bytes through this one.
        """
        return path if self.is_self else f"{self.base_url}{path}"


def _clean_dns(raw: str, fallback: str) -> str:
    return raw.rstrip(".") if raw else fallback


def discover() -> list[Node]:
    """Read the tailnet from `tailscale status --json`. Empty list if unavailable."""
    if not shutil.which("tailscale"):
        return []
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, timeout=10, check=False
        )
        if out.returncode != 0:
            return []
        data = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []

    nodes: list[Node] = []
    me = data.get("Self") or {}
    if me:
        nodes.append(
            Node(
                name=me.get("HostName", socket.gethostname()),
                dns=_clean_dns(me.get("DNSName", ""), me.get("HostName", "localhost")),
                ip=(me.get("TailscaleIPs") or [""])[0],
                online=True,
                is_self=True,
                os=me.get("OS", ""),
            )
        )

    for peer in (data.get("Peer") or {}).values():
        # Phones and tablets are on the tailnet but never run labboard.
        if peer.get("OS") in ("android", "ios"):
            continue
        nodes.append(
            Node(
                name=peer.get("HostName", "?"),
                dns=_clean_dns(peer.get("DNSName", ""), peer.get("HostName", "")),
                ip=(peer.get("TailscaleIPs") or [""])[0],
                online=bool(peer.get("Online")),
                os=peer.get("OS", ""),
            )
        )

    nodes.sort(key=lambda n: (not n.is_self, not n.online, n.name))
    return nodes


def _apply(node: Node, payload: dict) -> Node:
    node.pins = payload.get("pins", [])
    # Absent on nodes still running a pre-tickets build — an old peer degrades
    # to "no projects", never to an error.
    node.projects = payload.get("projects", [])
    node.reachable = True
    return node


async def _fetch(
    client: httpx.AsyncClient, node: Node, self_port: int, local: dict | None = None
) -> Node:
    # Answer for ourselves in-process. Going out over HTTP to our own port would make
    # the dashboard depend on the service being reachable at a guessed port — which is
    # exactly what breaks under `--reload`, under a non-default port, and in tests.
    if node.is_self and local is not None:
        return _apply(node, local)

    if not node.online and not node.is_self:
        node.error = "offline"
        return node

    # Skip the network round-trip (and the cert) when asking ourselves.
    url = f"http://127.0.0.1:{self_port}/api/node" if node.is_self else f"{node.base_url}/api/node"
    try:
        resp = await client.get(url, timeout=PEER_TIMEOUT)
        if resp.status_code == 200:
            _apply(node, resp.json())
        else:
            node.error = f"HTTP {resp.status_code}"
    except httpx.TimeoutException:
        node.error = "timed out"
    except httpx.HTTPError as exc:
        node.error = type(exc).__name__.replace("Error", "").lower() or "unreachable"
    return node


async def gather(self_port: int = 8765, local: dict | None = None) -> list[Node]:
    """Query every node concurrently. Failures degrade to an 'offline' badge.

    `local` is this node's own `/api/node` payload. Passing it keeps the machine you
    are looking at present in the results even with tailscale down or absent — your own
    projects should never vanish because the tailnet is unreachable.
    """
    nodes = discover()
    if not nodes:
        if local is None:
            return []
        # No tailnet at all: still show this machine, so the board works standalone.
        return [_apply(
            Node(name=socket.gethostname(), dns=socket.gethostname(), ip="",
                 online=True, is_self=True),
            local,
        )]
    async with httpx.AsyncClient(verify=True, follow_redirects=True) as client:
        return list(
            await asyncio.gather(*(_fetch(client, n, self_port, local) for n in nodes))
        )
