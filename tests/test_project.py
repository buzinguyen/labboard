"""Tests for manifest discovery and parsing."""

from __future__ import annotations

import pytest

from labboard import project
from labboard.project import ManifestError, collect, find_manifests, read_manifest


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "proj/logs").mkdir(parents=True)
    (tmp_path / "proj/outputs/eval").mkdir(parents=True)
    (tmp_path / "proj/labboard.toml").write_text(
        """
title = "safe_mjlab_zoo"
tags = ["mjlab"]
pins = ["logs"]

[[pin]]
path = "outputs/eval"
title = "eval sweeps"
tags = ["go2"]
"""
    )
    return tmp_path


# --- parsing -------------------------------------------------------------------


def test_shorthand_and_long_form_both_parse(repo):
    specs = read_manifest(repo / "proj/labboard.toml").pins
    assert len(specs) == 2

    by_name = {s.path.name: s for s in specs}
    assert by_name["logs"].title == "safe_mjlab_zoo · logs"
    assert by_name["logs"].tags == ["mjlab"]

    assert by_name["eval"].title == "safe_mjlab_zoo · eval sweeps"
    assert by_name["eval"].tags == ["go2", "mjlab"]  # file defaults merged in


def test_paths_resolve_relative_to_the_manifest(repo):
    specs = read_manifest(repo / "proj/labboard.toml").pins
    assert {s.path for s in specs} == {
        repo / "proj/logs",
        repo / "proj/outputs/eval",
    }


def test_absolute_paths_are_rejected(tmp_path):
    (tmp_path / "labboard.toml").write_text('pins = ["/etc"]\n')
    with pytest.raises(ManifestError, match="relative"):
        read_manifest(tmp_path / "labboard.toml")


def test_paths_escaping_the_project_are_rejected(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj/labboard.toml").write_text('pins = ["../../elsewhere"]\n')
    with pytest.raises(ManifestError, match="escapes"):
        read_manifest(tmp_path / "proj/labboard.toml")


def test_manifest_declaring_nothing_is_an_error(tmp_path):
    (tmp_path / "labboard.toml").write_text('title = "x"\n')
    with pytest.raises(ManifestError, match="neither"):
        read_manifest(tmp_path / "labboard.toml")


def test_malformed_toml_is_an_error(tmp_path):
    (tmp_path / "labboard.toml").write_text("pins = [\n")
    with pytest.raises(ManifestError):
        read_manifest(tmp_path / "labboard.toml")


def test_pin_entry_without_a_path_is_an_error(tmp_path):
    (tmp_path / "labboard.toml").write_text('[[pin]]\ntitle = "no path"\n')
    with pytest.raises(ManifestError, match="needs a `path`"):
        read_manifest(tmp_path / "labboard.toml")


# --- discovery -----------------------------------------------------------------


def test_finds_nested_manifests(repo):
    (repo / "other/deep").mkdir(parents=True)
    (repo / "other/deep/labboard.toml").write_text('pins = ["."]\n')
    found = find_manifests(repo)
    assert set(found) == {repo / "proj/labboard.toml", repo / "other/deep/labboard.toml"}


def test_does_not_recurse_past_a_manifest(repo):
    """A manifest owns its project; a nested one would fight over the same dirs."""
    (repo / "proj/sub").mkdir()
    (repo / "proj/sub/labboard.toml").write_text('pins = ["."]\n')
    assert find_manifests(repo) == [repo / "proj/labboard.toml"]


def test_depth_limit_is_honoured(repo):
    assert find_manifests(repo, max_depth=0) == []


def test_noisy_directories_are_skipped(repo):
    for junk in (".git", "node_modules", "wandb", ".venv"):
        (repo / junk).mkdir()
        (repo / junk / "labboard.toml").write_text('pins = ["."]\n')
    assert find_manifests(repo) == [repo / "proj/labboard.toml"]


# --- collect -------------------------------------------------------------------


def test_collect_reports_problems_without_aborting(repo):
    (repo / "broken").mkdir()
    (repo / "broken/labboard.toml").write_text("pins = [\n")

    specs, problems = collect([repo])
    assert len(specs) == 2, "the good manifest must still be collected"
    assert len(problems) == 1


def test_collect_deduplicates_the_same_directory(repo):
    specs, _ = collect([repo, repo])
    assert len(specs) == 2


def test_collect_on_a_missing_root_is_reported_not_raised(tmp_path):
    specs, problems = collect([tmp_path / "nope"])
    assert specs == []
    assert problems and "not a directory" in problems[0]


def test_spec_exists_reflects_the_filesystem(repo):
    (repo / "proj/labboard.toml").write_text('pins = ["logs", "never-created"]\n')
    specs = {s.path.name: s for s in read_manifest(repo / "proj/labboard.toml").pins}
    assert specs["logs"].exists
    assert not specs["never-created"].exists


def test_manifest_name_is_stable():
    assert project.MANIFEST_NAME == "labboard.toml"


# --- project declarations (main is a hostname, because the manifest is committed) ---


def test_a_manifest_can_declare_a_project_pin(tmp_path):
    (tmp_path / "labboard.toml").write_text(
        'project = "mjlab-go2"\nmain = "ws-3"\ntitle = "Go2"\npins = ["outputs"]\n'
    )
    (tmp_path / "outputs").mkdir()

    manifest = read_manifest(tmp_path / "labboard.toml", hostname="ws-3")

    assert manifest.project.kind == "project"
    assert manifest.project.project == "mjlab-go2"
    assert manifest.project.path == tmp_path.resolve()
    assert manifest.project.main is True
    # The artifact pin inherits the slug, so the dashboard can group them.
    assert [p.project for p in manifest.pins] == ["mjlab-go2"]
    assert manifest.pins[0].kind == "artifact"


def test_main_is_false_on_a_device_the_manifest_does_not_name(tmp_path):
    """The same committed file must not make every checkout claim ownership."""
    (tmp_path / "labboard.toml").write_text('project = "p"\nmain = "ws-3"\n')
    assert read_manifest(tmp_path / "labboard.toml", hostname="ws-4").project.main is False
    assert read_manifest(tmp_path / "labboard.toml", hostname="ws-3").project.main is True


def test_main_matches_the_bare_host_of_an_fqdn(tmp_path):
    (tmp_path / "labboard.toml").write_text('project = "p"\nmain = "ws-3"\n')
    got = read_manifest(tmp_path / "labboard.toml", hostname="ws-3.local")
    assert got.project.main is True


def test_a_literal_true_still_works_for_an_unshared_manifest(tmp_path):
    (tmp_path / "labboard.toml").write_text('project = "p"\nmain = true\n')
    assert read_manifest(tmp_path / "labboard.toml", hostname="anything").project.main is True


def test_a_manifest_may_declare_only_a_project(tmp_path):
    (tmp_path / "labboard.toml").write_text('project = "p"\n')
    manifest = read_manifest(tmp_path / "labboard.toml", hostname="h")
    assert manifest.pins == []
    assert manifest.specs == [manifest.project]


def test_a_path_shaped_slug_is_refused(tmp_path):
    (tmp_path / "labboard.toml").write_text('project = "../etc"\n')
    with pytest.raises(ManifestError, match="plain slug"):
        read_manifest(tmp_path / "labboard.toml", hostname="h")
