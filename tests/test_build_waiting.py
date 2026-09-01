"""
test_build_waiting.py — The control that starts a wait is the one that shows it.

Present and Export can both start work that does not finish on the click: a
build has to run first, and an image or handout export then rasterizes the
whole deck.  The button used to go insensitive while the only sign of life —
a spinner in the build chip — sat at the other end of the header, so the
control the writer pressed looked broken rather than busy.

What must hold:

*  A button that waits is released when the build lands, and also when it
   fails.  A button left spinning forever is worse than no indicator.
   There used to be a third case — a build that never started, because
   triggering one first had to save and the save was refused.  A build no
   longer saves, so it no longer has a way to decline to start.
*  Waits are counted, not flagged: an export holds one for the build and
   another for the render, and the two overlap.

Widgets cannot be built under this suite's conftest, so this drives the real
coordinator against the same stand-in window test_build_coordinator.py uses.
"""

import pytest

from presence.build_coordinator import BuildCoordinator
from tests.test_build_coordinator import FakeWindow


class FakeButton:
    """Stands in for the header button a _BusyIndicator is wrapped around."""

    def __init__(self) -> None:
        self.sensitive = True

    def set_sensitive(self, on) -> None:
        self.sensitive = on


def indicator():
    from presence.window import _BusyIndicator
    button = FakeButton()
    # Bypass __init__: building the Stack and Spinner needs a display.
    ind = _BusyIndicator.__new__(_BusyIndicator)
    ind._button  = button
    ind._spinner = _FakeSpinner()
    ind._stack   = _FakeStack()
    ind._waits   = 0
    return ind, button


class _FakeSpinner:
    def __init__(self): self.spinning = False
    def start(self):    self.spinning = True
    def stop(self):     self.spinning = False


class _FakeStack:
    def __init__(self): self.showing = "idle"
    def set_visible_child_name(self, name): self.showing = name


def coordinator(**kwargs):
    win  = FakeWindow(**kwargs)
    coord = BuildCoordinator(win)
    coord.trigger = lambda *a: (setattr(win, "triggered", win.triggered + 1)
                                or True)
    return coord, win


# ── Taking and releasing a wait ───────────────────────────────────────────────

def test_a_deck_that_is_already_current_never_looks_busy():
    """Nothing was waited for, so nothing should have spun."""
    coord, _win = coordinator(current="same", built="same")
    busy, button = indicator()

    coord.with_current_build(lambda: None, on_wait=busy)

    assert busy.busy is False
    assert button.sensitive is True


def test_waiting_for_a_build_shows_it_in_the_button():
    coord, _win = coordinator(current="edited", built="original")
    busy, button = indicator()

    coord.with_current_build(lambda: None, on_wait=busy)

    assert busy.busy is True
    assert busy._stack.showing == "busy"
    assert button.sensitive is False


def test_the_button_is_released_when_the_build_lands():
    coord, win = coordinator(current="edited", built="original")
    busy, button = indicator()
    coord.with_current_build(lambda: None, on_wait=busy)

    _complete(coord, win)

    assert busy.busy is False
    assert busy._stack.showing == "idle"
    assert button.sensitive is True


def test_the_button_is_released_when_the_build_fails():
    """A failed build must not leave a button spinning with nothing behind it."""
    coord, _win = coordinator(current="edited", built="original")
    busy, button = indicator()
    coord.with_current_build(lambda: None, on_wait=busy)

    coord.on_failed(None, "WeasyPrint blew up")

    assert busy.busy is False
    assert button.sensitive is True


# ── Overlapping waits ─────────────────────────────────────────────────────────

def test_a_second_wait_keeps_the_button_busy_after_the_first_is_released():
    """An export waits for a build, then for the render it feeds."""
    busy, button = indicator()

    busy(True)                       # the build
    busy(True)                       # the rasterizing thread
    busy(False)                      # the build landed
    assert busy.busy is True
    assert button.sensitive is False

    busy(False)                      # the render finished
    assert busy.busy is False
    assert button.sensitive is True


def test_a_stray_release_cannot_drive_the_count_below_nothing():
    busy, button = indicator()

    busy(False)
    busy(True)

    assert busy.busy is True
    assert button.sensitive is False


def test_two_buttons_waiting_on_one_build_are_both_released():
    coord, win = coordinator(current="edited", built="original")
    first,  _b1 = indicator()
    second, _b2 = indicator()

    coord.with_current_build(lambda: None, on_wait=first)
    coord.wait_for_build(second)
    _complete(coord, win)

    assert (first.busy, second.busy) == (False, False)


# ── Helpers ───────────────────────────────────────────────────────────────────

class _Converter:
    def __init__(self):
        self.slide_info = []
        self.thumbnails = []
        self.ratio = "16:9"


def _complete(coord, win) -> None:
    """Drive a successful build through the coordinator's own handler."""
    win._sidebar = _Sidebar()
    win._building_text = win._editor.get_text()
    win._speaking_rate = 110
    win._pres_path = None
    win._pack_pres = lambda: None
    win._file_path = None
    win._converter = _Converter()
    coord.set_build_folds([])
    coord.on_complete(_Converter(), 0, 0.0, "/tmp/out.pdf", "file:///out.html")


class _Sidebar:
    def __init__(self): self.converting = None
    def set_converting(self, on): self.converting = on
    def update_from_conversion(self, *a, **kw): return 0
    def update_from_text(self, text): pass
