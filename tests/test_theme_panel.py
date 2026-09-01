"""
test_theme_panel.py — the deck's own settings, in the right-hand inspector.

Theme, aspect ratio and logo belong to the document; the panel changes them
and the window writes them into the frontmatter of any deck that pins one.
So the invariant worth pinning hardest is the one that keeps that from
looping: being *told* what the document renders at must not read as the user
*choosing* it.  ``select_theme`` and ``select_ratio`` show a value; only a
click emits.

Everything that would reach the real themes directory, the thumbnail cache or
session.json is stubbed — the panel's own behaviour is what is under test.
"""
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from presence.slides.themes import Theme  # noqa: E402
from presence.theme_panel import ThemePanel, _SwatchCard, _apply_bg  # noqa: E402


THEMES = {
    "light":  Theme(name="Light",  slug="light",  bg="#ffffff", accent="#E17000"),
    "berlin": Theme(name="Berlin", slug="berlin", bg="#1a1a2e", accent="#5b8dee"),
    "noir":   Theme(name="Noir",   slug="noir",   bg="#111111", accent="#ff6b6b"),
}


class FakeConverter:
    """The few properties the panel reads and writes."""

    def __init__(self, theme="light", ratio="16:9"):
        self.theme      = theme
        self.ratio      = ratio
        self.logo_path  = None
        self.logo_scale = 1.0


class FakeEditor:
    def get_font_size(self):
        return 13


class FakeWindow:
    def __init__(self):
        self._editor = FakeEditor()


class FakeFileList:
    """Stands in for the Gdk.FileList a drop delivers."""

    def __init__(self, *paths):
        self._files = [_FakeGFile(p) for p in paths]

    def get_files(self):
        return self._files


class _FakeGFile:
    def __init__(self, path):
        self._path = str(path)

    def get_path(self):
        return self._path


@pytest.fixture
def panel(gtk, monkeypatch):
    """An attached ThemePanel, with disk and thumbnails stubbed out."""
    saved = []
    thumb_calls = []

    class FakeThumbCache:
        def get_async(self, theme, ratio, callback):
            thumb_calls.append((theme.slug, ratio))

        def invalidate(self, slug):
            pass

    monkeypatch.setattr("presence.slides.theme_loader.load_all_themes",
                        lambda *a, **k: dict(THEMES))
    monkeypatch.setattr("presence.theme_manager_ui._thumb_cache", FakeThumbCache())
    monkeypatch.setattr("presence.session.save_editor_prefs", saved.append)

    made = []

    def build(theme="light", ratio="16:9", attach=True):
        p = ThemePanel()
        conv = FakeConverter(theme=theme, ratio=ratio)
        win  = FakeWindow()
        if attach:
            p.attach(win, conv)
        made.append(p)
        p._test_converter = conv
        p._test_window    = win
        p._test_saved     = saved
        p._test_thumbs    = thumb_calls
        return p

    yield build
    for p in made:
        p.unparent() if p.get_parent() else None


def _signals(panel):
    """Record every signal the panel emits."""
    seen = []
    for name in ("theme-changed", "ratio-changed", "rebuild-needed"):
        panel.connect(name, lambda _p, *args, n=name: seen.append((n, *args)))
    return seen


def _cards(panel):
    """The swatch cards currently in the chooser's grid, in order."""
    out = []
    child = panel._swatch_flow.get_first_child()
    while child is not None:
        inner = child.get_child() if hasattr(child, "get_child") else child
        if isinstance(inner, _SwatchCard):
            out.append(inner)
        child = child.get_next_sibling()
    return out


# ── Attaching ─────────────────────────────────────────────────────────────────

def test_an_unattached_panel_does_nothing_rather_than_raising(panel):
    p = panel(attach=False)
    p.refresh()                       # no converter yet
    assert _cards(p) == []
    assert p.has_theme("light") is False


def test_attaching_loads_the_themes_and_syncs_the_controls(panel):
    p = panel(theme="berlin", ratio="4:3")
    assert p.has_theme("berlin") is True
    assert p.has_theme("nonesuch") is False
    assert [c.slug for c in _cards(p)] == ["berlin", "light", "noir"]
    assert p._ratio_row.get_selected() == p._ratios.index("4:3")
    assert p._preview_name.get_label() == "Berlin"


def test_the_current_theme_is_the_one_card_switched_on(panel):
    p = panel(theme="noir")
    active = [c.slug for c in _cards(p) if c.get_active()]
    assert active == ["noir"]


def test_refreshing_replaces_the_cards_rather_than_stacking_them(panel):
    p = panel()
    before = len(_cards(p))
    p.refresh()
    p.refresh()
    assert len(_cards(p)) == before


# ── Being told, versus being clicked ──────────────────────────────────────────

def test_being_told_the_theme_shows_it_without_claiming_a_choice(panel):
    # The window calls this with what the *document* renders at.  If it
    # emitted, the panel would answer the frontmatter by rewriting it.
    p = panel(theme="light")
    seen = _signals(p)

    p.select_theme("berlin")

    assert p._preview_name.get_label() == "Berlin"
    assert [c.slug for c in _cards(p) if c.get_active()] == ["berlin"]
    assert seen == []
    assert p._test_converter.theme == "light"      # unchanged


def test_being_told_the_ratio_shows_it_without_claiming_a_choice(panel):
    p = panel(ratio="16:9")
    seen = _signals(p)

    p.select_ratio("4:3")

    assert p._ratio_row.get_selected() == p._ratios.index("4:3")
    assert seen == []
    assert p._test_converter.ratio == "16:9"


def test_a_ratio_the_app_does_not_have_is_ignored(panel):
    p = panel(ratio="16:9")
    before = p._ratio_row.get_selected()
    p.select_ratio("garbage")
    assert p._ratio_row.get_selected() == before


def test_a_theme_that_is_not_installed_leaves_the_preview_alone(panel):
    p = panel(theme="light")
    p.select_theme("uninstalled")
    assert p._preview_name.get_label() == "Light"


# ── Choosing ──────────────────────────────────────────────────────────────────

def test_picking_a_swatch_changes_the_deck_and_asks_for_a_build(panel):
    p = panel(theme="light")
    seen = _signals(p)

    card = next(c for c in _cards(p) if c.slug == "berlin")
    card.set_active(True)

    assert p._test_converter.theme == "berlin"
    assert ("theme-changed", "berlin") in seen
    assert ("rebuild-needed",) in seen
    assert p._test_saved[-1]["theme"] == "berlin"


def test_the_card_being_switched_off_is_not_a_choice(panel):
    p = panel(theme="light")
    seen = _signals(p)
    card = next(c for c in _cards(p) if c.slug == "light")
    p._on_swatch_toggled(card, "light")     # card is active
    seen.clear()

    card.set_active(False)
    assert seen == []
    assert p._test_converter.theme == "light"


def test_choosing_a_ratio_changes_the_deck_and_asks_for_a_build(panel):
    p = panel(ratio="16:9")
    seen = _signals(p)

    p._ratio_row.set_selected(p._ratios.index("4:3"))

    assert p._test_converter.ratio == "4:3"
    assert ("ratio-changed", "4:3") in seen
    assert ("rebuild-needed",) in seen
    assert p._test_saved[-1]["ratio"] == "4:3"


# ── The logo ──────────────────────────────────────────────────────────────────

def test_the_logo_row_names_the_thing_rather_than_instructing(panel):
    """
    The title was "Select or drop logo" beside two labelled buttons, which
    left it about eighty pixels of a 300px panel and wrapped it over four
    lines.  A row's title names the thing; the instruction is the tooltip's.
    """
    p = panel()
    assert p._logo_row.get_title() == "Logo"
    assert "drop" in p._logo_row.get_tooltip_text().lower()
    # Neither line may wrap: a long file name would otherwise push the
    # buttons around in a 300px panel.
    if hasattr(p._logo_row, "get_subtitle_lines"):
        assert p._logo_row.get_title_lines() == 1
        assert p._logo_row.get_subtitle_lines() == 1


def test_a_long_logo_name_is_named_in_full_by_the_tooltip(panel, tmp_path):
    # The subtitle is one ellipsized line, so the tooltip is where the whole
    # path has to be.
    p = panel()
    logo = tmp_path / "a-rather-long-organisation-logo-name.svg"
    logo.write_text("<svg/>")
    p._update_logo_ui(logo)
    assert p._logo_row.get_subtitle() == logo.name
    assert str(logo) in p._logo_row.get_tooltip_text()


def test_the_icon_only_clear_button_still_says_what_it_is(panel):
    # It lost its "Clear" label to fit; a screen reader must not lose it too.
    p = panel()
    assert p._logo_clear_btn.get_label() is None
    assert p._logo_clear_btn.get_tooltip_text() == "Remove the logo"


def test_no_logo_hides_the_size_slider_and_greys_the_clear_button(panel):
    p = panel()
    p._update_logo_ui(None)
    assert p._logo_row.get_subtitle() == "None"
    assert p._logo_clear_btn.get_sensitive() is False
    assert p._logo_size_row.get_visible() is False


def test_a_logo_names_itself_and_reveals_its_size_slider(panel):
    p = panel()
    p._update_logo_ui(Path("/tmp/acme-logo.png"))
    assert p._logo_row.get_subtitle() == "acme-logo.png"
    assert p._logo_clear_btn.get_sensitive() is True
    assert p._logo_size_row.get_visible() is True


def test_dropping_a_picture_on_the_row_makes_it_the_logo(panel, tmp_path):
    p = panel()
    seen = _signals(p)
    logo = tmp_path / "mark.png"

    assert p._on_logo_drop(None, FakeFileList(logo), 0, 0) is True
    assert p._test_converter.logo_path == logo
    assert p._logo_row.get_subtitle() == "mark.png"
    assert ("rebuild-needed",) in seen


def test_only_a_picture_is_accepted_as_a_logo(panel, tmp_path):
    p = panel()
    for name in ("notes.pdf", "deck.md", "archive.zip"):
        assert p._on_logo_drop(None, FakeFileList(tmp_path / name), 0, 0) is False
    assert p._test_converter.logo_path is None

    for name in ("a.png", "b.JPG", "c.jpeg", "d.svg", "e.webp"):
        assert p._on_logo_drop(None, FakeFileList(tmp_path / name), 0, 0) is True


def test_an_empty_drop_is_refused(panel):
    p = panel()
    assert p._on_logo_drop(None, FakeFileList(), 0, 0) is False


def test_the_row_highlights_while_a_file_is_over_it(panel):
    p = panel()
    p._on_logo_drop_enter(None, 0, 0)
    assert "accent" in p._logo_row.get_css_classes()
    p._on_logo_drop_leave(None)
    assert "accent" not in p._logo_row.get_css_classes()


def test_clearing_the_logo_puts_the_row_back(panel, tmp_path):
    p = panel()
    p._on_logo_drop(None, FakeFileList(tmp_path / "mark.png"), 0, 0)
    seen = _signals(p)

    p._on_clear_logo()

    assert p._test_converter.logo_path is None
    assert p._logo_row.get_subtitle() == "None"
    assert p._logo_size_row.get_visible() is False
    assert ("rebuild-needed",) in seen


def test_the_size_slider_reads_out_to_one_decimal(panel):
    p = panel()
    seen = _signals(p)

    p._logo_scale.set_value(1.55)

    assert p._test_converter.logo_scale == 1.6
    assert p._logo_size_val.get_label() == "1.6×"
    assert ("rebuild-needed",) in seen


def test_a_stored_logo_size_is_restored_on_attach(panel):
    p = panel(attach=False)
    conv = FakeConverter()
    conv.logo_scale = 1.8
    conv.logo_path  = Path("/tmp/mark.png")
    p.attach(FakeWindow(), conv)
    assert p._logo_scale.get_value() == pytest.approx(1.8)
    assert p._logo_size_val.get_label() == "1.8×"
    assert p._logo_size_row.get_visible() is True


# ── What is persisted ─────────────────────────────────────────────────────────

def test_the_panel_saves_the_deck_settings_and_the_font_size(panel, tmp_path):
    p = panel(theme="noir", ratio="16:10")
    p._test_converter.logo_path = tmp_path / "mark.png"
    p._save_editor_prefs()

    assert p._test_saved[-1] == {
        "theme":     "noir",
        "ratio":     "16:10",
        "logo":      str(tmp_path / "mark.png"),
        "font_size": 13,
    }


def test_no_logo_is_saved_as_an_empty_string_not_the_word_none(panel):
    p = panel()
    p._save_editor_prefs()
    assert p._test_saved[-1]["logo"] == ""


def test_an_unattached_panel_saves_nothing(panel):
    p = panel(attach=False)
    p._save_editor_prefs()
    assert p._test_saved == []


# ── The chooser dialog ────────────────────────────────────────────────────────

def test_the_chooser_is_built_once_and_reused(panel, monkeypatch):
    p = panel()
    monkeypatch.setattr("gi.repository.Adw.Dialog.present",
                        lambda self, parent=None: None)
    p._on_change_theme()
    first = p._theme_dialog
    assert first is not None
    p._on_change_theme()
    assert p._theme_dialog is first


@pytest.fixture
def chooser(panel, monkeypatch):
    """
    An open chooser whose manage page is built without touching disk.

    theme_manager_ui binds ``load_all_themes`` at import, so stubbing the
    loader the panel uses does not reach it.
    """
    monkeypatch.setattr("gi.repository.Adw.Dialog.present",
                        lambda self, parent=None: None)
    monkeypatch.setattr("presence.theme_manager_ui.load_all_themes",
                        lambda *a, **k: dict(THEMES))
    monkeypatch.setattr("presence.theme_manager_ui._thumb_cache",
                        type("C", (), {"get_async": lambda s, t, r, cb: None,
                                       "invalidate": lambda s, slug: None})())

    def build(**kw):
        p = panel(**kw)
        p._on_change_theme()
        return p

    return build


def test_the_chooser_reaches_the_manage_page_without_a_second_dialog(chooser):
    """
    Making a theme used to live two dialogs away, on a page of Preferences,
    while choosing one lived here.  Both are pages of one navigation view now.
    """
    p = chooser()
    assert p._theme_nav.get_visible_page().get_title() == "Themes"

    p._on_manage_themes()
    assert p._theme_nav.get_visible_page().get_title() == "Manage Themes"

    p._theme_nav.pop()
    assert p._theme_nav.get_visible_page().get_title() == "Themes"


def test_the_manage_page_is_told_to_refresh_this_panel(chooser, monkeypatch):
    # It reaches back through refresh_themes(), which the Preferences dialog
    # used to answer by forwarding here.
    import presence.theme_manager_ui as tmu
    seen  = []
    real  = tmu.build_themes_page
    monkeypatch.setattr(
        tmu, "build_themes_page",
        lambda parent, conv, refresher=None: (
            seen.append(refresher) or real(parent, conv, refresher=refresher)
        ),
    )
    p = chooser()
    p._on_manage_themes()
    assert seen == [p]


def test_refreshing_from_the_manage_page_reloads_the_swatches(panel):
    p = panel()
    before = [c.slug for c in _cards(p)]
    p.refresh_themes()
    assert [c.slug for c in _cards(p)] == before


# ── The swatch card ───────────────────────────────────────────────────────────

def test_a_card_carries_the_slug_it_stands_for(gtk):
    card = _SwatchCard("berlin", "Berlin", "#1a1a2e", "#5b8dee")
    assert card.slug == "berlin"
    assert "card" in card.get_css_classes()


def test_one_css_provider_per_colour_not_per_widget(gtk):
    from presence.theme_panel import _bg_provider_cache
    _apply_bg(gtk.Box(), "#abcdef")
    provider = _bg_provider_cache["#abcdef"]
    _apply_bg(gtk.Box(), "#abcdef")
    _apply_bg(gtk.Box(), "#abcdef")
    assert _bg_provider_cache["#abcdef"] is provider
