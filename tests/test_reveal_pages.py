"""
test_reveal_pages.py — What a revealed deck turns out to be on the page.

A step is only real once WeasyPrint has laid it out, so this drives the whole
read-back — ``pagination.page_the_deck`` — over a document that reveals, a
document that overflows, and a document that does both, because the two use
the same machinery and the second is what would quietly break the first.

The invariant worth stating out loud: **every step of a slide is the same
geometry.**  Hidden blocks keep their boxes, so a slide that fits fits at
every step and a slide that overflows overflows at every step, and the fold
does not depend on which one was measured.
"""

import pytest

from presence.converter import Converter
from presence.slides.frontmatter import parse_frontmatter
from presence.slides.html import md_to_html_slides
from presence.slides.pagination import page_the_deck, step_page_indices
from presence.slides.splitter import split_slides
from presence.slides.utils import compute_slide_start_lines

REVEALING = """\
# A cover

with a subtitle

---

## Three things

First point
+++
Second point
+++
Third point

---

## Nothing held back

An ordinary slide.
"""

# The third slide is a wall of text that WeasyPrint cannot fit, so it emits a
# continuation page — the case that made a page stop being a slide.
OVERFLOWING = """\
## Opens

one
+++
two

---

## Spills

""" + "\n\n".join(f"Paragraph {i} of a slide with far too much on it. " * 6
                  for i in range(14)) + """

---

## Closes

fine
"""


def _laid_out(text, tmp_path):
    weasyprint = pytest.importorskip("weasyprint")
    meta, body = parse_frontmatter(text)
    slides = split_slides(body)
    ctx = Converter()._render_context(meta, tmp_path)
    html, info = md_to_html_slides(
        slides, ctx.css, None, meta,
        width=ctx.width, height=ctx.height, theme_bg=ctx.theme_bg,
        base_url=str(tmp_path),
        line_offsets=compute_slide_start_lines(text),
        reveal=True,
    )
    document = weasyprint.HTML(string=html, base_url=str(tmp_path)).render()
    return document, len(slides), info


@pytest.fixture(scope="module")
def revealing(tmp_path_factory):
    return _laid_out(REVEALING, tmp_path_factory.mktemp("reveal"))


@pytest.fixture(scope="module")
def overflowing(tmp_path_factory):
    return _laid_out(OVERFLOWING, tmp_path_factory.mktemp("spill"))


def _page_count(pdf_bytes: bytes) -> int:
    from presence.slides.thumbnails_render import (rasterizer_available,
                                                   _load_pdf_doc)
    if not rasterizer_available():
        pytest.skip("Poppler is not available to count pages")
    return _load_pdf_doc(pdf_bytes).get_n_pages()


# ── Where the steps landed ───────────────────────────────────────────────────

def test_each_step_is_a_page_of_its_own(revealing):
    document, n_slides, _info = revealing
    assert step_page_indices(document, n_slides) == [[0], [1, 2, 3], [4]]


def test_steps_and_continuation_pages_are_read_in_one_pass(overflowing):
    # The revealed slide has a page per step; the slide that spills still
    # starts where it starts, and its remainder is nobody's step.
    document, n_slides, _info = overflowing
    steps = step_page_indices(document, n_slides)
    assert [len(s) for s in steps] == [2, 1, 1]
    assert steps[0] == [0, 1]


# ── The PDF the deck is made of ──────────────────────────────────────────────

def test_the_pdf_holds_one_page_per_step(revealing):
    document, n_slides, _info = revealing
    paged = page_the_deck(document, n_slides)
    assert paged.steps == [[0], [1, 2, 3], [4]]
    assert _page_count(paged.pdf) == 5


def test_the_page_a_slide_is_indexed_by_shows_it_complete(revealing):
    document, n_slides, _info = revealing
    paged = page_the_deck(document, n_slides)
    # The strip, the exported images and the handout all index by this, and
    # all three want the slide with everything on it.
    assert paged.pages == [0, 3, 4]


def test_the_continuation_pages_still_go(overflowing):
    document, n_slides, _info = overflowing
    paged = page_the_deck(document, n_slides)
    # Two steps on the first slide, one page each for the other two — and
    # nothing of the remainder the overflowing slide left behind.
    assert paged.steps == [[0, 1], [2], [3]]
    assert _page_count(paged.pdf) == 4


def test_a_revealed_slide_is_not_mistaken_for_an_overflowing_one(revealing):
    document, n_slides, _info = revealing
    # Three consecutive pages holding one slide is what a reveal looks like
    # and what an overflow looks like; only the stamps tell them apart.
    assert page_the_deck(document, n_slides).fragmented == []


def test_the_slide_that_really_spills_is_still_reported(overflowing):
    document, n_slides, _info = overflowing
    assert page_the_deck(document, n_slides).fragmented == [1]


def test_the_fold_is_read_per_slide_not_per_step(revealing):
    document, n_slides, _info = revealing
    folds = page_the_deck(document, n_slides).folds
    assert len(folds) == n_slides
    assert folds == [None, None, None]


def test_a_step_does_not_move_what_is_already_on_the_slide(revealing):
    """
    The reason a step can be a page rather than a re-render.

    Every step is the same laid-out slide, so the block boxes are in the same
    place on all three pages — which is what makes the reveal look like the
    slide arriving rather than the slide being rebuilt.
    """
    document, _n, _info = revealing
    from presence.slides.pagination import walk_boxes

    def tops(page):
        return sorted(round(b.position_y, 1) for b in walk_boxes(page._page_box)
                      if getattr(getattr(b, "element", None), "get", None)
                      and b.element.get("data-src-line") is not None)

    pages = list(document.pages)
    first, last = tops(pages[1]), tops(pages[3])
    assert first == last[:len(first)]
