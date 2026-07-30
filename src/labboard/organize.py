"""Ways to structure the pin list once there are too many to scroll.

The flat list works fine at a dozen pins and collapses at a hundred. These are the
alternative shapes the Pins page can render: a filesystem tree built from shared path
prefixes, and a grouping by tag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Pin

UNTAGGED = "untagged"


@dataclass
class Node:
    """A directory prefix shared by one or more pins."""

    label: str  # the segment(s) this node covers, e.g. "Desktop/RESEARCH"
    path: str  # absolute path of the prefix
    pins: list[Pin] = field(default_factory=list)  # pins sitting exactly here
    children: list["Node"] = field(default_factory=list)

    @property
    def pin_count(self) -> int:
        """Pins at or below this node — what the group header shows."""
        return len(self.pins) + sum(c.pin_count for c in self.children)

    @property
    def activity(self) -> float:
        """Most recent activity anywhere in this subtree, for sorting."""
        best = max((p.activity for p in self.pins), default=0.0)
        return max([best, *(c.activity for c in self.children)])


class _Builder:
    __slots__ = ("children", "pins")

    def __init__(self) -> None:
        self.children: dict[str, _Builder] = {}
        self.pins: list[Pin] = []


def pin_tree(pins: list[Pin]) -> list[Node]:
    """Group pins into a tree by their longest shared path prefixes.

    `/a/b/c/d` and `/a/b/c/e` nest under a single `/a/b/c` group. `/a/b/c/d` and
    `/a/c/b/e` share only `/a`, so that is where they meet — the common prefix is
    computed per path *component*, never by string overlap.

    Chains of single-child directories are collapsed into one node ("Desktop/RESEARCH"
    rather than "Desktop" → "RESEARCH"), so the tree only branches where pins actually
    diverge.
    """
    root = _Builder()
    for pin in pins:
        node = root
        for part in Path(pin.path).parts:
            node = node.children.setdefault(part, _Builder())
        node.pins.append(pin)

    tree = [_convert(name, child, name) for name, child in _sorted(root.children)]

    # Pins under unrelated roots (/home/... and /mnt/...) would otherwise all hang off a
    # bare "/" node that carries no information. Drop it and promote its children, whose
    # labels become their absolute paths so they still read as roots.
    if len(tree) == 1 and tree[0].label == "/" and not tree[0].pins:
        promoted = tree[0].children
        for node in promoted:
            node.label = node.path
        return promoted

    return tree


def _sorted(children: dict[str, _Builder]) -> list[tuple[str, _Builder]]:
    return sorted(children.items(), key=lambda kv: kv[0].lower())


def _convert(label: str, builder: _Builder, path: str) -> Node:
    # Collapse a chain: this node holds no pins and has exactly one child, so it
    # carries no information of its own.
    while not builder.pins and len(builder.children) == 1:
        (name, only), = builder.children.items()
        label = f"{label}/{name}" if not label.endswith("/") else f"{label}{name}"
        path = str(Path(path) / name)
        builder = only

    return Node(
        label=label,
        path=path,
        pins=sorted(builder.pins, key=lambda p: p.title.lower()),
        children=[
            _convert(name, child, str(Path(path) / name))
            for name, child in _sorted(builder.children)
        ],
    )


def flatten(nodes: list[Node], depth: int = 0) -> list[tuple[int, Node]]:
    """Depth-first (depth, node) pairs — for templates that render a flat indented list."""
    out: list[tuple[int, Node]] = []
    for node in nodes:
        out.append((depth, node))
        out.extend(flatten(node.children, depth + 1))
    return out


def group_by_tag(pins: list[Pin]) -> list[tuple[str, list[Pin]]]:
    """(tag, pins) sorted by tag. A pin with several tags appears under each."""
    groups: dict[str, list[Pin]] = {}
    for pin in pins:
        for tag in pin.tags or [UNTAGGED]:
            groups.setdefault(tag, []).append(pin)

    ordered = sorted(groups.items(), key=lambda kv: (kv[0] == UNTAGGED, kv[0].lower()))
    return [(tag, sorted(ps, key=lambda p: p.title.lower())) for tag, ps in ordered]


def all_tags(pins: list[Pin]) -> list[str]:
    return sorted({t for p in pins for t in p.tags}, key=str.lower)
