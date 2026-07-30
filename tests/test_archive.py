"""Archive semantics — the reversible alternative to deleting a pin."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from labboard import config, project
from labboard.app import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "pins.toml"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "runs").mkdir()
    return tmp_path


@pytest.fixture
def client(env):
    with TestClient(create_app()) as c:
        yield c


def test_archiving_hides_without_deleting(env):
    pin = config.add_pin(env / "runs", title="runs")

    assert config.set_archived(pin.id, True)
    assert [p.id for p in config.load_pins()] == [pin.id], "the entry must survive"
    assert config.active_pins() == []

    config.set_archived(pin.id, False)
    assert [p.id for p in config.active_pins()] == [pin.id]


def test_archived_state_persists_across_reload(env):
    pin = config.add_pin(env / "runs")
    config.set_archived(pin.id, True)
    assert config.load_pins()[0].archived is True


def test_set_archived_on_unknown_pin_is_false(env):
    assert config.set_archived("nope", True) is False


def test_explicit_add_unarchives(env):
    """Typing `pin add` for an archived pin means you want it back."""
    pin = config.add_pin(env / "runs")
    config.set_archived(pin.id, True)

    config.add_pin(env / "runs")
    assert config.load_pins()[0].archived is False


def test_scan_does_not_resurrect_archived_pins(env):
    """Otherwise every scan would undo deliberate archiving."""
    pin = config.add_pin(env / "runs")
    config.set_archived(pin.id, True)

    config.add_pin(env / "runs", unarchive=False)
    assert config.load_pins()[0].archived is True


def test_pins_predating_the_field_default_to_active(env):
    """An older pins.toml has no `archived` key at all."""
    config.config_path().write_text('[[pins]]\nid = "x"\npath = "/tmp"\ntitle = "old"\n')
    assert config.load_pins()[0].archived is False


# --- over HTTP -----------------------------------------------------------------


def test_archive_and_restore_routes(client, env):
    pin = config.add_pin(env / "runs", title="runs")

    assert client.post(f"/pins/{pin.id}/archive", follow_redirects=False).status_code == 303
    assert config.active_pins() == []
    assert "runs" in client.get("/").text, "archived pins still appear in the Archived section"

    assert client.post(f"/pins/{pin.id}/restore", follow_redirects=False).status_code == 303
    assert len(config.active_pins()) == 1


def test_archived_pin_is_still_readable(client, env):
    """A pin is the access scope; archiving is a display concern, not a security one."""
    (env / "runs/note.txt").write_text("still here")
    pin = config.add_pin(env / "runs")
    client.post(f"/pins/{pin.id}/archive")

    assert client.get(f"/raw/{pin.id}/note.txt").text == "still here"


def test_delete_is_still_available_and_permanent(client, env):
    pin = config.add_pin(env / "runs")
    client.post(f"/pins/{pin.id}/delete", follow_redirects=False)
    assert config.load_pins() == []


def test_archived_pins_are_excluded_from_tree_and_tags(client, env):
    (env / "other").mkdir()
    keep = config.add_pin(env / "runs", title="keep", tags=["t"])
    hide = config.add_pin(env / "other", title="hide", tags=["t"])
    client.post(f"/pins/{hide.id}/archive")

    html = client.get("/?view=tag").text
    # "hide" appears once, in the Archived section — not under its tag group.
    assert html.count(">hide</a>") == 1
    assert ">keep</a>" in html
    assert keep.id in html


# --- recent --------------------------------------------------------------------


def test_recent_section_lists_a_freshly_touched_pin(client, env):
    config.add_pin(env / "runs", title="fresh")
    assert "Recent activity" in client.get("/").text


def test_recent_section_absent_when_nothing_is_fresh(client, env):
    import os

    old = env / "old"
    old.mkdir()
    config.add_pin(old, title="stale")
    stale = os.stat(old).st_mtime - 40 * 3600
    os.utime(old, (stale, stale))

    assert "Recent activity" not in client.get("/").text


def test_scan_registers_manifest_pins(env):
    (env / "proj/logs").mkdir(parents=True)
    (env / "proj" / project.MANIFEST_NAME).write_text('title = "p"\npins = ["logs"]\n')

    specs, problems = project.collect([env])
    assert problems == []
    for spec in specs:
        config.add_pin(spec.path, title=spec.title, tags=spec.tags, unarchive=False)

    assert [p.title for p in config.active_pins()] == ["p · logs"]
