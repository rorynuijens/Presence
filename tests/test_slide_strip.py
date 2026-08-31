"""
test_slide_strip.py — The thumbnail strip is not allowed to lie.

The strip is the deck's map, and it was drawing a wrong one.  Pictures come
from a build and the text moves on between builds, so the two lists have to be
matched up somehow; matching them by position meant that inserting a slide at
the top shifted every picture below it onto the wrong row — correct title,
someone else's picture — and left it there until the next build.

Two rules are pinned down here:

*  A picture follows its slide, not its index.
*  Everything the document alone can answer — title, script indicator, timing
   — is read out of the document, so it is never behind by a build.

The strip is a widget and this suite has no backend to build one in (see
conftest.py), so the matching and the reading are exercised as the plain
functions they were split into.
"""

import pytest

from presence.sidebar import RowState, carry_over, read_slides


def states(*rows) -> list:
    """Rows on screen: (key, picture) pairs, all of them freshly built."""
    return [RowState(key=k, thumbnail=t) for k, t in rows]


# ── A picture belongs to a slide, not to a row number ─────────────────────────

def test_a_slide_inserted_at_the_top_does_not_shift_every_picture_down():
    """The bug this replaces: A/B/C kept their titles and swapped pictures."""
    before = states(("a", "pic-a"), ("b", "pic-b"), ("c", "pic-c"))

    after = carry_over(before, ["new", "a", "b", "c"])

    assert [r.thumbnail for r in after] == [None, "pic-a", "pic-b", "pic-c"]


def test_a_slide_deleted_from_the_middle_does_not_shift_the_rest_up():
    before = states(("a", "pic-a"), ("b", "pic-b"), ("c", "pic-c"))

    after = carry_over(before, ["a", "c"])

    assert [r.thumbnail for r in after] == ["pic-a", "pic-c"]


def test_reordering_carries_each_picture_to_its_new_row():
    before = states(("a", "pic-a"), ("b", "pic-b"), ("c", "pic-c"))

    after = carry_over(before, ["c", "a", "b"])

    assert [r.thumbnail for r in after] == ["pic-c", "pic-a", "pic-b"]


def test_a_new_slide_has_no_picture_to_inherit():
    after = carry_over([], ["a", "b"])

    assert [r.thumbnail for r in after] == [None, None]


def test_duplicate_slides_do_not_both_claim_the_same_picture():
    """Two identical slides are two rows, and there is one picture between them."""
    before = states(("same", "pic"))

    after = carry_over(before, ["same", "same"])

    assert [r.thumbnail for r in after].count("pic") == 1


# ── Which rows are out of date ────────────────────────────────────────────────

def test_an_edited_slide_keeps_its_picture_and_is_marked_out_of_date():
    """Blanking it instead would strobe the strip on every pause in typing."""
    before = states(("a", "pic-a"), ("b", "pic-b"))

    after = carry_over(before, ["a", "b-edited"])

    assert after[1].thumbnail == "pic-b"
    assert after[1].stale is True


def test_the_slides_that_did_not_change_are_not_marked():
    """Only the edited slide is out of date; the chip speaks for the deck."""
    before = states(("a", "pic-a"), ("b", "pic-b"))

    after = carry_over(before, ["a", "b-edited"])

    assert after[0].stale is False


def test_an_inserted_slide_is_out_of_date_from_the_start():
    after = carry_over(states(("a", "pic-a")), ["new", "a"])

    assert [r.stale for r in after] == [True, False]


def test_a_row_that_was_already_out_of_date_stays_that_way():
    """An unrelated edit must not quietly declare the rest of the deck fresh."""
    before = [RowState(key="a", thumbnail="pic-a", stale=True),
              RowState(key="b", thumbnail="pic-b")]

    after = carry_over(before, ["a", "b", "c"])

    assert [r.stale for r in after] == [True, False, True]


def test_an_overflow_badge_follows_its_slide_too():
    before = [RowState(key="a"), RowState(key="b", overflow=True)]

    after = carry_over(before, ["new", "a", "b"])

    assert [r.overflow for r in after] == [False, False, True]


# ── What the document alone can say ───────────────────────────────────────────

DECK = """\
# Opening

---

## Second slide

Some words here.

^^^

And a script for the speaker to read out loud.

---

## Third

![a red barn](assets/barn.jpg)

Words beside the picture.
"""


def test_every_slide_is_read_out_of_the_text():
    assert [f.title for f in read_slides(DECK)] == [
        "Opening", "Second slide", "Third",
    ]


def test_a_slide_knows_whether_it_has_a_script_without_a_build():
    assert [f.has_notes for f in read_slides(DECK)] == [False, True, False]


def test_a_slide_with_a_script_is_timed_by_what_is_said():
    facts = read_slides(DECK)

    assert facts[1].timing.from_script is True
    assert facts[1].timing.seconds > 0


def test_frontmatter_is_not_a_slide():
    deck = "---\ntitle: A talk\n---\n\n# One\n\n---\n\n# Two\n"

    assert [f.title for f in read_slides(deck)] == ["One", "Two"]


def test_a_slide_that_only_changed_its_script_is_still_a_different_slide():
    """The strip shows the script indicator, so notes are part of identity."""
    a = read_slides("# One\n\n^^^\n\nsay this")[0]
    b = read_slides("# One\n\n^^^\n\nsay that")[0]

    assert a.key != b.key


def test_editing_the_body_changes_the_key_a_picture_is_matched_on():
    a = read_slides("# One\n\nbody")[0]
    b = read_slides("# One\n\nbody edited")[0]

    assert a.key != b.key


def test_a_slower_speaker_gets_a_longer_estimate():
    slow = read_slides(DECK, wpm=80)[1].timing.seconds
    fast = read_slides(DECK, wpm=200)[1].timing.seconds

    assert slow > fast
