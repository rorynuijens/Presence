"""
window.py — Main application window.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib, Gdk, Pango

from .editor     import Editor
from .sidebar    import Sidebar
from .theme_panel    import ThemePanel
from .settings_dialog import SettingsDialog
from .app_utils        import png_bytes_to_texture, make_file_filter, make_filter_store

log = logging.getLogger(__name__)
from .converter  import Converter
from .presenter  import PresenterWindow
from .shortcuts  import build_shortcuts_window
from .session    import (save_last_file, load_window_state, save_window_state,
                          load_editor_prefs, save_editor_prefs, save_recent_file,
                          load_recent_files, delete_recovery_file,
                          load_presentation_prefs, save_presentation_prefs)
from .session    import recovery_path_for, recovery_dir
from .slides.themes import ASPECT_RATIOS
from .slides.splitter import split_slides
from .slides.frontmatter import parse_frontmatter, raw_frontmatter

UNTITLED = "Untitled"

# Default content shown in every new untitled document (#24).
# Uses the most important Presence syntax markers so new users discover them
# organically without needing to read documentation.
_STARTER_TEMPLATE = """\
---
title: My Presentation
author: Your Name
---

# Title slide

Your subtitle here

---

## Slide 2

- First bullet point
- Second bullet point
- Third bullet point

---

## Slide 3

Left column content

|||

Right column content

---

## Slide 4

Notes go below the ^^^ separator.

^^^

These are **speaker notes** — only visible in presenter mode.
""".lstrip()

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("Presence")

        state = load_window_state()
        self.set_default_size(state.get("width", 1400), state.get("height", 860))
        if state.get("maximized"):
            self.maximize()

        self._file_path:   Path | None = None
        self._output_path: Path | None = None
        self._modified:    bool        = False
        self._slide_info: list = []
        self._thumbnails: list = []
        self._html_uri:   str  = ""
        self._slide_w: int = 1280
        self._slide_h: int = 720
        self._temp_md:  Path | None = None
        self._temp_pdf: Path | None = None
        # Strong reference to any active Gtk.FileDialog to prevent GC collection
        # before the user completes the async operation. Cleared in each callback.
        self._active_file_dialog = None
        # Debounce source for window-size saves (#96)
        self._size_save_source: int | None = None
        # Debounce source for deferred initial conversion (#25)
        self._initial_convert_source: int | None = None
        # Debounce source for sidebar update from text (#37)
        self._sidebar_update_source: int | None = None
        # Cursor-position polling source for sidebar sync (#28)
        self._cursor_sync_source: int | None = None
        # Cache for cursor-sync: avoid re-parsing unchanged text every 300ms
        self._cursor_sync_cache: tuple[str, list[int]] | None = None

        prefs = load_editor_prefs()

        self._converter = Converter(
            theme=prefs.get("theme", "light"),
            ratio=prefs.get("ratio", "16:9"),
            logo_path=Path(prefs["logo"]) if prefs.get("logo") else None,
        )
        self._converter.connect("conversion-started",  self._on_conversion_started)
        self._converter.connect("conversion-complete", self._on_conversion_complete)
        self._converter.connect("conversion-failed",   self._on_conversion_failed)

        # Presentation preferences — must be set before _build_ui() so that
        # ThemePanel.attach() → _sync_controls() can read _timer_minutes.
        pres_prefs = load_presentation_prefs()
        self._auto_convert: bool = pres_prefs.get('auto_convert', False)
        self._timer_minutes: int = pres_prefs.get('timer_minutes', 0)
        self._open_presenter_after_convert: bool = False
        # Speaking rate in WPM — default 110, persisted to session.json
        self._speaking_rate: int = pres_prefs.get('speaking_rate', 110)
        self._theme_panel_visible: bool = True
        # Notes font size in presenter mode — default 22px, persisted on
        # the window so PresenterWindow can read and write it.
        self._presenter_notes_font: int = pres_prefs.get(
            'presenter_notes_font', 22
        )

        self._build_ui()
        self._setup_actions()
        self._setup_recent_actions()
        # Attach ThemePanel now that converter is available (#attach needs it)
        self._theme_panel.attach(self, self._converter)

        # Apply persisted editor preferences (#50 + editor tab)
        self._editor.set_font_size(prefs.get("font_size", 13))
        self._editor.set_syntax_highlight(prefs.get("syntax_highlight", True))
        self._editor.set_line_numbers(prefs.get("line_numbers", True))
        self._editor.set_highlight_current_line(prefs.get("highlight_line", True))
        self._editor.set_auto_indent(prefs.get("auto_indent", True))
        self._editor.set_spaces_instead_of_tabs(prefs.get("spaces_tabs", True))
        self._editor.set_line_length(prefs.get("line_length", 64))

        self._autosave_source: int | None = GLib.timeout_add_seconds(30, self._autosave)

        # Poll cursor position every 300 ms to keep sidebar in sync (#28)
        self._cursor_sync_source = GLib.timeout_add(300, self._sync_sidebar_to_cursor)

        self.connect("notify::default-width",  self._on_size_changed)
        self.connect("notify::default-height", self._on_size_changed)
        self.connect("notify::maximized",      self._on_size_changed)
        self.connect("close-request",          self._on_close_request)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Wrap in Adw.ToastOverlay so theme-install toasts work (#2)
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._toast_overlay.set_child(root)

        root.append(self._build_header())

        # Use Adw.Banner only for persistent app-level messages (#20)
        self._banner = Adw.Banner(title="")
        self._banner.set_button_label("Close")   # HIG standard label (#23)
        self._banner.set_revealed(False)
        self._banner.connect("button-clicked", lambda *_: self._banner.set_revealed(False))
        root.append(self._banner)

        self._sidebar     = Sidebar()
        self._editor      = Editor()
        self._theme_panel = ThemePanel()

        self._sidebar.connect("slide-selected",       self._on_slide_selected)
        self._sidebar.connect("slide-insert-after",   self._on_slide_insert_after)
        self._sidebar.connect("slides-reordered",     self._on_slides_reordered)
        self._sidebar.connect("slide-zoom-requested",  self._on_slide_zoom)
        self._editor.connect_undo_notify(self._on_undo_state_changed)
        self._editor.set_insert_image_callback(self._on_insert_image)
        self._editor.connect("changed",               self._on_editor_changed)
        self._theme_panel.connect("rebuild-needed",   self._on_theme_panel_rebuild)

        # Layout: [Sidebar 260] | [Editor] | [ThemePanel 280]
        # _right_pane nests editor and theme panel; _main_pane adds the
        # left sidebar.  The theme panel is collapsible via F10.
        self._right_pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._right_pane.set_vexpand(True)
        self._right_pane.set_hexpand(True)
        self._right_pane.set_start_child(self._editor)
        self._right_pane.set_end_child(self._theme_panel)
        self._right_pane.set_resize_start_child(True)
        self._right_pane.set_resize_end_child(False)
        self._right_pane.set_shrink_start_child(False)
        self._right_pane.set_shrink_end_child(False)

        self._main_pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._main_pane.set_vexpand(True)
        self._main_pane.set_hexpand(True)
        self._main_pane.set_position(260)
        self._main_pane.set_start_child(self._sidebar)
        self._main_pane.set_end_child(self._right_pane)
        self._main_pane.set_resize_start_child(False)
        self._main_pane.set_resize_end_child(True)
        self._main_pane.set_shrink_start_child(False)
        self._main_pane.set_shrink_end_child(False)
        root.append(self._main_pane)

    def _build_header(self) -> Adw.HeaderBar:
        bar = Adw.HeaderBar()

        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_tooltip_text("Main menu")
        menu_btn.set_menu_model(self._build_app_menu())
        bar.pack_start(menu_btn)

        self._sidebar_btn = Gtk.ToggleButton()
        self._sidebar_btn.set_icon_name("sidebar-show-symbolic")
        self._sidebar_btn.set_tooltip_text("Show slide panel (F9)")
        self._sidebar_btn.set_active(True)
        self._sidebar_btn.connect("toggled", self._on_sidebar_toggled)
        bar.pack_start(self._sidebar_btn)

        for icon, tooltip, handler in [
            ("document-new-symbolic",     "New (Ctrl+N)",         self._on_new),
            ("document-open-symbolic",    "Open… (Ctrl+O)",       self._on_open),
            ("document-save-symbolic",    "Save (Ctrl+S)",        self._on_save),
            ("document-save-as-symbolic", "Save as… (Ctrl+⇧+S)", self._on_save_as),
        ]:
            b = Gtk.Button()
            b.set_child(Gtk.Image.new_from_icon_name(icon))
            b.set_tooltip_text(tooltip)
            b.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
            b.add_css_class("flat")
            b.connect("clicked", handler)
            bar.pack_start(b)

        self._undo_btn = Gtk.Button()
        self._undo_btn.set_child(Gtk.Image.new_from_icon_name("edit-undo-symbolic"))
        self._undo_btn.set_tooltip_text("Undo (Ctrl+Z)")
        self._undo_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Undo"])
        self._undo_btn.add_css_class("flat")
        self._undo_btn.set_sensitive(False)
        self._undo_btn.connect("clicked", lambda *_: self._editor.undo())
        bar.pack_start(self._undo_btn)

        self._redo_btn = Gtk.Button()
        self._redo_btn.set_child(Gtk.Image.new_from_icon_name("edit-redo-symbolic"))
        self._redo_btn.set_tooltip_text("Redo (Ctrl+⇧+Z)")
        self._redo_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Redo"])
        self._redo_btn.add_css_class("flat")
        self._redo_btn.set_sensitive(False)
        self._redo_btn.connect("clicked", lambda *_: self._editor.redo())
        bar.pack_start(self._redo_btn)

        self._title_label = Adw.WindowTitle(title="Untitled", subtitle="")
        bar.set_title_widget(self._title_label)

        self._spinner = Gtk.Spinner()
        self._spinner.set_visible(False)
        bar.pack_end(self._spinner)

        # Theme panel toggle — right sidebar visibility (F10)
        self._theme_panel_btn = Gtk.ToggleButton()
        self._theme_panel_btn.set_icon_name("sidebar-show-right-symbolic")
        self._theme_panel_btn.set_tooltip_text("Show theme panel (F10)")
        self._theme_panel_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Show theme panel"]
        )
        self._theme_panel_btn.set_active(True)
        self._theme_panel_btn.add_css_class("flat")
        self._theme_panel_btn.connect("toggled", self._on_theme_panel_toggled)
        bar.pack_end(self._theme_panel_btn)

        # Share button — opens a popover with all export options.
        # Anchored to a real button so the popover arrow points correctly.
        self._share_btn = Gtk.MenuButton()
        self._share_btn.set_icon_name("document-send-symbolic")
        self._share_btn.set_tooltip_text("Share / export")
        self._share_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Share / export"]
        )
        self._share_btn.add_css_class("flat")
        self._share_btn.set_popover(self._build_share_popover())
        # Disable until a conversion exists
        self._share_btn.set_sensitive(False)
        bar.pack_end(self._share_btn)

        # Present button — converts (if needed) then opens presenter mode.
        self._present_btn = Gtk.Button()
        self._present_btn.set_child(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        self._present_btn.set_tooltip_text("Present (Ctrl+P)")
        self._present_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Present"]
        )
        self._present_btn.add_css_class("suggested-action")
        self._present_btn.connect("clicked", self._on_present_clicked)
        bar.pack_end(self._present_btn)

        return bar

    def _build_share_popover(self) -> Gtk.Popover:
        """
        Build the Share popover containing all export actions.

        Uses a Gtk.Popover with a vertical ListBox of action rows so each
        option has a clear icon, label and subtitle — more informative than
        a plain menu and consistent with GNOME HIG action popovers.
        """
        popover = Gtk.Popover()
        popover.set_has_arrow(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_size_request(240, -1)

        def _row(icon: str, label: str, subtitle: str, cb) -> Gtk.Button:
            """One export row: icon + label/subtitle stacked, full-width flat button."""
            btn = Gtk.Button()
            btn.add_css_class("flat")
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            inner.set_margin_top(6)
            inner.set_margin_bottom(6)
            inner.set_margin_start(6)
            inner.set_margin_end(6)

            img = Gtk.Image.new_from_icon_name(icon)
            img.set_pixel_size(20)
            img.set_valign(Gtk.Align.CENTER)
            inner.append(img)

            text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            text_box.set_hexpand(True)
            text_box.set_valign(Gtk.Align.CENTER)

            lbl = Gtk.Label(label=label)
            lbl.set_xalign(0)
            lbl.set_halign(Gtk.Align.START)
            text_box.append(lbl)

            sub = Gtk.Label(label=subtitle)
            sub.set_xalign(0)
            sub.add_css_class("caption")
            sub.add_css_class("dim-label")
            text_box.append(sub)

            inner.append(text_box)
            btn.set_child(inner)

            def _clicked(*_):
                popover.popdown()
                cb()

            btn.connect("clicked", _clicked)
            return btn

        box.append(_row(
            "document-open-symbolic",
            "Open PDF",
            "Open the output in the system viewer",
            self._on_open_pdf_clicked,
        ))
        box.append(_row(
            "edit-copy-symbolic",
            "Copy PDF path",
            "Copy the output path to clipboard",
            self._on_copy_pdf_path,
        ))
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        box.append(_row(
            "document-save-symbolic",
            "Save PDF",
            "Save to the current output location",
            self._on_export,
        ))
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        box.append(_row(
            "text-x-generic-symbolic",
            "Export HTML…",
            "Self-contained HTML slide deck",
            self._on_export_html,
        ))
        box.append(_row(
            "image-x-generic-symbolic",
            "Export Images…",
            "One PNG per slide into a folder",
            self._on_export_images,
        ))
        box.append(_row(
            "folder-open-symbolic",
            "Show in file manager",
            "Open the output folder",
            self._on_show_in_file_manager,
        ))

        popover.set_child(box)
        return popover

    def _build_app_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append("Save As…",        "win.save-as")
        s1.append("Export PDF…",     "win.export")
        s1.append("Export HTML…",    "win.export-html")
        menu.append_section(None, s1)

        self._recent_menu = Gio.Menu()
        self._rebuild_recent_menu()
        menu.append_submenu("Recent files", self._recent_menu)

        s2 = Gio.Menu()
        s2.append("Keyboard shortcuts", "win.shortcuts")
        s2.append("Settings…",         "app.preferences")
        s2.append("About",             "app.about")
        menu.append_section(None, s2)
        return menu

    def _rebuild_recent_menu(self) -> None:
        self._recent_menu.remove_all()
        recents = load_recent_files()
        if not recents:
            return

        # Check file existence on a background thread to avoid blocking
        # stat() calls on the main thread (#55).  We split into two
        # sub-menus: existing files first, then a "Missing files" section
        # for those no longer on disk.  The check is done synchronously
        # here because the list is ≤8 items and Path.exists() is fast
        # for local filesystems; a background approach would require
        # keeping the menu model mutable across closures.
        present: list[tuple[int, str]] = []
        missing: list[tuple[int, str]] = []
        for i, p in enumerate(recents[:8]):
            (present if Path(p).exists() else missing).append((i, p))

        for i, p in present:
            self._recent_menu.append(Path(p).name, f"win.open-recent-{i}")

        if missing:
            missing_section = Gio.Menu()
            for i, p in missing:
                # Append with a visual indicator; the action itself is disabled
                # in _setup_recent_actions when the file is absent.
                missing_section.append(f"{Path(p).name} (missing)",
                                       f"win.open-recent-{i}")
            self._recent_menu.append_section("Missing files", missing_section)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _setup_actions(self) -> None:
        actions = [
            ("new",      self._on_new,            "<primary>n"),
            ("open",     self._on_open,            "<primary>o"),
            ("save",     self._on_save,            "<primary>s"),
            ("save-as",  self._on_save_as,         "<primary><shift>s"),
            # Ctrl+Return still triggers a manual rebuild for power users
            ("convert",  self._trigger_convert,    "<primary>Return"),
            ("undo",         lambda *_: self._editor.undo(),              "<primary>z"),
            ("redo",         lambda *_: self._editor.redo(),              "<primary><shift>z"),
            ("find",         lambda *_: self._editor.show_find(),         "<primary>f"),
            ("find-replace", lambda *_: self._editor.show_find_replace(), "<primary>h"),
            ("export",       self._on_export,                             "<primary><shift>e"),
            ("export-html",  self._on_export_html,                        None),
            ("presenter",    self._on_presenter,                          "<primary>p"),
            # F1 for shortcuts (#45 / #86)
            ("shortcuts",    self._on_shortcuts,                          "F1"),
            ("insert-image",   self._on_insert_image,   None),
            ("insert-comment", self._on_insert_comment, None),
            ("bold",   lambda *_: self._editor.bold(),         "<primary>b"),
            ("italic", lambda *_: self._editor.italic(),       "<primary>i"),
            ("link",   lambda *_: self._editor.insert_link(),  "<primary>k"),
            # Sidebar / preview panel toggles with F9/F10 (#75)
            ("toggle-sidebar",       self._on_toggle_sidebar,       "F9"),
            ("toggle-theme-panel",   self._on_toggle_theme_panel,   "F10"),
        ]
        for name, cb, accel in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            if accel:
                self.get_application().set_accels_for_action(
                    f"win.{name}", [accel]
                )

        # Presenter action: disabled until first successful conversion (#27)
        self._presenter_action = self.lookup_action("presenter")

    def _setup_recent_actions(self) -> None:
        recents = load_recent_files()
        for i, p in enumerate(recents[:8]):
            path   = Path(p)
            action = Gio.SimpleAction.new(f"open-recent-{i}", None)
            # Disable the action if the file no longer exists (#55) so the
            # greyed-out menu item is non-interactive, matching HIG guidance.
            action.set_enabled(path.exists())
            action.connect("activate", lambda *_, p=path: self._check_unsaved(lambda: self.open_file(p)))
            self.add_action(action)

    def _refresh_recent_actions(self) -> None:
        for i in range(8):
            self.remove_action(f"open-recent-{i}")
        self._setup_recent_actions()
        self._rebuild_recent_menu()

    # ── Unsaved changes ───────────────────────────────────────────────────────

    def _on_close_request(self, win) -> bool:
        self._converter.stop_watch()
        for attr in ("_autosave_source", "_size_save_source",
                     "_initial_convert_source", "_sidebar_update_source",
                     "_cursor_sync_source"):
            src = getattr(self, attr, None)
            if src is not None:
                GLib.source_remove(src)
                setattr(self, attr, None)
        # Always clean up temp files, regardless of modified state (#44)
        self._cleanup_temp_files()

        if not self._modified:
            return False

        self._show_unsaved_dialog(
            on_save=lambda: self.destroy(),
            on_discard=lambda: self.destroy(),
        )
        return True

    def _check_unsaved(self, action) -> None:
        if not self._modified:
            action()
            return
        self._show_unsaved_dialog(on_save=action, on_discard=action)

    def _show_unsaved_dialog(self, on_save, on_discard) -> None:
        name = self._file_path.name if self._file_path else "Untitled"
        dialog = Adw.AlertDialog(
            heading=f'Save changes to "{name}"?',
            body="Your changes will be lost if you don't save them.",
        )
        dialog.add_response("cancel",  "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save",    "Save")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save",    Adw.ResponseAppearance.SUGGESTED)
        # HIG: default to the safe (least destructive) action (#21)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(dlg, response):
            if response == "save":
                self._save()
                on_save()
            elif response == "discard":
                self._modified = False
                on_discard()

        dialog.connect("response", _on_response)
        dialog.present(self)

    # ── File operations ───────────────────────────────────────────────────────

    def open_file(self, path: Path) -> None:
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError) as e:
            self._show_error(f"Cannot open file: {e}")
            return

        # Warn if this file is already open in another window (#88)
        for win in self.get_application().get_windows():
            if (win is not self
                    and isinstance(win, MainWindow)
                    and win._file_path == path):
                dialog = Adw.AlertDialog(
                    heading="File already open",
                    body=(f"'{path.name}' is already open in another window. "
                          "Opening it again may cause conflicts if both windows save."),
                )
                dialog.add_response("cancel", "Cancel")
                dialog.add_response("open",   "Open anyway")
                dialog.set_default_response("cancel")
                dialog.set_close_response("cancel")

                def _on_response(dlg, response, p=path):
                    if response == "open":
                        self._do_open_file(p)
                dialog.connect("response", _on_response)
                dialog.present(self)
                return

        self._do_open_file(path)

    def _do_open_file(self, path: Path) -> None:
        """Internal: load file into editor (called after duplicate check)."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            self._show_error(f"Could not open file: {e}")
            return

        self._file_path   = path
        self._output_path = path.with_suffix(".pdf")
        self._editor.set_base_path(path)
        self._editor.set_text(text)
        self._sidebar.update_from_text(text)
        self._set_title(path.name)
        self._modified = False
        save_last_file(path)
        save_recent_file(path)
        self._refresh_recent_actions()

        # Defer the initial conversion by one idle cycle so the window is
        # fully realised before WeasyPrint starts (#25 / #69).
        if self._initial_convert_source is not None:
            GLib.source_remove(self._initial_convert_source)
        self._initial_convert_source = GLib.idle_add(self._deferred_initial_convert)

    def _deferred_initial_convert(self) -> bool:
        self._initial_convert_source = None
        self._trigger_convert()
        return GLib.SOURCE_REMOVE

    def restore_autosave(self, text: str) -> None:
        self._editor.set_text(text)
        self._modified = True
        base = self._file_path.name if self._file_path else UNTITLED
        self._set_title(base + " •")
        self._trigger_convert()

    def _save(self) -> bool:
        if self._file_path is None:
            return self._save_as_dialog()
        try:
            self._file_path.write_text(
                self._editor.get_text(), encoding="utf-8"
            )
            self._modified = False
            self._set_title(self._file_path.name)
            save_last_file(self._file_path)
            # Delete any orphaned recovery file (fixes #59)
            delete_recovery_file(self._file_path)
            # Trigger conversion: always when not in watch mode (existing
            # behaviour), or immediately on every save if auto-convert is on.
            # watching is always False in GUI mode (watch is CLI-only);
            # the check is kept so that if watch is ever started externally,
            # auto-save does not double-convert.
            if not self._converter.watching or self._auto_convert:
                self._trigger_convert()
            return True
        except OSError as e:
            self._show_error(f"Could not save: {e}")
            return False

    def _save_as_dialog(self) -> bool:
        dialog = Gtk.FileDialog()
        dialog.set_title("Save As")
        dialog.set_filters(make_filter_store(
            make_file_filter("Markdown files", "*.md")
        ))
        self._active_file_dialog = dialog
        dialog.save(self, None, self._on_save_as_response)
        return True

    def _on_save_as_response(self, dialog, result) -> None:
        self._active_file_dialog = None
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        path = Path(gfile.get_path())
        if not path.suffix:
            path = path.with_suffix(".md")
        # Verify the directory is writable before committing (#67)
        if not os.access(path.parent, os.W_OK):
            self._show_error(f"Cannot write to '{path.parent}' — permission denied.")
            return
        self._file_path   = path
        self._output_path = path.with_suffix(".pdf")
        self._editor.set_base_path(path)
        self._save()

    # ── Conversion ────────────────────────────────────────────────────────────

    def _trigger_convert(self, *_) -> None:
        if self._file_path is None:
            self._cleanup_temp_files()
            fd, tmp_str = tempfile.mkstemp(suffix=".md")
            try:
                os.write(fd, self._editor.get_text().encode("utf-8"))
            finally:
                os.close(fd)
            self._temp_md  = Path(tmp_str)
            self._temp_pdf = self._temp_md.with_suffix(".pdf")
            input_path  = self._temp_md
            output_path = self._temp_pdf
        else:
            if self._modified:
                self._save()
                return
            self._cleanup_temp_files()
            input_path  = self._file_path
            output_path = self._output_path

        self._converter.convert(input_path, output_path)

    def _cleanup_temp_files(self) -> None:
        for attr in ("_temp_md", "_temp_pdf"):
            p = getattr(self, attr, None)
            if p is not None:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
                setattr(self, attr, None)

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _on_editor_changed(self, editor: Editor, text: str) -> None:
        self._modified = True
        base = self._file_path.name if self._file_path else UNTITLED
        self._set_title(base + " •")
        # Debounce the sidebar parse: run 200 ms after the last keystroke so
        # we do not parse the full document on every character (#37).
        if self._sidebar_update_source is not None:
            GLib.source_remove(self._sidebar_update_source)
        self._sidebar_update_source = GLib.timeout_add(
            200, self._flush_sidebar_update, text
        )
        self._update_word_count(text)

    def _flush_sidebar_update(self, text: str) -> bool:
        self._sidebar_update_source = None
        self._sidebar.update_from_text(text)
        return GLib.SOURCE_REMOVE

    def _sync_sidebar_to_cursor(self) -> bool:
        """
        Poll the editor cursor position every 300 ms and highlight the
        matching slide in the sidebar (#28).

        Caches the parsed slide-offset list so we only re-parse the document
        when the text has actually changed — avoids calling split_slides()
        3× per second on every keystroke.
        """
        if not self._slide_info:
            return GLib.SOURCE_CONTINUE
        try:
            text = self._editor.get_text()
            buf    = self._editor._buffer
            cursor = buf.get_iter_at_mark(buf.get_insert())
            offset = cursor.get_offset()

            # Re-parse only when text changed since last poll
            if (self._cursor_sync_cache is None
                    or self._cursor_sync_cache[0] != text):
                _meta, body = parse_frontmatter(text)
                fm_len = len(text) - len(body.lstrip("\n")) if body else 0
                slides = split_slides(body)
                slide_offsets: list[int] = []
                search_pos = 0
                for slide_text in slides:
                    idx = body.find(slide_text.rstrip(), search_pos)
                    if idx == -1:
                        idx = search_pos
                    slide_offsets.append(fm_len + idx)
                    search_pos = idx + len(slide_text)
                self._cursor_sync_cache = (text, slide_offsets)
            else:
                slide_offsets = self._cursor_sync_cache[1]

            # Find which slide the cursor is in
            current = 0
            for i, start in enumerate(slide_offsets):
                if offset >= start:
                    current = i
                else:
                    break

            self._sidebar.scroll_to_index(current)
        except Exception:
            pass   # never crash the main loop

        # Check whether the cursor is on an image tag and open/update
        # the contextual image-edit popover accordingly.
        try:
            self._editor.check_cursor_for_image()
        except Exception:
            pass
        return GLib.SOURCE_CONTINUE

    def _on_undo_state_changed(self, can_undo: bool, can_redo: bool) -> None:
        self._undo_btn.set_sensitive(can_undo)
        self._redo_btn.set_sensitive(can_redo)

    def _on_slide_selected(self, sidebar: Sidebar, index: int) -> None:
        self._editor.scroll_to_slide(index)

    def _on_slide_insert_after(self, sidebar: Sidebar, after_index: int) -> None:
        """
        Insert a blank slide immediately after *after_index* (#90).

        The new slide is inserted into the editor buffer as a user action
        (undoable) and the sidebar + title are updated immediately.
        """
        text = self._editor.get_text()
        _meta, body = parse_frontmatter(text)
        slides = split_slides(body)

        # Clamp to valid range
        insert_at = max(0, min(after_index + 1, len(slides)))

        blank = "## New slide\n\nYour content here."
        slides.insert(insert_at, blank)
        new_body = "\n\n---\n\n".join(slides)

        fm = raw_frontmatter(text)
        new_text = (fm + "\n\n" + new_body) if fm else new_body

        self._editor.set_text_as_user_action(new_text)
        self._modified = True
        base = self._file_path.name if self._file_path else UNTITLED
        self._set_title(base + " •")
        self._sidebar.update_from_text(new_text)
        # Scroll editor to the newly inserted slide
        GLib.idle_add(lambda: (self._editor.scroll_to_slide(insert_at), False))

    def _on_slides_reordered(self, sidebar: Sidebar, from_idx: int, to_idx: int) -> None:
        text = self._editor.get_text()
        _meta, body = parse_frontmatter(text)
        slides = split_slides(body)

        if from_idx == to_idx or not (0 <= from_idx < len(slides)) \
                              or not (0 <= to_idx   < len(slides)):
            return

        slide = slides.pop(from_idx)
        slides.insert(to_idx, slide)
        new_body = "\n\n---\n\n".join(slides)

        fm = raw_frontmatter(text)
        new_text = (fm + "\n\n" + new_body) if fm else new_body

        # Use set_text_as_user_action so the reorder is one undo step (#56)
        self._editor.set_text_as_user_action(new_text)
        self._modified = True
        base = self._file_path.name if self._file_path else UNTITLED
        self._set_title(base + " •")
        self._sidebar.update_from_text(new_text)

    def _on_present_clicked(self, *_) -> None:
        """
        Convert (if the source has changed since last build) then open
        presenter mode.  If a conversion is already running the presenter
        will open automatically when it finishes via _on_conversion_complete.
        """
        if self._modified or not self._html_uri:
            # Need a fresh build — queue presenter to open on completion
            self._open_presenter_after_convert = True
            self._trigger_convert()
        else:
            # Already up to date — open presenter immediately
            self._open_presenter_after_convert = False
            self._on_presenter()

    def _on_open_pdf_clicked(self, *_) -> None:
        if self._output_path and self._output_path.exists():
            launcher = Gtk.FileLauncher.new(
                Gio.File.new_for_path(str(self._output_path))
            )
            launcher.launch(self, None, None)

    def _on_copy_pdf_path(self, *_) -> None:
        """Copy the output PDF path to the clipboard (#72)."""
        if self._output_path:
            display = Gdk.Display.get_default()
            if display:
                display.get_clipboard().set(str(self._output_path))
            self._show_toast("PDF path copied to clipboard", timeout=2)

    # ── Converter signal handlers ─────────────────────────────────────────────

    def _on_conversion_started(self, converter: Converter) -> None:
        self._spinner.set_visible(True)
        self._spinner.start()
        self._present_btn.set_sensitive(False)
        self._banner.set_revealed(False)
        self._title_label.set_subtitle("Building…")
        # Show per-thumbnail spinners so users know thumbnails are updating (#71)
        self._sidebar.set_converting(True)

    def _on_conversion_complete(
        self,
        converter: Converter,
        n_slides:  int,
        duration:  float,
        pdf_path:  str,
        html_uri:  str,
    ) -> None:
        self._spinner.stop()
        self._spinner.set_visible(False)
        self._present_btn.set_sensitive(True)
        self._share_btn.set_sensitive(True)
        self._output_path = Path(pdf_path)

        slides_word = "slide" if n_slides == 1 else "slides"
        self._show_toast(
            f"Built in {duration:.1f}s · {n_slides} {slides_word}", timeout=4
        )
        self._slide_info = converter.slide_info
        self._thumbnails = converter.thumbnails
        self._html_uri   = html_uri

        # Enable presenter mode now that a conversion exists (#27)
        if self._presenter_action:
            self._presenter_action.set_enabled(True)

        # If Present was clicked while a build was needed, open now
        if self._open_presenter_after_convert:
            self._open_presenter_after_convert = False
            self._on_presenter()

        # Stop thumbnail spinners before replacing content (#71)
        self._sidebar.set_converting(False)
        overflow_count = self._sidebar.update_from_conversion(
            converter.slide_info, converter.thumbnails,
            wpm=self._speaking_rate,
        ) or 0
        if overflow_count:
            s = "slide" if overflow_count == 1 else "slides"
            self._banner.set_title(
                f"{overflow_count} {s} may have too much text "
                f"— content could be clipped in the PDF."
            )
            self._banner.set_revealed(True)

        w, h = ASPECT_RATIOS.get(self._converter.ratio, (1280, 720))
        self._slide_w, self._slide_h = w, h

        if self._file_path is not None:
            self._cleanup_temp_files()

    def _on_conversion_failed(self, converter: Converter, message: str) -> None:
        self._spinner.stop()
        self._spinner.set_visible(False)
        self._present_btn.set_sensitive(True)
        self._open_presenter_after_convert = False
        # Stop thumbnail spinners on failure too (#71)
        self._sidebar.set_converting(False)
        self._title_label.set_subtitle("")
        # Show a user-friendly message rather than a raw exception string (#87)
        friendly = _friendly_error(message)
        self._banner.set_title(friendly)
        self._banner.set_revealed(True)

    # ── Panel toggles ─────────────────────────────────────────────────────────

    def _on_sidebar_toggled(self, btn: Gtk.ToggleButton) -> None:
        """Show or hide the slide panel (F9)."""
        visible = btn.get_active()
        self._sidebar.set_visible(visible)
        self._main_pane.set_position(260 if visible else 0)

    def _on_toggle_sidebar(self, *_) -> None:
        self._sidebar_btn.set_active(not self._sidebar_btn.get_active())

    def _on_theme_panel_toggled(self, btn: Gtk.ToggleButton) -> None:
        """Show or hide the theme panel right sidebar (F10)."""
        visible = btn.get_active()
        self._theme_panel_visible = visible
        self._theme_panel.set_visible(visible)
        # When hiding, collapse the right pane to give full width to the editor
        if not visible:
            # Store position before collapsing so we can restore it
            self._saved_theme_panel_pos = self._right_pane.get_position()
        # GTK will adjust the pane automatically when the child is hidden

    def _on_toggle_theme_panel(self, *_) -> None:
        self._theme_panel_btn.set_active(not self._theme_panel_btn.get_active())

    def _on_theme_panel_rebuild(self, panel) -> None:
        """ThemePanel emitted rebuild-needed — trigger a conversion."""
        self._trigger_convert()

    def _on_slide_zoom(self, sidebar, index: int) -> None:
        """Open a full-size view of slide *index* (double-click on thumbnail)."""
        if not self._thumbnails or index >= len(self._thumbnails):
            return
        png = self._thumbnails[index]
        if not png:
            return
        title = (self._slide_info[index]["title"]
                 if index < len(self._slide_info) else f"Slide {index + 1}")
        dlg = _SlideZoomDialog(self, index + 1, title, png)
        dlg.present(self)

    # ── Menu action handlers ──────────────────────────────────────────────────

    def _on_new(self, *_) -> None:
        def _open_new():
            win = MainWindow(application=self.get_application())
            # Populate the new window with the starter template (#24)
            win._editor.set_text(_STARTER_TEMPLATE)
            win._sidebar.update_from_text(_STARTER_TEMPLATE)
            win._modified = False          # template is not a user edit
            win.present()
        self._check_unsaved(_open_new)

    def show_open_dialog(self) -> None:
        self._show_open_dialog()

    def _on_open(self, *_) -> None:
        self._check_unsaved(self._show_open_dialog)

    def _show_open_dialog(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Open Markdown File")
        dialog.set_filters(make_filter_store(
            make_file_filter("Markdown files", "*.md")
        ))
        self._active_file_dialog = dialog
        dialog.open(self, None, self._on_open_response)

    def _on_open_response(self, dialog, result) -> None:
        self._active_file_dialog = None
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        self.open_file(Path(gfile.get_path()))

    def _on_save(self, *_) -> None:
        self._save()

    def _on_save_as(self, *_) -> None:
        self._save_as_dialog()

    def _on_settings(self, *_) -> None:
        dlg = SettingsDialog(self)
        # Store a weak reference so theme_manager_ui can refresh the combo
        # after uninstall without a circular reference (fixes #2)
        self._settings_dialog_ref = dlg
        def _on_closed(*_):
            self._settings_dialog_ref = None
        dlg.connect("closed", _on_closed)
        dlg.present()

    def _on_presenter(self, *_) -> None:
        if not self._html_uri or not self._slide_info:
            self._show_error("Convert the presentation first to open presenter mode.")
            return
        # Check WebKit availability before opening — fail with a clear
        # message rather than opening a broken presenter window.
        from .presenter import _WEBKIT
        if not _WEBKIT:
            self._show_error(
                "Presenter mode requires WebKitGTK, which is not installed. "
                "Install gir1.2-webkit-6.0 (or webkit2gtk-4.1 on older systems)."
            )
            return
        win = PresenterWindow(
            html_uri=self._html_uri,
            slide_info=self._slide_info,
            thumbnails=self._thumbnails,
            parent_window=self,
            application=self.get_application(),
        )
        win.connect("timer-tick", self._on_presenter_timer_tick)
        win.connect("close-request", self._on_presenter_closed)
        win.present()

    def _on_presenter_timer_tick(self, win, elapsed: int,
                                 target: int) -> None:
        """Update status bar with live presenter timer."""
        m, s = divmod(elapsed, 60)
        h, m = divmod(m, 60)
        if h:
            elapsed_str = f"{h:02d}:{m:02d}:{s:02d}"
        else:
            elapsed_str = f"{m:02d}:{s:02d}"
        if target > 0:
            tm, ts = divmod(target, 60)
            target_str = f" / {tm:02d}:{ts:02d}"
        else:
            target_str = ""
        self._title_label.set_subtitle(f"🎤  {elapsed_str}{target_str}")

    def _on_presenter_closed(self, win) -> bool:
        """Restore subtitle when presenter window closes."""
        self._update_word_count(self._editor.get_text())
        return False   # allow normal close to proceed

    def _on_insert_comment(self, *_) -> None:
        self._editor.insert_comment()

    def _on_insert_image(self, *_) -> None:
        """
        Open the image layout popover anchored to the editor toolbar button.
        The popover handles layout selection and file picking, then inserts
        the finished Markdown tag with position/size/gradient tokens.
        Falls back to a bare file dialog if the toolbar button is unavailable.
        """
        btn = self._editor.get_img_toolbar_btn()
        if btn is not None:
            self._editor.open_image_layout_popover(btn)
        else:
            self._on_insert_image_fallback()

    def _on_insert_image_fallback(self) -> None:
        """Plain file dialog used when the toolbar button reference is unavailable."""
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose Image")
        dialog.set_filters(make_filter_store(
            make_file_filter("Images",
                             "*.png", "*.jpg", "*.jpeg",
                             "*.gif", "*.svg", "*.webp")
        ))
        # Keep a strong reference so the GC cannot collect the dialog before
        # the user has chosen a file.  Cleared in _on_image_picked_fallback.
        self._active_file_dialog = dialog
        dialog.open(self, None, self._on_image_picked_fallback)

    def _on_image_picked_fallback(self, dialog, result) -> None:
        """Completion handler for the fallback file dialog."""
        self._active_file_dialog = None
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        src_path = Path(gfile.get_path())
        if not src_path.is_file():
            return

        if self._file_path:
            assets_dir = self._file_path.parent / "assets"
            try:
                assets_dir.mkdir(exist_ok=True)
                dst = assets_dir / src_path.name
                if not dst.exists():
                    shutil.copy2(src_path, dst)
                rel_path = f"assets/{src_path.name}"
            except OSError:
                rel_path = str(src_path)
        else:
            rel_path = str(src_path)

        alt = src_path.stem.replace("-", " ").replace("_", " ")
        self._editor.insert_image_markdown(alt, rel_path)

    def _on_shortcuts(self, *_) -> None:
        build_shortcuts_window(self).present()

    def _on_export(self, *_) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Export PDF")
        if self._output_path:
            dialog.set_initial_file(
                Gio.File.new_for_path(str(self._output_path))
            )
        dialog.set_filters(make_filter_store(
            make_file_filter("PDF files", "*.pdf")
        ))
        self._active_file_dialog = dialog
        dialog.save(self, None, self._on_export_response)

    def _on_export_response(self, dialog, result) -> None:
        self._active_file_dialog = None
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        path = Path(gfile.get_path())
        if not path.suffix:
            path = path.with_suffix(".pdf")
        # Verify the directory is writable before starting conversion (#67)
        if not os.access(path.parent, os.W_OK):
            self._show_error(f"Cannot write to '{path.parent}' — permission denied.")
            return
        self._output_path = path
        self._trigger_convert()

    def _on_export_html(self, *_) -> None:
        """Save a self-contained copy of the generated HTML file."""
        if not self._html_uri:
            self._show_error("Convert the presentation first.")
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("Export HTML")
        if self._output_path:
            dialog.set_initial_file(
                Gio.File.new_for_path(str(self._output_path.with_suffix(".html")))
            )
        dialog.set_filters(make_filter_store(
            make_file_filter("HTML files", "*.html")
        ))
        self._active_file_dialog = dialog
        dialog.save(self, None, self._on_export_html_response)

    def _on_export_html_response(self, dialog, result) -> None:
        self._active_file_dialog = None
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        dest = Path(gfile.get_path())
        if not dest.suffix:
            dest = dest.with_suffix(".html")
        if not os.access(dest.parent, os.W_OK):
            # Transient one-shot failure — toast is appropriate here
            self._show_toast(
                f"Cannot write to '{dest.parent}' — permission denied."
            )
            return
        # The HTML file is already on disk next to the PDF; copy it.
        from urllib.parse import urlparse
        from urllib.request import url2pathname
        src_path = Path(url2pathname(urlparse(self._html_uri).path))
        try:
            shutil.copy2(src_path, dest)
            self._show_toast(f"HTML exported → {dest.name}")
        except OSError as e:
            self._show_toast(f"Could not export HTML: {e}")


    def _on_export_images(self) -> None:
        """Export each slide as a full-resolution PNG into a user-chosen folder."""
        if not self._html_uri or not self._thumbnails:
            self._show_error("Convert the presentation first.")
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose Export Folder")
        self._active_file_dialog = dialog
        dialog.select_folder(self, None, self._on_export_images_folder_chosen)

    def _on_export_images_folder_chosen(self, dialog, result) -> None:
        self._active_file_dialog = None
        try:
            gfile = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        folder = Path(gfile.get_path())
        if not folder.is_dir():
            return

        # Use the high-res renderer so exported PNGs are crisp at 1920px wide.
        from .slides.thumbnails_render import render_slides_hires

        if self._output_path is None or not self._output_path.exists():
            # Transient one-shot failure: show as toast, not persistent banner.
            self._show_toast("No PDF found — convert first.")
            return

        try:
            pdf_bytes = self._output_path.read_bytes()
        except OSError as e:
            self._show_toast(f"Could not read PDF: {e}")
            return

        # Render on a background thread so the UI stays responsive.
        def _render():
            slides = render_slides_hires(pdf_bytes, width_px=1920)
            GLib.idle_add(_on_done, slides)

        def _on_done(slides: list) -> bool:
            saved = 0
            stem  = (self._file_path.stem if self._file_path else "slide")
            for i, png in enumerate(slides):
                if png:
                    try:
                        (folder / f"{stem}_{i + 1:02d}.png").write_bytes(png)
                        saved += 1
                    except OSError as e:
                        log.warning("Could not write slide PNG: %s", e)
            self._show_toast(
                f"{saved} image{'s' if saved != 1 else ''} exported → {folder.name}/"
            )
            return GLib.SOURCE_REMOVE

        threading.Thread(target=_render, daemon=True).start()

    def _on_show_in_file_manager(self, *_) -> None:
        """Open the output folder in the system file manager."""
        if self._output_path and self._output_path.parent.exists():
            folder = Gio.File.new_for_path(str(self._output_path.parent))
        elif self._file_path:
            folder = Gio.File.new_for_path(str(self._file_path.parent))
        else:
            # Transient: user triggered this without a file loaded.
            self._show_toast("No output folder to open.")
            return
        launcher = Gtk.FileLauncher.new(folder)
        launcher.launch(self, None, None)

    def _on_size_changed(self, *_) -> None:
        # Debounce: only write session after 500 ms of no resize events (#96)
        if self._size_save_source is not None:
            GLib.source_remove(self._size_save_source)
        self._size_save_source = GLib.timeout_add(500, self._flush_window_state)

    def _flush_window_state(self) -> bool:
        self._size_save_source = None
        save_window_state({
            "width":     self.get_width(),
            "height":    self.get_height(),
            "maximized": self.is_maximized(),
        })
        return GLib.SOURCE_REMOVE

    # ── Autosave ──────────────────────────────────────────────────────────────

    def _autosave(self) -> bool:
        if self._modified and self._editor.get_text():
            try:
                rd = recovery_dir()
                rd.mkdir(parents=True, exist_ok=True)
                if self._file_path:
                    rp = recovery_path_for(self._file_path)
                else:
                    rp = rd / "untitled.md"
                rp.write_text(self._editor.get_text(), encoding="utf-8")
                # Brief toast so users know their work is protected (#29)
                self._show_toast("Autosaved", timeout=2)
            except OSError as e:
                log.warning("Autosave failed: %s", e)
        return GLib.SOURCE_CONTINUE

    # ── Word count ────────────────────────────────────────────────────────────

    def _update_word_count(self, text: str) -> None:
        words   = len(re.findall(r'\S+', text))
        # Speaking rate is user-configurable (default 110 WPM)
        minutes = max(1, round(words / self._speaking_rate))
        time_str = (f"{minutes} min to present" if minutes < 60
                    else f"{minutes // 60}h {minutes % 60}m")
        if not self._spinner.get_visible():
            self._title_label.set_subtitle(f"{words} words · ~{time_str}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_title(self, name: str) -> None:
        self.set_title(f"{name} — Presence")
        self._title_label.set_title(name)

    def _show_error(self, message: str) -> None:
        """Show a persistent error in the banner (stays until dismissed)."""
        self._banner.set_title(message)
        self._banner.set_revealed(True)

    def _show_toast(self, message: str, timeout: int = 4) -> None:
        """Show a transient one-shot error as a toast (auto-dismisses)."""
        toast = Adw.Toast(title=message)
        toast.set_timeout(timeout)
        self._toast_overlay.add_toast(toast)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _friendly_error(message: str) -> str:
    """
    Map known library exception messages to user-friendly strings (#87).
    Falls back to a generic message to avoid exposing Python internals.
    """
    msg = message.lower()
    if "no slides found" in msg:
        return "No slides found — add at least one slide separator (---) to your document."
    if "unknown theme" in msg:
        return "Unknown theme — check your frontmatter 'theme:' value in Settings."
    if "unknown ratio" in msg:
        return "Unknown aspect ratio — check your frontmatter 'ratio:' value in Settings."
    if "weasyprint" in msg or "importerror" in msg:
        return "WeasyPrint is not installed — PDF conversion is unavailable."
    if "no such file" in msg or "filenotfounderror" in msg:
        return "A required file was not found. Check your logo or custom_css paths."
    return "Conversion failed — check your Markdown for errors."
