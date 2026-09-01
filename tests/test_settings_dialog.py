"""
test_settings_dialog.py — Preferences: the app's settings, not the document's.

The invariant this file exists for is **one setting, one home**.  Theme,
aspect ratio and logo belong to the deck and live in the inspector, which
writes them into the frontmatter.  They used to appear here as well, and on a
deck that pinned `theme:` only one of the two worked: the frontmatter wins in
`converter._render_context()`, so moving the control here ran a build and
brought the slides back unchanged.  A test that walks the dialog and finds no
such row is the cheapest guard against that coming back.
"""
import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

import presence.settings_dialog as sd_mod  # noqa: E402
import presence.theme_manager_ui as tmu  # noqa: E402
from presence.settings_dialog import SettingsDialog  # noqa: E402
from presence.slides.themes import Theme  # noqa: E402


class FakeEditor:
    def __init__(self):
        self.font_size   = 13
        self.line_length = 64
        self.calls       = []

    def get_font_size(self):          return self.font_size
    def get_line_length(self):        return self.line_length
    def set_font_size(self, pt):      self.font_size = pt;   self.calls.append(("font", pt))
    def set_line_length(self, n):     self.line_length = n;  self.calls.append(("length", n))

    def set_syntax_highlight(self, v):        self.calls.append(("syntax_highlight", v))
    def set_line_numbers(self, v):            self.calls.append(("line_numbers", v))
    def set_highlight_current_line(self, v):  self.calls.append(("highlight_line", v))
    def set_auto_indent(self, v):             self.calls.append(("auto_indent", v))
    def set_spaces_instead_of_tabs(self, v):  self.calls.append(("spaces_tabs", v))


class FakeConverter:
    theme     = "berlin"
    ratio     = "4:3"
    logo_path = None


class FakeSidebar:
    def __init__(self):
        self.rates = []

    def set_speaking_rate(self, rate):
        self.rates.append(rate)


class FakeSlidePanel:
    def __init__(self):
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1


class FakeWindow(Gtk.Window):
    """A stand-in MainWindow holding only what Preferences reads and writes."""

    def __init__(self):
        super().__init__()
        self._editor              = FakeEditor()
        self._converter           = FakeConverter()
        self._sidebar             = FakeSidebar()
        self._theme_panel         = FakeSlidePanel()
        self._timer_minutes       = 0
        self._auto_convert        = False
        self._presenter_notes_font = 22
        self._speaking_rate       = 110


@pytest.fixture
def settings(gtk, monkeypatch):
    """A real SettingsDialog over a stand-in window, writing nothing to disk."""
    editor_saves = []
    pres_saves   = []
    editor_prefs = {}
    pres_prefs   = {}

    monkeypatch.setattr(sd_mod, "save_editor_prefs", editor_saves.append)
    monkeypatch.setattr(sd_mod, "save_presentation_prefs", pres_saves.append)
    monkeypatch.setattr(sd_mod, "load_editor_prefs", lambda: dict(editor_prefs))
    monkeypatch.setattr(sd_mod, "load_presentation_prefs", lambda: dict(pres_prefs))

    # The themes page is built for real; only the disk and the renderer go.
    monkeypatch.setattr(tmu, "load_all_themes",
                        lambda *a, **k: {"light": Theme(name="Light", slug="light")})
    monkeypatch.setattr(tmu, "_thumb_cache",
                        type("C", (), {"get_async": lambda self, t, r, cb: None,
                                       "invalidate": lambda self, s: None})())

    # An Adw.PreferencesDialog exposes no page list and builds no widget tree
    # until it is presented, which a test must not do.  Record what it is
    # given instead; the pages themselves walk fine.
    pages = []
    original_add = Adw.PreferencesDialog.add

    def recording_add(self, page):
        pages.append(page)
        return original_add(self, page)

    monkeypatch.setattr(Adw.PreferencesDialog, "add", recording_add)

    made = []

    def build(**prefs):
        editor_prefs.update(prefs.pop("editor", {}))
        pres_prefs.update(prefs.pop("presentation", {}))
        parent = FakeWindow()
        pages.clear()
        dialog = SettingsDialog(parent)
        dialog._test_parent       = parent
        dialog._test_editor_saves = editor_saves
        dialog._test_pres_saves   = pres_saves
        dialog._test_pages        = list(pages)
        made.append((dialog, parent))
        return dialog

    yield build
    for dialog, parent in made:
        parent.destroy()


def _walk_titles(widget) -> list[str]:
    found = []
    if isinstance(widget, Adw.PreferencesRow):
        found.append(widget.get_title())
    child = widget.get_first_child()
    while child is not None:
        found.extend(_walk_titles(child))
        child = child.get_next_sibling()
    return found


def _row_titles(dialog) -> list[str]:
    """Every preferences row title on every page of the dialog."""
    out = []
    for page in dialog._test_pages:
        out.extend(_walk_titles(page))
    return out


def _page_titles(dialog) -> list[str]:
    return [p.get_title() for p in dialog._test_pages]


# ── One setting, one home ─────────────────────────────────────────────────────

def test_preferences_holds_no_control_that_belongs_to_the_document(settings):
    # Theme, ratio and logo are the deck's, and the inspector writes them into
    # its frontmatter.  A copy here can only set this machine's default, which
    # the frontmatter then overrides — the control would silently do nothing.
    titles = " | ".join(_row_titles(settings())).lower()
    for owned_by_the_document in ("screen ratio", "aspect", "logo"):
        assert owned_by_the_document not in titles


def test_preferences_holds_the_settings_that_are_the_apps(settings):
    titles = _row_titles(settings())
    for app_setting in ("Target duration", "Speaking rate", "Convert on save",
                        "Font size", "Line length guide"):
        assert app_setting in titles


def test_preferences_has_the_two_pages_it_documents(settings):
    assert _page_titles(settings()) == ["Presentation", "Editor"]


def test_preferences_is_no_longer_a_theme_library(settings):
    # Installing, editing and removing themes was a third page here.  A
    # library of content is not a setting, and keeping it under Settings
    # split the subject: the inspector chose a theme, this dialog made one.
    # It is a page of the inspector's theme chooser now.
    titles = _row_titles(settings())
    for moved in ("Install from file", "Create new theme",
                  "Open themes folder"):
        assert moved not in titles


# ── Opening with what was saved ───────────────────────────────────────────────

def test_the_dialog_opens_showing_the_saved_values(settings):
    dialog = settings(
        presentation={"timer_minutes": 25, "speaking_rate": 140,
                      "auto_convert": True},
        editor={"line_length": 80, "line_numbers": False},
    )
    assert int(dialog._timer_spin_row.get_value()) == 25
    assert int(dialog._rate_spin_row.get_value()) == 140
    assert int(dialog._ll_spin_row.get_value()) == 80
    assert dialog._switch_rows["line_numbers"].get_active() is False
    assert dialog._switch_rows["syntax_highlight"].get_active() is True


def test_the_font_size_shown_is_the_editor_s_own(settings):
    dialog = settings()
    dialog._test_parent._editor.font_size = 17
    assert int(SettingsDialog(dialog._test_parent)._font_spin_row.get_value()) == 17


# ── Timing ────────────────────────────────────────────────────────────────────

def test_a_target_duration_reaches_the_window_and_the_prefs(settings):
    dialog = settings()
    dialog._timer_spin_row.set_value(20)

    assert dialog._test_parent._timer_minutes == 20
    saved = dialog._test_pres_saves[-1]
    assert saved["timer_minutes"] == 20
    # Written whole, so a change to one key cannot drop the others.
    assert set(saved) == {"timer_minutes", "auto_convert",
                          "presenter_notes_font", "speaking_rate"}


def test_a_speaking_rate_also_tells_the_strip(settings):
    # The strip quotes a per-slide time from the same rate; if only the
    # presenter were told, the two would disagree about the same deck.
    dialog = settings()
    dialog._rate_spin_row.set_value(150)

    assert dialog._test_parent._speaking_rate == 150
    assert dialog._test_parent._sidebar.rates == [150]
    assert dialog._test_pres_saves[-1]["speaking_rate"] == 150


def test_convert_on_save_is_remembered(settings):
    dialog = settings()
    row = Adw.SwitchRow()
    row.set_active(True)
    dialog._on_auto_convert_toggled(row, None)

    assert dialog._test_parent._auto_convert is True
    assert dialog._test_pres_saves[-1]["auto_convert"] is True


def test_the_timing_prefs_carry_the_presenter_font_they_did_not_change(settings):
    dialog = settings()
    dialog._test_parent._presenter_notes_font = 30
    dialog._timer_spin_row.set_value(5)
    assert dialog._test_pres_saves[-1]["presenter_notes_font"] == 30


# ── Editor appearance ─────────────────────────────────────────────────────────

def test_the_font_size_reaches_the_editor(settings):
    dialog = settings()
    dialog._font_spin_row.set_value(18)
    assert ("font", 18) in dialog._test_parent._editor.calls
    assert dialog._test_editor_saves[-1]["font_size"] == 18


def test_the_line_length_guide_reaches_the_editor(settings):
    dialog = settings()
    dialog._ll_spin_row.set_value(72)
    assert ("length", 72) in dialog._test_parent._editor.calls
    assert dialog._test_editor_saves[-1]["line_length"] == 72


def test_every_editor_switch_drives_its_own_setter(settings):
    dialog = settings()
    editor = dialog._test_parent._editor
    for key in ("syntax_highlight", "line_numbers", "highlight_line",
                "auto_indent", "spaces_tabs"):
        editor.calls.clear()
        row = dialog._switch_rows[key]
        row.set_active(not row.get_active())
        assert editor.calls == [(key, row.get_active())]


def test_saving_editor_prefs_records_the_whole_form(settings):
    dialog = settings()
    dialog._switch_rows["auto_indent"].set_active(False)

    saved = dialog._test_editor_saves[-1]
    assert saved["theme"]       == "berlin"
    assert saved["ratio"]       == "4:3"
    assert saved["logo"]        == ""
    assert saved["font_size"]   == 13
    assert saved["line_length"] == 64
    assert saved["auto_indent"] is False
    assert saved["syntax_highlight"] is True


