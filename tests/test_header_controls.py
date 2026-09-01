"""
test_header_controls.py — the header bar's controls and the keys that reach
the same verbs.

Two things are pinned here.

**The keys GNOME reserves belong to the verbs GNOME reserves them for.**
Ctrl+P was Present, so the nearest thing this app has to Print — Export PDF,
which writes exactly the file Print-to-file would — sat on Ctrl+Shift+E
alone, and pressing the system's Print key started a slideshow.  F1 was the
shortcuts window rather than help, and Settings had no key at all.  A
shortcut reference that lists these is not enough on its own: it would
happily document the wrong binding, which is why this asserts the action
each key actually reaches.

**The Export popover is a boxed list.**  Its rows were hand-built out of flat
buttons carrying an icon and two stacked labels — the shape of an
Adw.ActionRow, under a comment claiming it was the HIG pattern — so they had
none of the row styling, activation behaviour or accessible role the real
thing brings.
"""
import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from presence.window import MainWindow  # noqa: E402
from presence.export_controller import ExportController  # noqa: E402


@pytest.fixture
def window(app_factory):
    """A registered application with one window, so accels read back."""
    app = app_factory()
    win = MainWindow(application=app)
    yield app, win
    win.destroy()


def _action_for(app, accel: str) -> str | None:
    return (app.get_actions_for_accel(accel) or [None])[0]


def _normalise(accel: str) -> str:
    """GTK hands accels back in its own spelling: `<primary>p` → `<Control>p`."""
    _ok, key, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_name(key, mods)


def _keys_for(app, action: str) -> set[str]:
    return {_normalise(a) for a in app.get_accels_for_action(action)}


# ── Where the reserved keys go ────────────────────────────────────────────────

def test_ctrl_p_writes_a_pdf_rather_than_starting_the_slideshow(window):
    app, _win = window
    assert _action_for(app, "<primary>p") == "win.export"


def test_the_shortcut_this_app_taught_still_works(window):
    app, _win = window
    assert _normalise("<primary><shift>e") in _keys_for(app, "win.export")


def test_present_is_on_the_key_every_other_deck_tool_uses(window):
    app, _win = window
    assert _action_for(app, "F5") == "win.presenter"


def test_the_shortcuts_reference_is_on_the_key_the_hig_gives_it(window):
    app, _win = window
    assert _action_for(app, "<primary>question") == "win.shortcuts"
    # F1 stays bound only because Presence ships no help manual to open.
    assert _normalise("F1") in _keys_for(app, "win.shortcuts")


def test_settings_has_the_key_every_gnome_app_has(window):
    app, _win = window
    assert _action_for(app, "<primary>comma") == "app.preferences"


def test_no_key_drives_two_verbs(window):
    app, win = window
    seen: dict[str, str] = {}
    for prefix, obj in (("win", win), ("app", app)):
        for name in obj.list_actions():
            for accel in app.get_accels_for_action(f"{prefix}.{name}"):
                assert accel not in seen, (
                    f"{accel} is bound to both {seen.get(accel)} "
                    f"and {prefix}.{name}"
                )
                seen[accel] = f"{prefix}.{name}"


# ── The Export popover ────────────────────────────────────────────────────────

def _rows(popover):
    listbox = popover.get_child()
    assert isinstance(listbox, Gtk.ListBox)
    out, child = [], listbox.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


def test_the_export_popover_offers_the_four_formats(window):
    _app, win = window
    rows = _rows(win._share_btn.get_popover())
    assert [r.get_title() for r in rows] == [
        "PDF…", "HTML…", "Images…", "Handout…"
    ]
    assert all(r.get_subtitle() for r in rows)


def test_the_export_rows_are_action_rows_in_a_boxed_list(window):
    _app, win = window
    popover = win._share_btn.get_popover()
    assert "boxed-list" in popover.get_child().get_css_classes()
    for row in _rows(popover):
        assert isinstance(row, Adw.ActionRow)
        assert row.get_activatable() is True


def test_activating_a_row_runs_its_export_and_closes_the_popover(window,
                                                                 monkeypatch):
    _app, win = window
    called = []
    # Patched on the controller that owns the export: the row is bound
    # straight to it now, with no window method in between.
    monkeypatch.setattr(ExportController, "export_handout",
                        lambda self, *a: called.append("handout"))
    # The popover is rebuilt so the row closes over the patched method.
    popover = win._build_share_popover()
    down = []
    monkeypatch.setattr(Gtk.Popover, "popdown", lambda self: down.append(self))

    _rows(popover)[3].emit("activated")
    assert called == ["handout"]
    assert down == [popover]


# ── What the window calls itself ──────────────────────────────────────────────

def test_the_window_title_is_the_document_and_not_the_app(window):
    """
    The shell already says which application a window belongs to, so
    "Untitled — Presence" said it twice.  The header widget only ever showed
    the document's name; the title bar and the task switcher now agree with
    it.
    """
    _app, win = window
    assert win.get_title() == "Untitled"

    win.set_document_title("barns.md")
    assert win.get_title() == "barns.md"
    assert win._title_label.get_title() == "barns.md"


def test_the_menu_names_the_app_in_its_about_item(window):
    """
    "About" alone is the one place the app's name belongs, and the only
    header-menu item the HIG spells out.
    """
    _app, win = window
    menu = win._build_app_menu()
    labels = []
    for section in range(menu.get_n_items()):
        link = menu.get_item_link(section, "section")
        if link is None:
            continue
        for i in range(link.get_n_items()):
            label = link.get_item_attribute_value(i, "label", None)
            if label is not None:
                labels.append(label.get_string())
    assert "About Presence" in labels
