"""
test_theme_editor.py — the theme wizard and the advanced editor, as widgets.

2 552 lines, the largest file in the project, and until now no test imported
it.  These build the real dialogs (see the ``gtk`` fixture in conftest.py) and
drive them the way the panel does.  Nothing is presented, so nothing appears.

Two things are stubbed, and only two: ``install_theme`` and
``load_all_themes``, which otherwise read and write the developer's own
``~/.local/share/presence/themes``.  What a theme directory should contain is
checked against the real ``_write_to_dir`` and read back with the real
``load_theme_from_dir``, so the format contract is not stubbed away.
"""
import dataclasses
import json

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from presence.slides.themes import Theme  # noqa: E402
from presence.slides.theme_loader import load_theme_from_dir  # noqa: E402
from presence.theme_editor import (  # noqa: E402
    _FONTS, _PRESETS, _ColourRow, ThemeEditor, ThemeEditorAdvanced,
    _auto_slug, _contrast_ratio, _hex_to_rgba, _rgba_to_hex,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def no_theme_dir(monkeypatch):
    """
    Keep the editors away from the real themes directory.

    Returns the list every install lands in, so a test can say what was
    installed without anything reaching disk.
    """
    installed = []
    themes = {}

    def fake_install(source_dir, themes_dir=None):
        data = json.loads((source_dir / "theme.json").read_text())
        theme = Theme(**{k: v for k, v in data.items()
                         if k in {f.name for f in dataclasses.fields(Theme)}
                         and not k.startswith("_")})
        installed.append((source_dir, theme))
        return theme

    monkeypatch.setattr("presence.theme_editor.install_theme", fake_install)
    monkeypatch.setattr("presence.theme_editor.load_all_themes", lambda: themes)
    return installed, themes


@pytest.fixture
def wizard(gtk, no_theme_dir):
    """A real ThemeEditor, torn down through its own cleanup."""
    made = []

    def build(existing_theme=None, on_installed=None):
        parent = gtk.Window()
        win = ThemeEditor(parent, existing_theme=existing_theme,
                          on_installed=on_installed)
        made.append((win, parent))
        return win

    yield build
    for win, parent in made:
        win._on_destroy()
        parent.destroy()


@pytest.fixture
def advanced(gtk, no_theme_dir):
    """A real ThemeEditorAdvanced, torn down through its own cleanup."""
    made = []

    def build(existing_theme=None, custom_css="", font_files=None,
              on_installed=None, wizard=None):
        parent = gtk.Window()
        win = ThemeEditorAdvanced(parent, existing_theme=existing_theme,
                                  custom_css=custom_css, font_files=font_files,
                                  on_installed=on_installed, wizard=wizard)
        made.append((win, parent))
        return win

    yield build
    for win, parent in made:
        win._returning_to_wizard = True    # do not close a wizard we did not open
        win._on_destroy()
        parent.destroy()


def _toggle(gtk, active=True):
    """An active ToggleButton, which is what the card handlers expect."""
    btn = gtk.ToggleButton()
    btn.set_active(active)
    return btn


# ── Colour arithmetic ─────────────────────────────────────────────────────────

def test_contrast_ratio_spans_the_whole_range():
    assert _contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0, abs=0.01)
    assert _contrast_ratio("#777777", "#777777") == pytest.approx(1.0, abs=0.01)
    # Order does not matter: it is a ratio of the lighter to the darker.
    assert _contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)


def test_contrast_ratio_expands_three_digit_hex():
    assert _contrast_ratio("#fff", "#000") == pytest.approx(
        _contrast_ratio("#ffffff", "#000000")
    )


def test_hex_round_trips_through_rgba():
    for hex_val in ("#000000", "#ffffff", "#e17000", "#5b8dee"):
        assert _rgba_to_hex(_hex_to_rgba(hex_val)) == hex_val


def test_an_unparseable_colour_falls_back_to_grey():
    # Not an exception into a live dialog: the swatch goes grey.
    assert _rgba_to_hex(_hex_to_rgba("not a colour")) == "#808080"


# ── Slugs ─────────────────────────────────────────────────────────────────────

def test_auto_slug_makes_a_filesystem_safe_identifier():
    assert _auto_slug("My Deck Theme") == "my-deck-theme"
    assert _auto_slug("Already-fine_2") == "already-fine_2"
    # Leading and trailing separators go, but a run in the middle is replaced
    # character for character rather than collapsed: "a  b" is "a--b".
    assert _auto_slug("  Spaced  Out  ") == "spaced--out"


def test_the_slug_alphabet_is_ascii_only():
    # Accented and non-Latin letters are not transliterated, they are
    # replaced.  The name keeps them; only the directory name loses them.
    assert _auto_slug("Ünïcødé!") == "n-c-d"


def test_a_name_with_nothing_usable_still_yields_a_slug():
    # A directory has to be called something — including a name written
    # entirely outside the slug alphabet.
    assert _auto_slug("!!!") == "theme"
    assert _auto_slug("") == "theme"
    assert _auto_slug("---") == "theme"
    assert _auto_slug("日本語") == "theme"


# ── The wizard's starting state ───────────────────────────────────────────────

def test_a_new_theme_starts_on_the_first_preset():
    theme = ThemeEditor._initial_theme(None)
    label, bg, fg, accent, title_bg, title_fg = _PRESETS[0]
    assert (theme.bg, theme.fg, theme.accent) == (bg, fg, accent)
    assert (theme.title_bg, theme.title_fg) == (title_bg, title_fg)


def test_editing_a_theme_copies_it_rather_than_aliasing_it():
    original = Theme(name="Mine", slug="mine", accent="#123456")
    copy = ThemeEditor._initial_theme(original)
    assert copy == original
    assert copy is not original


def test_a_builtin_theme_opens_read_only(wizard):
    win = wizard(existing_theme=Theme(name="Light", slug="light"))
    assert win._is_builtin is True
    assert wizard(existing_theme=Theme(name="Mine", slug="mine"))._is_builtin is False


def test_custom_css_is_carried_in_from_the_theme_on_disk(tmp_path):
    css = tmp_path / "theme.css"
    css.write_text(".slide { letter-spacing: 0.01em; }")
    theme = Theme(name="X", slug="x", custom_css_path=str(css))
    assert ".slide" in ThemeEditor._load_custom_css(theme)
    # A theme whose css file has since been deleted opens blank, not broken.
    css.unlink()
    assert ThemeEditor._load_custom_css(theme) == ""
    assert ThemeEditor._load_custom_css(None) == ""


# ── Stepping through the wizard ───────────────────────────────────────────────

def test_the_wizard_starts_at_the_first_step_with_back_disabled(wizard):
    win = wizard()
    win._go_to_step(0)
    assert win._step == 0
    assert win._back_btn.get_sensitive() is False
    assert win._next_btn.get_label() == "Next →"


def test_next_walks_forward_and_marks_each_step_done(wizard):
    win = wizard()
    win._go_to_step(0)
    for expected in (1, 2, 3):
        win._on_next()
        assert win._step == expected
        assert win._nav_rows[expected - 1]._done_icon.get_visible() is True
    assert win._next_btn.get_label() == "Done ✓"
    assert win._back_btn.get_sensitive() is True


def test_back_stops_at_the_first_step(wizard):
    win = wizard()
    win._go_to_step(0)
    win._on_back()
    assert win._step == 0
    win._go_to_step(2)
    win._on_back()
    assert win._step == 1


def test_the_last_step_installs_rather_than_stepping_off_the_end(wizard, no_theme_dir):
    installed, _ = no_theme_dir
    win = wizard()
    win._name_row.set_text("Finished Theme")
    win._go_to_step(3)
    win._on_next()
    assert win._step == 3                    # did not walk past the end
    assert [t.name for _, t in installed] == ["Finished Theme"]


def test_the_nav_list_jumps_to_the_step_it_names(wizard):
    win = wizard()
    win._on_nav_row_activated(win._nav_list, win._nav_rows[2])
    assert win._step == 2


# ── Name, and the slug that follows it ────────────────────────────────────────

def test_typing_a_name_derives_the_slug(wizard):
    win = wizard()
    win._name_row.set_text("My Deck Theme")
    assert win._theme.name == "My Deck Theme"
    assert win._theme.slug == "my-deck-theme"
    assert win._nav_title.get_label() == "My Deck Theme"


def test_an_emptied_name_leaves_the_heading_readable(wizard):
    win = wizard()
    win._name_row.set_text("")
    assert win._nav_title.get_label() == "New theme"


def test_a_hand_written_slug_survives_a_later_name_change(advanced):
    # Only the advanced editor exposes the slug.  Once it has been set by
    # hand it is the user's, and renaming the theme must not overwrite it.
    win = advanced()
    win._slug_row.set_text("house-style")
    win._name_row.set_text("Something Else Entirely")
    assert win._theme.slug == "house-style"
    assert win._theme.name == "Something Else Entirely"


def test_an_untouched_slug_still_follows_the_name(advanced):
    win = advanced()
    win._name_row.set_text("Second Try")
    assert win._theme.slug == "second-try"
    assert win._slug_row.get_text() == "second-try"


# ── Slug validation ───────────────────────────────────────────────────────────

def test_an_empty_or_malformed_slug_is_refused(wizard):
    win = wizard()
    assert win._validate_slug("") is False
    assert win._validate_slug("Has Capitals") is False
    assert win._validate_slug("has spaces") is False
    assert win._validate_slug("has/slash") is False
    assert win._validate_slug("fine-one_2") is True


def test_a_slug_already_installed_is_refused(wizard, no_theme_dir):
    _, themes = no_theme_dir
    themes["taken"] = Theme(name="Taken", slug="taken")
    win = wizard()
    assert win._validate_slug("taken") is False
    assert win._validate_slug("free") is True


def test_a_theme_may_keep_its_own_slug_while_being_edited(wizard, no_theme_dir):
    # Editing "mine" must not report that "mine" is already installed.
    _, themes = no_theme_dir
    themes["mine"] = Theme(name="Mine", slug="mine")
    win = wizard(existing_theme=Theme(name="Mine", slug="mine"))
    assert win._validate_slug("mine") is True


def test_a_refused_slug_marks_the_row_and_a_good_one_clears_it(wizard):
    win = wizard()
    win._validate_slug("no good")
    assert "error" in win._name_row.get_css_classes()
    assert win._slug_error_icon is not None
    assert win._slug_error_icon.get_visible() is True

    win._validate_slug("good-one")
    assert "error" not in win._name_row.get_css_classes()
    assert win._slug_error_icon.get_visible() is False


# ── Palette ───────────────────────────────────────────────────────────────────

def test_a_preset_sets_every_colour_it_owns(gtk, wizard):
    win = wizard()
    idx = 1                                   # Midnight
    label, bg, fg, accent, title_bg, title_fg = _PRESETS[idx]
    win._on_preset_toggled(_toggle(gtk), idx)

    assert win._preset_idx == idx
    assert (win._theme.bg, win._theme.fg, win._theme.accent) == (bg, fg, accent)
    assert (win._theme.title_bg, win._theme.title_fg) == (title_bg, title_fg)
    # The rows follow, so the pickers do not disagree with the theme.
    assert win._bg_row._entry.get_text() == bg
    assert win._fg_row._entry.get_text() == fg
    assert win._accent_row._entry.get_text() == accent


def test_the_card_being_switched_off_is_ignored(gtk, wizard):
    # set_group() deactivates the old card as it activates the new one; only
    # the activation carries a choice.
    win = wizard()
    before = win._theme
    win._on_preset_toggled(_toggle(gtk, active=False), 3)
    assert win._theme is before
    assert win._preset_idx == 0


def test_a_colour_typed_into_a_row_reaches_the_theme(wizard):
    win = wizard()
    win._bg_row._entry.set_text("#123456")
    assert win._theme.bg == "#123456"


def test_the_contrast_readout_grades_the_accent(wizard):
    win = wizard()
    win._theme = dataclasses.replace(win._theme, bg="#ffffff", accent="#000000")
    win._update_contrast()
    assert "good" in win._contrast_lbl.get_text()
    assert "success" in win._contrast_lbl.get_css_classes()

    win._theme = dataclasses.replace(win._theme, bg="#ffffff", accent="#fafafa")
    win._update_contrast()
    assert "too low" in win._contrast_lbl.get_text()
    assert "error" in win._contrast_lbl.get_css_classes()
    assert "success" not in win._contrast_lbl.get_css_classes()


# ── Font and title-slide steps ────────────────────────────────────────────────

def test_choosing_a_font_sets_the_body_stack(gtk, wizard):
    win = wizard()
    win._on_font_toggled(_toggle(gtk), 2)
    assert win._font_idx == 2
    assert win._theme.body_font == _FONTS[2][1]


def test_the_title_style_reads_the_palette_that_is_selected(gtk, wizard):
    win = wizard()
    win._on_preset_toggled(_toggle(gtk), 4)          # Parchment
    _, bg, fg, _, title_bg, title_fg = _PRESETS[4]

    win._on_title_style_toggled(_toggle(gtk), 0)     # dark cover
    assert (win._theme.title_bg, win._theme.title_fg) == (title_bg, title_fg)

    win._on_title_style_toggled(_toggle(gtk), 1)     # light cover
    assert (win._theme.title_bg, win._theme.title_fg) == (bg, fg)


def test_a_custom_title_style_leaves_the_colours_alone(gtk, wizard):
    win = wizard()
    win._theme = dataclasses.replace(win._theme,
                                     title_bg="#abcdef", title_fg="#fedcba")
    win._on_title_style_toggled(_toggle(gtk), 2)     # custom
    assert win._title_style == 2
    assert (win._theme.title_bg, win._theme.title_fg) == ("#abcdef", "#fedcba")


# ── What gets written ─────────────────────────────────────────────────────────

def test_optional_colours_are_left_out_rather_than_written_empty(wizard):
    # An empty string in theme.json is a colour; an absent key is "inherit".
    win = wizard()
    data = win._collect_theme_json()
    for key in ("accent2", "heading_color", "title_accent", "heading_font"):
        assert key not in data
    for key in ("name", "slug", "bg", "fg", "accent", "code_bg",
                "title_bg", "title_fg", "body_font", "mono_font",
                "base_size", "pygments_style"):
        assert key in data


def test_optional_colours_are_written_once_they_are_set(wizard):
    win = wizard()
    win._theme = dataclasses.replace(
        win._theme, accent2="#111111", heading_color="#222222",
        title_accent="#333333", heading_font="'Some Face', serif",
    )
    data = win._collect_theme_json()
    assert data["accent2"]       == "#111111"
    assert data["heading_color"] == "#222222"
    assert data["title_accent"]  == "#333333"
    assert data["heading_font"]  == "'Some Face', serif"


def test_callouts_are_written_as_lists_json_can_hold(wizard):
    win = wizard()
    data = win._collect_theme_json()
    for kind in ("tip", "info", "warning", "danger"):
        assert isinstance(data[f"callout_{kind}"], list)
        assert len(data[f"callout_{kind}"]) == 3
        assert isinstance(data[f"callout_icon_{kind}"], str)
    json.dumps(data)          # would raise on a tuple-keyed or non-JSON value


def test_a_written_theme_loads_back_as_the_same_theme(wizard, tmp_path):
    win = wizard()
    win._name_row.set_text("Round Trip")
    win._theme = dataclasses.replace(
        win._theme, author="A. Person", description="A test theme",
        bg="#101010", fg="#f0f0f0", accent="#ff8800", base_size=40,
    )
    dest = tmp_path / "round-trip"
    win._write_to_dir(dest)

    back = load_theme_from_dir(dest)
    assert back.name        == "Round Trip"
    assert back.slug        == "round-trip"
    assert back.author      == "A. Person"
    assert back.description == "A test theme"
    assert (back.bg, back.fg, back.accent) == ("#101010", "#f0f0f0", "#ff8800")
    assert back.base_size   == 40
    assert back.callout_tip == win._theme.callout_tip


def test_a_stylesheet_is_written_only_when_there_is_one(wizard, tmp_path):
    win = wizard()
    win._write_to_dir(tmp_path / "none")
    assert not (tmp_path / "none" / "theme.css").exists()

    win._custom_css = "   \n  "          # whitespace is not a stylesheet
    win._write_to_dir(tmp_path / "blank")
    assert not (tmp_path / "blank" / "theme.css").exists()

    win._custom_css = ".slide h1 { letter-spacing: -0.02em; }"
    win._write_to_dir(tmp_path / "some")
    assert (tmp_path / "some" / "theme.css").read_text() == win._custom_css


def test_fonts_are_bundled_and_a_missing_one_is_skipped(wizard, tmp_path):
    real = tmp_path / "MyFace-Regular.woff2"
    real.write_bytes(b"not really a font")
    gone = tmp_path / "Deleted-Bold.woff2"

    win = wizard()
    win._font_files = [real, gone]
    dest = tmp_path / "with-fonts"
    win._write_to_dir(dest)

    assert (dest / "fonts" / "MyFace-Regular.woff2").exists()
    assert not (dest / "fonts" / "Deleted-Bold.woff2").exists()


def test_no_fonts_directory_when_there_are_no_fonts(wizard, tmp_path):
    win = wizard()
    win._write_to_dir(tmp_path / "plain")
    assert not (tmp_path / "plain" / "fonts").exists()


# ── Installing ────────────────────────────────────────────────────────────────

def test_installing_writes_the_theme_and_tells_the_caller(wizard, no_theme_dir):
    installed, _ = no_theme_dir
    seen = []
    win = wizard(on_installed=seen.append)
    win._name_row.set_text("Installable")
    win._on_install()

    assert len(installed) == 1
    source_dir, theme = installed[0]
    assert source_dir.name == "installable"      # the slug names the directory
    assert theme.name == "Installable"
    assert [t.name for t in seen] == ["Installable"]


def test_a_refused_slug_stops_the_install(wizard, no_theme_dir):
    installed, themes = no_theme_dir
    themes["taken"] = Theme(name="Taken", slug="taken")
    win = wizard()
    win._name_row.set_text("Taken")
    win._on_install()
    assert installed == []


def test_a_nameless_theme_is_not_installed(wizard, no_theme_dir):
    installed, _ = no_theme_dir
    win = wizard()
    win._theme = dataclasses.replace(win._theme, name="   ", slug="blank-name")
    win._on_install()
    assert installed == []


# ── Forking a built-in ────────────────────────────────────────────────────────

def test_forking_names_the_copy_and_clears_the_author(gtk, wizard, monkeypatch):
    opened = []
    monkeypatch.setattr(ThemeEditor, "present",
                        lambda self, parent=None: opened.append(self))

    win = wizard(existing_theme=Theme(name="Light", slug="light",
                                      author="Built-in"))
    win._on_fork()

    assert len(opened) == 1
    forked = opened[0]._theme
    assert forked.name   == "Light (copy)"
    assert forked.slug   == "light-copy"
    assert forked.author == ""
    assert opened[0]._is_builtin is False       # the copy is editable
    opened[0]._on_destroy()


# ── Handing over to the advanced editor and back ──────────────────────────────

def test_the_advanced_editor_opens_with_the_wizard_state(gtk, wizard, monkeypatch):
    opened = []
    monkeypatch.setattr(ThemeEditorAdvanced, "present",
                        lambda self, parent=None: opened.append(self))

    win = wizard()
    win._name_row.set_text("Halfway")
    win._custom_css = ".slide { padding: 4rem; }"
    win._on_open_advanced()

    assert len(opened) == 1
    adv = opened[0]
    assert adv._theme.name  == "Halfway"
    assert adv._custom_css  == ".slide { padding: 4rem; }"
    assert adv._wizard is win               # so it can hand back
    adv._returning_to_wizard = True
    adv._on_destroy()


def test_going_back_to_the_wizard_carries_the_edits(gtk, wizard, advanced,
                                                    monkeypatch):
    monkeypatch.setattr(ThemeEditor, "present", lambda self, parent=None: None)
    monkeypatch.setattr(ThemeEditorAdvanced, "close", lambda self: None)

    wiz = wizard()
    adv = advanced(existing_theme=dataclasses.replace(wiz._theme), wizard=wiz)
    adv._theme      = dataclasses.replace(adv._theme, accent="#abcdef")
    adv._custom_css = ".slide { color: red; }"
    adv._font_files = ["a-font"]

    adv._on_back_to_wizard()

    assert wiz._theme.accent  == "#abcdef"
    assert wiz._custom_css    == ".slide { color: red; }"
    assert wiz._font_files    == ["a-font"]
    assert adv._returning_to_wizard is True


def test_closing_the_advanced_editor_outright_takes_the_wizard_with_it(
        gtk, wizard, advanced, monkeypatch):
    closed = []
    monkeypatch.setattr(ThemeEditor, "close", lambda self: closed.append(self))

    wiz = wizard()
    adv = advanced(wizard=wiz)
    adv._returning_to_wizard = False
    adv._on_destroy()
    assert closed == [wiz]


def test_returning_to_the_wizard_does_not_close_it(gtk, wizard, advanced,
                                                   monkeypatch):
    closed = []
    monkeypatch.setattr(ThemeEditor, "close", lambda self: closed.append(self))

    wiz = wizard()
    adv = advanced(wizard=wiz)
    adv._returning_to_wizard = True
    adv._on_destroy()
    assert closed == []


# ── The advanced editor's own fields ──────────────────────────────────────────

def test_metadata_rows_reach_the_theme(advanced):
    win = advanced()
    win._author_row.set_text("A. Person")
    win._desc_row.set_text("For quarterly reviews")
    assert win._theme.author      == "A. Person"
    assert win._theme.description == "For quarterly reviews"


def test_a_callout_triple_is_replaced_whole(advanced):
    win = advanced()
    win._on_callout_changed("tip", ("#111111", "#222222", "#333333"))
    assert win._theme.callout_tip == ("#111111", "#222222", "#333333")


def test_a_callout_icon_is_trimmed_to_something_css_can_hold(advanced):
    # The value lands inside a CSS content: '…' string, so a quote would
    # close it early and a long run would not fit the badge.
    win = advanced()
    row = Adw.EntryRow()
    row.set_text("it's a very long icon")
    win._on_icon_changed("tip", row)
    assert "'" not in win._theme.callout_icon_tip
    assert len(win._theme.callout_icon_tip) <= 4


def test_editing_the_stylesheet_updates_what_will_be_written(advanced):
    win = advanced()
    buf = Gtk.TextBuffer()
    buf.set_text(".slide { margin: 0; }")
    win._on_css_changed(buf)
    assert win._custom_css == ".slide { margin: 0; }"


# ── Bundled fonts, in the advanced editor ─────────────────────────────────────

def test_adding_a_font_lists_it_once(advanced, tmp_path):
    face = tmp_path / "MyFace-Regular.woff2"
    face.write_bytes(b"x")
    win = advanced()
    win._add_font_row(face)
    win._add_font_row(face)
    assert win._font_files == [face]
    assert list(win._font_rows) == [face]


def test_a_font_named_wrongly_is_flagged_rather_than_refused(advanced, tmp_path):
    bad  = tmp_path / "myface.woff2"          # no Family-Weight split
    good = tmp_path / "MyFace-Bold.woff2"
    for p in (bad, good):
        p.write_bytes(b"x")
    win = advanced()
    win._add_font_row(bad)
    win._add_font_row(good)

    assert "error" in win._font_rows[bad].get_css_classes()
    assert "Rename" in win._font_rows[bad].get_subtitle()
    assert "error" not in win._font_rows[good].get_css_classes()
    assert win._font_files == [bad, good]     # still bundled


def test_removing_a_font_drops_it_from_the_bundle(advanced, tmp_path):
    face = tmp_path / "MyFace-Regular.woff2"
    face.write_bytes(b"x")
    win = advanced()
    win._add_font_row(face)
    win._remove_font(face, win._font_rows[face])
    assert win._font_files == []
    assert win._font_rows == {}


# ── The colour row ────────────────────────────────────────────────────────────

def test_a_valid_hex_reaches_the_callback(gtk):
    seen = []
    row = _ColourRow("Background", "#ffffff", seen.append)
    row._entry.set_text("#123456")
    assert seen == ["#123456"]
    assert _rgba_to_hex(row._picker.get_rgba()) == "#123456"


def test_a_half_typed_hex_is_not_reported(gtk):
    # Typing "#12345" on the way to "#123456" must not push a broken colour
    # into the theme on every keystroke.
    seen = []
    row = _ColourRow("Background", "#ffffff", seen.append)
    for text in ("#", "#1", "#12", "#1234", "#12345", "nonsense"):
        row._entry.set_text(text)
    assert seen == []
    row._entry.set_text("#abc")               # three-digit is valid
    assert seen == ["#abc"]


def test_setting_a_colour_from_code_does_not_echo_back(gtk):
    # set_colour() is how a preset drives the row.  If it re-entered the
    # callback the preset would be re-applied colour by colour.
    seen = []
    row = _ColourRow("Accent", "#000000", seen.append)
    row.set_colour("#ff8800")
    assert seen == []
    assert row._entry.get_text() == "#ff8800"
    assert _rgba_to_hex(row._picker.get_rgba()) == "#ff8800"


def test_a_nullable_row_reports_empty_and_shows_it(gtk):
    seen = []
    row = _ColourRow("Accent 2", "#123456", seen.append, nullable=True)
    row._entry.set_text("")
    assert seen == [""]
    assert "colour-row-empty-swatch" in row._picker.get_css_classes()


def test_a_required_row_ignores_being_emptied(gtk):
    seen = []
    row = _ColourRow("Background", "#123456", seen.append, nullable=False)
    row._entry.set_text("")
    assert seen == []
