"""
layout.py — Decide how a slide should be laid out.

Slides used to be dispatched on image count alone, and only two arrangements
existed: one image beside text, or two images flanking it. Anything else fell
back to "first image only", so a slide with three pictures silently showed
one. This module decides instead, and the decision is a pure function of the
slide's content so it can be tested without rendering anything.

The rule that governs everything here: **the slide decides, unless it was
told.** Placement is automatic by default — every picture with nothing
pinned on it is arranged from the slide's own content, which is what makes a
deck of them look like one deck. A writer who wants a particular picture
somewhere particular says so in an attribute block after the tag, and that
one decision is deferred to; see ``image_attrs.py`` for the syntax.

What is deferred to is the block, never the alt text. ``parse_image_layout()``
in splitter.py still parses the retired token string — that file is read-only
— and nothing reads its answer, here or downstream. So a document written
against the old syntax still opens, still reads its alt text as a
description, and still lays itself out automatically.

AUTO_IMAGE_LAYOUT remains the answer for everything nobody pinned. An
override is a layer over it, never a replacement for it, so a picture that
pins only its filter is still placed by the rules below.
"""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["LayoutPlan", "AUTO_IMAGE_LAYOUT", "choose_layout",
           "gallery_columns", "auto_size", "cell_fit", "single_side",
           "shape_of", "text_weight", "PAIR_SIZE", "TEXT_LONG_WORDS"]


# How wide each picture of a flanking pair is, leaving the rest for the text
# between them.
PAIR_SIZE = "30"

# The two sides a lone picture can take, in the order they are handed out.
_SIDES = ("right", "left")

# Below this, a picture is taller than it is wide by enough to want a column
# rather than a row. Two of them either side of the text is the arrangement
# that used to need |left| and |right|.
_PORTRAIT_ASPECT = 0.9

# Above this, a picture is wide enough to read as landscape rather than
# square. Nothing here branches on the difference; it is published so a
# stylesheet can.
_LANDSCAPE_ASPECT = 1.15

# Above this many words the text stops fitting in half a slide and takes the
# extra width. auto_size() acts on it; text_weight() names it.
TEXT_LONG_WORDS = 40


# What an automatically placed image looks like. Mostly the values the token
# parser used as its defaults, so a deck written before the tokens went away
# still renders much as it did.
#
# Two exceptions, and both were there to keep words legible on top of a
# picture. The gradient laid the theme's background under a picture's edge
# and faded it across 60% of the panel; the opacity washed the whole picture
# out to three-quarters strength. With the picture beside the words rather
# than behind them there is nothing to protect, so the gradient is gone and
# the picture is shown at full strength — which is what the gallery has
# always done, since it never read this value at all.
#
# This dict is the single owner of the *default* answer, and the base every
# override is layered over. Nothing in the renderer may invent a treatment
# value of its own: what it reads is this dict updated by the picture's own
# attribute block, resolved once in image_attrs.extract_images_with_attrs().
AUTO_IMAGE_LAYOUT: dict = {
    "position":  "right",
    "size":      "50",
    "opacity":   100,
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

    # What the decision was made from, rather than what it decided. These
    # are published on the slide div so a stylesheet can arrive at its own
    # arrangement; see THEME-CONTRACT.md.
    text:     str = "none"     # "none" | "short" | "long"
    images:   int = 0
    shapes:   tuple = field(default_factory=tuple)  # per image, see shape_of()

    # What the writer pinned on each picture, in image order — one dict per
    # image, empty where nothing was pinned. Carried on the plan rather than
    # looked up again downstream so that the arrangement and the appearance
    # are decided from the same reading of the document.
    treatments: tuple = field(default_factory=tuple)

    # True when the slide is the picture: `full` was pinned, so the text is
    # not rendered rather than being covered over by it.
    text_dropped: bool = False


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


def shape_of(aspect: "float | None") -> str:
    """
    Name an image's proportions: portrait, landscape, square, or unknown.

    Only "portrait" changes any decision here — it is what earns two pictures
    the flanking pair. The other three are published rather than used, because
    a stylesheet may well want to treat a tall picture differently from a wide
    one in a gallery cell, and it cannot measure the file itself.
    """
    if aspect is None:
        return "unknown"
    if aspect < _PORTRAIT_ASPECT:
        return "portrait"
    if aspect > _LANDSCAPE_ASPECT:
        return "landscape"
    return "square"


def text_weight(cleaned_md: str) -> str:
    """
    How much of the slide the words want: "none", "short" or "long".

    The same threshold auto_size() sizes a picture by, named so it can be
    selected on. "none" is the condition that makes a lone picture a bleed.
    """
    words = len(cleaned_md.split())
    if words == 0:
        return "none"
    return "short" if words <= TEXT_LONG_WORDS else "long"


def _is_portrait(aspect: "float | None") -> bool:
    """Whether an image is tall enough to want a column of its own."""
    return shape_of(aspect) == "portrait"


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
    if word_count <= TEXT_LONG_WORDS:
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

    An image may pin its own placement in an attribute block, which is read
    from each image's "attrs" — what the writer asked for — and never from
    its "layout", which always carries a placement because the automatic
    answer is one. A pinned placement is honoured for a lone picture; on a
    slide of several, arrangement is the grid's and only the treatments are
    the writer's, because "put this one on the left" has no answer that keeps
    the others somewhere sensible.

    Kinds: "text", "bleed", "single", "pair", "gallery".
    """
    count = len(images)
    aspects = list(aspects or [])

    def aspect(i: int) -> "float | None":
        return aspects[i] if i < len(aspects) else None

    def pinned(i: int) -> dict:
        img = images[i]
        return img.get("attrs") or {} if isinstance(img, dict) else {}

    treatments = tuple(pinned(i) for i in range(count))

    # The facts every plan carries, whatever it decides.
    facts = dict(text=text_weight(cleaned_md), images=count,
                 shapes=tuple(shape_of(aspect(i)) for i in range(count)),
                 treatments=treatments)

    if count == 0:
        return LayoutPlan("text", **facts)

    if count == 1:
        position = treatments[0].get("position")

        # The slide *is* the picture: the words are not rendered, so there is
        # nothing for it to sit beside whatever the slide says.
        if position == "full":
            return LayoutPlan("bleed", text_dropped=True, **facts)

        # Behind the words rather than beside them — the one arrangement the
        # automatic rules will not choose, because it is a decision about
        # legibility that only the writer can make.
        if position == "background":
            return LayoutPlan("bleed", **facts)

        if position in ("left", "right", "top", "bottom"):
            return LayoutPlan("single",
                              size=auto_size(len(cleaned_md.split())),
                              position=position,
                              **facts)

        # Nothing to set the picture beside, so it takes the slide. Anything
        # else would be a half-empty slide.
        if not cleaned_md.strip():
            return LayoutPlan("bleed", **facts)
        # Everything else goes beside the words rather than behind them.
        # A heading and a picture used to be laid out as a caption over a
        # full-bleed image, which is a strong effect to apply to a slide
        # whose author only wrote a title and dropped in a photograph.
        return LayoutPlan("single",
                          size=auto_size(len(cleaned_md.split())),
                          position=single_side(side_ordinal),
                          **facts)

    if count == 2 and cleaned_md.strip():
        # Two tall pictures with text between them is a row of three
        # columns. Two wide ones stacked would leave the text nowhere to go,
        # so those keep the gallery.
        if _is_portrait(aspect(0)) and _is_portrait(aspect(1)):
            return LayoutPlan("pair", **facts)

    columns, spans = gallery_columns(count)
    return LayoutPlan("gallery", columns, spans, **facts)
