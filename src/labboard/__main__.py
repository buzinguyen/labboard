"""Entry point for `python -m labboard` and the uvicorn reloader's import string."""

from __future__ import annotations

import os

from .app import create_app

# Module-level app so `uvicorn labboard.__main__:app --reload` works; the reloader
# re-imports this module in a fresh process, so the port comes from the environment.
app = create_app(self_port=int(os.environ.get("LABBOARD_PORT", "8765")))

if __name__ == "__main__":
    from .cli import main

    raise SystemExit(main())
