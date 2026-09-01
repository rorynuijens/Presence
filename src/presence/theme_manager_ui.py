"""
theme_manager_ui.py — Installing, editing and removing theme packages.

This page used to be the third page of Preferences.  Preferences holds
settings; a theme library is content, and the project had already moved
theme *selection* out of here for exactly that reason — leaving the odd
split where you chose a theme in the inspector and made one two dialogs
away, under Settings.  ``build_themes_page()`` is now pushed onto the theme
chooser's own ``Adw.NavigationView``, so every theme action lives beside the
themes.

*refresher* is anything with a ``refresh_themes()`` method — in practice the
``ThemePanel``, whose swatches have to follow an install or an uninstall.
"""

import shutil
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, GdkPixbuf, Gdk, Gio

from .slides.theme_loader import (load_all_themes, install_theme,
                                        uninstall_theme, user_themes_dir)
from .slides.themes import BUILTIN_THEMES, Theme

from .theme_editor import ThemeEditor

# ── Thumbnail cache ───────────────────────────────────────────────────────────

class ThumbCache:
    """
    Renders and caches theme preview thumbnails as PNG files on disk.

    Both cache reads and renders are performed on background threads so the
    main thread is never blocked by disk I/O (fixes #63).
    """

    def __init__(self) -> None:
        from gi.repository import GLib as _GLib
        self._dir = Path(_GLib.get_user_cache_dir()) / "presence" / "theme-thumbs"
        self._dir.mkdir(parents=True, exist_ok=True)

    def get_async(self, theme: Theme, ratio: str, callback) -> None:
        """
        Retrieve (or render) the thumbnail for *theme*.

        *callback(png_bytes: bytes | None)* is called on the main thread.
        All disk I/O — including reading a cached file — runs on a daemon
        thread (fixes #63).
        """
        cache_file = self._dir / f"{theme.slug}.png"
        threading.Thread(
            target=self._load_or_render,
            args=(theme, ratio, cache_file, callback),
            daemon=True,
        ).start()

    def invalidate(self, slug: str) -> None:
        cache_file = self._dir / f"{slug}.png"
        cache_file.unlink(missing_ok=True)

    def _load_or_render(self, theme: Theme, ratio: str,
                        cache_file: Path, callback) -> None:
        # Check cache freshness on the background thread
        if cache_file.exists():
            try:
                if theme.custom_css_path:
                    css_mtime   = Path(theme.custom_css_path).stat().st_mtime
                    cache_mtime = cache_file.stat().st_mtime
                    if cache_mtime >= css_mtime:
                        GLib.idle_add(callback, cache_file.read_bytes())
                        return
                else:
                    GLib.idle_add(callback, cache_file.read_bytes())
                    return
            except OSError:
                pass

        self._render(theme, ratio, cache_file, callback)

    def _render(self, theme: Theme, ratio: str,
                cache_file: Path, callback) -> None:
        try:
            from .slides.thumbnails_render import render_thumbnails
            from .slides.themes import ASPECT_RATIOS
            from .slides.css import build_css
            import weasyprint

            w, h = ASPECT_RATIOS.get(ratio, (1280, 720))
            css  = build_css(theme, w, h, None)
            html = (
                f"<!DOCTYPE html><html><head><meta charset='UTF-8'>"
                f"<style>{css}</style></head><body>"
                f"<div class='slide'>"
                f"<h1>Slide heading</h1>"
                f"<h2>Subtitle or section</h2>"
                f"<ul>"
                f"<li>First bullet point with some text</li>"
                f"<li>Second bullet <strong>bold</strong></li>"
                f"<li>Third bullet <em>italic</em></li>"
                f"</ul>"
                f"</div>"
                f"</body></html>"
            )
            pdf_bytes  = weasyprint.HTML(string=html).write_pdf()
            thumbnails = render_thumbnails(pdf_bytes, 1)
            if thumbnails and thumbnails[0]:
                png = thumbnails[0]
                cache_file.write_bytes(png)
                GLib.idle_add(callback, png)
                return
        except Exception as exc:
            print(f"Theme thumbnail render failed for '{theme.slug}': {exc}")
        GLib.idle_add(callback, None)


_thumb_cache = ThumbCache()


# ── Public builder ────────────────────────────────────────────────────────────

def build_themes_page(parent_window, converter,
                      refresher=None) -> Adw.PreferencesPage:
    """
    Build the "Manage themes" page.

    This page handles installing, uninstalling and locating theme packages.
    Theme *selection* is the job of the chooser that pushes this page — there
    is no "Use this theme" button here, to avoid the split-controls
    anti-pattern.

    *refresher* is told (``refresh_themes()``) whenever the installed set
    changes, so the chooser's swatches follow.
    """
    page = Adw.PreferencesPage()
    page.set_title("Manage themes")
    page.set_icon_name("preferences-desktop-appearance-symbolic")

    # ── Install group ─────────────────────────────────────────────────────────
    install_group = Adw.PreferencesGroup(title="Install a theme")
    install_group.set_description(
        "Themes are folders or .zip files containing a theme.json file"
    )

    new_theme_row = Adw.ActionRow(
        title="Create new theme",
        subtitle="Design a theme with the visual editor",
    )
    new_theme_btn = Gtk.Button(label="New theme…")
    new_theme_btn.add_css_class("flat")
    new_theme_btn.set_valign(Gtk.Align.CENTER)
    new_theme_btn.connect(
        "clicked",
        lambda *_: ThemeEditor(
            parent_window,
            on_installed=lambda t: _on_installed(t, page, converter),
        ).present(parent_window),
    )
    new_theme_row.add_suffix(new_theme_btn)
    install_group.add(new_theme_row)

    install_row = Adw.ActionRow(
        title="Install from file",
        subtitle="Choose a .zip or folder",
    )
    install_btn = Gtk.Button(label="Install…")
    install_btn.add_css_class("flat")
    install_btn.set_valign(Gtk.Align.CENTER)
    install_btn.connect("clicked", lambda *_: _on_install(parent_window,
                                                           page, converter))
    install_row.add_suffix(install_btn)
    install_group.add(install_row)

    folder_row = Adw.ActionRow(
        title="Open themes folder",
        subtitle=str(user_themes_dir()),
    )
    folder_btn = Gtk.Button()
    folder_btn.set_child(
        Gtk.Image.new_from_icon_name("folder-open-symbolic")
    )
    folder_btn.update_property(
        [Gtk.AccessibleProperty.LABEL], ["Open themes folder"]
    )
    folder_btn.add_css_class("flat")
    folder_btn.set_valign(Gtk.Align.CENTER)
    folder_btn.connect("clicked", lambda *_: _open_themes_folder(parent_window))
    folder_row.add_suffix(folder_btn)
    install_group.add(folder_row)

    page.add(install_group)

    # ── Installed themes list ─────────────────────────────────────────────────
    themes_group = Adw.PreferencesGroup(title="Installed themes")
    themes_group.set_description("Built-in themes cannot be removed")
    themes_group._presence_rows = []
    page.add(themes_group)

    _populate_themes(themes_group, parent_window, converter, refresher)

    page._themes_group  = themes_group
    page._parent_window = parent_window
    page._converter     = converter
    page._refresher     = refresher

    return page


# ── Theme list population ─────────────────────────────────────────────────────

def _populate_themes(group: Adw.PreferencesGroup, parent_window,
                     converter, refresher=None) -> None:
    for row in getattr(group, "_presence_rows", []):
        group.remove(row)
    group._presence_rows = []

    all_themes = load_all_themes()
    for slug, theme in sorted(all_themes.items()):
        row = _build_theme_row(theme, parent_window, converter, group,
                               is_builtin=(slug in BUILTIN_THEMES),
                               refresher=refresher)
        group.add(row)
        group._presence_rows.append(row)


def _build_theme_row(theme: Theme, parent_window, converter,
                     group: Adw.PreferencesGroup,
                     is_builtin: bool, refresher=None) -> Adw.ExpanderRow:
    row = Adw.ExpanderRow(
        title=theme.name,
        subtitle=f"{theme.author}  ·  v{theme.version}",
    )

    thumb = Gtk.Picture()
    thumb.set_size_request(213, 120)
    thumb.set_content_fit(Gtk.ContentFit.CONTAIN)
    thumb.set_margin_top(8)
    thumb.set_margin_bottom(8)
    thumb.set_valign(Gtk.Align.CENTER)

    spinner = Gtk.Spinner()
    spinner.start()
    spinner.set_size_request(32, 32)
    spinner.set_valign(Gtk.Align.CENTER)
    spinner.set_halign(Gtk.Align.CENTER)

    stack = Gtk.Stack()
    stack.add_named(spinner, "spinner")
    stack.add_named(thumb,   "thumb")
    stack.set_visible_child_name("spinner")
    stack.set_margin_start(12)
    stack.set_margin_end(12)

    thumb_row = Adw.ActionRow(title="Preview")
    thumb_row.add_suffix(stack)

    def _on_thumb(png_bytes):
        if png_bytes:
            try:
                loader = GdkPixbuf.PixbufLoader.new_with_type("png")
                loader.write(png_bytes)
                loader.close()
                texture = Gdk.Texture.new_for_pixbuf(loader.get_pixbuf())
                thumb.set_paintable(texture)
                stack.set_visible_child_name("thumb")
            except Exception:
                pass
        spinner.stop()
        stack.set_visible_child_name("thumb")

    _thumb_cache.get_async(theme, converter.ratio, _on_thumb)
    row.add_row(thumb_row)

    if theme.description:
        desc_row = Adw.ActionRow(title=theme.description)
        desc_row.set_sensitive(False)
        row.add_row(desc_row)

    # "Use this theme" removed — theme selection lives in the Slides tab to
    # avoid the split-controls anti-pattern.  This page only manages packages.
    if not is_builtin:
        actions_row = Adw.ActionRow()

        edit_btn = Gtk.Button(label="Edit…")
        edit_btn.add_css_class("flat")
        edit_btn.set_valign(Gtk.Align.CENTER)
        edit_btn.connect(
            "clicked",
            lambda *_: ThemeEditor(
                parent_window,
                existing_theme=theme,
                on_installed=lambda t: _after_change(
                    group, parent_window, converter, refresher
                ),
            ).present(parent_window),
        )
        actions_row.add_suffix(edit_btn)

        del_btn = Gtk.Button(label="Uninstall")
        del_btn.add_css_class("destructive-action")
        del_btn.add_css_class("flat")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.connect("clicked", lambda btn, *_: _on_uninstall(
            theme.slug, btn.get_root() or parent_window, group, converter,
            refresher,
        ))
        actions_row.add_suffix(del_btn)
        row.add_row(actions_row)

    return row


# ── Actions ───────────────────────────────────────────────────────────────────

def _on_install(parent_window, page, converter) -> None:
    from .app_utils import make_file_filter, make_filter_store
    dialog = Gtk.FileDialog()
    dialog.set_title("Choose Theme Package")
    dialog.set_filters(make_filter_store(
        make_file_filter("Theme packages (folder or .zip)", "*.zip")
    ))
    # Store on page to prevent GC from collecting the dialog before the
    # async callback fires. Cleared in _on_install_response.
    page._active_file_dialog = dialog
    dialog.open(parent_window, None,
                lambda d, r: _on_install_response(d, r, page, converter))


def _on_install_response(dialog, result, page, converter) -> None:
    page._active_file_dialog = None
    try:
        gfile = dialog.open_finish(result)
    except GLib.Error:
        return

    path_str = gfile.get_path()
    if not path_str:
        return
    source = Path(path_str)

    def _do_install():
        try:
            t = install_theme(source)
            GLib.idle_add(_on_installed, t, page, converter)
        except (ValueError, OSError) as exc:
            # Use the settings dialog as parent if it is still open;
            # fall back to the main window if it has been closed.
            parent = page._parent_window
            GLib.idle_add(_show_error, "Failed to install theme",
                          str(exc), parent)

    threading.Thread(target=_do_install, daemon=True).start()


def _after_change(group, parent_window, converter, refresher) -> None:
    """Put the list and whatever displays themes elsewhere back in step."""
    _populate_themes(group, parent_window, converter, refresher)
    if refresher is not None:
        refresher.refresh_themes()


def _on_installed(theme: Theme, page, converter) -> bool:
    _after_change(page._themes_group, page._parent_window, converter,
                  page._refresher)
    # Show a toast via the window's toast overlay (fixes #2)
    toast = Adw.Toast(title=f"Theme '{theme.name}' installed")
    toast.set_timeout(3)
    w = page._parent_window
    if hasattr(w, "_toast_overlay"):
        w._toast_overlay.add_toast(toast)
    return GLib.SOURCE_REMOVE


def _show_error(heading: str, message: str, parent_window) -> bool:
    """An alert names what failed rather than heading itself "Error"."""
    dialog = Adw.AlertDialog(
        heading=heading,
        body=message,
    )
    dialog.add_response("ok", "OK")
    dialog.set_default_response("ok")
    dialog.present(parent_window)
    return GLib.SOURCE_REMOVE


def _on_uninstall(slug: str, parent_window, group, converter,
                  refresher=None) -> None:
    # Look up the display name for the confirmation dialog so users see
    # the friendly name ("My Dark Theme") rather than the slug ("my-dark-theme").
    all_themes = load_all_themes()
    display_name = all_themes.get(slug, type("T", (), {"name": slug})()).name

    dialog = Adw.AlertDialog(
        heading="Uninstall theme?",
        body=f"'{display_name}' will be removed from your themes folder.",
    )
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("remove", "Uninstall")
    dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def _on_response(dlg, response):
        if response == "remove":
            try:
                _thumb_cache.invalidate(slug)
                uninstall_theme(slug)
                # The list this row is in, and the swatches elsewhere.
                _after_change(group, parent_window, converter, refresher)
            except (ValueError, OSError) as exc:
                _show_error("Could not uninstall the theme", str(exc),
                            parent_window)

    dialog.connect("response", _on_response)
    dialog.present(parent_window)


def _open_themes_folder(parent_window) -> None:
    folder = user_themes_dir()
    launcher = Gtk.FileLauncher.new(
        Gio.File.new_for_path(str(folder))
    )
    launcher.launch(parent_window, None, None)
