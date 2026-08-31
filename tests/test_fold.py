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


# ── Which page a slide's fold is measured on ──────────────────────────────────
#
# A slide that overflows does not get clipped by WeasyPrint, it gets a
# continuation page, so the deck's pages stop lining up with its slides.
# Reading folds in page order then blames the overflow on the *next* slide and
# leaves the one that really ran out of room unmarked — and those folds drive
# the editor's rule, the thumbnail's FULL badge and the "too much text" banner.

OVERFLOW = "## Too much\n\n" + "\n\n".join(
    f"Paragraph {i} of a slide that goes on and on and will not fit inside a "
    "720 pixel tall box no matter how you slice it." for i in range(30)
)

SPILLING_DECK = "\n\n---\n\n".join([
    "# Title\n\nsub",                              # 0 — fits
    OVERFLOW,                                       # 1 — spills
    "## After\n\n- ok",                            # 2 — fits
    OVERFLOW.replace("Too much", "Also too much"),  # 3 — spills
    "## Last\n\n- fine",                           # 4 — fits
])


@pytest.fixture(scope="module")
def laid_out(tmp_path_factory):
    """The spilling deck, through the same layout the PDF comes from."""
    weasyprint = pytest.importorskip("weasyprint")
    from presence.converter import Converter
    from presence.slides.html import md_to_html_slides

    base = tmp_path_factory.mktemp("deck")
    meta, body = parse_frontmatter(SPILLING_DECK)
    slides = split_slides(body)
    ctx = Converter()._render_context(meta, base)
    html, _info = md_to_html_slides(
        slides, ctx.css, None, meta,
        width=ctx.width, height=ctx.height, theme_bg=ctx.theme_bg,
        base_url=str(base),
        line_offsets=compute_slide_start_lines(SPILLING_DECK),
    )
    document = weasyprint.HTML(string=html, base_url=str(base)).render()
    return document, len(slides)


def test_the_deck_really_does_spill(laid_out):
    """Without this the rest of these tests would pass for the wrong reason."""
    document, n_slides = laid_out
    assert len(document.pages) > n_slides


def test_every_fold_names_a_line_inside_its_own_slide(laid_out):
    from presence.converter import _measure_folds, _slide_page_indices

    document, n_slides = laid_out
    starts = compute_slide_start_lines(SPILLING_DECK)
    folds = _measure_folds(document, n_slides,
                           _slide_page_indices(document, n_slides))

    for index, fold in enumerate(folds):
        if fold is None:
            continue
        end = starts[index + 1] if index + 1 < n_slides else len(
            SPILLING_DECK.splitlines())
        assert starts[index] <= fold < end, (
            f"slide {index} folds at line {fold}, which belongs to another slide"
        )


def test_the_slides_that_overflow_are_the_ones_that_get_marked(laid_out):
    from presence.converter import _measure_folds, _slide_page_indices

    document, n_slides = laid_out
    folds = _measure_folds(document, n_slides,
                           _slide_page_indices(document, n_slides))

    assert [f is not None for f in folds] == [False, True, False, True, False]


def test_reading_folds_in_page_order_is_what_goes_wrong(laid_out):
    """Pins the bug itself, so the page mapping cannot be quietly dropped."""
    from presence.converter import _measure_folds, _slide_page_indices

    document, n_slides = laid_out
    by_page  = _measure_folds(document, n_slides)
    by_slide = _measure_folds(document, n_slides,
                              _slide_page_indices(document, n_slides))
    assert by_page != by_slide


def test_one_slide_on_its_own_needs_no_mapping(laid_out):
    """The live path renders a single slide, so page 0 is that slide's page."""
    from presence.converter import _measure_folds

    document, _n = laid_out
    assert _measure_folds(document, 1) == _measure_folds(document, 1, [0])
