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

import re

__all__ = ["LayoutPlan", "AUTO_IMAGE_LAYOUT", "choose_layout",
           "gallery_columns", "auto_size", "cell_fit", "is_caption",
           "PAIR_SIZE"]


# How wide each picture of a flanking pair is, leaving the rest for the text
# between them.
PAIR_SIZE = "30"

# Text this short can sit on a picture without competing with it. Above it,
# the words need a column of their own. The step matches auto_size()'s first
# one, where a slide is already judged to be mostly picture.
_CAPTION_WORDS = 12

# Markdown that wants to be read rather than glanced at. A list or a table
# over a photograph is a legibility problem however few words it holds.
_STRUCTURED_MD = re.compile(
    r"^\s*(?:[-*+]\s|\d+\.\s|>|\||```|    \S)", re.MULTILINE)

# Below this, a picture is taller than it is wide by enough to want a column
# rather than a row. Two of them either side of the text is the arrangement
# that used to need |left| and |right|.
_PORTRAIT_ASPECT = 0.9


# What an automatically placed image looks like. These are the values the
# token parser used as its defaults, kept exactly so that a deck written
# before the tokens went away still renders the way it always did wherever
# its author never overrode them.
#
# This dict is the single owner of the answer. Nothing else in the renderer
# may read a treatment value off a parsed image.
AUTO_IMAGE_LAYOUT: dict = {
    "position":  "right",
    "size":      "50",
    "gradient":  True,
    "opacity":   75,
    "fade":      None,
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
      "single"  — one image beside text, its own position honoured
      "bleed"   — one image, no text, filling the slide
      "pair"    — two images flanking text, positions honoured
      "gallery" — a grid; the only arrangement that can hold every image
    """
    kind:    str
    columns: int = 0          # gallery only
    spans:   tuple = field(default_factory=tuple)   # gallery: cells spanning 2
    size:    str | None = None   # single: chosen width, when none was written


def is_caption(cleaned_md: str) -> bool:
    """
    Whether a slide's text is brief and plain enough to sit on a picture.

    This is the automatic form of the old ``|background|`` token: a heading
    and a few words over a full-bleed image is a deliberate kind of slide,
    and the only thing that made it one was the writer saying so. What makes
    it work is that there is little enough text to read at a glance, and that
    the text is prose rather than a structure that needs alignment to be
    read.
    """
    text = cleaned_md.strip()
    if not text:
        return False
    if _STRUCTURED_MD.search(text):
        return False
    return len(text.split()) <= _CAPTION_WORDS


def _is_portrait(aspect: "float | None") -> bool:
    """Whether an image is tall enough to want a column of its own."""
    return aspect is not None and aspect < _PORTRAIT_ASPECT


def auto_size(word_count: int) -> str:
    """
    How much width an unsized image should take beside *word_count* words.

    A fixed half-and-half wastes the slide when there is a line of text, and
    crowds it when there is a paragraph. The steps are coarse on purpose:
    layout that shifts with every word typed would be worse than one that is
    merely imperfect.
    """
    if word_count <= 12:
        return "60"
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
                  aspects: "list | None" = None) -> LayoutPlan:
    """
    Pick a layout for a slide.

    *cleaned_md* is the slide's markdown with its images removed; *images* is
    the list extract_images() returned; *aspects* holds each image's width /
    height where it could be read, and None where it could not.

    Both arrangements that used to need a token are chosen here instead:
    a full-bleed picture under a caption, and two portraits flanking the
    text. Where the shapes cannot be measured the answer falls back to the
    gallery, which shows everything.

    Kinds: "text", "bleed", "caption", "single", "pair", "gallery".
    """
    count = len(images)
    if count == 0:
        return LayoutPlan("text")

    aspects = list(aspects or [])

    def aspect(i: int) -> "float | None":
        return aspects[i] if i < len(aspects) else None

    if count == 1:
        if not cleaned_md.strip():
            return LayoutPlan("bleed")
        # A caption sits on the picture rather than beside it. It is a
        # separate kind from a wordless bleed because it has to be readable:
        # the renderer lays a scrim under the words, which a picture with no
        # words on it must not get.
        if is_caption(cleaned_md):
            return LayoutPlan("caption")
        return LayoutPlan("single", size=auto_size(len(cleaned_md.split())))

    if count == 2 and cleaned_md.strip():
        # Two tall pictures with text between them is a row of three
        # columns. Two wide ones stacked would leave the text nowhere to go,
        # so those keep the gallery.
        if _is_portrait(aspect(0)) and _is_portrait(aspect(1)):
            return LayoutPlan("pair")

    columns, spans = gallery_columns(count)
    return LayoutPlan("gallery", columns, spans)
