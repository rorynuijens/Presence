"""
test_editor_selection.py — Selection handling and the toolbar action set.

PyGObject's override of gtk_text_buffer_get_selection_bounds() returns
(start, end), not the C signature's (ok, start, end).  Unpacking three values
raised for every selection, so "select a word and make it bold" had been
broken since the initial release; it surfaced when the toolbar was cut down
and those actions moved to menus.  These tests pin the fixed behaviour.

GTK 4 widgets cannot be constructed without a display (see conftest.py), so
these call the real Editor methods against a real GtkTextBuffer instead of
building an Editor.
"""

from unittest.mock import MagicMock

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from presence.editor import Editor  # noqa: E402


class BufferOnly:
    """
    The only attribute the selection helpers touch, plus the real helper.

    _get_selection() calls self._selection_bounds(), so borrow Editor's own
    implementation rather than reimplementing the thing under test.
    """

    _selection_bounds = Editor._selection_bounds

    def __init__(self, text: str = "") -> None:
        self._buffer = Gtk.TextBuffer()
        self._buffer.set_text(text)

    def select_all(self) -> None:
        self._buffer.select_range(
            self._buffer.get_start_iter(), self._buffer.get_end_iter()
        )


def test_selection_bounds_is_none_without_a_selection():
    assert Editor._selection_bounds(BufferOnly("hello")) is None


def test_selection_bounds_returns_two_iters():
    stub = BufferOnly("hello")
    stub.select_all()
    bounds = Editor._selection_bounds(stub)
    assert bounds is not None
    start, end = bounds
    assert stub._buffer.get_text(start, end, True) == "hello"


def test_get_selection_reports_the_selected_text():
    stub = BufferOnly("hello")
    stub.select_all()
    assert Editor._get_selection(stub) == ("hello", True)


def test_get_selection_is_empty_without_a_selection():
    assert Editor._get_selection(BufferOnly("hello")) == ("", False)


def test_get_selection_survives_a_selection_of_the_whole_buffer():
    """The regression: any selection at all used to raise ValueError."""
    stub = BufferOnly("one two three")
    stub.select_all()
    text, has_selection = Editor._get_selection(stub)
    assert has_selection is True
    assert text == "one two three"


# ── Toolbar action set ────────────────────────────────────────────────────────

EXPECTED_ACTIONS = {
    "heading-1", "heading-2", "heading-3", "bold", "italic", "strikethrough",
    "inline-code", "code-block", "blockquote", "bullet-list", "numbered-list",
    "link", "comment", "find", "find-replace",
}


def _specs():
    return Editor._editor_action_specs(MagicMock())


def test_every_action_removed_from_the_toolbar_still_exists():
    """The bar was cut to slide structure; nothing may be dropped outright."""
    assert {name for name, _label, _cb in _specs()} == EXPECTED_ACTIONS


def test_actions_are_labelled_for_menus():
    for name, label, _cb in _specs():
        assert label and label[0].isupper(), f"{name} needs a menu label"


def test_action_names_are_unique():
    names = [name for name, _l, _c in _specs()]
    assert len(names) == len(set(names))
