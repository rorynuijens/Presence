"""
test_image_treatments.py — A pinned treatment reaching the page.

Two things are being defended here, and they pull in opposite directions.

**Colour has to be baked into the pixels.** WeasyPrint 68 drops CSS `filter`
and every blend mode at parse time, so a greyscale written as a stylesheet
rule is a greyscale that silently does not happen. `_apply_img_effects()`
re-encodes the file and hands back a data URI, which is the only mechanism
that works in the PDF — and therefore in the thumbnails and the slideshow,
which are pictures of the PDF.

**Arrangement must not be.** Everything a theme could reasonably disagree
with — where the panel is, what it crops to, how faded it is — arrives as a
`data-*` attribute or a `--p-*` custom property, because an inline
declaration outranks every selector. test_layout_attrs.py enforces that for
the automatic path; these do it for the pinned one, which is the path that
can actually carry a value worth overriding.
"""

import re

import pytest
from PIL import Image

from presence.slides.html import md_to_html_slides


@pytest.fixture
def pictures(tmp_path):
    """A picture with obvious colour, so a filter is visible in its bytes."""
    Image.new("RGB", (800, 450), (40, 90, 200)).save(tmp_path / "wide.png")
    Image.new("RGB", (400, 700), (200, 90, 40)).save(tmp_path / "tall.png")
    return str(tmp_path)


def _render(slides, base_url=None):
    return md_to_html_slides(slides, "/* css */", None, {}, base_url=base_url)[0]


def _slide_tag(html, index=0):
    return re.findall(r'<div class="slide["\s][^>]*>', html)[index]


def _inline_styles(html):
    return [d.strip()
            for decls in re.findall(r'style="([^"]*)"', html)
            for d in decls.split(";") if d.strip()]


# ── Arrangement arrives as attributes and measurements ───────────────────────

@pytest.mark.parametrize("block,attr,value", [
    ("{left}",       "data-img-pos",   "left"),
    ("{top}",        "data-img-pos",   "top"),
    ("{background}", "data-img-pos",   "background"),
    ("{contain}",    "data-img-fit",   "contain"),
    ("{align-top}",  "data-img-focal", "focal-top"),
    ("{align-left}", "data-img-focal", "focal-left"),
])
def test_a_pinned_arrangement_reaches_the_slide_div(pictures, block, attr, value):
    html = _render([f"## Heading\n\nSome words.\n\n![a](wide.png){block}"],
                   base_url=pictures)
    assert f'{attr}="{value}"' in _slide_tag(html)


@pytest.mark.parametrize("block", [
    "{contain align-top}",
    "{contain align-bottom}",
    "{contain align-left}",
    "{contain align-right}",
])
def test_alignment_survives_contain(pictures, block):
    """
    Alignment used to be dropped whenever the fit was not cover, which is the
    one mode where all four directions always do something: a letterboxed
    picture has bars to sit against, while a cropped one can only move along
    the axis it actually overflows. So "alignment does nothing" was true, and
    truest exactly where the writer had most reason to expect it to work.
    """
    html = _render([f"## Heading\n\nWords.\n\n![a](wide.png){block}"],
                   base_url=pictures)
    assert "data-img-focal" in _slide_tag(html)


def test_a_gallery_cell_keeps_its_alignment_under_contain(pictures):
    html = _render(["## Gallery\n\n![a](wide.png){contain align-top}\n\n"
                    "![b](wide.png)\n\n![c](wide.png)"], base_url=pictures)
    cells = re.findall(r'<div class="gallery-cell[^>]*>', html)
    assert 'data-img-focal="focal-top"' in cells[0]


def test_the_pair_keeps_its_alignment_under_contain(pictures):
    html = _render(["## Pair\n\nWords between them.\n\n"
                    "![a](tall.png){contain align-top}\n\n![b](tall.png)"],
                   base_url=pictures)
    assert 'data-img-focal="focal-top"' in html


def test_centre_is_the_default_and_says_nothing(pictures):
    """It is what the stylesheet already does, so writing it would be noise."""
    html = _render(["## Heading\n\nWords.\n\n![a](wide.png){align-center}"],
                   base_url=pictures)
    assert "data-img-focal" not in _slide_tag(html)


def test_opacity_arrives_as_a_measurement_not_a_declaration(pictures):
    html = _render(["## Heading\n\nWords.\n\n![a](wide.png){opacity40}"],
                   base_url=pictures)
    assert "--p-img-opacity:0.40" in html


def test_nothing_a_theme_could_argue_with_is_written_inline(pictures):
    """The strong form: every inline declaration is a custom property."""
    html = _render([
        "## Beside\n\nWords.\n\n![a](wide.png){left contain align-top opacity40}",
        "## Gallery\n\n![a](wide.png){greyscale}\n\n"
        "![b](wide.png){opacity30}\n\n![c](tall.png){contain}",
        "## Pair\n\nWords.\n\n![a](tall.png){opacity50}\n\n![b](tall.png){sepia}",
    ], base_url=pictures)
    for decl in _inline_styles(html):
        assert decl.startswith("--p-"), f"not a measurement: {decl}"


def test_no_css_filter_is_ever_emitted(pictures):
    """WeasyPrint drops it, so emitting one would be a silent no-op."""
    html = _render([
        "## A\n\nWords.\n\n![a](wide.png){greyscale}",
        "## B\n\nWords.\n\n![b](wide.png){blur10}",
        "## C\n\nWords.\n\n![c](wide.png){sepia}",
    ], base_url=pictures)
    assert "filter:" not in html
    assert "filter(" not in html


# ── Colour is baked into the file ────────────────────────────────────────────

@pytest.mark.parametrize("block", [
    "{greyscale}", "{bw}", "{sepia}", "{blur8}",
    "{lighten}", "{darken}", "{tint-navy}",
])
def test_a_colour_treatment_is_baked_into_the_picture(pictures, block):
    html = _render([f"## Heading\n\nWords.\n\n![a](wide.png){block}"],
                   base_url=pictures)
    assert "data:image" in html, "the treatment never reached the pixels"


def test_an_untreated_picture_is_left_as_a_path(pictures):
    """Baking every picture would inline the whole deck for no reason."""
    html = _render(["## Heading\n\nWords.\n\n![a](wide.png)"], base_url=pictures)
    assert "data:image" not in html
    assert "wide.png" in html


def test_two_treatments_of_one_picture_differ(pictures):
    """The cache is keyed on the treatment, not only on the file."""
    grey = _render(["## A\n\nWords.\n\n![a](wide.png){greyscale}"],
                   base_url=pictures)
    sepia = _render(["## A\n\nWords.\n\n![a](wide.png){sepia}"],
                    base_url=pictures)
    assert grey != sepia


def test_a_missing_file_falls_back_to_its_path(pictures):
    """A broken path is a diagnostic, not a crash in the baker."""
    html = _render(["## Heading\n\nWords.\n\n![a](nope.png){sepia}"],
                   base_url=pictures)
    assert "nope.png" in html


# ── Every layout honours a treatment, not just the lone picture ──────────────

def test_a_gallery_cell_carries_its_own_treatment(pictures):
    html = _render(["## Gallery\n\n![a](wide.png){greyscale}\n\n"
                    "![b](wide.png)\n\n![c](wide.png)"], base_url=pictures)
    cells = re.findall(r'<div class="gallery-cell[^>]*>.*?</div>', html)
    baked = [c for c in cells if "data:image" in c]
    assert len(baked) == 1, "one cell was treated; only that one may be baked"


def test_a_gallery_cell_can_pin_its_own_fit(pictures):
    html = _render(["## Gallery\n\n![a](wide.png){contain}\n\n"
                    "![b](wide.png)\n\n![c](wide.png)"], base_url=pictures)
    cells = re.findall(r'<div class="gallery-cell[^>]*>', html)
    assert 'data-fit="contain"' in cells[0]


def test_each_half_of_a_pair_keeps_its_own_treatment(pictures):
    html = _render(["## Pair\n\nWords between them.\n\n"
                    "![a](tall.png){sepia}\n\n![b](tall.png)"],
                   base_url=pictures)
    assert 'class="slide has-two-images"' in html
    assert html.count("data:image") == 1


def test_the_pair_honours_a_pinned_fit(pictures):
    html = _render(["## Pair\n\nWords between them.\n\n"
                    "![a](tall.png){contain}\n\n![b](tall.png)"],
                   base_url=pictures)
    assert 'data-img-fit="contain"' in html


# ── full: the slide is the picture ───────────────────────────────────────────

def test_full_does_not_render_the_slides_text(pictures):
    html = _render(["## Heading\n\nThis must not appear.\n\n"
                    "![a](wide.png){full}"], base_url=pictures)
    assert "This must not appear" not in html
    assert "Heading" not in html
    assert 'data-img-pos="background"' in _slide_tag(html)


def test_background_keeps_the_text_over_the_picture(pictures):
    html = _render(["## Heading\n\nThis must appear.\n\n"
                    "![a](wide.png){background}"], base_url=pictures)
    assert "This must appear" in html
    assert 'data-img-pos="background"' in _slide_tag(html)
