"""
test_layout_render.py — Auto layout as it reaches the page.

Two things are being defended. A slide must never lose an image, which is the
defect auto layout exists to fix. And a slide the writer arranged must render
exactly as it did before, which is the promise that makes the change safe to
ship.
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


# ── What the writer arranged is left alone ───────────────────────────────────

def test_left_and_right_still_flank_the_text():
    html = _render("## T\n\n![a|left|30](a.png)\n\n![b|right|30](b.png)")
    assert _kind(html) == "pair"


def test_top_and_bottom_still_split_vertically():
    html = _render("## T\n\n![a|top](a.png)\n\n![b|bottom](b.png)")
    assert _kind(html) == "pair"


def test_a_positioned_single_image_keeps_its_position():
    html = _render("## T\n\ntext\n\n![a|left|30](a.png)")
    assert 'data-img-pos="left"' in html
    assert "30%" in html


def test_a_positioned_image_alone_is_not_forced_to_fill():
    """Phase 2 must not seize a slide the writer positioned."""
    html = _render("![a|left|30](a.png)")
    assert 'data-img-pos="left"' in html


def test_per_image_effects_survive_the_gallery():
    html = _render("## T\n\n![a|nogradient](a.png)\n\n![b](b.png)")
    assert len(_srcs(html)) == 2


# ── An image alone fills the slide ───────────────────────────────────────────

def test_an_unpositioned_image_alone_fills_the_slide():
    html = _render("![a](a.png)")
    assert 'data-img-pos="background"' in html


def test_an_image_with_text_still_shares_the_slide():
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
