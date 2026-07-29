"""
test_layout.py — Which layout a slide gets.

The decision is a pure function, so it is tested directly rather than through
a render. The invariant that matters most: a slide never loses an image, and
an explicit choice is never overridden.
"""

import pytest

from presence.slides.layout import (LayoutPlan, choose_layout,
                                    gallery_columns, positions_specified)
from presence.slides.splitter import extract_images


def _img(position="right"):
    return {"src": "x.png", "layout": {"position": position}}


# ── Recovering "the writer did not say" ───────────────────────────────────────

def test_no_token_reads_as_unspecified():
    assert positions_specified("![a](x.png)") == [False]


def test_a_position_token_reads_as_specified():
    assert positions_specified("![a|left](x.png)") == [True]


def test_other_tokens_do_not_count_as_a_position():
    assert positions_specified("![a|50|nogradient|blur5](x.png)") == [False]


def test_one_answer_per_image_in_order():
    md = "![a|left](x.png)\n\n![b](y.png)\n\n![c|top](z.png)"
    assert positions_specified(md) == [True, False, True]


def test_it_agrees_with_the_parser_about_what_an_image_is():
    md = "![a|left](x.png)\n\n![b](y.png)"
    _cleaned, images = extract_images(md)
    assert len(positions_specified(md)) == len(images)


# ── The decision ──────────────────────────────────────────────────────────────

def test_no_images_is_a_text_slide():
    assert choose_layout(True, [], []).kind == "text"


def test_one_image_with_text_keeps_todays_layout():
    assert choose_layout(True, [_img()], [False]).kind == "single"


def test_one_image_alone_fills_the_slide():
    assert choose_layout(False, [_img()], [False]).kind == "bleed"


def test_a_chosen_position_is_never_overridden_by_bleed():
    """Phase 2 must not seize a slide where the writer positioned the image."""
    assert choose_layout(False, [_img("left")], [True]).kind == "single"


def test_two_images_the_writer_arranged_keep_the_flanking_layout():
    plan = choose_layout(True, [_img("left"), _img("right")], [True, True])
    assert plan.kind == "pair"


def test_top_and_bottom_is_also_an_arrangement():
    plan = choose_layout(True, [_img("top"), _img("bottom")], [True, True])
    assert plan.kind == "pair"


def test_two_untokened_images_go_to_the_gallery_rather_than_vanishing():
    """The reported defect: both default to 'right' and one used to be lost."""
    plan = choose_layout(True, [_img(), _img()], [False, False])
    assert plan.kind == "gallery"
    assert plan.columns == 2


def test_two_images_in_an_unusable_pair_still_show_both():
    plan = choose_layout(True, [_img("left"), _img("top")], [True, True])
    assert plan.kind == "gallery"


def test_half_specified_pairs_are_not_treated_as_arranged():
    plan = choose_layout(True, [_img("left"), _img("right")], [True, False])
    assert plan.kind == "gallery"


@pytest.mark.parametrize("count", [3, 4, 5, 6, 9, 12])
def test_more_than_two_images_always_reaches_the_gallery(count):
    images = [_img() for _ in range(count)]
    plan = choose_layout(True, images, [False] * count)
    assert plan.kind == "gallery"


def test_explicit_positions_cannot_shrink_a_large_set():
    """Three images cannot be a 'pair' however they are tokened."""
    images = [_img("left"), _img("right"), _img("top")]
    assert choose_layout(True, images, [True, True, True]).kind == "gallery"


def test_a_short_specified_list_is_safe():
    plan = choose_layout(True, [_img(), _img()], [])
    assert plan.kind == "gallery"


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
