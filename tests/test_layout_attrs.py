"""
test_layout_attrs.py — What the engine publishes for a stylesheet to arrange by.

A theme cannot count words or read an image header, so it can only arrive at
its own arrangement if the engine says what it measured. These tests pin the
two halves of that: the facts `layout.py` records on every plan, and their
arrival on the slide div.

The last group is the one that stops the seam closing again. Every
size-dependent decision used to be written onto the element as an inline
style, which outranks every selector a theme can write — so a rule that puts
a raw `width` or `object-fit` back inline is not a style choice, it is the
removal of a theme's ability to disagree.
"""

import re

from PIL import Image

import pytest

from presence.slides.html import md_to_html_slides
from presence.slides.layout import (TEXT_LONG_WORDS, choose_layout, shape_of,
                                    text_weight)
from presence.slides.css import build_css
from presence.slides.themes import BUILTIN_THEMES


# ── Helpers ──────────────────────────────────────────────────────────────────

def _slide_div(html: str, index: int = 0) -> str:
    """The opening tag of the *index*-th slide div."""
    tags = re.findall(r'<div class="slide["\s][^>]*>', html)
    return tags[index]


def _attr(tag: str, name: str) -> "str | None":
    m = re.search(rf'{name}="([^"]*)"', tag)
    return m.group(1) if m else None


def _render(slides: list[str], base_url: "str | None" = None) -> str:
    return md_to_html_slides(slides, "/* css */", None, {}, base_url=base_url)[0]


@pytest.fixture
def pictures(tmp_path):
    """Three images of known shape, and the base_url that finds them."""
    Image.new("RGB", (800, 450), "navy").save(tmp_path / "wide.png")
    Image.new("RGB", (400, 700), "teal").save(tmp_path / "tall.png")
    Image.new("RGB", (500, 500), "olive").save(tmp_path / "square.png")
    # Tall enough to disagree with a gallery cell rather than merely differ
    # from it; see cell_fit() on why the crop band is as wide as it is.
    Image.new("RGB", (300, 1000), "maroon").save(tmp_path / "sliver.png")
    return str(tmp_path)


# ── text_weight: the threshold auto_size already used, named ────────────────

def test_text_weight_names_the_three_cases():
    assert text_weight("") == "none"
    assert text_weight("   \n  ") == "none"
    assert text_weight("a few words") == "short"
    assert text_weight("word " * (TEXT_LONG_WORDS + 1)) == "long"


def test_text_weight_boundary_is_the_size_threshold():
    assert text_weight("word " * TEXT_LONG_WORDS) == "short"
    assert text_weight("word " * (TEXT_LONG_WORDS + 1)) == "long"


# ── shape_of ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("aspect, expected", [
    (None,  "unknown"),
    (0.5,   "portrait"),
    (0.89,  "portrait"),
    (1.0,   "square"),
    (1.14,  "square"),
    (1.78,  "landscape"),
])
def test_shape_of(aspect, expected):
    assert shape_of(aspect) == expected


def test_shape_of_agrees_with_the_pair_decision():
    """The pair exists for two portraits, so that word must mean what it did."""
    plan = choose_layout("text", [{"src": "a"}, {"src": "b"}], [0.6, 0.6])
    assert plan.kind == "pair"
    assert plan.shapes == ("portrait", "portrait")


# ── Every plan carries the facts, whatever it decided ───────────────────────

@pytest.mark.parametrize("md, images, aspects, kind, text, count", [
    ("",                    [],                       [],          "text",    "none",  0),
    ("a b c",               [],                       [],          "text",    "short", 0),
    ("",                    [{"src": "a"}],           [1.7],       "bleed",   "none",  1),
    ("a b c",               [{"src": "a"}],           [1.7],       "single",  "short", 1),
    ("word " * 60,          [{"src": "a"}],           [1.7],       "single",  "long",  1),
    ("hi", [{"src": "a"}, {"src": "b"}],              [0.6, 0.6],  "pair",    "short", 2),
    ("hi", [{"src": "a"}, {"src": "b"}, {"src": "c"}], [1.7, 1.7, 1.7], "gallery", "short", 3),
])
def test_every_plan_reports_what_it_was_decided_from(md, images, aspects,
                                                     kind, text, count):
    plan = choose_layout(md, images, aspects)
    assert plan.kind == kind
    assert plan.text == text
    assert plan.images == count
    assert len(plan.shapes) == count


def test_shapes_records_an_unreadable_image_rather_than_omitting_it():
    """A shape that could not be measured still occupies its position."""
    plan = choose_layout("hi", [{"src": "a"}, {"src": "b"}, {"src": "c"}],
                         [1.7, None, 0.5])
    assert plan.shapes == ("landscape", "unknown", "portrait")


# ── The facts reach the slide div ───────────────────────────────────────────

def test_a_text_slide_says_so(pictures):
    tag = _slide_div(_render(["## Heading\n\nSome words."]))
    assert _attr(tag, "data-layout") == "text"
    assert _attr(tag, "data-text") == "short"
    assert _attr(tag, "data-images") == "0"


def test_the_cover_is_named_as_the_cover():
    """A title slide is dispatched before any layout is chosen, and says so."""
    tag = _slide_div(_render(["# Just a Title"]))
    assert "title-slide" in tag
    assert _attr(tag, "data-layout") == "title"


@pytest.mark.parametrize("md, kind, shapes", [
    ("![a](wide.png)",                              "bleed",   "landscape"),
    ("## Words\n\n![a](wide.png)",                  "single",  "landscape"),
    ("## Words\n\n![a](tall.png)\n\n![b](tall.png)", "pair",   "portrait portrait"),
])
def test_image_layouts_publish_their_shapes(pictures, md, kind, shapes):
    tag = _slide_div(_render([md], base_url=pictures))
    assert _attr(tag, "data-layout") == kind
    assert _attr(tag, "data-shapes") == shapes


def test_a_gallery_reports_every_picture(pictures):
    md = "## Four\n\n" + "\n\n".join(
        f"![x]({n}.png)" for n in ("wide", "tall", "square", "wide"))
    tag = _slide_div(_render([md], base_url=pictures))
    assert _attr(tag, "data-layout") == "gallery"
    assert _attr(tag, "data-images") == "4"
    assert _attr(tag, "data-shapes") == "landscape portrait square landscape"


def test_a_gallery_cell_carries_its_own_shape_and_fit(pictures):
    """The slide's list cannot address one cell, so each cell repeats itself."""
    md = "## Two\n\n![a](wide.png)\n\n![b](sliver.png)"
    html = _render([md], base_url=pictures)
    cells = re.findall(r'<div class="gallery-cell"[^>]*>', html)
    assert len(cells) == 2
    assert _attr(cells[0], "data-shape") == "landscape"
    assert _attr(cells[1], "data-shape") == "portrait"
    # An ordinary portrait still crops — the cells are wide, so mild
    # disagreement is not enough. Only a shape this far off earns the bars.
    assert _attr(cells[0], "data-fit") == "cover"
    assert _attr(cells[1], "data-fit") == "contain"


def test_a_new_layout_cannot_ship_without_the_facts(pictures):
    """Every slide of every kind, stamped at the one dispatch site."""
    html = _render([
        "# Cover",
        "## Text only",
        "![a](wide.png)",
        "## Beside\n\n![a](wide.png)",
        "## Pair\n\n![a](tall.png)\n\n![b](tall.png)",
        "## Gallery\n\n![a](wide.png)\n\n![b](wide.png)\n\n![c](wide.png)",
    ], base_url=pictures)
    tags = re.findall(r'<div class="slide["\s][^>]*>', html)
    assert len(tags) == 6
    for tag in tags:
        for name in ("data-layout", "data-text", "data-images",
                     "data-slide-index"):
            assert _attr(tag, name) is not None, f"{name} missing from {tag}"


# ── The measurements stay out of the way of a stylesheet ────────────────────

# Properties a theme has a real reason to override. Any of these written as an
# inline style outranks every selector, so the arrangement stops being the
# theme's to decide.
_RESERVED = ("width", "height", "top", "left", "right", "bottom",
             "padding-left", "padding-right", "padding-top", "padding-bottom",
             "object-fit", "opacity", "grid-template-columns", "grid-auto-rows")


def test_no_arrangement_property_is_written_inline(pictures):
    html = _render([
        "# Cover",
        "![a](wide.png)",
        "## Beside\n\n![a](wide.png)",
        "## Long\n\n" + "word " * 60 + "\n\n![a](wide.png)",
        "## Pair\n\n![a](tall.png)\n\n![b](tall.png)",
        "## Gallery\n\n![a](wide.png)\n\n![b](tall.png)\n\n![c](square.png)",
    ], base_url=pictures)
    offenders = []
    for decls in re.findall(r'style="([^"]*)"', html):
        for decl in decls.split(";"):
            name = decl.split(":")[0].strip()
            if name in _RESERVED:
                offenders.append(decl.strip())
    assert not offenders, f"inline arrangement styles: {offenders}"


def test_what_is_written_inline_is_only_a_measurement(pictures):
    """Inline styles are custom properties, and nothing else."""
    html = _render([
        "## Beside\n\n![a](wide.png)",
        "## Gallery\n\n![a](wide.png)\n\n![b](wide.png)\n\n![c](wide.png)",
    ], base_url=pictures)
    for decls in re.findall(r'style="([^"]*)"', html):
        for decl in filter(None, (d.strip() for d in decls.split(";"))):
            assert decl.startswith("--p-"), f"not a measurement: {decl}"


@pytest.mark.parametrize("var, consumer", [
    ("--p-img-size",        ".slide.has-image[data-img-pos=\"right\"] .slide-image"),
    ("--p-img-pad",         ".slide.has-image[data-img-pos=\"right\"] .slide-text"),
    ("--p-gallery-columns", ".slide.has-gallery .gallery"),
    ("--p-gallery-row",     ".slide.has-gallery .gallery"),
    ("--p-progress",        ".progress-bar-fill"),
])
def test_the_stylesheet_is_what_consumes_each_measurement(var, consumer):
    """
    Each published measurement has a rule reading it. Without one the value
    would be inert and the arrangement would have quietly moved back inline.
    """
    css = build_css(BUILTIN_THEMES["light"], 1280, 720, None)
    assert consumer in css
    assert f"var({var}" in css
