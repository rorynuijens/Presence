"""
test_layout_render.py — Auto layout as it reaches the page.

Two things are being defended. A slide must never lose an image, which is the
defect auto layout exists to fix. And the tokens an older document may still
carry in its alt text must not reach the page — splitter.py goes on parsing
them, so "ignored" has to be proved rather than assumed.
"""

import re

import pytest

from presence.slides.html import md_to_html_slides


def _render(md: str) -> str:
    return md_to_html_slides([md], "/* css */", None, {})[0]


def _srcs(html: str) -> list[str]:
    return re.findall(r'<img[^>]*src="([^"]+)"', html)


def _kind(html: str) -> str:
    for marker, name in (("has-gallery", "gallery"),
                         ("has-two-images", "pair"),
                         ("has-image", "image")):
        if marker in html:
            return name
    return "text"


def _images(count: int, tokens: str = "") -> str:
    suffix = f"|{tokens}" if tokens else ""
    return "\n\n".join(f"![img{i}{suffix}]({i}.png)" for i in range(count))


# ── No image is ever dropped ─────────────────────────────────────────────────

@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 6, 7, 9, 12])
def test_every_image_reaches_the_slide(count):
    html = _render("## Title\n\n" + _images(count))
    assert len(_srcs(html)) == count


def test_two_untokened_images_both_appear():
    """The reported defect: the second used to vanish."""
    html = _render("## Title\n\n![a](a.png)\n\n![b](b.png)")
    assert _srcs(html) == ["a.png", "b.png"]
    assert _kind(html) == "gallery"


def test_a_third_image_is_not_silently_discarded():
    html = _render("## T\n\n![a](a.png)\n\n![b](b.png)\n\n![c](c.png)")
    assert _srcs(html) == ["a.png", "b.png", "c.png"]


# ── Old tokens do not reach the page ─────────────────────────────────────────

def test_a_position_token_no_longer_places_the_image():
    html = _render("## T\n\ntext\n\n![a|left|30](a.png)")
    assert 'data-img-pos="right"' in html
    assert 'data-img-pos="left"' not in html


def test_a_size_token_no_longer_sets_the_width():
    """30 was the written size; 60 is what this much text earns."""
    html = _render("## T\n\ntext\n\n![a|left|30](a.png)")
    assert "60%" in html
    assert "30%" not in html


def test_background_no_longer_makes_a_backdrop_behind_text():
    html = _render("## T\n\nsome words\n\n![a|background](a.png)")
    assert 'data-img-pos="right"' in html


def test_a_flanking_pair_becomes_a_gallery():
    html = _render("## T\n\n![a|left|30](a.png)\n\n![b|right|30](b.png)")
    assert _kind(html) == "gallery"
    assert _srcs(html) == ["a.png", "b.png"]


def test_treatment_tokens_leave_no_trace_in_the_markup():
    html = _render("## T\n\ntext\n\n![a|tint-#204080|zoom150|flip-h](a.png)")
    assert "slide-image-tint" not in html
    assert "scale(1.5" not in html
    assert "scaleX(-1)" not in html


def test_a_tokened_slide_renders_the_same_as_an_untokened_one():
    """The strongest form of "ignored": identical markup either way."""
    tokened = _render("## T\n\nsome words\n\n"
                      "![a|left|30|nogradient|opacity20|blur4](a.png)")
    bare    = _render("## T\n\nsome words\n\n![a](a.png)")
    assert tokened == bare


# ── An image alone fills the slide ───────────────────────────────────────────

def test_an_unpositioned_image_alone_fills_the_slide():
    html = _render("![a](a.png)")
    assert 'data-img-pos="background"' in html


def test_an_image_with_text_shares_the_slide():
    html = _render("## T\n\nsome words\n\n![a](a.png)")
    assert 'data-img-pos="right"' in html


# ── Grid shape reaches the markup ────────────────────────────────────────────

def test_the_grid_declares_its_columns():
    assert "repeat(2, 1fr)" in _render("## T\n\n" + _images(4))
    assert "repeat(3, 1fr)" in _render("## T\n\n" + _images(6))


def test_three_images_span_the_last_cell():
    html = _render("## T\n\n" + _images(3))
    assert html.count('data-span="2"') == 1


def test_text_sits_above_the_grid():
    html = _render("## Heading\n\n" + _images(3))
    assert '<div class="gallery-text">' in html
    assert html.index("gallery-text") < html.index('class="gallery"')


def test_a_gallery_without_text_has_no_empty_text_block():
    assert "gallery-text" not in _render(_images(4))


# ── Unrelated slides are untouched ───────────────────────────────────────────

@pytest.mark.parametrize("md,expected", [
    ("## T\n\njust words", "text"),
    ("# Title\n\nsubtitle", "text"),
    ("## T\n\nleft\n\n|||\n\nright", "text"),
])
def test_slides_without_images_are_unaffected(md, expected):
    assert _kind(_render(md)) == expected


def test_javascript_urls_are_still_rejected_in_a_gallery():
    html = _render("## T\n\n![a](javascript:alert(1))\n\n![b](b.png)")
    assert "javascript:" not in html
