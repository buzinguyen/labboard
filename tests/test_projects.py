"""Project pins, cross-device rollup, and the dashboard routes.

The load-bearing claim is the first section: a project pin names a code checkout, and
labboard must serve nothing from it. If that ever regresses, pinning a project would
put source — and whatever a checkout dragged in — on the tailnet.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from labboard import board, config, tailnet, tasks
from labboard.app import create_app
from labboard.safety import AccessDenied, resolve


@pytest.fixture(autouse=True)
def no_real_tailnet(monkeypatch):
    """Never let a route test reach the actual tailnet.

    `/projects` gathers from peers, so without this a live workstation's real projects
    leak into assertions — which is exactly how these tests first went red. Returning no
    peers also exercises the standalone path, where `gather()` synthesizes a self node.
    """
    monkeypatch.setattr(tailnet, "discover", lambda: [])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "pins.toml"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    repo = tmp_path / "code" / "mjlab-go2"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "train.py").write_text("# proprietary\n")
    (repo / "docs" / "log" / "tasks").mkdir(parents=True)
    (repo / "docs" / "log" / "tasks" / "T007.md").write_text(
        "---\nid: T007\ntitle: Halve the action scale\nstatus: active\n"
        "runs: [E014]\nartifacts: [" + str(tmp_path / "artifacts" / "go2") + "]\n"
        "updated: 2026-08-01\n---\n## Question\nDoes it fix the loiter optimum?\n"
    )
    (repo / "docs" / "log" / "tasks" / "T006.md").write_text(
        "---\nid: T006\ntitle: Earlier thing\nstatus: done\nclosed: 2026-07-30\n---\n"
    )

    artifacts = tmp_path / "artifacts" / "go2"
    artifacts.mkdir(parents=True)
    (artifacts / "reward.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    project = config.add_pin(repo, kind=config.PROJECT, project="mjlab-go2", main=True)
    artifact = config.add_pin(artifacts, title="go2 runs", project="mjlab-go2")

    app = create_app()
    with TestClient(app) as c:
        c.project_pin = project
        c.artifact_pin = artifact
        c.repo = repo
        yield c


# --- a project pin serves nothing -------------------------------------------------


def test_resolve_refuses_a_project_pin_outright(client):
    for rel in ("", "src/train.py", "docs/log/tasks/T007.md"):
        with pytest.raises(AccessDenied):
            resolve(client.project_pin, rel)


@pytest.mark.parametrize(
    "url",
    [
        "/b/{id}",
        "/b/{id}/src/train.py",
        "/raw/{id}/src/train.py",
        "/thumb/{id}/src/train.py",
        "/video/{id}/src/train.py",
        "/nested-media/{id}",
        "/b/{id}/docs/log/tasks/T007.md",
    ],
)
def test_no_route_serves_bytes_from_a_project_pin(client, url):
    resp = client.get(url.format(id=client.project_pin.id))
    assert resp.status_code == 403, url
    assert "proprietary" not in resp.text


def test_the_source_tree_is_not_leaked_through_the_dashboard(client):
    assert "proprietary" not in client.get("/projects").text


# --- pins ------------------------------------------------------------------------


def test_project_and_artifact_pins_are_separable(client):
    assert client.project_pin.is_project
    assert not client.artifact_pin.is_project
    assert [p.id for p in config.project_pins()] == [client.project_pin.id]
    assert [p.id for p in config.artifact_pins()] == [client.artifact_pin.id]


def test_pins_written_before_this_feature_load_as_artifacts(tmp_path, monkeypatch):
    """An existing pins.toml has no `kind` key and must keep working untouched."""
    cfg = tmp_path / "pins.toml"
    cfg.write_text(
        '[[pins]]\nid = "abc"\npath = "/tmp"\ntitle = "old"\n'
    )
    monkeypatch.setenv(config.CONFIG_ENV, str(cfg))
    pin = config.load_pins()[0]
    assert pin.kind == config.ARTIFACT
    assert not pin.is_project


def test_an_unrecognized_kind_fails_closed(tmp_path, monkeypatch):
    cfg = tmp_path / "pins.toml"
    cfg.write_text('[[pins]]\nid = "abc"\npath = "/tmp"\ntitle = "x"\nkind = "future"\n')
    monkeypatch.setenv(config.CONFIG_ENV, str(cfg))
    # Unknown means "do not serve bytes", not "serve everything".
    assert config.load_pins()[0].is_project


# --- local view -------------------------------------------------------------------


def test_local_projects_reads_tickets_and_resolves_artifacts(client):
    views = board.local_projects()
    assert [v.slug for v in views] == ["mjlab-go2"]
    assert views[0].main is True
    ticket = next(t for t in views[0].tickets if t.id == "T007")
    assert ticket.status == "active"
    assert ticket.artifact_links[0]["url"] == f"/b/{client.artifact_pin.id}"


def test_api_node_publishes_projects(client):
    payload = client.get("/api/node").json()
    assert payload["projects"][0]["slug"] == "mjlab-go2"
    ids = [t["id"] for t in payload["projects"][0]["tickets"]]
    assert set(ids) == {"T006", "T007"}


def test_ticket_bodies_are_truncated_on_the_wire(client, monkeypatch):
    monkeypatch.setattr(tasks, "MAX_BODY_CHARS", 10)
    payload = client.get("/api/node").json()
    for ticket in payload["projects"][0]["tickets"]:
        assert len(ticket["body"]) <= 10


# --- rollup ----------------------------------------------------------------------


def _node(name, projects=None, pins=None, reachable=True):
    node = tailnet.Node(name=name, dns=f"{name}.ts.net", ip="", online=True)
    node.reachable = reachable
    node.projects = projects or []
    node.pins = pins or []
    return node


def _project(slug, main, tickets=(), receipts=()):
    return {
        "slug": slug, "title": slug, "main": main, "pin_id": "x", "path": "/p",
        "tickets": [t for t in tickets], "receipts": [r for r in receipts],
    }


def _ticket(tid, status="open", **kw):
    return {"id": tid, "title": tid, "status": status, **kw}


def test_tickets_come_only_from_the_main_device():
    main = _node("ws-3", [_project("p", True, [_ticket("T1", "active")])])
    other = _node("ws-4", [_project("p", False, [_ticket("STALE", "active")])])

    entry = board.rollup([main, other])[0]

    assert [t.id for t in entry.tickets] == ["T1"]
    assert entry.main_node == "ws-3"
    assert sorted(entry.nodes) == ["ws-3", "ws-4"]


def test_receipts_come_from_every_device():
    main = _node("ws-3", [_project("p", True, [_ticket("T1", "active")],
                                   [{"id": "R1", "title": "local", "status": "done"}])])
    other = _node("ws-4", [_project("p", False, receipts=[
        {"id": "R2", "title": "remote", "status": "done"}])])

    entry = board.rollup([main, other])[0]
    assert {r.id for r in entry.receipts} == {"R1", "R2"}
    assert {r.node for r in entry.receipts} == {"ws-3", "ws-4"}


def test_a_project_nobody_claims_is_flagged():
    entry = board.rollup([_node("ws-4", [_project("p", False)])])[0]
    assert entry.warnings and "no device claims" in entry.warnings[0]
    assert entry.health == "warn"


def test_two_devices_claiming_main_is_flagged():
    nodes = [_node("ws-3", [_project("p", True)]), _node("ws-4", [_project("p", True)])]
    entry = board.rollup(nodes)[0]
    assert any("claim main" in w for w in entry.warnings)


def test_more_than_one_active_ticket_is_flagged():
    node = _node("ws-3", [_project("p", True, [
        _ticket("T1", "active"), _ticket("T2", "active")])])
    entry = board.rollup([node])[0]
    assert any("one project, one ticket" in w for w in entry.warnings)
    # The dashboard still shows one, so the page stays usable while you fix it.
    assert entry.current.id == "T1"


def test_unreachable_nodes_contribute_nothing():
    assert board.rollup([_node("ws-3", [_project("p", True)], reachable=False)]) == []


def test_receipts_are_pending_until_acknowledged():
    receipts = [{"id": "R1", "title": "done", "status": "done"}]
    unacked = _node("a", [_project("p", True, [_ticket("T1", "active")], receipts)])
    assert len(board.rollup([unacked])[0].pending) == 1

    acked = _node("a", [_project("p", True,
                                 [_ticket("T1", "active", acked=["R1"])], receipts)])
    assert board.rollup([acked])[0].pending == []


def test_artifact_pins_attach_to_their_project():
    node = _node(
        "ws-3",
        [_project("p", True)],
        pins=[{"id": "abc", "title": "runs", "project": "p", "kind": "artifact",
               "exists": True}],
    )
    entry = board.rollup([node])[0]
    assert entry.artifact_pins[0]["url"] == "https://ws-3.ts.net/b/abc"


def test_artifact_links_are_absolutized_to_the_owning_node():
    node = _node("ws-3", [_project("p", True, [
        _ticket("T1", "active", artifact_links=[{"label": "x", "url": "/b/abc"}])])])
    entry = board.rollup([node])[0]
    assert entry.current.artifact_links[0]["url"] == "https://ws-3.ts.net/b/abc"


def test_staleness_is_measured_from_the_current_ticket():
    fresh = _node("a", [_project("p", True, [
        _ticket("T1", "active", updated="2026-08-03")])])
    entry = board.rollup([fresh])[0]
    assert entry.stale_days is not None

    old = _node("a", [_project("p", True, [_ticket("T1", "active", updated="2020-01-01")])])
    assert board.rollup([old])[0].is_stale


def test_projects_needing_attention_sort_first():
    nodes = [
        _node("a", [_project("quiet", True, [_ticket("T1", "active", updated="2026-08-03")])]),
        _node("b", [_project("broken", False)]),
    ]
    assert [r.slug for r in board.rollup(nodes)][0] == "broken"


# --- routes -----------------------------------------------------------------------


def test_dashboard_shows_the_project_and_its_current_ticket(client):
    resp = client.get("/projects")
    assert resp.status_code == 200
    assert "mjlab-go2" in resp.text
    assert "Halve the action scale" in resp.text
    # The done ticket is not the current one, so it stays off the card.
    assert "Earlier thing" not in resp.text


def test_dashboard_has_a_note_box_backed_by_local_storage_only(client):
    text = client.get("/projects").text
    assert 'data-project="mjlab-go2"' in text
    assert "localStorage" in text
    # No write route exists for notes; the browser is the only store.
    assert "/notes" not in text


def test_board_lists_every_ticket_and_filters(client):
    every = client.get("/board").text
    assert "T006" in every and "T007" in every

    live = client.get("/board?status=live").text
    assert "T007" in live and "T006" not in live

    done = client.get("/board?status=done").text
    assert "T006" in done and "T007" not in done


def test_board_filters_by_project(client):
    assert "T007" in client.get("/board?project=mjlab-go2").text
    assert "T007" not in client.get("/board?project=nope").text


def test_empty_dashboard_explains_how_to_add_a_project(tmp_path, monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "empty.toml"))
    with TestClient(create_app()) as c:
        assert "--project" in c.get("/projects").text


def test_still_no_write_routes_beyond_pin_management(client):
    """The read-only guarantee: notes are client-side precisely so this stays true."""
    posts = {
        r.path for r in client.app.routes
        if "POST" in getattr(r, "methods", set()) or "PUT" in getattr(r, "methods", set())
    }
    assert posts == {
        "/pins",
        "/pins/{pin_id}/archive",
        "/pins/{pin_id}/restore",
        "/pins/{pin_id}/delete",
    }


# --- the project page ------------------------------------------------------------


def test_project_page_shows_every_ticket_not_just_the_current_one(client):
    resp = client.get("/p/mjlab-go2")
    assert resp.status_code == 200
    # The dashboard card previews only the active ticket; this page is the full list.
    assert "Halve the action scale" in resp.text   # T007, active
    assert "Earlier thing" in resp.text            # T006, done
    assert "T006" in resp.text and "T007" in resp.text


def test_project_page_groups_tickets_by_status(client):
    text = client.get("/p/mjlab-go2").text
    assert text.index("active") < text.index("Halve the action scale")
    # Each status heading appears with its count.
    assert "(1)" in text


def test_project_page_anchors_each_ticket_for_deep_linking(client):
    """The dashboard preview links to /p/<slug>#T007, so the id must exist."""
    assert 'id="T007"' in client.get("/p/mjlab-go2").text


def test_unknown_project_is_a_404(client):
    assert client.get("/p/nope").status_code == 404


def test_project_page_does_not_leak_the_source_tree(client):
    assert "proprietary" not in client.get("/p/mjlab-go2").text


def test_project_page_carries_the_same_note_box(client):
    """Same localStorage key as the card, so it is one note, not two."""
    text = client.get("/p/mjlab-go2").text
    assert 'data-project="mjlab-go2"' in text
    assert "localStorage" in text


def test_project_page_renders_ticket_bodies(client):
    assert "Does it fix the loiter optimum?" in client.get("/p/mjlab-go2").text


# --- the dashboard is a grid of links --------------------------------------------


def test_dashboard_cards_link_into_the_project(client):
    text = client.get("/projects").text
    assert 'class="proj-grid"' in text
    assert 'href="/p/mjlab-go2"' in text


def test_dashboard_preview_links_to_the_current_ticket_anchor(client):
    assert 'href="/p/mjlab-go2#T007"' in client.get("/projects").text


def test_the_note_script_is_emitted_once_per_page(client):
    """The macro renders per card; the script must not be duplicated per card."""
    assert client.get("/projects").text.count("labboard.note.") == 1


# --- navigation between the two views --------------------------------------------


def test_table_view_links_back_to_the_project(client):
    """The project page offers Table view; the table must offer the way back."""
    text = client.get("/board?project=mjlab-go2").text
    assert 'href="/p/mjlab-go2"' in text
    assert "Back to mjlab-go2" in text


def test_table_view_ticket_ids_link_to_their_anchor(client):
    assert 'href="/p/mjlab-go2#T007"' in client.get("/board?project=mjlab-go2").text


def test_unfiltered_board_has_no_back_link_to_nowhere(client):
    assert "Back to" not in client.get("/board").text


def test_table_view_carries_the_project_note(client):
    """Same localStorage key, so it is one note across card, project page and table."""
    text = client.get("/board?project=mjlab-go2").text
    assert 'data-project="mjlab-go2"' in text
    assert text.count("labboard.note.") == 1


def test_unfiltered_board_shows_no_note_box(client):
    """With every project listed there is no single note to show."""
    text = client.get("/board").text
    # Assert on rendered markup, not on class names — those also appear in the
    # stylesheet, where their presence means nothing.
    assert "data-project=" not in text
    assert "labboard.note." not in text


def test_the_note_column_is_plain_so_it_aligns_with_the_content(client):
    """Panel chrome pushed the note a border-plus-padding below the left column."""
    for url in ("/p/mjlab-go2", "/board?project=mjlab-go2"):
        assert 'class="split-side plain"' in client.get(url).text, url
