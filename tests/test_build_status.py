"""
test_build_status.py — The rule behind the build-status chip.

The chip reports whether the built PDF still matches the document.  It is a
text comparison rather than a dirty flag, so undoing back to what was built
reports "up to date" again without a rebuild.

The rule lives on BuildCoordinator, which reads it off the window it serves.
These drive a real coordinator over a stand-in window holding the three fields
it looks at, so no display is needed (see conftest.py).
"""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from presence.build_coordinator import BuildCoordinator  # noqa: E402


class FakeEditor:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text(self) -> str:
        return self._text


class WindowState:
    """
    The three fields the build state is read from.

    Two of them are the coordinator's own now, so only the editor is stood
    in for; the coordinator is asked about itself.
    """

    def __init__(self, current: str, built: str | None, converting: bool = False):
        self.editor = FakeEditor(current)
        self._coord = BuildCoordinator(self)
        self._coord.built_text = built
        self._coord.converting = converting

    def _build_state(self) -> str:
        return self._coord.state()


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
    state.editor._text = "original plus an edit"
    assert state._build_state() == "stale"
    state.editor._text = "original"          # undo
    assert state._build_state() == "current"


def test_whitespace_only_edits_still_count_as_stale():
    """They change the rendered slide, so they are not cosmetic here."""
    assert WindowState("a\n\nb", "a\nb")._build_state() == "stale"


def test_a_failed_build_leaves_the_document_stale():
    """built_text is untouched on failure, so the chip keeps asking."""
    state = WindowState("edited", "original", converting=True)
    state._coord.converting = False           # failure handler does this
    assert state._build_state() == "stale"
