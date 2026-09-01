"""
test_reveal_editing.py — What the writer sees of a reveal before presenting it.

A step marker is three characters in a monospace stream, so the two places
that have to say it is there are the editor, which draws a rule at it, and
the strip, which says how many presses the slide will take.  Neither waits
for a build: both read the document.

The editor logic is driven over a plain GtkTextBuffer the way
test_focus_mode.py does it — a widget cannot be constructed without a
display, and the scan is the part worth pinning anyway.
"""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from presence.editor import Editor, _NO_BLOCK  # noqa: E402
from presence.sidebar import read_slides  # noqa: E402


class FakeEditor:
    """Editor's own band scan over a plain buffer; see test_focus_mode.py."""

    _get_line_text        = Editor._get_line_text
    _is_slide_sep         = Editor._is_slide_sep
    _is_notes_sep         = Editor._is_notes_sep
    _is_step_sep          = Editor._is_step_sep
    _update_badge_starts  = Editor._update_badge_starts
    _current_slide_block  = Editor._current_slide_block
    _focus_block          = Editor._focus_block
    _update_focus_dim     = Editor._update_focus_dim
    _update_current_slide_tint = Editor._update_current_slide_tint

    def __init__(self, text: str) -> None:
        self._buffer = Gtk.TextBuffer()
        self._buffer.set_text(text)
        self._buffer.create_tag("presence-current-slide")
        self._buffer.create_tag("presence-unfocused")
        self._badge_starts: list = []
        self._slide_ranges: list = []
        self._sep_bands: list = []
        self._step_bands: list = []
        self._tinted_block = _NO_BLOCK
        self._focus_mode = False
        self._focus_range = None
        self._update_badge_starts()


DECK = """\
# One

first
+++
second

---

## Two

nothing held back

^^^

The script mentions +++ and that is prose, not structure.
"""


# ── The editor ───────────────────────────────────────────────────────────────

def test_a_step_marker_is_found_where_it_is():
    ed = FakeEditor(DECK)
    assert ed._step_bands == [3]


def test_a_step_marker_opens_no_slide():
    ed = FakeEditor(DECK)
    # Two slides, one band: the deck is still divided by "---" alone.
    assert len(ed._slide_ranges) == 2
    assert [line for _num, line, _title in ed._sep_bands] == [6]


def test_a_marker_in_the_script_is_prose():
    # Below "^^^" the writer is writing what they will say; a "+++" there
    # divides nothing, and the scan is already skipping those lines.
    ed = FakeEditor(DECK)
    assert 15 not in ed._step_bands


def test_the_predicate_wants_the_marker_alone_on_its_line():
    ed = FakeEditor("+++\na +++ b\n++++\n")
    assert ed._is_step_sep(0)
    assert not ed._is_step_sep(1)
    assert not ed._is_step_sep(2)


def test_the_toolbar_inserts_the_marker_the_engine_reads():
    from presence.slides.reveal import STEP_MARKER
    import inspect
    source = inspect.getsource(Editor._reveal_step)
    assert "STEP_MARKER" in source
    assert STEP_MARKER == "+++"


# ── The strip ────────────────────────────────────────────────────────────────

def test_a_row_counts_the_presses_its_slide_takes():
    facts = read_slides(DECK)
    assert [f.steps for f in facts] == [2, 1]


def test_the_marker_is_not_a_word_of_the_talk():
    plain   = read_slides("## H\n\nfirst\n\nsecond\n")
    stepped = read_slides("## H\n\nfirst\n+++\nsecond\n")
    assert stepped[0].timing.words == plain[0].timing.words
    assert stepped[0].timing.seconds == plain[0].timing.seconds


def test_a_directive_is_not_a_word_of_the_talk_either():
    plain   = read_slides("## H\n\n- a\n- b\n")
    stepped = read_slides("## H\n\n<!-- reveal: lists -->\n\n- a\n- b\n")
    assert stepped[0].timing.words == plain[0].timing.words
    assert stepped[0].steps == 3


def test_a_deck_that_reveals_nothing_says_one_step_a_slide():
    facts = read_slides("# A\n\ntext\n\n---\n\n# B\n\nmore\n")
    assert [f.steps for f in facts] == [1, 1]
