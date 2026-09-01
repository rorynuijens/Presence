"""
test_theme_manager_ui.py — the Manage-themes page and its thumbnail cache.

This page manages theme *packages* — install, uninstall, open the folder.
Choosing a theme is the inspector's job, beside the document whose
frontmatter it writes, so what is checked here is that the page never grows a
"use this theme" affordance of its own, and that every path that changes the
installed set puts the list and the inspector back in step.

The real ``install_theme``/``uninstall_theme`` and the WeasyPrint thumbnail
render are stubbed; both are covered where they live.
"""
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

import presence.theme_manager_ui as tmu  # noqa: E402
from presence.slides.themes import Theme  # noqa: E402


BUILTIN = Theme(name="Light", slug="light", author="Built-in", version="1.0")
MINE    = Theme(name="My Theme", slug="my-theme", author="Me", version="2.0",
                description="For quarterly reviews")


class FakeConverter:
    theme = "light"
    ratio = "16:9"
    logo_path = None


class FakeSettingsDialog:
    def __init__(self):
        self.refreshed = 0

    def refresh_themes(self):
        self.refreshed += 1


@pytest.fixture
def themes_page(gtk, monkeypatch):
    """A built Manage-themes page, with disk and thumbnails stubbed out."""
    thumb_calls = []

    class FakeThumbCache:
        def __init__(self):
            self.invalidated = []

        def get_async(self, theme, ratio, callback):
            thumb_calls.append((theme.slug, ratio))

        def invalidate(self, slug):
            self.invalidated.append(slug)

    cache = FakeThumbCache()
    monkeypatch.setattr(tmu, "_thumb_cache", cache)
    monkeypatch.setattr(tmu, "load_all_themes",
                        lambda *a, **k: {"light": BUILTIN, "my-theme": MINE})
    monkeypatch.setattr(tmu, "BUILTIN_THEMES", {"light": BUILTIN})

    made = []

    def build(settings_dialog=None):
        parent = gtk.Window()
        conv   = FakeConverter()
        page   = tmu.build_themes_page(parent, conv, settings_dialog=settings_dialog)
        made.append(parent)
        page._test_cache  = cache
        page._test_thumbs = thumb_calls
        return page

    yield build
    for parent in made:
        parent.destroy()


def _rows(page):
    return {r.get_title(): r for r in page._themes_group._presence_rows}


def _buttons(row):
    """Every button label anywhere inside an ExpanderRow's added rows."""
    found = []

    def walk(widget):
        if isinstance(widget, Gtk.Button):
            found.append(widget.get_label())
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(row)
    return found


# ── The page ──────────────────────────────────────────────────────────────────

def test_the_page_lists_every_installed_theme(themes_page):
    page = themes_page()
    assert set(_rows(page)) == {"Light", "My Theme"}


def test_a_row_names_its_author_and_version(themes_page):
    page = themes_page()
    assert _rows(page)["My Theme"].get_subtitle() == "Me  ·  v2.0"


def test_a_builtin_theme_offers_no_way_to_remove_it(themes_page):
    page = themes_page()
    labels = _buttons(_rows(page)["Light"])
    assert "Uninstall" not in labels
    assert "Edit…" not in labels


def test_a_user_theme_can_be_edited_and_removed(themes_page):
    page = themes_page()
    labels = _buttons(_rows(page)["My Theme"])
    assert "Uninstall" in labels
    assert "Edit…" in labels


def test_the_page_never_offers_to_apply_a_theme(themes_page):
    # Choosing a theme belongs to the inspector, beside the document whose
    # frontmatter it writes.  A second control here would be the split
    # -controls anti-pattern this page was built to avoid.
    page = themes_page()
    for row in page._themes_group._presence_rows:
        for label in _buttons(row):
            assert "Use" not in (label or "")


def test_every_row_asks_for_its_thumbnail_at_the_deck_ratio(themes_page):
    page = themes_page()
    assert sorted(page._test_thumbs) == [("light", "16:9"), ("my-theme", "16:9")]


def test_repopulating_replaces_the_rows_rather_than_stacking_them(themes_page):
    page = themes_page()
    before = len(page._themes_group._presence_rows)
    tmu._populate_themes(page._themes_group, page._parent_window, page._converter)
    tmu._populate_themes(page._themes_group, page._parent_window, page._converter)
    assert len(page._themes_group._presence_rows) == before
    assert set(_rows(page)) == {"Light", "My Theme"}


# ── After an install ──────────────────────────────────────────────────────────

def test_an_install_refreshes_the_list_and_the_inspector(themes_page):
    sd   = FakeSettingsDialog()
    page = themes_page(settings_dialog=sd)
    tmu._on_installed(MINE, page, page._converter)
    assert sd.refreshed == 1
    assert set(_rows(page)) == {"Light", "My Theme"}


def test_an_install_with_preferences_already_closed_still_works(themes_page):
    page = themes_page(settings_dialog=None)
    assert tmu._on_installed(MINE, page, page._converter) is not None


def test_an_install_toasts_the_window_that_has_an_overlay(gtk, themes_page):
    page = themes_page()
    toasts = []
    page._parent_window._toast_overlay = type(
        "O", (), {"add_toast": lambda self, t: toasts.append(t.get_title())}
    )()
    tmu._on_installed(MINE, page, page._converter)
    assert toasts == ["Theme 'My Theme' installed"]


def test_a_failed_install_says_why(gtk, monkeypatch):
    shown = []
    monkeypatch.setattr(Adw.AlertDialog, "present",
                        lambda self, parent=None: shown.append(self))
    tmu._show_install_error("theme.json is not valid JSON", gtk.Window())
    assert len(shown) == 1
    assert shown[0].get_heading() == "Failed to install theme"
    assert shown[0].get_body() == "theme.json is not valid JSON"


# ── Uninstalling ──────────────────────────────────────────────────────────────

def test_uninstalling_asks_first_and_names_the_theme_not_the_slug(themes_page,
                                                                  monkeypatch):
    shown = []
    monkeypatch.setattr(Adw.AlertDialog, "present",
                        lambda self, parent=None: shown.append(self))
    page = themes_page()

    tmu._on_uninstall("my-theme", page._parent_window,
                      page._themes_group, page._converter)

    assert len(shown) == 1
    assert shown[0].get_heading() == "Uninstall theme?"
    assert "'My Theme'" in shown[0].get_body()
    assert shown[0].get_default_response() == "cancel"


def test_cancelling_removes_nothing(themes_page, monkeypatch):
    shown, removed = [], []
    monkeypatch.setattr(Adw.AlertDialog, "present",
                        lambda self, parent=None: shown.append(self))
    monkeypatch.setattr(tmu, "uninstall_theme", lambda slug: removed.append(slug))
    page = themes_page()

    tmu._on_uninstall("my-theme", page._parent_window,
                      page._themes_group, page._converter)
    shown[0].emit("response", "cancel")

    assert removed == []
    assert page._test_cache.invalidated == []


def test_confirming_removes_the_theme_and_forgets_its_thumbnail(themes_page,
                                                                monkeypatch):
    shown, removed = [], []
    monkeypatch.setattr(Adw.AlertDialog, "present",
                        lambda self, parent=None: shown.append(self))
    monkeypatch.setattr(tmu, "uninstall_theme", lambda slug: removed.append(slug))
    page = themes_page()

    tmu._on_uninstall("my-theme", page._parent_window,
                      page._themes_group, page._converter)
    shown[0].emit("response", "remove")

    assert removed == ["my-theme"]
    # A stale thumbnail would outlive the theme it was rendered from.
    assert page._test_cache.invalidated == ["my-theme"]


def test_a_failed_uninstall_says_why_rather_than_raising(themes_page, monkeypatch):
    shown = []
    monkeypatch.setattr(Adw.AlertDialog, "present",
                        lambda self, parent=None: shown.append(self))

    def boom(slug):
        raise ValueError("Cannot uninstall built-in theme 'light'")

    monkeypatch.setattr(tmu, "uninstall_theme", boom)
    page = themes_page()

    tmu._on_uninstall("light", page._parent_window,
                      page._themes_group, page._converter)
    shown[0].emit("response", "remove")

    assert shown[-1].get_heading() == "Failed to install theme"
    assert "built-in" in shown[-1].get_body()


def test_uninstalling_syncs_an_inspector_the_window_still_holds(themes_page,
                                                                monkeypatch):
    shown = []
    monkeypatch.setattr(Adw.AlertDialog, "present",
                        lambda self, parent=None: shown.append(self))
    monkeypatch.setattr(tmu, "uninstall_theme", lambda slug: None)
    page = themes_page()
    sd = FakeSettingsDialog()
    page._parent_window._settings_dialog_ref = sd

    tmu._on_uninstall("my-theme", page._parent_window,
                      page._themes_group, page._converter)
    shown[0].emit("response", "remove")

    assert sd.refreshed == 1


# ── Opening the folder ────────────────────────────────────────────────────────

def test_opening_the_folder_points_at_the_user_themes_directory(gtk, monkeypatch):
    launched = []

    class FakeLauncher:
        def __init__(self, gfile):
            self._gfile = gfile

        def launch(self, parent, cancellable, callback):
            launched.append(self._gfile.get_path())

    monkeypatch.setattr(Gtk.FileLauncher, "new", staticmethod(FakeLauncher))
    tmu._open_themes_folder(gtk.Window())
    assert launched == [str(tmu.user_themes_dir())]


# ── The thumbnail cache ───────────────────────────────────────────────────────

@pytest.fixture
def cache(gtk, monkeypatch, tmp_path):
    """A real ThumbCache pointed at a temporary directory."""
    # idle_add would otherwise defer every callback to a main loop no test runs.
    monkeypatch.setattr(tmu.GLib, "idle_add", lambda fn, *a: fn(*a))
    c = tmu.ThumbCache()
    c._dir = tmp_path
    return c


def test_a_cached_thumbnail_is_read_rather_than_rendered(cache, tmp_path,
                                                          monkeypatch):
    rendered = []
    monkeypatch.setattr(tmu.ThumbCache, "_render",
                        lambda self, t, r, f, cb: rendered.append(t.slug))
    (tmp_path / "my-theme.png").write_bytes(b"cached png")

    got = []
    cache._load_or_render(MINE, "16:9", tmp_path / "my-theme.png", got.append)

    assert got == [b"cached png"]
    assert rendered == []


def test_a_missing_thumbnail_is_rendered(cache, tmp_path, monkeypatch):
    rendered = []
    monkeypatch.setattr(tmu.ThumbCache, "_render",
                        lambda self, t, r, f, cb: rendered.append(t.slug))

    cache._load_or_render(MINE, "16:9", tmp_path / "absent.png", lambda p: None)
    assert rendered == ["my-theme"]


def test_a_thumbnail_older_than_the_stylesheet_is_re_rendered(cache, tmp_path,
                                                              monkeypatch):
    import os
    rendered = []
    monkeypatch.setattr(tmu.ThumbCache, "_render",
                        lambda self, t, r, f, cb: rendered.append(t.slug))

    css = tmp_path / "theme.css"
    css.write_text(".slide {}")
    thumb = tmp_path / "styled.png"
    thumb.write_bytes(b"stale")
    os.utime(thumb, (1, 1))            # older than the stylesheet

    styled = Theme(name="Styled", slug="styled", custom_css_path=str(css))
    cache._load_or_render(styled, "16:9", thumb, lambda p: None)
    assert rendered == ["styled"]


def test_a_thumbnail_newer_than_the_stylesheet_is_kept(cache, tmp_path,
                                                       monkeypatch):
    import os
    rendered = []
    monkeypatch.setattr(tmu.ThumbCache, "_render",
                        lambda self, t, r, f, cb: rendered.append(t.slug))

    css = tmp_path / "theme.css"
    css.write_text(".slide {}")
    os.utime(css, (1, 1))
    thumb = tmp_path / "styled.png"
    thumb.write_bytes(b"fresh")

    styled = Theme(name="Styled", slug="styled", custom_css_path=str(css))
    got = []
    cache._load_or_render(styled, "16:9", thumb, got.append)
    assert got == [b"fresh"]
    assert rendered == []


def test_invalidating_forgets_a_thumbnail_and_tolerates_a_missing_one(cache,
                                                                      tmp_path):
    (tmp_path / "my-theme.png").write_bytes(b"png")
    cache.invalidate("my-theme")
    assert not (tmp_path / "my-theme.png").exists()
    cache.invalidate("never-existed")        # must not raise
