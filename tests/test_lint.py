"""Ticket validation.

These checks are the only thing standing between "the convention says to write tickets
this way" and an agent quietly not doing it, so they need to fire on the real mistakes
and stay quiet otherwise — a linter nobody trusts gets ignored.
"""

from __future__ import annotations

from datetime import date, timedelta

from labboard import lint, tasks


def _ticket(tid="T001", **kw):
    kw.setdefault("status", "open")
    kw.setdefault("updated", date.today().isoformat())
    kw.setdefault("filename", f"{tid}-slug.md")
    kw.setdefault("body", "## Question\nq\n## Done when\nmetric < 0.05\n")
    return tasks.Task(id=tid, title=kw.pop("title", "t"), **kw)


def _messages(problems, level=None):
    return " | ".join(p.message for p in problems if level is None or p.level == level)


def test_a_well_formed_ticket_produces_nothing():
    assert lint.check([_ticket(status="active")], []) == []


def test_two_active_tickets_is_an_error():
    problems = lint.check(
        [_ticket("T001", status="active"), _ticket("T002", status="active")], []
    )
    assert "one ticket at a time" in _messages(problems, lint.ERROR)


def test_duplicate_ids_are_an_error():
    problems = lint.check([_ticket("T001"), _ticket("T001")], [])
    assert "duplicate id" in _messages(problems, lint.ERROR)


def test_missing_updated_is_an_error():
    assert "no `updated:`" in _messages(lint.check([_ticket(updated="")], []), lint.ERROR)


def test_unparseable_updated_is_an_error():
    problems = lint.check([_ticket(updated="last tuesday")], [])
    assert "not an ISO date" in _messages(problems, lint.ERROR)


def test_a_future_updated_is_an_error():
    ahead = (date.today() + timedelta(days=3)).isoformat()
    assert "in the future" in _messages(lint.check([_ticket(updated=ahead)], []), lint.ERROR)


def test_done_without_a_closed_date_is_an_error():
    problems = lint.check([_ticket(status="done", runs=["E1"], body="## Results\nx")], [])
    assert "`closed:` is empty" in _messages(problems, lint.ERROR)


def test_a_closed_date_on_a_live_ticket_is_a_warning():
    problems = lint.check([_ticket(status="active", closed="2026-08-01")], [])
    assert "but status is `active`" in _messages(problems, lint.WARN)


def test_done_without_results_or_runs_warns():
    problems = lint.check(
        [_ticket(status="done", closed="2026-08-01", body="## Question\nq\n")], []
    )
    assert "no `## Results`" in _messages(problems, lint.WARN)
    assert "which experiment answered it" in _messages(problems, lint.WARN)


def test_a_live_ticket_without_done_when_warns():
    problems = lint.check([_ticket(status="active", body="## Question\njust asking\n")], [])
    assert "no `## Done when`" in _messages(problems, lint.WARN)


def test_an_empty_body_warns():
    assert "empty body" in _messages(lint.check([_ticket(body="   ")], []), lint.WARN)


def test_a_filename_that_does_not_match_the_id_warns():
    problems = lint.check([_ticket("T001", filename="T009-wrong.md")], [])
    assert "does not match filename" in _messages(problems, lint.WARN)


def test_no_active_ticket_at_all_warns():
    assert "no ticket is active" in _messages(lint.check([_ticket(status="open")], []))


def test_a_finished_project_does_not_nag_about_an_active_ticket():
    """Every ticket done is a finished project, not a mistake."""
    done = _ticket(status="done", closed="2026-08-01", runs=["E1"], body="## Results\nx")
    assert lint.check([done], []) == []


# --- artifacts ------------------------------------------------------------------


def test_an_artifact_outside_every_pin_warns(tmp_path):
    real = tmp_path / "run"
    real.mkdir()
    ticket = _ticket(status="active", artifacts=[str(real)],
                     artifact_links=[{"label": str(real), "url": None}])
    problems = lint.check([ticket], [])
    assert "not under any artifact pin" in _messages(problems, lint.WARN)


def test_a_pinned_artifact_that_does_not_exist_warns(tmp_path):
    missing = tmp_path / "gone"
    ticket = _ticket(status="active", artifacts=[str(missing)],
                     artifact_links=[{"label": str(missing), "url": "/b/abc"}])
    problems = lint.check([ticket], [])
    assert "does not exist on disk" in _messages(problems, lint.WARN)


def test_a_pinned_artifact_that_exists_is_silent(tmp_path):
    real = tmp_path / "run"
    real.mkdir()
    ticket = _ticket(status="active", artifacts=[str(real)],
                     artifact_links=[{"label": str(real), "url": "/b/abc"}])
    assert lint.check([ticket], []) == []


# --- receipts -------------------------------------------------------------------


def test_a_receipt_naming_no_ticket_is_an_error():
    receipt = tasks.Task(id="R1", title="x", status="done", kind="receipt", run="E1")
    assert "names no `task:`" in _messages(lint.check([], [receipt]), lint.ERROR)


def test_a_receipt_pointing_at_a_missing_ticket_is_an_error():
    receipt = tasks.Task(id="R1", title="x", status="done", kind="receipt",
                         task="T404", run="E1")
    problems = lint.check([_ticket("T001")], [receipt])
    assert "not a ticket here" in _messages(problems, lint.ERROR)


def test_a_receipt_without_a_run_warns():
    receipt = tasks.Task(id="R1", title="x", status="done", kind="receipt", task="T001")
    problems = lint.check([_ticket("T001", status="active")], [receipt])
    assert "names no `run:`" in _messages(problems, lint.WARN)


# --- shape ------------------------------------------------------------------------


def test_errors_sort_before_warnings():
    problems = lint.check([_ticket(updated="", body="")], [])
    assert problems[0].level == lint.ERROR


def test_summarize_counts_both_levels():
    problems = lint.check([_ticket(updated="", body="")], [])
    errors, warnings = lint.summarize(problems)
    assert errors >= 1 and warnings >= 1


def test_problems_survive_a_round_trip_over_the_wire():
    original = lint.check([_ticket(updated="")], [])[0]
    assert lint.from_dict(original.to_dict()) == original
