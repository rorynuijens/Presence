"""
layout.py — Decide how a slide should be laid out.

Slides used to be dispatched on image count alone, and only two arrangements
existed: one image beside text, or two images flanking it. Anything else fell
back to "first image only", so a slide with three pictures silently showed
one. This module decides instead, and the decision is a pure function of the
slide's content so it can be tested without rendering anything.

The rule that governs everything here: **the slide decides.** There is no
longer any way for a writer to place an image by hand, so there is no
explicit choice to defer to and no need to work out whether one was made.
Alt text is a description, and nothing but a description.

``parse_image_layout()`` in splitter.py still parses the old tokens — that
file is read-only — so a document written against the old syntax keeps
opening. Its tokens simply never reach the renderer: everything an image
looks like now comes from AUTO_IMAGE_LAYOUT and the plan chosen here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["LayoutPlan", "AUTO_IMAGE_LAYOUT", "choose_layout",
           "gallery_columns", "auto_size", "cell_fit", "single_side",
           "PAIR_SIZE"]


# How wide each picture of a flanking pair is, leaving the rest for the text
# between them.
PAIR_SIZE = "30"

# The two sides a lone picture can take, in the order they are handed out.
_SIDES = ("right", "left")

# Below this, a picture is taller than it is wide by enough to want a column
# rather than a row. Two of them either side of the text is the arrangement
# that used to need |left| and |right|.
_PORTRAIT_ASPECT = 0.9


# What an automatically placed image looks like. Mostly the values the token
# parser used as its defaults, so a deck written before the tokens went away
# still renders much as it did.
#
# The exception is the gradient, which used to lay the theme's background
# under the edge of every picture and fade it out across 60% of the panel.
# It was there to keep a caption legible over a photograph; with the picture
# beside the words rather than behind them there is nothing to protect, and
# it only softened an edge that reads better hard.
#
# This dict is the single owner of the answer. Nothing else in the renderer
# may read a treatment value off a parsed image.
AUTO_IMAGE_LAYOUT: dict = {
    "position":  "right",
    "size":      "50",
    "opacity":   75,
    "fit":       "cover",
    "focal":     "focal-center",
    "grayscale": 0,
    "blur":      0,
    "tint":      None,
    "flip_h":    False,
    "flip_v":    False,
    "zoom":      100,
}


@dataclass(frozen=True)
class LayoutPlan:
    """
    How to render one slide.

    *kind* selects the renderer:
      "text"    — no images
      "single"  — one image beside text, on the side the plan names
      "bleed"   — one image, no text, filling the slide
      "pair"    — two images flanking text, positions honoured
      "gallery" — a grid; the only arrangement that can hold every image
    """
    kind:     str
    columns:  int = 0          # gallery only
    spans:    tuple = field(default_factory=tuple)  # gallery: cells spanning 2
    size:     str | None = None  # single: chosen width
    position: str | None = None  # single: which side the picture takes


def single_side(ordinal: int) -> str:
    """
    Which side the *ordinal*-th lone picture in the deck takes.

    Right first, then left, then right again. A deck whose every picture sat
    on the same edge read as a template being filled in; alternating gives
    the eye somewhere else to go on the next slide.

    This is the one thing here that is not a function of the slide's own
    content, and it costs what that implies: insert a picture slide near the
    top and every one below it swaps sides. That is the price of the
    alternation, and it was chosen knowing it.
    """
    return _SIDES[ordinal % len(_SIDES)]


def _is_portrait(aspect: "float | None") -> bool:
    """Whether an image is tall enough to want a column of its own."""
    return aspect is not None and aspect < _PORTRAIT_ASPECT


def auto_size(word_count: int) -> str:
    """
    How much width an unsized image should take beside *word_count* words.

    Half and half is the answer until the words stop fitting in half a
    slide, at which point the text takes the extra. A short slide used to
    give the picture 60% — a heading beside a big photograph — but that is
    the same instinct that used to put the heading *on* the photograph, and
    it is not what a title and a picture should look like. The steps are
    coarse on purpose: layout that shifts with every word typed would be
    worse than one that is merely imperfect.
    """
    if word_count <= 40:
        return "50"
    return "40"


def cell_fit(image_aspect: "float | None", cell_aspect: float) -> str:
    """
    Whether a gallery cell should crop its image or letterbox it.

    Cropping looks better than letterboxing until the shapes disagree badly,
    at which point cover throws away most of the picture — a portrait in a
    landscape cell keeps a vertical strip of itself. Past that point showing
    the whole image, bars and all, is the lesser loss.

    The band is wide because the cells are: a 16:9 slide minus its heading
    leaves a short, wide area, so cells run around 2.5:1 and even an ordinary
    landscape photo is a "mismatch" against them. Judging mildly-off shapes
    as mismatches would letterbox nearly everything and leave the grid full
    of gaps. Only genuine disagreement — a portrait in a wide cell, or the
    reverse — earns the bars.
    """
    if not image_aspect or cell_aspect <= 0:
        return "cover"
    ratio = image_aspect / cell_aspect
    return "cover" if 0.45 <= ratio <= 2.2 else "contain"


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


def choose_layout(cleaned_md: str, images: list,
                  aspects: "list | None" = None,
                  side_ordinal: int = 0) -> LayoutPlan:
    """
    Pick a layout for a slide.

    *cleaned_md* is the slide's markdown with its images removed; *images* is
    the list extract_images() returned; *aspects* holds each image's width /
    height where it could be read, and None where it could not.

    *side_ordinal* is how many lone pictures come before this slide in the
    deck, which decides which side this one takes; see single_side().

    Two portraits flanking the text is chosen here rather than written. Where
    the shapes cannot be measured the answer falls back to the gallery, which
    shows everything.

    Kinds: "text", "bleed", "single", "pair", "gallery".
    """
    count = len(images)
    if count == 0:
        return LayoutPlan("text")

    aspects = list(aspects or [])

    def aspect(i: int) -> "float | None":
        return aspects[i] if i < len(aspects) else None

    if count == 1:
        # Nothing to set the picture beside, so it takes the slide. Anything
        # else would be a half-empty slide.
        if not cleaned_md.strip():
            return LayoutPlan("bleed")
        # Everything else goes beside the words rather than behind them.
        # A heading and a picture used to be laid out as a caption over a
        # full-bleed image, which is a strong effect to apply to a slide
        # whose author only wrote a title and dropped in a photograph.
        return LayoutPlan("single",
                          size=auto_size(len(cleaned_md.split())),
                          position=single_side(side_ordinal))

    if count == 2 and cleaned_md.strip():
        # Two tall pictures with text between them is a row of three
        # columns. Two wide ones stacked would leave the text nowhere to go,
        # so those keep the gallery.
        if _is_portrait(aspect(0)) and _is_portrait(aspect(1)):
            return LayoutPlan("pair")

    columns, spans = gallery_columns(count)
    return LayoutPlan("gallery", columns, spans)
