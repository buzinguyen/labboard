"""Tests for the path guard.

This is the whole security boundary, so it gets adversarial tests rather than
happy-path ones.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from labboard import config
from labboard.safety import AccessDenied, PinUnavailable, is_denied, resolve, safe_relparts


@pytest.fixture
def pin(tmp_path, monkeypatch):
    """A pinned dir with a run-like layout, plus a secret sitting *outside* it."""
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "pins.toml"))

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.txt").write_text("secret")

    root = tmp_path / "run"
    (root / "videos").mkdir(parents=True)
    (root / "videos" / "eval.mp4").write_bytes(b"\x00\x01")
    (root / "REPORT.md").write_text("# hi")
    (root / ".env").write_text("WANDB_API_KEY=nope")

    return config.add_pin(root, title="run")


# --- containment ---------------------------------------------------------------


def test_empty_relpath_is_the_root(pin):
    assert resolve(pin, "") == pin.root


def test_normal_descent(pin):
    assert resolve(pin, "videos/eval.mp4") == pin.root / "videos" / "eval.mp4"


def test_leading_slash_and_dot_segments_are_normalized_or_refused(pin):
    assert resolve(pin, "./videos") == pin.root / "videos"
    with pytest.raises(AccessDenied):
        resolve(pin, "/etc/passwd")


@pytest.mark.parametrize(
    "attack",
    [
        "../outside/loot.txt",
        "videos/../../outside/loot.txt",
        "..",
        "a/../../..",
        "../..",  # what `%2e%2e%2f%2e%2e` becomes once Starlette decodes it
    ],
)
def test_traversal_is_refused(pin, attack):
    with pytest.raises(AccessDenied):
        resolve(pin, attack)


def test_percent_encoding_is_the_transport_layers_job(pin):
    """The guard sees already-decoded paths, so `%2f` is a literal character here.

    Starlette URL-decodes `{path:path}` before the route calls us: `..%2f..` arrives
    as `../..` and is caught by `test_traversal_is_refused`. If the *undecoded* text
    ever reaches the guard it is a single, harmless component naming a file that does
    not exist inside the pin — never a separator.
    """
    got = resolve(pin, "videos/..%2f..")
    assert got.is_relative_to(pin.root)
    assert not got.exists()


def test_absolute_path_injection(pin):
    """`Path(root) / "/etc/passwd"` silently escapes — guard must catch it."""
    with pytest.raises(AccessDenied):
        resolve(pin, "/etc/shadow")


def test_nul_byte(pin):
    with pytest.raises(AccessDenied):
        resolve(pin, "videos/eval\x00.mp4")


# --- symlinks ------------------------------------------------------------------


def test_symlink_escaping_the_pin_is_refused(pin, tmp_path):
    os.symlink(tmp_path / "outside", pin.root / "escape")
    with pytest.raises(AccessDenied):
        resolve(pin, "escape/loot.txt")


def test_symlink_staying_inside_the_pin_is_allowed(pin):
    """`latest -> <timestamp>` is a real and common layout; it must keep working."""
    os.symlink(pin.root / "videos", pin.root / "latest")
    assert resolve(pin, "latest/eval.mp4") == pin.root / "videos" / "eval.mp4"


def test_symlink_onto_a_denied_name_is_refused(pin):
    """The requested components look innocent; only the resolved target is denied."""
    os.symlink(pin.root / ".env", pin.root / "innocent.txt")
    with pytest.raises(AccessDenied):
        resolve(pin, "innocent.txt")


# --- deny-list -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [".env", ".ENV", ".env.production", "id_ed25519", "id_ed25519.pub", "server.pem",
     "private.key", "vault.kdbx", ".git", ".ssh", ".netrc", "secrets"],
)
def test_denied_names(name):
    assert is_denied(name)


@pytest.mark.parametrize(
    "name",
    ["eval.mp4", "REPORT.md", "model_400.pt", "figure.png", "environment.yaml", "keyframes.csv"],
)
def test_allowed_names(name):
    assert not is_denied(name)


def test_denied_file_inside_a_valid_pin(pin):
    with pytest.raises(AccessDenied):
        resolve(pin, ".env")


# --- pin lifecycle -------------------------------------------------------------


def test_missing_root_raises_pin_unavailable(pin):
    import shutil

    shutil.rmtree(pin.root)
    with pytest.raises(PinUnavailable):
        resolve(pin, "")


def test_nonexistent_child_resolves_then_404s_upstream(pin):
    """Guard allows it (it is inside the pin); the route is what reports 404."""
    got = resolve(pin, "videos/missing.mp4")
    assert got == pin.root / "videos" / "missing.mp4"
    assert not got.exists()


def test_safe_relparts_strips_noise():
    assert safe_relparts("a//b/./c") == ("a", "b", "c")
    assert safe_relparts("") == ()
