"""Route-level tests, with emphasis on the guard holding at the HTTP boundary.

The unit tests in test_safety.py prove `resolve()` is correct; these prove no route
bypasses it, including through URL-encoded traversal that only becomes dangerous
after Starlette decodes it.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from labboard import config
from labboard.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "pins.toml"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.txt").write_text("secret")

    root = tmp_path / "run"
    (root / "figs").mkdir(parents=True)
    (root / "figs" / "reward.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    (root / "REPORT.md").write_text("# Results\n\n![reward](figs/reward.png)\n\nLooks fine.\n")
    (root / "metrics.csv").write_text("step,reward\n0,1.5\n100,2.5\n")
    (root / "train.log").write_text("epoch 1\nepoch 2\n")
    (root / ".env").write_text("WANDB_API_KEY=nope")
    (root / "model_400.pt").write_bytes(b"\x00" * 64)

    pin = config.add_pin(root, title="run", tags=["go2"])
    app = create_app()
    with TestClient(app) as c:
        c.pin = pin
        c.outside = outside
        yield c


# --- basics --------------------------------------------------------------------


def test_index_lists_the_pin(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "run" in resp.text


def test_healthz(client):
    assert client.get("/healthz").json()["ok"] is True


def test_api_node_exposes_metadata_only(client):
    payload = client.get("/api/node").json()
    assert payload["pins"][0]["title"] == "run"
    assert payload["pins"][0]["exists"] is True
    # Metadata, never contents.
    assert "loot" not in client.get("/api/node").text


def test_browse_lists_entries(client):
    resp = client.get(f"/b/{client.pin.id}")
    assert resp.status_code == 200
    assert "metrics.csv" in resp.text
    assert "figs" in resp.text


def test_dotfiles_and_denied_names_are_not_listed(client):
    resp = client.get(f"/b/{client.pin.id}")
    assert ".env" not in resp.text


# --- the guard, at the HTTP layer ----------------------------------------------


@pytest.mark.parametrize("path", [
    "../outside/loot.txt",
    "figs/../../outside/loot.txt",
    "%2e%2e/outside/loot.txt",          # decoded to ../ by Starlette before routing
    "..%2f..%2foutside%2floot.txt",     # decoded to ../../outside/loot.txt
])
def test_traversal_blocked_on_every_byte_route(client, path):
    for prefix in ("/b", "/raw", "/thumb", "/video"):
        resp = client.get(f"{prefix}/{client.pin.id}/{path}")
        assert resp.status_code in (403, 404), f"{prefix}/{path} → {resp.status_code}"
        assert "secret" not in resp.text


def test_denied_file_is_403_not_served(client):
    resp = client.get(f"/raw/{client.pin.id}/.env")
    assert resp.status_code == 403
    assert "WANDB_API_KEY" not in resp.text


def test_symlink_escape_blocked_over_http(client):
    os.symlink(client.outside, client.pin.root / "escape")
    resp = client.get(f"/raw/{client.pin.id}/escape/loot.txt")
    assert resp.status_code == 403
    assert "secret" not in resp.text


def test_unknown_pin_is_404(client):
    assert client.get("/b/deadbeef00").status_code == 404


def test_missing_file_is_404(client):
    assert client.get(f"/b/{client.pin.id}/nope.png").status_code == 404


def test_no_write_routes_exist(client):
    """Read-only is a design guarantee, not a convention — assert it structurally."""
    mutating = {
        (route.path, method)
        for route in client.app.routes
        for method in getattr(route, "methods", set())
        if method in {"PUT", "PATCH", "DELETE"}
    }
    assert mutating == set()

    posts = {
        route.path
        for route in client.app.routes
        for method in getattr(route, "methods", set())
        if method == "POST"
    }
    # The only POSTs manage pins (entries in a config file), never file content.
    assert posts == {
        "/pins",
        "/pins/{pin_id}/archive",
        "/pins/{pin_id}/restore",
        "/pins/{pin_id}/delete",
    }


# --- viewers -------------------------------------------------------------------


def test_markdown_report_renders_and_rewrites_relative_images(client):
    resp = client.get(f"/b/{client.pin.id}/REPORT.md")
    assert resp.status_code == 200
    assert f"/raw/{client.pin.id}/figs/reward.png" in resp.text


def test_report_is_surfaced_on_the_directory_page(client):
    resp = client.get(f"/b/{client.pin.id}")
    assert "Looks fine." in resp.text


def test_directory_listing_comes_before_report_and_media(client):
    """Navigating deeper is the common action, so folders must not be pushed below
    a long report or a large gallery."""
    (client.pin.root / "rollout.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    html = client.get(f"/b/{client.pin.id}").text
    listing = html.index('<table class="listing"')
    report = html.index("<h2>📝")
    media = html.index("<h2>Media")

    assert listing < report < media, "listing must render first, then report, then media"


def test_csv_preview(client):
    resp = client.get(f"/b/{client.pin.id}/metrics.csv")
    assert resp.status_code == 200
    assert "reward" in resp.text and "2.5" in resp.text


def test_log_file_renders_inline(client):
    resp = client.get(f"/b/{client.pin.id}/train.log")
    assert resp.status_code == 200
    assert "epoch 2" in resp.text


def test_checkpoint_gets_no_preview_only_download(client):
    resp = client.get(f"/b/{client.pin.id}/model_400.pt")
    assert resp.status_code == 200
    assert "No inline preview" in resp.text


def test_raw_download_sets_attachment(client):
    resp = client.get(f"/raw/{client.pin.id}/metrics.csv?download=1")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]


def test_range_request_is_honoured(client):
    """Video scrubbing depends on this; verify rather than assume."""
    resp = client.get(f"/raw/{client.pin.id}/metrics.csv", headers={"Range": "bytes=0-4"})
    assert resp.status_code == 206
    assert resp.headers["content-range"].startswith("bytes 0-4/")
    assert len(resp.content) == 5


# --- pin lifecycle over HTTP ----------------------------------------------------


def test_add_and_remove_pin(client, tmp_path):
    other = tmp_path / "another"
    other.mkdir()

    resp = client.post("/pins", data={"path": str(other), "title": "another"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert any(p.title == "another" for p in config.load_pins())

    pin_id = next(p.id for p in config.load_pins() if p.title == "another")
    resp = client.post(f"/pins/{pin_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert not any(p.title == "another" for p in config.load_pins())


def test_adding_a_file_as_a_pin_is_rejected(client, tmp_path):
    afile = tmp_path / "notadir.txt"
    afile.write_text("x")
    resp = client.post("/pins", data={"path": str(afile)}, follow_redirects=False)
    assert resp.status_code == 400


def test_pin_pointing_at_a_deleted_directory_reports_410(client, tmp_path):
    import shutil

    gone = tmp_path / "gone"
    gone.mkdir()
    pin = config.add_pin(gone, title="gone")
    shutil.rmtree(gone)
    assert client.get(f"/b/{pin.id}").status_code == 410
