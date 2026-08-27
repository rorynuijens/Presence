"""
build_coordinator.py — When the PDF gets made, and who is waiting for it.

Saving writes the Markdown; building makes the deck.  They are separate, and
this module owns the second one: what starts a build, what the header chip
says about it, and what runs once it lands.

Three things live here that used to be spread through the window:

*  **Whether the build still matches the document.**  Compared by text rather
   than by a modified flag, so undoing back to the built state correctly reads
   as up to date again.
*  **The deferral.**  Everything that consumes the output — Present, the three
   exports made from the PDF, Open PDF — goes through ``with_current_build``,
   so nothing can quietly ship the previous version of the deck.
*  **Fold lines.**  Two sources say where a slide runs out of room and they
   agree, because both come from the same WeasyPrint layout: a build measures
   every slide at once, the canvas measures the slide being edited on every
   keystroke.  The canvas is always the fresher of the two for the slide it
   covers, so its answer wins for that one slide and the build's holds for the
   rest.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from .slides.themes import ASPECT_RATIOS

log = logging.getLogger(__name__)


class BuildCoordinator:
    """Owns build state, the header chip and fold lines for :class:`MainWindow`."""

    def __init__(self, window) -> None:
        self._win = window
        self._fold_lines: list = []

    # ── Starting a build ──────────────────────────────────────────────────────

    def trigger(self, *_) -> None:
        """Build the document as it currently stands."""
        win = self._win
        if win._file_path is None:
            # Nothing on disk yet, so the converter — which reads from a
            # path — gets a temporary copy to read.
            win._cleanup_temp_files()
            fd, tmp_str = tempfile.mkstemp(suffix=".md")
            try:
                os.write(fd, win._editor.get_text().encode("utf-8"))
            finally:
                os.close(fd)
            win._temp_md  = Path(tmp_str)
            win._temp_pdf = win._temp_md.with_suffix(".pdf")
            input_path, output_path = win._temp_md, win._temp_pdf
        else:
            # The converter reads from disk, so pending edits must land first.
            # write_document() rather than save() so auto-convert cannot
            # recurse back into here.
            if win._modified and not win._documents.write_document():
                return
            win._cleanup_temp_files()
            input_path, output_path = win._file_path, win._output_path

        win._converter.convert(input_path, output_path)

    def with_current_build(self, action) -> None:
        """
        Run *action* against a build that matches the document.

        Anything consuming the PDF or HTML goes through here, so no export
        or presentation can quietly ship the previous version of the deck.
        """
        win = self._win
        if self.state() == "current" and win._html_uri:
            action()
            return
        win._after_build = action
        self.trigger()

    # ── What the chip says ────────────────────────────────────────────────────

    def state(self) -> str:
        """One of 'building', 'stale' or 'current'."""
        win = self._win
        if win._converting:
            return "building"
        if win._built_text is None:
            return "stale"
        return "current" if win._editor.get_text() == win._built_text else "stale"

    def update_chip(self) -> None:
        """Reflect the build state; safe to call as often as convenient."""
        win = self._win
        state = self.state()

        if state == "building":
            win._chip_visual.set_visible_child_name("spinner")
            win._chip_spinner.start()
            win._chip_label.set_label("Building…")
            win._chip_label.add_css_class("dim-label")
            win._build_chip.set_sensitive(False)
            win._build_chip.set_tooltip_text("Building the PDF…")
        else:
            win._chip_spinner.stop()
            win._chip_visual.set_visible_child_name("icon")
            win._build_chip.set_sensitive(True)
            win._build_chip.set_tooltip_text("Rebuild now (Ctrl+Return)")
            if state == "current":
                win._chip_icon.set_from_icon_name("object-select-symbolic")
                win._chip_label.set_label("Up to date")
                win._chip_label.add_css_class("dim-label")
            else:
                win._chip_icon.set_from_icon_name("view-refresh-symbolic")
                win._chip_label.set_label("Rebuild needed")
                win._chip_label.remove_css_class("dim-label")

        win._build_chip.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [f"{win._chip_label.get_label()} — rebuild"],
        )

    # ── Converter signals ─────────────────────────────────────────────────────

    def on_started(self, _converter) -> None:
        win = self._win
        win._converting = True
        # The document as it stands is what this build will contain; on
        # success it becomes the baseline the chip compares against.
        win._building_text = win._editor.get_text()
        win._present_btn.set_sensitive(False)
        win._banner.set_revealed(False)
        self.update_chip()
        # Show per-thumbnail spinners so users know thumbnails are updating (#71)
        win._sidebar.set_converting(True)

    def on_complete(self, converter, n_slides: int, duration: float,
                    pdf_path: str, html_uri: str) -> None:
        win = self._win
        win._converting = False
        win._built_text = win._building_text
        self.update_chip()
        win._present_btn.set_sensitive(True)
        win._share_btn.set_sensitive(True)
        win._output_path = Path(pdf_path)

        # No toast: a routine build that succeeded is what the chip is for.
        log.debug("Built %d slides in %.2fs", n_slides, duration)
        win._slide_info = converter.slide_info
        win._thumbnails = converter.thumbnails
        win._html_uri   = html_uri

        # Enable presenter mode now that a conversion exists (#27)
        if win._presenter_action:
            win._presenter_action.set_enabled(True)

        # Anything that was waiting for a current build can run now.
        if win._after_build is not None:
            pending, win._after_build = win._after_build, None
            pending()

        # Draw the fold rules measured from the page that was just laid out.
        self.set_build_folds(
            [info.get("fold_line") for info in converter.slide_info]
        )

        # Stop thumbnail spinners before replacing content (#71)
        win._sidebar.set_converting(False)
        overflow_count = win._sidebar.update_from_conversion(
            converter.slide_info, converter.thumbnails,
            wpm=win._speaking_rate,
        ) or 0
        if overflow_count:
            s = "slide" if overflow_count == 1 else "slides"
            win._banner.set_title(
                f"{overflow_count} {s} may have too much text "
                f"— content could be clipped in the PDF."
            )
            win._banner.set_revealed(True)

        win._slide_w, win._slide_h = ASPECT_RATIOS.get(
            win._converter.ratio, (1280, 720)
        )

        if win._file_path is not None:
            win._cleanup_temp_files()
        if win._pres_path:
            win._pack_pres()

    def on_failed(self, _converter, message: str) -> None:
        from .window import _friendly_error

        win = self._win
        win._converting = False
        # Leave _built_text alone: a failed build did not change what is on
        # disk, so the chip correctly keeps saying a rebuild is needed.
        self.update_chip()
        win._present_btn.set_sensitive(True)
        # Whatever was queued cannot run against a failed build.
        win._after_build = None
        # Stop thumbnail spinners on failure too (#71)
        win._sidebar.set_converting(False)
        # Show a user-friendly message rather than a raw exception string (#87)
        win._banner.set_title(_friendly_error(message))
        win._banner.set_revealed(True)

    # ── Fold lines ────────────────────────────────────────────────────────────

    @property
    def fold_lines(self) -> list:
        return list(self._fold_lines)

    def set_build_folds(self, folds: list) -> None:
        """Replace every fold line from a completed build."""
        self._fold_lines = list(folds)
        self._win._editor.set_fold_lines(self._fold_lines)

    def set_live_fold(self, index: int, fold_line) -> None:
        """Update one slide's fold line from a canvas render."""
        if index < 0:
            return
        # A slide added since the last build has no slot yet.
        if index >= len(self._fold_lines):
            self._fold_lines.extend([None] * (index + 1 - len(self._fold_lines)))
        elif self._fold_lines[index] == fold_line:
            return                      # nothing moved; skip the redraw
        self._fold_lines[index] = fold_line
        self._win._editor.set_fold_lines(self._fold_lines)
