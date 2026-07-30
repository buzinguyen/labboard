"""Tests for pin grouping."""

from __future__ import annotations

from labboard.config import Pin
from labboard.organize import UNTAGGED, all_tags, flatten, group_by_tag, pin_tree


def mk(path: str, title: str = "", tags: list[str] | None = None) -> Pin:
    return Pin(id=path, path=path, title=title or path.rsplit("/", 1)[-1], tags=tags or [])


def labels(nodes) -> list[str]:
    return [n.label for n in nodes]


# --- prefix grouping -----------------------------------------------------------


def test_siblings_share_their_parent():
    """d and e are distinct directories, so they stay as children of the shared /a/b/c."""
    tree = pin_tree([mk("/a/b/c/d"), mk("/a/b/c/e")])
    assert labels(tree) == ["/a/b/c"]
    assert labels(tree[0].children) == ["d", "e"]
    assert tree[0].pins == []
    assert tree[0].pin_count == 2


def test_divergent_paths_meet_at_the_real_common_prefix():
    """/a/b/c/d and /a/c/b/e share only /a — components, never string overlap."""
    tree = pin_tree([mk("/a/b/c/d"), mk("/a/c/b/e")])
    assert labels(tree) == ["/a"]
    root = tree[0]
    assert root.pins == []
    assert labels(root.children) == ["b/c/d", "c/b/e"]


def test_single_child_chains_are_collapsed():
    tree = pin_tree([mk("/home/buzi/Desktop/RESEARCH/WAYMO/OUTPUT")])
    assert labels(tree) == ["/home/buzi/Desktop/RESEARCH/WAYMO/OUTPUT"]


def test_pin_that_is_also_a_parent_of_another_pin():
    tree = pin_tree([mk("/a/b"), mk("/a/b/c")])
    assert labels(tree) == ["/a/b"]
    node = tree[0]
    assert [p.path for p in node.pins] == ["/a/b"]
    assert labels(node.children) == ["c"]
    assert [p.path for p in node.children[0].pins] == ["/a/b/c"]


def test_three_way_split():
    tree = pin_tree([mk("/r/x/1"), mk("/r/x/2"), mk("/r/y/1")])
    root = tree[0]
    assert root.label == "/r"
    # x genuinely branches so it stays a group; y has a single descendant, so the
    # chain collapses to "y/1" rather than adding a level that says nothing.
    assert labels(root.children) == ["x", "y/1"]
    assert root.pin_count == 3


def test_unrelated_roots_stay_separate():
    tree = pin_tree([mk("/home/a/run"), mk("/mnt/data/run")])
    assert labels(tree) == ["/home/a/run", "/mnt/data/run"]


def test_pin_count_counts_the_whole_subtree():
    tree = pin_tree([mk("/a/b/1"), mk("/a/b/2"), mk("/a/c/3")])
    assert tree[0].pin_count == 3


def test_empty():
    assert pin_tree([]) == []


def test_flatten_is_depth_first_with_depths():
    tree = pin_tree([mk("/r/x/1"), mk("/r/y/2")])
    flat = flatten(tree)
    assert [(d, n.label) for d, n in flat] == [(0, "/r"), (1, "x/1"), (1, "y/2")]


def test_children_sorted_case_insensitively():
    tree = pin_tree([mk("/r/Zeta/1"), mk("/r/alpha/2")])
    assert labels(tree[0].children) == ["alpha/2", "Zeta/1"]


# --- tags ----------------------------------------------------------------------


def test_group_by_tag_lists_a_pin_under_each_of_its_tags():
    pins = [mk("/a", tags=["mjlab", "go2"]), mk("/b", tags=["mjlab"])]
    groups = dict(group_by_tag(pins))
    assert {p.path for p in groups["mjlab"]} == {"/a", "/b"}
    assert {p.path for p in groups["go2"]} == {"/a"}


def test_untagged_pins_are_grouped_and_sorted_last():
    groups = group_by_tag([mk("/a", tags=["z"]), mk("/b")])
    assert [tag for tag, _ in groups] == ["z", UNTAGGED]


def test_all_tags_is_deduped_and_sorted():
    assert all_tags([mk("/a", tags=["b", "A"]), mk("/c", tags=["A"])]) == ["A", "b"]
