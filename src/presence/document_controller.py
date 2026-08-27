"""
document_controller.py — The document as a file on disk.

Opening, saving, saving-as, autosaving and the unsaved-changes question, in
one place.  All of it is about the Markdown; none of it is about the build,
which is :mod:`build_coordinator`'s subject.

Two things are worth keeping in view while reading this:

*  **Save never builds.**  Writing the file and producing a PDF are separate
   verbs, and conflating them is what used to put seconds between Ctrl+S and
   being able to type again.  ``save()`` converts afterwards only when the
   writer has asked for that in Settings.
*  **A .pres bundle is a directory pretending to be a file.**  ``_file_path``
   is always the Markdown inside it, ``_pres_path`` the bundle the writer
   thinks they are editing; the title, the recent list and the recovery key
   all follow ``_pres_path`` when there is one.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib

from .app_utils import make_file_filter, make_filter_store
from .session import (save_last_file, save_recent_file, delete_recovery_file,
                      recovery_path_for, recovery_dir)

log = logging.getLogger(__name__)

# The name a document has before it has been saved anywhere.
UNTITLED = "Untitled"


class DocumentController:
    """Owns opening, saving and autosaving on behalf of :class:`MainWindow`."""

    def __init__(self, window) -> None:
        self._win = window
        # Set while a Save As chooser is open: what to run once the document
        # actually reaches disk.
        self._save_as_done = None

    # ── Unsaved changes ───────────────────────────────────────────────────────

    def check_unsaved(self, action) -> None:
        """Run *action*, asking first if the document has unsaved changes."""
        if not self._win._modified:
            action()
            return
        self.show_unsaved_dialog(on_save=action, on_discard=action)

    def show_unsaved_dialog(self, on_save, on_discard) -> None:
        win = self._win
        display = win._pres_path or win._file_path
        name = display.name if display else UNTITLED
        dialog = Adw.AlertDialog(
            heading=f'Save changes to "{name}"?',
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
                win._modified = False
                on_discard()

        dialog.connect("response", _on_response)
        dialog.present(win)

    # ── Opening ───────────────────────────────────────────────────────────────

    def open_file(self, path: Path) -> None:
        win = self._win
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError) as e:
            win._show_error(f"Cannot open file: {e}")
            return

        is_pres = path.suffix.lower() == ".pres"

        if self._already_open_elsewhere(path, is_pres):
            self._ask_open_anyway(path, is_pres)
            return

        if is_pres:
            win._open_pres_file(path)
        else:
            self.load_into_editor(path)

    def _already_open_elsewhere(self, path: Path, is_pres: bool) -> bool:
        """True when another window of this app already holds *path* (#88)."""
        win = self._win
        for other in win.get_application().get_windows():
            if other is win or not isinstance(other, type(win)):
                continue
            held = other._pres_path if is_pres else other._file_path
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
                win._open_pres_file(path)
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
            win._show_error(f"Could not open file: {e}")
            return

        win._file_path   = path
        win._output_path = path.with_suffix(".pdf")
        win._editor.set_base_path(path)
        win._editor.set_text(text)
        win._sidebar.update_from_text(text)
        # set_text() suppresses the editor's change signals, so drive the
        # canvas directly — a freshly opened file starts at slide 1.
        win._current_slide = 0
        win._refresh_canvas(text)
        win._update_word_count(text)
        win._update_build_chip()
        display = win._pres_path or path
        win._set_title(display.name)
        win._modified = False
        save_last_file(display)
        save_recent_file(display)
        win._refresh_recent_actions()

        # Defer the initial conversion by one idle cycle so the window is
        # fully realised before WeasyPrint starts (#25 / #69).
        if win._initial_convert_source is not None:
            GLib.source_remove(win._initial_convert_source)
        win._initial_convert_source = GLib.idle_add(self._deferred_initial_convert)

    def _deferred_initial_convert(self) -> bool:
        win = self._win
        win._initial_convert_source = None
        win._trigger_convert()
        return GLib.SOURCE_REMOVE

    def restore_autosave(self, text: str) -> None:
        """Put a recovered draft back in the editor, still unsaved."""
        win = self._win
        win._editor.set_text(text)
        win._modified = True
        display = win._pres_path or win._file_path
        base = display.name if display else UNTITLED
        win._set_title(base + " •")
        win._refresh_canvas(text)
        win._update_build_chip()
        win._trigger_convert()

    # ── Saving ────────────────────────────────────────────────────────────────

    def save(self, on_done=None) -> bool:
        """
        Write the document, and rebuild only if asked to on every save.

        Saving used to always run a full PDF build, which put seconds between
        Ctrl+S and being able to type again.  The live canvas already shows
        the slide, and the status chip says when the PDF has fallen behind,
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
            if self._win._auto_convert:
                self._win._trigger_convert()
            if on_done is not None:
                on_done()

        return self.write_document(on_done=_saved)

    def write_document(self, on_done=None) -> bool:
        """
        Write the document to disk.  Never builds.

        *on_done* runs after the write lands — see :meth:`save` for why it
        cannot simply be the next statement at the call site.
        """
        win = self._win
        if win._file_path is None:
            return self.save_as_dialog(on_done=on_done)
        try:
            win._file_path.write_text(win._editor.get_text(), encoding="utf-8")
            if win._pres_path:
                win._pack_pres()
            win._modified = False
            display = win._pres_path or win._file_path
            win._set_title(display.name)
            save_last_file(display)
            # Delete any orphaned recovery file (fixes #59)
            delete_recovery_file(display)
        except OSError as e:
            win._show_error(f"Could not save: {e}")
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
        if win._pres_path:
            dialog.set_initial_file(Gio.File.new_for_path(str(win._pres_path)))
        win._active_file_dialog = dialog
        # Held rather than passed, because the answer comes back through a
        # GTK callback.  Whoever is waiting on this save waits here.
        self._save_as_done = on_done
        dialog.save(win, None, self._on_save_as_response)
        return True

    def _on_save_as_response(self, dialog, result) -> None:
        win = self._win
        win._active_file_dialog = None
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
            win._show_error(f"Cannot write to '{path.parent}' — permission denied.")
            return

        if path.suffix.lower() == ".pres":
            win._setup_pres_save(path, on_done=on_done)
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
        if win._pres_path and win._file_path:
            old_assets = win._file_path.parent / "assets"
            if old_assets.is_dir():
                try:
                    shutil.copytree(old_assets, path.parent / "assets",
                                    dirs_exist_ok=True)
                except OSError as e:
                    log.warning("Could not copy assets: %s", e)

        old_pres_temp = win._pres_temp_dir
        win._pres_path = None
        win._pres_temp_dir = None
        win._file_path   = path
        win._output_path = path.with_suffix(".pdf")
        win._editor.set_base_path(path)
        self.save(on_done=on_done)
        if old_pres_temp and old_pres_temp.exists():
            shutil.rmtree(old_pres_temp, ignore_errors=True)

    # ── Autosave ──────────────────────────────────────────────────────────────

    def autosave(self) -> bool:
        """Write a recovery copy of an unsaved document.  Runs on a timer."""
        win = self._win
        if win._modified and win._editor.get_text():
            try:
                rd = recovery_dir()
                rd.mkdir(parents=True, exist_ok=True)
                display = win._pres_path or win._file_path
                rp = recovery_path_for(display) if display else rd / "untitled.md"
                rp.write_text(win._editor.get_text(), encoding="utf-8")
                # Brief toast so users know their work is protected (#29)
                win._show_toast("Autosaved", timeout=2)
            except OSError as e:
                log.warning("Autosave failed: %s", e)
        return GLib.SOURCE_CONTINUE
