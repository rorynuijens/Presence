"""
test_shortcuts.py — the keyboard shortcut reference, checked against reality.

A shortcuts window is documentation that lives in the binary, so the failure
mode is drift: a shortcut gets bound and the reference never hears about it.
The test that matters here is therefore not "the window builds" but "every
accelerator the app actually binds appears in it".

Writing it found one: Ctrl+Q was bound by ``Application._setup_actions`` and
listed nowhere.
"""
import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk  # noqa: E402

from presence.shortcuts import build_shortcuts_window  # noqa: E402
from presence.window import MainWindow  # noqa: E402


def _normalise(accel: str) -> str | None:
    """`<primary>n` and `<Primary>n` are the same key; compare them as one."""
    ok, key, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_name(key, mods) if ok else None


def _documented(window) -> set[str]:
    """Every accelerator the shortcuts window lists."""
    found = set()

    def walk(widget):
        if type(widget).__name__ == "ShortcutsShortcut":
            accel = widget.get_property("accelerator")
            if accel:
                found.add(_normalise(accel))
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(window)
    return found


@pytest.fixture
def app_and_window(app_factory):
    """A registered application with one window, so accels can be read back."""
    app = app_factory()
    return app, MainWindow(application=app)


def _bound(app, win) -> dict[str, str]:
    """Every accelerator the app binds to an action, by action name."""
    out = {}
    for prefix, obj in (("win", win), ("app", app)):
        for name in obj.list_actions():
            for accel in app.get_accels_for_action(f"{prefix}.{name}"):
                out[_normalise(accel)] = f"{prefix}.{name}"
    return out


# ── The window itself ─────────────────────────────────────────────────────────

def test_the_reference_builds_against_its_parent(gtk):
    parent = gtk.Window()
    win = build_shortcuts_window(parent)
    assert win.get_transient_for() is parent
    assert win.get_modal() is True
    parent.destroy()


def test_every_listed_accelerator_is_a_key_gtk_can_parse(gtk):
    parent = gtk.Window()
    win = build_shortcuts_window(parent)
    assert None not in _documented(win)
    parent.destroy()


# ── Against what the app actually binds ───────────────────────────────────────

def test_every_bound_shortcut_is_documented(app_and_window):
    app, win = app_and_window
    documented = _documented(build_shortcuts_window(win))
    undocumented = {accel: action
                    for accel, action in _bound(app, win).items()
                    if accel not in documented}
    assert undocumented == {}


def test_the_reference_covers_more_than_the_actions(app_and_window):
    # Presenter navigation — Escape, the arrows, space — is read by a key
    # controller rather than bound to an action, so it can only appear here.
    app, win = app_and_window
    documented = _documented(build_shortcuts_window(win))
    for key in ("Escape", "Left", "Right", "space"):
        assert _normalise(key) in documented
