"""
editor.py — Markdown editor with formatting toolbar, find/replace,
and contextual image-layout editing.

When the cursor rests on a line containing a Markdown image tag, a
contextual popover appears anchored to that line, pre-populated with
the current layout tokens (position, size, gradient).  Any change in
the popover is written back to the buffer immediately — no Apply button.
This mirrors the iA Presenter UX pattern.
"""
from __future__ import annotations

import logging
import math
import re
import shutil
from pathlib import Path
from typing import Callable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, GObject, Pango, Gio

try:
    from gi.repository import PangoCairo as _PangoCairo  # used in badge drawing
except ImportError:
    _PangoCairo = None  # type: ignore[assignment]

try:
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource
    _GTKSOURCE_AVAILABLE = True
except (ValueError, ImportError):
    try:
        gi.require_version("GtkSource", "4")
        from gi.repository import GtkSource
        _GTKSOURCE_AVAILABLE = True
    except (ValueError, ImportError):
        _GTKSOURCE_AVAILABLE = False

log = logging.getLogger(__name__)

# Regex shared with md_to_slides.slides — keep in sync.
_IMAGE_RE = re.compile(
    r'!\[([^\]]*)\]'
    r'\('
    r'([^)\s"\']+)'
    r'(?:\s+"[^"]*")?'
    r'\)',
)

# Same heading rule the sidebar's titles use, so a band and its
# thumbnail never disagree about what a slide is called.
from .slides.splitter import _strip_inline_markdown


def _same_file(a: Path, b: Path) -> bool:
    """True when *a* and *b* hold the same bytes, so a copy can be skipped."""
    try:
        if a.samefile(b):
            return True
    except OSError:
        pass
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False

_HEADING_RE = re.compile(r'^#{1,3}\s+(.+)$')

# Invalidation value for the cached current-slide range.  Not None: None is a
# real answer (the cursor is in no slide), and comparing a stale None against
# a fresh one would skip the repaint that clears the previous marking.
_NO_BLOCK: tuple[int, int] = (-1, -1)


class _TableInsertPopover(Gtk.Popover):
    """
    Grid-picker popover for inserting a Markdown table.

    Hover over the grid to choose dimensions; click to insert.
    Maximum 6×6. Default highlighted selection updates as the
    mouse moves over cells.
    """
    _MAX_COLS = 6
    _MAX_ROWS = 6
    _CELL_PX  = 22

    def __init__(self, parent_widget: Gtk.Widget,
                 insert_cb) -> None:
        super().__init__()
        self._insert_cb = insert_cb
        self._hover_col = 0
        self._hover_row = 0
        self.set_parent(parent_widget)
        self.set_has_arrow(True)
        self.set_autohide(True)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(10)
        root.set_margin_bottom(10)
        root.set_margin_start(10)
        root.set_margin_end(10)

        self._size_label = Gtk.Label(label="Insert table")
        self._size_label.add_css_class("caption")
        self._size_label.add_css_class("dim-label")
        root.append(self._size_label)

        # Drawing area for the cell grid
        self._drawing = Gtk.DrawingArea()
        w = self._MAX_COLS * (self._CELL_PX + 3) + 3
        h = self._MAX_ROWS * (self._CELL_PX + 3) + 3
        self._drawing.set_size_request(w, h)
        self._drawing.set_draw_func(self._draw)

        # Motion tracking
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave",  self._on_leave)
        self._drawing.add_controller(motion)

        # Click to insert
        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self._drawing.add_controller(click)

        root.append(self._drawing)
        self.set_child(root)

    def _draw(self, area, cr, width, height) -> None:
        gap = 3
        sz  = self._CELL_PX
        style = area.get_style_context()
        found, accent = style.lookup_color("accent_bg_color")
        if not found:
            accent = style.get_color()
        for row in range(self._MAX_ROWS):
            for col in range(self._MAX_COLS):
                x = col * (sz + gap) + gap
                y = row * (sz + gap) + gap
                selected = (col < self._hover_col
                            and row < self._hover_row)
                if selected:
                    cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.85)
                else:
                    cr.set_source_rgba(0.28, 0.28, 0.30, 0.9)
                cr.rectangle(x, y, sz, sz)
                cr.fill()
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.08)
                cr.rectangle(x, y, sz, sz)
                cr.stroke()

    def _on_motion(self, ctrl, x, y) -> None:
        sz  = self._CELL_PX
        gap = 3
        col = min(self._MAX_COLS, int(x // (sz + gap)) + 1)
        row = min(self._MAX_ROWS, int(y // (sz + gap)) + 1)
        if col != self._hover_col or row != self._hover_row:
            self._hover_col = col
            self._hover_row = row
            self._size_label.set_label(
                f"{col} × {row} table" if col > 0 and row > 0 else "Insert table"
            )
            self._drawing.queue_draw()

    def _on_leave(self, ctrl) -> None:
        self._hover_col = 0
        self._hover_row = 0
        self._size_label.set_label("Insert table")
        self._drawing.queue_draw()

    def _on_click(self, ctrl, n_press, x, y) -> None:
        cols = self._hover_col
        rows = self._hover_row
        if cols < 1 or rows < 1:
            return
        self.popdown()
        self._insert_cb(cols, rows)


# Minimum-width strategy
# ─────────────────────────────────────────────────────────────────────────────
# PyGObject 3.x does NOT dispatch Python do_measure() overrides via the GTK4
# GTypeClass vfunc slot.  Defining do_measure in a Python subclass has no
# effect — GTK's C code never calls it.
#
# What actually works in this environment:
#
# 1. GtkTextView / GtkSourceView with wrap_mode=WORD_CHAR: the C-level
#    measure() already reports minimum_width=0 (only natural=longest-line).
#    GtkScrolledWindow(NEVER) with the text view as direct child allocates
#    the text view max(0, viewport_width) = viewport_width → text wraps.
#
# 2. The editor toolbar (20+ buttons ≈ 1050 px natural) is wrapped in a
#    GtkScrolledWindow(EXTERNAL, NEVER).  GTK's C code for EXTERNAL policy
#    reports minimum=0 to the parent while allocating the toolbar its full
#    natural width internally.  set_overflow(HIDDEN) on the scroll window
#    clips any buttons that extend beyond the window edge.  This prevents the
#    toolbar's natural width from propagating as the window's minimum, which
#    was the root cause of the AdwToastOverlay warnings and the header-bar
#    right buttons becoming inaccessible at medium window widths.


# ── Editor ────────────────────────────────────────────────────────────────────

class Editor(Gtk.Box):
    """
    Markdown editor with formatting toolbar, find/replace, and
    contextual image-layout editing (iA Presenter-style).

    Signals:
        changed (text: str)  — emitted on every buffer change, undebounced.
            How long an edit is worth waiting for is not this widget's to
            decide: it used to hold two timers of its own, and whoever was
            listening held more behind them.  :class:`settle_clock.SettleClock`
            owns that now, and this says only that the text moved.
    """

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # Something the writer needs told about, raised from a place with no
        # window reference of its own. The window turns it into a toast.
        "notify-user": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    # Blank space opened above and below a separator line.  The band's rule is
    # drawn in the upper half of it, clear of the "---" glyphs.
    _SEP_SPACE = 16

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._base_path:        Path | None = None
        self._insert_image_cb = None
        self._active_file_dialog = None
        # The toolbar insert-image button — used as popover anchor
        self._img_toolbar_btn:  Gtk.Button | None = None
        # Set by the window: (layout, description, edit_cb) -> bool.  Returns
        # True when the inspector took the image, in which case no popover
        # opens.  Falls back to the popover whenever the panel is closed.
        # Text marks at the lines where slides run out of room.  Marks rather
        # than line numbers so the rules stay attached to the content they
        # describe while the writer edits above them.
        self._fold_marks: list = []
        self._font_size: int = 13
        self._line_length: int = 64  # column position for margin guide
        self._table_popover: _TableInsertPopover | None = None

        # Slide-number badge overlay (DrawingArea over the gutter)
        self._badge_starts:  list[tuple[int,int]] = []  # (slide_num, line_no)
        # (slide_num, separator_line, title) for the band drawn at each
        # slide boundary.  Rebuilt by _update_badge_starts on every change,
        # so unlike the fold rules these need no marks to stay accurate.
        self._sep_bands: list[tuple[int, int, str]] = []
        # Line range currently washed as "the slide you are in", so the tag is
        # only reapplied when the cursor actually crosses a boundary.
        self._tinted_block: tuple[int, int] | None = _NO_BLOCK
        # Focus mode: the same boundary, used the other way round.
        self._focus_mode: bool = False
        # The lit range while focus mode is on, None while it is off.  The
        # band and fold overlays read it to fade what they draw outside it,
        # so they need it even when the tint has not moved.
        self._focus_range: tuple[int, int] | None = None
        self._slide_ranges:  list[tuple[int,int]] = []  # (first, last) per slide

        # Image-edit popover state
        # Last line number that had an image tag — avoids re-scanning if
        # the cursor stays on the same line.
        # The full match (src, alt) of the currently-tracked image tag

        if _GTKSOURCE_AVAILABLE:
            self._init_source_view()
        else:
            self._init_plain_view()

        self._buffer.connect("mark-set", self._on_cursor_moved)

        # Must precede any menu that references editor.* actions.
        self._install_action_group()
        self._view.set_extra_menu(self._build_context_menu())

        # Find bar inside a Revealer for slide-down animation (#53)
        self._find_revealer = Gtk.Revealer()
        self._find_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN
        )
        self._find_revealer.set_transition_duration(150)
        self._find_bar = self._build_find_bar()
        self._find_bar.set_visible(True)   # Revealer controls visibility
        self._find_revealer.set_child(self._find_bar)
        self.append(self._find_revealer)

        # Overlay wraps the scroll window so we can position the image-edit
        # anchor widget anywhere over the editor text area.
        # GtkScrolledWindow(NEVER) with GtkTextView/GtkSourceView as direct child:
        # the text view implements GtkScrollable with hscroll-policy=MINIMUM, and
        # GTK's C measure() reports minimum_width=0 for WORD_CHAR wrap mode.
        # The scroll window therefore also reports minimum=0 and allocates the
        # text view exactly viewport_width → text reflows correctly on resize.
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self._view)

        self._img_edit_overlay = Gtk.Overlay()
        self._img_edit_overlay.set_child(scroll)
        self._img_edit_overlay.set_vexpand(True)

        # Toolbar: wrap in a ScrolledWindow(EXTERNAL, NEVER) so that GTK's
        # C-level EXTERNAL-policy measure() reports minimum=0 to the parent,
        # keeping the bar from setting the window's minimum width.  It used to
        # also hide six unreachable buttons; the bar is now six controls wide
        # and this is only insurance for extreme window sizes.
        toolbar_scroll = Gtk.ScrolledWindow()
        toolbar_scroll.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        toolbar_scroll.set_child(self._build_toolbar())
        toolbar_scroll.set_overflow(Gtk.Overflow.HIDDEN)

        self.append(self._img_edit_overlay)
        self.append(toolbar_scroll)

        # Slide-number badge drawing area — overlaid on the gutter column.
        # Positioned at the far left of the overlay (over the gutter).
        # pick_from(NONE) so it never intercepts mouse events.
        if _GTKSOURCE_AVAILABLE:
            # Slide-number badge drawing area — overlaid on the gutter column.
            # set_can_target(False) so it never intercepts mouse events.
            # Slide bands: added before the fold overlay so a fold rule, which
            # is the more urgent signal, paints on top where they coincide.
            self._sep_draw = Gtk.DrawingArea()
            self._sep_draw.set_hexpand(True)
            self._sep_draw.set_vexpand(True)
            self._sep_draw.set_can_target(False)
            self._sep_draw.set_draw_func(self._draw_slide_bands)
            self._img_edit_overlay.add_overlay(self._sep_draw)

            # Fold rules span the full width, so they get their own overlay.
            self._fold_draw = Gtk.DrawingArea()
            self._fold_draw.set_hexpand(True)
            self._fold_draw.set_vexpand(True)
            self._fold_draw.set_can_target(False)
            self._fold_draw.set_draw_func(self._draw_folds)
            self._img_edit_overlay.add_overlay(self._fold_draw)
            # Recompute slide starts whenever the buffer changes.
            self._buffer.connect('changed', self._update_badge_starts)
            self._buffer.connect(
                'changed',
                lambda *_: self._fold_draw.queue_draw()
                if getattr(self, "_fold_draw", None) is not None else None,
            )
            GLib.idle_add(self._update_badge_starts)
            # After realize, measure the gutter width and wire up scroll.
            self._view.connect('realize', self._on_view_realize_badges)

        self._drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        self._drop_target.connect("drop",   self._on_drop)
        self._drop_target.connect("motion", self._on_drop_motion)
        # "leave" signal not connected — no visual feedback needed on drag leave
        self._view.add_controller(self._drop_target)

    # ── View initialisation ───────────────────────────────────────────────────

    def _init_source_view(self) -> None:
        lang_manager = GtkSource.LanguageManager.get_default()
        lang         = lang_manager.get_language("markdown")

        self._buffer = GtkSource.Buffer()
        if lang:
            self._buffer.set_language(lang)
        self._buffer.set_highlight_syntax(True)
        self._buffer.set_highlight_matching_brackets(True)
        self._apply_style()
        self._buffer.connect("changed", self._on_buffer_changed)

        self._search_settings = GtkSource.SearchSettings()
        self._search_settings.set_wrap_around(True)
        self._search_context  = GtkSource.SearchContext.new(
            self._buffer, self._search_settings
        )
        self._search_context.set_highlight(True)

        self._view = GtkSource.View()
        self._view.set_buffer(self._buffer)
        self._view.set_monospace(True)
        self._view.set_show_line_numbers(True)
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._view.set_tab_width(4)
        self._view.set_insert_spaces_instead_of_tabs(True)
        self._view.set_auto_indent(True)
        self._view.set_highlight_current_line(True)
        self._view.set_left_margin(52)
        self._view.set_right_margin(16)
        self._view.set_top_margin(12)
        self._view.set_bottom_margin(12)
        # Use GtkSourceView's built-in column guide — this draws a visual line
        # at the configured column without inflating the widget's minimum size.
        self._view.set_show_right_margin(True)
        self._view.set_right_margin_position(self._line_length)

        self._css_provider = Gtk.CssProvider()
        self._view.get_style_context().add_provider(
            self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._apply_font_css()
        self._has_search = True

    def _init_plain_view(self) -> None:
        self._buffer = Gtk.TextBuffer()
        self._buffer.connect("changed", self._on_buffer_changed)

        self._view = Gtk.TextView()
        self._view.set_buffer(self._buffer)
        self._view.set_monospace(True)
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._view.set_left_margin(52)
        self._view.set_right_margin(16)
        self._view.set_top_margin(12)
        self._view.set_bottom_margin(12)

        self._css_provider = Gtk.CssProvider()
        self._view.get_style_context().add_provider(
            self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._apply_font_css()
        self._has_search = False

    def _apply_font_css(self) -> None:
        """Apply the current font size via CSS."""
        size = str(self._font_size)
        css = "textview { font-family: Monospace; font-size: " + size + "pt; }"
        try:
            self._css_provider.load_from_string(css)
        except AttributeError:
            self._css_provider.load_from_data(css.encode())

    # ── Public API ────────────────────────────────────────────────────────────

    def get_text(self) -> str:
        start = self._buffer.get_start_iter()
        end   = self._buffer.get_end_iter()
        return self._buffer.get_text(start, end, True)

    def get_cursor_offset(self) -> int:
        """Return the character offset of the insertion cursor in the buffer."""
        cursor = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        return cursor.get_offset()

    def set_text(self, text: str) -> None:
        # Signals stay blocked: a document being replaced is not an edit.
        # What must follow instead is SettleClock.document_replaced(), which
        # the callers that do this all make.
        self._buffer.handler_block_by_func(self._on_buffer_changed)
        try:
            self._buffer.set_text(text)
        finally:
            self._buffer.handler_unblock_by_func(self._on_buffer_changed)
        # Reset slide state when the document is replaced
        self._badge_starts      = []
        self._slide_ranges      = []
        # Schedule badge and image-tag scan now that the buffer has content
        GLib.idle_add(self._update_badge_starts)
        GLib.idle_add(self._highlight_image_tags)

    def set_text_as_user_action(self, text: str) -> None:
        """Replace buffer contents as one undoable step (e.g. slide reorder)."""
        self._buffer.handler_block_by_func(self._on_buffer_changed)
        try:
            if _GTKSOURCE_AVAILABLE:
                # GtkSource.Buffer (and Gtk.TextBuffer) always have
                # begin/end_user_action — no hasattr check required.
                self._buffer.begin_user_action()
                try:
                    start = self._buffer.get_start_iter()
                    end   = self._buffer.get_end_iter()
                    self._buffer.delete(start, end)
                    self._buffer.insert(self._buffer.get_start_iter(), text)
                finally:
                    # Always close the user action even if delete/insert raises,
                    # otherwise the buffer is left permanently inside an action.
                    self._buffer.end_user_action()
            else:
                self._buffer.set_text(text)
        finally:
            self._buffer.handler_unblock_by_func(self._on_buffer_changed)

    def scroll_to_slide(self, slide_index: int) -> None:
        from .slides.utils import compute_slide_offsets

        full_text = self.get_text()
        slide_offsets = compute_slide_offsets(full_text)
        if not slide_offsets or slide_index >= len(slide_offsets):
            return

        target_offset = slide_offsets[slide_index]
        target_line = full_text.count('\n', 0, target_offset)

        ok, start_it = self._buffer.get_iter_at_line(target_line)
        if not ok:
            return
        # Place the cursor rather than selecting the slide.  A selection was
        # how you used to see which slide you were on, but it is a poor sign
        # for it — one keystroke replaces the lot — and the wash now says it
        # without putting the text at risk.
        self._buffer.place_cursor(start_it)
        self._view.scroll_to_iter(start_it, 0.05, True, 0.0, 0.0)
        self._view.grab_focus()

    # ── Undo / redo ───────────────────────────────────────────────────────────

    def undo(self) -> None:
        if self._buffer.get_can_undo():
            self._buffer.undo()
        self._view.grab_focus()

    def redo(self) -> None:
        if self._buffer.get_can_redo():
            self._buffer.redo()
        self._view.grab_focus()

    def connect_undo_notify(self, callback) -> None:
        def _notify(*_):
            callback(self._buffer.get_can_undo(), self._buffer.get_can_redo())
        self._buffer.connect("notify::can-undo", _notify)
        self._buffer.connect("notify::can-redo", _notify)

    def set_font_size(self, pt: int) -> None:
        pt = max(8, min(pt, 32))
        if pt == self._font_size:
            return
        self._font_size = pt
        self._apply_font_css()

    def get_font_size(self) -> int:
        return self._font_size

    def set_line_length(self, cols: int) -> None:
        """Update the column guide position."""
        self._line_length = cols
        self._update_wrap_margin()

    def get_line_length(self) -> int:
        return self._line_length

    def _update_wrap_margin(self) -> None:
        """Update the GtkSourceView column guide to _line_length columns."""
        if not _GTKSOURCE_AVAILABLE:
            return
        cols = self._line_length
        if cols > 0:
            self._view.set_show_right_margin(True)
            self._view.set_right_margin_position(cols)
        else:
            self._view.set_show_right_margin(False)


    def set_syntax_highlight(self, enabled: bool) -> None:
        if _GTKSOURCE_AVAILABLE and hasattr(self._buffer, "set_highlight_syntax"):
            self._buffer.set_highlight_syntax(enabled)

    def set_line_numbers(self, enabled: bool) -> None:
        if _GTKSOURCE_AVAILABLE and hasattr(self._view, "set_show_line_numbers"):
            self._view.set_show_line_numbers(enabled)

    def set_highlight_current_line(self, enabled: bool) -> None:
        if _GTKSOURCE_AVAILABLE and hasattr(self._view, "set_highlight_current_line"):
            self._view.set_highlight_current_line(enabled)

    def set_auto_indent(self, enabled: bool) -> None:
        if _GTKSOURCE_AVAILABLE and hasattr(self._view, "set_auto_indent"):
            self._view.set_auto_indent(enabled)

    def set_spaces_instead_of_tabs(self, enabled: bool) -> None:
        if _GTKSOURCE_AVAILABLE and hasattr(self._view,
                                            "set_insert_spaces_instead_of_tabs"):
            self._view.set_insert_spaces_instead_of_tabs(enabled)

    def insert_image_markdown(self, alt: str, rel_path: str) -> None:
        self._replace_selection(f"![{alt}]({rel_path})")

    def get_current_slide_markdown(self) -> str:
        """Return the full raw Markdown of the slide at the current cursor position."""
        buf = self._buffer
        full_text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        cursor_line = buf.get_iter_at_mark(buf.get_insert()).get_line()
        lines = full_text.splitlines()
        sep_lines = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
        slide_start = 0
        slide_end = len(lines)
        for sep in sep_lines:
            if sep <= cursor_line:
                slide_start = sep + 1
            else:
                slide_end = sep
                break
        return "\n".join(lines[slide_start:slide_end])

    def get_img_toolbar_btn(self) -> "Gtk.Button | None":
        return self._img_toolbar_btn

    def choose_image_to_insert(self) -> None:
        self._choose_image_to_insert()

    # ── Find bar ──────────────────────────────────────────────────────────────

    def show_find(self) -> None:
        self._find_revealer.set_reveal_child(True)
        self._replace_row.set_visible(False)
        self._search_entry.grab_focus()
        bounds = self._selection_bounds()
        if bounds is not None:
            start, end = bounds
            self._search_entry.set_text(
                self._buffer.get_text(start, end, True)
            )

    def show_find_replace(self) -> None:
        self._find_revealer.set_reveal_child(True)
        self._replace_row.set_visible(True)
        self._search_entry.grab_focus()

    def hide_find(self) -> None:
        self._find_revealer.set_reveal_child(False)
        if self._has_search:
            self._search_settings.set_search_text("")
        self._view.grab_focus()

    def _build_find_bar(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        bar.append(sep)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_row.set_margin_start(8)
        search_row.set_margin_end(8)
        search_row.set_margin_top(4)
        search_row.set_margin_bottom(4)

        search_row.append(Gtk.Image.new_from_icon_name("edit-find-symbolic"))

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_hexpand(True)
        self._search_entry.set_placeholder_text("Find…")
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("activate", self._find_next)
        search_row.append(self._search_entry)

        self._match_label = Gtk.Label(label="")
        self._match_label.add_css_class("dim-label")
        self._match_label.add_css_class("caption")
        search_row.append(self._match_label)

        def _nav_btn(icon, tip, cb):
            b = Gtk.Button()
            b.set_child(Gtk.Image.new_from_icon_name(icon))
            b.set_tooltip_text(tip)
            b.update_property([Gtk.AccessibleProperty.LABEL], [tip])
            b.add_css_class("flat")
            b.connect("clicked", cb)
            return b

        search_row.append(_nav_btn("go-up-symbolic",   "Previous match", self._find_prev))
        search_row.append(_nav_btn("go-down-symbolic",  "Next match",     self._find_next))

        self._case_btn = Gtk.ToggleButton()
        self._case_btn.set_label("Aa")
        self._case_btn.set_tooltip_text("Match case")
        self._case_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Match case"])
        self._case_btn.add_css_class("flat")
        self._case_btn.connect("toggled", self._on_case_toggled)
        search_row.append(self._case_btn)

        self._regex_btn = Gtk.ToggleButton()
        self._regex_btn.set_label(".*")
        self._regex_btn.set_tooltip_text("Regular expression")
        self._regex_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Regular expression"])
        self._regex_btn.add_css_class("flat")
        self._regex_btn.connect("toggled", self._on_regex_toggled)
        search_row.append(self._regex_btn)

        close_btn = Gtk.Button()
        close_btn.set_child(
            Gtk.Image.new_from_icon_name("window-close-symbolic"))
        close_btn.set_tooltip_text("Close find bar (Escape)")
        close_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Close find bar"])
        close_btn.add_css_class("flat")
        close_btn.connect("clicked", lambda *_: self.hide_find())
        search_row.append(close_btn)

        bar.append(search_row)

        self._replace_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._replace_row.set_margin_start(8)
        self._replace_row.set_margin_end(8)
        self._replace_row.set_margin_bottom(4)
        self._replace_row.set_visible(False)

        self._replace_row.append(
            Gtk.Image.new_from_icon_name("edit-find-replace-symbolic"))

        self._replace_entry = Gtk.Entry()
        self._replace_entry.set_hexpand(True)
        self._replace_entry.set_placeholder_text("Replace with…")
        self._replace_entry.connect("activate", self._replace_one)
        self._replace_row.append(self._replace_entry)

        for label, cb in (("Replace", self._replace_one),
                           ("Replace all", self._replace_all)):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.connect("clicked", cb)
            self._replace_row.append(b)

        bar.append(self._replace_row)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_find_key)
        bar.add_controller(key_ctrl)

        return bar

    # ── Search signal handlers ────────────────────────────────────────────────

    def _on_search_changed(self, entry):
        if self._has_search:
            self._search_settings.set_search_text(entry.get_text())
            self._update_match_label()

    def _on_case_toggled(self, btn):
        if self._has_search:
            self._search_settings.set_case_sensitive(btn.get_active())
            self._update_match_label()

    def _on_regex_toggled(self, btn):
        if self._has_search:
            self._search_settings.set_regex_enabled(btn.get_active())
            self._update_match_label()

    def _find_next(self, *_):
        if not self._has_search:
            return
        insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        found, start, end, _w = self._search_context.forward(insert)
        if found:
            self._buffer.select_range(start, end)
            self._view.scroll_to_iter(start, 0.1, True, 0.5, 0.5)
        self._update_match_label()

    def _find_prev(self, *_):
        if not self._has_search:
            return
        insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        found, start, end, _w = self._search_context.backward(insert)
        if found:
            self._buffer.select_range(start, end)
            self._view.scroll_to_iter(start, 0.1, True, 0.5, 0.5)
        self._update_match_label()

    def _replace_one(self, *_):
        if not self._has_search:
            return
        replacement = self._replace_entry.get_text()
        bounds = self._selection_bounds()
        if bounds is not None:
            start, end = bounds
            try:
                self._search_context.replace(start, end, replacement, -1)
            except Exception:
                self._match_label.set_text("Replace failed")
                return
        self._find_next()

    def _replace_all(self, *_):
        if not self._has_search:
            return
        try:
            count = self._search_context.replace_all(
                self._replace_entry.get_text(), -1)
            self._match_label.set_text(f"Replaced {count}")
        except Exception:
            self._match_label.set_text("Error")

    def _update_match_label(self):
        if not self._has_search:
            return
        count = self._search_context.get_occurrences_count()
        if not self._search_settings.get_search_text():
            self._match_label.set_text("")
        elif count == 0:
            self._match_label.set_text("No results")
        elif count == -1:
            self._match_label.set_text("…")
        else:
            self._match_label.set_text(
                f"{count} result{'s' if count != 1 else ''}")

    def _on_find_key(self, ctrl, keyval, keycode, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.hide_find()
            return True
        return False

    # ── Toolbar ───────────────────────────────────────────────────────────────

    # ── Toolbar ───────────────────────────────────────────────────────────────

    # Everything the toolbar used to show as its own button.  Each entry is
    # (action name, label, callback) and feeds both the overflow menu and the
    # text view's context menu, so the two can never drift apart.
    def _editor_action_specs(self) -> list[tuple[str, str, object]]:
        return [
            ("heading-1",     "Heading 1",        lambda: self._heading(1)),
            ("heading-2",     "Heading 2",        lambda: self._heading(2)),
            ("heading-3",     "Heading 3",        lambda: self._heading(3)),
            ("bold",          "Bold",             self.bold),
            ("italic",        "Italic",           self.italic),
            ("strikethrough", "Strikethrough",
             lambda: self._wrap("~~", "~~", "text")),
            ("inline-code",   "Inline code",
             lambda: self._wrap("`", "`", "code")),
            ("code-block",    "Code block",       self._code_block),
            ("blockquote",    "Blockquote",       lambda: self._line_prefix("> ")),
            ("bullet-list",   "Bullet list",      lambda: self._line_prefix("- ")),
            ("numbered-list", "Numbered list",    lambda: self._line_prefix("1. ")),
            ("link",          "Link…",            self._link),
            ("comment",       "Slide comment",    self.insert_comment),
            ("find",          "Find…",            self.show_find),
            ("find-replace",  "Find and replace…", self.show_find_replace),
        ]

    def _install_action_group(self) -> None:
        """
        Publish the formatting actions as an "editor" action group.

        Menus reference them by name, which is what lets the overflow menu and
        the context menu share one definition.
        """
        group = Gio.SimpleActionGroup()
        for name, _label, cb in self._editor_action_specs():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=cb: cb())
            group.add_action(action)
        # GTK 4 has no getter for an inserted action group, so keep the
        # reference — it is the only handle for querying or extending it.
        self._action_group = group
        self.insert_action_group("editor", group)

    def _build_overflow_menu(self) -> Gio.Menu:
        """Everything that is not a slide-structure insert."""
        labels = {name: label for name, label, _ in self._editor_action_specs()}

        def section(*names) -> Gio.Menu:
            m = Gio.Menu()
            for n in names:
                m.append(labels[n], f"editor.{n}")
            return m

        menu = Gio.Menu()
        menu.append_section(None, section("heading-1", "heading-2", "heading-3"))
        menu.append_section(None, section("bold", "italic", "strikethrough",
                                          "inline-code", "code-block"))
        menu.append_section(None, section("blockquote", "bullet-list",
                                          "numbered-list"))
        menu.append_section(None, section("link", "comment"))
        menu.append_section(None, section("find", "find-replace"))
        return menu

    def _build_context_menu(self) -> Gio.Menu:
        """
        Formatting for the text view's own right-click menu.

        This is where a selection-based action belongs: the pointer is already
        on the text, and GNOME users look here for what applies to a selection.
        """
        labels = {name: label for name, label, _ in self._editor_action_specs()}

        fmt = Gio.Menu()
        for name in ("bold", "italic", "strikethrough", "inline-code", "link"):
            fmt.append(labels[name], f"editor.{name}")

        menu = Gio.Menu()
        menu.append_submenu("Format", fmt)
        return menu

    def _build_toolbar(self) -> Gtk.Box:
        """
        Slide structure only, plus an overflow menu.

        The bar used to carry twenty buttons and a comment conceding that it
        clipped; with the slide canvas that used to take half the window it
        clipped six of
        them off with no scrollbar to reach them.  What stays is the work that
        has no keystroke and no equivalent anywhere else — the separators that
        make a Markdown file a deck, and the two inserts that open a popover.
        Formatting moved to the overflow menu and the text context menu, both
        driven by the same action group.
        """
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        bar.set_margin_start(6)
        bar.set_margin_end(6)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        bar.add_css_class("toolbar")

        def icon_btn(icon, tooltip, cb):
            b = Gtk.Button()
            b.set_child(Gtk.Image.new_from_icon_name(icon))
            b.set_tooltip_text(tooltip)
            b.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
            b.add_css_class("flat")
            b.connect("clicked", cb)
            return b

        def sep():
            s = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            s.set_margin_start(4)
            s.set_margin_end(4)
            return s

        # Slide structure — the separators that turn prose into slides.
        bar.append(icon_btn("list-add-symbolic",
                            "New slide (---)", lambda *_: self._new_slide()))
        bar.append(icon_btn("view-dual-symbolic",
                            "Two columns (|||)", lambda *_: self._two_columns()))
        bar.append(icon_btn("document-edit-symbolic",
                            "Speaker notes (^^^)", lambda *_: self._speaker_notes()))
        bar.append(sep())

        self._img_toolbar_btn = icon_btn(
            "insert-image-symbolic", "Insert image",
            lambda *_: (self._insert_image_cb() if self._insert_image_cb
                        else self._choose_image_to_insert())
        )
        bar.append(self._img_toolbar_btn)

        self._tbl_toolbar_btn = icon_btn(
            "view-grid-symbolic", "Insert table",
            lambda *_: self._open_table_popover(self._tbl_toolbar_btn)
        )
        bar.append(self._tbl_toolbar_btn)
        bar.append(sep())

        overflow = Gtk.MenuButton()
        overflow.set_icon_name("view-more-symbolic")
        overflow.set_tooltip_text("Formatting and find")
        overflow.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Formatting and find"]
        )
        overflow.add_css_class("flat")
        overflow.set_menu_model(self._build_overflow_menu())
        bar.append(overflow)

        return bar

    # ── Formatting actions ────────────────────────────────────────────────────

    def bold(self) -> None:    self._wrap("**", "**", "bold text")
    def italic(self) -> None:  self._wrap("*",  "*",  "italic text")
    def insert_link(self) -> None: self._link()

    def insert_comment(self) -> None:
        self._replace_selection("<!-- note:  -->")
        insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        insert.backward_chars(4)
        self._buffer.place_cursor(insert)
        self._view.grab_focus()

    def _selection_bounds(self):
        """
        Return (start, end) iters for the selection, or None if there is none.

        PyGObject's override of gtk_text_buffer_get_selection_bounds() drops
        the C function's gboolean and returns an empty tuple when nothing is
        selected — never the three values the C signature suggests.
        """
        bounds = self._buffer.get_selection_bounds()
        return bounds if bounds else None

    def _get_selection(self):
        bounds = self._selection_bounds()
        if bounds is None:
            return "", False
        start, end = bounds
        return self._buffer.get_text(start, end, True), True

    def _replace_selection(self, text: str) -> None:
        self._buffer.begin_user_action()
        try:
            if self._buffer.get_has_selection():
                self._buffer.delete_selection(True, True)
            self._buffer.insert_at_cursor(text)
        finally:
            self._buffer.end_user_action()
        self._view.grab_focus()

    def _wrap(self, before, after, placeholder) -> None:
        selected, has_sel = self._get_selection()
        text = selected if has_sel else placeholder
        self._replace_selection(f"{before}{text}{after}")
        if not has_sel:
            self._select_placeholder(placeholder, before)

    def _heading(self, level: int) -> None:
        self._buffer.begin_user_action()
        try:
            insert  = self._buffer.get_iter_at_mark(self._buffer.get_insert())
            line_no = insert.get_line()
            ok1, it = self._buffer.get_iter_at_line(line_no)
            if not ok1:
                return
            end_it = it.copy()
            end_it.forward_to_line_end()
            line     = self._buffer.get_text(it, end_it, True)
            stripped = line.lstrip("#").lstrip(" ")
            prefix   = "#" * level + " "
            self._buffer.delete(it, end_it)
            ok2, it2 = self._buffer.get_iter_at_line(line_no)
            if ok2:
                self._buffer.insert(it2, prefix + stripped)
        finally:
            self._buffer.end_user_action()
        self._view.grab_focus()

    def _line_prefix(self, prefix: str) -> None:
        self._buffer.begin_user_action()
        try:
            insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
            ok, it = self._buffer.get_iter_at_line(insert.get_line())
            if ok:
                self._buffer.insert(it, prefix)
        finally:
            self._buffer.end_user_action()
        self._view.grab_focus()

    def _code_block(self) -> None:
        selected, has_sel = self._get_selection()
        code = selected if has_sel else "code here"
        self._replace_selection(f"```\n{code}\n```")
        if not has_sel:
            self._select_placeholder("code here", "```\n")

    def _new_slide(self)     -> None: self._replace_selection("\n\n---\n\n")
    def _two_columns(self)   -> None: self._replace_selection("\n\n|||\n\n")
    def _speaker_notes(self) -> None:
        self._replace_selection("\n\n^^^\n\nSpeaker notes here")

    def _link(self) -> None:
        selected, has_sel = self._get_selection()
        text = selected if has_sel else "link text"
        self._replace_selection(f"[{text}](url)")
        if not has_sel:
            self._select_placeholder("link text", "[")

    def _open_table_popover(self, anchor: Gtk.Widget) -> None:
        """Open the table-dimension picker anchored to *anchor*."""
        if self._table_popover is None:
            self._table_popover = _TableInsertPopover(
                anchor, self._insert_table
            )
        self._table_popover.popup()

    def _insert_table(self, cols: int, rows: int) -> None:
        """Insert a Markdown table skeleton with *cols* columns and *rows* rows."""
        header = '| ' + ' | '.join(f'Col {c+1}' for c in range(cols)) + ' |'
        sep    = '|' + '|'.join('-------' for _ in range(cols)) + '|'
        body   = ('| ' + ' | '.join(' ' * 5 for _ in range(cols)) + ' |\n'
                  ) * (rows - 1)   # rows-1 because header is row 1
        self._replace_selection(f'\n{header}\n{sep}\n{body}')
        self._view.grab_focus()

    def _choose_image_to_insert(self) -> None:
        """
        Ask for an image file and insert it at the cursor.

        There is nothing to configure on the way in any more — the slide
        arranges the picture from its own content — so the button opens the
        file chooser directly instead of a popover of layout controls.

        The file's own name becomes the description. It is a guess, but a
        better starting point than an empty alt, and it is one word away
        from being right.
        """
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Choose image")
        img_filter = Gtk.FileFilter()
        img_filter.set_name("Images")
        for ext in sorted(self._IMAGE_EXTS):
            img_filter.add_pattern(f"*{ext}")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(img_filter)
        dialog.set_filters(store)

        # Keep a strong reference: Gtk.FileDialog.open() is async and the
        # dialog would otherwise be collected before the callback fires.
        self._active_file_dialog = dialog

        def _on_done(dlg, result):
            self._active_file_dialog = None
            try:
                gfile = dlg.open_finish(result)
            except GLib.Error:
                return
            path = gfile.get_path() or gfile.get_uri()
            if not path:
                return
            desc = Path(path).stem.replace("-", " ").replace("_", " ").strip()
            self._on_layout_insert(desc, path)

        dialog.open(self.get_root(), None, _on_done)

    # Characters a slide's image source cannot carry.  The syntax ends the
    # source at whitespace or a closing paren, so a picture called
    # "My Holiday Photo.png" produces a tag that parses as no image at all,
    # and "shot(1).png" produces one truncated to "shot(1".  Presence owns
    # the copy it puts in assets/, so it names that copy something the
    # document can actually refer to.
    _UNSAFE_IN_SRC = re.compile(r'[\s()"\']+')

    @staticmethod
    def _asset_name(src: Path) -> str:
        """A file name for assets/ that a slide's image source can hold."""
        stem = Editor._UNSAFE_IN_SRC.sub("-", src.stem).strip("-")
        return (stem or "image") + src.suffix

    def _copy_into_assets(self, src: Path) -> "str | None":
        """
        Copy *src* next to the document and return the path to write.

        Returns None when there is nowhere to copy to or the copy failed;
        the caller decides what to say about that.  An existing file of the
        same name is reused only when it is the same picture — otherwise a
        second "shot.png" from a different folder would silently replace the
        first one's contents in the document.
        """
        if not self._base_path:
            return None
        assets_dir = self._base_path.parent / "assets"
        try:
            assets_dir.mkdir(exist_ok=True)
            name = self._asset_name(src)
            dst = assets_dir / name
            stem, suffix = Path(name).stem, Path(name).suffix
            counter = 2
            while dst.exists() and not _same_file(src, dst):
                dst = assets_dir / f"{stem}-{counter}{suffix}"
                counter += 1
            if not dst.exists():
                shutil.copy2(src, dst)
            return f"assets/{dst.name}"
        except OSError as exc:
            log.warning("Could not copy image %s into assets: %s", src, exc)
            return None

    def _on_layout_insert(self, alt: str, rel_path: str) -> None:
        src_path = Path(rel_path)
        if src_path.is_absolute() and src_path.exists():
            copied = self._copy_into_assets(src_path)
            if copied is not None:
                rel_path = copied
            elif self._UNSAFE_IN_SRC.search(rel_path):
                # The path cannot be written into a slide as it stands and
                # there is nowhere to put a copy.  A tag that renders nothing
                # is worse than saying why.
                self.emit(
                    "notify-user",
                    f"'{src_path.name}' cannot be linked from an unsaved "
                    f"presentation because of the spaces or brackets in its "
                    f"name. Save the presentation first and Presence will "
                    f"copy the picture next to it."
                )
                return
        self._replace_selection(f"![{alt}]({rel_path})")
        self._view.grab_focus()

    # ── Drag and drop ─────────────────────────────────────────────────────────

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif",
                   ".svg", ".webp", ".bmp", ".tiff"}

    def _on_drop_motion(self, target, x, y) -> Gdk.DragAction:
        files = target.get_value()
        if files is not None:
            for gfile in files.get_files():
                p = Path(gfile.get_path() or "")
                if p.suffix.lower() in self._IMAGE_EXTS:
                    return Gdk.DragAction.COPY
            return 0
        return Gdk.DragAction.COPY

    def _on_drop(self, target, file_list, x, y) -> bool:
        files = file_list.get_files()
        if not files:
            return False
        inserted   = []
        unreadable = []
        unsafe     = []
        for gfile in files:
            path_str = gfile.get_path()
            if not path_str:
                continue
            src    = Path(path_str)
            suffix = src.suffix.lower()
            if suffix not in self._IMAGE_EXTS:
                continue
            if self._base_path:
                rel = self._copy_into_assets(src)
                if rel is None:
                    # Under Flatpak this is normally the sandbox: the drop
                    # carries a real path the app is not permitted to read.
                    # Inserting it anyway produces a tag that silently
                    # renders nothing, which is worse than saying so.
                    unreadable.append(src.name)
                    continue
            elif self._UNSAFE_IN_SRC.search(str(src)):
                # Nowhere to copy to, and the path cannot be written into a
                # slide as it stands.
                unsafe.append(src.name)
                continue
            else:
                rel = str(src)
            alt = src.stem.replace("-", " ").replace("_", " ")
            inserted.append(f"![{alt}]({rel})")

        if unreadable:
            names = ", ".join(unreadable[:3])
            more  = f" and {len(unreadable) - 3} more" if len(unreadable) > 3 else ""
            self.emit("notify-user",
                      f"Could not read {names}{more}. Presence may not have "
                      f"permission to open files in that folder.")
        if unsafe:
            names = ", ".join(unsafe[:3])
            more  = f" and {len(unsafe) - 3} more" if len(unsafe) > 3 else ""
            self.emit("notify-user",
                      f"Could not add {names}{more} to an unsaved presentation "
                      f"because of the spaces or brackets in the name. Save the "
                      f"presentation first and Presence will copy them next to it.")
        if not inserted:
            return False

        target = self._drop_target_iter(x, y)
        snippet = "\n\n" + "\n\n".join(inserted)
        # Leave a blank line after the image unless the document has one
        # already, so repeated drops do not stack up empty lines.
        after = target.copy()
        after.forward_char()
        if not (after.is_end() or after.ends_line()):
            snippet += "\n"

        self._buffer.begin_user_action()
        try:
            self._buffer.insert(target, snippet)
        finally:
            self._buffer.end_user_action()
        self._view.grab_focus()
        return True

    def _drop_target_iter(self, x, y) -> "Gtk.TextIter":
        """
        Where a file dropped at (*x*, *y*) should be inserted.

        Dropping onto a place in the text means "put it here", so the drop
        position is used rather than wherever the cursor happens to be — which
        could be anywhere, including inside the frontmatter, and inserting
        there destroys it.

        Returns an iter at the end of the target line, so the image becomes
        its own paragraph rather than being spliced into a word.
        """
        it = None
        try:
            bx, by = self._view.window_to_buffer_coords(
                Gtk.TextWindowType.WIDGET, int(x), int(y)
            )
            ok, found = self._view.get_iter_at_location(bx, by)
            if ok:
                it = found
        except Exception:
            it = None
        if it is None:
            it = self._buffer.get_iter_at_mark(self._buffer.get_insert())

        line = max(it.get_line(), self._first_body_line())
        ok, target = self._buffer.get_iter_at_line(line)
        if not ok:
            target = self._buffer.get_end_iter()
        if not target.ends_line():
            target.forward_to_line_end()
        return target

    def _first_body_line(self) -> int:
        """
        First line that is not part of the YAML frontmatter.

        An image dropped into the frontmatter block silently breaks the
        document's title, theme and ratio, so nothing is ever inserted above
        this line.
        """
        if not self._is_slide_sep(0):
            return 0
        n = self._buffer.get_line_count()
        for line in range(1, min(n, 50)):
            if self._is_slide_sep(line):
                return line + 1
        return 0

    def set_insert_image_callback(self, cb) -> None:
        self._insert_image_cb = cb

    def set_base_path(self, path) -> None:
        self._base_path = Path(path) if path else None

    def _select_placeholder(self, placeholder: str, before: str) -> None:
        insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        end    = insert.copy()
        start  = insert.copy()
        start.backward_chars(len(placeholder))
        if self._buffer.get_text(start, end, True) == placeholder:
            self._buffer.select_range(start, end)

    # ── Slide-number badge overlay ────────────────────────────────────────────

    def _get_line_text(self, ln: int) -> str:
        """Return the stripped text of line *ln*, or '' on error."""
        ok, it = self._buffer.get_iter_at_line(ln)
        if not ok:
            return ""
        end = it.copy()
        end.forward_to_line_end()
        return self._buffer.get_text(it, end, False).strip()

    def _is_slide_sep(self, ln: int) -> bool:
        """Return True if line *ln* is a slide separator (--- with optional spaces)."""
        return bool(re.match(r'^-{3,}' + r'\s*$', self._get_line_text(ln)))

    def _is_notes_sep(self, ln: int) -> bool:
        """Return True if line *ln* is a notes separator (^^^)."""
        return bool(re.match(r'^\^{3,}' + r'\s*$', self._get_line_text(ln)))

    def _on_view_realize_badges(self, view) -> None:
        """
        Called once after the GtkSourceView is realized.

        Connects the vertical scroll adjustment so the drawn overlays follow
        the text.
        """
        try:
            scroll = view.get_parent()
            if scroll is not None:
                vadj = scroll.get_vadjustment()
                if vadj is not None:
                    def _on_scroll(*_):
                        for area in (getattr(self, "_sep_draw", None),
                                     getattr(self, "_fold_draw", None)):
                            if area is not None:
                                area.queue_draw()
                    vadj.connect("value-changed", _on_scroll)
        except Exception:
            pass

    def _update_badge_starts(self, *_) -> bool:
        """
        Single-pass scan that builds:
          _badge_starts : [(slide_num, badge_line), ...]
          _slide_ranges : [(tint_first, tint_last), ...]

        Model
        -----
        The document body (after any YAML frontmatter) is a sequence of
        slides separated by "---" lines.

        For TINTING each slide's block covers:
          • All lines from the first line of the slide (inclusive) through
            the "---" separator that ends it (inclusive), OR the end of
            the file for the last slide.
          • If a "^^^" notes separator is found within the slide body, the
            tint stops AT the "^^^" line (inclusive) — the prose notes
            below are not tinted.

        For BADGE PLACEMENT the badge appears on the first line that is
        neither blank, "---", nor "^^^".

        Returns GLib.SOURCE_REMOVE so it works as a one-shot idle callback.
        """
        n = self._buffer.get_line_count()

        # ── Skip YAML frontmatter (--- ... ---) ───────────────────────────
        body_start = 0
        frontmatter_end = -1
        if self._is_slide_sep(0):
            for ln in range(1, min(n, 50)):
                if self._is_slide_sep(ln):
                    body_start = ln + 1
                    frontmatter_end = ln
                    break

        # ── Single-pass scan ──────────────────────────────────────────────
        slide_ranges: list[tuple[int, int]] = []
        starts_list:  list[int]             = []
        sep_lines:    list[int]             = []   # separator opening each slide
        titles:       list[str]             = []   # heading text per slide
        heading: str = ""                          # first heading in this slide

        slide_first   = body_start   # first line of current slide
        # Slide 1 opens on the frontmatter's closing fence, if the document
        # has frontmatter; without it, slide 1 simply has no boundary above.
        opening_sep   = frontmatter_end
        pending_sep   = -1           # separator just seen
        badge_line    = -1           # first content line found so far
        in_notes      = False        # True after ^^^ within a slide
        tint_last     = body_start   # last line included in tint so far

        for ln in range(body_start, n):
            t = self._get_line_text(ln)
            is_sep   = self._is_slide_sep(ln)
            is_notes = self._is_notes_sep(ln)

            if is_sep:
                # This separator opens the next slide, so it is where that
                # slide's band belongs.
                pending_sep = ln
                # End of current slide — only commit if there was actual content.
                if badge_line >= 0:
                    # Include the --- line in the tint only when there are no
                    # notes (notes freeze tint_last before the ---).
                    # When notes exist, tint_last is already frozen at ^^^.
                    if not in_notes:
                        tint_last = ln   # extend tint to include the ---
                    slide_ranges.append((slide_first, tint_last))
                    starts_list.append(badge_line)
                    titles.append(heading)
                    sep_lines.append(opening_sep)
                # Start next slide on the following line
                opening_sep = pending_sep
                slide_first = ln + 1
                badge_line  = -1
                heading     = ""
                in_notes    = False
                tint_last   = ln + 1
                continue

            if is_notes:
                # Notes separator — tint stops here (^^^ included)
                if not in_notes:
                    tint_last = ln
                    in_notes  = True
                continue

            if in_notes:
                continue  # skip prose notes

            tint_last = ln  # extend tint to this line
            if badge_line < 0 and t:  # first non-empty, non-sep, non-notes line
                badge_line = ln
            if not heading:
                m = _HEADING_RE.match(t or "")
                if m:
                    heading = _strip_inline_markdown(m.group(1).strip())

        # ── Commit last slide (no trailing ---) ───────────────────────────
        if slide_first < n:
            slide_ranges.append((slide_first, tint_last))
            starts_list.append(
                badge_line if badge_line >= 0 else slide_first
            )
            titles.append(heading)
            sep_lines.append(opening_sep)

        self._badge_starts = list(enumerate(starts_list, start=1))
        self._slide_ranges = slide_ranges

        # The first slide opens the document rather than a separator, so it
        # has no band; every other slide gets one naming what follows it.
        self._sep_bands = [
            (num, sep, title or f"Slide {num}")
            for num, (sep, title) in enumerate(zip(sep_lines, titles), start=1)
            if sep >= 0
        ]

        if getattr(self, "_sep_draw", None) is not None:
            self._sep_draw.queue_draw()
        # Boundaries may have moved under the cursor.
        self._tinted_block = _NO_BLOCK
        self._update_current_slide_tint()
        return GLib.SOURCE_REMOVE

    # ── Slide bands ───────────────────────────────────────────────────────────

    def _line_y(self, line_no: int) -> "float | None":
        """
        Y of *line_no* in overlay coordinates, or None if it is out of view.

        buffer_to_window_coords(TEXT) already accounts for the view's top
        margin, and the view sits at the overlay's origin, so the value needs
        no further adjustment — measured, because the deleted badge code
        added top_margin here and drew everything a margin too low.
        """
        ok, it = self._buffer.get_iter_at_line(line_no)
        if not ok:
            return None
        buf_rect = self._view.get_iter_location(it)
        _x, y = self._view.buffer_to_window_coords(
            Gtk.TextWindowType.TEXT, 0, buf_rect.y
        )
        return float(y)

    def _draw_slide_bands(self, area, cr, width, height) -> None:
        """
        Draw a rule at each slide boundary, naming the slide it opens.

        The separators are the document's real structure, and as bare "---"
        in a monospace stream they were its faintest signal.  Drawn this way
        the source scrolls like a deck, and the slide numbers no longer need
        a second home in the gutter.
        """
        if not self._sep_bands or not self._view.get_realized():
            return

        layout = None
        if _PangoCairo is not None:
            layout = _PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription.from_string("Sans 8"))

        focus = self._focus_range
        for slide_num, sep_line, title in self._sep_bands:
            y = self._line_y(sep_line)
            if y is None or y < -40 or y > height + 40:
                continue

            # In focus mode a full-strength rule across a dimmed page would
            # be the loudest thing left on it.  Only the two that bracket the
            # slide you are in stay lit; the rest recede with their text.
            fade = 1.0
            if focus is not None and sep_line not in (focus[0] - 1,
                                                      focus[1] + 1):
                fade = 0.3

            # Float the rule in the space the separator tag opens above the
            # line, so it never strikes through the "---" glyphs.
            y = round(y - self._SEP_SPACE / 2) + 0.5

            label_w = 0.0
            if layout is not None:
                layout.set_text(f"{slide_num} \u00b7 {title}", -1)
                label_w = layout.get_pixel_size()[0]

            cr.save()
            cr.set_source_rgba(0.55, 0.55, 0.55, 0.45 * fade)
            cr.set_line_width(1.0)
            cr.move_to(0.0, y)
            cr.line_to(max(0.0, width - label_w - 20.0), y)
            cr.stroke()
            cr.restore()

            if layout is not None:
                cr.save()
                cr.set_source_rgba(0.55, 0.55, 0.55, 0.95 * fade)
                cr.move_to(width - label_w - 12.0, y - 7.0)
                _PangoCairo.show_layout(cr, layout)
                cr.restore()

    # ── Fold rules ────────────────────────────────────────────────────────────

    def set_fold_lines(self, lines) -> None:
        """
        Mark the lines where each slide runs out of room.

        *lines* holds one entry per slide — a 0-based line number, or None
        for a slide that fits.  Positions are held as text marks so they
        follow the content as the document is edited, rather than pointing at
        whatever ends up on that line number later.
        """
        for mark in self._fold_marks:
            try:
                self._buffer.delete_mark(mark)
            except Exception:
                pass
        self._fold_marks = []

        last_line = self._buffer.get_line_count() - 1
        for line_no in lines or ():
            if line_no is None or not (0 <= line_no <= last_line):
                continue
            ok, it = self._buffer.get_iter_at_line(line_no)
            if not ok:
                continue
            mark = self._buffer.create_mark(None, it, True)
            self._fold_marks.append(mark)

        if getattr(self, "_fold_draw", None) is not None:
            self._fold_draw.queue_draw()

    def clear_fold_lines(self) -> None:
        """Forget every fold rule."""
        self.set_fold_lines([])

    def _draw_folds(self, area, cr, width, height) -> None:
        """
        Draw a hairline where each slide stops fitting, labelled in the margin.

        Markdown is unbounded and a slide is 1280x720; this is the only place
        that tension is visible while writing rather than after a build.
        """
        if not self._fold_marks or not self._view.get_realized():
            return

        layout = None
        if _PangoCairo is not None:
            layout = _PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription.from_string("Sans 8"))

        focus = self._focus_range
        for mark in self._fold_marks:
            try:
                it = self._buffer.get_iter_at_mark(mark)
            except Exception:
                continue
            line = it.get_line()
            y = self._line_y(line)
            if y is None or y < -20 or y > height + 20:
                continue   # scrolled out of view

            # A red warning about a slide you are not writing is exactly the
            # interruption focus mode exists to remove.
            fade = 1.0
            if focus is not None and not focus[0] <= line <= focus[1]:
                fade = 0.25

            y = round(y) + 0.5          # crisp single-pixel rule
            label_w = 0.0
            if layout is not None:
                layout.set_text("slide is full", -1)
                label_w = layout.get_pixel_size()[0]

            cr.save()
            cr.set_source_rgba(0.85, 0.30, 0.25, 0.75 * fade)
            cr.set_line_width(1.0)
            cr.set_dash([3.0, 3.0])
            cr.move_to(0.0, y)
            cr.line_to(max(0.0, width - label_w - 20.0), y)
            cr.stroke()
            cr.restore()

            if layout is not None:
                cr.save()
                cr.set_source_rgba(0.85, 0.30, 0.25, 0.9 * fade)
                cr.move_to(width - label_w - 12.0, y - 7.0)
                _PangoCairo.show_layout(cr, layout)
                cr.restore()

    # ── Style ─────────────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        """Apply syntax highlighting scheme and separator tag."""
        if not _GTKSOURCE_AVAILABLE:
            return

        mgr = GtkSource.StyleSchemeManager.get_default()
        scheme = mgr.get_scheme("presence-markdown")
        if scheme:
            self._buffer.set_style_scheme(scheme)
        else:
            for name in ("Adwaita", "adwaita", "tango", "classic"):
                s = mgr.get_scheme(name)
                if s:
                    self._buffer.set_style_scheme(s)
                    break

        self._apply_separator_tag()
        self._apply_image_tag()
        self._apply_current_slide_tag()
        self._apply_focus_tag()

    def _apply_current_slide_tag(self) -> None:
        """
        Create (or recolour) the tag that washes the slide you are in.

        paragraph-background spans the full line width and paints beneath the
        text, which an overlay cannot do — a drawn rectangle would sit on top
        of what you are reading.

        The wash is deliberately faint.  It marks which block of the document
        you are working in; anything stronger would compete with the text and
        with the fold rule.
        """
        table = self._buffer.get_tag_table()
        tag = table.lookup("presence-current-slide")
        if tag is None:
            tag = self._buffer.create_tag("presence-current-slide")

        dark = False
        try:
            dark = Adw.StyleManager.get_default().get_dark()
        except Exception:
            pass
        rgba = Gdk.RGBA()
        # Lighten on dark backgrounds, darken on light ones: the same
        # translucent black would only ever muddy a dark theme.
        rgba.parse("rgba(255,255,255,0.05)" if dark else "rgba(0,0,0,0.04)")
        tag.set_property("paragraph-background-rgba", rgba)
        self._tinted_block = _NO_BLOCK      # force a repaint at the new colour

    def _apply_focus_tag(self) -> None:
        """
        Create (or recolour) the tag that dims everything you are not in.

        An opaque grey rather than a translucent one, because flattening the
        syntax colours is the point: text outside the slide you are writing
        should read as one quiet block, not as dimmer highlighting that still
        asks to be parsed.

        One grey, not a light and a dark one.  The wash picks its colour off
        Adw.StyleManager.get_dark(), but presence-markdown.xml pins the
        editor's own background to #ffffff whatever the system is set to, so
        the system's answer does not describe this widget.  Read against the
        background the scheme actually paints instead.
        """
        table = self._buffer.get_tag_table()
        tag = table.lookup("presence-unfocused")
        if tag is None:
            tag = self._buffer.create_tag("presence-unfocused")

        rgba = Gdk.RGBA()
        # Far enough from the background to stay legible when you glance at
        # it — focus mode hides nothing — and far enough from the foreground
        # that the eye does not land there.
        rgba.parse("#b6b6b6")
        tag.set_property("foreground-rgba", rgba)
        self._tinted_block = _NO_BLOCK      # force a repaint at the new colour

    def _current_slide_block(self, line_no: int) -> "tuple[int, int] | None":
        """
        The divider-to-divider line range containing *line_no*.

        Returns None inside the frontmatter, which belongs to no slide.  The
        dividers themselves stay untinted so each slide reads as its own
        block with the band sitting in the gap between.
        """
        last = self._buffer.get_line_count() - 1
        bands = self._sep_bands
        if not bands:
            return (0, last)           # one slide, no separators

        seps = [sep for _num, sep, _title in bands]
        idx = -1
        for i, sep in enumerate(seps):
            if sep <= line_no:
                idx = i
            else:
                break

        if idx == -1:
            # Above every band: frontmatter when band 1 opens slide 1,
            # otherwise the first slide of a document without frontmatter.
            if bands[0][0] == 1:
                return None
            return (0, max(0, seps[0] - 1))

        start = seps[idx] + 1
        end = (seps[idx + 1] - 1) if idx + 1 < len(seps) else last
        return (start, end) if start <= end else None

    def _focus_block(self, line_no: int) -> "tuple[int, int] | None":
        """
        The block focus mode keeps lit, which is not quite the washed one.

        _current_slide_block() returns None in the frontmatter because the
        frontmatter is not a slide and washing it as one would lie.  Focus
        mode asks a different question — what am I editing — and there the
        frontmatter is a perfectly good answer, so it gets lit like any
        other block rather than dimming the whole document.
        """
        bands = self._sep_bands
        if bands and bands[0][0] == 1 and 0 <= line_no < bands[0][1]:
            # Stop before the closing fence: it opens slide 1, and dividers
            # belong to no block, exactly as the wash treats them.
            return (0, bands[0][1] - 1)
        return self._current_slide_block(line_no)

    def _update_current_slide_tint(self, *_) -> None:
        """
        Mark the block the cursor is in; a no-op while it stays put.

        Two ways of saying the same thing, and only ever one at a time: the
        wash tints the block you are in, focus mode takes the contrast out
        of every other one.  Painting both would say it twice.
        """
        table = self._buffer.get_tag_table()
        tag = table.lookup("presence-current-slide")
        if tag is None:
            return

        cursor = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        line   = cursor.get_line()
        block  = (self._focus_block(line) if self._focus_mode
                  else self._current_slide_block(line))
        if block == self._tinted_block:
            return
        self._tinted_block = block
        self._focus_range = block if self._focus_mode else None

        self._buffer.remove_tag(tag, self._buffer.get_start_iter(),
                                self._buffer.get_end_iter())
        self._update_focus_dim(block)
        # The bands and fold rules fade with the text they annotate, so they
        # have to be redrawn whenever the lit block moves.
        for area in (getattr(self, "_sep_draw", None),
                     getattr(self, "_fold_draw", None)):
            if area is not None:
                area.queue_draw()

        if block is None or self._focus_mode:
            return
        ok1, start = self._buffer.get_iter_at_line(block[0])
        if not ok1:
            return
        # End at the start of the divider's line rather than at the end of the
        # block's own: forward_to_line_end() jumps to the *next* line when the
        # iter already sits at one — which an empty line always does — and a
        # zero-length range on a trailing blank line paints nothing at all,
        # leaving a gap above the divider.
        last_line = self._buffer.get_line_count() - 1
        if block[1] < last_line:
            ok2, end = self._buffer.get_iter_at_line(block[1] + 1)
            if not ok2:
                return
        else:
            end = self._buffer.get_end_iter()
        self._buffer.apply_tag(tag, start, end)

    def _update_focus_dim(self, block: "tuple[int, int] | None") -> None:
        """Grey out everything outside *block*; clear it when focus is off."""
        table = self._buffer.get_tag_table()
        tag = table.lookup("presence-unfocused")
        if tag is None:
            return

        self._buffer.remove_tag(tag, self._buffer.get_start_iter(),
                                self._buffer.get_end_iter())
        if not self._focus_mode or block is None:
            return

        # Syntax highlighting colours the same characters, and between two
        # tags the higher priority wins rather than the one applied last.
        # GtkSourceView creates its tags as it highlights, so the table
        # grows behind us and the top has to be re-taken every time.
        tag.set_priority(table.get_size() - 1)

        ok, top = self._buffer.get_iter_at_line(block[0])
        if ok:
            self._buffer.apply_tag(tag, self._buffer.get_start_iter(), top)

        last_line = self._buffer.get_line_count() - 1
        if block[1] < last_line:
            ok2, bottom = self._buffer.get_iter_at_line(block[1] + 1)
            if ok2:
                self._buffer.apply_tag(tag, bottom, self._buffer.get_end_iter())

    def set_focus_mode(self, enabled: bool) -> None:
        """
        Dim every slide but the one the cursor is in.

        The wash inverted: instead of tinting the block you are working in,
        take the contrast away from the rest, so the slide you are writing
        is the only thing on the page at full strength.
        """
        enabled = bool(enabled)
        if enabled == self._focus_mode:
            return
        self._focus_mode = enabled
        self._tinted_block = _NO_BLOCK      # both marks are recomputed from scratch
        self._update_current_slide_tint()

    def get_focus_mode(self) -> bool:
        return self._focus_mode

    def _on_cursor_moved(self, buffer, location, mark) -> None:
        if mark is buffer.get_insert():
            self._update_current_slide_tint()

    def _apply_separator_tag(self) -> None:
        """
        Create a TextTag that highlights slide-separator lines (---).

        Uses @accent_color-equivalent orange that works in both light and
        dark mode.  The tag is re-applied on every buffer change via
        _on_buffer_changed so separators are always highlighted.
        """
        tag_table = self._buffer.get_tag_table()

        # Create the tag once; reuse on subsequent calls
        if tag_table.lookup("presence-separator") is None:
            # The drawn band is what marks a slide boundary now, so the "---"
            # itself is demoted to a quiet reminder that this is still just
            # Markdown you can edit.  It stays visible and editable — hiding
            # it would mean invisible text the cursor can fall into.
            #
            # The line spacing is what the band occupies: without it the rule
            # would strike through the characters instead of floating above
            # them, and slides would still run together as one stream.
            self._buffer.create_tag(
                "presence-separator",
                foreground="#9a9996",
                pixels_above_lines=self._SEP_SPACE,
                pixels_below_lines=self._SEP_SPACE,
            )

        # Initial application — buffer may already have content
        self._highlight_separators()

        # Re-highlight on every change — connect only once using a flag on
        # the buffer to avoid duplicate connections if _apply_style is called again.
        if not getattr(self._buffer, '_separator_connected', False):
            self._buffer.connect("changed", lambda *_: self._highlight_separators())
            self._buffer._separator_connected = True

    def _highlight_separators(self) -> None:
        """Scan the buffer and apply/clear the separator tag on --- lines."""
        tag_table = self._buffer.get_tag_table()
        tag = tag_table.lookup("presence-separator")
        if tag is None:
            return

        # Remove tag from entire buffer first
        start = self._buffer.get_start_iter()
        end   = self._buffer.get_end_iter()
        self._buffer.remove_tag(tag, start, end)

        # Re-apply to every line whose stripped content is exactly "---"
        n = self._buffer.get_line_count()
        for line_no in range(n):
            ok, line_start = self._buffer.get_iter_at_line(line_no)
            if not ok:
                continue
            line_end = line_start.copy()
            line_end.forward_to_line_end()
            text = self._buffer.get_text(line_start, line_end, False)
            if text.strip() == "---":
                self._buffer.apply_tag(tag, line_start, line_end)

    # ── Image-tag syntax highlighting ─────────────────────────────────────────

    # Matches the full Markdown image tag so we can colour sub-spans:
    #   group 1 — "!["          punctuation
    #   group 2 — alt text      (may contain layout tokens separated by |)
    #   group 3 — "]("          punctuation
    #   group 4 — src path      (file path or URL)
    #   group 5 — ")"           punctuation
    _IMAGE_HIGHLIGHT_RE = re.compile(
        r'(!\[)'          # group 1: opening punctuation
        r'([^\]]*)'       # group 2: alt text (layout tokens live here)
        r'(\]\()'         # group 3: middle punctuation
        r'([^)\s"\']+)'   # group 4: src path
        r'(\))',          # group 5: closing punctuation
    )

    def _apply_image_tag(self) -> None:
        """
        Create TextTags for Presence image-layout syntax and wire them up.

        Three visual layers are applied to every ![alt](src) tag:
          • presence-img-punct  — muted colour for ![ ]( ) delimiters
          • presence-img-desc   — dim colour for the description
          • presence-img-src    — dim italic for the file path / URL

        These are GtkTextBuffer tags, not GtkSource language rules, so they
        work even when GtkSourceView is not available and survive theme changes
        because they use fixed colours chosen to contrast on both light/dark
        editor backgrounds.

        The tags are created once and re-applied on every buffer change via
        the same 'changed' signal used by the separator highlighter.
        """
        if not _GTKSOURCE_AVAILABLE:
            return   # plain Gtk.TextBuffer has no create_tag on GtkSource.Buffer

        tag_table = self._buffer.get_tag_table()

        # Punctuation: ![ ]( ) — muted so they don't compete with content
        if tag_table.lookup("presence-img-punct") is None:
            self._buffer.create_tag(
                "presence-img-punct",
                foreground="#888888",
            )

        # The alt text (the human-readable description):
        # slightly muted so the punctuation and path frame it
        if tag_table.lookup("presence-img-desc") is None:
            self._buffer.create_tag(
                "presence-img-desc",
                foreground="#555577",
            )

        # Source path / URL: dim italic — important but secondary
        if tag_table.lookup("presence-img-src") is None:
            self._buffer.create_tag(
                "presence-img-src",
                foreground="#777777",
                style=2,                # Pango.Style.ITALIC
            )

        # Initial scan
        self._highlight_image_tags()

        # Re-scan on every buffer change — connect only once.
        if not getattr(self._buffer, '_image_tag_connected', False):
            self._buffer.connect("changed", lambda *_: self._highlight_image_tags())
            self._buffer._image_tag_connected = True

    def _highlight_image_tags(self) -> None:
        """
        Scan the full buffer text and apply image-layout highlighting tags.

        Runs as a single regex pass over the full buffer text, then converts
        character offsets to TextIters for each match group.  This is O(n)
        in document size but is only triggered by the debounced 'changed'
        signal so it never runs more than once per keystroke batch.
        """
        tag_table = self._buffer.get_tag_table()
        punct_tag = tag_table.lookup("presence-img-punct")
        desc_tag  = tag_table.lookup("presence-img-desc")
        src_tag   = tag_table.lookup("presence-img-src")
        if not all((punct_tag, desc_tag, src_tag)):
            return

        buf_start = self._buffer.get_start_iter()
        buf_end   = self._buffer.get_end_iter()

        # Clear all three tags from the entire buffer before reapplying.
        # This is simpler and safer than trying to diff old vs new ranges.
        for tag in (punct_tag, desc_tag, src_tag):
            self._buffer.remove_tag(tag, buf_start, buf_end)

        full_text = self._buffer.get_text(buf_start, buf_end, False)

        for m in self._IMAGE_HIGHLIGHT_RE.finditer(full_text):
            # Helper: convert a character offset in full_text to a TextIter.
            def _iter(offset: int):
                return self._buffer.get_iter_at_offset(offset)

            open_start  = m.start(1)   # "!["
            open_end    = m.end(1)
            alt_start   = m.start(2)   # alt text content
            alt_end     = m.end(2)
            mid_start   = m.start(3)   # "]("
            mid_end     = m.end(3)
            src_start   = m.start(4)   # file path
            src_end     = m.end(4)
            close_start = m.start(5)   # ")"
            close_end   = m.end(5)

            # Colour ![ and ]( and ) as punctuation
            self._buffer.apply_tag(punct_tag, _iter(open_start),  _iter(open_end))
            self._buffer.apply_tag(punct_tag, _iter(mid_start),   _iter(mid_end))
            self._buffer.apply_tag(punct_tag, _iter(close_start), _iter(close_end))

            # Colour the src path
            self._buffer.apply_tag(src_tag, _iter(src_start), _iter(src_end))

            # The alt text is a plain description now, so it colours as one
            # span — there are no layout tokens left to tell apart from it.
            if full_text[alt_start:alt_end].strip():
                self._buffer.apply_tag(desc_tag, _iter(alt_start), _iter(alt_end))

    # ── Reporting a change ────────────────────────────────────────────────────

    def _on_buffer_changed(self, buffer) -> None:
        self.emit("changed", self.get_text())
