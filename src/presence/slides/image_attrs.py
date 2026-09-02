"""
image_attrs.py — What a writer may say about one picture.

Placement is automatic by default: ``layout.py`` reads the slide's own
content and decides. This module is the other half of that sentence — the
attribute block a writer (or the image inspector) puts after an image tag to
overrule one part of the decision::

    ![a red barn](assets/barn.jpg){background contain opacity70 sepia}

Three things about the shape of that syntax are load-bearing.

**It is not the alt text.** Layout used to be a token string inside the
description — ``![a red barn|background|opacity70](barn.jpg)`` — and
``splitter.parse_image_layout()`` still parses exactly that, because
``splitter.py`` is mode ``444``. Nothing here reads its answer and nothing
downstream does either, so an old document goes on laying itself out
automatically and its alt text goes on being read aloud as a description.
The two syntaxes do not meet.

**It must stay on the image's own line.** ``renderer.py`` stamps every block
with ``data-src-line`` and ``pagination.measure_folds()`` reads those stamps
back to find where a slide runs out of room, so a pre-pass that changed the
line count would move every fold below it. ``take_image_attrs()`` therefore
strips the block in place and the pattern cannot span a newline.

**Every override is a layer over AUTO_IMAGE_LAYOUT, never a replacement.**
An image with no block resolves to exactly that dict, which is why a deck
written before any of this renders byte-identically to the way it did.

This module is also the only writer of the syntax. The arrangement this
replaced had four pieces of code that could write layout tokens and two of
them had already drifted apart; ``format_attrs()`` is the one function that
turns a decision back into text.
"""
from __future__ import annotations

import re

from .splitter import _IMAGE_RE
from .layout   import AUTO_IMAGE_LAYOUT

__all__ = ["IMAGE_PLACEMENTS", "IMAGE_FITS", "IMAGE_ALIGNMENTS",
           "IMAGE_FILTERS", "DEFAULT_BLUR",
           "IMAGE_WITH_ATTRS_RE", "IMAGE_ATTRS_GROUP",
           "parse_attrs", "format_attrs", "take_image_attrs",
           "extract_images_with_attrs"]


# ── The vocabulary ────────────────────────────────────────────────────────

# Where the picture goes. "background" fills the slide behind the words;
# "full" fills it *instead of* them — the slide becomes the picture, and its
# text is not rendered at all rather than being covered up by it.
IMAGE_PLACEMENTS = ("left", "right", "top", "bottom", "background", "full")

# Whether the picture crops to fill its panel or letterboxes inside it.
IMAGE_FITS = ("cover", "contain")

# Which part of a cropped picture survives the crop. Spelled `align-*` in the
# document and stored as the `focal` key, which is what css.py already keys
# its object-position rules on.
IMAGE_ALIGNMENTS = ("center", "left", "right", "top", "bottom")

# One at a time. These are the six treatments a writer picks between, not a
# set to combine: "sepia lighten" is a colour grade, and grading is a theme's
# business rather than a per-picture one.
IMAGE_FILTERS = ("bw", "greyscale", "sepia", "blur", "lighten", "darken")

# How much blur `blur` means when the writer does not say. The token accepts
# a strength (`blur12`) because blur is the one filter whose right amount
# depends on the picture rather than on the effect.
DEFAULT_BLUR = 6
_MAX_BLUR    = 20

_OPACITY_RE = re.compile(r'^opacity(\d{1,3})$')
_BLUR_RE    = re.compile(r'^blur(\d{1,2})$')
_TINT_RE    = re.compile(r'^tint-(#[0-9a-fA-F]{3,8}|[a-zA-Z]{2,30})$')

# An image tag with an optional attribute block glued to its closing paren.
# Built from the splitter's own pattern so the two cannot drift: that file is
# read-only and its idea of where a source ends is the one the document is
# actually parsed by. Its two groups are alt and src, so the block is 3.
_ATTR_BLOCK = r'(?:\{([^}\n]*)\})?'
IMAGE_WITH_ATTRS_RE = re.compile(_IMAGE_RE.pattern + _ATTR_BLOCK)

# Which group of IMAGE_WITH_ATTRS_RE holds the block, given that the
# splitter's pattern contributes the groups before it. Published because the
# editor matches against the same pattern to find the tag it is rewriting.
IMAGE_ATTRS_GROUP = _IMAGE_RE.groups + 1


def parse_attrs(token_str: str) -> dict:
    """
    Read an attribute block into overrides for AUTO_IMAGE_LAYOUT.

    Tokens are whitespace-separated and order-insensitive. Unrecognised ones
    are ignored rather than refused — the same tolerance the old token parser
    had, and for the same reason: a typo in a treatment should cost the
    treatment, not the picture.

    Returns only the keys the block actually spoke about, so the caller can
    layer it over the automatic answer and see at a glance what was pinned.
    """
    out: dict = {}
    for raw in (token_str or "").split():
        token = raw.strip().lower()
        if not token:
            continue

        if token in IMAGE_PLACEMENTS:
            out["position"] = token
        elif token in IMAGE_FITS:
            out["fit"] = token
        elif token.startswith("align-") and token[6:] in IMAGE_ALIGNMENTS:
            out["focal"] = f"focal-{token[6:]}"
        elif m := _OPACITY_RE.match(token):
            out["opacity"] = max(0, min(100, int(m.group(1))))
        elif m := _BLUR_RE.match(token):
            # `blur12` is both the filter and its strength.
            out["filter"] = "blur"
            out["blur"] = max(1, min(_MAX_BLUR, int(m.group(1))))
        elif m := _TINT_RE.match(raw.strip()):
            out["tint"] = m.group(1)
        elif token in IMAGE_FILTERS:
            out["filter"] = token
            if token == "blur":
                out.setdefault("blur", DEFAULT_BLUR)
    return out


def format_attrs(attrs: dict) -> str:
    """
    Write overrides back out as an attribute block, without the braces.

    Canonical order, and only what differs from the automatic answer, so the
    inspector rewriting the same picture twice produces the same text and a
    control returned to Automatic leaves nothing behind. Returns "" when
    there is nothing to say, which is the caller's cue to drop the block.
    """
    parts: list[str] = []

    pos = attrs.get("position")
    if pos in IMAGE_PLACEMENTS:
        parts.append(pos)

    fit = attrs.get("fit")
    if fit in IMAGE_FITS:
        parts.append(fit)

    focal = attrs.get("focal")
    if isinstance(focal, str) and focal.startswith("focal-"):
        align = focal[6:]
        if align in IMAGE_ALIGNMENTS:
            parts.append(f"align-{align}")

    opacity = attrs.get("opacity")
    if isinstance(opacity, int) and opacity != AUTO_IMAGE_LAYOUT["opacity"]:
        parts.append(f"opacity{max(0, min(100, opacity))}")

    tint = attrs.get("tint")
    if tint:
        parts.append(f"tint-{tint}")

    filt = attrs.get("filter")
    if filt in IMAGE_FILTERS:
        if filt == "blur":
            blur = attrs.get("blur", DEFAULT_BLUR)
            blur = max(1, min(_MAX_BLUR, int(blur)))
            parts.append("blur" if blur == DEFAULT_BLUR else f"blur{blur}")
        else:
            parts.append(filt)

    return " ".join(parts)


def take_image_attrs(slide_md: str) -> "tuple[str, list[dict]]":
    """
    Strip every attribute block out of *slide_md*, in document order.

    The image tags themselves are left exactly where they are, for the
    splitter to remove — this only takes the braces off the end. Nothing here
    may add or remove a line: what comes back is fed to the renderer, whose
    per-block ``data-src-line`` stamps are what the editor's fold marker and
    the overflow warning are both measured against.

    Returns the markdown without the blocks, and one override dict per image
    (empty where that image carried no block).
    """
    overrides: list[dict] = []

    def _take(m: re.Match) -> str:
        block = m.group(IMAGE_ATTRS_GROUP)
        overrides.append(parse_attrs(block) if block is not None else {})
        if block is None:
            return m.group(0)
        # Keep everything up to the opening brace and drop the rest: when the
        # optional group matched, the closing brace is the match's last
        # character, so what is left is exactly the image tag.
        return m.group(0)[:m.start(IMAGE_ATTRS_GROUP) - m.start() - 1]

    return IMAGE_WITH_ATTRS_RE.sub(_take, slide_md), overrides


def extract_images_with_attrs(slide_md: str) -> "tuple[str, list[dict]]":
    """
    ``splitter.extract_images()``, with each picture's overrides applied.

    The one door to an image's appearance. Every caller that needs a slide's
    pictures — the renderer, the thumbnail strip's reader, the script's word
    count — goes through here, because a caller that used the splitter
    directly would leave the attribute block in the text and count its tokens
    as words the speaker has to say.

    Each image comes back as ``{"src": …, "layout": …, "attrs": …}``.
    "layout" is the resolved appearance — AUTO_IMAGE_LAYOUT updated by
    whatever the block pinned, so no block means that dict unchanged.

    "attrs" is the separate half, and the separation is the point: it holds
    only what the writer actually pinned. ``choose_layout()`` is decided from
    that and never from "layout", because "layout" always has a position in
    it — the automatic one — and a plan that read it could not tell a picture
    placed on the right by the deck's own alternation from one a writer sent
    there on purpose.
    """
    from .splitter import extract_images   # local: keeps the 444 file's import cheap

    stripped, overrides = take_image_attrs(slide_md)
    cleaned, images = extract_images(stripped)

    for i, img in enumerate(images):
        override = overrides[i] if i < len(overrides) else {}
        img["attrs"]  = dict(override)
        img["layout"] = {**AUTO_IMAGE_LAYOUT, "filter": None, **override}

    return cleaned, images
