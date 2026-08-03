"""Markdown and tabular rendering for the inline viewers.

The point of the markdown path is agent-written `REPORT.md` files: an agent finishes a
run, writes a report next to the figures it references, and registers the directory.
For that to be worth anything, relative links like `![](videos/eval.mp4)` have to
resolve — so every relative src/href is rewritten to a `/raw/<pin>/...` URL.
"""

from __future__ import annotations

import csv
import io
from pathlib import PurePosixPath
from urllib.parse import quote, urlparse

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer_for_filename
from pygments.util import ClassNotFound

MAX_TABLE_ROWS = 500


def _highlight(code: str, lang: str, _attrs) -> str:
    try:
        lexer = get_lexer_by_name(lang) if lang else None
    except ClassNotFound:
        lexer = None
    if lexer is None:
        return ""  # let markdown-it escape it normally
    return highlight(code, lexer, HtmlFormatter(nowrap=False, cssclass="hl"))


def _is_relative(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return not parsed.scheme and not parsed.netloc and not url.startswith(("/", "#"))


def render_markdown(text: str, pin_id: str, doc_rel: str) -> str:
    """Render markdown, rewriting relative asset links against the doc's directory."""
    md = MarkdownIt("commonmark", {"highlight": _highlight, "linkify": True}).enable(
        ["table", "strikethrough"]
    )
    tokens = md.parse(text)
    base = PurePosixPath(doc_rel).parent

    def rewrite(url: str) -> str:
        if not _is_relative(url):
            return url
        target = (base / url).as_posix() if str(base) != "." else url
        # Normalize ./ and ../ so a report can reference a sibling run directory.
        parts: list[str] = []
        for part in target.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        return f"/raw/{pin_id}/" + "/".join(quote(p) for p in parts)

    for token in tokens:
        stack = [token]
        while stack:
            tok = stack.pop()
            if tok.type == "image":
                src = tok.attrGet("src")
                if src:
                    tok.attrSet("src", rewrite(src))
            elif tok.type == "link_open":
                href = tok.attrGet("href")
                if href:
                    tok.attrSet("href", rewrite(href))
            if tok.children:
                stack.extend(tok.children)

    return md.renderer.render(tokens, md.options, {})


def render_ticket(text: str) -> str:
    """Render a ticket body.

    Two differences from `render_markdown`. Raw HTML is disabled, because a ticket body
    reaches the dashboard from another node and there is no reason for it to carry
    markup. And nothing is rewritten to `/raw/`: a project pin serves no bytes, so a
    relative path here has nothing to point at — results are linked through the
    ticket's `artifacts:` field instead.
    """
    md = MarkdownIt("commonmark", {"highlight": _highlight, "html": False}).enable(
        ["table", "strikethrough"]
    )
    return md.render(text)


def render_code(text: str, filename: str) -> str:
    try:
        lexer = guess_lexer_for_filename(filename, text)
    except ClassNotFound:
        return f"<pre class='plain'>{_escape(text)}</pre>"
    return highlight(text, lexer, HtmlFormatter(cssclass="hl"))


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def read_table(text: str, delimiter: str | None = None) -> tuple[list[str], list[list[str]], bool]:
    """Parse CSV/TSV → (header, rows, truncated). Sniffed delimiter, capped rows."""
    sample = text[:8192]
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return [], [], False

    rows: list[list[str]] = []
    truncated = False
    for row in reader:
        if len(rows) >= MAX_TABLE_ROWS:
            truncated = True
            break
        rows.append(row)
    return header, rows, truncated


def pygments_css() -> str:
    return HtmlFormatter(cssclass="hl").get_style_defs(".hl")
