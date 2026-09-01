"""
test_presenter_chrome.py — the presenter view as a window on a desktop.

Three things this window did wrong, none of them about presenting:

  * it had no header bar at all, only a Gtk.Box of controls, so the one
    toplevel a speaker looks at for an hour had no title, no close button and
    nothing to drag;
  * "End" wore ``destructive-action``, which is the styling that warns
    something is about to be lost, and ending a talk loses nothing;
  * nothing inhibited idle, so a slide discussed for longer than GNOME's
    five-minute blank took the audience screen down with it — in an app whose
    settings measure a target duration in minutes.

``test_presenter.py`` covers what the presenter *does*; this covers what it is
while it does it.  Its stand-in slideshow is borrowed from there, because the
real one presents itself and fullscreens on a monitor.
"""
import pytest

pytest.importorskip("gi")

from presence import presenter as P  # noqa: E402
from test_presenter import FakeSlideshow, _slides  # noqa: E402


class FakeApplication:
    """Records the inhibit taken for the talk, and the release."""

    def __init__(self) -> None:
        self.taken = []
        self.released = []

    def inhibit(self, window, flags, reason):
        self.taken.append((window, flags, reason))
        return 4711

    def uninhibit(self, cookie):
        self.released.append(cookie)


@pytest.fixture
def presenter(gtk, monkeypatch):
    """A real PresenterWindow whose audience window and session are stand-ins."""
    monkeypatch.setattr(P, "SlideshowWindow", FakeSlideshow)
    made = []

    def build(n=6):
        parent = gtk.Window()
        app = FakeApplication()
        monkeypatch.setattr(parent, "get_application", lambda: app)
        win = P.PresenterWindow(
            html_uri="file:///nonexistent.html",
            slide_info=_slides(n),
            thumbnails=[],
            parent_window=parent,
        )
        made.append((win, parent))
        return win, app

    yield build
    for win, parent in made:
        win.do_close_request()
        win.destroy()
        parent.destroy()


def _walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


# ── A window like every other window ──────────────────────────────────────────

def test_the_presenter_has_a_header_bar(presenter, gtk):
    from gi.repository import Adw

    win, _ = presenter()
    bars = [w for w in _walk(win.get_content()) if isinstance(w, Adw.HeaderBar)]
    assert len(bars) == 1


def test_the_header_bar_carries_the_window_controls(presenter):
    from gi.repository import Adw

    win, _ = presenter()
    bar = next(w for w in _walk(win.get_content())
               if isinstance(w, Adw.HeaderBar))
    assert bar.get_show_end_title_buttons()


def test_every_control_is_still_reachable(presenter):
    """
    The controls moved out of a hand-built box and into the header bar's two
    zones; what must not happen is one of them being left behind in the move.
    """
    win, _ = presenter()
    tooltips = {w.get_tooltip_text() for w in _walk(win.get_content())}
    for expected in (
        "Previous slide (←)",
        "Next slide (→)",
        "Reset timer",
        "Decrease notes font size",
        "Increase notes font size",
        "Move slideshow to other screen",
        "Blank audience screen (B)",
        "End presentation (Escape)",
        "Slide progress",
    ):
        assert expected in tooltips, expected


def test_ending_a_talk_is_not_dressed_as_destruction(presenter):
    win, _ = presenter()
    assert not [w for w in _walk(win.get_content())
                if w.has_css_class("destructive-action")]


def test_the_presenter_does_not_repeat_the_app_name_in_its_title(presenter):
    """
    The shell already says which application a window belongs to, so a title
    of "Presence — Presenter Mode" said it twice.
    """
    win, _ = presenter()
    assert win.get_title() == "Presenter"


def test_the_audience_window_does_not_either(gtk):
    show = P.SlideshowWindow(pdf_path=None, n_steps=1,
                             presenter_window=None, parent_window=None)
    try:
        assert show.get_title() == "Slideshow"
    finally:
        show.close_by_presenter()


# ── The screen stays on ───────────────────────────────────────────────────────

def test_a_talk_holds_the_session_awake(presenter):
    from gi.repository import Gtk

    win, app = presenter()
    assert len(app.taken) == 1
    window, flags, reason = app.taken[0]
    assert window is win
    assert flags == Gtk.ApplicationInhibitFlags.IDLE
    assert reason


def test_the_session_may_sleep_again_when_the_talk_ends(presenter):
    win, app = presenter()
    win.do_close_request()
    assert app.released == [4711]


def test_the_inhibit_is_released_once_however_often_it_closes(presenter):
    win, app = presenter()
    win.do_close_request()
    win.do_close_request()
    assert app.released == [4711]


def test_a_window_with_no_application_still_opens(gtk, monkeypatch):
    """
    There is no session manager in a test run, and a refused inhibit is not a
    reason to fail to start a talk.
    """
    monkeypatch.setattr(P, "SlideshowWindow", FakeSlideshow)
    parent = gtk.Window()
    win = P.PresenterWindow(
        html_uri="file:///nonexistent.html",
        slide_info=_slides(3),
        thumbnails=[],
        parent_window=parent,
    )
    assert win._inhibit_cookie == 0
    win.do_close_request()
    win.destroy()
    parent.destroy()


# ── The progress strip ────────────────────────────────────────────────────────

class _RecordingContext:
    """A Cairo stand-in that remembers where the dots were asked for."""

    def __init__(self) -> None:
        self.arcs = []

    def arc(self, cx, cy, r, *_):
        self.arcs.append((cx, cy, r))

    def set_source_rgba(self, *_): pass
    def set_line_width(self, *_):  pass
    def fill(self):                pass
    def stroke(self):              pass


def test_the_progress_dots_span_the_strip(presenter):
    """
    The dots sit at a fixed pitch no more: in a full-width strip that left a
    short cluster marooned in the middle, saying nothing about position.  How
    far along the row a dot is is now how far into the deck it is.
    """
    win, _ = presenter(n=6)
    cr = _RecordingContext()
    win._draw_progress(win._progress_bar, cr, 1000, 20)

    centres = [cx for cx, _, _ in cr.arcs]
    assert len(centres) >= 6
    assert min(centres) < 10
    assert max(centres) > 990


def test_a_one_slide_deck_draws_no_progress_at_all(presenter):
    win, _ = presenter(n=1)
    cr = _RecordingContext()
    win._draw_progress(win._progress_bar, cr, 1000, 20)
    assert cr.arcs == []
