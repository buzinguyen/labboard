"""Autolinking in ticket bodies: what becomes a link, and what must not."""

from __future__ import annotations

from labboard.render import render_ticket

REFS = [
    {"text": "~/artifacts/go2/E014", "url": "https://ws-3.ts.net/b/abc/E014"},
    {"text": "E014", "url": "https://ws-3.ts.net/b/abc/E014"},
]


def test_a_resolved_path_becomes_a_link():
    html = render_ticket("results in ~/artifacts/go2/E014 now", REFS)
    assert '<a href="https://ws-3.ts.net/b/abc/E014" class="xref">~/artifacts/go2/E014</a>' in html


def test_a_run_id_becomes_a_link():
    assert 'class="xref">E014</a>' in render_ticket("E014 converged", REFS)


def test_a_ticket_id_links_to_its_anchor_on_the_project_page():
    html = render_ticket("supersedes T006", [], ["T006"], "mjlab-go2")
    assert '<a href="/p/mjlab-go2#T006" class="xref">T006</a>' in html


def test_an_unknown_ticket_id_stays_prose():
    html = render_ticket("supersedes T999", [], ["T006"], "mjlab-go2")
    assert "<a" not in html and "T999" in html


def test_a_bare_url_is_linkified():
    html = render_ticket("run at https://wandb.ai/x/y/runs/abc")
    assert '<a href="https://wandb.ai/x/y/runs/abc"' in html


def test_a_path_inside_a_code_span_is_left_alone():
    """Code is quoted deliberately; rewriting it would be wrong."""
    html = render_ticket("use `~/artifacts/go2/E014` as the flag", REFS)
    assert "<code>~/artifacts/go2/E014</code>" in html
    assert 'class="xref"' not in html


def test_a_path_inside_a_fenced_block_is_left_alone():
    html = render_ticket("```\ncp ~/artifacts/go2/E014 .\n```", REFS)
    assert 'class="xref"' not in html


def test_an_explicit_link_keeps_its_own_target():
    html = render_ticket("[the run](https://example.com/other) here", REFS + [
        {"text": "the run", "url": "https://ws-3.ts.net/b/abc"}])
    assert 'href="https://example.com/other"' in html
    assert 'href="https://ws-3.ts.net/b/abc"' not in html


def test_the_longest_matching_path_wins():
    refs = [
        {"text": "~/artifacts/go2", "url": "/b/abc"},
        {"text": "~/artifacts/go2/E014", "url": "/b/abc/E014"},
    ]
    html = render_ticket("in ~/artifacts/go2/E014 today", refs)
    assert 'href="/b/abc/E014"' in html
    assert 'href="/b/abc"' not in html


def test_a_ticket_id_inside_a_longer_word_is_not_linked():
    html = render_ticket("XT006Y and T006S are not refs", [], ["T006"], "p")
    assert "<a" not in html


def test_text_around_a_link_is_preserved():
    html = render_ticket("before E014 after", REFS)
    assert "before " in html and " after" in html


def test_raw_html_in_a_ticket_is_escaped():
    """Bodies arrive from another node; they are prose, not markup."""
    html = render_ticket("<script>alert(1)</script>")
    assert "<script>" not in html


def test_no_refs_renders_normally():
    assert "<p>plain prose</p>" in render_ticket("plain prose")
