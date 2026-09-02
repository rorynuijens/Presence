"""
settle_clock.py — when a keystroke becomes a picture, and in what order.

Every fact in this app has an owner.  *When things happen* did not, and the
typing pipeline was where that showed: a keystroke fanned out across five
timers in two files, two of them racing, and the race was settled by one
subsystem reaching into another's pending source and running its callback
early.  This class is that owner.

**One clock, two fuses.**  The editor now reports a buffer change the moment
it happens and keeps no timer of its own.  What that change is worth waiting
for is decided here: :data:`LIVE_MS` after the last keystroke the slide under
the cursor goes off to be laid out, and :data:`SETTLE_MS` after it the
document is called settled and everything read straight out of the text —
the strip's titles, timings and script marks, the inspector, the word count,
the header chip — catches up.  The second is a fallback: when a picture comes
back first, it brings the words with it.

**Words and picture are published together, or not at all.**  That is the
invariant, and it is the whole reason this class exists.  A frame is only
ever handed to the strip whose rows were read from the very text that frame
was laid out from, because the row indices the frame counts have to be the
rows the strip is showing.  So a frame does one of three things:

*  its text is what the strip already shows — hand the picture over;
*  its text is the newest text there is — publish those words first, then
   the picture, and the settle timer has nothing left to do;
*  its text is neither — the writer typed again while it was rendering.
   Drop it.  A render of the newer text was armed by that same keystroke and
   is at most :data:`LIVE_MS` away.

The third case is the one the old arrangement got wrong: it forced the
strip *back* to the frame's older words, cancelling the pending update that
carried the newer ones.  It healed itself on the next frame, and did not
when that frame failed to render — leaving the titles and timings one edit
stale until the writer typed again, with nothing on screen saying so.

**Which slide.**  The cursor is polled rather than watched, because a
GtkTextView moves its insertion point for many reasons and only its position
matters here.  That poll is also this class's backstop: the buffer can be
replaced without a signal (:meth:`Editor.set_text` blocks its handler so a
document swap is not an edit), and a poll that finds text nobody announced
treats it as one rather than showing the old deck forever.  Callers that
replace the buffer deliberately say so with :meth:`document_replaced` and do
not wait for it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from gi.repository import GLib

from .slides.utils import compute_slide_offsets

if TYPE_CHECKING:                       # imported for the annotations only
    from .build_coordinator import BuildCoordinator
    from .converter import Converter
    from .document_controller import DocumentController
    from .editor import Editor
    from .sidebar import Sidebar

log = logging.getLogger(__name__)


class ClockHost(Protocol):
    """
    What this class needs from the window it belongs to.  Nine names.

    Written down so it can be checked, and so it cannot quietly grow — see
    :class:`build_coordinator.BuildHost`.
    """

    editor:    Editor
    sidebar:   Sidebar
    converter: Converter
    documents: DocumentController
    builds:    BuildCoordinator

    def live_render_width(self) -> int | None: ...
    def mark_modified(self) -> None: ...
    def sync_panel_to_document(self, text: str) -> None: ...
    def update_word_count(self, text: str) -> None: ...


class SettleClock:
    """Owns the timing of the typing pipeline for :class:`MainWindow`."""

    #: Pause after the last keystroke before the document counts as settled.
    SETTLE_MS = 400
    #: Shorter fuse for the picture, which has to keep up with typing.
    LIVE_MS   = 150
    #: How often the insertion point is read.
    CURSOR_MS = 300
    #: Pause after the size slider stops moving before re-rendering at it.
    RESIZE_MS = 250

    def __init__(self, window: ClockHost) -> None:
        self._win = window

        #: The slide the cursor is in — which is the one that gets rendered.
        self.current_slide: int = 0

        self._settle_source: int | None = None
        self._live_source:   int | None = None
        self._resize_source: int | None = None
        self._cursor_source: int | None = None

        # The three texts this class keeps apart, and why.  _pending is the
        # buffer as last reported: the newest truth there is.  _published is
        # what the strip and the inspector are currently showing.  _frame is
        # what the render now in flight was handed.  A frame is only usable
        # against a strip built from its own text, so all three are needed to
        # know which of them that is.
        self._pending_text:   str = ""
        self._published_text: str = ""
        self._frame_text:     str = ""

        # Parsed slide offsets for the cursor poll, kept so an unchanged
        # document is not re-split three times a second.
        self._cursor_cache: tuple[str, list[int]] | None = None

    # ── Running ───────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin following the cursor.  Idempotent."""
        if self._cursor_source is None:
            self._cursor_source = GLib.timeout_add(
                self.CURSOR_MS, self._poll_cursor)

    def stop(self) -> None:
        """Release every timer.  The window calls this on the way out."""
        for attr in ("_settle_source", "_live_source",
                     "_resize_source", "_cursor_source"):
            self._cancel(attr)

    # ── What the editor reports ───────────────────────────────────────────────

    def on_editor_changed(self, _editor, text: str) -> None:
        """The buffer changed.  Bound straight to the editor's signal."""
        self._pending_text = text
        # Immediately, because the document became modified at the keystroke
        # and not 400 ms later.  Both setters underneath compare before they
        # notify, so a title that has not moved costs nothing.
        self._win.mark_modified()
        self._arm("_live_source",   self.LIVE_MS,   self.refresh)
        self._arm("_settle_source", self.SETTLE_MS, self._settle)

    def document_replaced(self, text: str, slide: int | None = None) -> None:
        """
        The buffer was replaced wholesale — opened, recovered, reordered.

        Nothing about that is worth debouncing: the writer is not mid-word,
        and the strip would otherwise show the previous document for another
        fifth of a second.  Anything already in flight is for text that no
        longer exists, so it goes.

        *slide* is where to leave the cursor's slide; ``None`` keeps it where
        it was, which is what an edit that rewrote the text under a stationary
        cursor — a frontmatter key, a reorder — wants.
        """
        for attr in ("_settle_source", "_live_source", "_resize_source"):
            self._cancel(attr)
        self._pending_text = text
        if slide is not None:
            self.current_slide = slide
        self._cursor_cache = None
        self._publish(text)
        self.refresh()

    def go_to_slide(self, index: int) -> None:
        """Render *index* instead — the strip was clicked, or a slide added."""
        self.current_slide = index
        self.refresh()

    def request_render_soon(self) -> None:
        """
        Re-render once the size slider settles.

        The strip resizes immediately without this — its pictures are
        rasterized larger than the slider can ask for — but the row under the
        cursor came from the live path at the old width, so it alone has to
        be made again.
        """
        self._arm("_resize_source", self.RESIZE_MS, self.refresh)

    # ── Publishing ────────────────────────────────────────────────────────────

    def _settle(self) -> None:
        """The writer stopped typing and no picture arrived first."""
        self._publish(self._pending_text)

    def _publish(self, text: str) -> bool:
        """
        Show *text* everywhere that reads the document directly.

        Returns whether anything moved, and cancels the settle timer when it
        did: this state is published, and there is nothing left to wait for.
        """
        if text == self._published_text:
            return False
        self._published_text = text
        self._cancel("_settle_source")

        win = self._win
        win.sidebar.update_from_text(text)
        win.sync_panel_to_document(text)
        win.update_word_count(text)
        win.builds.update_chip()
        return True

    # ── The picture ───────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Ask for the slide under the cursor, at the text last reported."""
        for attr in ("_live_source", "_resize_source"):
            self._cancel(attr)

        width = self._win.live_render_width()
        if width is None:               # strip put away, or nothing to draw with
            return

        text = self._pending_text
        self._frame_text = text
        self._win.converter.render_slide_async(
            text, self._win.documents.base_dir or Path.home(),
            self.current_slide, width, self._on_frame,
        )

    def _on_frame(self, frame, error) -> None:
        """A rendered slide, back on the main thread."""
        if error is not None:
            # Not raised to the writer: a slide that will not render leaves
            # its row marked out of date, which is true and already visible,
            # and a banner on every keystroke through a half-typed slide
            # would be noise.  A real fault is reported by the build.
            log.debug("Live slide render failed", exc_info=error)
            return

        text = self._frame_text
        if text != self._published_text and not self._publish_if_newest(text):
            # The words have moved on since this was handed out, and the
            # render of the newer ones is already armed.  Showing this
            # picture would put it against rows it was not counted from.
            return

        # Laid out by the engine that makes the deck, and fresher than the
        # build's answer for this one slide, so both the picture and the fold
        # it measured are taken as they are.
        self._win.sidebar.set_live_slide(frame.index, frame.png, frame.fold_line)
        self._win.builds.set_live_fold(frame.index, frame.fold_line)

    def _publish_if_newest(self, text: str) -> bool:
        """Let the words catch up to a picture, but only forwards."""
        if text != self._pending_text:
            return False
        self._publish(text)
        return True

    # ── Which slide the cursor is in ──────────────────────────────────────────

    def _poll_cursor(self) -> bool:
        try:
            win    = self._win
            text   = win.editor.get_text()
            offset = win.editor.get_cursor_offset()

            # A buffer that changed without saying so — set_text() blocks the
            # editor's own handler.  Deliberate replacements announce
            # themselves; this is what catches the ones that do not.
            if text != self._pending_text:
                self.on_editor_changed(win.editor, text)

            if self._cursor_cache is None or self._cursor_cache[0] != text:
                self._cursor_cache = (text, compute_slide_offsets(text))
            offsets = self._cursor_cache[1]

            current = 0
            for i, start in enumerate(offsets):
                if offset >= start:
                    current = i
                else:
                    break

            # Not gated on a build: the strip is read out of the text, so it
            # has rows to select from the first keystroke.
            win.sidebar.scroll_to_index(current)

            # Edits inside one slide are already covered by the live fuse;
            # this is for the cursor crossing into another one.
            if current != self.current_slide:
                self.go_to_slide(current)

            # Which picture the writer is on, for an inspector already
            # showing one.  The editor reports only when the answer moves,
            # and opening the panel is a click's business, not a poll's.
            win.editor.check_cursor_for_image()
        except Exception:
            log.debug("Cursor sync error", exc_info=True)
        return GLib.SOURCE_CONTINUE

    # ── Timers ────────────────────────────────────────────────────────────────

    def _arm(self, attr: str, delay_ms: int, callback) -> None:
        """(Re)start one named timer.  The name is cleared before it fires."""
        self._cancel(attr)

        def _fire() -> bool:
            setattr(self, attr, None)
            callback()
            return GLib.SOURCE_REMOVE

        setattr(self, attr, GLib.timeout_add(delay_ms, _fire))

    def _cancel(self, attr: str) -> None:
        source = getattr(self, attr, None)
        if source is not None:
            GLib.source_remove(source)
            setattr(self, attr, None)
