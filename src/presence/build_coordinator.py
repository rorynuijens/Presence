"""
build_coordinator.py — When the PDF gets made, and who is waiting for it.

Saving writes the Markdown; building makes the deck.  They are separate in
both directions — a save builds only if the writer asked for that in
Settings, and a build never saves — and this module owns the second one:
what starts a build, what the header chip says about it, and what runs once
it lands.

Four things live here that used to be spread through the window:

*  **The build's state.**  ``built_text``, ``building_text``, ``converting``,
   ``slide_info``, ``thumbnails``, ``html_uri``, the deck's pixel size and
   the scratch PDF an unsaved deck builds to are all owned here.  They were
   window attributes this class reached in and wrote — so the window, this
   class and the export controller could each move them, and none of them
   owned them.  The window asks (``self._builds.html_uri``); nothing writes
   these but this class.
*  **Whether the build still matches the document.**  Compared by text rather
   than by a modified flag, so undoing back to the built state correctly reads
   as up to date again.
*  **The deferral.**  Everything that consumes the output — Present, the three
   exports made from the PDF, Open PDF — goes through ``with_current_build``,
   so nothing can quietly ship the previous version of the deck.
*  **Fold lines.**  Two sources say where a slide runs out of room and they
   agree, because both come from the same WeasyPrint layout: a build measures
   every slide at once, the live render measures the slide being edited on
   every keystroke.  That one is always the fresher for the slide it
   covers, so its answer wins for that one slide and the build's holds for the
   rest.

The header chip is the window's widget — it sits in the header bar and the
window packs it — but everything it says comes from here, so the window hands
its parts over once with :meth:`attach_chip` rather than being read for them.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from .slides.themes import ASPECT_RATIOS

if TYPE_CHECKING:                       # imported for the annotations only
    from .converter import Converter
    from .document_controller import DocumentController
    from .editor import Editor
    from .sidebar import Sidebar

log = logging.getLogger(__name__)


class BuildHost(Protocol):
    """
    What this class needs from the window it belongs to.  Seven names.

    Written down so it can be checked, and so it cannot quietly grow: the
    controllers used to take an untyped ``window`` and reach for whatever
    they liked on it, which is how forty-seven private attributes ended up
    being this seam.  Also a Gtk.Window, since dialogs are parented on it.
    """

    editor:         Editor
    sidebar:        Sidebar
    converter:      Converter
    documents:      DocumentController
    banner:         Gtk.Widget           # Adw.Banner
    present_button: Gtk.Button
    speaking_rate:  int


class BuildCoordinator:
    """Owns build state, the header chip and fold lines for :class:`MainWindow`."""

    def __init__(self, window: BuildHost) -> None:
        self._win = window

        # ── The build ────────────────────────────────────────────────────────
        # The document as of the last build that landed, and as of the one
        # running now.  The chip compares text rather than tracking a flag.
        self.built_text:    str | None = None
        self.building_text: str | None = None
        self.converting:    bool       = False
        # What the last build produced, for everything made out of it.
        self.slide_info: list = []
        self.thumbnails: list = []
        self.html_uri:   str  = ""
        # The deck's pixel size, from the aspect ratio the build used.
        self.slide_w: int = 1280
        self.slide_h: int = 720
        # Where an unsaved deck's PDF goes.  One scratch file, reused until
        # the deck is saved; there is no scratch Markdown, because the
        # converter is handed the buffer.
        self.temp_pdf: Path | None = None
        # What runs once the build now being asked for lands.
        self._after_build = None

        self._fold_lines: list = []
        # Busy indicators taken by whoever is waiting on the running build,
        # released together when it lands or fails.
        self._waiting: list = []
        # The header chip's parts, handed over by the window once built.
        self._chip = self._visual = self._icon = self._spinner = self._label = None

    # ── Starting a build ──────────────────────────────────────────────────────

    def trigger(self, *_) -> None:
        """
        Build the document as it is on screen right now.

        A build never saves.  It hands the editor's text to the converter,
        so Present, Ctrl+Return and the exports all show what the writer
        sees, without writing anything to disk they did not ask for.
        """
        win  = self._win
        docs = win.documents
        if docs.file_path is None:
            # An unsaved draft has no folder.  Its PDF goes to one scratch
            # file, reused until the deck is saved, and base_dir is None,
            # which tells the converter this is a draft (see sources.py).
            output_path = self._scratch_pdf()
            base_dir    = None
        else:
            output_path = docs.output_path or docs.file_path.with_suffix(".pdf")
            # The document's own directory, never the output's: Export PDF
            # re-points the build at wherever the writer chose to save it,
            # and the deck's pictures still live next to the Markdown.
            base_dir    = docs.file_path.parent

        win.converter.convert(win.editor.get_text(), base_dir, output_path)

    def _scratch_pdf(self) -> Path:
        """The PDF path for a deck that has never been saved."""
        if self.temp_pdf is None:
            fd, tmp_str = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            self.temp_pdf = Path(tmp_str)
        return self.temp_pdf

    def cleanup_scratch(self) -> None:
        """Delete the scratch deck an unsaved document was built to."""
        if self.temp_pdf is None:
            return
        try:
            self.temp_pdf.unlink(missing_ok=True)
        except OSError:
            pass
        self.temp_pdf = None

    def with_current_build(self, action, on_wait=None) -> None:
        """
        Run *action* against a build that matches the document.

        Anything consuming the PDF or HTML goes through here, so no export
        or presentation can quietly ship the previous version of the deck.

        *on_wait* is the busy indicator of the control that asked — the
        Present or Export button — called with True when the action has to
        wait for a build and with False once that build settles, either way.
        A deck that is already current never waits, so it is never called.
        """
        if self.state() == "current" and self.html_uri:
            action()
            return
        self._after_build = action
        if on_wait is not None:
            self.wait_for_build(on_wait)
        self.trigger()

    def build_for_export(self, on_done, on_wait=None) -> None:
        """
        Build the deck to wherever Export just pointed it, and say when it lands.

        Exporting a PDF cannot go through :meth:`with_current_build`: the deck
        may already be current and still need writing to the chosen path, so
        this always builds.  *on_wait* is the Export button, which is the only
        sign the writer gets that a file is on its way — the other three
        formats finish with a toast, and this one does too.  It is taken
        before the trigger, so a build cannot land before the wait is held.
        """
        self._after_build = on_done
        if on_wait is not None:
            self.wait_for_build(on_wait)
        self.trigger()

    def after_build(self, action) -> None:
        """Run *action* once the build about to be asked for lands."""
        self._after_build = action

    def wait_for_build(self, on_wait) -> None:
        """Hold *on_wait* busy until the running build lands or fails."""
        self._waiting.append(on_wait)
        on_wait(True)

    def _settle_waits(self) -> None:
        """Release every indicator waiting on the build that just resolved."""
        waiting, self._waiting = self._waiting, []
        for on_wait in waiting:
            on_wait(False)

    # ── What the chip says ────────────────────────────────────────────────────

    def attach_chip(self, chip, visual, icon, spinner, label) -> None:
        """Take the header chip's parts from the window that built them."""
        self._chip    = chip
        self._visual  = visual
        self._icon    = icon
        self._spinner = spinner
        self._label   = label

    def state(self) -> str:
        """One of 'building', 'stale' or 'current'."""
        if self.converting:
            return "building"
        if self.built_text is None:
            return "stale"
        return "current" if self._win.editor.get_text() == self.built_text else "stale"

    def update_chip(self) -> None:
        """Reflect the build state; safe to call as often as convenient."""
        if self._chip is None:
            return                      # the header bar is not built yet
        state = self.state()

        if state == "building":
            self._visual.set_visible_child_name("spinner")
            self._spinner.start()
            self._label.set_label("Building…")
            self._label.add_css_class("dim-label")
            self._chip.set_sensitive(False)
            self._chip.set_tooltip_text("Building the PDF…")
        else:
            self._spinner.stop()
            self._visual.set_visible_child_name("icon")
            self._chip.set_sensitive(True)
            self._chip.set_tooltip_text("Rebuild now (Ctrl+Return)")
            if state == "current":
                self._icon.set_from_icon_name("object-select-symbolic")
                self._label.set_label("Up to date")
                self._label.add_css_class("dim-label")
            else:
                self._icon.set_from_icon_name("view-refresh-symbolic")
                self._label.set_label("Rebuild needed")
                self._label.remove_css_class("dim-label")

        self._chip.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [f"{self._label.get_label()} — rebuild"],
        )

    # ── Converter signals ─────────────────────────────────────────────────────

    def on_started(self, _converter) -> None:
        win = self._win
        self.converting = True
        # The document as it stands is what this build will contain; on
        # success it becomes the baseline the chip compares against.
        self.building_text = win.editor.get_text()
        win.present_button.set_sensitive(False)
        win.banner.set_revealed(False)
        self.update_chip()
        # Show per-thumbnail spinners so users know thumbnails are updating (#71)
        win.sidebar.set_converting(True)

    def on_complete(self, converter, n_slides: int, duration: float,
                    pdf_path: str, html_uri: str) -> None:
        win = self._win
        self.converting = False
        self.built_text = self.building_text
        self.update_chip()
        win.present_button.set_sensitive(True)
        win.documents.output_path = Path(pdf_path)

        # No toast: a routine build that succeeded is what the chip is for.
        log.debug("Built %d slides in %.2fs", n_slides, duration)
        self.slide_info = converter.slide_info
        self.thumbnails = converter.thumbnails
        self.html_uri   = html_uri

        # Anything that was waiting for a current build can run now.
        if self._after_build is not None:
            pending, self._after_build = self._after_build, None
            pending()

        # Draw the fold rules measured from the page that was just laid out.
        self.set_build_folds(
            [info.get("fold_line") for info in converter.slide_info]
        )

        # Stop thumbnail spinners before replacing content (#71)
        win.sidebar.set_converting(False)
        # Shown against the text this build was made from, not the text being
        # typed now, so every picture lands beside the words it was rendered
        # from.  Where the two have drifted apart the live document follows
        # immediately, carrying these pictures onto the slides they still
        # belong to and marking the rest as out of date.
        win.sidebar.update_from_conversion(
            converter.slide_info, converter.thumbnails,
            markdown_text=self.built_text or "",
            wpm=win.speaking_rate,
        )
        live_text = win.editor.get_text()
        if live_text != self.built_text:
            win.sidebar.update_from_text(live_text)

        # A slide that overflowed, a picture that did not resolve and a theme
        # that is not installed all render as a slide that looks deliberate,
        # so the build has to say so.  Composed by the slides layer, which is
        # also what the CLI prints, so both say the same thing about the same
        # deck.
        warnings = converter.warnings
        if warnings:
            win.banner.set_title(" ".join(warnings))
            win.banner.set_revealed(True)
        else:
            win.banner.set_revealed(False)

        self.slide_w, self.slide_h = ASPECT_RATIOS.get(
            win.converter.ratio, (1280, 720)
        )

        if win.documents.file_path is not None:
            self.cleanup_scratch()
        if win.documents.pres_path:
            win.documents.pack_pres()

        # Last, so that whatever this build re-enabled above cannot leave a
        # button that is still working looking ready.
        self._settle_waits()

    def on_failed(self, _converter, message: str) -> None:
        from .window import _friendly_error

        win = self._win
        self.converting = False
        # Leave built_text alone: a failed build did not change what is on
        # disk, so the chip correctly keeps saying a rebuild is needed.
        self.update_chip()
        win.present_button.set_sensitive(True)
        # Whatever was queued cannot run against a failed build.
        self._after_build = None
        # Stop thumbnail spinners on failure too (#71)
        win.sidebar.set_converting(False)
        # Show a user-friendly message rather than a raw exception string (#87)
        win.banner.set_title(_friendly_error(message))
        win.banner.set_revealed(True)
        self._settle_waits()

    # ── Fold lines ────────────────────────────────────────────────────────────

    @property
    def fold_lines(self) -> list:
        return list(self._fold_lines)

    def set_build_folds(self, folds: list) -> None:
        """Replace every fold line from a completed build."""
        self._fold_lines = list(folds)
        self._win.editor.set_fold_lines(self._fold_lines)

    def set_live_fold(self, index: int, fold_line) -> None:
        """Update one slide's fold line from a live render."""
        if index < 0:
            return
        # A slide added since the last build has no slot yet.
        if index >= len(self._fold_lines):
            self._fold_lines.extend([None] * (index + 1 - len(self._fold_lines)))
        elif self._fold_lines[index] == fold_line:
            return                      # nothing moved; skip the redraw
        self._fold_lines[index] = fold_line
        self._win.editor.set_fold_lines(self._fold_lines)
