"""
test_live_render.py — The live single-slide render is what the deck will be.

The point of the medium path is that the writer, the room and the reader are
all looking at one WeasyPrint layout.  Two things follow, and both are checked
here against real renders rather than stand-ins:

*  a live frame is the same picture as the corresponding PDF page, and
*  the fold line it measures is the one a full build would measure.

The coalescing rule is checked separately, because it is the part that keeps
typing responsive: a request arriving mid-render replaces the pending one
rather than queueing behind it, and a superseded result is dropped instead of
flashing the wrong slide.

Converter is a plain GObject, so none of this needs a display.
"""

import pytest

from pathlib import Path

from presence.converter import Converter, SlideFrame
from presence.slides.thumbnails_render import render_page_png

pytest.importorskip("weasyprint")

TWO_SLIDES = "# One\n\nA short slide.\n\n---\n\n# Two\n\nAnother short slide.\n"

# Enough paragraphs that the second slide cannot fit its box.
OVERFLOWING = "# Fits\n\nShort.\n\n---\n\n# Too much\n\n" + "\n\n".join(
    f"Paragraph {i} with a fair amount of text on it." for i in range(30)
)


@pytest.fixture
def converter():
    return Converter(theme="light")


# ── Rendering one slide ───────────────────────────────────────────────────────

def test_a_frame_carries_the_slide_and_the_deck_it_came_from(converter, tmp_path):
    frame = converter._render_frame(TWO_SLIDES, tmp_path, 1, 640)

    assert isinstance(frame, SlideFrame)
    assert frame.index == 1
    assert frame.n_slides == 2
    assert (frame.width, frame.height) == (1280, 720)
    assert frame.png.startswith(b"\x89PNG")


def test_an_index_past_the_end_lands_on_the_last_slide(converter, tmp_path):
    frame = converter._render_frame(TWO_SLIDES, tmp_path, 99, 640)

    assert frame.index == 1


def test_a_negative_index_lands_on_the_first_slide(converter, tmp_path):
    frame = converter._render_frame(TWO_SLIDES, tmp_path, -4, 640)

    assert frame.index == 0


def test_a_document_with_no_slides_is_a_recoverable_refusal(converter, tmp_path):
    """A slide-less document is an expected state, so ValueError, not a crash."""
    with pytest.raises(ValueError):
        converter._render_frame("", tmp_path, 0, 640)


def test_the_frame_is_rendered_at_the_width_it_was_asked_for(converter, tmp_path):
    from PIL import Image
    import io

    narrow = converter._render_frame(TWO_SLIDES, tmp_path, 0, 400)
    wide   = converter._render_frame(TWO_SLIDES, tmp_path, 0, 800)

    assert Image.open(io.BytesIO(narrow.png)).width == 400
    assert Image.open(io.BytesIO(wide.png)).width == 800


# ── One engine ────────────────────────────────────────────────────────────────

def test_a_live_frame_is_the_same_picture_as_the_pdf_page(converter, tmp_path):
    """
    The whole point of the change: what the writer sees is what the deck is.

    Both sides go through WeasyPrint and then Poppler, so agreement here is
    byte-for-byte rather than approximate.
    """
    import weasyprint
    from presence.slides.frontmatter import parse_frontmatter
    from presence.slides.splitter import split_slides
    from presence.slides.html import md_to_html_slides

    for index in (0, 1):
        frame = converter._render_frame(TWO_SLIDES, tmp_path, index, 640)

        # The whole deck, the way convert() builds it.
        meta, body = parse_frontmatter(TWO_SLIDES)
        ctx = converter._render_context(meta, tmp_path)
        html, _info = md_to_html_slides(
            split_slides(body), ctx.css, ctx.logo_b64, meta,
            width=ctx.width, height=ctx.height, theme_bg=ctx.theme_bg,
            base_url=str(tmp_path),
        )
        deck_pdf = weasyprint.HTML(string=html, base_url=str(tmp_path)).write_pdf()

        assert frame.png == render_page_png(deck_pdf, index, 640)


# ── Folds ─────────────────────────────────────────────────────────────────────

def test_a_slide_that_fits_has_no_fold(converter, tmp_path):
    frame = converter._render_frame(OVERFLOWING, tmp_path, 0, 640)

    assert frame.fold_line is None


def test_an_overflowing_slide_folds_at_a_line_in_that_slide(converter, tmp_path):
    frame = converter._render_frame(OVERFLOWING, tmp_path, 1, 640)

    assert frame.fold_line is not None
    # Line numbers are into the whole document, not into the slide, which is
    # what lets the editor put a rule on the right row.
    slide_two_starts = OVERFLOWING.index("# Too much")
    first_line_of_slide_two = OVERFLOWING[:slide_two_starts].count("\n") + 1
    assert frame.fold_line > first_line_of_slide_two


def test_the_live_fold_is_the_one_a_build_would_measure(converter, tmp_path):
    """
    The live render and the build must not disagree about where a slide runs out.

    They cannot, now: both measure the same WeasyPrint layout.  This pins that
    down against the converter's own build so a future change cannot quietly
    reintroduce two answers.
    """
    src = tmp_path / "deck.md"
    src.write_text(OVERFLOWING)

    from presence.slides.frontmatter import parse_frontmatter
    from presence.slides.splitter import split_slides
    from presence.slides.html import md_to_html_slides
    from presence.slides.utils import compute_slide_start_lines
    from presence.converter import _measure_folds
    import weasyprint

    meta, body = parse_frontmatter(OVERFLOWING)
    ctx = converter._render_context(meta, tmp_path)
    slides = split_slides(body)
    html, _info = md_to_html_slides(
        slides, ctx.css, ctx.logo_b64, meta,
        width=ctx.width, height=ctx.height, theme_bg=ctx.theme_bg,
        base_url=str(tmp_path),
        line_offsets=compute_slide_start_lines(OVERFLOWING),
    )
    document = weasyprint.HTML(string=html, base_url=str(tmp_path)).render()
    build_folds = _measure_folds(document, len(slides))

    live_folds = [
        converter._render_frame(OVERFLOWING, tmp_path, i, 640).fold_line
        for i in range(len(slides))
    ]

    assert live_folds == build_folds


# ── Coalescing ────────────────────────────────────────────────────────────────

class _StubbedConverter(Converter):
    """Records what the worker was asked to render, without rendering it."""

    def __init__(self):
        super().__init__()
        self.rendered = []
        self.release = None

    def _render_frame(self, text, base_dir, index, width_px):
        self.rendered.append(index)
        if self.release is not None:
            self.release.wait(timeout=5)
        return SlideFrame(png=b"x", fold_line=None, index=index,
                          n_slides=9, width=1280, height=720)


def test_a_burst_of_requests_renders_only_the_newest(tmp_path):
    """
    Typing quickly must not queue a frame per keystroke.

    The worker is held inside the first render while four more requests
    arrive; only the last of them should be picked up when it is let go.
    """
    import threading

    conv = _StubbedConverter()
    conv.release = threading.Event()
    delivered = []
    done = threading.Event()

    def cb(frame, error):
        delivered.append(frame.index if frame else error)
        done.set()
        return False

    conv.render_slide_async("doc", tmp_path, 0, 640, cb)
    # Wait until the worker is actually inside the first render.
    for _ in range(500):
        if conv.rendered:
            break
        threading.Event().wait(0.01)

    for index in (1, 2, 3, 4):
        conv.render_slide_async("doc", tmp_path, index, 640, cb)
    conv.release.set()

    _spin_until(lambda: len(conv.rendered) >= 2)

    # Slide 0 was already running; of 1-4 only 4 is still wanted.
    assert conv.rendered == [0, 4]


def test_a_superseded_frame_is_never_delivered(tmp_path):
    """A slow gallery frame must not overwrite a newer text frame."""
    import threading
    from gi.repository import GLib

    conv = _StubbedConverter()
    conv.release = threading.Event()
    delivered = []

    def cb(frame, error):
        delivered.append(frame.index if frame else error)
        return False

    conv.render_slide_async("doc", tmp_path, 0, 640, cb)
    for _ in range(500):
        if conv.rendered:
            break
        threading.Event().wait(0.01)
    conv.render_slide_async("doc", tmp_path, 7, 640, cb)   # supersedes slide 0
    conv.release.set()

    _spin_until(lambda: len(conv.rendered) >= 2)
    _drain_idle()

    assert delivered == [7]


def test_an_error_reaches_the_callback_rather_than_the_thread(tmp_path):
    from gi.repository import GLib

    conv = Converter(theme="light")
    delivered = []
    conv.render_slide_async("", tmp_path, 0, 640,
                            lambda f, e: (delivered.append((f, e)), False)[1])

    _spin_until(lambda: bool(delivered), drain=True)

    frame, error = delivered[0]
    assert frame is None
    assert isinstance(error, ValueError)


def _spin_until(predicate, timeout: float = 10.0, drain: bool = False) -> None:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if drain:
            _drain_idle()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached in time")


def _drain_idle() -> None:
    from gi.repository import GLib
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


# ── Overflow pages ────────────────────────────────────────────────────────────

def test_an_overflowing_slide_leaves_a_continuation_page(converter, tmp_path):
    """
    The reason slides need a page map at all.

    A slide is a fixed box with overflow:hidden, but WeasyPrint fragments a
    block that does not fit rather than clipping it, so a deck's page count
    can exceed its slide count.  Pinned here because everything made out of
    the PDF depends on knowing it happens.
    """
    document = _layout(converter, OVERFLOWING, tmp_path)

    assert len(document.pages) > 2          # two slides, more than two pages


def test_the_page_map_skips_a_slide_s_overflow(converter, tmp_path):
    from presence.converter import _slide_page_indices

    over = "\n\n".join(f"Paragraph {i} with a fair amount of text on it."
                       for i in range(30))
    doc = f"# One\n\nShort.\n\n---\n\n# Two\n\n{over}\n\n---\n\n# Three\n\nShort.\n"
    document = _layout(converter, doc, tmp_path)

    pages = _slide_page_indices(document, 3)

    assert pages[0] == 0
    assert pages[1] == 1
    # Slide three starts after slide two's spill, not at page 2.
    assert pages[2] > 2
    assert pages[2] < len(document.pages)


def test_a_deck_with_nothing_overflowing_maps_one_to_one(converter, tmp_path):
    from presence.converter import _slide_page_indices

    document = _layout(converter, TWO_SLIDES, tmp_path)

    assert _slide_page_indices(document, 2) == [0, 1]


def test_the_page_map_falls_back_when_it_cannot_read_the_stamps(converter):
    """An unreadable document must not take the build down with it."""
    from presence.converter import _slide_page_indices

    class Broken:
        @property
        def pages(self):
            raise RuntimeError("no box tree here")

    assert _slide_page_indices(Broken(), 3) == [0, 1, 2]


def _layout(converter, markdown: str, base_dir: Path):
    """Lay a document out the way convert() does, and hand back the document."""
    import weasyprint
    from presence.slides.frontmatter import parse_frontmatter
    from presence.slides.splitter import split_slides
    from presence.slides.html import md_to_html_slides

    meta, body = parse_frontmatter(markdown)
    ctx = converter._render_context(meta, base_dir)
    html, _info = md_to_html_slides(
        split_slides(body), ctx.css, ctx.logo_b64, meta,
        width=ctx.width, height=ctx.height, theme_bg=ctx.theme_bg,
        base_url=str(base_dir),
    )
    return weasyprint.HTML(string=html, base_url=str(base_dir)).render()
