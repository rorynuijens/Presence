"""
test_build_warnings.py — What a build has to say, and what it must not ship.

Three things used to go wrong in a way the rendered slide could not show:

*  a slide with too much text left its continuation pages *in the exported
   PDF* — a headerless remainder sliced through a line, and sometimes a page
   holding nothing but the footer.  Every other consumer of the build already
   skipped them; the one file a writer hands to somebody else did not.
*  a picture whose path did not resolve rendered as a gap the layout looked
   like it had chosen, with nothing said anywhere.
*  a theme named in the frontmatter that is not installed made the CLI refuse
   to produce a deck at all, while the window silently rendered in whichever
   theme sorted first.

All three now produce a deck *and* a sentence, built by one function over one
set of facts so the CLI's stderr and the window's banner agree.
"""

import pytest

from presence.slides.diagnostics import (build_warnings, overflow_warning,
                                         missing_image_warning)
from presence.slides.frontmatter import parse_frontmatter
from presence.slides.splitter import split_slides
from presence.slides.utils import compute_slide_start_lines, image_is_missing


# A long list, not long prose: the two overflow differently and the
# difference is the point.  A list breaks between items at the page edge, so
# nothing on the slide's own first page crosses the fold — see
# test_a_cleanly_broken_slide_has_no_fold_of_its_own.
OVERFLOW = "## Too much\n\n" + "\n".join(
    f"- Point {i} of a slide that goes on and on and will not fit inside a "
    "720 pixel tall box no matter how you slice it." for i in range(24)
)

SPILLING_DECK = "\n\n---\n\n".join([
    "# Title\n\nsub",       # 0 — fits
    OVERFLOW,               # 1 — spills onto pages of its own
    "## Last\n\n- fine",    # 2 — fits
])


@pytest.fixture(scope="module")
def spilling(tmp_path_factory):
    """The spilling deck, through the layout the PDF comes from."""
    weasyprint = pytest.importorskip("weasyprint")
    from presence.converter import Converter
    from presence.slides.html import md_to_html_slides

    base = tmp_path_factory.mktemp("deck")
    meta, body = parse_frontmatter(SPILLING_DECK)
    slides = split_slides(body)
    ctx = Converter()._render_context(meta, base)
    html, info = md_to_html_slides(
        slides, ctx.css, None, meta,
        width=ctx.width, height=ctx.height, theme_bg=ctx.theme_bg,
        base_url=str(base),
        line_offsets=compute_slide_start_lines(SPILLING_DECK),
    )
    document = weasyprint.HTML(string=html, base_url=str(base)).render()
    return document, len(slides), info


def _page_count(pdf_bytes: bytes) -> int:
    """Pages in *pdf_bytes*, read with the library the app itself reads with."""
    from presence.slides.thumbnails_render import (rasterizer_available,
                                                   _load_pdf_doc)
    if not rasterizer_available():
        pytest.skip("Poppler not available")
    return _load_pdf_doc(pdf_bytes).get_n_pages()


# ── The PDF is one page per slide ─────────────────────────────────────────────

def test_the_deck_really_does_spill(spilling):
    """Without this the rest of these tests would pass for the wrong reason."""
    document, n_slides, _info = spilling
    assert len(document.pages) > n_slides


def test_the_exported_pdf_has_one_page_per_slide(spilling):
    from presence.slides.pagination import slide_page_indices, slide_pages_pdf

    document, n_slides, _info = spilling
    pdf, _pages = slide_pages_pdf(
        document, slide_page_indices(document, n_slides)
    )
    assert _page_count(pdf) == n_slides


def test_the_trim_says_which_page_each_slide_landed_on(spilling):
    """
    Not an assumption the callers may make for themselves.

    The thumbnails, the handout and the slideshow all index the bytes they
    are handed.  Trimming renumbers every page after the first overflow, so
    the new numbering comes back with the bytes rather than being inferred
    from the fact that a trim was attempted.
    """
    from presence.slides.pagination import slide_page_indices, slide_pages_pdf

    document, n_slides, _info = spilling
    laid_out = slide_page_indices(document, n_slides)
    assert laid_out != list(range(n_slides))    # the fragments moved things

    _pdf, pages = slide_pages_pdf(document, laid_out)
    assert pages == list(range(n_slides))


def test_a_deck_that_fits_is_left_alone(tmp_path):
    from presence.slides.pagination import slide_page_indices, slide_pages_pdf
    from presence.converter import Converter
    from presence.slides.html import md_to_html_slides

    weasyprint = pytest.importorskip("weasyprint")
    deck = "# One\n\nshort\n\n---\n\n## Two\n\nalso short"
    meta, body = parse_frontmatter(deck)
    slides = split_slides(body)
    ctx = Converter()._render_context(meta, tmp_path)
    html, _info = md_to_html_slides(
        slides, ctx.css, None, meta, width=ctx.width, height=ctx.height,
        theme_bg=ctx.theme_bg, base_url=str(tmp_path),
    )
    document = weasyprint.HTML(string=html, base_url=str(tmp_path)).render()
    pages = slide_page_indices(document, len(slides))
    assert pages == [0, 1]
    _pdf, after = slide_pages_pdf(document, pages)
    assert after == [0, 1]


# ── Which slides count as having run out of room ──────────────────────────────

def test_a_cleanly_broken_slide_has_no_fold_of_its_own(spilling):
    """
    The reason `clipped` exists.

    A slide that breaks at the page edge has nothing crossing its own first
    page, so the fold is silent about the one slide that actually lost
    content.  If this ever starts failing, the fold has become sufficient
    and the union in overflow_warning() can go.
    """
    from presence.slides.pagination import (slide_page_indices, measure_folds,
                                            fragmented_slides)

    document, n_slides, _info = spilling
    laid_out = slide_page_indices(document, n_slides)
    assert fragmented_slides(document, laid_out) == [1]
    assert measure_folds(document, n_slides, laid_out)[1] is None


def test_a_clipped_slide_is_reported_even_with_no_fold():
    assert overflow_warning([{}, {"clipped": True}, {}]).startswith("Slide 2 ")


def test_a_folded_slide_is_reported_even_when_it_was_not_fragmented():
    assert overflow_warning([{}, {"fold_line": 12}, {}]).startswith("Slide 2 ")


def test_several_slides_are_named_in_one_sentence():
    warning = overflow_warning([{"clipped": True}, {}, {"fold_line": 3}])
    assert warning.startswith("Slides 1 and 3 have ")


def test_a_deck_that_fits_says_nothing_about_overflow():
    assert overflow_warning([{}, {"fold_line": None}]) is None


# ── Pictures that are not there ───────────────────────────────────────────────

def test_a_picture_that_is_not_there_is_missing(tmp_path):
    assert image_is_missing("nope.png", str(tmp_path))


def test_a_picture_that_is_there_is_not(tmp_path):
    (tmp_path / "barn.jpg").write_bytes(b"not really a jpeg")
    assert not image_is_missing("barn.jpg", str(tmp_path))


def test_a_remote_or_inline_picture_is_never_reported(tmp_path):
    assert not image_is_missing("https://example.com/x.png", str(tmp_path))
    assert not image_is_missing("data:image/png;base64,AAAA", str(tmp_path))


def test_a_broken_picture_path_is_recorded_on_its_slide(tmp_path):
    from presence.slides.html import md_to_html_slides

    deck = "# One\n\ntext\n\n---\n\n![a chart](nope.png)\n\n## Two\n\nwords"
    slides = split_slides(deck)
    _html, info = md_to_html_slides(slides, "", None, {},
                                    base_url=str(tmp_path))
    assert info[0]["missing_images"] == []
    assert info[1]["missing_images"] == ["nope.png"]
    assert "nope.png" in missing_image_warning(info)


def test_a_single_slide_render_still_reports_the_whole_deck(tmp_path):
    """
    The live path renders one slide.  It must not therefore forget that a
    picture three slides away is broken — slide_info always covers the deck.
    """
    from presence.slides.html import md_to_html_slides

    deck = "# One\n\ntext\n\n---\n\n![a chart](nope.png)\n\n## Two"
    slides = split_slides(deck)
    _html, info = md_to_html_slides(slides, "", None, {}, only_index=0,
                                    base_url=str(tmp_path))
    assert info[1]["missing_images"] == ["nope.png"]


# ── One sentence per fact, and none when there is nothing to say ──────────────

def test_a_clean_build_says_nothing():
    assert build_warnings([{"fold_line": None, "missing_images": []}]) == []


def test_every_fact_gets_its_own_sentence():
    warnings = build_warnings(
        [{"fold_line": 4, "missing_images": ["a.png"]}],
        theme_warning="Theme 'x' is not installed — rendered in Light instead.",
    )
    assert len(warnings) == 3
    assert warnings[0].startswith("Theme 'x'")


# ── The banner is where the window says it ────────────────────────────────────
#
# Widgets cannot be built under this suite's conftest without a display, so
# these drive the real coordinator against the stand-in window the other
# controller tests use.

from tests.test_build_coordinator import FakeWindow    # noqa: E402
from presence.build_coordinator import BuildCoordinator  # noqa: E402


class _Sidebar:
    def set_converting(self, on): pass
    def update_from_conversion(self, *a, **kw): return 0
    def update_from_text(self, text): pass


class _Converter:
    def __init__(self, warnings=()):
        self.slide_info = []
        self.thumbnails = []
        self.warnings   = list(warnings)
        self.ratio      = "16:9"


def _complete(warnings=()):
    win = FakeWindow("text")
    win.sidebar   = _Sidebar()
    win.converter = _Converter()
    coord = BuildCoordinator(win)
    coord.trigger = lambda *a: None
    coord.building_text = "text"
    coord.set_build_folds([])
    coord.on_complete(_Converter(warnings), 1, 0.0, "/tmp/o.pdf", "file:///o")
    return win


def test_the_banner_says_what_the_build_said():
    win = _complete(["slide 2 has more text than fits.",
                     "1 picture could not be found: nope.png (slide 3)."])

    assert win.banner.revealed is True
    assert "slide 2" in win.banner.title
    assert "nope.png" in win.banner.title


def test_a_clean_build_puts_the_banner_away():
    """
    A banner left up from the previous build is worse than none: it names a
    slide the writer has already fixed.
    """
    win = _complete([])

    assert win.banner.revealed is False
