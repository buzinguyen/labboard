"""labboard — read-only, federated artifact browser over a Tailscale tailnet.

Every machine runs this same service, serving only its own disk. A portal view
aggregates each node's pin list so there is one bookmark, but file bytes always
travel directly from the origin node to the browser.

Nothing here ever copies a file between machines.
"""

__version__ = "0.1.0"
