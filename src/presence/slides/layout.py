"""
layout.py — Decide how a slide should be laid out.

Slides used to be dispatched on image count alone, and only two arrangements
existed: one image beside text, or two images flanking it. Anything else fell
back to "first image only", so a slide with three pictures silently showed
one. This module decides instead, and the decision is a pure function of the
slide's content so it can be tested without rendering anything.

The rule that governs everything here: **an explicit choice always wins.**
Auto layout is what happens when the writer did not say, never a correction
of what they did say.

Knowing whether they said anything is harder than it looks. The parser seeds
``position`` with "right" before anyone sees the image, so a layout dict
cannot distinguish "the writer typed |right|" from "the writer typed
nothing". positions_specified() recovers that by asking the same question of
the same markdown, using the parser's own regex and vocabulary rather than a
second copy of either.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .splitter import _IMAGE_RE, IMAGE_POSITIONS

__all__ = ["LayoutPlan", "positions_specified", "choose_layout",
           "gallery_columns"]


# Position pairs the two-image renderer understands. Anything else used to
# drop an image on the floor.
_KNOWN_PAIRS = frozenset({("left", "right"), ("right", "left"),
                          ("top", "bottom"), ("bottom", "top")})


@dataclass(frozen=True)
class LayoutPlan:
    """
    How to render one slide.

    *kind* selects the renderer:
      "text"    — no images
      "single"  — one image beside text, its own position honoured
      "bleed"   — one image, no text, filling the slide
      "pair"    — two images flanking text, positions honoured
      "gallery" — a grid; the only arrangement that can hold every image
    """
    kind:    str
    columns: int = 0          # gallery only
    spans:   tuple = field(default_factory=tuple)   # gallery: cells spanning 2


def positions_specified(slide_md: str) -> list[bool]:
    """
    Whether each image in *slide_md* carries a position token, in order.

    Uses the parser's regex and position vocabulary, so this cannot drift
    from what parse_image_layout() accepts.
    """
    return [
        any(token.strip().lower() in IMAGE_POSITIONS
            for token in match.group(1).split("|"))
        for match in _IMAGE_RE.finditer(slide_md)
    ]


def gallery_columns(count: int) -> tuple[int, tuple]:
    """
    Column count for *count* images, and which cells span two columns.

    Two and four sit in a square-ish grid; three reads better as two over a
    full-width third than as three narrow strips on a 16:9 slide. Beyond
    four, three columns keeps each picture large enough to be worth showing.
    """
    if count <= 2:
        return 2, ()
    if count == 3:
        return 2, (3,)        # the third — the last — spans both columns
    if count == 4:
        return 2, ()
    return 3, ()


def choose_layout(has_text: bool, images: list, specified: list) -> LayoutPlan:
    """
    Pick a layout for a slide.

    *images* is the list extract_images() returned; *specified* is the
    matching list from positions_specified(). A short *specified* is treated
    as "not specified", which is the safe direction: it can only route a
    slide to the gallery, which shows everything.
    """
    count = len(images)
    if count == 0:
        return LayoutPlan("text")

    def was_specified(i: int) -> bool:
        return specified[i] if i < len(specified) else False

    if count == 1:
        # An image alone on a slide should fill it. With text present, or
        # with a position the writer chose, leave today's behaviour alone.
        if not has_text and not was_specified(0):
            return LayoutPlan("bleed")
        return LayoutPlan("single")

    if count == 2:
        pair = (images[0]["layout"].get("position"),
                images[1]["layout"].get("position"))
        # Only honour the flanking layout when the writer actually asked for
        # it. Two seeded defaults look like ("right", "right") — not a pair,
        # and previously the second image simply vanished.
        if was_specified(0) and was_specified(1) and pair in _KNOWN_PAIRS:
            return LayoutPlan("pair")
        columns, spans = gallery_columns(2)
        return LayoutPlan("gallery", columns, spans)

    columns, spans = gallery_columns(count)
    return LayoutPlan("gallery", columns, spans)
