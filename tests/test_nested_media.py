"""Nested-media discovery: the sidebar that finds results buried in subfolders."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from labboard import config
from labboard.app import create_app
from labboard.browse import find_nested_media

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A run tree shaped like the real thing: weights on top, media buried."""
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "pins.toml"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    root = tmp_path / "runs"
    (root / "e058/videos").mkdir(parents=True)
    (root / "e058/figs").mkdir(parents=True)
    (root / "e057/wandb/run-abc/files/media/videos").mkdir(parents=True)

    (root / "model_400.pt").write_bytes(b"\x00" * 8)      # top level: no media
    (root / "config.yaml").write_text("lr: 3e-4")
    (root / "e058/videos/eval.mp4").write_bytes(b"\x00" * 8)
    (root / "e058/figs/reward.png").write_bytes(PNG)
    (root / "e057/wandb/run-abc/files/media/videos/rollout.mp4").write_bytes(b"\x00" * 8)

    return root


def names(entries) -> set[str]:
    return {e.name for e in entries}


# --- discovery -----------------------------------------------------------------


def test_finds_media_buried_several_levels_down(tree):
    found, truncated = find_nested_media(tree, tree)
    assert names(found) == {"eval.mp4", "reward.png", "rollout.mp4"}
    assert not truncated


def test_wandb_is_searched_not_skipped(tree):
    """wandb/run-*/files/media is exactly where eval clips live."""
    found, _ = find_nested_media(tree, tree)
    assert "rollout.mp4" in names(found)


def test_files_in_the_current_directory_are_excluded(tree):
    """Those already render in the page's own gallery."""
    (tree / "toplevel.png").write_bytes(PNG)
    found, _ = find_nested_media(tree, tree)
    assert "toplevel.png" not in names(found)


def test_non_media_is_ignored(tree):
    found, _ = find_nested_media(tree, tree)
    assert not {"model_400.pt", "config.yaml"} & names(found)


def test_rel_paths_are_relative_to_the_pin_root(tree):
    found, _ = find_nested_media(tree, tree)
    assert {e.rel for e in found} == {
        "e058/videos/eval.mp4",
        "e058/figs/reward.png",
        "e057/wandb/run-abc/files/media/videos/rollout.mp4",
    }


def test_newest_first(tree):
    old = tree / "e058/figs/reward.png"
    os.utime(old, (1, 1))
    found, _ = find_nested_media(tree, tree)
    assert found[-1].name == "reward.png"


def test_searching_from_a_subdirectory(tree):
    found, _ = find_nested_media(tree, tree / "e058")
    assert names(found) == {"eval.mp4", "reward.png"}


# --- bounds --------------------------------------------------------------------


def test_result_cap_reports_truncation(tree):
    burst = tree / "e058/figs"
    for i in range(30):
        (burst / f"frame_{i:03d}.png").write_bytes(PNG)

    found, truncated = find_nested_media(tree, tree, limit=10)
    assert len(found) == 10
    assert truncated


def test_depth_limit(tree):
    found, _ = find_nested_media(tree, tree, max_depth=1)
    # e058/videos/eval.mp4 is two levels down, so a depth of 1 must not reach it.
    assert names(found) == set()


def test_noisy_directories_are_skipped(tree):
    junk = tree / "node_modules/pkg"
    junk.mkdir(parents=True)
    (junk / "logo.png").write_bytes(PNG)
    found, _ = find_nested_media(tree, tree)
    assert "logo.png" not in names(found)


def test_hidden_directories_are_skipped(tree):
    hidden = tree / ".cache/thumbs"
    hidden.mkdir(parents=True)
    (hidden / "x.png").write_bytes(PNG)
    found, _ = find_nested_media(tree, tree)
    assert "x.png" not in names(found)


# --- safety --------------------------------------------------------------------


def test_symlinked_directories_are_not_followed(tree, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(PNG)
    os.symlink(outside, tree / "e058/link")

    found, _ = find_nested_media(tree, tree)
    assert "secret.png" not in names(found)


def test_symlinked_file_pointing_outside_is_dropped(tree, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(PNG)
    os.symlink(outside / "secret.png", tree / "e058/figs/innocent.png")

    found, _ = find_nested_media(tree, tree)
    assert "innocent.png" not in names(found), "must not advertise what /raw would refuse"


# --- route ---------------------------------------------------------------------


@pytest.fixture
def client(tree):
    pin = config.add_pin(tree, title="runs")
    with TestClient(create_app()) as c:
        c.pin = pin
        yield c


def test_fragment_lists_nested_media(client):
    resp = client.get(f"/nested-media/{client.pin.id}")
    assert resp.status_code == 200
    assert 'data-count="3"' in resp.text
    assert "eval.mp4" in resp.text


def test_fragment_is_empty_when_nothing_is_nested(client):
    resp = client.get(f"/nested-media/{client.pin.id}/e058/figs")
    assert 'data-count="0"' in resp.text


def test_fragment_respects_the_pin_boundary(client):
    assert client.get(f"/nested-media/{client.pin.id}/../../etc").status_code in (403, 404)


def test_fragment_on_a_file_is_404(client):
    assert client.get(f"/nested-media/{client.pin.id}/model_400.pt").status_code == 404


def test_browse_page_wires_up_the_sidebar(client):
    html = client.get(f"/b/{client.pin.id}").text
    assert f'data-src="/nested-media/{client.pin.id}"' in html
