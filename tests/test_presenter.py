"""
test_presenter.py — the presenter view and the audience screen, as widgets.

These are the first tests in the suite that build real GTK widgets.  They can
be, now that conftest.py no longer pins the backend GTK 4 dropped; see the
``gtk`` fixture there for what that costs (a display) and how to supply one.

Nothing here presents a window.  ``SlideshowWindow`` is driven directly —
constructing a toplevel neither maps nor shows it — while ``PresenterWindow``
is built with a stand-in slideshow, because its ``__init__`` presents the
audience window and fullscreens it, which a test run must not do to the
desktop it is running on.

What is worth pinning here is the part of a talk that cannot be retried: the
audience screen showing the right page, the blank staying blank until it is
taken down, and every guard on the closing path holding once the window is
gone.
"""
import io

import pytest

pytest.importorskip("gi")
PIL = pytest.importorskip("PIL.Image")

from presence.presenter import PresenterWindow, SlideshowWindow, _slide_pages_from  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _png(colour=(200, 40, 40)) -> bytes:
    """A tiny valid PNG, so a Gtk.Picture has something real to hold."""
    buf = io.BytesIO()
    PIL.new("RGB", (8, 6), colour).save(buf, format="PNG")
    return buf.getvalue()


def _slides(n=3, *, notes=True, pages=None) -> list[dict]:
    """slide_info shaped the way converter.convert() hands it over."""
    out = []
    for i in range(n):
        info = {
            "title": f"Slide {i + 1} title",
            "notes": f"What to say over slide {i + 1}." if notes else "",
            "body":  f"# Slide {i + 1} title\n\nSome words.",
        }
        if pages is not None:
            info["page_index"] = pages[i]
        out.append(info)
    return out


class FakeSlideshow:
    """
    Stands in for SlideshowWindow while the presenter is under test.

    The real one presents itself and fullscreens on a monitor; what the
    presenter needs from it is four verbs, and this records them.
    """

    def __init__(self, **kwargs):
        self.kwargs      = kwargs
        self.shown       = []
        self.blanks      = []
        self.presented   = 0
        self.loaded      = 0
        self.moved       = 0
        self.closed      = 0

    def present(self):            self.presented += 1
    def load(self):               self.loaded += 1
    def show_slide(self, index):  self.shown.append(index)
    def set_blank(self, colour):  self.blanks.append(colour)
    def move_to_other_monitor(self): self.moved += 1
    def close_by_presenter(self): self.closed += 1


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def slideshow(gtk):
    """A real SlideshowWindow, torn down through its own closing path."""
    made = []

    def build(n_slides=3, slide_pages=None, pdf_path=None):
        win = SlideshowWindow(pdf_path=pdf_path, n_slides=n_slides,
                              presenter_window=None, parent_window=None,
                              slide_pages=slide_pages)
        made.append(win)
        return win

    yield build
    for win in made:
        win.close_by_presenter()


@pytest.fixture
def presenter(gtk, monkeypatch):
    """A real PresenterWindow whose audience window is a stand-in."""
    monkeypatch.setattr("presence.presenter.SlideshowWindow", FakeSlideshow)
    made = []

    def build(slide_info=None, thumbnails=None, **parent_prefs):
        parent = gtk.Window()
        for key, value in parent_prefs.items():
            setattr(parent, key, value)
        win = PresenterWindow(
            html_uri="file:///nonexistent.html",
            slide_info=slide_info if slide_info is not None else _slides(),
            thumbnails=thumbnails if thumbnails is not None else [],
            parent_window=parent,
        )
        made.append((win, parent))
        return win

    yield build
    for win, parent in made:
        win.do_close_request()
        win.destroy()
        parent.destroy()


# ── The page a slide lives on ─────────────────────────────────────────────────
#
# A slide that overflows leaves a continuation page behind it, so the n-th
# page stops being the n-th slide.  Everything made out of the PDF has to
# index by page_index; the slideshow is the copy of that rule the audience
# sees, so it is the one worth pinning.

def test_slide_pages_read_off_the_build():
    assert _slide_pages_from(_slides(3, pages=[0, 1, 3])) == [0, 1, 3]


def test_slide_pages_none_when_the_build_did_not_say():
    # A build made before slides carried a page index: fall back rather than
    # index by a half-filled list.
    info = _slides(3, pages=[0, 1, 3])
    del info[1]["page_index"]
    assert _slide_pages_from(info) is None
    assert _slide_pages_from([]) is None


def test_slideshow_asks_for_the_page_the_slide_starts_on(slideshow):
    win = slideshow(n_slides=3, slide_pages=[0, 1, 3])
    assert [win._page_for(i) for i in range(3)] == [0, 1, 3]


def test_slideshow_falls_back_to_one_page_per_slide(slideshow):
    win = slideshow(n_slides=3, slide_pages=None)
    assert [win._page_for(i) for i in range(3)] == [0, 1, 2]
    # Off the end, rather than IndexError into a live presentation.
    assert win._page_for(9) == 9


# ── Navigating the audience screen ────────────────────────────────────────────

def test_show_slide_clamps_to_the_deck(slideshow):
    win = slideshow(n_slides=3)
    win.show_slide(-5)
    assert win._pending == 0
    win.show_slide(99)
    assert win._pending == 2


def test_show_slide_puts_the_page_on_screen(slideshow):
    win = slideshow(n_slides=2)
    win._pages[1] = _png()
    win.show_slide(1)
    assert win._picture.get_paintable() is not None


# ── Blanking ──────────────────────────────────────────────────────────────────

def test_blanking_takes_the_slide_down(slideshow):
    win = slideshow(n_slides=2)
    win._pages[0] = _png()
    win.show_slide(0)
    win.set_blank("#000000")
    assert win._blanked is True
    assert win._picture.get_paintable() is None


def test_unblanking_puts_the_same_slide_back(slideshow):
    win = slideshow(n_slides=2)
    win._pages[1] = _png()
    win.show_slide(1)
    win.set_blank("#ffffff")
    win.set_blank(None)
    assert win._blanked is False
    assert win._pending == 1
    assert win._picture.get_paintable() is not None


def test_the_prefetch_landing_does_not_lift_a_blank(slideshow):
    # The deck finishes rasterizing while the speaker is talking to a blank
    # screen.  Adopting the pages must not put a slide back in front of the
    # room; only the speaker takes the blank down.
    win = slideshow(n_slides=2)
    win.set_blank("#000000")
    win._store_pages([_png(), _png()])
    assert win._pages[0] is not None      # adopted
    assert win._picture.get_paintable() is None   # but still blank


def test_moving_on_lifts_the_blank(slideshow):
    win = slideshow(n_slides=2)
    win.set_blank("#000000")
    win._pages[1] = _png()
    win.show_slide(1)
    assert win._blanked is False
    assert win._picture.get_paintable() is not None


# ── The closing path ──────────────────────────────────────────────────────────

def test_closing_drops_the_deck_and_disarms_every_guard(slideshow):
    win = slideshow(n_slides=3)
    win._pages[0] = _png()
    win.close_by_presenter()

    assert win._closing is True
    assert win._pages == []
    assert win._pdf_bytes is None
    assert win._monitors_handler is None
    # Every entry point a pending callback could still reach must be inert
    # rather than raising into the main loop.
    win.show_slide(1)
    win.set_blank("#000000")
    win.set_blank(None)
    win._store_pages([_png()])
    win.load()
    win.move_to_other_monitor()


def test_the_user_closing_the_slideshow_only_hides_it(slideshow):
    # Closing from the taskbar must not destroy the window the presenter is
    # still driving; it hides, and the presenter can put it back.
    win = slideshow(n_slides=2)
    assert win.do_close_request() is True
    win._closing = True
    assert win.do_close_request() is False


# ── The presenter view ────────────────────────────────────────────────────────

def test_presenter_opens_the_audience_window(presenter):
    win = presenter()
    assert isinstance(win._slideshow, FakeSlideshow)
    assert win._slideshow.presented == 1
    assert win._slideshow.loaded == 1


def test_presenter_hands_the_page_map_to_the_slideshow(presenter):
    win = presenter(slide_info=_slides(3, pages=[0, 1, 3]))
    assert win._slideshow.kwargs["slide_pages"] == [0, 1, 3]


def test_going_to_a_slide_moves_both_screens(presenter):
    win = presenter()
    win._go_to(1)
    assert win._current == 1
    assert win._slideshow.shown[-1] == 1
    assert win._counter_label.get_text() == "2 / 3"
    assert win._slide_title_label.get_label() == "Slide 2 title"


def test_navigation_clamps_at_both_ends(presenter):
    win = presenter()
    win._go_to(0)
    win._prev()
    assert win._current == 0
    win._go_to(2)
    win._next()
    assert win._current == 2
    assert win._counter_label.get_text() == "3 / 3"


def test_thumbnails_follow_the_cursor_and_clear_past_the_end(presenter):
    win = presenter(thumbnails=[_png(), _png(), _png()])
    win._go_to(0)
    assert win._current_picture.get_paintable() is not None
    assert win._next_picture.get_paintable() is not None
    win._go_to(2)                       # last slide: nothing comes next
    assert win._current_picture.get_paintable() is not None
    assert win._next_picture.get_paintable() is None


def test_a_deck_with_no_thumbnails_yet_still_navigates(presenter):
    win = presenter(thumbnails=[])
    win._go_to(1)
    assert win._current_picture.get_paintable() is None


# ── Blanking, from the speaker's keyboard ─────────────────────────────────────

def test_b_and_w_blank_to_their_colours(presenter):
    win = presenter()
    win._toggle_blank("black")
    assert win._slideshow.blanks[-1] == "#000000"
    win._toggle_blank("black")
    assert win._slideshow.blanks[-1] is None
    win._toggle_blank("white")
    assert win._slideshow.blanks[-1] == "#ffffff"


def test_a_navigation_key_lifts_the_blank_instead_of_advancing(presenter):
    from gi.repository import Gdk
    win = presenter()
    win._go_to(0)
    win._on_key(None, Gdk.KEY_b, 0, 0)
    assert win._blank_color == "black"

    handled = win._on_key(None, Gdk.KEY_Right, 0, 0)
    assert handled is True
    assert win._blank_color == ""
    assert win._current == 0            # the slide did not move

    win._on_key(None, Gdk.KEY_Right, 0, 0)
    assert win._current == 1


def test_arrows_and_space_step_the_deck(presenter):
    from gi.repository import Gdk
    win = presenter()
    win._go_to(0)
    for key in (Gdk.KEY_Right, Gdk.KEY_Page_Down, Gdk.KEY_space):
        before = win._current
        win._on_key(None, key, 0, 0)
        assert win._current == min(before + 1, 2)
    win._on_key(None, Gdk.KEY_Left, 0, 0)
    assert win._current == 1


# ── Pace ──────────────────────────────────────────────────────────────────────

def test_pace_reads_the_window_a_slide_is_due_in(presenter):
    win = presenter()
    win._schedule = [60, 120, 180]

    win._current, win._elapsed = 1, 30      # due from 60s: still early
    win._update_pace()
    assert win._pace_label.get_text() == "0:30 ahead"
    assert "ahead" in win._pace_label.get_css_classes()

    win._elapsed = 90                       # inside 60–120
    win._update_pace()
    assert win._pace_label.get_text() == "on pace"

    win._elapsed = 150                      # past 120
    win._update_pace()
    assert win._pace_label.get_text() == "0:30 behind"
    assert "behind" in win._pace_label.get_css_classes()


def test_pace_says_nothing_when_there_is_nothing_to_measure(presenter):
    win = presenter()
    win._schedule = []
    win._update_pace()
    assert win._pace_label.get_text() == ""
    win._schedule = [0, 0]
    win._update_pace()
    assert win._pace_label.get_text() == ""


def test_a_slide_says_whether_its_time_is_scripted_or_guessed(presenter):
    win = presenter(slide_info=[
        {"title": "One", "notes": "Say this over the first slide.", "body": ""},
        {"title": "Two", "notes": "",                               "body": "words"},
    ])
    win._schedule = [30, 75]

    win._current = 0
    win._update_slide_time()
    assert win._slide_time_label.get_text() == "0:30 of script"

    win._current = 1
    win._update_slide_time()
    assert win._slide_time_label.get_text() == "0:45 estimated"


# ── The clock ─────────────────────────────────────────────────────────────────

def test_counting_up_formats_and_warns(presenter):
    win = presenter()
    win._target_secs = 0

    win._elapsed = 0
    win._tick()
    assert win._timer_label.get_text() == "00:01"

    win._elapsed = 25 * 60 - 1
    win._tick()
    assert "warning" in win._timer_label.get_css_classes()

    win._elapsed = 35 * 60 - 1
    win._tick()
    assert "error" in win._timer_label.get_css_classes()
    assert "warning" not in win._timer_label.get_css_classes()

    win._elapsed = 3599
    win._tick()
    assert win._timer_label.get_text() == "01:00:00"


def test_counting_down_to_a_target_goes_negative_rather_than_stopping(presenter):
    win = presenter()
    win._target_secs = 10 * 60

    win._elapsed = 0
    win._tick()
    assert win._timer_label.get_text() == "09:59"

    win._elapsed = 10 * 60
    win._tick()
    assert win._timer_label.get_text() == "-00:01"
    assert "error" in win._timer_label.get_css_classes()


def test_the_target_comes_from_the_window_prefs(presenter):
    win = presenter(_timer_minutes=20, _speaking_rate=140,
                    _presenter_notes_font=28)
    assert win._target_secs == 20 * 60
    assert win._wpm == 140
    assert win._notes_font_size == 28


def test_a_tick_tells_the_main_window(presenter):
    win = presenter()
    win._target_secs = 300
    seen = []
    win.connect("timer-tick", lambda _w, e, t: seen.append((e, t)))
    win._elapsed = 41
    win._tick()
    assert seen == [(42, 300)]


def test_resetting_the_clock_clears_the_colours(presenter):
    win = presenter()
    win._elapsed = 40 * 60
    win._tick()
    assert "error" in win._timer_label.get_css_classes()
    win._reset_timer()
    assert win._elapsed == 0
    assert win._timer_label.get_text() == "00:00"
    assert "error" not in win._timer_label.get_css_classes()
    assert "warning" not in win._timer_label.get_css_classes()


def test_the_timer_starts_deferred_and_stops_with_the_window(presenter):
    win = presenter()
    win._deferred_start()
    assert win._timer_src is not None
    assert win._current == 0
    win.do_close_request()
    assert win._timer_src is None
    assert win._slideshow.closed == 1


# ── The script ────────────────────────────────────────────────────────────────

def test_the_whole_talk_is_one_document_with_a_section_per_slide(presenter):
    win = presenter()
    buf = win._notes_view.get_buffer()
    text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    assert len(win._script_ranges) == 3
    assert len(win._script_marks) == 3
    for i in range(3):
        assert f"{i + 1} · Slide {i + 1} title" in text
        assert f"What to say over slide {i + 1}." in text
    # Sections run in order and do not overlap.
    for (_, end), (start, _) in zip(win._script_ranges, win._script_ranges[1:]):
        assert start >= end


def test_a_slide_with_nothing_to_say_is_named_not_left_blank(presenter):
    win = presenter(slide_info=_slides(2, notes=False))
    buf = win._notes_view.get_buffer()
    text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
    assert text.count("— nothing scripted —") == 2


def test_focusing_a_section_dims_the_rest(presenter):
    win = presenter()
    buf = win._notes_view.get_buffer()
    dim = buf.get_tag_table().lookup("dim")

    win._focus_script(1)
    lo, hi = win._script_ranges[1]

    inside = buf.get_iter_at_offset(lo + 1)
    assert not inside.has_tag(dim)
    before = buf.get_iter_at_offset(max(0, lo - 1))
    assert before.has_tag(dim)
    after = buf.get_iter_at_offset(hi + 1)
    assert after.has_tag(dim)


def test_focusing_out_of_range_leaves_the_script_alone(presenter):
    win = presenter()
    win._focus_script(99)
    win._focus_script(-1)


def test_resizing_the_script_keeps_the_place(presenter, monkeypatch):
    import presence.session as session
    monkeypatch.setattr(session, "save_presentation_prefs", lambda prefs: None)
    monkeypatch.setattr(session, "load_presentation_prefs", lambda: {})

    win = presenter(_presenter_notes_font=22)
    win._go_to(1)

    win._on_font_increase()
    assert win._notes_font_size == 24
    assert win._current == 1

    for _ in range(20):
        win._on_font_increase()
    assert win._notes_font_size == 40        # clamped
    for _ in range(20):
        win._on_font_decrease()
    assert win._notes_font_size == 14        # clamped


def test_swapping_screens_asks_the_slideshow(presenter):
    win = presenter()
    win._swap_screens()
    assert win._slideshow.moved == 1
