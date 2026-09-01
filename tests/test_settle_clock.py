"""
test_settle_clock.py — Words and picture, published together or not at all.

The typing pipeline used to be two clocks racing.  The editor debounced a
keystroke twice over — 400 ms for the words, 150 ms for the picture — the
window debounced the words again by 200 on top, and the frame that came back
first settled the race by reaching into the window's pending source, killing
it and running its callback early with its own, older text.

That is the behaviour pinned here, in its fixed form:

*  a keystroke publishes nothing until the document settles, and publishes
   once however many keystrokes there were;
*  a frame that carries the newest text brings those words with it, and the
   settle timer then has nothing left to do;
*  **a frame never pushes the strip back to older words** — the case the old
   arrangement got wrong, and which stuck the strip's titles and timings one
   edit behind when the next render happened to fail;
*  a picture is only ever handed to a strip whose rows were read from the
   very text that picture was laid out from, because the frame's slide
   indices have to be the rows the strip is showing.

`SettleClock` is a plain object over a Protocol, so none of this needs a
display or a widget: the host below is everything `ClockHost` promises.  The
timings are shortened by subclass — the real constants are pinned separately,
as the one relation between them that has to hold.
"""

import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib                                  # noqa: E402

from presence.settle_clock import SettleClock                    # noqa: E402


# ── The window, as far as the clock can see it ────────────────────────────────

class FakeEditor:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.offset = 0

    def get_text(self) -> str:      return self.text
    def get_cursor_offset(self) -> int: return self.offset


class FakeSidebar:
    def __init__(self, journal) -> None:
        self._journal = journal
        self.scrolled = []

    def update_from_text(self, text) -> None:
        self._journal.append(("words", text))

    def set_live_slide(self, index, png, fold_line) -> None:
        self._journal.append(("picture", index))

    def scroll_to_index(self, index) -> None:
        self.scrolled.append(index)


class FakeConverter:
    """Holds the request instead of rendering it, so the test delivers it."""

    def __init__(self) -> None:
        self.requests = []              # (text, index, width)
        self._callback = None

    def render_slide_async(self, text, base_dir, index, width_px, callback):
        self.requests.append((text, index, width_px))
        self._callback = callback

    def deliver(self, frame=None, error=None) -> None:
        assert self._callback is not None, "nothing was asked for"
        self._callback(frame, error)


class FakeBuilds:
    def __init__(self) -> None:
        self.chips = 0
        self.folds = []

    def update_chip(self) -> None:              self.chips += 1
    def set_live_fold(self, index, fold_line):  self.folds.append((index, fold_line))


class FakeDocuments:
    base_dir = None


class FakeFrame:
    def __init__(self, index: int = 0, fold_line=None) -> None:
        self.index = index
        self.png = b"PNG"
        self.fold_line = fold_line


class FakeWindow:
    """Everything ClockHost promises, and nothing else."""

    def __init__(self, text: str = "") -> None:
        self.journal = []
        self.editor    = FakeEditor(text)
        self.sidebar   = FakeSidebar(self.journal)
        self.converter = FakeConverter()
        self.builds    = FakeBuilds()
        self.documents = FakeDocuments()
        self.width = 320
        self.modified = 0
        self.panel_syncs = []
        self.word_counts = []

    def live_render_width(self):            return self.width
    def mark_modified(self):                self.modified += 1
    def sync_panel_to_document(self, text): self.panel_syncs.append(text)
    def update_word_count(self, text):      self.word_counts.append(text)


class QuickClock(SettleClock):
    """The same clock with the waits shortened, so a test need not sit them out."""

    SETTLE_MS = 40
    LIVE_MS   = 10
    CURSOR_MS = 20
    RESIZE_MS = 15


@pytest.fixture
def clock():
    win = FakeWindow()
    c = QuickClock(win)
    yield c, win
    c.stop()


def _spin(ms: float) -> None:
    """Let GLib run its timers for *ms* milliseconds."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.002)


def _words(win) -> list:
    return [text for kind, text in win.journal if kind == "words"]


# ── The settle ────────────────────────────────────────────────────────────────

def test_a_keystroke_publishes_nothing_until_the_document_settles(clock):
    c, win = clock

    c.on_editor_changed(None, "# A")

    assert win.journal == []
    _spin(c.SETTLE_MS * 2)
    assert _words(win) == ["# A"]


def test_a_burst_of_keystrokes_publishes_once(clock):
    c, win = clock

    for text in ("#", "# A", "# Ab", "# Abc"):
        c.on_editor_changed(None, text)

    _spin(c.SETTLE_MS * 2)

    assert _words(win) == ["# Abc"]


def test_settling_brings_everything_that_reads_the_text(clock):
    """One publish, one consistent set of readers — not four fan-out sites."""
    c, win = clock

    c.on_editor_changed(None, "# A")
    _spin(c.SETTLE_MS * 2)

    assert _words(win) == ["# A"]
    assert win.panel_syncs == ["# A"]
    assert win.word_counts == ["# A"]
    assert win.builds.chips == 1


def test_the_dirty_marker_does_not_wait_for_the_settle(clock):
    """The document became modified at the keystroke, not 400 ms later."""
    c, win = clock

    c.on_editor_changed(None, "# A")

    assert win.modified == 1


def test_text_that_has_not_moved_is_not_published_again(clock):
    c, win = clock

    c.on_editor_changed(None, "# A")
    _spin(c.SETTLE_MS * 2)
    c.on_editor_changed(None, "# A")
    _spin(c.SETTLE_MS * 2)

    assert _words(win) == ["# A"]


# ── A frame brings its own words ──────────────────────────────────────────────

def test_a_frame_publishes_the_words_it_was_laid_out_from(clock):
    c, win = clock

    c.on_editor_changed(None, "# A")
    _spin(c.LIVE_MS * 2)
    assert win.converter.requests == [("# A", 0, 320)]

    win.converter.deliver(FakeFrame(index=0))

    # Words first, then the picture: the frame's slide indices are only the
    # strip's rows once the strip has been read from the frame's own text.
    assert win.journal == [("words", "# A"), ("picture", 0)]


def test_a_frame_that_arrives_first_leaves_the_settle_nothing_to_do(clock):
    c, win = clock

    c.on_editor_changed(None, "# A")
    _spin(c.LIVE_MS * 2)
    win.converter.deliver(FakeFrame(index=0))
    _spin(c.SETTLE_MS * 2)

    assert _words(win) == ["# A"]      # once, not twice


def test_a_frame_for_words_already_showing_is_just_a_picture(clock):
    c, win = clock

    c.on_editor_changed(None, "# A")
    _spin(c.SETTLE_MS * 2)             # words land on the settle
    win.converter.deliver(FakeFrame(index=0))

    assert win.journal == [("words", "# A"), ("picture", 0)]


# ── The race the old arrangement lost ─────────────────────────────────────────

def test_a_late_frame_never_puts_the_older_words_back(clock):
    """
    The bug this class was written for.

    A render goes out for A, the writer types B, and A's picture comes back.
    The old code cancelled B's pending update and republished A — the strip's
    titles, numbers and timings walked backwards.  Now the stale frame is
    simply dropped: a render of B was armed by that same keystroke.
    """
    c, win = clock

    c.on_editor_changed(None, "# A")
    _spin(c.LIVE_MS * 2)               # A is out to be rendered
    c.on_editor_changed(None, "# B")   # the writer types on
    win.converter.deliver(FakeFrame(index=0))   # A's picture, too late

    assert win.journal == []           # neither the words nor the picture

    _spin(c.SETTLE_MS * 2)
    assert _words(win) == ["# B"]


def test_a_stale_frame_followed_by_a_failed_render_still_shows_the_newest_words(clock):
    """
    The symptom that made the race visible, and it had no indicator.

    Under the old ordering the stale frame published A and cancelled B's
    timer; B's render then failed, so nothing published B and the strip sat
    on A's titles until the writer typed again.
    """
    c, win = clock

    c.on_editor_changed(None, "# A")
    _spin(c.LIVE_MS * 2)
    c.on_editor_changed(None, "# B")
    win.converter.deliver(FakeFrame(index=0))            # A, dropped
    _spin(c.LIVE_MS * 2)                                 # B goes out
    win.converter.deliver(error=RuntimeError("no layout"))

    _spin(c.SETTLE_MS * 2)

    assert _words(win) == ["# B"]


def test_a_render_that_fails_shows_no_picture_and_no_error(clock):
    c, win = clock

    c.on_editor_changed(None, "# A")
    _spin(c.LIVE_MS * 2)
    win.converter.deliver(error=RuntimeError("no layout"))

    assert win.journal == []
    assert win.builds.folds == []


def test_a_frame_carries_its_fold_to_the_build(clock):
    c, win = clock

    c.on_editor_changed(None, "# A")
    _spin(c.LIVE_MS * 2)
    win.converter.deliver(FakeFrame(index=0, fold_line=12))

    assert win.builds.folds == [(0, 12)]


# ── Nothing to draw with ──────────────────────────────────────────────────────

def test_no_render_is_asked_for_when_there_is_nowhere_to_show_it(clock):
    """The strip is put away, or the machine cannot rasterize."""
    c, win = clock
    win.width = None

    c.on_editor_changed(None, "# A")
    _spin(c.SETTLE_MS * 2)

    assert win.converter.requests == []
    assert _words(win) == ["# A"]      # the words still land


# ── A document replaced wholesale ─────────────────────────────────────────────

def test_replacing_the_document_publishes_at_once(clock):
    c, win = clock

    c.document_replaced("# Opened", slide=0)

    assert _words(win) == ["# Opened"]
    assert win.converter.requests == [("# Opened", 0, 320)]


def test_replacing_the_document_drops_what_was_pending_for_the_old_one(clock):
    c, win = clock

    c.on_editor_changed(None, "# Old")
    c.document_replaced("# New", slide=0)
    _spin(c.SETTLE_MS * 2)

    assert _words(win) == ["# New"]


def test_replacing_the_document_can_leave_the_cursor_where_it_was(clock):
    """A frontmatter rewrite or a reorder does not send the writer to slide 1."""
    c, win = clock
    c.go_to_slide(3)

    c.document_replaced("# Rewritten")

    assert c.current_slide == 3


# ── Which slide ───────────────────────────────────────────────────────────────

TWO_SLIDES = "# One\n\nfirst\n\n---\n\n# Two\n\nsecond\n"


def test_the_cursor_crossing_into_another_slide_renders_it(clock):
    c, win = clock
    win.editor.text = TWO_SLIDES
    c.document_replaced(TWO_SLIDES, slide=0)
    win.converter.requests.clear()

    win.editor.offset = TWO_SLIDES.index("# Two") + 2
    c.start()
    _spin(c.CURSOR_MS * 3)

    assert c.current_slide == 1
    assert win.converter.requests[-1][1] == 1


def test_clicking_a_row_renders_that_slide_without_waiting_for_the_poll(clock):
    c, win = clock

    c.go_to_slide(2)

    assert c.current_slide == 2
    assert win.converter.requests[-1][1] == 2


def test_a_buffer_replaced_without_a_word_is_caught_by_the_poll(clock):
    """
    set_text() blocks the editor's own handler, so a swap is not an edit.

    Every deliberate caller says so with document_replaced(); this is the
    backstop for the one that forgets, and it is why the poll reads the
    buffer rather than trusting what it was last told.
    """
    c, win = clock
    win.editor.text = "# Swapped in behind the clock"

    c.start()
    _spin(c.CURSOR_MS + c.SETTLE_MS * 2)

    assert _words(win) == ["# Swapped in behind the clock"]


# ── The real waits ────────────────────────────────────────────────────────────

def test_the_picture_is_asked_for_before_the_document_is_called_settled():
    """
    The one relation between the constants that has to hold.

    The settle is the fallback: when a picture can be had, it should be on
    its way before the words give up waiting for it, so the two arrive
    together rather than the strip updating twice.
    """
    assert SettleClock.LIVE_MS < SettleClock.SETTLE_MS
