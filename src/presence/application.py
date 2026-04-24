"""
application.py — Adw.Application subclass.
"""

import sys
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib

from .window  import MainWindow
from .session import load_last_file, save_last_file, recovery_path_for

APP_ID = "io.gitlab.gtk4_apps1.Presence"


class Application(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.connect("activate", self._on_activate)
        self.connect("open", self._on_open)
        self._setup_actions()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _on_activate(self, app: "Application") -> None:
        win = self._new_window()
        win.present()

        last = load_last_file()
        if last:
            win.open_file(last)
            # Defer the recovery dialog so the window surface is mapped first
            # (fixes #47 — presenting a dialog before the window is realised
            # can produce an unparented dialog on Wayland).
            GLib.idle_add(self._check_recovery, win, last)
        else:
            win.show_open_dialog()

    def _on_open(self, app: "Application", files: list, n_files: int, hint: str) -> None:
        """
        Handle files passed on the command line or via the file manager.

        Reuses the active window if it is a fresh, unmodified, untitled window
        rather than always opening a new one (fixes #77 — avoids blank+file
        double-window on first launch).
        """
        existing = self.get_active_window()
        for i, gfile in enumerate(files):
            path = Path(gfile.get_path())
            if (i == 0
                    and isinstance(existing, MainWindow)
                    and not existing._modified
                    and existing._file_path is None):
                win = existing
            else:
                win = self._new_window()
            win.open_file(path)
            win.present()
            save_last_file(path)

    def _new_window(self) -> "MainWindow":
        return MainWindow(application=self)

    def _check_recovery(self, win: "MainWindow", original: Path) -> bool:
        recovery = recovery_path_for(original)
        if not recovery.exists():
            return GLib.SOURCE_REMOVE
        try:
            recovery_mtime = recovery.stat().st_mtime
            original_mtime = original.stat().st_mtime
        except OSError:
            return GLib.SOURCE_REMOVE

        if recovery_mtime <= original_mtime:
            return GLib.SOURCE_REMOVE

        self._offer_recovery(win, original, recovery)
        return GLib.SOURCE_REMOVE

    def _offer_recovery(self, win: "MainWindow", original: Path, recovery: Path) -> None:
        dialog = Adw.AlertDialog(
            heading="Restore autosaved version?",
            body=(
                f"An autosave of '{original.name}' is newer than the saved file. "
                "Restore it to avoid losing work?"
            ),
        )
        dialog.add_response("cancel",  "Keep saved version")
        dialog.add_response("restore", "Restore autosave")
        dialog.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(dlg, response):
            if response == "restore":
                try:
                    text = recovery.read_text(encoding="utf-8")
                    win.restore_autosave(text)
                except OSError:
                    pass

        dialog.connect("response", _on_response)
        dialog.present(win)

    # ── Application-wide actions ──────────────────────────────────────────────

    def _setup_actions(self) -> None:
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<primary>q"])

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

        # Standard GNOME app.preferences action (fixes #68)
        prefs_action = Gio.SimpleAction.new("preferences", None)
        prefs_action.connect("activate", self._on_preferences)
        self.add_action(prefs_action)

    def _on_preferences(self, *_) -> None:
        win = self.get_active_window()
        if isinstance(win, MainWindow):
            win._on_settings()

    def _on_about(self, *_) -> None:
        dialog = Adw.AboutDialog(
            application_name="Presence",
            application_icon=APP_ID,
            developer_name="Rory",
            version="1.0.0",
            comments="Convert Markdown files to PDF slideshows.",
        )
        # GTK_LICENSE_UNKNOWN (0) — replace with e.g. Adw.AboutDialog.GTK_LICENSE_GPL_3_0
        # once the project licence is decided.
        dialog.set_license_type(0)
        dialog.present(self.get_active_window())


def main() -> int:
    app = Application()
    return app.run(sys.argv)
