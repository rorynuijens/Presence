"""
test_fold.py — Source-line mapping and overflow measurement.

The fold marker is only worth having if the line it names is the line to
edit.  That rests on two things: blocks carrying the source line they came
from, and slides knowing where they start in the document.
"""

import re

import pytest

from presence.slides.renderer import render_slide_content
from presence.slides.utils import compute_slide_start_lines
from presence.slides.splitter import split_slides
from presence.slides.frontmatter import parse_frontmatter

DOC = """\
---
title: Deck
---

# First

Opening line

---

## Second

- one
- two
- three
"""


def _stamped(md, offset):
    """Rendered blocks as [(tag, source line)], in document order.

    A list and its first item legitimately share a line, so this cannot be
    keyed by line without losing one of them.
    """
    html = render_slide_content(md, line_offset=offset)
    return [(m.group(1), int(m.group(2)))
            for m in re.finditer(r'<(\w+) data-src-line="(\d+)"', html)]


# ── Source-line stamping ──────────────────────────────────────────────────────

def test_no_attributes_without_an_offset():
    assert "data-src-line" not in render_slide_content("# Hi\n\nText")


def test_blocks_carry_their_source_line():
    assert _stamped("# Hi\n\nText\n", 0) == [("h1", 0), ("p", 2)]


def test_offset_shifts_every_block():
    assert _stamped("# Hi\n\nText\n", 10) == [("h1", 10), ("p", 12)]


def test_list_items_are_stamped_individually():
    """A fourteen-item list must not fold at its first item."""
    assert _stamped("- a\n- b\n- c\n", 0) == [
        ("ul", 0), ("li", 0), ("li", 1), ("li", 2),
    ]


def test_rendering_is_unchanged_apart_from_the_attributes():
    plain = render_slide_content("# Hi\n\nText\n")
    stamped = render_slide_content("# Hi\n\nText\n", line_offset=0)
    assert re.sub(r' data-src-line="\d+"', "", stamped) == plain


def test_the_shared_parser_is_not_mutated():
    """Stamping must not leak into later unstamped renders."""
    render_slide_content("# Hi\n", line_offset=5)
    assert "data-src-line" not in render_slide_content("# Hi\n")


# ── Slide start lines ─────────────────────────────────────────────────────────

def test_slide_start_lines_point_at_the_first_content_line():
    lines = DOC.splitlines()
    starts = compute_slide_start_lines(DOC)
    assert [lines[i] for i in starts] == ["# First", "## Second"]


def test_one_start_line_per_slide():
    _meta, body = parse_frontmatter(DOC)
    assert len(compute_slide_start_lines(DOC)) == len(split_slides(body))


def test_start_lines_account_for_frontmatter():
    without = "# First\n\nOpening line\n"
    assert compute_slide_start_lines(without) == [0]
    assert compute_slide_start_lines(DOC)[0] == 4


def test_stamped_lines_land_on_the_right_source_text():
    """The whole chain: slide start + block offset names the real line."""
    lines = DOC.splitlines()
    _meta, body = parse_frontmatter(DOC)
    for slide, start in zip(split_slides(body), compute_slide_start_lines(DOC)):
        for _tag, src_line in _stamped(slide, start):
            rendered_text = lines[src_line].lstrip("#-* ").strip()
            if rendered_text:
                assert rendered_text in slide
