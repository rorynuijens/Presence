"""
test_focus_mode.py — Which block focus mode lights, and what it dims.

Focus mode reuses the current-slide machinery with the sign flipped, so the
things worth pinning are the two places the two modes disagree: the
frontmatter, which is washed as no slide but focused as a block, and the
clearing of the dim when the mode goes off while the cursor sits somewhere
the wash would also call None.

GTK 4 widgets cannot be constructed without a display (see conftest.py), so
these drive the real Editor methods against a real GtkTextBuffer rather than
building an Editor.
"""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from presence.editor import Editor, _NO_BLOCK  # noqa: E402


class FakeEditor:
    """
    Editor's own block logic over a plain buffer.

    Borrowed rather than reimplemented — a copy would pass while the real
    thing broke.  The drawing areas are absent on purpose: the tint pass
    reaches for them with getattr and must cope with their absence.
    """

    _get_line_text        = Editor._get_line_text
    _is_slide_sep         = Editor._is_slide_sep
    _is_notes_sep         = Editor._is_notes_sep
    _is_step_sep          = Editor._is_step_sep
    _update_badge_starts  = Editor._update_badge_starts
    _current_slide_block  = Editor._current_slide_block
    _focus_block          = Editor._focus_block
    _update_focus_dim     = Editor._update_focus_dim
    _update_current_slide_tint = Editor._update_current_slide_tint
    set_focus_mode        = Editor.set_focus_mode
    get_focus_mode        = Editor.get_focus_mode

    def __init__(self, text: str) -> None:
        self._buffer = Gtk.TextBuffer()
        self._buffer.set_text(text)
        self._buffer.create_tag("presence-current-slide")
        self._buffer.create_tag("presence-unfocused")
        self._badge_starts: list = []
        self._slide_ranges: list = []
        self._sep_bands: list = []
        self._tinted_block = _NO_BLOCK
        self._focus_mode = False
        self._focus_range = None
        self._update_badge_starts()

    def put_cursor(self, line: int) -> None:
        ok, it = self._buffer.get_iter_at_line(line)
        assert ok
        self._buffer.place_cursor(it)
        self._tinted_block = _NO_BLOCK   # the cursor moved without a signal
        self._update_current_slide_tint()

    def dimmed_lines(self) -> set[int]:
        """Line numbers carrying the unfocused tag, by their first character."""
        tag = self._buffer.get_tag_table().lookup("presence-unfocused")
        out = set()
        for ln in range(self._buffer.get_line_count()):
            ok, it = self._buffer.get_iter_at_line(ln)
            if ok and it.has_tag(tag):
                out.add(ln)
        return out

    def washed(self) -> bool:
        tag = self._buffer.get_tag_table().lookup("presence-current-slide")
        it = self._buffer.get_start_iter()
        return it.forward_to_tag_toggle(tag)


#  0 ---
#  1 title: Deck
#  2 ---
#  3 (blank)
#  4 # First
#  5 (blank)
#  6 Opening line
#  7 (blank)
#  8 ---
#  9 (blank)
# 10 ## Second
# 11 (blank)
# 12 Closing line
DOC = (
    "---\ntitle: Deck\n---\n\n"
    "# First\n\nOpening line\n\n"
    "---\n\n"
    "## Second\n\nClosing line\n"
)


def test_frontmatter_is_a_block_to_focus_but_not_to_wash():
    ed = FakeEditor(DOC)
    # The wash refuses the frontmatter; focus mode claims it, stopping short
    # of the closing fence that opens slide 1.
    assert ed._current_slide_block(1) is None
    assert ed._focus_block(1) == (0, 1)


def test_focus_dims_every_other_slide():
    ed = FakeEditor(DOC)
    ed.set_focus_mode(True)
    ed.put_cursor(6)                       # inside slide 1

    dimmed = ed.dimmed_lines()
    assert {0, 1, 2}.issubset(dimmed)      # frontmatter and its fence
    assert {9, 10, 12}.issubset(dimmed)    # slide 2
    assert not {4, 6}.intersection(dimmed)  # the slide being written


def test_the_wash_stands_down_while_focus_is_on():
    ed = FakeEditor(DOC)
    ed.put_cursor(6)
    assert ed.washed()                     # wash alone

    ed.set_focus_mode(True)
    assert not ed.washed()                 # focus alone, never both

    ed.set_focus_mode(False)
    assert ed.washed()
    assert ed.dimmed_lines() == set()


def test_turning_focus_off_in_the_frontmatter_clears_the_dim():
    """
    The case a None-valued cache would have missed.

    In the frontmatter the wash's answer is None both before and after the
    mode changes, so a cache keyed on None would compare equal and skip the
    repaint, leaving the document greyed out with focus mode off.
    """
    ed = FakeEditor(DOC)
    ed.set_focus_mode(True)
    ed.put_cursor(1)
    assert ed.dimmed_lines()               # slides are dim, frontmatter lit

    ed.set_focus_mode(False)
    assert ed.dimmed_lines() == set()


def test_a_document_without_separators_dims_nothing():
    ed = FakeEditor("# Only slide\n\nJust the one.\n")
    ed.set_focus_mode(True)
    ed.put_cursor(0)
    assert ed.dimmed_lines() == set()
    assert ed.get_focus_mode() is True
