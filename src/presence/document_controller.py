"""
document_controller.py — The document as a file on disk.

Opening, saving, saving-as, autosaving, the unsaved-changes question and the
`.pres` bundle, in one place.  All of it is about the Markdown; none of it is
about the build, which is :mod:`build_coordinator`'s subject.

Three things are worth keeping in view while reading this:

*  **This owns where the document is.**  ``file_path``, ``output_path``,
   ``pres_path``, ``pres_temp_dir`` and ``modified`` live here, not on the
   window.  They used to be window attributes that this class reached in and
   wrote, which meant the window and both other controllers could each move
   the document and none of them owned it.  The window asks
   (``self._documents.file_path``); nothing writes these but this class.
*  **Save never builds.**  Writing the file and producing a PDF are separate
   verbs, and conflating them is what used to put seconds between Ctrl+S and
   being able to type again.  ``save()`` converts afterwards only when the
   writer has asked for that in Settings.
*  **A .pres bundle is a directory pretending to be a file.**  ``file_path``
   is always the Markdown inside it, ``pres_path`` the bundle the writer
   thinks they are editing; the title, the recent list and the recovery key
   all follow ``pres_path`` when there is one — which is what
   ``display_path`` answers.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib

from .app_utils import make_file_filter, make_filter_store
from .session import (save_last_file, save_recent_file, delete_recovery_file,
                      recovery_path_for, recovery_dir)

if TYPE_CHECKING:                       # imported for the annotations only
    from .build_coordinator import BuildCoordinator
    from .editor import Editor
    from .sidebar import Sidebar

log = logging.getLogger(__name__)

# The name a document has before it has been saved anywhere.
UNTITLED = "Untitled"


class DocumentHost(Protocol):
    """
    What this class needs from the window it belongs to.

    Written down so it can be checked, and so it cannot quietly grow — see
    :class:`build_coordinator.BuildHost`.  Also a Gtk.Window: the Save As
    chooser and the unsaved-changes question are both parented on it.
    """

    editor:         Editor
    sidebar:        Sidebar
    builds:         BuildCoordinator
    # Its own, read off the *other* windows when checking for a second copy
    # of a file that is already open.
    documents:      "DocumentController"
    auto_convert:   bool
    current_slide:  int

    def show_toast(self, message: str, timeout: int = ...) -> None: ...
    def show_error(self, message: str) -> None: ...
    def set_document_title(self, name: str) -> None: ...
    def hold_file_dialog(self, dialog: Gtk.FileDialog | None) -> None: ...
    def sync_panel_to_document(self, text: str) -> None: ...
    def refresh_live_slide(self, text: str | None = ...) -> None: ...
    def update_word_count(self, text: str) -> None: ...
    def refresh_recent_actions(self) -> None: ...
    def get_application(self) -> Gtk.Application: ...


class DocumentController:
    """Owns where the document is, and every way it reaches or leaves disk."""

    def __init__(self, window: DocumentHost) -> None:
        self._win = window

        # ── The document ─────────────────────────────────────────────────────
        # The Markdown itself.  Inside the bundle's temp dir when one is open.
        self.file_path:     Path | None = None
        # Where the build writes its PDF.  Export PDF re-points this.
        self.output_path:   Path | None = None
        # The .pres bundle the writer thinks they are editing, if any.
        self.pres_path:     Path | None = None
        # Where that bundle is unpacked while it is open.
        self.pres_temp_dir: Path | None = None
        # Whether the buffer has moved on from what is on disk.
        self.modified:      bool        = False

        # Set while a Save As chooser is open: what to run once the document
        # actually reaches disk.
        self._save_as_done = None
        # The first build after an open, deferred by one idle cycle.
        self._initial_convert_source: int | None = None

    # ── Where the document is ─────────────────────────────────────────────────

    @property
    def display_path(self) -> Path | None:
        """What the writer thinks they are editing: the bundle, else the file."""
        return self.pres_path or self.file_path

    @property
    def display_name(self) -> str:
        """The document's name for the title bar, or "Untitled"."""
        display = self.display_path
        return display.name if display else UNTITLED

    @property
    def base_dir(self) -> Path | None:
        """The directory relative image sources resolve against."""
        return self.file_path.parent if self.file_path else None

    def shut_down(self) -> None:
        """Release what outlives a closed window: the deferred first build."""
        if self._initial_convert_source is not None:
            GLib.source_remove(self._initial_convert_source)
            self._initial_convert_source = None

    # ── Unsaved changes ───────────────────────────────────────────────────────

    def check_unsaved(self, action) -> None:
        """Run *action*, asking first if the document has unsaved changes."""
        if not self.modified:
            action()
            return
        self.show_unsaved_dialog(on_save=action, on_discard=action)

    def show_unsaved_dialog(self, on_save, on_discard) -> None:
        win = self._win
        dialog = Adw.AlertDialog(
            heading=f'Save changes to "{self.display_name}"?',
            body="Your changes will be lost if you don't save them.",
        )
        dialog.add_response("cancel",  "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save",    "Save")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save",    Adw.ResponseAppearance.SUGGESTED)
        # HIG: default to the safe (least destructive) action (#21)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(_dlg, response):
            if response == "save":
                # Not save() then on_save(): for a document that has never
                # been saved, save() only opens the Save As chooser, so
                # running the continuation here would close the window — or
                # replace the buffer — while the file it promised to write is
                # still an unanswered dialog.  on_save() waits for the write.
                self.save(on_done=on_save)
            elif response == "discard":
                self.modified = False
                on_discard()

        dialog.connect("response", _on_response)
        dialog.present(win)

    # ── Opening ───────────────────────────────────────────────────────────────

    def open_file(self, path: Path) -> None:
        win = self._win
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError) as e:
            win.show_error(f"Cannot open file: {e}")
            return

        is_pres = path.suffix.lower() == ".pres"

        if self._already_open_elsewhere(path, is_pres):
            self._ask_open_anyway(path, is_pres)
            return

        if is_pres:
            self.open_pres_file(path)
        else:
            self.load_into_editor(path)

    def _already_open_elsewhere(self, path: Path, is_pres: bool) -> bool:
        """True when another window of this app already holds *path* (#88)."""
        win = self._win
        for other in win.get_application().get_windows():
            if other is win or not isinstance(other, type(win)):
                continue
            docs = other.documents
            held = docs.pres_path if is_pres else docs.file_path
            if held == path:
                return True
        return False

    def _ask_open_anyway(self, path: Path, is_pres: bool) -> None:
        win = self._win
        dialog = Adw.AlertDialog(
            heading="File already open",
            body=(f"'{path.name}' is already open in another window. "
                  "Opening it again may cause conflicts if both windows save."),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("open",   "Open anyway")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(_dlg, response):
            if response != "open":
                return
            if is_pres:
                self.open_pres_file(path)
            else:
                self.load_into_editor(path)

        dialog.connect("response", _on_response)
        dialog.present(win)

    def load_into_editor(self, path: Path) -> None:
        """Read *path* into the editor and start the first build."""
        win = self._win
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            win.show_error(f"Could not open file: {e}")
            return

        self.file_path   = path
        self.output_path = path.with_suffix(".pdf")
        win.editor.set_base_path(path)
        win.editor.set_text(text)
        win.sidebar.update_from_text(text)
        win.sync_panel_to_document(text)
        # set_text() suppresses the editor's change signals, so drive the
        # live render directly — a freshly opened file starts at slide 1.
        win.current_slide = 0
        win.refresh_live_slide(text)
        win.update_word_count(text)
        win.builds.update_chip()
        self.modified = False
        display = self.pres_path or path        # known: file_path is *path*
        win.set_document_title(display.name)
        save_last_file(display)
        save_recent_file(display)
        win.refresh_recent_actions()

        # Defer the initial conversion by one idle cycle so the window is
        # fully realised before WeasyPrint starts (#25 / #69).
        if self._initial_convert_source is not None:
            GLib.source_remove(self._initial_convert_source)
        self._initial_convert_source = GLib.idle_add(self._deferred_initial_convert)

    def _deferred_initial_convert(self) -> bool:
        self._initial_convert_source = None
        self._win.builds.trigger()
        return GLib.SOURCE_REMOVE

    def restore_autosave(self, text: str) -> None:
        """Put a recovered draft back in the editor, still unsaved."""
        win = self._win
        win.editor.set_text(text)
        win.sync_panel_to_document(text)
        self.modified = True
        win.set_document_title(self.display_name + " •")
        win.refresh_live_slide(text)
        win.builds.update_chip()
        win.builds.trigger()

    # ── Saving ────────────────────────────────────────────────────────────────

    def save(self, on_done=None) -> bool:
        """
        Write the document, and rebuild only if asked to on every save.

        Saving used to always run a full PDF build, which put seconds between
        Ctrl+S and being able to type again.  The strip already shows the
        slide, and the status chip says when the PDF has fallen behind,
        so the build is now something you ask for — from the chip, Ctrl+Return,
        Present, or an export.

        *on_done* runs once the document is on disk, which is **not** always
        before this returns: a document that has never been saved has to ask
        for a filename first.  It never runs if the save is cancelled or
        fails.  The return value only says the save has not already failed;
        anything that must not happen until the file exists belongs in
        *on_done*.
        """
        def _saved() -> None:
            if self._win.auto_convert:
                self._win.builds.trigger()
            if on_done is not None:
                on_done()

        return self.write_document(on_done=_saved)

    def write_document(self, on_done=None) -> bool:
        """
        Write the document to disk.  Never builds.

        *on_done* runs after the write lands — see :meth:`save` for why it
        cannot simply be the next statement at the call site.
        """
        win  = self._win
        path = self.file_path
        if path is None:
            return self.save_as_dialog(on_done=on_done)
        try:
            path.write_text(win.editor.get_text(), encoding="utf-8")
            if self.pres_path:
                self.pack_pres()
            self.modified = False
            display = self.pres_path or path
            win.set_document_title(display.name)
            save_last_file(display)
            # Delete any orphaned recovery file (fixes #59)
            delete_recovery_file(display)
        except OSError as e:
            win.show_error(f"Could not save: {e}")
            return False
        if on_done is not None:
            on_done()
        return True

    def save_as_dialog(self, on_done=None) -> bool:
        win = self._win
        dialog = Gtk.FileDialog()
        dialog.set_title("Save As")
        dialog.set_filters(make_filter_store(
            make_file_filter("Presence bundle", "*.pres"),
            make_file_filter("Markdown files", "*.md"),
        ))
        if self.pres_path:
            dialog.set_initial_file(Gio.File.new_for_path(str(self.pres_path)))
        win.hold_file_dialog(dialog)
        # Held rather than passed, because the answer comes back through a
        # GTK callback.  Whoever is waiting on this save waits here.
        self._save_as_done = on_done
        dialog.save(win, None, self._on_save_as_response)
        return True

    def _on_save_as_response(self, dialog, result) -> None:
        win = self._win
        win.hold_file_dialog(None)
        # Claim the continuation up front: every path out of here either runs
        # it or drops it, and none may leave it behind for the next save.
        on_done, self._save_as_done = self._save_as_done, None
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return                      # cancelled — nothing was written
        path_str = gfile.get_path()
        if not path_str:
            return
        path = Path(path_str)
        if not path.suffix:
            path = path.with_suffix(".pres")
        if not os.access(path.parent, os.W_OK):
            win.show_error(f"Cannot write to '{path.parent}' — permission denied.")
            return

        if path.suffix.lower() == ".pres":
            self.setup_pres_save(path, on_done=on_done)
        else:
            self._save_as_markdown(path, on_done=on_done)

    def _save_as_markdown(self, path: Path, on_done=None) -> None:
        """
        Save a bundle out as a plain .md, taking its assets with it.

        A .pres keeps its images in a temp directory beside the Markdown; a
        loose .md has nowhere to point at once that directory is gone, so the
        assets are copied out before the bundle is let go.
        """
        win = self._win
        if self.pres_path and self.file_path:
            old_assets = self.file_path.parent / "assets"
            if old_assets.is_dir():
                try:
                    shutil.copytree(old_assets, path.parent / "assets",
                                    dirs_exist_ok=True)
                except OSError as e:
                    log.warning("Could not copy assets: %s", e)

        old_pres_temp = self.pres_temp_dir
        self.pres_path     = None
        self.pres_temp_dir = None
        self.file_path     = path
        self.output_path   = path.with_suffix(".pdf")
        win.editor.set_base_path(path)
        self.save(on_done=on_done)
        if old_pres_temp and old_pres_temp.exists():
            shutil.rmtree(old_pres_temp, ignore_errors=True)

    # ── .pres bundles ─────────────────────────────────────────────────────────

    def open_pres_file(self, pres_path: Path) -> None:
        """Extract a .pres ZIP bundle to a temp dir and load slides.md from it."""
        win = self._win
        self.cleanup_pres_temp()
        try:
            # Use shared /tmp in Flatpak so the generated PDF is accessible to
            # external viewers via the OpenURI portal (Flatpak's $TMPDIR is private).
            tmp_base = "/tmp" if os.environ.get("FLATPAK_ID") else None
            tmp_dir = Path(tempfile.mkdtemp(prefix="presence-", dir=tmp_base))
        except OSError as e:
            win.show_error(f"Could not create temp directory: {e}")
            return
        self.pres_temp_dir = tmp_dir
        try:
            with zipfile.ZipFile(pres_path, "r") as zf:
                zf.extractall(tmp_dir)
        except (zipfile.BadZipFile, OSError) as e:
            win.show_error(f"Could not open '{pres_path.name}': {e}")
            self.cleanup_pres_temp()
            return
        md_path = tmp_dir / "slides.md"
        if not md_path.exists():
            win.show_error("Invalid .pres file: 'slides.md' not found inside.")
            self.cleanup_pres_temp()
            return
        self.pres_path = pres_path
        self.load_into_editor(md_path)

    def pack_pres(self) -> None:
        """Re-pack the temp dir into the .pres ZIP bundle atomically."""
        if not self.pres_path or not self.file_path:
            return
        tmp = self.pres_path.with_suffix(".pres~")
        try:
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(self.file_path, "slides.md")
                for name in ("slides.pdf", "slides.html"):
                    p = self.file_path.parent / name
                    if p.exists():
                        zf.write(p, name)
                assets_dir = self.file_path.parent / "assets"
                if assets_dir.is_dir():
                    for asset in sorted(assets_dir.iterdir()):
                        if asset.is_file():
                            zf.write(asset, f"assets/{asset.name}")
            tmp.replace(self.pres_path)
        except OSError as e:
            log.warning("Could not write .pres bundle: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def cleanup_pres_temp(self) -> None:
        """Remove the pres temp dir and clear pres state."""
        self.discard_pres_temp()
        self.pres_path = None

    def discard_pres_temp(self) -> None:
        """Remove the unpacked bundle, keeping the path it came from."""
        if self.pres_temp_dir and self.pres_temp_dir.exists():
            shutil.rmtree(self.pres_temp_dir, ignore_errors=True)
        self.pres_temp_dir = None

    def setup_pres_save(self, pres_path: Path, on_done=None) -> None:
        """Switch to .pres bundle mode, creating a temp dir for the working copy."""
        win = self._win
        old_file_path = self.file_path
        old_pres_temp = self.pres_temp_dir
        try:
            tmp_base = "/tmp" if os.environ.get("FLATPAK_ID") else None
            tmp_dir = Path(tempfile.mkdtemp(prefix="presence-", dir=tmp_base))
        except OSError as e:
            win.show_error(f"Could not create temp directory: {e}")
            return
        if old_file_path:
            old_assets = old_file_path.parent / "assets"
            if old_assets.is_dir():
                try:
                    shutil.copytree(old_assets, tmp_dir / "assets")
                except OSError as e:
                    log.warning("Could not copy assets to bundle: %s", e)
        self.pres_temp_dir = tmp_dir
        self.pres_path     = pres_path
        md_path = tmp_dir / "slides.md"
        self.file_path   = md_path
        self.output_path = tmp_dir / "slides.pdf"
        win.editor.set_base_path(md_path)
        self.save(on_done=on_done)
        if old_pres_temp and old_pres_temp != tmp_dir and old_pres_temp.exists():
            shutil.rmtree(old_pres_temp, ignore_errors=True)

    # ── Autosave ──────────────────────────────────────────────────────────────

    def autosave(self) -> bool:
        """Write a recovery copy of an unsaved document.  Runs on a timer."""
        win = self._win
        if self.modified and win.editor.get_text():
            try:
                rd = recovery_dir()
                rd.mkdir(parents=True, exist_ok=True)
                display = self.display_path
                rp = recovery_path_for(display) if display else rd / "untitled.md"
                rp.write_text(win.editor.get_text(), encoding="utf-8")
                # Brief toast so users know their work is protected (#29)
                win.show_toast("Autosaved", timeout=2)
            except OSError as e:
                log.warning("Autosave failed: %s", e)
        return GLib.SOURCE_CONTINUE
