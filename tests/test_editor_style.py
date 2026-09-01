"""
test_editor_style.py — the editor follows the system's light/dark style.

The editor shipped one colour scheme for a long time, pinned to a white
background, and nothing anywhere read ``Adw.StyleManager`` to choose it.  So a
writer in GNOME's dark style got a dark header bar, a dark thumbnail strip and
a dark inspector wrapped around a white sheet — the largest surface in the
window — with no preference to change it.

What is pinned here is that the scheme tracks the system style in *both*
directions, that the colours the buffer sets itself move with it, and that
neither scheme pins the selection colour, because that one is the user's
accent and not the app's to choose.
"""
import pytest

pytest.importorskip("gi")

from presence.editor import (  # noqa: E402
    _SCHEMES_DARK, _SCHEMES_LIGHT, _TAG_COLOURS,
)


@pytest.fixture
def style(gtk):
    """
    Drive Adw.StyleManager, and hand the system style back afterwards.

    The manager is a process-wide singleton, so a test that forced dark and
    walked away would hand the next one a dark desktop it never asked for.
    """
    from gi.repository import Adw

    manager = Adw.StyleManager.get_default()
    before = manager.get_color_scheme()

    class Style:
        def light(self) -> None:
            manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)

        def dark(self) -> None:
            manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

    yield Style()
    manager.set_color_scheme(before)


@pytest.fixture
def editor(gtk):
    """A real Editor.  Never presented — it is not even in a window."""
    from presence.editor import Editor

    made = []

    def build():
        ed = Editor()
        made.append(ed)
        return ed

    yield build
    for ed in made:
        ed.run_dispose()


def _scheme_id(editor) -> str:
    return editor._buffer.get_style_scheme().get_id()


def _tag_colour(editor, name: str) -> str:
    tag = editor._buffer.get_tag_table().lookup(name)
    return tag.get_property("foreground-rgba").to_string()


# ── Which scheme the editor opens in ──────────────────────────────────────────

def test_a_dark_desktop_opens_a_dark_editor(style, editor):
    style.dark()
    assert _scheme_id(editor()) == _SCHEMES_DARK[0]


def test_a_light_desktop_opens_a_light_editor(style, editor):
    style.light()
    assert _scheme_id(editor()) == _SCHEMES_LIGHT[0]


# ── And that it keeps following ───────────────────────────────────────────────

def test_the_editor_follows_the_system_both_ways(style, editor):
    style.light()
    ed = editor()
    assert _scheme_id(ed) == _SCHEMES_LIGHT[0]

    style.dark()
    assert _scheme_id(ed) == _SCHEMES_DARK[0]

    style.light()
    assert _scheme_id(ed) == _SCHEMES_LIGHT[0]


def test_the_tags_the_buffer_colours_itself_follow_too(style, editor):
    """
    The scheme cannot reach these — they are GtkTextBuffer tags, not language
    rules — so a scheme swap that left them behind would put the separator
    marks, the image tag and focus mode's wash at light-mode greys on a dark
    sheet.
    """
    style.light()
    ed = editor()
    light = {name: _tag_colour(ed, name) for name in _TAG_COLOURS}

    style.dark()
    dark = {name: _tag_colour(ed, name) for name in _TAG_COLOURS}

    assert set(light) == set(_TAG_COLOURS)
    for name in _TAG_COLOURS:
        assert light[name] != dark[name], f"{name} did not follow the style"


# ── What the schemes may and may not decide ───────────────────────────────────

def test_the_bundled_schemes_are_installed(gtk, editor):
    """
    Building an editor puts the package directory on the manager's search
    path.  Without it a pip install found neither scheme — meson's copy lands
    somewhere already searched, a pip install's does not — and the editor fell
    back to plain Adwaita with none of Presence's own Markdown colours.
    """
    from gi.repository import GtkSource

    editor()
    manager = GtkSource.StyleSchemeManager.get_default()
    for name in (_SCHEMES_LIGHT[0], _SCHEMES_DARK[0]):
        assert manager.get_scheme(name) is not None, name


def test_neither_scheme_pins_the_selection_colour(gtk, editor):
    """
    Selection and the caret are the user's accent.  The scheme this pair
    replaced set selection to #4a90d9 — GTK 3's blue — so the editor was the
    one place in the app where changing the desktop accent did nothing.
    """
    from gi.repository import GtkSource

    editor()
    manager = GtkSource.StyleSchemeManager.get_default()
    for name in (_SCHEMES_LIGHT[0], _SCHEMES_DARK[0]):
        scheme = manager.get_scheme(name)
        assert scheme.get_style("selection") is None, name
        assert scheme.get_style("cursor") is None, name


# ── And that it lets go ───────────────────────────────────────────────────────

def test_the_editor_lets_go_of_the_style_manager_with_its_window(gtk, editor):
    """
    The manager outlives every window, so a handler left on it would keep the
    editor and its buffer alive for the rest of the session — one per window
    the writer ever closed.
    """
    ed = editor()
    window = gtk.Window()
    window.set_child(ed)
    assert ed._dark_handler is not None

    window.set_child(None)
    assert ed._dark_handler is None
    window.destroy()
