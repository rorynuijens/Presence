"""
sidebar.py — Slide thumbnail sidebar with drag-to-reorder.
"""

import logging

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GObject, GLib, GdkPixbuf, Gdk, Pango

log = logging.getLogger(__name__)

from .app_utils import png_bytes_to_texture


_css_provider_registered = False


def _ensure_css_provider(display) -> None:
    global _css_provider_registered
    if _css_provider_registered:
        return
    css = Gtk.CssProvider()
    try:
        css.load_from_string(
            ".drop-target-highlight {"
            "  background: alpha(@accent_bg_color, 0.2);"
            "  border-top: 2px solid @accent_color;"
            "}"
        )
    except AttributeError:
        css.load_from_data(
            b".drop-target-highlight {"
            b"  background: alpha(@accent_bg_color, 0.2);"
            b"  border-top: 2px solid @accent_color;"
            b"}"
        )
    Gtk.StyleContext.add_provider_for_display(
        display,
        css,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _css_provider_registered = True


class Sidebar(Gtk.Box):
    """
    Vertical panel showing slide thumbnails with drag-to-reorder.

    Signals:
        slide-selected   (index: int)
        slides-reordered (from_index: int, to_index: int)
    """

    __gsignals__ = {
        "slide-selected":      (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "slides-reordered":    (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        # Emitted when the user requests a new slide after *index* (#90)
        "slide-insert-after":  (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        # Emitted on double-click — caller opens a full-size zoom dialog
        "slide-zoom-requested": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_size_request(260, -1)

        # ── Toolbar with "add slide" button (#90) ─────────────────────────────
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        toolbar.add_css_class("toolbar")
        toolbar.set_margin_start(6)
        toolbar.set_margin_end(6)
        toolbar.set_margin_top(4)
        toolbar.set_margin_bottom(4)

        add_btn = Gtk.Button()
        add_btn.set_child(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_btn.set_tooltip_text("Insert slide after selected")
        add_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Insert slide after selected"])
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", self._on_add_slide_clicked)
        toolbar.append(add_btn)

        self.append(toolbar)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Stack: empty state vs. list (#54) ────────────────────────────────
        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)

        # Empty state — shown when no slides exist yet
        empty_page = Adw.StatusPage()
        empty_page.set_icon_name("document-edit-symbolic")
        empty_page.set_title("No slides yet")
        empty_page.set_description("Start writing to see your slides here.")
        empty_page.add_css_class("compact")
        self._stack.add_named(empty_page, "empty")

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-activated", self._on_row_activated)

        _ensure_css_provider(self._list.get_display())

        scroll.set_child(self._list)
        self._stack.add_named(scroll, "list")
        self.append(self._stack)

        self._rows: list[_SlideRow] = []
        self._converting: bool = False   # tracks thumbnail-load state (#71)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_converting(self, converting: bool) -> None:
        """Show/hide per-thumbnail spinner overlays during conversion (#71)."""
        self._converting = converting
        for row in self._rows:
            row.set_converting(converting)

    def update_from_text(self, markdown_text: str) -> None:
        from md_to_slides.slides import split_slides, infer_slide_title
        from md_to_slides.frontmatter import parse_frontmatter

        _, markdown_text = parse_frontmatter(markdown_text)
        slides = split_slides(markdown_text)
        titles = [
            infer_slide_title(s, fallback=f"Slide {i + 1}")
            for i, s in enumerate(slides)
        ]

        if len(titles) == len(self._rows):
            for row, title in zip(self._rows, titles):
                row.set_title(title)
            self._update_stack()
            return

        existing_thumbs = [row.get_thumbnail() for row in self._rows]
        self._rebuild(titles, notes_list=[""] * len(titles),
                      thumbnails=existing_thumbs)

    def update_from_conversion(self, slide_info: list[dict], thumbnails: list,
                               wpm: int = 110) -> None:
        import re as _re
        titles    = [s["title"] for s in slide_info]
        notes     = [s.get("notes", "") for s in slide_info]
        has_notes = [bool(n.strip()) for n in notes]
        word_counts = [
            len(_re.findall(r'\S+', s.get('body', s.get('title', ''))))
            for s in slide_info
        ]
        # Flag slides with >120 words as potentially overflowing
        overflows = [w > 120 for w in word_counts]
        self._rebuild(titles, notes, thumbnails, has_notes,
                      word_counts, wpm, overflows)

    def select_slide(self, index: int) -> None:
        if 0 <= index < len(self._rows):
            self._list.select_row(self._rows[index])

    def scroll_to_index(self, index: int) -> None:
        """
        Scroll the list so the row at *index* is visible, and select it,
        without emitting slide-selected (cursor sync must not cause re-entry).
        Called by the 300ms cursor-polling timer in window.py.
        """
        if not (0 <= index < len(self._rows)):
            return
        row = self._rows[index]
        # Block the row-activated signal so scrolling programmatically does
        # not trigger _on_row_activated and cause an editor scroll loop.
        self._list.handler_block_by_func(self._on_row_activated)
        try:
            self._list.select_row(row)
        finally:
            self._list.handler_unblock_by_func(self._on_row_activated)
        # Scroll the row into view — GTK4 ListBox rows expose this via
        # the underlying Adjustment of the parent ScrolledWindow.
        GLib.idle_add(self._scroll_row_into_view, row)

    def _scroll_row_into_view(self, row: "Gtk.ListBoxRow") -> bool:
        """Scroll *row* into the visible viewport of the list."""
        adj = None
        parent = self._list.get_parent()
        if isinstance(parent, Gtk.ScrolledWindow):
            adj = parent.get_vadjustment()
        if adj is None:
            return GLib.SOURCE_REMOVE
        alloc = row.get_allocation()
        if alloc.height == 0:
            return GLib.SOURCE_REMOVE
        row_top    = alloc.y
        row_bottom = alloc.y + alloc.height
        view_top    = adj.get_value()
        view_bottom = view_top + adj.get_page_size()
        if row_top < view_top:
            adj.set_value(row_top)
        elif row_bottom > view_bottom:
            adj.set_value(row_bottom - adj.get_page_size())
        return GLib.SOURCE_REMOVE

    def selected_index(self) -> int:
        """Return the 0-based index of the selected slide, or -1."""
        row = self._list.get_selected_row()
        if isinstance(row, _SlideRow):
            return row.index
        return len(self._rows) - 1   # default: after last slide

    # ── Private ───────────────────────────────────────────────────────────────

    def _update_stack(self) -> None:
        """Switch between empty state and list depending on row count (#54)."""
        self._stack.set_visible_child_name("list" if self._rows else "empty")

    def _rebuild(self, titles, notes_list, thumbnails,
                 has_notes: list | None = None,
                 word_counts: list | None = None,
                 wpm: int = 110,
                 overflows: list | None = None) -> int:
        for row in self._rows:
            self._list.remove(row)
        self._rows.clear()

        for i, title in enumerate(titles):
            notes = notes_list[i] if i < len(notes_list) else ""
            png   = thumbnails[i] if i < len(thumbnails) else None
            row   = _SlideRow(i + 1, title, notes, png, self)
            if self._converting:
                row.set_converting(True)
            self._list.append(row)
            self._rows.append(row)
            if has_notes is not None and i < len(has_notes):
                row.set_has_notes(has_notes[i])
            if word_counts is not None and i < len(word_counts):
                row.set_stats(word_counts[i], wpm)
            if overflows is not None and i < len(overflows):
                row.set_overflow(overflows[i])

        self._update_stack()
        return sum(overflows) if overflows else 0

    def _on_add_slide_clicked(self, *_) -> None:
        """Emit slide-insert-after with the currently selected index (#90)."""
        self.emit("slide-insert-after", self.selected_index())

    def _on_row_activated(self, listbox, row) -> None:
        if isinstance(row, _SlideRow):
            self.emit("slide-selected", row.index)

    def _on_drop(self, from_index: int, to_index: int) -> None:
        """
        Move the row at *from_index* to *to_index* in both the data list
        and the Gtk.ListBox widget.

        Uses a full rebuild of the ListBox order rather than partial removes
        to ensure the widget order always matches self._rows (fixes #8).
        """
        if from_index == to_index:
            return
        if not (0 <= from_index < len(self._rows)):
            return
        if not (0 <= to_index < len(self._rows)):
            return

        # Update data list
        row = self._rows.pop(from_index)
        self._rows.insert(to_index, row)

        # Rebuild ListBox order by removing all rows then re-appending in
        # the correct sequence.  This is O(n) but n is always small and
        # provably correct regardless of direction.
        for r in self._rows:
            self._list.remove(r)
        for r in self._rows:
            self._list.append(r)

        # Renumber
        for i, r in enumerate(self._rows):
            r.set_number(i + 1)

        self.emit("slides-reordered", from_index, to_index)


class _SlideRow(Gtk.ListBoxRow):
    """A single slide: thumbnail + number + title, with drag-and-drop."""

    # Display at 240×135 — fits a 260px-wide panel with comfortable margins
    # and is large enough to read heading text and see colour accurately.
    _DISPLAY_W = 240
    _DISPLAY_H = 135

    def __init__(self, number, title, notes, png_bytes, sidebar: Sidebar) -> None:
        super().__init__()
        self.index    = number - 1
        self._sidebar = sidebar
        self._png_bytes = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(8)
        outer.set_margin_end(8)

        # Overlay: picture behind, spinner on top during conversion (#71)
        overlay = Gtk.Overlay()
        overlay.set_size_request(self._DISPLAY_W, self._DISPLAY_H)

        self._picture = Gtk.Picture()
        self._picture.set_size_request(self._DISPLAY_W, self._DISPLAY_H)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.add_css_class("card")
        self._set_thumbnail(png_bytes)
        overlay.set_child(self._picture)

        self._spinner = Gtk.Spinner()
        self._spinner.set_halign(Gtk.Align.CENTER)
        self._spinner.set_valign(Gtk.Align.CENTER)
        self._spinner.set_visible(False)
        overlay.add_overlay(self._spinner)

        # No-notes indicator — shown when a slide has content but no speaker
        # notes, so the presenter knows before going on stage.
        # document-edit-symbolic communicates "notes are missing / need writing".
        self._no_notes_icon = Gtk.Image.new_from_icon_name(
            'document-edit-symbolic'
        )
        self._no_notes_icon.set_pixel_size(12)
        self._no_notes_icon.set_halign(Gtk.Align.END)
        self._no_notes_icon.set_valign(Gtk.Align.END)
        self._no_notes_icon.set_margin_end(4)
        self._no_notes_icon.set_margin_bottom(4)
        self._no_notes_icon.set_tooltip_text('No speaker notes')
        self._no_notes_icon.set_visible(False)
        # Tint yellow so it's noticeable but not alarming
        self._no_notes_icon.add_css_class('warning')
        overlay.add_overlay(self._no_notes_icon)

        # Overflow badge — shown when a slide has too much text.
        # Uses the "error" CSS class so the colour follows Adwaita's semantic
        # destructive palette in both light and dark mode and high-contrast.
        self._overflow_badge = Gtk.Label(label="FULL")
        self._overflow_badge.add_css_class("caption")
        self._overflow_badge.add_css_class("error")
        self._overflow_badge.set_halign(Gtk.Align.START)
        self._overflow_badge.set_valign(Gtk.Align.START)
        self._overflow_badge.set_margin_start(4)
        self._overflow_badge.set_margin_top(4)
        self._overflow_badge.set_tooltip_text(
            "This slide may have too much text — content could be clipped in the PDF"
        )
        self._overflow_badge.set_visible(False)
        overlay.add_overlay(self._overflow_badge)

        outer.append(overlay)

        meta_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        meta_row.set_margin_top(4)

        self._num_label = Gtk.Label(label=str(number))
        self._num_label.add_css_class("caption")
        self._num_label.add_css_class("dim-label")
        self._num_label.set_valign(Gtk.Align.CENTER)

        self._title_label = Gtk.Label(label=title)
        self._title_label.add_css_class("caption")
        self._title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._title_label.set_xalign(0)
        self._title_label.set_hexpand(True)

        meta_row.append(self._num_label)
        meta_row.append(self._title_label)
        outer.append(meta_row)

        # Per-slide word count + estimated time
        self._stats_label = Gtk.Label(label="")
        self._stats_label.add_css_class("caption")
        self._stats_label.add_css_class("dim-label")
        self._stats_label.set_xalign(0)
        self._stats_label.set_margin_bottom(2)
        self._stats_label.set_visible(False)
        outer.append(self._stats_label)

        self.set_child(outer)

        # Double-click opens the full-size zoom dialog
        click_ctrl = Gtk.GestureClick()
        click_ctrl.set_button(1)   # primary mouse button
        click_ctrl.connect("released", self._on_click_released)
        outer.add_controller(click_ctrl)

        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect("prepare",    self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        outer.add_controller(drag_source)

        drop_target = Gtk.DropTarget.new(GObject.TYPE_INT, Gdk.DragAction.MOVE)
        drop_target.connect("drop",   self._on_drop)
        drop_target.connect("motion", self._on_drop_motion)
        drop_target.connect("leave",  self._on_drop_leave)
        outer.add_controller(drop_target)

    # ── Public helpers ────────────────────────────────────────────────────────

    def set_title(self, title: str) -> None:
        self._title_label.set_label(title)

    def set_number(self, number: int) -> None:
        self.index = number - 1
        self._num_label.set_label(str(number))

    def get_thumbnail(self):
        return self._png_bytes

    def set_thumbnail(self, png_bytes) -> None:
        self._set_thumbnail(png_bytes)

    def set_overflow(self, overflow: bool) -> None:
        """Show/hide the FULL overflow warning badge."""
        self._overflow_badge.set_visible(overflow)

    def set_stats(self, words: int, wpm: int) -> None:
        """Show word count and estimated speaking time below the title."""
        if words == 0:
            self._stats_label.set_visible(False)
            return
        secs = max(1, round(words / wpm * 60))
        if secs < 60:
            time_str = f"{secs}s"
        else:
            m, s = divmod(secs, 60)
            time_str = f"{m}m {s:02d}s"
        # Warn visually if slide is very dense (>80 words)
        self._stats_label.set_label(f"{words} words · ~{time_str}")
        self._stats_label.set_visible(True)
        if words > 80:
            self._stats_label.add_css_class("warning")
            self._stats_label.set_tooltip_text(
                "This slide has a lot of text — consider splitting it"
            )
        else:
            self._stats_label.remove_css_class("warning")
            self._stats_label.set_tooltip_text("")

    def set_has_notes(self, has_notes: bool) -> None:
        """Show/hide the no-notes warning indicator."""
        self._no_notes_icon.set_visible(not has_notes)

    def set_converting(self, converting: bool) -> None:
        """Show/hide spinner overlay to indicate thumbnail is being rebuilt (#71)."""
        if converting:
            self._spinner.set_visible(True)
            self._spinner.start()
        else:
            self._spinner.stop()
            self._spinner.set_visible(False)

    # ── Click callbacks ───────────────────────────────────────────────────────

    def _on_click_released(self, ctrl, n_press, x, y) -> None:
        """Emit slide-zoom-requested on double-click."""
        if n_press == 2:
            self._sidebar.emit("slide-zoom-requested", self.index)

    # ── Drag callbacks ────────────────────────────────────────────────────────

    def _on_drag_prepare(self, source, x, y):
        value = GObject.Value()
        value.init(GObject.TYPE_INT)
        value.set_int(self.index)
        return Gdk.ContentProvider.new_for_value(value)

    def _on_drag_begin(self, source, drag) -> None:
        if self._png_bytes:
            texture = png_bytes_to_texture(self._png_bytes)
            if texture is not None:
                Gtk.DragIcon.set_from_paintable(drag, texture, 0, 0)
                return
        Gtk.DragIcon.get_for_drag(drag).set_child(
            Gtk.Label(label=self._title_label.get_label())
        )

    def _on_drop(self, target, value, x, y) -> bool:
        from_index = int(value)
        # Look up self.index via the live list to guard against stale cache
        # (fixes #6 — self.index could be stale if a prior reorder didn't
        # complete its renumber before another drag started).
        try:
            to_index = self._sidebar._rows.index(self)
        except ValueError:
            to_index = self.index
        self._remove_highlight()
        self._sidebar._on_drop(from_index, to_index)
        return True

    def _on_drop_motion(self, target, x, y) -> Gdk.DragAction:
        self.add_css_class("drop-target-highlight")
        return Gdk.DragAction.MOVE

    def _on_drop_leave(self, target) -> None:
        self._remove_highlight()

    def _remove_highlight(self) -> None:
        self.remove_css_class("drop-target-highlight")

    # ── Thumbnail ─────────────────────────────────────────────────────────────

    def _set_thumbnail(self, png_bytes) -> None:
        self._png_bytes = png_bytes
        if png_bytes:
            texture = png_bytes_to_texture(png_bytes)
            if texture is not None:
                self._picture.set_paintable(texture)
                return
        self._png_bytes = None
        self._picture.set_paintable(None)
