"""
test_image_attrs.py — The attribute block a writer puts on one picture.

Placement is automatic by default, so the thing worth pinning hardest is the
default: a picture with no block must resolve to exactly AUTO_IMAGE_LAYOUT,
because that is what makes every deck written before this feature render the
way it always did.

The second invariant is about line numbers. `renderer.py` stamps every block
with `data-src-line` and `pagination.measure_folds()` reads those stamps back
to find where a slide runs out of room, so a pre-pass that added or removed a
line would move every fold below it. Stripping happens in place.
"""

import pytest

from presence.slides.image_attrs import (DEFAULT_BLUR, format_attrs,
                                         parse_attrs, take_image_attrs,
                                         extract_images_with_attrs)
from presence.slides.layout import AUTO_IMAGE_LAYOUT, choose_layout


# ── Reading a block ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("token,key,value", [
    ("left",        "position", "left"),
    ("background",  "position", "background"),
    ("full",        "position", "full"),
    ("contain",     "fit",      "contain"),
    ("align-top",   "focal",    "focal-top"),
    ("align-left",  "focal",    "focal-left"),
    ("opacity40",   "opacity",  40),
    ("sepia",       "filter",   "sepia"),
    ("bw",          "filter",   "bw"),
    ("darken",      "filter",   "darken"),
    ("tint-navy",   "tint",     "navy"),
])
def test_each_token_pins_one_thing(token, key, value):
    assert parse_attrs(token)[key] == value


def test_tokens_are_order_insensitive():
    a = parse_attrs("left sepia opacity40")
    b = parse_attrs("opacity40 sepia left")
    assert a == b


def test_a_typo_costs_the_treatment_not_the_picture():
    """Unknown tokens are ignored, the way the old parser ignored them."""
    attrs = parse_attrs("lefft sepiaa left")
    assert attrs == {"position": "left"}


def test_blur_carries_its_own_strength():
    assert parse_attrs("blur")["blur"] == DEFAULT_BLUR
    assert parse_attrs("blur12")["blur"] == 12
    assert parse_attrs("blur12")["filter"] == "blur"


def test_out_of_range_values_are_clamped_not_refused():
    assert parse_attrs("opacity400")["opacity"] == 100
    assert parse_attrs("blur99")["blur"] == 20


def test_a_hex_tint_keeps_its_case_for_css():
    assert parse_attrs("tint-#0A3D62")["tint"] == "#0A3D62"


# ── Writing one back ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("block", [
    "left",
    "background contain",
    "left align-top opacity70",
    "full sepia",
    "right cover align-right opacity25 tint-#0a3d62 blur12",
])
def test_a_block_survives_the_round_trip(block):
    """What the panel writes must parse back to what it meant."""
    assert format_attrs(parse_attrs(block)) == block


def test_nothing_pinned_writes_no_block():
    """A picture returned to Automatic loses its braces entirely, rather than
    keeping a block that spells out today's defaults."""
    assert format_attrs({}) == ""
    assert format_attrs({"opacity": 100}) == ""


def test_the_default_blur_is_written_without_its_number():
    assert format_attrs({"filter": "blur", "blur": DEFAULT_BLUR}) == "blur"


# ── Taking it out of the document ────────────────────────────────────────────

def test_the_block_leaves_and_the_tag_stays():
    md = "![a red barn](barn.jpg){left sepia}"
    out, overrides = take_image_attrs(md)
    assert out == "![a red barn](barn.jpg)"
    assert overrides == [{"position": "left", "filter": "sepia"}]


def test_stripping_never_changes_the_line_count():
    """A fold is a line number; a pre-pass that moved one would move them all."""
    md = ("## Heading\n\n![a](a.png){left}\n\ntext\n\n"
          "![b](b.png){background darken}\n\nmore\n")
    out, _ = take_image_attrs(md)
    assert out.count("\n") == md.count("\n")
    assert len(out.splitlines()) == len(md.splitlines())


def test_a_picture_with_no_block_reports_an_empty_override():
    _, overrides = take_image_attrs("![a](a.png)\n\n![b](b.png){full}")
    assert overrides == [{}, {"position": "full"}]


def test_braces_that_follow_no_picture_are_left_alone():
    md = "Some text {not an attribute block} and more."
    out, overrides = take_image_attrs(md)
    assert out == md
    assert overrides == []


# ── The default is the whole point ───────────────────────────────────────────

def test_a_picture_with_no_block_resolves_to_the_automatic_answer():
    _, images = extract_images_with_attrs("![a red barn](barn.jpg)")
    resolved = dict(images[0]["layout"])
    resolved.pop("filter")          # the one key the resolver adds
    assert resolved == AUTO_IMAGE_LAYOUT


def test_an_override_is_a_layer_over_the_automatic_answer():
    """Pinning a filter must not disturb the placement it said nothing about."""
    _, images = extract_images_with_attrs("![a](a.png){sepia}")
    layout = images[0]["layout"]
    assert layout["filter"] == "sepia"
    assert layout["position"] == AUTO_IMAGE_LAYOUT["position"]
    assert layout["size"] == AUTO_IMAGE_LAYOUT["size"]


def test_what_was_pinned_is_kept_apart_from_what_it_resolved_to():
    """
    The plan is decided from "attrs" and never from "layout".

    "layout" always carries a position, because the automatic answer is one —
    so a plan that read it could not tell a picture the deck put on the right
    from one the writer sent there.
    """
    _, images = extract_images_with_attrs("![a](a.png){sepia}")
    assert images[0]["attrs"] == {"filter": "sepia"}
    assert images[0]["layout"]["position"] == "right"


# ── Reaching the plan ────────────────────────────────────────────────────────

PROSE = " ".join(["word"] * 10)


@pytest.mark.parametrize("position,kind", [
    ("left",       "single"),
    ("right",      "single"),
    ("top",        "single"),
    ("bottom",     "single"),
    ("background", "bleed"),
    ("full",       "bleed"),
])
def test_a_pinned_placement_reaches_the_plan(position, kind):
    _, images = extract_images_with_attrs(f"![a](a.png){{{position}}}")
    plan = choose_layout(PROSE, images)
    assert plan.kind == kind


def test_a_pinned_side_beats_the_decks_alternation():
    """single_side() hands out right, then left. A pinned left wins on the
    slide that would have been given right."""
    _, images = extract_images_with_attrs("![a](a.png){left}")
    assert choose_layout(PROSE, images, side_ordinal=0).position == "left"


def test_full_drops_the_text_and_background_keeps_it():
    _, full = extract_images_with_attrs("![a](a.png){full}")
    _, back = extract_images_with_attrs("![a](a.png){background}")
    assert choose_layout(PROSE, full).text_dropped is True
    assert choose_layout(PROSE, back).text_dropped is False


def test_a_treatment_alone_does_not_move_the_picture():
    """Pinning sepia is not a placement, so the deck goes on arranging it."""
    _, images = extract_images_with_attrs("![a](a.png){sepia}")
    plan = choose_layout(PROSE, images, side_ordinal=1)
    assert plan.kind == "single"
    assert plan.position == "left"       # the alternation's answer, not a pin
