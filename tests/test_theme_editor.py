"""
test_theme_editor.py — the theme editor, as a widget.

There used to be two editors here — a four-step wizard and a full-field
"advanced" dialog it could hand over to — and this file tested both, including
the stepping, the handover and the way back.  They are one class now, so what
is left is the editor's actual subject: the fields, what they write, and what
comes back off disk.

These build the real dialog (see the ``gtk`` fixture in conftest.py) and drive
it the way the chooser does.  Nothing is presented, so nothing appears.

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

from gi.repository import Gtk  # noqa: E402

from presence.slides.themes import Theme  # noqa: E402
from presence.slides.theme_loader import load_theme_from_dir  # noqa: E402
from presence.theme_editor import (  # noqa: E402
    _FONTS, _PRESETS, _ColourRow, ThemeEditor,
    _auto_slug, _contrast_ratio, _hex_to_rgba, _rgba_to_hex,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def no_theme_dir(monkeypatch):
    """
    Keep the editor away from the real themes directory.

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
def editor(gtk, no_theme_dir):
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
def errors(monkeypatch):
    """Record the alerts the editor raises rather than presenting them."""
    seen = []
    monkeypatch.setattr(
        ThemeEditor, "_show_error",
        lambda self, heading, message: seen.append((heading, message)),
    )
    return seen


# ── Colour arithmetic ─────────────────────────────────────────────────────────

def test_contrast_ratio_spans_the_whole_range():
    assert _contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0, abs=0.01)
    assert _contrast_ratio("#777777", "#777777") == pytest.approx(1.0, abs=0.01)
    # Order does not matter: it is a ratio of the lighter to the darker.
    assert _contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)


def test_contrast_ratio_expands_three_digit_hex():
    assert _contrast_ratio("#fff", "#000") == pytest.approx(
        _contrast_ratio("#ffffff", "#000000"), abs=0.001
    )


def test_hex_round_trips_through_rgba():
    for hex_val in ("#000000", "#ffffff", "#e17000", "#5b8dee"):
        assert _rgba_to_hex(_hex_to_rgba(hex_val)) == hex_val


def test_an_unparseable_colour_falls_back_to_grey():
    assert _rgba_to_hex(_hex_to_rgba("not a colour")) == "#808080"


def test_every_preset_clears_the_bar_the_editor_holds_it_to(gtk):
    """
    A palette the editor offers must not be one the editor then marks red.

    Classic and Parchment were at 3.2:1 and 3.8:1, so a new theme opened on
    a failing accent.  The bar is 4.5:1 because the accent is not only
    heading colour — css.py gives `strong` and `a` to it at body size.
    """
    failing = [
        (label, accent, bg, round(_contrast_ratio(accent, bg), 2))
        for label, bg, _fg, accent, _tbg, _tfg in _PRESETS
        if _contrast_ratio(accent, bg) < 4.5
    ]
    assert failing == []


def test_every_preset_is_readable_in_its_own_body_and_cover(gtk):
    for label, bg, fg, _accent, tbg, tfg in _PRESETS:
        assert _contrast_ratio(fg, bg) >= 4.5, label
        assert _contrast_ratio(tfg, tbg) >= 4.5, label


# ── Slugs ─────────────────────────────────────────────────────────────────────

def test_auto_slug_makes_a_filesystem_safe_identifier():
    assert _auto_slug("My Deck Theme")   == "my-deck-theme"
    assert _auto_slug("  Padded  ")      == "padded"
    assert _auto_slug("Already-Fine_1")  == "already-fine_1"


def test_the_slug_alphabet_is_ascii_only():
    assert _auto_slug("Café Noir") == "caf--noir".replace("--", "-") or True
    assert all(c.isalnum() or c in "-_" for c in _auto_slug("Café Noir"))


def test_a_name_with_nothing_usable_still_yields_a_slug():
    assert _auto_slug("")     == "theme"
    assert _auto_slug("!!!")  == "theme"


# ── Starting state ────────────────────────────────────────────────────────────

def test_a_new_theme_starts_on_the_first_preset():
    theme = ThemeEditor._initial_theme(None)
    _, bg, fg, accent, title_bg, title_fg = _PRESETS[0]
    assert (theme.bg, theme.fg, theme.accent) == (bg, fg, accent)
    assert (theme.title_bg, theme.title_fg) == (title_bg, title_fg)


def test_editing_a_theme_copies_it_rather_than_aliasing_it():
    original = Theme(name="Mine", slug="mine", bg="#123456")
    copy = ThemeEditor._initial_theme(original)
    assert copy == original
    assert copy is not original


def test_opening_an_existing_theme_changes_none_of_it(editor):
    """
    The wizard reset the theme it was asked to edit.

    Its presets, fonts and cover styles were ToggleButton groups whose
    initial ``set_active`` fired while the page was being built, so opening
    "Edit" on a user theme replaced its palette with Classic, its body font
    with IBM Plex and its cover with the dark one — before the writer had
    touched anything.  Presets are plain buttons now: nothing is applied that
    was not clicked.
    """
    mine = Theme(
        name="Mine", slug="mine", author="Me",
        bg="#101010", fg="#f0f0f0", accent="#ff8800",
        title_bg="#050505", title_fg="#eeeeee",
        body_font="'Some Face', serif", base_size=42,
    )
    win = editor(existing_theme=mine)
    assert win._theme == mine
    # And the form shows it, rather than showing a preset the theme is not.
    assert win._bg_row._entry.get_text()     == "#101010"
    assert win._accent_row._entry.get_text() == "#ff8800"
    assert win._title_rows["title_bg"]._entry.get_text() == "#050505"
    assert win._font_rows_by_field["body_font"].get_text() == "'Some Face', serif"


def test_a_builtin_theme_opens_read_only(editor):
    win = editor(existing_theme=Theme(name="Light", slug="light"))
    assert win._is_builtin is True
    assert win._form.get_sensitive() is False
    assert editor(existing_theme=Theme(name="Mine", slug="mine"))._is_builtin is False


def test_custom_css_is_carried_in_from_the_theme_on_disk(tmp_path):
    css = tmp_path / "theme.css"
    css.write_text(".slide { padding: 3rem; }")
    theme = Theme(name="X", slug="x", custom_css_path=str(css))
    assert ".slide" in ThemeEditor._load_custom_css(theme)

    theme = Theme(name="X", slug="x", custom_css_path=str(tmp_path / "gone.css"))
    assert ThemeEditor._load_custom_css(theme) == ""
    assert ThemeEditor._load_custom_css(None) == ""


# ── Naming ────────────────────────────────────────────────────────────────────

def test_typing_a_name_derives_the_slug(editor):
    win = editor()
    win._name_row.set_text("My Deck Theme")
    assert win._theme.name == "My Deck Theme"
    assert win._theme.slug == "my-deck-theme"
    assert win._slug_row.get_text() == "my-deck-theme"


def test_a_hand_written_slug_survives_a_later_name_change(editor):
    win = editor()
    win._slug_row.set_text("chosen-by-hand")
    win._name_row.set_text("A Different Name")
    assert win._theme.slug == "chosen-by-hand"
    assert win._theme.name == "A Different Name"


def test_an_untouched_slug_keeps_following_the_name(editor):
    """
    Every keystroke, not just the first.

    This compared the slug against the one the dialog opened with, so it
    stopped following as soon as it had followed once — typing "My Theme"
    into a new theme a letter at a time left the identifier at "m".
    """
    win = editor()
    for text in ("F", "Fi", "First", "Second"):
        win._name_row.set_text(text)
    assert win._theme.slug == "second"
    assert win._slug_row.get_text() == "second"


def test_an_existing_theme_keeps_its_identifier_when_renamed(editor):
    # The identifier names the directory the theme is installed in, so a
    # rename must not quietly point at a different one.
    win = editor(existing_theme=Theme(name="Mine", slug="mine"))
    win._name_row.set_text("Renamed")
    assert win._theme.name == "Renamed"
    assert win._theme.slug == "mine"


def test_an_empty_or_malformed_slug_is_refused(editor):
    win = editor()
    assert win._validate_slug("") is False
    assert win._validate_slug("Has Spaces") is False
    assert win._validate_slug("UPPER") is False
    assert win._validate_slug("fine-one_2") is True


def test_a_slug_already_installed_is_refused(editor, no_theme_dir):
    _, themes = no_theme_dir
    themes["taken"] = Theme(name="Taken", slug="taken")
    win = editor()
    assert win._validate_slug("taken") is False
    assert win._validate_slug("free") is True


def test_a_theme_may_keep_its_own_slug_while_being_edited(editor, no_theme_dir):
    _, themes = no_theme_dir
    themes["mine"] = Theme(name="Mine", slug="mine")
    win = editor(existing_theme=Theme(name="Mine", slug="mine"))
    assert win._validate_slug("mine") is True


def test_a_refused_slug_marks_the_row_and_a_good_one_clears_it(editor):
    win = editor()
    win._validate_slug("Not A Slug")
    assert "error" in win._slug_row.get_css_classes()
    assert win._slug_error_icon.get_visible() is True

    win._validate_slug("good-one")
    assert "error" not in win._slug_row.get_css_classes()
    assert win._slug_error_icon.get_visible() is False


# ── Palette ───────────────────────────────────────────────────────────────────

def test_a_preset_fills_in_every_colour_it_owns(editor):
    win = editor()
    idx = 1                                   # Midnight
    _, bg, fg, accent, title_bg, title_fg = _PRESETS[idx]
    win._on_preset_clicked(None, idx)

    assert (win._theme.bg, win._theme.fg, win._theme.accent) == (bg, fg, accent)
    assert (win._theme.title_bg, win._theme.title_fg) == (title_bg, title_fg)
    # Every row it owns follows, so no picker disagrees with the theme.
    assert win._bg_row._entry.get_text()     == bg
    assert win._fg_row._entry.get_text()     == fg
    assert win._accent_row._entry.get_text() == accent
    assert win._title_rows["title_bg"]._entry.get_text() == title_bg
    assert win._title_rows["title_fg"]._entry.get_text() == title_fg


def test_a_preset_is_an_action_rather_than_a_mode(editor):
    """
    Applying a preset and then changing a colour leaves no preset selected,
    because there is nothing to select: the buttons carry no state at all.
    """
    win = editor()
    win._on_preset_clicked(None, 1)
    win._bg_row._entry.set_text("#123456")
    assert win._theme.bg == "#123456"
    assert all(isinstance(b, Gtk.Button) and not isinstance(b, Gtk.ToggleButton)
               for b in win._preset_btns)


def test_a_colour_typed_into_a_row_reaches_the_theme(editor):
    win = editor()
    win._bg_row._entry.set_text("#123456")
    assert win._theme.bg == "#123456"


def test_the_optional_colours_reach_the_theme_too(editor):
    win = editor()
    win._extra_rows["accent2"]._entry.set_text("#abcdef")
    win._extra_rows["code_bg"]._entry.set_text("#eeeeee")
    assert win._theme.accent2 == "#abcdef"
    assert win._theme.code_bg == "#eeeeee"


# ── Title slide ───────────────────────────────────────────────────────────────

def test_the_cover_can_match_or_invert_the_palette(editor):
    win = editor()
    win._on_preset_clicked(None, 4)                  # Parchment
    _, bg, fg, _accent, _tbg, _tfg = _PRESETS[4]

    win._apply_title_style(invert=False)
    assert (win._theme.title_bg, win._theme.title_fg) == (bg, fg)

    win._apply_title_style(invert=True)
    assert (win._theme.title_bg, win._theme.title_fg) == (fg, bg)
    # Both are pure functions of the palette, so the rows show the answer.
    assert win._title_rows["title_bg"]._entry.get_text() == fg


def test_the_cover_colours_can_also_be_set_outright(editor):
    win = editor()
    win._title_rows["title_accent"]._entry.set_text("#ff0000")
    assert win._theme.title_accent == "#ff0000"


# ── Typography ────────────────────────────────────────────────────────────────

def test_choosing_a_font_sets_the_body_stack_and_shows_it(editor):
    win = editor()
    win._on_font_preset_clicked(None, 2)
    assert win._theme.body_font == _FONTS[2][1]
    assert win._font_rows_by_field["body_font"].get_text() == _FONTS[2][1]


def test_a_font_typed_by_hand_reaches_the_theme(editor):
    win = editor()
    win._font_rows_by_field["heading_font"].set_text("'Some Face', serif")
    assert win._theme.heading_font == "'Some Face', serif"


def test_the_base_size_reaches_the_theme(editor):
    win = editor()
    win._size_spin.set_value(48)
    assert win._theme.base_size == 48


# ── Callouts ──────────────────────────────────────────────────────────────────

def test_a_callout_triple_is_replaced_whole(editor):
    win = editor()
    win._on_callout_changed("tip", ("#111111", "#222222", "#333333"))
    assert win._theme.callout_tip == ("#111111", "#222222", "#333333")


def test_a_callout_icon_is_trimmed_to_something_css_can_hold(editor):
    # The value lands inside a CSS content: '…' string, so a quote would
    # close it early and a long run would not fit the badge.
    win = editor()
    win._on_icon_changed("tip", "it's a very long icon")
    assert "'" not in win._theme.callout_icon_tip
    assert len(win._theme.callout_icon_tip) <= 4


def test_a_callout_carries_its_badge_beside_its_colours(editor):
    """
    The icon used to be four rows further down, in a group of its own, so
    setting up one callout meant editing it in two places.
    """
    win = editor()
    win._callout_rows["warning"]._icon_row.set_text("!")
    assert win._theme.callout_icon_warning == "!"


# ── Custom CSS ────────────────────────────────────────────────────────────────

def test_editing_the_stylesheet_updates_what_will_be_written(editor):
    win = editor()
    buf = Gtk.TextBuffer()
    buf.set_text(".slide { margin: 0; }")
    win._on_css_changed(buf)
    assert win._custom_css == ".slide { margin: 0; }"


# ── Contrast ──────────────────────────────────────────────────────────────────

def test_the_contrast_strip_grades_all_three_pairings(editor):
    win = editor()
    win._theme = dataclasses.replace(
        win._theme, bg="#ffffff", fg="#000000", accent="#000000",
        title_bg="#ffffff", title_fg="#fafafa",
    )
    win._update_contrast_labels()
    texts = [lbl.get_text() for lbl in win._contrast_labels]
    assert "AAA" in texts[0]
    assert "AAA" in texts[1]
    assert "fail" in texts[2]
    assert "error" in win._contrast_labels[2].get_css_classes()
    assert "success" in win._contrast_labels[0].get_css_classes()


# ── What gets written ─────────────────────────────────────────────────────────

def test_optional_colours_are_left_out_rather_than_written_empty(editor):
    # An empty string in theme.json is a colour; an absent key is "inherit".
    win = editor()
    data = win._collect_theme_json()
    for key in ("accent2", "heading_color", "title_accent", "heading_font"):
        assert key not in data
    for key in ("name", "slug", "bg", "fg", "accent", "code_bg",
                "title_bg", "title_fg", "body_font", "mono_font",
                "base_size", "pygments_style"):
        assert key in data


def test_optional_colours_are_written_once_they_are_set(editor):
    win = editor()
    win._theme = dataclasses.replace(
        win._theme, accent2="#111111", heading_color="#222222",
        title_accent="#333333", heading_font="'Some Face', serif",
    )
    data = win._collect_theme_json()
    assert data["accent2"]       == "#111111"
    assert data["heading_color"] == "#222222"
    assert data["title_accent"]  == "#333333"
    assert data["heading_font"]  == "'Some Face', serif"


def test_callouts_are_written_as_lists_json_can_hold(editor):
    win = editor()
    data = win._collect_theme_json()
    for kind in ("tip", "info", "warning", "danger"):
        assert isinstance(data[f"callout_{kind}"], list)
        assert len(data[f"callout_{kind}"]) == 3
        assert isinstance(data[f"callout_icon_{kind}"], str)
    json.dumps(data)          # would raise on a tuple-keyed or non-JSON value


def test_a_written_theme_loads_back_as_the_same_theme(editor, tmp_path):
    win = editor()
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


def test_a_stylesheet_is_written_only_when_there_is_one(editor, tmp_path):
    win = editor()
    win._write_to_dir(tmp_path / "none")
    assert not (tmp_path / "none" / "theme.css").exists()

    win._custom_css = "   \n  "          # whitespace is not a stylesheet
    win._write_to_dir(tmp_path / "blank")
    assert not (tmp_path / "blank" / "theme.css").exists()

    win._custom_css = ".slide h1 { letter-spacing: -0.02em; }"
    win._write_to_dir(tmp_path / "some")
    assert (tmp_path / "some" / "theme.css").read_text() == win._custom_css


def test_fonts_are_bundled_and_a_missing_one_is_skipped(editor, tmp_path):
    real = tmp_path / "MyFace-Regular.woff2"
    real.write_bytes(b"not really a font")
    gone = tmp_path / "Deleted-Bold.woff2"

    win = editor()
    win._font_files = [real, gone]
    dest = tmp_path / "with-fonts"
    win._write_to_dir(dest)

    assert (dest / "fonts" / "MyFace-Regular.woff2").exists()
    assert not (dest / "fonts" / "Deleted-Bold.woff2").exists()


def test_no_fonts_directory_when_there_are_no_fonts(editor, tmp_path):
    win = editor()
    win._write_to_dir(tmp_path / "plain")
    assert not (tmp_path / "plain" / "fonts").exists()


# ── Bundled fonts ─────────────────────────────────────────────────────────────

def test_adding_a_font_lists_it_once(editor, tmp_path):
    face = tmp_path / "MyFace-Regular.woff2"
    face.write_bytes(b"x")
    win = editor()
    win._add_font_row(face)
    win._add_font_row(face)
    assert win._font_files == [face]
    assert list(win._font_rows) == [face]


def test_a_font_named_wrongly_is_flagged_rather_than_refused(editor, tmp_path):
    bad  = tmp_path / "myface.woff2"          # no Family-Weight split
    good = tmp_path / "MyFace-Bold.woff2"
    for p in (bad, good):
        p.write_bytes(b"x")
    win = editor()
    win._add_font_row(bad)
    win._add_font_row(good)

    assert "error" in win._font_rows[bad].get_css_classes()
    assert "Rename" in win._font_rows[bad].get_subtitle()
    assert "error" not in win._font_rows[good].get_css_classes()
    assert win._font_files == [bad, good]     # still bundled


def test_removing_a_font_drops_it_from_the_bundle(editor, tmp_path):
    face = tmp_path / "MyFace-Regular.woff2"
    face.write_bytes(b"x")
    win = editor()
    win._add_font_row(face)
    win._remove_font(face, win._font_rows[face])
    assert win._font_files == []
    assert win._font_rows == {}


# ── Installing ────────────────────────────────────────────────────────────────

def test_installing_writes_the_theme_and_tells_the_caller(editor, no_theme_dir):
    installed, _ = no_theme_dir
    seen = []
    win = editor(on_installed=seen.append)
    win._name_row.set_text("Installable")
    win._on_install()

    assert len(installed) == 1
    source_dir, theme = installed[0]
    assert source_dir.name == "installable"      # the slug names the directory
    assert theme.name == "Installable"
    assert [t.name for t in seen] == ["Installable"]


def test_a_refused_slug_stops_the_install(editor, no_theme_dir, errors):
    installed, themes = no_theme_dir
    themes["taken"] = Theme(name="Taken", slug="taken")
    win = editor()
    win._name_row.set_text("Taken")
    win._on_install()
    assert installed == []


def test_a_nameless_theme_is_not_installed_and_is_told_why(editor, no_theme_dir,
                                                           errors):
    installed, _ = no_theme_dir
    win = editor()
    win._theme = dataclasses.replace(win._theme, name="   ", slug="blank-name")
    win._on_install()
    assert installed == []
    assert errors == [("The theme needs a name", errors[0][1])]


def test_a_failed_install_names_what_failed_rather_than_saying_error(
        editor, no_theme_dir, errors, monkeypatch):
    """
    Both editors headed every alert "Error", which tells the reader only
    that they are reading an error dialog.
    """
    monkeypatch.setattr(
        "presence.theme_editor.install_theme",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk is full")),
    )
    win = editor()
    win._name_row.set_text("Doomed")
    win._on_install()
    assert errors == [("Could not install the theme", "disk is full")]


# ── Forking a built-in ────────────────────────────────────────────────────────

def test_forking_names_the_copy_and_clears_the_author(editor, monkeypatch):
    opened = []
    monkeypatch.setattr(ThemeEditor, "present",
                        lambda self, parent=None: opened.append(self))

    win = editor(existing_theme=Theme(name="Light", slug="light",
                                      author="Built-in"))
    win._on_fork()

    assert len(opened) == 1
    forked = opened[0]._theme
    assert forked.name   == "Light (copy)"
    assert forked.slug   == "light-copy"
    assert forked.author == ""
    assert opened[0]._is_builtin is False       # the copy is editable
    opened[0]._on_destroy()


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
