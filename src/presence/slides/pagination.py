"""
pagination.py — Reading slide structure back off a laid-out WeasyPrint page.

A slide is a fixed box with ``overflow: hidden``, but WeasyPrint fragments a
block that does not fit rather than clipping it, so a slide with too much text
emits a *continuation page*: a headerless remainder that begins mid-sentence,
sometimes followed by a page carrying nothing but the theme's footer.  Page
number and slide number part company the moment that happens.

Everything Presence makes out of the PDF has to be told which page a slide
starts on, and — because those continuation pages are not slides — the PDF
handed to a reader has to be built from that list too.  Both engines need
that, and the CLI must not import GTK, so it lives here rather than in
``converter.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


def walk_boxes(box):
    """Yield *box* and every descendant of it."""
    stack = [box]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(getattr(current, "children", ()) or ())


def slide_page_indices(document, n_slides: int) -> list[int]:
    """
    The PDF page each slide starts on, from the ``data-slide-index`` stamps.

    Falls back to one page per slide when the stamps cannot be read, which is
    exactly right for the overwhelmingly common case of nothing overflowing.
    """
    found: dict[int, int] = {}
    try:
        for page_number, page in enumerate(document.pages):
            for box in walk_boxes(page._page_box):
                element = getattr(box, "element", None)
                if element is None or not hasattr(element, "get"):
                    continue
                raw = element.get("data-slide-index")
                if raw is None:
                    continue
                try:
                    index = int(raw)
                except ValueError:
                    continue
                # First page carrying this slide is where it starts; later
                # pages are its overflow.
                found.setdefault(index, page_number)
                break
    except Exception:
        log.debug("Slide/page mapping failed", exc_info=True)

    return [found.get(i, i) for i in range(n_slides)]


def step_page_indices(document, n_slides: int) -> list[list[int]]:
    """
    The PDF page of every reveal step, slide by slide.

    A slide with no steps has one page and answers with a one-element list,
    which is what every deck written before this existed answers with — so
    the flattened result is exactly :func:`slide_page_indices`' answer and
    the rest of the build cannot tell the difference.

    Read from the same stamps and in the same pass: ``data-slide-index``
    says whose page this is and ``data-step`` says which of its steps, so a
    step's continuation pages fall away for the same reason a slide's do.
    """
    found: dict[tuple[int, int], int] = {}
    try:
        for page_number, page in enumerate(document.pages):
            for box in walk_boxes(page._page_box):
                element = getattr(box, "element", None)
                if element is None or not hasattr(element, "get"):
                    continue
                raw = element.get("data-slide-index")
                if raw is None:
                    continue
                try:
                    index = int(raw)
                    step  = int(element.get("data-step") or 0)
                except ValueError:
                    continue
                found.setdefault((index, step), page_number)
                break
    except Exception:
        log.debug("Slide/step/page mapping failed", exc_info=True)

    if not found:
        return [[i] for i in range(n_slides)]

    steps: list[list[int]] = []
    for index in range(n_slides):
        pages = [page for (slide, _step), page in sorted(found.items())
                 if slide == index]
        steps.append(pages or [index])
    return steps


def fragmented_slides(document, pages: list[int]) -> list[int]:
    """
    Indices of the slides that needed more than one page.

    Not the same question as the fold, and neither one subsumes the other.
    The fold catches text reaching into the padding a theme reserves for the
    slide number, which is content the reader can still see; this catches
    content WeasyPrint moved onto a page of its own, which is content
    :func:`slide_pages_pdf` then drops.  A slide that breaks cleanly at the
    page edge has no box crossing its own first page and no fold at all — so
    asking only the fold would stay silent about the one slide that actually
    lost something.
    """
    try:
        total = len(document.pages)
    except Exception:
        return []

    over = []
    for index, start in enumerate(pages):
        end = pages[index + 1] if index + 1 < len(pages) else total
        if end - start > 1:
            over.append(index)
    return over


def slide_pages_pdf(document, pages: list[int]) -> "tuple[bytes, list[int]]":
    """
    Write the PDF with one page per slide, leaving out spill-over pages.

    When a slide has too much text, WeasyPrint carries the rest onto an
    extra page.  That page is not a slide, so it is left out.  The slide
    shows what fits, and the editor already marks where it stops.

    Returns the PDF and the page each slide ended up on.  Normally that is
    simply 0, 1, 2… but if the pages cannot be picked out, the whole
    document is written instead, and the list says so.  Everything that
    shows slides uses that list rather than guessing.
    """
    try:
        all_pages = list(document.pages)
        wanted    = [all_pages[p] for p in pages if 0 <= p < len(all_pages)]
        if len(wanted) == len(pages):
            if len(wanted) < len(all_pages):
                document = document.copy(wanted)
            return document.write_pdf(**_PDF_OPTIONS), list(range(len(pages)))
    except Exception:
        log.warning("Could not drop continuation pages; exporting every page",
                    exc_info=True)

    return document.write_pdf(**_PDF_OPTIONS), list(pages)


# How every deck PDF is written.  pdf_tags adds the reading order and each
# picture's alt text, so a screen reader can follow the slides.  It is an
# option of writing the PDF, not of laying it out.
_PDF_OPTIONS = {"pdf_tags": True}


# ── The whole read-back, in one order ────────────────────────────────────────

@dataclass(frozen=True)
class Paged:
    """What a laid-out deck turns out to be, once it has been read back."""

    pdf:        bytes
    steps:      list[list[int]]      # per slide, the page of each of its steps
    folds:      list[int | None]     # per slide, the line that runs over
    fragmented: list[int]            # slides that needed a second page

    @property
    def pages(self) -> list[int]:
        """The page showing each slide complete — its last step."""
        return [s[-1] for s in self.steps]


def page_the_deck(document, n_slides: int) -> Paged:
    """
    Read a laid-out document back, and write the PDF the deck is made of.

    One function because the order is the whole difficulty, and both engines
    have to take the steps in it:

    1. Where every slide and every step of one landed, *before* anything is
       dropped — a slide that overflows leaves a continuation page behind
       it, and from there on the n-th page is not the n-th slide.
    2. The folds, off each slide's own first page.  Reading them in page
       order would blame a continuation page's overrun on the next slide and
       leave the one that really overflowed unmarked, and they can only be
       measured on the untrimmed document, which is the only place the
       overrun is still visible.
    3. Which slides fragmented — asked of the step pages rather than the
       slide pages, because two steps of one slide are two pages on purpose
       and would otherwise read as an overflow on every revealed slide.
    4. The PDF, from the step pages alone.
    """
    steps    = step_page_indices(document, n_slides)
    flat     = [page for slide in steps for page in slide]
    owner    = [index for index, slide in enumerate(steps) for _ in slide]

    folds      = measure_folds(document, n_slides, [s[0] for s in steps])
    fragmented = sorted({owner[i] for i in fragmented_slides(document, flat)
                         if i < len(owner)})

    pdf_bytes, pages = slide_pages_pdf(document, flat)

    renumbered: list[list[int]] = [[] for _ in range(n_slides)]
    for position, page in enumerate(pages):
        if position < len(owner):
            renumbered[owner[position]].append(page)
    for index, slide in enumerate(renumbered):
        if not slide:
            slide.append(index)

    return Paged(pdf=pdf_bytes, steps=renumbered,
                 folds=folds, fragmented=fragmented)


# ── The fold: where a slide runs out of room ─────────────────────────────────

def measure_folds(document, n_slides: int,
                   page_indices: list[int] | None = None) -> list[int | None]:
    """
    Find, per slide, the first source line whose block runs past the slide.

    Slides are a fixed box with overflow:hidden, so WeasyPrint lays every
    block out and simply clips what does not fit.  Walking the box tree after
    layout therefore says exactly where a slide runs out of room, which a
    word count can only guess at.  The data-src-line attributes put there by
    the renderer turn a y coordinate back into a line the writer can edit.

    *page_indices* says which PDF page each slide starts on, as measured by
    :func:`slide_page_indices`.  It is not optional information once any
    slide overflows: WeasyPrint emits a continuation page for that slide, so
    the n-th page stops being the n-th slide and every fold measured after it
    would be read off the wrong page — naming a line in a slide the writer
    was not told about, and leaving the slide that really overflows unmarked.
    Omitting it means one page per slide, which is what a single-slide render
    render has.

    Returns one entry per slide: the line number, or None when it all fits.
    Any failure yields None rather than a wrong line — the box tree is
    WeasyPrint's internal representation and may change between versions.
    """
    try:
        pages = list(document.pages)
    except Exception:
        return [None] * n_slides

    if page_indices is None:
        page_indices = list(range(n_slides))

    folds: list[int | None] = []
    for index in range(n_slides):
        page = page_indices[index] if index < len(page_indices) else index
        folds.append(
            fold_line_for_page(pages[page]) if 0 <= page < len(pages) else None
        )
    return folds


def fold_line_for_page(page) -> "int | None":
    """First line that runs past the slide's text area on *page*, or None."""
    try:
        boxes = list(walk_boxes(page._page_box))
        limit = content_bottom(boxes, page.height)

        best_y: float | None = None
        best_line: int | None = None

        for box in boxes:
            line = box_line(box)
            if line is None:
                continue
            # Skip containers: a <ul> is stamped with its first item's line,
            # so letting it compete would fold the list at an item that fits.
            # A box counts only when its whole subtree comes from one line.
            if subtree_lines(box) != {line}:
                continue
            # Ignore zero-height boxes, which carry no visible content.
            if box.height <= 0 or box.position_y + box.height <= limit:
                continue
            # Topmost box that crosses: where the slide runs out of room.
            if best_y is None or box.position_y < best_y:
                best_y = box.position_y
                best_line = line
        return best_line
    except Exception:
        log.debug("Fold measurement failed", exc_info=True)
        return None


def box_line(box) -> "int | None":
    """The source line stamped on *box*, if any."""
    element = getattr(box, "element", None)
    if element is None or not hasattr(element, "get"):
        return None
    raw = element.get("data-src-line")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def subtree_lines(box) -> set:
    """Every source line appearing in *box* and its descendants."""
    return {line for line in (box_line(b) for b in walk_boxes(box))
            if line is not None}


def content_bottom(boxes, page_height: float) -> float:
    """
    Bottom of the slide's text area.

    Themes reserve the lower padding for the slide number and progress bar,
    so content reaching into it collides with them — the slide has run out of
    room even though overflow:hidden would not clip until the page edge.
    Measuring against the text area warns at the point the design intends.
    """
    for box in boxes:
        element = getattr(box, "element", None)
        if element is None or not hasattr(element, "get"):
            continue
        classes = (element.get("class") or "").split()
        if "slide" in classes:
            return box.content_box_y() + box.height
    return page_height
