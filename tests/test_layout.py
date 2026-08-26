"""
test_layout.py — Which layout a slide gets.

The decision is a pure function, so it is tested directly rather than through
a render. Two invariants matter: a slide never loses an image, and the layout
depends on the slide's content alone — never on tokens someone typed into an
image's alt text, which no longer mean anything.
"""

import pytest

from presence.slides.layout import (AUTO_IMAGE_LAYOUT, LayoutPlan, auto_size,
                                    cell_fit, choose_layout, gallery_columns)


def _img(position="right"):
    """An image as extract_images() hands it over.

    *position* is deliberately still settable: these tests exist partly to
    prove that setting it changes nothing.
    """
    return {"src": "x.png", "layout": {"position": position}}


# ── The decision ──────────────────────────────────────────────────────────────

def test_no_images_is_a_text_slide():
    assert choose_layout(True, []).kind == "text"


def test_one_image_with_text_shares_the_slide():
    assert choose_layout(True, [_img()]).kind == "single"


def test_one_image_alone_fills_the_slide():
    assert choose_layout(False, [_img()]).kind == "bleed"


def test_two_images_go_to_the_gallery():
    plan = choose_layout(True, [_img(), _img()])
    assert plan.kind == "gallery"
    assert plan.columns == 2


@pytest.mark.parametrize("count", [3, 4, 5, 6, 9, 12])
def test_more_than_two_images_always_reaches_the_gallery(count):
    assert choose_layout(True, [_img() for _ in range(count)]).kind == "gallery"


# ── Tokens no longer decide anything ─────────────────────────────────────────

@pytest.mark.parametrize("position",
                         ["left", "right", "top", "bottom", "background"])
def test_a_position_on_the_image_does_not_change_the_plan(position):
    """An old document's tokens survive parsing but must not reach the plan."""
    assert choose_layout(False, [_img(position)]).kind == "bleed"
    assert choose_layout(True, [_img(position)]).kind == "single"


def test_a_flanking_pair_is_no_longer_special():
    """left+right used to mean "put these either side of the text"."""
    assert choose_layout(True, [_img("left"), _img("right")]).kind == "gallery"


def test_top_and_bottom_is_no_longer_special_either():
    assert choose_layout(True, [_img("top"), _img("bottom")]).kind == "gallery"


def test_the_plan_ignores_the_layout_dict_entirely():
    """Whatever the parser produced, the same content gets the same plan."""
    tokened = [{"src": "x.png", "layout": {"position": "background",
                                           "size": "20", "blur": 9,
                                           "grayscale": 80, "zoom": 300}}]
    bare    = [{"src": "x.png", "layout": {}}]
    assert choose_layout(True, tokened, 30) == choose_layout(True, bare, 30)


# ── One owner for what an auto-placed image looks like ───────────────────────

def test_auto_layout_covers_every_key_the_renderer_reads():
    """A missing key would silently fall back to a .get() default elsewhere,
    which is exactly the drift this dict exists to prevent."""
    assert set(AUTO_IMAGE_LAYOUT) == {
        "position", "size", "gradient", "opacity", "fade", "fit", "focal",
        "grayscale", "blur", "tint", "flip_h", "flip_v", "zoom",
    }


def test_auto_layout_applies_no_treatment():
    assert AUTO_IMAGE_LAYOUT["grayscale"] == 0
    assert AUTO_IMAGE_LAYOUT["blur"] == 0
    assert AUTO_IMAGE_LAYOUT["zoom"] == 100
    assert AUTO_IMAGE_LAYOUT["tint"] is None
    assert AUTO_IMAGE_LAYOUT["flip_h"] is False
    assert AUTO_IMAGE_LAYOUT["flip_v"] is False


# ── Grid shape ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("count,columns", [
    (2, 2), (3, 2), (4, 2), (5, 3), (6, 3), (9, 3),
])
def test_column_counts(count, columns):
    assert gallery_columns(count)[0] == columns


def test_three_images_read_as_two_over_one():
    """Two side by side, then the third across the full width.

    The spanning cell must be the last one; spanning the second instead puts
    every image on its own row and overflows the slide.
    """
    columns, spans = gallery_columns(3)
    assert columns == 2
    assert spans == (3,)


def test_even_grids_have_no_spanning_cells():
    for count in (2, 4, 6):
        assert gallery_columns(count)[1] == ()


# ── Width chosen from how much text shares the slide ─────────────────────────

def test_a_line_of_text_leaves_the_image_most_of_the_slide():
    assert auto_size(6) == "60"


def test_a_paragraph_takes_room_back_from_the_image():
    assert auto_size(90) == "40"


def test_the_middle_case_is_the_old_fixed_half():
    assert auto_size(25) == "50"


def test_the_width_always_comes_from_the_word_count():
    """There is no longer a written size that could suppress this."""
    assert choose_layout(True, [_img()], word_count=6).size == "60"
    assert choose_layout(True, [_img()], word_count=90).size == "40"


# ── Cropping versus letterboxing ─────────────────────────────────────────────

def test_a_shape_close_to_its_cell_is_cropped():
    assert cell_fit(1.5, 1.6) == "cover"


def test_an_ordinary_photo_in_a_wide_cell_is_still_cropped():
    """Cells run about 2.5:1; treating that as a mismatch would letterbox
    nearly every picture and leave the grid full of gaps."""
    assert cell_fit(4 / 3, 2.5) == "cover"


def test_a_portrait_in_a_wide_cell_is_shown_whole():
    assert cell_fit(2 / 3, 2.5) == "contain"


def test_a_panorama_in_a_tall_cell_is_shown_whole():
    assert cell_fit(4.0, 0.6) == "contain"


def test_an_unknown_shape_falls_back_to_cropping():
    assert cell_fit(None, 2.5) == "cover"
    assert cell_fit(1.5, 0) == "cover"
