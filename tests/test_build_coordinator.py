"""
test_build_coordinator.py — Deferring work until the build matches the deck.

Two rules are worth pinning down here, because both are the kind that fail
silently rather than loudly:

*  Nothing that consumes the PDF may run against a stale build, and whatever
   was waiting must run exactly once when the build lands — not twice, and
   not at all if the build failed.
*  Fold lines come from two places that agree by construction: a build
   measures every slide, the live render measures the one being edited.  The
   live answer is fresher for its own slide and must not disturb the rest.

Widgets cannot be built under this suite's conftest (GTK 4 has no offscreen
backend), so these drive the real coordinator against a stand-in window.
"""

import pytest

from presence.build_coordinator import BuildCoordinator


class FakeEditor:
    def __init__(self, text: str = "") -> None:
        self._text = text
        self.folds = None

    def get_text(self) -> str:
        return self._text

    def set_fold_lines(self, lines) -> None:
        self.folds = list(lines)


class FakeLabel:
    def __init__(self) -> None:
        self.text = None
        self.classes = set()

    def set_label(self, text):        self.text = text
    def get_label(self):              return self.text
    def add_css_class(self, name):    self.classes.add(name)
    def remove_css_class(self, name): self.classes.discard(name)


class FakeChip:
    """The header button and the widgets inside it."""

    def __init__(self) -> None:
        self.sensitive = None
        self.tooltip = None
        self.a11y = None
        self.icon = None
        self.child = None
        self.spinning = False

    # the button
    def set_sensitive(self, on):       self.sensitive = on
    def set_tooltip_text(self, text):  self.tooltip = text
    def update_property(self, _props, values): self.a11y = values[0]
    # the stack inside it
    def set_visible_child_name(self, name): self.child = name
    # the icon inside that
    def set_from_icon_name(self, name):     self.icon = name
    # the spinner beside it
    def start(self): self.spinning = True
    def stop(self):  self.spinning = False


class FakeDocuments:
    """The document state the coordinator asks about, and nothing else."""

    def __init__(self) -> None:
        self.file_path   = None
        self.output_path = None
        self.pres_path   = None
        self.packed      = 0

    @property
    def base_dir(self):
        return self.file_path.parent if self.file_path else None

    def pack_pres(self) -> None:
        self.packed += 1


class FakeWindow:
    """
    Everything BuildCoordinator now asks a window for.

    Six names, and every one of them a widget or a collaborator.  It used to
    be fourteen, because the build's own state — built_text, converting,
    html_uri, what was waiting on the build — lived on the window and this
    stand-in had to declare it.  That state is the coordinator's now, so
    these tests set it on the coordinator directly.
    """

    def __init__(self, current: str = ""):
        self.editor         = FakeEditor(current)
        self.sidebar        = _Sidebar()
        self.banner         = _Banner()
        self.present_button = _Button()
        self.documents      = FakeDocuments()
        self.speaking_rate  = 110
        self.triggered      = 0


class _Banner:
    def __init__(self): self.title = None; self.revealed = None
    def set_title(self, t): self.title = t
    def set_revealed(self, r): self.revealed = r


class _Button:
    def __init__(self): self.sensitive = None
    def set_sensitive(self, on): self.sensitive = on


class _Sidebar:
    def __init__(self): self.converting = None
    def set_converting(self, on): self.converting = on


def coordinator(current: str = "", built=None, converting: bool = False):
    """A real coordinator with a stand-in window and a stand-in chip."""
    win   = FakeWindow(current)
    coord = BuildCoordinator(win)
    coord.built_text = built
    coord.converting = converting
    coord.html_uri   = "file:///built.html"
    # trigger() is stubbed out: these tests are about when a build is asked
    # for and what happens when it lands, not about WeasyPrint.
    coord.trigger = lambda *a: setattr(win, "triggered", win.triggered + 1)
    win.chip       = FakeChip()
    win.chip_label = FakeLabel()
    coord.attach_chip(win.chip, win.chip, win.chip, win.chip, win.chip_label)
    return coord, win


# ── Deferring until the build is current ──────────────────────────────────────

def test_a_current_build_runs_the_action_straight_away():
    coord, win = coordinator(current="same", built="same")
    ran = []

    coord.with_current_build(lambda: ran.append(True))

    assert ran == [True]
    assert win.triggered == 0
    assert coord._after_build is None


def test_a_stale_build_defers_the_action_and_asks_for_a_build():
    coord, win = coordinator(current="edited", built="original")
    ran = []

    coord.with_current_build(lambda: ran.append(True))

    assert ran == []                      # must not ship the old deck
    assert win.triggered == 1
    assert coord._after_build is not None


def test_no_build_yet_also_defers():
    coord, win = coordinator(current="text", built=None)
    ran = []

    coord.with_current_build(lambda: ran.append(True))

    assert ran == []
    assert win.triggered == 1


def test_a_build_with_no_html_defers_even_when_the_text_matches():
    """'current' is not enough — there must be output to hand over."""
    coord, win = coordinator(current="same", built="same")
    coord.html_uri = None
    ran = []

    coord.with_current_build(lambda: ran.append(True))

    assert ran == []
    assert win.triggered == 1


# ── Failure ───────────────────────────────────────────────────────────────────

def test_a_failed_build_drops_whatever_was_waiting():
    """A queued export cannot run against a build that did not happen."""
    coord, win = coordinator(current="edited", built="original")
    ran = []
    coord.with_current_build(lambda: ran.append(True))
    assert coord._after_build is not None

    coord.on_failed(None, "WeasyPrint blew up")

    assert ran == []
    assert coord._after_build is None


def test_a_failed_build_leaves_the_document_stale():
    """built_text is untouched on failure, so the chip keeps asking."""
    coord, win = coordinator(current="edited", built="original", converting=True)
    coord.converting = False

    assert coord.state() == "stale"


# ── Fold lines ────────────────────────────────────────────────────────────────

def test_a_build_replaces_every_fold_line():
    coord, win = coordinator()

    coord.set_build_folds([None, 12, None])

    assert coord.fold_lines == [None, 12, None]
    assert win.editor.folds == [None, 12, None]


def test_a_live_render_updates_only_its_own_slide():
    coord, win = coordinator()
    coord.set_build_folds([None, 12, 30])

    coord.set_live_fold(1, 18)

    assert coord.fold_lines == [None, 18, 30]


def test_an_unchanged_fold_does_not_redraw():
    coord, win = coordinator()
    coord.set_build_folds([None, 12, 30])
    win.editor.folds = None                 # watch for a second call

    coord.set_live_fold(1, 12)

    assert win.editor.folds is None


def test_a_slide_added_since_the_build_gets_a_slot():
    """Typing a new slide must not raise before the next build catches up."""
    coord, win = coordinator()
    coord.set_build_folds([None])

    coord.set_live_fold(3, 44)

    assert coord.fold_lines == [None, None, None, 44]


def test_a_fold_can_be_cleared_when_the_slide_stops_overflowing():
    coord, win = coordinator()
    coord.set_build_folds([21])

    coord.set_live_fold(0, None)

    assert coord.fold_lines == [None]


def test_fold_lines_are_handed_out_as_a_copy():
    """A caller must not be able to edit the coordinator's list in place."""
    coord, _win = coordinator()
    coord.set_build_folds([1, 2])

    coord.fold_lines.append(3)

    assert coord.fold_lines == [1, 2]


# ── What the chip says ────────────────────────────────────────────────────────

def test_the_chip_shows_a_spinner_and_refuses_clicks_while_building():
    coord, win = coordinator(current="a", built="a", converting=True)

    coord.update_chip()

    assert win.chip.child == "spinner"
    assert win.chip.spinning is True
    assert win.chip_label.get_label() == "Building…"
    assert win.chip.sensitive is False


def test_the_chip_says_up_to_date_when_the_build_matches():
    coord, win = coordinator(current="same", built="same")

    coord.update_chip()

    assert win.chip_label.get_label() == "Up to date"
    assert win.chip.icon == "object-select-symbolic"
    assert win.chip.sensitive is True


def test_the_chip_asks_for_a_rebuild_once_the_document_moves_on():
    coord, win = coordinator(current="edited", built="original")

    coord.update_chip()

    assert win.chip_label.get_label() == "Rebuild needed"
    assert win.chip.icon == "view-refresh-symbolic"
    # Undimmed: this one wants attention, "Up to date" does not.
    assert "dim-label" not in win.chip_label.classes


def test_the_chip_tells_a_screen_reader_what_it_says():
    coord, win = coordinator(current="same", built="same")

    coord.update_chip()

    assert win.chip.a11y == "Up to date — rebuild"


def test_a_failed_build_stops_the_spinner_and_shows_why():
    coord, win = coordinator(current="edited", built="original", converting=True)

    coord.on_failed(None, "No slides found — separate slides with ---")

    assert coord.converting is False
    assert win.chip.spinning is False
    assert win.sidebar.converting is False
    assert win.banner.revealed is True
    assert "slides" in win.banner.title
