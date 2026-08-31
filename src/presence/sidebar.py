"""
sidebar.py — Slide thumbnail sidebar with drag-to-reorder.
"""

import difflib
import logging
from dataclasses import dataclass

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GObject, GLib, GdkPixbuf, Gdk, Pango

log = logging.getLogger(__name__)

from .app_utils import png_bytes_to_texture
from .slides.script import Timing, slide_timing


# Size a thumbnail is displayed at.  The live renderer reads the width when
# the canvas is closed and the strip is the only thing asking for pixels.
#
# 240 was small enough that a heading was a smudge and a slide could only be
# told apart from its neighbour by its colour — which left the canvas as the
# only place a slide could actually be looked at.  At this size a heading
# reads, so the strip answers "does this slide look right?" for most slides
# and the canvas is left with the ones where the body text matters.
THUMBNAIL_WIDTH  = 320
THUMBNAIL_HEIGHT = 180

# The strip's own width: a thumbnail plus the row margins either side of it.
SIDEBAR_WIDTH = THUMBNAIL_WIDTH + 20


@dataclass(frozen=True)
class _SlideFacts:
    """What a slide's own source says about it, with no build involved."""

    key:       str          # content identity, for matching rows across edits
    title:     str
    timing:    Timing
    has_notes: bool


@dataclass(frozen=True)
class RowState:
    """What a row on screen is showing, as far as matching is concerned."""

    key:       str
    thumbnail: object = None
    stale:     bool = False
    overflow:  bool = False


def carry_over(previous: "list[RowState]", new_keys: list) -> "list[RowState]":
    """
    Match the rows already on screen to the slides now in the document.

    Position is not identity.  Pairing the two lists off by index means that
    inserting a slide at the top slides every picture below it onto the wrong
    row — correct title, someone else's picture — and leaves it there until
    the next build.  A sequence match over slide content anchors the slides
    that did not change, so only the run that really differs loses its place.

    A slide that was edited keeps its old picture, marked out of date: it is
    still that slide, and blanking it would strobe the strip on every pause
    in typing.  A slide that was inserted has no picture to keep.
    """
    new_keys = list(new_keys)
    carried  = [RowState(key=k, stale=True) for k in new_keys]
    filled: set = set()
    spent:  set = set()

    matcher = difflib.SequenceMatcher(
        a=[r.key for r in previous], b=new_keys, autojunk=False
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            span = i2 - i1
        elif tag == "replace":
            span = min(i2 - i1, j2 - j1)
        else:                                   # insert / delete
            continue
        for off in range(span):
            src = previous[i1 + off]
            carried[j1 + off] = RowState(
                key=new_keys[j1 + off],
                thumbnail=src.thumbnail,
                # An unchanged slide keeps whatever it was; a changed one is
                # out of date by definition, however fresh its picture was.
                stale=src.stale if tag == "equal" else True,
                overflow=src.overflow,
            )
            filled.add(j1 + off)
            spent.add(i1 + off)

    # A slide moved by editing the text rather than by dragging its row reads
    # as a deletion and an insertion, and would arrive at its new position
    # with no picture.  Whatever the sequence match did not place is offered
    # once more by key alone: same content, same slide, same picture.
    loose: dict = {}
    for i, row in enumerate(previous):
        if i not in spent:
            loose.setdefault(row.key, []).append(row)
    for j, key in enumerate(new_keys):
        if j in filled or not loose.get(key):
            continue
        src = loose[key].pop(0)
        carried[j] = RowState(key=key, thumbnail=src.thumbnail,
                              stale=src.stale, overflow=src.overflow)

    return carried


def read_slides(markdown_text: str, wpm: int = 110) -> "list[_SlideFacts]":
    """
    Everything the strip can know from the document alone.

    Both update paths go through here, so a row's number, title, script
    indicator and timing cannot say one thing while a build is fresh and
    something else a keystroke later.  Only the picture and the overflow
    badge are left needing a build, because only they are measured from one.

    The slide is split the way the deck splits it — notes off the body,
    images out of the text — so the words this times are the words the
    presenter view and the handout will time too.
    """
    from .slides.splitter import (split_slides, infer_slide_title,
                                  extract_speaker_notes, extract_images)
    from .slides.frontmatter import parse_frontmatter

    _meta, body = parse_frontmatter(markdown_text)

    facts: list[_SlideFacts] = []
    for i, slide_md in enumerate(split_slides(body)):
        slide_body, notes = extract_speaker_notes(slide_md)
        cleaned, _images  = extract_images(slide_body)
        facts.append(_SlideFacts(
            key=f"{slide_body}\x00{notes}",
            title=infer_slide_title(slide_body, fallback=f"Slide {i + 1}"),
            timing=slide_timing(notes, cleaned, wpm),
            has_notes=bool(notes.strip()),
        ))
    return facts


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
        self.set_size_request(SIDEBAR_WIDTH, -1)

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
        # The document the rows were last read from, so a settings change can
        # re-time them without the window handing the text over again.
        self._source: str = ""
        # Speaking rate the per-slide timings are quoted at.  Held here so a
        # text update can time a slide without the window handing it over on
        # every keystroke; Settings pushes changes in via set_speaking_rate().
        self._wpm: int = 110

    # ── Public API ────────────────────────────────────────────────────────────

    def set_speaking_rate(self, wpm: int) -> None:
        """Re-time every slide at a new words-per-minute setting."""
        if wpm == self._wpm:
            return
        self._wpm = wpm
        for row, timing in zip(self._rows, self._timings_at(wpm)):
            row.set_stats(timing)

    def _timings_at(self, wpm: int) -> list:
        return [f.timing for f in read_slides(self._source, wpm)]

    def set_converting(self, converting: bool) -> None:
        """
        Spin the rows a build is actually going to change.

        Every row used to spin, which said that a build was running but not
        what it was for.  A slide that has not moved since the last build is
        not waiting for anything, and its picture is already right.
        """
        self._converting = converting
        for row in self._rows:
            row.set_converting(converting and row.is_stale())

    def update_from_text(self, markdown_text: str) -> None:
        """
        Re-read the document and show what it says, keeping the pictures.

        Called on every pause in typing.  Titles, timings and the script
        indicator come straight out of the text, so they are never behind;
        the pictures are carried across from the rows already on screen by
        :func:`carry_over`, which matches on slide content rather than on
        position.
        """
        self._source = markdown_text
        facts   = read_slides(markdown_text, self._wpm)
        carried = carry_over(self._row_states(), [f.key for f in facts])
        self._apply(facts,
                    [c.thumbnail for c in carried],
                    [c.stale     for c in carried],
                    [c.overflow  for c in carried])

    def update_from_conversion(self, slide_info: list[dict], thumbnails: list,
                               markdown_text: str, wpm: int = 110) -> int:
        """
        Show the deck a build just made, and return how many slides overflow.

        *markdown_text* is the document the build was made from rather than
        the one being typed now, so every picture lands beside the words it
        was rendered from.  The window follows this with the live text when
        the two have drifted apart.
        """
        self._wpm   = wpm
        self._source = markdown_text
        facts = read_slides(markdown_text, wpm)

        # Overflow is measured from the laid-out page rather than guessed:
        # the converter reports the line where each slide runs out of room,
        # and None when it fits.  A build that predates the measurement
        # falls back to the old >120-word heuristic.
        import re as _re
        overflows = []
        for i, info in enumerate(slide_info):
            fold = info.get("fold_line", "missing")
            if fold != "missing":
                overflows.append(fold is not None)
            else:
                overflows.append(
                    len(_re.findall(r'\S+', info.get('body', ''))) > 120
                )

        # Everything here came out of one build, so nothing on screen is
        # older than the text beside it.
        return self._apply(facts, thumbnails,
                           [False] * len(facts), overflows)

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

    def _row_states(self) -> list:
        """The rows on screen, as :func:`carry_over` needs to see them."""
        return [
            RowState(key=row.key, thumbnail=row.get_thumbnail(),
                     stale=row.is_stale(), overflow=row.is_overflow())
            for row in self._rows
        ]

    def _make_rows(self, count: int) -> None:
        """
        Grow or shrink the strip to *count* rows, reusing the ones that fit.

        Rows are widgets with a selection and a scroll position attached, and
        the strip is refreshed on every pause in typing; tearing all of them
        down each time would drop the selection and jump the list.
        """
        while len(self._rows) > count:
            self._list.remove(self._rows.pop())
        while len(self._rows) < count:
            row = _SlideRow(len(self._rows) + 1, self)
            self._list.append(row)
            self._rows.append(row)

    def _apply(self, facts: list, thumbnails: list,
               stale: list, overflows: list) -> int:
        """Put *facts* on screen and return how many slides overflow."""
        self._make_rows(len(facts))

        for i, f in enumerate(facts):
            row = self._rows[i]
            row.key = f.key
            row.set_number(i + 1)
            row.set_title(f.title)
            row.set_stats(f.timing)
            row.set_has_notes(f.has_notes)
            row.set_thumbnail(thumbnails[i] if i < len(thumbnails) else None)
            row.set_overflow(bool(overflows[i]) if i < len(overflows) else False)
            is_stale = bool(stale[i]) if i < len(stale) else False
            row.set_stale(is_stale)
            row.set_converting(self._converting and is_stale)

        self._update_stack()
        return sum(1 for o in overflows[:len(facts)] if o)

    def set_live_slide(self, index: int, png: bytes, fold_line) -> None:
        """
        Replace one row's picture with a fresh render of the slide being edited.

        The canvas has already laid this slide out with the engine that makes
        the deck, so the strip may as well show the result: the row the writer
        is working on stops waiting for a build, and its overflow badge is
        measured rather than remembered.
        """
        if not (0 <= index < len(self._rows)):
            return
        row = self._rows[index]
        row.set_thumbnail(png)
        row.set_overflow(fold_line is not None)
        row.set_stale(False)
        row.set_converting(False)

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

    _DISPLAY_W = THUMBNAIL_WIDTH
    _DISPLAY_H = THUMBNAIL_HEIGHT

    # How far a picture is faded when it is older than the words beside it.
    # Gentle, because the badge above is what actually says so: a slide on a
    # white theme barely reads as dimmed at all, and one on a black theme
    # reads as broken long before this.
    _STALE_OPACITY = 0.6

    def __init__(self, number: int, sidebar: Sidebar) -> None:
        super().__init__()
        self.index    = number - 1
        self._sidebar = sidebar
        self._png_bytes = None
        self._stale     = False
        # Content identity, set by Sidebar._apply.  What makes a row this
        # slide rather than the slide that happens to sit at its index.
        self.key: str = ""

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

        # Out-of-date marker.  The same icon the header chip uses for
        # "Rebuild needed", because it means the same thing one slide down:
        # what you are looking at is older than what you wrote.  Dimming
        # alone was carrying this and could not — a white slide at 45%
        # opacity on a white strip is still a white slide.
        self._stale_icon = Gtk.Image.new_from_icon_name("view-refresh-symbolic")
        self._stale_icon.set_pixel_size(12)
        self._stale_icon.set_halign(Gtk.Align.END)
        self._stale_icon.set_valign(Gtk.Align.START)
        self._stale_icon.set_margin_end(4)
        self._stale_icon.set_margin_top(4)
        self._stale_icon.set_tooltip_text("Edited since the last build")
        self._stale_icon.add_css_class("dim-label")
        self._stale_icon.set_visible(False)
        overlay.add_overlay(self._stale_icon)

        outer.append(overlay)

        meta_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        meta_row.set_margin_top(4)

        self._num_label = Gtk.Label(label=str(number))
        self._num_label.add_css_class("caption")
        self._num_label.add_css_class("dim-label")
        self._num_label.set_valign(Gtk.Align.CENTER)

        self._title_label = Gtk.Label(label="")
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

    def is_overflow(self) -> bool:
        return self._overflow_badge.get_visible()

    def is_stale(self) -> bool:
        return self._stale

    def set_stale(self, stale: bool) -> None:
        """
        Mark the picture as older than the words beside it.

        Only a row whose own slide has changed since the build is marked, so
        the strip says which slides are out of date instead of shading the
        whole deck on every keystroke.  The header chip is about the deck;
        this is about one slide.
        """
        if stale == self._stale:
            return
        self._stale = stale
        self._stale_icon.set_visible(stale)
        self._refresh_dimming()

    def _refresh_dimming(self) -> None:
        """A picture is dimmed only when there is one and it is out of date."""
        faded = self._stale and self._png_bytes is not None
        self._picture.set_opacity(self._STALE_OPACITY if faded else 1.0)

    def set_stats(self, timing: Timing) -> None:
        """
        Show how long the slide takes, and the words that estimate counted.

        The count is named when it comes from the script, because a slide
        holding six words and ninety seconds of talking otherwise reads as
        a mistake in the arithmetic rather than as a long slide.
        """
        if timing.words == 0:
            self._stats_label.set_visible(False)
            return
        if timing.seconds < 60:
            time_str = f"{timing.seconds}s"
        else:
            m, s = divmod(timing.seconds, 60)
            time_str = f"{m}m {s:02d}s"
        noun = "script words" if timing.from_script else "words"
        self._stats_label.set_label(f"{timing.words} {noun} · ~{time_str}")
        self._stats_label.set_visible(True)

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
        """
        Show *png_bytes* as this slide's picture.

        Returns early when handed the bytes already on screen.  The strip is
        refreshed on every pause in typing and most rows carry their picture
        over unchanged; decoding an identical PNG into a texture each time is
        the entire cost of doing that.
        """
        if png_bytes is self._png_bytes:
            return
        texture = png_bytes_to_texture(png_bytes) if png_bytes else None
        self._png_bytes = png_bytes if texture is not None else None
        self._picture.set_paintable(texture)
        self._refresh_dimming()
