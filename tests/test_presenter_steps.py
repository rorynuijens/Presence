"""
test_presenter_steps.py — Presenting a deck that holds part of a slide back.

What the advance key moves through stops being the slide list the moment a
slide reveals in steps, and everything that *describes* the talk — the
script, the schedule, the progress dots, the title — has to go on counting
slides.  That split is the whole of what is pinned here.

Nothing presents a window: the audience screen is a stand-in, the way
test_presenter.py builds one.
"""

import pytest

pytest.importorskip("gi")

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from presence.presenter import PresenterWindow, _stops_from  # noqa: E402


def _slides(steps: list[int], notes: bool = True) -> list[dict]:
    """A deck whose n-th slide takes ``steps[n]`` presses to get through."""
    info, page = [], 0
    for i, count in enumerate(steps):
        info.append({
            "title": f"Slide {i + 1} title",
            "notes": f"What to say over slide {i + 1}." if notes else "",
            "body":  f"# Slide {i + 1} title\n\nSome words.",
            "page_index": page + count - 1,
            "step_pages": list(range(page, page + count)),
        })
        page += count
    return info


class FakeSlideshow:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.shown  = []

    def present(self):            pass
    def load(self):               pass
    def show_step(self, index):   self.shown.append(index)
    def set_blank(self, colour):  pass
    def move_to_other_monitor(self): pass
    def close_by_presenter(self): pass


@pytest.fixture
def presenter(gtk, monkeypatch):
    monkeypatch.setattr("presence.presenter.SlideshowWindow", FakeSlideshow)
    made = []

    def build(steps=(1, 3, 1), **parent_prefs):
        parent = gtk.Window()
        for key, value in parent_prefs.items():
            setattr(parent, key, value)
        win = PresenterWindow(
            html_uri="file:///nonexistent.html",
            slide_info=_slides(list(steps)),
            thumbnails=[b""] * len(steps),
            parent_window=parent,
        )
        made.append((win, parent))
        return win

    yield build
    for win, parent in made:
        win.do_close_request()
        win.destroy()
        parent.destroy()


# ── Reading the stops off the build ──────────────────────────────────────────

def test_a_slide_with_steps_is_several_stops():
    stops, pages = _stops_from(_slides([1, 3, 1]))
    assert stops == [(0, 0), (1, 0), (1, 1), (1, 2), (2, 0)]
    assert pages == [0, 1, 2, 3, 4]


def test_a_build_without_steps_is_one_stop_per_slide():
    info = _slides([1, 1])
    for slide in info:
        del slide["step_pages"]
    assert _stops_from(info)[0] == [(0, 0), (1, 0)]


# ── Moving through them ──────────────────────────────────────────────────────

def test_the_advance_key_walks_the_steps_before_the_next_slide(presenter):
    win = presenter()
    win._go_to(0)
    win._next()                       # into slide 2, first step
    assert win._current == 1
    win._next()
    assert (win._current, win._pos) == (1, 2)
    win._next()
    assert (win._current, win._pos) == (1, 3)
    win._next()                       # only now the third slide
    assert win._current == 2


def test_going_back_walks_the_steps_too(presenter):
    win = presenter()
    win._go_to(2)
    win._prev()
    # Back into the slide before, at its *last* step: that is what was on
    # the screen a press ago.
    assert (win._current, win._pos) == (1, 3)


def test_down_skips_what_the_slide_is_still_holding_back(presenter):
    win = presenter()
    win._go_to(1)
    win._next_slide()
    assert win._current == 2


def test_up_goes_to_the_top_of_the_slide_then_out_of_it(presenter):
    win = presenter()
    win._go_to(1)
    win._next()                       # second step of slide 2
    win._prev_slide()                 # back to its first step
    assert (win._current, win._pos) == (1, 1)
    win._prev_slide()                 # and now out of it
    assert win._current == 0


def test_jumping_to_a_slide_lands_on_its_first_step(presenter):
    win = presenter()
    win._go_to(1)
    assert win._pos == 1
    win._go_to(2)
    assert win._pos == 4


def test_the_audience_screen_is_driven_by_stop(presenter):
    win = presenter()
    win._go_to(0)
    win._slideshow.shown.clear()
    for _ in range(4):
        win._next()
    assert win._slideshow.shown == [1, 2, 3, 4]


def test_the_slideshow_is_told_how_many_stops_there_are(presenter):
    win = presenter()
    assert win._slideshow.kwargs["n_steps"] == 5
    assert win._slideshow.kwargs["step_pages"] == [0, 1, 2, 3, 4]


# ── What still counts slides ─────────────────────────────────────────────────

def test_the_counter_says_the_slide_and_the_step_within_it(presenter):
    win = presenter()
    win._go_to(0)
    assert win._counter_label.get_text() == "1 / 3"
    win._next()
    assert win._counter_label.get_text() == "2 / 3 · 1 of 3"
    win._next()
    assert win._counter_label.get_text() == "2 / 3 · 2 of 3"


def test_a_step_does_not_move_the_title_or_the_script(presenter):
    win = presenter()
    win._go_to(1)
    title = win._slide_title_label.get_label()
    scrolled = win._notes_view.get_buffer().get_text(
        *win._notes_view.get_buffer().get_bounds(), False)
    win._next()
    assert win._slide_title_label.get_label() == title
    assert win._notes_view.get_buffer().get_text(
        *win._notes_view.get_buffer().get_bounds(), False) == scrolled
    assert win._current == 1


def test_the_deck_is_still_timed_in_slides(presenter):
    win = presenter()
    # One schedule entry per slide, not per stop: a reveal is not a section
    # of the talk with a duration of its own.
    assert len(win._schedule) == 3


def test_the_next_pane_says_what_it_is_showing(presenter):
    win = presenter()
    win._go_to(1)
    # Still inside the slide on screen: what comes next is the rest of it.
    assert win._next_label.get_label() == "Next step"
    win._next()
    win._next()
    assert win._next_label.get_label() == "Next slide"
