"""
build_coordinator.py — When the PDF gets made, and who is waiting for it.

Saving writes the Markdown; building makes the deck.  They are separate in
both directions — a save builds only if the writer asked for that in
Settings, and a build never saves — and this module owns the second one:
what starts a build, what the header chip says about it, and what runs once
it lands.

Three things live here that used to be spread through the window:

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
        # Busy indicators taken by whoever is waiting on the running build,
        # released together when it lands or fails.
        self._waiting: list = []

    # ── Starting a build ──────────────────────────────────────────────────────

    def trigger(self, *_) -> None:
        """
        Build the document as it currently stands.

        **A build does not save.**  The editor's buffer is the document, and
        that is what goes to the converter — so Present, Ctrl+Return, the
        chip and the three exports made from the PDF all render what the
        writer is looking at without writing it anywhere the writer did not
        ask for.  It used to be the other way round: the converter read from
        a path, so a build first wrote the buffer over the file on disk, and
        an untitled document got a temporary copy of itself to be read back.
        """
        win = self._win
        if win._file_path is None:
            # No document directory, so relative image sources have nothing
            # to resolve against and the PDF has nowhere of its own to go.
            # One scratch file serves every build until the deck is saved.
            output_path = self._scratch_pdf()
            base_dir    = output_path.parent
        else:
            output_path = win._output_path or win._file_path.with_suffix(".pdf")
            # The document's own directory, never the output's: Export PDF
            # re-points the build at wherever the writer chose to save it,
            # and the deck's pictures still live next to the Markdown.
            base_dir    = win._file_path.parent

        win._converter.convert(win._editor.get_text(), base_dir, output_path)

    def _scratch_pdf(self) -> Path:
        """The PDF path for a deck that has never been saved."""
        win = self._win
        if win._temp_pdf is None:
            fd, tmp_str = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            win._temp_pdf = Path(tmp_str)
        return win._temp_pdf

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
        win = self._win
        if self.state() == "current" and win._html_uri:
            action()
            return
        win._after_build = action
        if on_wait is not None:
            self.wait_for_build(on_wait)
        self.trigger()

    def after_build(self, action) -> None:
        """Run *action* once the build about to be asked for lands."""
        self._win._after_build = action

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
        win._output_path = Path(pdf_path)

        # No toast: a routine build that succeeded is what the chip is for.
        log.debug("Built %d slides in %.2fs", n_slides, duration)
        win._slide_info = converter.slide_info
        win._thumbnails = converter.thumbnails
        win._html_uri   = html_uri

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
        # Shown against the text this build was made from, not the text being
        # typed now, so every picture lands beside the words it was rendered
        # from.  Where the two have drifted apart the live document follows
        # immediately, carrying these pictures onto the slides they still
        # belong to and marking the rest as out of date.
        overflow_count = win._sidebar.update_from_conversion(
            converter.slide_info, converter.thumbnails,
            markdown_text=win._built_text or "",
            wpm=win._speaking_rate,
        ) or 0
        live_text = win._editor.get_text()
        if live_text != win._built_text:
            win._sidebar.update_from_text(live_text)
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

        # Last, so that whatever this build re-enabled above cannot leave a
        # button that is still working looking ready.
        self._settle_waits()

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
        self._settle_waits()

    # ── Fold lines ────────────────────────────────────────────────────────────

    @property
    def fold_lines(self) -> list:
        return list(self._fold_lines)

    def set_build_folds(self, folds: list) -> None:
        """Replace every fold line from a completed build."""
        self._fold_lines = list(folds)
        self._win._editor.set_fold_lines(self._fold_lines)

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
        self._win._editor.set_fold_lines(self._fold_lines)
