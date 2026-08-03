"""The path guard.

Since a pin is the *only* authorization scope in labboard, this module is the entire
boundary between the web and the filesystem. Every route that touches disk must go
through `resolve()`; nothing else is allowed to build a path from user input.

Threats handled here:
  * `..` traversal, in the request path or via a crafted component
  * absolute-path injection — note `Path("/root") / "/etc/passwd" == Path("/etc/passwd")`,
    a pathlib footgun that silently escapes if you only concatenate
  * symlink escape — a `latest -> ../../elsewhere` link inside a pinned run dir
  * NUL-byte smuggling
  * sensitive filenames that happen to sit inside a legitimately pinned tree

Deliberately NOT handled: TOCTOU between `resolve()` and the subsequent `open()`.
Defending that needs `O_NOFOLLOW` walking, and the threat model here is a read-only
service on a single-user tailnet — not worth the complexity.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

__all__ = ["AccessDenied", "PinUnavailable", "is_denied", "resolve", "safe_relparts"]


class AccessDenied(Exception):
    """Request resolved outside its pin, or hit a deny-listed name. Render as 403."""


class PinUnavailable(Exception):
    """The pinned root is gone or unreadable (deleted dir, unmounted disk). Render as 410."""


# Exact filenames (case-insensitive) never listed and never served, even inside a valid pin.
DENY_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".git",  # not an artifact, and .git/config can carry remote tokens
        ".git-credentials",
        ".htpasswd",
        ".npmrc",
        ".pypirc",
        ".ssh",
        ".gnupg",
        ".aws",
        ".docker",
        ".kube",
        "credentials",
        "secrets",
        "id_rsa",
        "id_dsa",
    }
)

# Prefix / suffix rules, for the families the exact list can't enumerate
# (`.env.production`, `id_ed25519.pub`, `wildcard.pem`, ...).
DENY_PREFIXES = (".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "secret")
DENY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".ovpn", ".kdbx", ".asc")


def is_denied(name: str) -> bool:
    """True if this single path component must never be listed or served."""
    lowered = name.lower()
    return (
        lowered in DENY_NAMES
        or lowered.startswith(DENY_PREFIXES)
        or lowered.endswith(DENY_SUFFIXES)
    )


def safe_relparts(relpath: str) -> tuple[str, ...]:
    """Normalize a user-supplied relative path, rejecting anything hostile.

    Returns the cleaned components. Raises `AccessDenied` rather than silently
    sanitizing, so a probe shows up as a 403 in the log instead of quietly
    resolving to something adjacent.
    """
    if not relpath:
        return ()
    if "\x00" in relpath:
        raise AccessDenied("NUL byte in path")

    pure = PurePosixPath(relpath)
    if pure.is_absolute():
        # Would escape the root entirely under `/` concatenation.
        raise AccessDenied("absolute path")

    parts = tuple(p for p in pure.parts if p not in ("", "."))
    if any(p == ".." for p in parts):
        raise AccessDenied("parent traversal")
    for part in parts:
        if is_denied(part):
            raise AccessDenied(f"deny-listed component: {part}")
    return parts


def resolve(pin, relpath: str = "") -> Path:
    """Resolve `relpath` inside `pin`, or raise.

    `pin.root` is already realpath'd, so both sides of the containment check are
    real paths — that is what makes the symlink case fall out correctly: a link
    pointing outside the pin resolves to an outside path and fails containment.
    """
    # A project pin names a code checkout so labboard can read its tickets. Serving
    # bytes from one would put the source tree — and whatever a colleague's checkout
    # dragged in — on the tailnet. There is no relpath for which that is intended, so
    # refuse the whole pin here rather than trying to allow-list a subtree. Tickets are
    # read via a constant path in `tasks.py` that never sees user input.
    if getattr(pin, "kind", "artifact") == "project":
        raise AccessDenied("project pins expose tickets only, never files")

    root = pin.root
    if not root.is_dir():
        raise PinUnavailable(str(root))

    parts = safe_relparts(relpath)
    if not parts:
        return root

    # `.resolve()` is non-strict: a missing path still normalizes, and we 404 later.
    target = root.joinpath(*parts).resolve()

    if target != root and not target.is_relative_to(root):
        raise AccessDenied(f"escapes pin: {target}")

    # Re-check post-resolution: a symlink may have landed us on a denied name
    # that was not visible in the requested components.
    for part in target.relative_to(root).parts:
        if is_denied(part):
            raise AccessDenied(f"deny-listed component: {part}")

    return target
