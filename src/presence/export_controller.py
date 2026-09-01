"""
export_controller.py — Handing a deck to someone else.

Four formats leave the app: PDF, HTML, a folder of images, and a handout.
They differ only in what they write; the way they ask where to write it is
the same five steps every time — put up a file dialog, take the answer,
default the suffix, refuse a directory that cannot be written, then act.
``_ask_save_path`` is those five steps, once, so each format is left saying
only the thing that makes it different.

The one real asymmetry is kept: exporting a PDF re-points the build at the
chosen path and converts, because the PDF *is* the build's own output.  The
other three consume a build that already matches the document, which is what
``BuildCoordinator.with_current_build()`` guarantees.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, GLib

from .app_utils import make_file_filter, make_filter_store
from .slides.frontmatter import parse_frontmatter

if TYPE_CHECKING:                       # imported for the annotations only
    from .build_coordinator import BuildCoordinator
    from .document_controller import DocumentController
    from .editor import Editor

log = logging.getLogger(__name__)


class ExportHost(Protocol):
    """
    What this class needs from the window it belongs to.

    Written down so it can be checked, and so it cannot quietly grow — see
    :class:`build_coordinator.BuildHost`.  Also a Gtk.Window, since the save
    choosers are parented on it.
    """

    editor:      Editor
    documents:   DocumentController
    builds:      BuildCoordinator
    export_busy: Callable[[bool], None]   # window._BusyIndicator

    def show_toast(self, message: str, timeout: int = ...) -> None: ...
    def show_error(self, message: str) -> None: ...
    def hold_file_dialog(self, dialog: Gtk.FileDialog | None) -> None: ...

# Width, in pixels, of an exported slide image.  1920 is sharp on any normal
# screen without making a folder of PNGs unreasonable to move around.
IMAGE_EXPORT_WIDTH = 1920

# A handout is read at arm's length rather than projected, so its pictures
# need far less resolution than an exported image does.
HANDOUT_IMAGE_WIDTH = 1000


class ExportController:
    """Owns the four export flows on behalf of :class:`MainWindow`."""

    def __init__(self, window: ExportHost) -> None:
        self._win = window

    # ── The one dialog ────────────────────────────────────────────────────────

    def _ask_save_path(
        self,
        title:        str,
        suffix:       str,
        filter_label: str,
        on_chosen,
        initial_name: str | None = None,
        initial_file: Path | None = None,
        transient:    bool = True,
    ) -> None:
        """
        Ask where to save, then call ``on_chosen(path)`` with a usable one.

        *suffix* is appended when the writer did not type one.  A destination
        whose directory cannot be written is reported and *on_chosen* is not
        called — better to say so now than to fail after a build (#67).

        *transient* picks how that refusal is reported: PDF export surfaces it
        in the error banner because the export is the build, while the others
        are one-shot actions and get a toast.
        """
        win = self._win
        dialog = Gtk.FileDialog()
        dialog.set_title(title)

        if initial_name:
            dialog.set_initial_name(initial_name)
        if initial_file is not None:
            dialog.set_initial_file(Gio.File.new_for_path(str(initial_file)))
        folder = self._initial_folder()
        if folder is not None and initial_file is None:
            dialog.set_initial_folder(Gio.File.new_for_path(str(folder)))

        dialog.set_filters(make_filter_store(
            make_file_filter(filter_label, f"*{suffix}")
        ))

        def _response(dlg, result) -> None:
            win.hold_file_dialog(None)
            try:
                gfile = dlg.save_finish(result)
            except GLib.Error:
                return                      # the writer cancelled
            path_str = gfile.get_path()
            if not path_str:
                return
            path = Path(path_str)
            if not path.suffix:
                path = path.with_suffix(suffix)
            if not os.access(path.parent, os.W_OK):
                message = f"Cannot write to '{path.parent}' — permission denied."
                if transient:
                    win.show_toast(message)
                else:
                    win.show_error(message)
                return
            on_chosen(path)

        win.hold_file_dialog(dialog)
        dialog.save(win, None, _response)

    def _initial_folder(self) -> Path | None:
        """Where the dialog should open: beside the document being written."""
        display = self._win.documents.display_path
        return display.parent if display else None

    def _document_stem(self, fallback: str = "presentation") -> str:
        display = self._win.documents.display_path
        return display.stem if display else fallback

    # ── PDF ───────────────────────────────────────────────────────────────────

    def export_pdf(self, *_) -> None:
        docs = self._win.documents
        self._ask_save_path(
            title="Export PDF",
            suffix=".pdf",
            filter_label="PDF files",
            initial_name=(docs.pres_path.stem + ".pdf") if docs.pres_path else None,
            initial_file=(docs.output_path
                          if not docs.pres_path and docs.output_path else None),
            transient=False,
            on_chosen=self._write_pdf,
        )

    def _write_pdf(self, path: Path) -> None:
        # The PDF is the build's own output, so exporting one is just building
        # somewhere else.
        win = self._win
        win.documents.output_path = path
        win.builds.build_for_export(
            lambda: win.show_toast(f"PDF exported → {path.name}")
        )

    # ── HTML ──────────────────────────────────────────────────────────────────

    def export_html(self, *_) -> None:
        """Save a self-contained copy of the generated HTML file."""
        win  = self._win
        docs = win.documents
        self._ask_save_path(
            title="Export HTML",
            suffix=".html",
            filter_label="HTML files",
            initial_name=(docs.pres_path.stem + ".html") if docs.pres_path else None,
            initial_file=(docs.output_path.with_suffix(".html")
                          if not docs.pres_path and docs.output_path else None),
            on_chosen=lambda dest: win.builds.with_current_build(
                lambda: self._copy_built_html(dest), on_wait=win.export_busy
            ),
        )

    def _copy_built_html(self, dest: Path) -> None:
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        win = self._win
        src_path = Path(url2pathname(urlparse(win.builds.html_uri).path))
        try:
            shutil.copy2(src_path, dest)
            win.show_toast(f"HTML exported → {dest.name}")
        except OSError as e:
            win.show_toast(f"Could not export HTML: {e}")

    # ── Images ────────────────────────────────────────────────────────────────

    def export_images(self, *_) -> None:
        """Export each slide as a full-resolution PNG into a chosen folder."""
        win = self._win
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose Export Folder")

        def _response(dlg, result) -> None:
            win.hold_file_dialog(None)
            try:
                gfile = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            path_str = gfile.get_path()
            if not path_str:
                return
            folder = Path(path_str)
            if not folder.is_dir():
                return
            win.builds.with_current_build(
                lambda: self.render_slide_images(folder),
                on_wait=win.export_busy,
            )

        win.hold_file_dialog(dialog)
        dialog.select_folder(win, None, _response)

    def render_slide_images(self, folder: Path) -> None:
        from .slides.thumbnails_render import render_slides_hires

        win = self._win
        pdf_bytes = self._read_built_pdf("No PDF found — convert first.")
        if pdf_bytes is None:
            return

        stem = self._document_stem("slide")
        pages = self._slide_pages()

        # Rasterising a whole deck is seconds of work, so it runs off the
        # main thread and reports back on it — with the Export button held
        # busy meanwhile, since the build it waited for has already let go.
        win.export_busy(True)

        def _render() -> None:
            slides = render_slides_hires(pdf_bytes, width_px=IMAGE_EXPORT_WIDTH,
                                         pages=pages)
            GLib.idle_add(_on_done, slides)

        def _on_done(slides: list) -> bool:
            saved = 0
            for i, png in enumerate(slides):
                if not png:
                    continue
                try:
                    (folder / f"{stem}_{i + 1:02d}.png").write_bytes(png)
                    saved += 1
                except OSError as e:
                    log.warning("Could not write slide PNG: %s", e)
            win.show_toast(
                f"{saved} image{'s' if saved != 1 else ''} exported → {folder.name}/"
            )
            win.export_busy(False)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=_render, daemon=True).start()

    # ── Handout ───────────────────────────────────────────────────────────────

    def export_handout(self, *_) -> None:
        """Export the talk as a document: each slide with its script."""
        win = self._win
        self._ask_save_path(
            title="Export Handout",
            suffix=".pdf",
            filter_label="PDF files",
            initial_name=f"{self._document_stem()}-handout.pdf",
            # A handout is made of slide pictures, so it needs a build that
            # matches the document just as much as any other export does.
            on_chosen=lambda dest: win.builds.with_current_build(
                lambda: self.write_handout(dest), on_wait=win.export_busy
            ),
        )

    def write_handout(self, dest: Path) -> None:
        win = self._win
        pdf_bytes = self._read_built_pdf("No build to make a handout from.")
        if pdf_bytes is None:
            return

        meta, _body = parse_frontmatter(win.editor.get_text())
        slide_info = list(win.builds.slide_info)
        pages = self._slide_pages()
        win.export_busy(True)

        def _render() -> None:
            try:
                from .slides.thumbnails_render import render_slides_hires
                from .slides.handout import build_handout_html
                import weasyprint

                pngs = render_slides_hires(pdf_bytes, width_px=HANDOUT_IMAGE_WIDTH,
                                           pages=pages)
                html = build_handout_html(slide_info, pngs, meta)
                data = weasyprint.HTML(string=html).write_pdf()
            except Exception as exc:
                log.exception("Handout export failed")
                GLib.idle_add(_failed, str(exc))
                return
            GLib.idle_add(_done, data)

        def _done(data: bytes) -> bool:
            try:
                dest.write_bytes(data)
                win.show_toast(f"Handout exported → {dest.name}")
            except OSError as e:
                win.show_toast(f"Could not write the handout: {e}")
            win.export_busy(False)
            return GLib.SOURCE_REMOVE

        def _failed(message: str) -> bool:
            win.show_toast(f"Could not build the handout: {message}")
            win.export_busy(False)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=_render, daemon=True).start()

    # ── Shared ────────────────────────────────────────────────────────────────

    def _slide_pages(self) -> "list[int] | None":
        """
        The PDF page each slide starts on, as the last build measured it.

        None when the build predates the mapping, in which case the
        rasterizers fall back to one page per slide.
        """
        pages = [info.get("page_index") for info in self._win.builds.slide_info]
        return pages if pages and all(p is not None for p in pages) else None

    def _read_built_pdf(self, missing_message: str) -> bytes | None:
        """
        The bytes of the current build, or None with the reason toasted.

        Three of the four exports are made out of the built PDF rather than
        out of the Markdown, so they all need this and all fail the same way.
        """
        win = self._win
        pdf_path = win.documents.output_path
        if pdf_path is None or not pdf_path.exists():
            win.show_toast(missing_message)
            return None
        try:
            return pdf_path.read_bytes()
        except OSError as e:
            win.show_toast(f"Could not read the built PDF: {e}")
            return None
