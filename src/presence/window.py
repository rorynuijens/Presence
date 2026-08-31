"""
window.py — Main application window.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib, Gdk, Pango

from .editor     import Editor
from .preview    import SlideCanvas
from .sidebar    import Sidebar, THUMBNAIL_WIDTH, SIDEBAR_WIDTH
from .theme_panel    import ThemePanel
from .inspector      import Inspector
from .settings_dialog import SettingsDialog
from .app_utils        import png_bytes_to_texture, make_file_filter, make_filter_store

log = logging.getLogger(__name__)
from .converter  import Converter
from .export_controller import ExportController
from .document_controller import DocumentController, UNTITLED
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
        self._canvas_open:      bool = state.get("canvas_visible", True)
        self._canvas_position:  int  = state.get("canvas_position", 0)

        self._file_path:     Path | None = None
        self._output_path:   Path | None = None
        self._pres_path:     Path | None = None   # .pres bundle path (user-visible)
        self._pres_temp_dir: Path | None = None   # temp dir for extracted .pres content
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
        # The document the slide now being rendered was read from.
        self._live_render_text: str = ""
        # Cursor-position polling source for sidebar sync (#28)
        self._cursor_sync_source: int | None = None
        # Cache for cursor-sync: avoid re-parsing unchanged text every 300ms
        self._cursor_sync_cache: tuple[str, list[int]] | None = None
        # Slide the cursor is currently in — drives the live canvas
        self._current_slide: int = 0
        # Build status: the document text the last successful build contained,
        # against which the status chip decides whether a rebuild is needed.
        self._built_text:    str | None = None
        self._building_text: str | None = None
        self._converting:    bool = False
        # Debounce source for saving the canvas divider position
        self._canvas_pos_source: int | None = None

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
        # Callable to run once the build matches the document, if anything is
        # waiting on it (Present, an export, opening the PDF).
        self._after_build = None
        # Speaking rate in WPM — default 110, persisted to session.json
        self._speaking_rate: int = pres_prefs.get('speaking_rate', 110)
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

        # Restore persisted panel visibility — done after _build_ui so the
        # revealer and button already exist.  Starts hidden by default so the
        # panel doesn't inflate the window's minimum width at startup.
        if self._theme_panel_open:
            self._theme_panel_btn.set_active(True)

        self._sidebar.set_speaking_rate(self._speaking_rate)

        # Apply persisted editor preferences (#50 + editor tab)
        self._editor.set_font_size(prefs.get("font_size", 13))
        self._editor.set_syntax_highlight(prefs.get("syntax_highlight", True))
        self._editor.set_line_numbers(prefs.get("line_numbers", True))
        self._editor.set_highlight_current_line(prefs.get("highlight_line", True))
        self._editor.set_auto_indent(prefs.get("auto_indent", True))
        self._editor.set_spaces_instead_of_tabs(prefs.get("spaces_tabs", True))
        self._editor.set_line_length(prefs.get("line_length", 64))
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
        self._banner = Adw.Banner(title="")
        self._banner.set_button_label("Close")   # HIG standard label (#23)
        self._banner.set_revealed(False)
        self._banner.connect("button-clicked", lambda *_: self._banner.set_revealed(False))
        root.add_top_bar(self._banner)

        self._sidebar     = Sidebar()
        self._editor      = Editor()
        self._canvas      = SlideCanvas()
        self._exports     = ExportController(self)
        self._documents   = DocumentController(self)
        self._builds      = BuildCoordinator(self)
        self._theme_panel = ThemePanel()
        self._inspector = Inspector(self._theme_panel)

        self._sidebar.connect("slide-selected",       self._on_slide_selected)
        self._sidebar.connect("slide-insert-after",   self._on_slide_insert_after)
        self._sidebar.connect("slides-reordered",     self._on_slides_reordered)
        self._sidebar.connect("slide-zoom-requested",  self._on_slide_zoom)
        self._editor.connect_undo_notify(self._on_undo_state_changed)
        self._editor.set_insert_image_callback(self._on_insert_image)
        self._editor.connect("changed",               self._on_editor_changed)
        self._editor.connect("live-changed",          self._on_editor_live_changed)
        self._editor.connect("notify-user",           self._on_editor_notify_user)
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

        # Editor ‖ live canvas.  A Paned rather than a fixed split so the
        # writer decides how much of the window is source and how much is
        # slide; the divider position is persisted between sessions.
        self._canvas_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._canvas_paned.set_hexpand(True)
        self._canvas_paned.set_vexpand(True)
        self._canvas_paned.set_resize_start_child(True)
        self._canvas_paned.set_resize_end_child(True)
        self._canvas_paned.set_shrink_start_child(False)
        self._canvas_paned.set_shrink_end_child(False)
        self._editor.set_size_request(360, -1)
        self._canvas.set_size_request(280, -1)
        self._canvas_paned.set_start_child(self._editor)
        self._canvas_paned.set_end_child(self._canvas)
        self._canvas_paned.connect(
            "notify::position", self._on_canvas_position_changed
        )
        # Catches every way the canvas can come back — the toggle, the
        # breakpoint releasing on a wider window — so it is never restored
        # showing a slide from before the document changed.
        self._canvas.connect("notify::visible", self._on_canvas_visibility)
        self._canvas.connect("render-size-changed", self._on_canvas_render_size)

        editor_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        editor_row.append(self._canvas_paned)
        editor_row.append(self._theme_revealer)

        # Left sidebar: thumbnail strip, collapses to drawer on narrow windows.
        self._left_split = Adw.OverlaySplitView()
        self._left_split.set_sidebar_position(Gtk.PackType.START)
        self._left_split.set_sidebar(self._sidebar)
        self._left_split.set_max_sidebar_width(SIDEBAR_WIDTH)
        self._left_split.set_min_sidebar_width(0)
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

        # Apply persisted canvas visibility.  Hiding the widget is enough —
        # GtkPaned gives the whole width to the remaining child.
        self._canvas.set_visible(self._canvas_open)
        if self._canvas_position > 0:
            self._canvas_paned.set_position(self._canvas_position)

        # Only one breakpoint applies at a time, so the narrower condition
        # must repeat what the wider one does and be added last.
        #
        # Below 1180 px there is not enough room for source and slide
        # side by side, so the canvas steps aside; below 800 px the
        # thumbnail sidebar becomes a drawer as well.  Both are restored
        # to their previous state when the window grows again.
        bp_canvas = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 1180px")
        )
        bp_canvas.add_setter(self._canvas, "visible", False)
        self.add_breakpoint(bp_canvas)

        # Raised with the strip: a wider sidebar reaches the point where it
        # crowds the editor sooner, and the number that matters is how much
        # room is left for the text, not how wide the window is.
        bp_sidebar = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 880px")
        )
        bp_sidebar.add_setter(self._canvas, "visible", False)
        bp_sidebar.add_setter(self._left_split, "collapsed", True)
        # Header gets crowded; the chip's icon still carries the state.
        bp_sidebar.add_setter(self._chip_label, "visible", False)
        self.add_breakpoint(bp_sidebar)

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

        # Live canvas toggle — shows the slide the cursor is in (F8)
        self._canvas_btn = Gtk.ToggleButton()
        self._canvas_btn.set_icon_name("view-reveal-symbolic")
        self._canvas_btn.set_tooltip_text("Show live canvas (F8)")
        self._canvas_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Show live canvas"]
        )
        self._canvas_btn.add_css_class("flat")
        self._canvas_btn.set_active(self._canvas_open)
        self._canvas_btn.connect("toggled", self._on_canvas_toggled)
        bar.pack_end(self._canvas_btn)

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
        self._share_btn.set_tooltip_text("Export…")
        self._share_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Export"]
        )
        self._share_btn.add_css_class("flat")
        self._share_btn.set_popover(self._build_share_popover())
        # Enabled from the start.  Every format routes through
        # _with_current_build(), which builds first when the deck has moved
        # on, so there is nothing left for a greyed-out button to protect
        # against — only a control that looked broken until a build happened.
        self._export_busy = _BusyIndicator(
            self._share_btn, "document-send-symbolic"
        )
        bar.pack_end(self._share_btn)

        # Present button — converts (if needed) then opens presenter mode.
        self._present_btn = Gtk.Button()
        self._present_busy = _BusyIndicator(
            self._present_btn, "media-playback-start-symbolic"
        )
        self._present_btn.set_tooltip_text("Present (Ctrl+P)")
        self._present_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Present"]
        )
        self._present_btn.add_css_class("suggested-action")
        self._present_btn.connect("clicked", self._on_present_clicked)
        bar.pack_end(self._present_btn)

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
        self._chip_icon = Gtk.Image.new_from_icon_name("object-select-symbolic")
        self._chip_spinner = Gtk.Spinner()

        self._chip_visual = Gtk.Stack()
        self._chip_visual.add_named(self._chip_icon, "icon")
        self._chip_visual.add_named(self._chip_spinner, "spinner")

        self._chip_label = Gtk.Label(label="Up to date")
        self._chip_label.add_css_class("caption")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(self._chip_visual)
        box.append(self._chip_label)

        chip = Gtk.Button()
        chip.set_child(box)
        chip.add_css_class("flat")
        chip.connect("clicked", self._trigger_convert)
        return chip

    def _build_share_popover(self) -> Gtk.Popover:
        """
        Build the Export popover: the three formats, and nothing else.

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
            "document-save-symbolic",
            "PDF…",
            "The deck as a PDF file",
            self._on_export,
        ))
        box.append(_row(
            "text-x-generic-symbolic",
            "HTML…",
            "A self-contained web page",
            self._on_export_html,
        ))
        box.append(_row(
            "image-x-generic-symbolic",
            "Images…",
            "One PNG per slide, into a folder",
            self._on_export_images,
        ))
        box.append(_row(
            "view-paged-symbolic",
            "Handout…",
            "Your slides and script, to read or print",
            self._on_export_handout,
        ))

        popover.set_child(box)
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
            ("export-html",   self._on_export_html,                       None),
            ("export-images", self._on_export_images,                     None),
            ("export-handout", self._on_export_handout,                    None),
            ("open-pdf",      self._on_open_pdf_clicked,                  None),
            ("show-output",   self._on_show_in_file_manager,              None),
            ("copy-pdf-path", self._on_copy_pdf_path,                     None),
            # Through _on_present_clicked, not _on_presenter: the shortcut
            # used to open the presenter against whatever the last build
            # left behind, while the button beside it built first.
            ("presenter",    self._on_present_clicked,                    "<primary>p"),
            # F1 for shortcuts (#45 / #86)
            ("shortcuts",    self._on_shortcuts,                          "F1"),
            ("insert-image",   self._on_insert_image,   None),
            ("insert-comment", self._on_insert_comment, None),
            ("bold",   lambda *_: self._editor.bold(),         "<primary>b"),
            ("italic", lambda *_: self._editor.italic(),       "<primary>i"),
            ("link",   lambda *_: self._editor.insert_link(),  "<primary>k"),
            # Panel toggles: slides F9, canvas F8, themes F10 (#75)
            ("toggle-sidebar",       self._on_toggle_sidebar,       "F9"),
            ("toggle-canvas",        self._on_toggle_canvas,        "F8"),
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

    def _refresh_recent_actions(self) -> None:
        for i in range(8):
            self.remove_action(f"open-recent-{i}")
        self._setup_recent_actions()
        self._rebuild_recent_menu()

    # ── Unsaved changes ───────────────────────────────────────────────────────

    def _on_close_request(self, win) -> bool:
        if not self._modified:
            self._shut_down()
            return False

        # Torn down only once the window is really going: cancelling the
        # question — or cancelling the Save As chooser behind it — leaves a
        # window the writer keeps typing in, and it must keep autosaving.
        self._documents.show_unsaved_dialog(
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
        self._converter.stop_watch()
        for attr in ("_autosave_source", "_size_save_source",
                     "_initial_convert_source", "_sidebar_update_source",
                     "_cursor_sync_source", "_canvas_pos_source"):
            src = getattr(self, attr, None)
            if src is not None:
                GLib.source_remove(src)
                setattr(self, attr, None)
        # Always clean up temp files, regardless of modified state (#44)
        self._cleanup_temp_files()

    def _check_unsaved(self, action) -> None:
        self._documents.check_unsaved(action)

    # ── File operations ───────────────────────────────────────────────────────

    def open_file(self, path: Path) -> None:
        self._documents.open_file(path)

    def restore_autosave(self, text: str) -> None:
        self._documents.restore_autosave(text)

    def _save(self, on_done=None) -> bool:
        return self._documents.save(on_done=on_done)

    def _write_document(self, on_done=None) -> bool:
        return self._documents.write_document(on_done=on_done)

    def _save_as_dialog(self, on_done=None) -> bool:
        return self._documents.save_as_dialog(on_done=on_done)

    # ── Conversion ────────────────────────────────────────────────────────────

    def _trigger_convert(self, *_) -> bool:
        return self._builds.trigger()

    def _build_for_export(self, on_done) -> None:
        """
        Build the deck to wherever Export just pointed it, and say when it lands.

        Exporting a PDF cannot go through _with_current_build(): the deck may
        already be current and still need writing to the chosen path, so this
        always builds.  The Export button holds the wait, which is the only
        sign the writer gets that a file is on its way — the other three
        formats finish with a toast, and this one now does too.
        """
        self._builds.after_build(on_done)
        if self._trigger_convert():
            self._builds.wait_for_build(self._export_busy)

    def _cleanup_temp_files(self) -> None:
        for attr in ("_temp_md", "_temp_pdf"):
            p = getattr(self, attr, None)
            if p is not None:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
                setattr(self, attr, None)

    # ── .pres bundle support ──────────────────────────────────────────────────

    def _on_destroy(self, *_) -> None:
        if self._pres_temp_dir and self._pres_temp_dir.exists():
            shutil.rmtree(self._pres_temp_dir, ignore_errors=True)
        self._pres_temp_dir = None

    def _open_pres_file(self, pres_path: Path) -> None:
        """Extract a .pres ZIP bundle to a temp dir and load slides.md from it."""
        self._cleanup_pres_temp()
        try:
            # Use shared /tmp in Flatpak so the generated PDF is accessible to
            # external viewers via the OpenURI portal (Flatpak's $TMPDIR is private).
            tmp_base = "/tmp" if os.environ.get("FLATPAK_ID") else None
            tmp_dir = Path(tempfile.mkdtemp(prefix="presence-", dir=tmp_base))
        except OSError as e:
            self._show_error(f"Could not create temp directory: {e}")
            return
        self._pres_temp_dir = tmp_dir
        try:
            with zipfile.ZipFile(pres_path, "r") as zf:
                zf.extractall(tmp_dir)
        except (zipfile.BadZipFile, OSError) as e:
            self._show_error(f"Could not open '{pres_path.name}': {e}")
            self._cleanup_pres_temp()
            return
        md_path = tmp_dir / "slides.md"
        if not md_path.exists():
            self._show_error(f"Invalid .pres file: 'slides.md' not found inside.")
            self._cleanup_pres_temp()
            return
        self._pres_path = pres_path
        self._documents.load_into_editor(md_path)

    def _pack_pres(self) -> None:
        """Re-pack the temp dir into the .pres ZIP bundle atomically."""
        if not self._pres_path or not self._file_path:
            return
        tmp = self._pres_path.with_suffix(".pres~")
        try:
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(self._file_path, "slides.md")
                for name in ("slides.pdf", "slides.html"):
                    p = self._file_path.parent / name
                    if p.exists():
                        zf.write(p, name)
                assets_dir = self._file_path.parent / "assets"
                if assets_dir.is_dir():
                    for asset in sorted(assets_dir.iterdir()):
                        if asset.is_file():
                            zf.write(asset, f"assets/{asset.name}")
            tmp.replace(self._pres_path)
        except OSError as e:
            log.warning("Could not write .pres bundle: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _cleanup_pres_temp(self) -> None:
        """Remove the pres temp dir and clear pres state."""
        if self._pres_temp_dir and self._pres_temp_dir.exists():
            shutil.rmtree(self._pres_temp_dir, ignore_errors=True)
        self._pres_temp_dir = None
        self._pres_path = None

    def _setup_pres_save(self, pres_path: Path, on_done=None) -> None:
        """Switch to .pres bundle mode, creating a temp dir for the working copy."""
        old_file_path = self._file_path
        old_pres_temp = self._pres_temp_dir
        try:
            tmp_base = "/tmp" if os.environ.get("FLATPAK_ID") else None
            tmp_dir = Path(tempfile.mkdtemp(prefix="presence-", dir=tmp_base))
        except OSError as e:
            self._show_error(f"Could not create temp directory: {e}")
            return
        if old_file_path:
            old_assets = old_file_path.parent / "assets"
            if old_assets.is_dir():
                try:
                    shutil.copytree(old_assets, tmp_dir / "assets")
                except OSError as e:
                    log.warning("Could not copy assets to bundle: %s", e)
        self._pres_temp_dir = tmp_dir
        self._pres_path = pres_path
        md_path = tmp_dir / "slides.md"
        self._file_path = md_path
        self._output_path = tmp_dir / "slides.pdf"
        self._editor.set_base_path(md_path)
        self._save(on_done=on_done)
        if old_pres_temp and old_pres_temp != tmp_dir and old_pres_temp.exists():
            shutil.rmtree(old_pres_temp, ignore_errors=True)

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _on_editor_changed(self, editor: Editor, text: str) -> None:
        self._modified = True
        display = self._pres_path or self._file_path
        base = display.name if display else UNTITLED
        self._set_title(base + " •")
        # Debounce the sidebar parse: run 200 ms after the last keystroke so
        # we do not parse the full document on every character (#37).
        self._debounce("_sidebar_update_source", 200, self._flush_sidebar_update, text)
        self._update_word_count(text)
        self._update_build_chip()

    def _flush_sidebar_update(self, text: str) -> bool:
        self._sidebar_update_source = None
        self._sidebar.update_from_text(text)
        return GLib.SOURCE_REMOVE

    # ── Live canvas ───────────────────────────────────────────────────────────

    def _on_editor_live_changed(self, editor: Editor, text: str) -> None:
        """Editor settled for 150 ms — re-render the slide under the cursor."""
        self._refresh_canvas(text)

    def _canvas_base_dir(self) -> Path:
        """Directory relative image paths in the document resolve against."""
        if self._file_path is not None:
            return self._file_path.parent
        return Path.home()

    def _live_render_width(self) -> int | None:
        """
        Width to rasterize the slide under the cursor at, or None for nobody.

        Two surfaces want that render and either one is reason enough to pay
        for it: the canvas wants it at pane width, and with the canvas closed
        the thumbnail strip still wants it, at thumbnail width, so the row
        being edited keeps up either way.  With both away there is nothing to
        show it on and the writer pays nothing.
        """
        if not self._canvas.available:
            return None
        if self._canvas.get_visible():
            return self._canvas.render_width
        if self._left_split.get_show_sidebar():
            return THUMBNAIL_WIDTH * max(1, self.get_scale_factor())
        return None

    def _refresh_canvas(self, text: str | None = None) -> None:
        """
        Ask for the current slide to be rendered, for whoever is showing it.

        The render runs on a background thread and lands in _on_canvas_frame;
        requests coalesce, so typing quickly never queues stale frames.
        """
        width = self._live_render_width()
        if width is None:
            return

        if text is None:
            text = self._editor.get_text()

        # Kept so the frame that comes back can be matched to the words it
        # was laid out from; a request that is superseded never arrives, so
        # whatever does arrive belongs to this text.
        self._live_render_text = text
        self._converter.render_slide_async(
            text, self._canvas_base_dir(), self._current_slide,
            width, self._on_canvas_frame,
        )

    def _on_canvas_frame(self, frame, error) -> None:
        """Receive a rendered slide on the main thread and show it."""
        if error is not None:
            if isinstance(error, ValueError):
                # Expected, recoverable states — an empty or slide-less document.
                self._canvas.show_message("Nothing to show yet", str(error))
            else:
                log.debug("Canvas render failed", exc_info=error)
                self._canvas.show_message(
                    "Cannot render this slide", _friendly_error(str(error))
                )
            return

        if self._canvas.get_visible():
            self._canvas.show_slide(frame.png, frame.width, frame.height)

        # Bring the strip up to the text this frame was rendered from before
        # handing the picture over.  The strip's text update is debounced
        # twice — the editor settles, then the window does — so it reliably
        # lands after a render, and a picture applied before it would be
        # marked out of date again by the words catching up behind it.  This
        # also lines the row indices up with the slides the frame counted.
        if self._sidebar_update_source is not None:
            GLib.source_remove(self._sidebar_update_source)
        self._flush_sidebar_update(self._live_render_text)
        # The canvas laid this slide out with the same engine the build uses,
        # so its fold is as authoritative as the build's — and it is fresher.
        # The strip gets the picture for the same reason: the row being
        # edited need not wait for a build to stop being out of date.
        self._sidebar.set_live_slide(frame.index, frame.png, frame.fold_line)
        self._set_live_fold(frame.index, frame.fold_line)

    def _on_canvas_render_size(self, _canvas) -> None:
        """The pane resized enough that the slide needs re-rasterizing."""
        self._refresh_canvas()

    def _set_build_folds(self, folds: list) -> None:
        self._builds.set_build_folds(folds)

    def _set_live_fold(self, index: int, fold_line) -> None:
        self._builds.set_live_fold(index, fold_line)

    def _on_canvas_toggled(self, btn: Gtk.ToggleButton) -> None:
        """Show or hide the live canvas (F8)."""
        visible = btn.get_active()
        self._canvas.set_visible(visible)
        self._canvas_open = visible
        self._save_window_state()

    def _on_canvas_visibility(self, canvas: SlideCanvas, _param) -> None:
        """Re-render whenever the canvas becomes visible again."""
        if canvas.get_visible():
            self._refresh_canvas()

    def _on_toggle_canvas(self, *_) -> None:
        self._canvas_btn.set_active(not self._canvas_btn.get_active())

    def _on_canvas_position_changed(self, paned: Gtk.Paned, _param) -> None:
        """Persist the divider position, debounced against drag jitter."""
        if not self._canvas.get_visible():
            return
        self._canvas_position = paned.get_position()
        self._debounce("_canvas_pos_source", 400, self._flush_canvas_position)

    def _flush_canvas_position(self) -> bool:
        self._canvas_pos_source = None
        self._save_window_state()
        return GLib.SOURCE_REMOVE

    def _sync_sidebar_to_cursor(self) -> bool:
        """
        Poll the editor cursor position every 300 ms and highlight the
        matching slide in the sidebar (#28).

        Caches the parsed slide-offset list so we only re-parse the document
        when the text has actually changed — avoids calling split_slides()
        3× per second on every keystroke.

        The same position drives the live canvas, which always shows the
        slide the cursor is in.
        """
        try:
            from .slides.utils import compute_slide_offsets
            text   = self._editor.get_text()
            offset = self._editor.get_cursor_offset()

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
            self._sidebar.scroll_to_index(current)

            # Follow the cursor across slide boundaries.  Edits within one
            # slide are handled by the live-changed signal instead.
            if current != self._current_slide:
                self._current_slide = current
                self._refresh_canvas(text)
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
        self._editor.scroll_to_slide(index)
        # Update the canvas now rather than waiting for the cursor poll —
        # a click should land on the slide immediately.
        self._current_slide = index
        self._refresh_canvas()

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
        display = self._pres_path or self._file_path
        base = display.name if display else UNTITLED
        self._set_title(base + " •")
        self._sidebar.update_from_text(new_text)
        self._current_slide = insert_at
        self._refresh_canvas(new_text)
        self._update_build_chip()
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
        display = self._pres_path or self._file_path
        base = display.name if display else UNTITLED
        self._set_title(base + " •")
        self._sidebar.update_from_text(new_text)
        self._refresh_canvas(new_text)
        self._update_build_chip()

    def _with_current_build(self, action, on_wait=None) -> None:
        self._builds.with_current_build(action, on_wait)

    def _on_present_clicked(self, *_) -> None:
        """Build if the deck has moved on, then open presenter mode."""
        self._with_current_build(self._on_presenter, on_wait=self._present_busy)

    def _on_open_pdf_clicked(self, *_) -> None:
        self._with_current_build(self._open_built_pdf)

    def _open_built_pdf(self) -> None:
        if not (self._output_path and self._output_path.exists()):
            return
        pdf_path = self._output_path
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
            self._show_toast(
                "PDF viewer unavailable — PDF saved to Documents/presentation.pdf",
                timeout=8,
            )
        except OSError:
            self._show_error(
                f"Could not open a PDF viewer.\n\nThe PDF is at:\n{self._output_path}"
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
            self._show_error(f"Could not open PDF: {e.message}")

    def _on_copy_pdf_path(self, *_) -> None:
        """Copy the output PDF path to the clipboard (#72)."""
        if self._output_path:
            display = Gdk.Display.get_default()
            if display:
                display.get_clipboard().set(str(self._output_path))
            self._show_toast("PDF path copied to clipboard", timeout=2)

    # ── Build status ──────────────────────────────────────────────────────────

    # ── Build state ───────────────────────────────────────────────────────────
    #
    # BuildCoordinator owns all of this; the window keeps the names the
    # actions and converter signals are wired to.

    def _build_state(self) -> str:
        return self._builds.state()

    def _update_build_chip(self) -> None:
        self._builds.update_chip()

    def _on_conversion_started(self, converter: Converter) -> None:
        self._builds.on_started(converter)

    def _on_conversion_complete(self, converter: Converter, n_slides: int,
                                duration: float, pdf_path: str,
                                html_uri: str) -> None:
        self._builds.on_complete(converter, n_slides, duration,
                                 pdf_path, html_uri)

    def _on_conversion_failed(self, converter: Converter, message: str) -> None:
        self._builds.on_failed(converter, message)

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
        self._editor.set_focus_mode(enabled)
        action = self.lookup_action("focus-mode")
        if action is not None:
            action.set_state(GLib.Variant.new_boolean(enabled))
        if persist:
            save_editor_prefs({"focus_mode": enabled})

    def _on_left_split_show_changed(self, split, _param) -> None:
        self._sidebar_btn.set_active(split.get_show_sidebar())

    def _on_panel_theme_changed(self, panel, slug: str) -> None:
        self._sync_frontmatter_key("theme", slug)

    def _on_panel_ratio_changed(self, panel, ratio: str) -> None:
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
        text = self._editor.get_text()
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

        self._editor.set_text_as_user_action(new_text)
        self._modified = True
        display = self._pres_path or self._file_path
        self._set_title((display.name if display else UNTITLED) + " •")
        self._sidebar.update_from_text(new_text)
        self._refresh_canvas(new_text)
        self._update_build_chip()

    def _on_theme_panel_rebuild(self, panel) -> None:
        """ThemePanel emitted rebuild-needed — restyle the canvas, rebuild the PDF."""
        # Theme files may have been edited in place, so drop the cached CSS
        # rather than relying on the cache key alone.
        self._converter.invalidate_render_cache()
        self._canvas.clear()
        self._refresh_canvas()
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
            # After present(), so the canvas has an allocation to scale into.
            win._refresh_canvas(_STARTER_TEMPLATE)
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
        self._active_file_dialog = dialog
        dialog.open(self, None, self._on_open_response)

    def _on_open_response(self, dialog, result) -> None:
        self._active_file_dialog = None
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
        win = PresenterWindow(
            html_uri=self._html_uri,
            pdf_path=self._output_path,
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
        self._title_label.set_subtitle(f"Speaking: {elapsed_str}{target_str}")

    def _on_presenter_closed(self, win) -> bool:
        """Restore subtitle when presenter window closes."""
        self._update_word_count(self._editor.get_text())
        return False   # allow normal close to proceed

    def _on_insert_comment(self, *_) -> None:
        self._editor.insert_comment()

    def _on_editor_notify_user(self, _editor, message: str) -> None:
        """Surface something the editor could only detect, not report."""
        self._show_toast(message, timeout=6)

    def _on_insert_image(self, *_) -> None:
        """
        Ask the editor for an image and insert it at the cursor.

        This used to anchor a popover of layout controls to the toolbar
        button, with a bare file dialog as the fallback when that button was
        unavailable. There are no layout controls now, so both paths were the
        same file dialog and only one is left.
        """
        self._editor.choose_image_to_insert()

    def _on_shortcuts(self, *_) -> None:
        build_shortcuts_window(self).present()

    # ── Export ────────────────────────────────────────────────────────────────
    #
    # The four flows live in ExportController; the window keeps only the names
    # the actions are wired to.

    def _on_export(self, *_) -> None:
        self._exports.export_pdf()

    def _on_export_html(self, *_) -> None:
        self._exports.export_html()

    def _on_export_images(self, *_) -> None:
        self._exports.export_images()

    def _on_export_handout(self, *_) -> None:
        self._exports.export_handout()

    def _on_show_in_file_manager(self, *_) -> None:
        """Open the output folder in the system file manager."""
        if self._pres_path:
            folder = Gio.File.new_for_path(str(self._pres_path.parent))
        elif self._output_path and self._output_path.parent.exists():
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
            "canvas_visible":      self._canvas_btn.get_active(),
            "canvas_position":     self._canvas_position,
        })

    # ── Autosave ──────────────────────────────────────────────────────────────

    def _autosave(self) -> bool:
        return self._documents.autosave()

    # ── Word count ────────────────────────────────────────────────────────────

    def _update_word_count(self, text: str) -> None:
        # Counted the way the strip and the presenter count it: what each
        # slide's script says, or its own text where there is no script.
        # Counting the file's tokens instead made "---", "^^^" and every
        # "#" a word somebody was going to say out loud.
        # Speaking rate is user-configurable (default 110 WPM)
        timing  = document_timing(text, self._speaking_rate)
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
