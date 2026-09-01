"""
window.py — Main application window.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib, Gdk, Pango

from .editor     import Editor
from .sidebar    import Sidebar, sidebar_width
from .theme_panel    import ThemePanel
from .inspector      import Inspector
from .settings_dialog import SettingsDialog
from .app_utils        import png_bytes_to_texture, make_file_filter, make_filter_store

log = logging.getLogger(__name__)
from .converter  import Converter
from .export_controller import ExportController
from .document_controller import DocumentController
from .build_coordinator import BuildCoordinator
from .presenter  import PresenterWindow
from .shortcuts  import build_shortcuts_window
from .session    import (save_last_file, load_window_state, save_window_state,
                          load_editor_prefs, save_editor_prefs, save_recent_file,
                          load_recent_files, delete_recovery_file,
                          load_presentation_prefs, save_presentation_prefs)
from .session    import recovery_path_for, recovery_dir
from .slides.themes import ASPECT_RATIOS
from .slides.splitter import split_slides
from .slides.script import document_timing
from .slides.frontmatter import parse_frontmatter, raw_frontmatter
from .slides.thumbnails_render import rasterizer_available


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


class _BusyIndicator:
    """
    A header button whose icon becomes a spinner while it is working.

    Present and Export can both start work that does not finish on the click:
    a build has to run first, and an image or handout export then rasterizes
    the whole deck.  Until now the button went insensitive and the only sign
    of life was the build chip at the other end of the header — the control
    the writer pressed looked broken rather than busy.

    Waits are counted rather than flagged, because an export takes one for the
    build and another for the render and the two overlap; the button has to
    stay busy until the last of them lets go.
    """

    def __init__(self, button: Gtk.Widget, icon_name: str) -> None:
        self._button  = button
        self._spinner = Gtk.Spinner()
        self._stack   = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(Gtk.Image.new_from_icon_name(icon_name), "idle")
        self._stack.add_named(self._spinner, "busy")
        button.set_child(self._stack)
        self._waits = 0

    @property
    def busy(self) -> bool:
        return self._waits > 0

    def __call__(self, busy: bool) -> None:
        """Take or release one wait — usable directly as an on_wait callback."""
        self._waits = max(0, self._waits + (1 if busy else -1))
        if self._waits:
            self._spinner.start()
            self._stack.set_visible_child_name("busy")
        else:
            self._stack.set_visible_child_name("idle")
            self._spinner.stop()
        self._button.set_sensitive(not self._waits)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("Presence")

        state = load_window_state()
        self.set_default_size(state.get("width", 1400), state.get("height", 860))
        if state.get("maximized"):
            self.maximize()
        self._theme_panel_open: bool = state.get("theme_panel_visible", False)
        self._thumbnail_size:   int  = state.get("thumbnail_size", 320)

        # Where the document is and what the build made of it are owned by
        # the two controllers below, not by this window.
        #
        # Strong reference to any active Gtk.FileDialog to prevent GC collection
        # before the user completes the async operation. Cleared in each callback.
        self._active_file_dialog = None
        # Debounce source for window-size saves (#96)
        self._size_save_source: int | None = None
        # Debounce source for sidebar update from text (#37)
        self._sidebar_update_source: int | None = None
        # The document the slide now being rendered was read from.
        self._live_render_text: str = ""
        # Cursor-position polling source for sidebar sync (#28)
        self._cursor_sync_source: int | None = None
        # Cache for cursor-sync: avoid re-parsing unchanged text every 300ms
        self._cursor_sync_cache: tuple[str, list[int]] | None = None
        # Slide the cursor is currently in — drives the live render
        self.current_slide: int = 0
        # Debounce sources for the thumbnail-size slider: one to save the
        # setting, one to re-render the slide under the cursor at the new
        # size.  Both are deferred so dragging stays smooth.
        self._thumb_size_source: int | None = None
        self._thumb_render_source: int | None = None
        # Whether slides can be rasterized at all, so a machine without
        # Poppler or Cairo does not ask on every keystroke.
        self._can_rasterize: bool = rasterizer_available()
        # What the inspector is currently showing, so a keystroke that
        # changes neither does not restart its thumbnail render.
        self._panel_shown_theme: str = ""
        self._panel_shown_ratio: str = ""

        # Built before the UI, because the header bar wires its chip and its
        # buttons straight to them.
        self.exports   = ExportController(self)
        self.documents = DocumentController(self)
        self.builds    = BuildCoordinator(self)

        prefs = load_editor_prefs()

        self.converter = Converter(
            theme=prefs.get("theme", "light"),
            ratio=prefs.get("ratio", "16:9"),
            logo_path=Path(prefs["logo"]) if prefs.get("logo") else None,
        )
        self.converter.connect("conversion-started",  self._on_conversion_started)
        self.converter.connect("conversion-complete", self._on_conversion_complete)
        self.converter.connect("conversion-failed",   self._on_conversion_failed)

        # Presentation preferences — must be set before _build_ui() so that
        # ThemePanel.attach() → _sync_controls() can read _timer_minutes.
        pres_prefs = load_presentation_prefs()
        self.auto_convert: bool = pres_prefs.get('auto_convert', False)
        self._timer_minutes: int = pres_prefs.get('timer_minutes', 0)
        # Speaking rate in WPM — default 110, persisted to session.json
        self.speaking_rate: int = pres_prefs.get('speaking_rate', 110)
        # Notes font size in presenter mode — default 22px, persisted on
        # the window so PresenterWindow can read and write it.
        self._presenter_notes_font: int = pres_prefs.get(
            'presenter_notes_font', 22
        )

        self._build_ui()
        self._setup_actions()
        self._setup_recent_actions()
        # Attach ThemePanel now that converter is available (#attach needs it)
        self._theme_panel.attach(self, self.converter)

        # Restore persisted panel visibility — done after _build_ui so the
        # revealer and button already exist.  Starts hidden by default so the
        # panel doesn't inflate the window's minimum width at startup.
        if self._theme_panel_open:
            self._theme_panel_btn.set_active(True)

        self.sidebar.set_speaking_rate(self.speaking_rate)
        # notify=False: this is reading the stored setting, not changing it.
        self.sidebar.set_thumbnail_size(self._thumbnail_size, notify=False)
        self._apply_sidebar_width()

        # Apply persisted editor preferences (#50 + editor tab)
        self.editor.set_font_size(prefs.get("font_size", 13))
        self.editor.set_syntax_highlight(prefs.get("syntax_highlight", True))
        self.editor.set_line_numbers(prefs.get("line_numbers", True))
        self.editor.set_highlight_current_line(prefs.get("highlight_line", True))
        self.editor.set_auto_indent(prefs.get("auto_indent", True))
        self.editor.set_spaces_instead_of_tabs(prefs.get("spaces_tabs", True))
        self.editor.set_line_length(prefs.get("line_length", 64))
        # Focus mode is a mode, not a setting, but it still outlives the
        # window: the app should open the way it was left.
        self._set_focus_mode(prefs.get("focus_mode", False), persist=False)

        self._autosave_source: int | None = GLib.timeout_add_seconds(30, self._autosave)

        # Poll cursor position every 300 ms to keep sidebar in sync (#28)
        self._cursor_sync_source = GLib.timeout_add(300, self._sync_sidebar_to_cursor)

        self.connect("notify::default-width",  self._on_size_changed)
        self.connect("notify::default-height", self._on_size_changed)
        self.connect("notify::maximized",      self._on_size_changed)
        self.connect("close-request",          self._on_close_request)
        self.connect("destroy",                self._on_destroy)

        self._update_build_chip()

    # ── What application.py reads ─────────────────────────────────────────────
    #
    # application.py is mode 444 and asks a window whether it is holding a
    # document before reusing it.  Read-only, because the document controller
    # is the one that moves either of them.

    @property
    def _file_path(self) -> "Path | None":
        return self.documents.file_path

    @property
    def _modified(self) -> bool:
        return self.documents.modified

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # AdwToolbarView must be the direct content of AdwApplicationWindow so
        # that libadwaita can integrate header bars with the window chrome.
        # Nesting it inside AdwToastOverlay breaks this integration and causes
        # a gtk_box_append critical from libadwaita's internal layout code.
        root = Adw.ToolbarView()
        self.set_content(root)

        root.add_top_bar(self._build_header())

        # Use Adw.Banner only for persistent app-level messages (#20)
        self.banner = Adw.Banner(title="")
        self.banner.set_button_label("Close")   # HIG standard label (#23)
        self.banner.set_revealed(False)
        self.banner.connect("button-clicked", lambda *_: self.banner.set_revealed(False))
        root.add_top_bar(self.banner)

        self.sidebar     = Sidebar()
        self.editor      = Editor()
        self._theme_panel = ThemePanel()
        self._inspector = Inspector(self._theme_panel)

        self.sidebar.connect("slide-selected",       self._on_slide_selected)
        self.sidebar.connect("slide-insert-after",   self._on_slide_insert_after)
        self.sidebar.connect("slides-reordered",     self._on_slides_reordered)
        self.sidebar.connect("thumbnail-size-changed", self._on_thumbnail_size)
        self.editor.connect_undo_notify(self._on_undo_state_changed)
        self.editor.set_insert_image_callback(self._on_insert_image)
        self.editor.connect("changed",               self._on_editor_changed)
        self.editor.connect("live-changed",          self._on_editor_live_changed)
        self.editor.connect("notify-user",           self._on_editor_notify_user)
        self._theme_panel.connect("rebuild-needed",   self._on_theme_panel_rebuild)
        self._theme_panel.connect("theme-changed",    self._on_panel_theme_changed)
        self._theme_panel.connect("ratio-changed",    self._on_panel_ratio_changed)

        # Right sidebar: theme panel shown inline via a Revealer.
        # Using a Revealer (not a nested OverlaySplitView) avoids the overlay
        # clipping issues that arise when OverlaySplitView is used as the
        # *content* of another OverlaySplitView.
        self._theme_sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self._theme_revealer = Gtk.Revealer()
        self._theme_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self._theme_revealer.set_reveal_child(False)
        # set_visible(False) removes the revealer from layout negotiation entirely,
        # so the ThemePanel's set_size_request(300) doesn't inflate the window's
        # minimum width at startup.  set_reveal_child alone only clips rendering.
        self._theme_revealer.set_visible(False)
        self._theme_revealer.set_hexpand(False)
        self._theme_revealer.connect(
            "notify::child-revealed", self._on_theme_revealer_state_changed
        )
        panel_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        panel_box.append(self._theme_sep)
        panel_box.append(self._inspector)
        self._theme_revealer.set_child(panel_box)

        # The window is the source and the strip beside it.  There was a
        # second pane here showing the slide under the cursor at reading
        # size, which is now what the strip itself does — at whatever size
        # the writer sets — so the editor gets the width back.
        self.editor.set_size_request(360, -1)
        self.editor.set_hexpand(True)
        self.editor.set_vexpand(True)

        editor_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        editor_row.append(self.editor)
        editor_row.append(self._theme_revealer)

        # Left sidebar: thumbnail strip, collapses to drawer on narrow windows.
        self._left_split = Adw.OverlaySplitView()
        self._left_split.set_sidebar_position(Gtk.PackType.START)
        self._left_split.set_sidebar(self.sidebar)
        self._left_split.connect(
            "notify::show-sidebar", self._on_left_split_show_changed
        )

        self._left_split.set_content(editor_row)

        # Wrap main content in ToastOverlay so theme-install toasts work (#2).
        # Placed inside ToolbarView's content area (not wrapping the ToolbarView)
        # so toasts appear over the content but not over header bars.
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._left_split)
        root.set_content(self._toast_overlay)

        # Below this there is not enough room for the source and the strip
        # side by side, so the strip becomes a drawer and the chip drops its
        # label.  The threshold follows the chosen thumbnail size, because
        # what matters is the room left for the text rather than the width of
        # the window; _apply_sidebar_width() moves it when the slider does.
        self._narrow_bp = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 1px")
        )
        self._narrow_bp.add_setter(self._left_split, "collapsed", True)
        # Header gets crowded; the chip's icon still carries the state.
        self._narrow_bp.add_setter(self._chip_label, "visible", False)
        self.add_breakpoint(self._narrow_bp)
        self._apply_sidebar_width()

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

        # New, Save, Save as, Undo and Redo used to sit here.  Every one of
        # them has a universal keystroke and a menu entry, and GNOME does not
        # want them in the header; opening a document is the one thing here
        # that a newcomer cannot guess a shortcut for.
        open_btn = Gtk.Button()
        open_btn.set_child(Gtk.Image.new_from_icon_name("document-open-symbolic"))
        open_btn.set_tooltip_text("Open… (Ctrl+O)")
        open_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Open"])
        open_btn.add_css_class("flat")
        open_btn.connect("clicked", self._on_open)
        bar.pack_start(open_btn)

        self._title_label = Adw.WindowTitle(title="Untitled", subtitle="")
        bar.set_title_widget(self._title_label)

        self._build_chip = self._build_status_chip()

        # Theme panel toggle — right sidebar visibility (F10)
        self._theme_panel_btn = Gtk.ToggleButton()
        self._theme_panel_btn.set_icon_name("sidebar-show-right-symbolic")
        self._theme_panel_btn.set_tooltip_text("Show inspector (F10)")
        self._theme_panel_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Show inspector"]
        )
        self._theme_panel_btn.set_active(False)
        self._theme_panel_btn.add_css_class("flat")
        self._theme_panel_btn.connect("toggled", self._on_theme_panel_toggled)
        bar.pack_end(self._theme_panel_btn)

        # Export — one verb, with the format as the choice inside it.  This
        # was "Share / export" over a popover that mixed three formats with
        # opening the working PDF and revealing its folder; those are about
        # the build rather than about handing a deck to someone, and have
        # moved to the menu.
        self._share_btn = Gtk.MenuButton()
        self._share_btn.set_icon_name("document-send-symbolic")
        self._share_btn.set_tooltip_text("Export… (Ctrl+P for PDF)")
        self._share_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Export"]
        )
        self._share_btn.add_css_class("flat")
        self._share_btn.set_popover(self._build_share_popover())
        # Enabled from the start.  Every format routes through
        # _with_current_build(), which builds first when the deck has moved
        # on, so there is nothing left for a greyed-out button to protect
        # against — only a control that looked broken until a build happened.
        self.export_busy = _BusyIndicator(
            self._share_btn, "document-send-symbolic"
        )
        bar.pack_end(self._share_btn)

        # Present button — converts (if needed) then opens presenter mode.
        self.present_button = Gtk.Button()
        self._present_busy = _BusyIndicator(
            self.present_button, "media-playback-start-symbolic"
        )
        self.present_button.set_tooltip_text("Present (F5)")
        self.present_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Present"]
        )
        self.present_button.add_css_class("suggested-action")
        self.present_button.connect("clicked", self._on_present_clicked)
        bar.pack_end(self.present_button)

        # Packed last so it sits leftmost of the end group, beside Present:
        # it describes the state of what Present and Share act on.
        bar.pack_end(self._build_chip)

        return bar

    def _build_status_chip(self) -> Gtk.Button:
        """
        The one place that says whether the build matches the document.

        Replaces a bare spinner plus a toast on every successful build: state
        you can miss and a notification for something routine.  Clicking it
        rebuilds, so the indicator and its remedy are the same control.
        """
        icon    = Gtk.Image.new_from_icon_name("object-select-symbolic")
        spinner = Gtk.Spinner()

        visual = Gtk.Stack()
        visual.add_named(icon, "icon")
        visual.add_named(spinner, "spinner")

        # Kept on the window only because the breakpoint hides it at narrow
        # widths; everything it *says* comes from the coordinator.
        self._chip_label = Gtk.Label(label="Up to date")
        self._chip_label.add_css_class("caption")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(visual)
        box.append(self._chip_label)

        chip = Gtk.Button()
        chip.set_child(box)
        chip.add_css_class("flat")
        chip.connect("clicked", self.builds.trigger)
        self.builds.attach_chip(chip, visual, icon, spinner, self._chip_label)
        return chip

    def _build_share_popover(self) -> Gtk.Popover:
        """
        Build the Export popover: the three formats, and nothing else.

        A boxed list of Adw.ActionRows.  These rows were hand-built out of
        flat Gtk.Buttons carrying an icon and two stacked labels, in the
        shape of an ActionRow — under a comment saying that was the HIG
        pattern.  Using the row itself brings the styling, the row height,
        the activation behaviour and the accessible role with it.
        """
        popover = Gtk.Popover()
        popover.set_has_arrow(True)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        listbox.set_size_request(260, -1)
        listbox.set_margin_top(6)
        listbox.set_margin_bottom(6)
        listbox.set_margin_start(6)
        listbox.set_margin_end(6)

        def _row(icon: str, label: str, subtitle: str, cb) -> None:
            row = Adw.ActionRow(title=label, subtitle=subtitle)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            row.set_activatable(True)
            row.connect("activated", lambda *_: (popover.popdown(), cb()))
            listbox.append(row)

        _row("document-save-symbolic", "PDF…",
             "The deck as a PDF file", self._on_export)
        _row("text-x-generic-symbolic", "HTML…",
             "A self-contained web page", self._on_export_html)
        _row("image-x-generic-symbolic", "Images…",
             "One PNG per slide, into a folder", self._on_export_images)
        _row("view-paged-symbolic", "Handout…",
             "Your slides and script, to read or print", self._on_export_handout)

        popover.set_child(listbox)
        return popover

    def _build_app_menu(self) -> Gio.Menu:
        """
        The menu now holds everything the header stopped showing.

        Two verbs act on the document — Save writes the Markdown, Export
        writes a deck someone else can open — and the build that sits between
        them is not a concept the menu mentions.
        """
        menu = Gio.Menu()

        s0 = Gio.Menu()
        s0.append("New window",  "win.new")
        s0.append("Open…",       "win.open")
        s0.append("Save",        "win.save")
        s0.append("Save as…",    "win.save-as")
        menu.append_section(None, s0)

        s1 = Gio.Menu()
        s1.append("Undo", "win.undo")
        s1.append("Redo", "win.redo")
        menu.append_section(None, s1)

        # A mode you turn on while writing, not a preference you configure
        # once, so it belongs in the menu rather than in Settings.  The
        # action is stateful, which is what draws this as a check item.
        s1b = Gio.Menu()
        s1b.append("Focus mode", "win.focus-mode")
        menu.append_section(None, s1b)

        export_menu = Gio.Menu()
        export_menu.append("PDF…",    "win.export")
        export_menu.append("HTML…",   "win.export-html")
        export_menu.append("Images…",  "win.export-images")
        export_menu.append("Handout…", "win.export-handout")
        menu.append_submenu("Export", export_menu)

        s3 = Gio.Menu()
        s3.append("Open the built PDF", "win.open-pdf")
        s3.append("Show output folder", "win.show-output")
        s3.append("Copy PDF path",      "win.copy-pdf-path")
        menu.append_section(None, s3)

        self._recent_menu = Gio.Menu()
        self._rebuild_recent_menu()
        menu.append_submenu("Recent files", self._recent_menu)

        s4 = Gio.Menu()
        s4.append("Keyboard shortcuts", "win.shortcuts")
        s4.append("Settings…",          "app.preferences")
        s4.append("About",              "app.about")
        menu.append_section(None, s4)
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
        # Accelerators are lists: a standard key GNOME reserves for a verb
        # goes first, and any older binding this app taught follows it.
        actions = [
            ("new",      self._on_new,            ["<primary>n"]),
            ("open",     self._on_open,            ["<primary>o"]),
            ("save",     self._on_save,            ["<primary>s"]),
            ("save-as",  self._on_save_as,         ["<primary><shift>s"]),
            # Ctrl+Return still triggers a manual rebuild for power users
            ("convert",  self._trigger_convert,    ["<primary>Return"]),
            ("undo",         lambda *_: self.editor.undo(),              ["<primary>z"]),
            ("redo",         lambda *_: self.editor.redo(),              ["<primary><shift>z"]),
            ("find",         lambda *_: self.editor.show_find(),         ["<primary>f"]),
            ("find-replace", lambda *_: self.editor.show_find_replace(), ["<primary>h"]),
            # Ctrl+P is the system's Print, and a deck printed to a file is
            # exactly what Export PDF writes; it used to open the presenter,
            # which left the nearest thing to Print on Ctrl+Shift+E alone.
            # That binding is kept — it is what this app taught.
            ("export",       self._on_export,          ["<primary>p", "<primary><shift>e"]),
            ("export-html",   self._on_export_html,                       None),
            ("export-images", self._on_export_images,                     None),
            ("export-handout", self._on_export_handout,                    None),
            ("open-pdf",      self._on_open_pdf_clicked,                  None),
            ("show-output",   self._on_show_in_file_manager,              None),
            ("copy-pdf-path", self._on_copy_pdf_path,                     None),
            # Through _on_present_clicked, not _on_presenter: the shortcut
            # used to open the presenter against whatever the last build
            # left behind, while the button beside it built first.  F5 is
            # what every other presentation tool starts a slideshow with.
            ("presenter",    self._on_present_clicked,                    ["F5"]),
            # Ctrl+? is the HIG's key for this; F1 is Help, and stays bound
            # here only because Presence ships no help manual for it to open.
            ("shortcuts",    self._on_shortcuts,          ["<primary>question", "F1"]),
            ("insert-image",   self._on_insert_image,   None),
            ("insert-comment", self._on_insert_comment, None),
            ("bold",   lambda *_: self.editor.bold(),         ["<primary>b"]),
            ("italic", lambda *_: self.editor.italic(),       ["<primary>i"]),
            ("link",   lambda *_: self.editor.insert_link(),  ["<primary>k"]),
            # Panel toggles: slides F9, themes F10 (#75)
            ("toggle-sidebar",       self._on_toggle_sidebar,       ["F9"]),
            ("toggle-theme-panel",   self._on_toggle_theme_panel,   ["F10"]),
        ]
        for name, cb, accels in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            if accels:
                self.get_application().set_accels_for_action(
                    f"win.{name}", accels
                )

        # app.preferences is the Application's action, but only a window is
        # ever around to bind it; Ctrl+comma is the GNOME-wide key for it.
        self.get_application().set_accels_for_action(
            "app.preferences", ["<primary>comma"]
        )

        # Focus mode carries state so the menu can draw it checked, which
        # the plain actions above cannot do.
        focus = Gio.SimpleAction.new_stateful(
            "focus-mode", None, GLib.Variant.new_boolean(False)
        )
        focus.connect("activate", self._on_focus_mode)
        self.add_action(focus)
        self.get_application().set_accels_for_action(
            "win.focus-mode", ["<primary><shift>f"]
        )

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

    def refresh_recent_actions(self) -> None:
        for i in range(8):
            self.remove_action(f"open-recent-{i}")
        self._setup_recent_actions()
        self._rebuild_recent_menu()

    # ── Unsaved changes ───────────────────────────────────────────────────────

    def _on_close_request(self, win) -> bool:
        if not self.documents.modified:
            self._shut_down()
            return False

        # Torn down only once the window is really going: cancelling the
        # question — or cancelling the Save As chooser behind it — leaves a
        # window the writer keeps typing in, and it must keep autosaving.
        self.documents.show_unsaved_dialog(
            on_save=self._close_now,
            on_discard=self._close_now,
        )
        return True

    def _close_now(self) -> None:
        """Stop the window's timers and close it for good."""
        self._shut_down()
        self.destroy()

    def _shut_down(self) -> None:
        """Release everything that outlives a closed window."""
        self.converter.stop_watch()
        for attr in ("_autosave_source", "_size_save_source",
                     "_sidebar_update_source", "_cursor_sync_source",
                     "_thumb_size_source", "_thumb_render_source"):
            src = getattr(self, attr, None)
            if src is not None:
                GLib.source_remove(src)
                setattr(self, attr, None)
        self.documents.shut_down()
        # Always clean up temp files, regardless of modified state (#44)
        self.builds.cleanup_scratch()

    def _check_unsaved(self, action) -> None:
        self.documents.check_unsaved(action)

    # ── File operations ───────────────────────────────────────────────────────

    def open_file(self, path: Path) -> None:
        self.documents.open_file(path)

    def restore_autosave(self, text: str) -> None:
        self.documents.restore_autosave(text)

    def _save(self, on_done=None) -> bool:
        return self.documents.save(on_done=on_done)

    def _write_document(self, on_done=None) -> bool:
        return self.documents.write_document(on_done=on_done)

    def _save_as_dialog(self, on_done=None) -> bool:
        return self.documents.save_as_dialog(on_done=on_done)

    # ── Conversion ────────────────────────────────────────────────────────────

    def _trigger_convert(self, *_) -> None:
        self.builds.trigger()

    # ── .pres bundle support ──────────────────────────────────────────────────
    #
    # Extracting, re-packing and cleaning up a bundle is all about where the
    # document is, so it lives in DocumentController.  What is left here is
    # the window's own destroy signal.

    def _on_destroy(self, *_) -> None:
        self.documents.discard_pres_temp()

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _on_editor_changed(self, editor: Editor, text: str) -> None:
        self._mark_modified()
        # Debounce the sidebar parse: run 200 ms after the last keystroke so
        # we do not parse the full document on every character (#37).
        self._debounce("_sidebar_update_source", 200, self._flush_sidebar_update, text)
        self.update_word_count(text)
        self._update_build_chip()

    def _flush_sidebar_update(self, text: str) -> bool:
        self._sidebar_update_source = None
        self.sidebar.update_from_text(text)
        self.sync_panel_to_document(text)
        return GLib.SOURCE_REMOVE

    def sync_panel_to_document(self, text: str) -> None:
        """
        Show the inspector the theme and ratio this document renders at.

        Frontmatter outranks the app's own setting — `_render_context()`
        reads `meta.get("theme", self.theme)` — so a deck carrying
        `theme: berlin` comes out berlin whatever this machine last chose,
        while the panel went on showing this machine's choice.  Which meant
        the panel could name one theme and the slides beside it be another.

        Display only: `select_theme()` and `select_ratio()` set the controls
        without touching the converter or emitting, so nothing here starts a
        build.  Both are skipped when the value has not moved, because
        showing a theme re-renders its preview thumbnail.
        """
        try:
            meta, _ = parse_frontmatter(text)
        except Exception:            # a half-typed block is not an error here
            meta = {}

        theme = str(meta.get("theme") or self.converter.theme)
        if theme != self._panel_shown_theme and self._theme_panel.has_theme(theme):
            self._panel_shown_theme = theme
            self._theme_panel.select_theme(theme)

        ratio = str(meta.get("ratio") or self.converter.ratio)
        if ratio != self._panel_shown_ratio:
            self._panel_shown_ratio = ratio
            self._theme_panel.select_ratio(ratio)

    # ── The live slide ────────────────────────────────────────────────────────

    def _on_editor_live_changed(self, editor: Editor, text: str) -> None:
        """Editor settled for 150 ms — re-render the slide under the cursor."""
        self.refresh_live_slide(text)

    def _document_base_dir(self) -> Path:
        """Directory relative image paths in the document resolve against."""
        return self.documents.base_dir or Path.home()

    def _apply_sidebar_width(self) -> None:
        """
        Size the strip to the chosen thumbnail size, and move the breakpoint.

        Both bounds are set to the same number so the strip is exactly that
        wide: OverlaySplitView otherwise sizes its sidebar as a fraction of
        the window and the maximum alone would not widen it.
        """
        width = sidebar_width(self.sidebar.thumbnail_width)
        self._left_split.set_min_sidebar_width(width)
        self._left_split.set_max_sidebar_width(width)
        # Collapse to a drawer once the editor would be left under ~540px.
        self._narrow_bp.set_condition(
            Adw.BreakpointCondition.parse(f"max-width: {width + 540}px")
        )

    def _live_render_width(self) -> int | None:
        """
        Width to rasterize the slide under the cursor at, or None for nobody.

        The strip is the only thing that shows it now, so this is its
        thumbnail size in device pixels — and None whenever the strip is put
        away or the machine cannot rasterize, so a writer who has hidden it
        pays nothing for it.
        """
        if not self._can_rasterize or not self._left_split.get_show_sidebar():
            return None
        return self.sidebar.thumbnail_width * max(1, self.get_scale_factor())

    def refresh_live_slide(self, text: str | None = None) -> None:
        """
        Ask for the slide under the cursor to be re-rendered for the strip.

        The render runs on a background thread and lands in _on_live_frame;
        requests coalesce, so typing quickly never queues stale frames.
        """
        width = self._live_render_width()
        if width is None:
            return

        if text is None:
            text = self.editor.get_text()

        # Kept so the frame that comes back can be matched to the words it
        # was laid out from; a request that is superseded never arrives, so
        # whatever does arrive belongs to this text.
        self._live_render_text = text
        self.converter.render_slide_async(
            text, self._document_base_dir(), self.current_slide,
            width, self._on_live_frame,
        )

    def _on_live_frame(self, frame, error) -> None:
        """Receive a rendered slide on the main thread and hand it to the strip."""
        if error is not None:
            # Nothing is shown for this: a slide that will not render leaves
            # its row marked out of date, which is true and is already
            # visible, and a banner raised on every keystroke while a slide
            # is halfway typed would be noise.  A real fault in the document
            # is reported by the build, which is where it can be acted on.
            log.debug("Live slide render failed", exc_info=error)
            return

        # Bring the strip up to the text this frame was rendered from before
        # handing the picture over.  The strip's text update is debounced
        # twice — the editor settles, then the window does — so it reliably
        # lands after a render, and a picture applied before it would be
        # marked out of date again by the words catching up behind it.  This
        # also lines the row indices up with the slides the frame counted.
        if self._sidebar_update_source is not None:
            GLib.source_remove(self._sidebar_update_source)
        self._flush_sidebar_update(self._live_render_text)
        # This slide was laid out by the same engine the build uses, so its
        # fold is as authoritative as the build's — and it is fresher.  The
        # strip gets the picture for the same reason: the row being edited
        # need not wait for a build to stop being out of date.
        self.sidebar.set_live_slide(frame.index, frame.png, frame.fold_line)
        self._set_live_fold(frame.index, frame.fold_line)

    def _set_build_folds(self, folds: list) -> None:
        self.builds.set_build_folds(folds)

    def _set_live_fold(self, index: int, fold_line) -> None:
        self.builds.set_live_fold(index, fold_line)

    def _on_thumbnail_size(self, _sidebar, width: int) -> None:
        """
        The size slider moved: widen the strip, then catch up behind it.

        The resize itself is immediate — the pictures are already rasterized
        larger than the slider can ask for, so this is a relayout.  The two
        slower consequences are deferred so a drag stays smooth: writing the
        setting down, and re-rendering the slide under the cursor, whose
        picture came from the live path at the old size rather than from the
        build's oversized one.
        """
        self._thumbnail_size = width
        self._apply_sidebar_width()
        self._debounce("_thumb_size_source", 400, self._flush_thumbnail_size)
        self._debounce("_thumb_render_source", 250, self._flush_thumbnail_render)

    def _flush_thumbnail_size(self) -> bool:
        self._thumb_size_source = None
        self._save_window_state()
        return GLib.SOURCE_REMOVE

    def _flush_thumbnail_render(self) -> bool:
        self._thumb_render_source = None
        self.refresh_live_slide()
        return GLib.SOURCE_REMOVE

    def _sync_sidebar_to_cursor(self) -> bool:
        """
        Poll the editor cursor position every 300 ms and highlight the
        matching slide in the sidebar (#28).

        Caches the parsed slide-offset list so we only re-parse the document
        when the text has actually changed — avoids calling split_slides()
        3× per second on every keystroke.

        The same position drives the live render, so the strip's row for the
        slide the cursor is in is the one kept current.
        """
        try:
            from .slides.utils import compute_slide_offsets
            text   = self.editor.get_text()
            offset = self.editor.get_cursor_offset()

            # Re-parse only when text changed since last poll
            if (self._cursor_sync_cache is None
                    or self._cursor_sync_cache[0] != text):
                slide_offsets = compute_slide_offsets(text)
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

            # No longer gated on a build: the strip is read out of the text,
            # so it has rows to select from the first keystroke.
            self.sidebar.scroll_to_index(current)

            # Follow the cursor across slide boundaries.  Edits within one
            # slide are handled by the live-changed signal instead.
            if current != self.current_slide:
                self.current_slide = current
                self.refresh_live_slide(text)
        except Exception:
            log.debug("Cursor sync error", exc_info=True)
        return GLib.SOURCE_CONTINUE

    def _on_undo_state_changed(self, can_undo: bool, can_redo: bool) -> None:
        # The buttons are gone; the menu items grey out via their actions.
        for name, enabled in (("undo", can_undo), ("redo", can_redo)):
            action = self.lookup_action(name)
            if action is not None:
                action.set_enabled(enabled)

    def _on_slide_selected(self, sidebar: Sidebar, index: int) -> None:
        self.editor.scroll_to_slide(index)
        # Re-render now rather than waiting for the cursor poll — a click
        # should land on the slide immediately.
        self.current_slide = index
        self.refresh_live_slide()

    def _on_slide_insert_after(self, sidebar: Sidebar, after_index: int) -> None:
        """
        Insert a blank slide immediately after *after_index* (#90).

        The new slide is inserted into the editor buffer as a user action
        (undoable) and the sidebar + title are updated immediately.
        """
        text = self.editor.get_text()
        _meta, body = parse_frontmatter(text)
        slides = split_slides(body)

        # Clamp to valid range
        insert_at = max(0, min(after_index + 1, len(slides)))

        blank = "## New slide\n\nYour content here."
        slides.insert(insert_at, blank)
        new_body = "\n\n---\n\n".join(slides)

        fm = raw_frontmatter(text)
        new_text = (fm + "\n\n" + new_body) if fm else new_body

        self.editor.set_text_as_user_action(new_text)
        self._mark_modified()
        self.sidebar.update_from_text(new_text)
        self.current_slide = insert_at
        self.refresh_live_slide(new_text)
        self._update_build_chip()
        # Scroll editor to the newly inserted slide.  Spelled out rather than
        # a lambda returning a tuple: that returned (None, False), and it only
        # stopped repeating because PyGObject cannot make a gboolean out of a
        # tuple and falls back to false.
        def _scroll_to_the_new_slide() -> bool:
            self.editor.scroll_to_slide(insert_at)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(_scroll_to_the_new_slide)

    def _on_slides_reordered(self, sidebar: Sidebar, from_idx: int, to_idx: int) -> None:
        text = self.editor.get_text()
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
        self.editor.set_text_as_user_action(new_text)
        self._mark_modified()
        self.sidebar.update_from_text(new_text)
        self.refresh_live_slide(new_text)
        self._update_build_chip()

    def _with_current_build(self, action, on_wait=None) -> None:
        self.builds.with_current_build(action, on_wait)

    def _on_present_clicked(self, *_) -> None:
        """Build if the deck has moved on, then open presenter mode."""
        self._with_current_build(self._on_presenter, on_wait=self._present_busy)

    def _on_open_pdf_clicked(self, *_) -> None:
        self._with_current_build(self._open_built_pdf)

    def _open_built_pdf(self) -> None:
        if not (self.documents.output_path and self.documents.output_path.exists()):
            return
        pdf_path = self.documents.output_path
        if os.environ.get("FLATPAK_ID"):
            # /tmp inside the Flatpak sandbox is a private tmpfs — the host
            # sees a different /tmp.  Use the XDG cache dir instead: its path
            # (~/.var/app/<id>/cache/) is identical inside the sandbox and on
            # the host, so flatpak-spawn --host xdg-open can reach the file.
            cache_dir = Path(GLib.get_user_cache_dir())
            cache_dir.mkdir(parents=True, exist_ok=True)
            host_pdf = cache_dir / "presentation-preview.pdf"
            try:
                shutil.copy2(pdf_path, host_pdf)
                os.chmod(host_pdf, 0o644)
                pdf_path = host_pdf
            except OSError:
                pass
            # Run xdg-open on the HOST via flatpak-spawn, bypassing the portal.
            # The OpenURI portal (used by Gtk.FileLauncher) is unreliable on
            # some GNOME installations and returns "application launch failed".
            try:
                subprocess.Popen(
                    ["flatpak-spawn", "--host", "xdg-open", str(pdf_path)]
                )
                return
            except OSError:
                pass
            # flatpak-spawn unavailable — copy to Documents as last resort.
            self._copy_pdf_to_documents(pdf_path)
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(pdf_path)))
        launcher.launch(self, None, self._on_pdf_launch_finish)

    def _copy_pdf_to_documents(self, src: Path) -> None:
        docs = Path(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS))
        dest = docs / "presentation.pdf"
        try:
            shutil.copy2(src, dest)
            self.show_toast(
                "PDF viewer unavailable — PDF saved to Documents/presentation.pdf",
                timeout=8,
            )
        except OSError:
            self.show_error(
                f"Could not open a PDF viewer.\n\nThe PDF is at:\n{self.documents.output_path}"
            )

    def _on_pdf_launch_finish(self, launcher, result) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as e:
            log.warning("PDF launch via portal failed (%s): %s", e.domain, e.message)
            launcher.open_containing_folder(self, None, self._on_pdf_folder_finish)

    def _on_pdf_folder_finish(self, launcher, result) -> None:
        try:
            launcher.open_containing_folder_finish(result)
        except GLib.Error as e:
            self.show_error(f"Could not open PDF: {e.message}")

    def _on_copy_pdf_path(self, *_) -> None:
        """Copy the output PDF path to the clipboard (#72)."""
        if self.documents.output_path:
            display = Gdk.Display.get_default()
            if display:
                display.get_clipboard().set(str(self.documents.output_path))
            self.show_toast("PDF path copied to clipboard", timeout=2)

    # ── Build status ──────────────────────────────────────────────────────────

    # ── Build state ───────────────────────────────────────────────────────────
    #
    # BuildCoordinator owns all of this; the window keeps the names the
    # actions and converter signals are wired to.

    def _build_state(self) -> str:
        return self.builds.state()

    def _update_build_chip(self) -> None:
        self.builds.update_chip()

    def _on_conversion_started(self, converter: Converter) -> None:
        self.builds.on_started(converter)

    def _on_conversion_complete(self, converter: Converter, n_slides: int,
                                duration: float, pdf_path: str,
                                html_uri: str) -> None:
        self.builds.on_complete(converter, n_slides, duration,
                                 pdf_path, html_uri)

    def _on_conversion_failed(self, converter: Converter, message: str) -> None:
        self.builds.on_failed(converter, message)

    # ── Panel toggles ─────────────────────────────────────────────────────────

    def _on_sidebar_toggled(self, btn: Gtk.ToggleButton) -> None:
        """Show or hide the slide panel (F9)."""
        self._left_split.set_show_sidebar(btn.get_active())

    def _on_toggle_sidebar(self, *_) -> None:
        self._sidebar_btn.set_active(not self._sidebar_btn.get_active())

    def _on_theme_panel_toggled(self, btn: Gtk.ToggleButton) -> None:
        """Show or hide the theme panel right sidebar (F10)."""
        visible = btn.get_active()
        if visible:
            # Make the revealer part of the layout BEFORE starting the animation
            # so the slide-in plays from the correct fully-allocated width.
            self._theme_revealer.set_visible(True)
            self._theme_revealer.set_reveal_child(True)
        else:
            # Start hide animation; _on_theme_revealer_state_changed will call
            # set_visible(False) once child-revealed reaches False (animation done).
            self._theme_revealer.set_reveal_child(False)
        self._save_window_state()

    def _on_theme_revealer_state_changed(self, revealer, _param) -> None:
        """After the hide animation completes, remove the panel from layout."""
        if not revealer.get_child_revealed():
            revealer.set_visible(False)

    def _on_toggle_theme_panel(self, *_) -> None:
        self._theme_panel_btn.set_active(not self._theme_panel_btn.get_active())

    def _on_focus_mode(self, action, _param) -> None:
        self._set_focus_mode(not action.get_state().get_boolean())

    def _set_focus_mode(self, enabled: bool, persist: bool = True) -> None:
        """
        Turn focus mode on or off, and remember it for next time.

        *persist* is False when restoring at startup, which is reading the
        preference rather than setting it — writing it straight back would
        touch session.json on every launch for nothing.
        """
        enabled = bool(enabled)
        self.editor.set_focus_mode(enabled)
        action = self.lookup_action("focus-mode")
        if action is not None:
            action.set_state(GLib.Variant.new_boolean(enabled))
        if persist:
            save_editor_prefs({"focus_mode": enabled})

    def _on_left_split_show_changed(self, split, _param) -> None:
        self._sidebar_btn.set_active(split.get_show_sidebar())

    def _on_panel_theme_changed(self, panel, slug: str) -> None:
        self._panel_shown_theme = slug
        self._sync_frontmatter_key("theme", slug)

    def _on_panel_ratio_changed(self, panel, ratio: str) -> None:
        self._panel_shown_ratio = ratio
        self._sync_frontmatter_key("ratio", ratio)

    def _sync_frontmatter_key(self, key: str, value: str) -> None:
        """
        Write *key* into the document's frontmatter when it pins one.

        A document's frontmatter outranks the app's own setting, so a deck
        carrying `theme: light` ignored the panel entirely: the panel moved,
        a build ran, and the output was identical.  The panel is the control
        for the document's settings, so it edits the document — which also
        keeps the choice with the file rather than in this machine's prefs.

        Documents without the key are left alone; there the app setting
        already applies, and adding keys nobody asked for would be worse.
        """
        text = self.editor.get_text()
        block = raw_frontmatter(text)
        if not block:
            return

        pattern = re.compile(rf"^([ \t]*{re.escape(key)}[ \t]*:[ \t]*)(.*)$",
                             re.MULTILINE)
        m = pattern.search(block)
        if m is None or m.group(2).strip() == value:
            return

        new_block = block[:m.start()] + m.group(1) + value + block[m.end():]
        new_text = new_block + text[len(block):]

        self.editor.set_text_as_user_action(new_text)
        self._mark_modified()
        self.sidebar.update_from_text(new_text)
        self.refresh_live_slide(new_text)
        self._update_build_chip()

    def _on_theme_panel_rebuild(self, panel) -> None:
        """ThemePanel emitted rebuild-needed — restyle the strip, rebuild the PDF."""
        # Theme files may have been edited in place, so drop the cached CSS
        # rather than relying on the cache key alone.
        self.converter.invalidate_render_cache()
        self.refresh_live_slide()
        self._trigger_convert()

    # ── Menu action handlers ──────────────────────────────────────────────────

    def _on_new(self, *_) -> None:
        def _open_new():
            win = MainWindow(application=self.get_application())
            # Populate the new window with the starter template (#24)
            win.editor.set_text(_STARTER_TEMPLATE)
            win.sidebar.update_from_text(_STARTER_TEMPLATE)
            win.documents.modified = False   # template is not a user edit
            win.present()
            # After present(), so the strip has an allocation to scale into.
            win.refresh_live_slide(_STARTER_TEMPLATE)
        self._check_unsaved(_open_new)

    def show_open_dialog(self) -> None:
        self._show_open_dialog()

    def _on_open(self, *_) -> None:
        self._check_unsaved(self._show_open_dialog)

    def _show_open_dialog(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Open File")
        dialog.set_filters(make_filter_store(
            make_file_filter("Presence files", "*.pres", "*.md"),
            make_file_filter("Presence bundle", "*.pres"),
            make_file_filter("Markdown files", "*.md"),
        ))
        self.hold_file_dialog(dialog)
        dialog.open(self, None, self._on_open_response)

    def _on_open_response(self, dialog, result) -> None:
        self.hold_file_dialog(None)
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        path_str = gfile.get_path()
        if not path_str:
            return
        self.open_file(Path(path_str))

    def _on_save(self, *_) -> None:
        self._save()

    def _on_save_as(self, *_) -> None:
        self._save_as_dialog()

    def _on_settings(self, *_) -> None:
        # The window used to hold a reference to the open dialog so that
        # theme_manager_ui could reach back through it to refresh the
        # inspector after an uninstall.  That page is no longer in
        # Preferences, and it is handed the panel to refresh directly.
        SettingsDialog(self).present(self)

    def _on_presenter(self, *_) -> None:
        if not self.builds.html_uri or not self.builds.slide_info:
            self.show_error("Convert the presentation first to open presenter mode.")
            return
        win = PresenterWindow(
            html_uri=self.builds.html_uri,
            pdf_path=self.documents.output_path,
            slide_info=self.builds.slide_info,
            thumbnails=self.builds.thumbnails,
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
        self._title_label.set_subtitle(f"Speaking: {elapsed_str}{target_str}")

    def _on_presenter_closed(self, win) -> bool:
        """Restore subtitle when presenter window closes."""
        self.update_word_count(self.editor.get_text())
        return False   # allow normal close to proceed

    def _on_insert_comment(self, *_) -> None:
        self.editor.insert_comment()

    def _on_editor_notify_user(self, _editor, message: str) -> None:
        """Surface something the editor could only detect, not report."""
        self.show_toast(message, timeout=6)

    def _on_insert_image(self, *_) -> None:
        """
        Ask the editor for an image and insert it at the cursor.

        This used to anchor a popover of layout controls to the toolbar
        button, with a bare file dialog as the fallback when that button was
        unavailable. There are no layout controls now, so both paths were the
        same file dialog and only one is left.
        """
        self.editor.choose_image_to_insert()

    def _on_shortcuts(self, *_) -> None:
        build_shortcuts_window(self).present()

    # ── Export ────────────────────────────────────────────────────────────────
    #
    # The four flows live in ExportController; the window keeps only the names
    # the actions are wired to.

    def _on_export(self, *_) -> None:
        self.exports.export_pdf()

    def _on_export_html(self, *_) -> None:
        self.exports.export_html()

    def _on_export_images(self, *_) -> None:
        self.exports.export_images()

    def _on_export_handout(self, *_) -> None:
        self.exports.export_handout()

    def _on_show_in_file_manager(self, *_) -> None:
        """Open the output folder in the system file manager."""
        if self.documents.pres_path:
            folder = Gio.File.new_for_path(str(self.documents.pres_path.parent))
        elif self.documents.output_path and self.documents.output_path.parent.exists():
            folder = Gio.File.new_for_path(str(self.documents.output_path.parent))
        elif self.documents.file_path:
            folder = Gio.File.new_for_path(str(self.documents.file_path.parent))
        else:
            # Transient: user triggered this without a file loaded.
            self.show_toast("No output folder to open.")
            return
        launcher = Gtk.FileLauncher.new(folder)
        launcher.launch(self, None, None)

    def _on_size_changed(self, *_) -> None:
        # Debounce: only write session after 500 ms of no resize events (#96)
        self._debounce("_size_save_source", 500, self._flush_window_state)

    def _flush_window_state(self) -> bool:
        self._size_save_source = None
        self._save_window_state()
        return GLib.SOURCE_REMOVE

    def _save_window_state(self) -> None:
        """Write the full window state — every caller saves every key."""
        save_window_state({
            "width":               self.get_width(),
            "height":              self.get_height(),
            "maximized":           self.is_maximized(),
            "theme_panel_visible": self._theme_panel_btn.get_active(),
            "thumbnail_size":      self._thumbnail_size,
        })

    # ── Autosave ──────────────────────────────────────────────────────────────

    def _autosave(self) -> bool:
        return self.documents.autosave()

    # ── Word count ────────────────────────────────────────────────────────────

    def update_word_count(self, text: str) -> None:
        # Counted the way the strip and the presenter count it: what each
        # slide's script says, or its own text where there is no script.
        # Counting the file's tokens instead made "---", "^^^" and every
        # "#" a word somebody was going to say out loud.
        # Speaking rate is user-configurable (default 110 WPM)
        timing  = document_timing(text, self.speaking_rate)
        words   = timing.words
        minutes = max(1, round(timing.seconds / 60))
        time_str = (f"{minutes} min to present" if minutes < 60
                    else f"{minutes // 60}h {minutes % 60}m")
        self._title_label.set_subtitle(f"{words} words · ~{time_str}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _debounce(self, attr: str, delay_ms: int, cb, *args) -> None:
        src = getattr(self, attr, None)
        if src is not None:
            GLib.source_remove(src)
        def _fire():
            setattr(self, attr, None)
            cb(*args)
            return GLib.SOURCE_REMOVE
        setattr(self, attr, GLib.timeout_add(delay_ms, _fire))

    def set_document_title(self, name: str) -> None:
        self.set_title(f"{name} — Presence")
        self._title_label.set_title(name)

    def _mark_modified(self) -> None:
        """Note that the buffer has moved on from disk, and say so in the title."""
        self.documents.modified = True
        self.set_document_title(self.documents.display_name + " •")

    def hold_file_dialog(self, dialog) -> None:
        """
        Keep a strong reference to an open Gtk.FileDialog, or let one go.

        A dialog answered through an async callback is collected out from
        under that callback otherwise.  One door, so every flow that opens
        one releases it the same way.
        """
        self._active_file_dialog = dialog

    def show_error(self, message: str) -> None:
        """Show a persistent error in the banner (stays until dismissed)."""
        self.banner.set_title(message)
        self.banner.set_revealed(True)

    def show_toast(self, message: str, timeout: int = 4) -> None:
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
