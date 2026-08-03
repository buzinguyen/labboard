"""Ticket reading: frontmatter, discovery bounds, symlink containment, artifact links.

The security-relevant claim here is narrower than in `test_safety.py` — no user input
reaches these paths — but a project pin points at a code checkout, so the module must
never read outside the repo it was given even when the repo itself contains links.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from labboard import config, tasks


# --- frontmatter ---------------------------------------------------------------


def test_scalars_inline_lists_and_block_lists_all_parse():
    meta, body = tasks.parse_frontmatter(
        "---\n"
        "id: T007\n"
        "title: Does halving action scale fix the loiter optimum?\n"
        "tags: [go2, reward]\n"
        "artifacts:\n"
        "  - ~/artifacts/go2/E014\n"
        "  - ~/artifacts/go2/E015\n"
        "---\n"
        "## Question\n"
    )
    assert meta["id"] == "T007"
    assert meta["title"].endswith("loiter optimum?")  # a colon in a value is not a split
    assert meta["tags"] == ["go2", "reward"]
    assert meta["artifacts"] == ["~/artifacts/go2/E014", "~/artifacts/go2/E015"]
    assert body.startswith("## Question")


def test_quotes_are_stripped_and_comments_ignored():
    meta, _ = tasks.parse_frontmatter('---\n# a note\nid: "T1"\ntitle: \'x\'\n---\n')
    assert meta == {"id": "T1", "title": "x"}


def test_empty_inline_list_is_empty_not_a_string():
    meta, _ = tasks.parse_frontmatter("---\ntags: []\n---\n")
    assert meta["tags"] == []


def test_a_file_without_frontmatter_is_all_body():
    meta, body = tasks.parse_frontmatter("# just markdown\n")
    assert meta == {}
    assert body == "# just markdown\n"


def test_unterminated_frontmatter_does_not_swallow_the_file():
    # Better to show a ticket with no metadata than to lose it from the board.
    meta, body = tasks.parse_frontmatter("---\nid: T1\ntitle: oops\n")
    assert meta == {}
    assert "oops" in body


# --- ticket files ---------------------------------------------------------------


def _write(root, name, text):
    path = tasks.tasks_dir(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_tickets_and_receipts_are_read_separately(tmp_path):
    _write(tmp_path, "T001-first.md", "---\nid: T001\ntitle: First\nstatus: active\n---\nbody\n")
    _write(tmp_path, "outbox/R-ws3-e014.md",
           "---\nid: R-ws3-e014\ntask: T001\nrun: E014\ntitle: converged\n---\n")

    tickets, receipts = tasks.load_tasks(tmp_path, "proj")

    assert [t.id for t in tickets] == ["T001"]
    assert tickets[0].kind == "task"
    assert tickets[0].project == "proj"
    assert [r.id for r in receipts] == ["R-ws3-e014"]
    assert receipts[0].kind == "receipt"
    assert receipts[0].task == "T001"


def test_unknown_status_falls_back_to_open(tmp_path):
    _write(tmp_path, "T1.md", "---\nid: T1\nstatus: wibble\n---\n")
    tickets, _ = tasks.load_tasks(tmp_path)
    assert tickets[0].status == "open"


def test_missing_id_and_title_fall_back_to_the_filename(tmp_path):
    _write(tmp_path, "T042-something.md", "---\nstatus: open\n---\nbody\n")
    tickets, _ = tasks.load_tasks(tmp_path)
    assert tickets[0].id == "T042-something"
    assert tickets[0].title == "T042-something"


def test_tickets_sort_by_what_needs_attention(tmp_path):
    for name, status in (("a", "done"), ("b", "open"), ("c", "active"), ("d", "blocked")):
        _write(tmp_path, f"{name}.md", f"---\nid: {name}\nstatus: {status}\n---\n")
    tickets, _ = tasks.load_tasks(tmp_path)
    assert [t.id for t in tickets] == ["c", "d", "b", "a"]


def test_non_markdown_and_hidden_files_are_ignored(tmp_path):
    _write(tmp_path, "T1.md", "---\nid: T1\n---\n")
    _write(tmp_path, "notes.txt", "not a ticket")
    _write(tmp_path, ".draft.md", "---\nid: hidden\n---\n")
    tickets, _ = tasks.load_tasks(tmp_path)
    assert [t.id for t in tickets] == ["T1"]


def test_missing_tasks_directory_is_not_an_error(tmp_path):
    assert tasks.load_tasks(tmp_path) == ([], [])


def test_an_oversized_file_is_skipped(tmp_path):
    _write(tmp_path, "huge.md", "x" * (tasks.MAX_TASK_BYTES + 1))
    _write(tmp_path, "T1.md", "---\nid: T1\n---\n")
    tickets, _ = tasks.load_tasks(tmp_path)
    assert [t.id for t in tickets] == ["T1"]


def test_the_ticket_count_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "MAX_TASKS", 3)
    for i in range(10):
        _write(tmp_path, f"T{i:03d}.md", f"---\nid: T{i:03d}\n---\n")
    tickets, _ = tasks.load_tasks(tmp_path)
    assert len(tickets) == 3


# --- containment ----------------------------------------------------------------


def test_a_symlinked_tasks_directory_is_refused(tmp_path):
    """`docs/log/tasks` pointing outside the repo must read nothing."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "T1.md").write_text("---\nid: leaked\n---\n")

    repo = tmp_path / "repo"
    (repo / "docs" / "log").mkdir(parents=True)
    (repo / "docs" / "log" / "tasks").symlink_to(elsewhere)

    assert tasks.load_tasks(repo) == ([], [])


def test_a_ticket_symlinked_from_outside_is_dropped(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("---\nid: leaked\n---\n")

    repo = tmp_path / "repo"
    _write(repo, "T1.md", "---\nid: T1\n---\n")
    (tasks.tasks_dir(repo) / "linked.md").symlink_to(outside / "secret.md")

    tickets, _ = tasks.load_tasks(repo)
    assert [t.id for t in tickets] == ["T1"]


# --- linking results back --------------------------------------------------------


@pytest.fixture
def pins(tmp_path, monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "pins.toml"))
    root = tmp_path / "artifacts"
    (root / "go2" / "E014").mkdir(parents=True)
    outer = config.add_pin(root, title="artifacts")
    inner = config.add_pin(root / "go2", title="go2")
    return {"root": root, "outer": outer, "inner": inner}


def test_artifact_under_a_pin_resolves_to_a_browse_url(pins):
    url = tasks.artifact_url(str(pins["root"] / "go2" / "E014"), config.artifact_pins())
    # Most specific pin wins: the go2 pin contains it, so link through that one.
    assert url == f"/b/{pins['inner'].id}/E014"


def test_the_pin_root_itself_resolves_without_a_trailing_path(pins):
    assert tasks.artifact_url(str(pins["root"]), [pins["outer"]]) == f"/b/{pins['outer'].id}"


def test_an_unpinned_path_has_no_url(pins, tmp_path):
    assert tasks.artifact_url(str(tmp_path / "nowhere"), config.artifact_pins()) is None


def test_project_pins_never_provide_artifact_urls(tmp_path, monkeypatch):
    """A project pin serves no bytes, so it must never be offered as a results link."""
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "pins.toml"))
    repo = tmp_path / "repo"
    (repo / "logs").mkdir(parents=True)
    pin = config.add_pin(repo, kind=config.PROJECT, project="p")
    assert tasks.artifact_url(str(repo / "logs"), [pin]) is None


def test_attach_links_labels_unresolvable_paths(pins):
    task = tasks.Task(id="T1", title="t", status="open",
                      artifacts=[str(pins["root"] / "go2"), "/nope/missing"])
    tasks.attach_links(task, config.artifact_pins())
    assert task.artifact_links[0]["url"] is not None
    assert task.artifact_links[1]["url"] is None
    assert task.artifact_links[1]["label"] == "/nope/missing"


# --- dates -----------------------------------------------------------------------


def test_days_since_counts_whole_days():
    assert tasks.days_since(date.today().isoformat()) == 0
    assert tasks.days_since((date.today() - timedelta(days=5)).isoformat()) == 5


def test_days_since_tolerates_a_full_timestamp_and_rejects_junk():
    assert tasks.days_since(date.today().isoformat() + "T14:30:00Z") == 0
    assert tasks.days_since("not a date") is None
    assert tasks.days_since("") is None


# --- card preview ----------------------------------------------------------------


def test_preview_skips_headings_and_blank_lines():
    """A ticket opens with `## Question`; that word previews every ticket identically."""
    task = tasks.Task(id="T1", title="t", status="open",
                      body="## Question\n\nDoes halving the action scale help?\n")
    assert task.preview == "Does halving the action scale help?"


def test_preview_strips_list_and_quote_markers():
    task = tasks.Task(id="T1", title="t", status="open", body="# H\n- first bullet\n")
    assert task.preview == "first bullet"


def test_preview_of_a_headings_only_body_is_empty():
    task = tasks.Task(id="T1", title="t", status="open", body="## Question\n## Results\n")
    assert task.preview == ""


# --- clickable references in the body --------------------------------------------


def test_a_path_under_a_pin_becomes_a_ref(pins):
    task = tasks.Task(id="T1", title="t", status="open",
                      body=f"Results landed in {pins['root']}/go2/E014 overnight.")
    refs = tasks.find_refs(task, config.artifact_pins())
    assert [r["text"] for r in refs] == [f"{pins['root']}/go2/E014"]
    assert refs[0]["url"].startswith("/b/")


def test_a_path_outside_every_pin_is_not_a_ref(pins, tmp_path):
    task = tasks.Task(id="T1", title="t", status="open",
                      body=f"see {tmp_path}/elsewhere/thing for the raw logs")
    assert tasks.find_refs(task, config.artifact_pins()) == []


def test_prose_that_merely_contains_a_slash_is_not_a_path(pins):
    task = tasks.Task(id="T1", title="t", status="open",
                      body="either/or, and 3/4 of runs; see and/or")
    assert tasks.find_refs(task, config.artifact_pins()) == []


def test_trailing_punctuation_is_not_part_of_the_path(pins):
    task = tasks.Task(id="T1", title="t", status="open",
                      body=f"landed in {pins['root']}/go2/E014.")
    refs = tasks.find_refs(task, config.artifact_pins())
    assert refs and not refs[0]["text"].endswith(".")


def test_a_run_id_resolves_through_the_tickets_own_artifacts(pins):
    """`E014` in prose should reach the same place as the E014 in `artifacts:`."""
    task = tasks.Task(id="T1", title="t", status="open",
                      artifacts=[f"{pins['root']}/go2/E014"],
                      body="E014 converged at 18k steps.")
    refs = {r["text"]: r["url"] for r in tasks.find_refs(task, config.artifact_pins())}
    assert "E014" in refs and refs["E014"].startswith("/b/")


def test_a_run_id_with_no_matching_artifact_is_not_a_ref(pins):
    task = tasks.Task(id="T1", title="t", status="open", body="E099 is queued.")
    assert [r["text"] for r in tasks.find_refs(task, config.artifact_pins())] == []


def test_refs_are_deduplicated(pins):
    task = tasks.Task(id="T1", title="t", status="open",
                      body=f"{pins['root']}/go2 twice: {pins['root']}/go2")
    assert len(tasks.find_refs(task, config.artifact_pins())) == 1
