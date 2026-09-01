"""
test_application.py — app lifecycle: actions, file opening, crash recovery.

Nothing here presents a window.  ``_on_activate`` is the one entry point that
must, so it is left to the app itself; what it delegates to — the recovery
check, the open-files handler, the action table — is all reachable without it.

The recovery path is the reason this file is worth having.  It decides whether
to offer a writer back work they did not save, on a comparison of two
timestamps, and it is the only code in the app that can silently discard an
autosave by getting that comparison the wrong way round.
"""
import os
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio  # noqa: E402

import presence.application as app_mod  # noqa: E402
from presence.application import APP_ID, Application  # noqa: E402
from presence.window import MainWindow  # noqa: E402


class FakeWindow:
    """Only what the recovery flow touches."""

    def __init__(self):
        self.restored = []

    def restore_autosave(self, text):
        self.restored.append(text)


@pytest.fixture
def bare_app(gtk):
    """An Application with no registration — enough to call its methods on."""
    return Application.__new__(Application)


@pytest.fixture
def alert_dialogs(monkeypatch):
    """Catch every Adw.AlertDialog instead of putting it on screen."""
    shown = []
    monkeypatch.setattr(Adw.AlertDialog, "present",
                        lambda self, parent=None: shown.append(self))
    return shown


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    """
    A document and its autosave, with the mtimes a test asks for.

    Returns (app, original, recovery_file, offers) where *offers* records the
    calls _check_recovery decides to make.
    """
    original = tmp_path / "talk.md"
    original.write_text("# Saved\n")
    recovery_file = tmp_path / "talk.md.autosave"

    monkeypatch.setattr(app_mod, "recovery_path_for", lambda p: recovery_file)

    app = Application.__new__(Application)     # see the bare_app fixture
    offers = []
    app._offer_recovery = lambda win, o, r: offers.append((o, r))
    return app, original, recovery_file, offers


def _age(path: Path, seconds: float) -> None:
    """Set *path*'s mtime to a fixed point, so comparisons are not racy."""
    os.utime(path, (seconds, seconds))


# ── Actions ───────────────────────────────────────────────────────────────────

def test_the_app_registers_the_actions_gnome_expects(app_factory):
    app = app_factory()
    assert sorted(app.list_actions()) == ["about", "preferences", "quit"]


def test_quit_has_the_standard_accelerator(app_factory):
    assert app_factory().get_accels_for_action("app.quit") == ["<Control>q"]


def test_the_app_handles_files_opened_from_outside(gtk):
    app = Application()
    assert app.get_flags() & Gio.ApplicationFlags.HANDLES_OPEN
    assert app.get_application_id() == APP_ID


def test_preferences_opens_the_settings_of_the_active_window(app_factory,
                                                              monkeypatch):
    opened = []
    monkeypatch.setattr(MainWindow, "_on_settings",
                        lambda self, *a: opened.append(self))
    app = app_factory()
    win = MainWindow(application=app)

    app._on_preferences()
    assert opened == [win]


def test_preferences_with_no_window_does_nothing(app_factory, monkeypatch):
    opened = []
    monkeypatch.setattr(MainWindow, "_on_settings",
                        lambda self, *a: opened.append(self))
    app_factory()._on_preferences()
    assert opened == []


def test_about_names_the_app(gtk, monkeypatch):
    shown = []
    monkeypatch.setattr(Adw.AboutDialog, "present",
                        lambda self, parent=None: shown.append(self))
    Application()._on_about()
    assert len(shown) == 1
    assert shown[0].get_application_name() == "Presence"
    assert shown[0].get_application_icon() == APP_ID


# ── Crash recovery ────────────────────────────────────────────────────────────

def test_no_autosave_means_nothing_to_offer(recovery):
    app, original, recovery_file, offers = recovery
    assert not recovery_file.exists()
    app._check_recovery(FakeWindow(), original)
    assert offers == []


def test_an_autosave_older_than_the_file_is_not_offered(recovery):
    # The writer saved after the autosave was taken; the autosave is stale
    # and offering it would be offering to undo the save.
    app, original, recovery_file, offers = recovery
    recovery_file.write_text("# Older\n")
    _age(recovery_file, 1000)
    _age(original, 2000)

    app._check_recovery(FakeWindow(), original)
    assert offers == []


def test_an_autosave_newer_than_the_file_is_offered(recovery):
    app, original, recovery_file, offers = recovery
    recovery_file.write_text("# Newer, unsaved\n")
    _age(original, 1000)
    _age(recovery_file, 2000)

    app._check_recovery(FakeWindow(), original)
    assert offers == [(original, recovery_file)]


def test_an_autosave_the_same_age_is_not_offered(recovery):
    app, original, recovery_file, offers = recovery
    recovery_file.write_text("# Same\n")
    _age(original, 1500)
    _age(recovery_file, 1500)

    app._check_recovery(FakeWindow(), original)
    assert offers == []


def test_a_document_that_has_gone_away_is_not_recovered_over(recovery):
    app, original, recovery_file, offers = recovery
    recovery_file.write_text("# Orphan\n")
    original.unlink()

    app._check_recovery(FakeWindow(), original)
    assert offers == []


# ── Offering it ───────────────────────────────────────────────────────────────

def test_the_offer_names_the_document_and_defaults_to_keeping_the_save(
        bare_app, tmp_path, alert_dialogs):
    win = FakeWindow()
    original = tmp_path / "talk.md"
    rec      = tmp_path / "talk.md.autosave"
    rec.write_text("# Unsaved work\n")

    bare_app._offer_recovery(win, original, rec)

    assert len(alert_dialogs) == 1
    dialog = alert_dialogs[0]
    assert dialog.get_heading() == "Restore autosaved version?"
    assert "talk.md" in dialog.get_body()
    # Destructive by default is the wrong default: keeping the save is safe.
    assert dialog.get_default_response() == "cancel"


def test_restoring_puts_the_autosaved_text_into_the_window(bare_app, tmp_path,
                                                           alert_dialogs):
    win = FakeWindow()
    original = tmp_path / "talk.md"
    rec      = tmp_path / "talk.md.autosave"
    rec.write_text("# Unsaved work\n")

    bare_app._offer_recovery(win, original, rec)
    alert_dialogs[0].emit("response", "restore")
    assert win.restored == ["# Unsaved work\n"]


def test_keeping_the_saved_version_restores_nothing(bare_app, tmp_path,
                                                    alert_dialogs):
    win = FakeWindow()
    original = tmp_path / "talk.md"
    rec      = tmp_path / "talk.md.autosave"
    rec.write_text("# Unsaved work\n")

    bare_app._offer_recovery(win, original, rec)
    alert_dialogs[0].emit("response", "cancel")
    assert win.restored == []


def test_an_autosave_that_cannot_be_read_is_not_forced_on_the_writer(
        bare_app, tmp_path, alert_dialogs):
    win = FakeWindow()
    original = tmp_path / "talk.md"
    rec      = tmp_path / "gone.autosave"          # never written

    bare_app._offer_recovery(win, original, rec)
    alert_dialogs[0].emit("response", "restore")
    assert win.restored == []


# ── Opening files from outside the app ────────────────────────────────────────

@pytest.fixture
def opening(app_factory, monkeypatch):
    """An app whose windows record what they were asked to open."""
    opened = []
    monkeypatch.setattr(MainWindow, "open_file",
                        lambda self, path: opened.append((self, path)))
    monkeypatch.setattr(MainWindow, "present", lambda self: None)
    monkeypatch.setattr(app_mod, "save_last_file", lambda p: None)
    return app_factory(), opened


def _gfiles(*paths):
    return [Gio.File.new_for_path(str(p)) for p in paths]


def test_one_file_reuses_a_blank_untitled_window(opening, tmp_path):
    # Otherwise first launch shows a blank window and the file's window
    # beside it.
    app, opened = opening
    existing = MainWindow(application=app)
    doc = tmp_path / "talk.md"

    app._on_open(app, _gfiles(doc), 1, "")

    assert opened == [(existing, doc)]
    assert len(app.get_windows()) == 1


def test_a_window_with_unsaved_work_is_not_reused(opening, tmp_path):
    app, opened = opening
    existing = MainWindow(application=app)
    existing.documents.modified = True
    doc = tmp_path / "talk.md"

    app._on_open(app, _gfiles(doc), 1, "")

    assert opened[0][0] is not existing
    assert len(app.get_windows()) == 2


def test_a_window_that_already_holds_a_document_is_not_reused(opening, tmp_path):
    app, opened = opening
    existing = MainWindow(application=app)
    existing.documents.file_path = tmp_path / "other.md"
    doc = tmp_path / "talk.md"

    app._on_open(app, _gfiles(doc), 1, "")

    assert opened[0][0] is not existing
    assert len(app.get_windows()) == 2


def test_each_further_file_gets_a_window_of_its_own(opening, tmp_path):
    app, opened = opening
    MainWindow(application=app)
    one, two, three = (tmp_path / f"{n}.md" for n in ("one", "two", "three"))

    app._on_open(app, _gfiles(one, two, three), 3, "")

    assert [p for _, p in opened] == [one, two, three]
    assert len({w for w, _ in opened}) == 3


def test_the_last_file_opened_is_remembered(app_factory, monkeypatch, tmp_path):
    remembered = []
    monkeypatch.setattr(MainWindow, "open_file", lambda self, path: None)
    monkeypatch.setattr(MainWindow, "present", lambda self: None)
    monkeypatch.setattr(app_mod, "save_last_file", remembered.append)

    app = app_factory()
    MainWindow(application=app)
    one, two = tmp_path / "one.md", tmp_path / "two.md"

    app._on_open(app, _gfiles(one, two), 2, "")
    assert remembered == [one, two]
