"""
test_layout.py — Which layout a slide gets.

The decision is a pure function, so it is tested directly rather than through
a render. Two invariants matter: a slide never loses an image, and the layout
depends on the slide's content alone — never on tokens someone typed into an
image's alt text, which no longer mean anything.
"""

import pytest

from presence.slides.layout import (AUTO_IMAGE_LAYOUT, PAIR_SIZE, LayoutPlan,
                                    auto_size, cell_fit, choose_layout,
                                    gallery_columns, is_caption)

# Text long enough that it wants a column of its own rather than a picture
# to sit on. Used wherever a test is about something other than length.
PROSE = "## Heading\n\n" + "word " * 30
PORTRAITS = [0.6, 0.7]


def _img(position="right"):
    """An image as extract_images() hands it over.

    *position* is deliberately still settable: these tests exist partly to
    prove that setting it changes nothing.
    """
    return {"src": "x.png", "layout": {"position": position}}


# ── The decision ──────────────────────────────────────────────────────────────

def test_no_images_is_a_text_slide():
    assert choose_layout(PROSE, []).kind == "text"


def test_one_image_with_prose_shares_the_slide():
    assert choose_layout(PROSE, [_img()]).kind == "single"


def test_one_image_alone_fills_the_slide():
    assert choose_layout("", [_img()]).kind == "bleed"


def test_two_images_go_to_the_gallery():
    plan = choose_layout(PROSE, [_img(), _img()])
    assert plan.kind == "gallery"
    assert plan.columns == 2


@pytest.mark.parametrize("count", [3, 4, 5, 6, 9, 12])
def test_more_than_two_images_always_reaches_the_gallery(count):
    imgs = [_img() for _ in range(count)]
    assert choose_layout(PROSE, imgs, [0.6] * count).kind == "gallery"


# ── A caption sits on the picture ────────────────────────────────────────────

def test_a_heading_alone_is_a_caption():
    assert is_caption("## Just a heading")


def test_a_paragraph_is_not_a_caption():
    assert not is_caption("## H\n\n" + "word " * 40)


@pytest.mark.parametrize("md", [
    "## H\n\n- a\n- b",
    "## H\n\n1. first\n2. second",
    "## H\n\n> quoted",
    "## H\n\n| a | b |",
    "## H\n\n```\ncode\n```",
])
def test_structure_is_never_a_caption(md):
    """Few words, but a list or table over a photo is unreadable."""
    assert not is_caption(md)


def test_empty_text_is_not_a_caption():
    """Nothing to caption with — that slide is a bleed for another reason."""
    assert not is_caption("   ")


def test_a_captioned_image_fills_the_slide():
    """The automatic form of the old |background| token."""
    assert choose_layout("## A statement", [_img()]).kind == "caption"


def test_a_caption_is_a_different_kind_from_a_wordless_bleed():
    """They render differently: only the caption gets a scrim under it."""
    assert choose_layout("", [_img()]).kind == "bleed"
    assert choose_layout("## Words", [_img()]).kind == "caption"


def test_prose_takes_a_column_back_from_the_picture():
    assert choose_layout(PROSE, [_img()]).kind == "single"


# ── Two portraits flank the text ─────────────────────────────────────────────

def test_two_portraits_with_text_flank_it():
    """The automatic form of the old |left| + |right| pair."""
    plan = choose_layout(PROSE, [_img(), _img()], PORTRAITS)
    assert plan.kind == "pair"


def test_two_landscapes_stay_a_gallery():
    """Stacked wide images would leave the text nowhere to go."""
    assert choose_layout(PROSE, [_img(), _img()], [1.8, 1.6]).kind == "gallery"


def test_a_mixed_pair_stays_a_gallery():
    assert choose_layout(PROSE, [_img(), _img()], [0.6, 1.8]).kind == "gallery"


def test_two_portraits_without_text_stay_a_gallery():
    """A pair is text with pictures either side; with no text it is a grid."""
    assert choose_layout("", [_img(), _img()], PORTRAITS).kind == "gallery"


def test_unmeasurable_shapes_fall_back_to_the_gallery():
    """Which shows both images, so it is the safe direction."""
    assert choose_layout(PROSE, [_img(), _img()], [None, None]).kind == "gallery"
    assert choose_layout(PROSE, [_img(), _img()]).kind == "gallery"


def test_three_portraits_are_not_a_pair():
    imgs = [_img() for _ in range(3)]
    assert choose_layout(PROSE, imgs, [0.6, 0.6, 0.6]).kind == "gallery"


def test_a_pair_leaves_room_for_the_text_between_it():
    assert 0 < int(PAIR_SIZE) * 2 < 100


# ── Tokens no longer decide anything ─────────────────────────────────────────

@pytest.mark.parametrize("position",
                         ["left", "right", "top", "bottom", "background"])
def test_a_position_on_the_image_does_not_change_the_plan(position):
    """An old document's tokens survive parsing but must not reach the plan."""
    assert choose_layout("", [_img(position)]).kind == "bleed"
    assert choose_layout(PROSE, [_img(position)]).kind == "single"


def test_a_written_pair_no_longer_decides_anything():
    """left+right used to mean "put these either side of the text"; now the
    shapes decide, and these two are wide."""
    plan = choose_layout(PROSE, [_img("left"), _img("right")], [1.8, 1.6])
    assert plan.kind == "gallery"


def test_the_plan_ignores_the_layout_dict_entirely():
    """Whatever the parser produced, the same content gets the same plan."""
    tokened = [{"src": "x.png", "layout": {"position": "background",
                                           "size": "20", "blur": 9,
                                           "grayscale": 80, "zoom": 300}}]
    bare    = [{"src": "x.png", "layout": {}}]
    assert choose_layout(PROSE, tokened) == choose_layout(PROSE, bare)


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
    assert choose_layout("word " * 20, [_img()]).size == "50"
    assert choose_layout("word " * 90, [_img()]).size == "40"


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
