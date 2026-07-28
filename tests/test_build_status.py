"""
test_build_status.py — The rule behind the build-status chip.

The chip reports whether the built PDF still matches the document.  It is a
text comparison rather than a dirty flag, so undoing back to what was built
reports "up to date" again without a rebuild.

GTK 4 widgets cannot be constructed without a display (see conftest.py), so
these call MainWindow._build_state against a stand-in holding the same fields.
"""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from presence.window import MainWindow  # noqa: E402


class FakeEditor:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text(self) -> str:
        return self._text


class WindowState:
    """The three fields _build_state() reads."""

    _build_state = MainWindow._build_state

    def __init__(self, current: str, built: str | None, converting: bool = False):
        self._editor = FakeEditor(current)
        self._built_text = built
        self._converting = converting


def test_building_wins_over_everything():
    state = WindowState("a", "a", converting=True)
    assert state._build_state() == "building"


def test_stale_before_the_first_build():
    assert WindowState("some text", None)._build_state() == "stale"


def test_current_when_the_document_matches_the_build():
    assert WindowState("same", "same")._build_state() == "current"


def test_stale_when_the_document_has_moved_on():
    assert WindowState("edited", "original")._build_state() == "stale"


def test_undoing_back_to_the_built_text_reports_current():
    """Why a text comparison and not a modified flag."""
    state = WindowState("original", "original")
    state._editor._text = "original plus an edit"
    assert state._build_state() == "stale"
    state._editor._text = "original"          # undo
    assert state._build_state() == "current"


def test_whitespace_only_edits_still_count_as_stale():
    """They change the rendered slide, so they are not cosmetic here."""
    assert WindowState("a\n\nb", "a\nb")._build_state() == "stale"


def test_a_failed_build_leaves_the_document_stale():
    """_built_text is untouched on failure, so the chip keeps asking."""
    state = WindowState("edited", "original", converting=True)
    state._converting = False                 # failure handler does this
    assert state._build_state() == "stale"
