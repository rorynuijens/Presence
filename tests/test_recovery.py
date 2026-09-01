"""
test_recovery.py — Autosave is a promise; this is what keeps it.

Three things were wrong with recovery, and all three are about a file being
written that nothing would ever read back:

*  **A draft that was never named had nowhere to be found.**
   ``recovery_path_for()`` keys on the document's path, and a document that
   has never been saved has none, so every unsaved draft in every window
   autosaved to one shared ``untitled.md``.  Nothing looked for that name —
   ``Application._check_recovery`` only ever asks about ``load_last_file()``
   — and the next unsaved draft overwrote it.  The writer was toasted
   "Autosaved" every thirty seconds over a file that was unreachable and
   about to be destroyed.
*  **Only one document was ever checked.**  A file opened from the file
   manager, the command line, Open… or the recent list got no check at all.
*  **The directory was never swept.**  ``delete_recovery_file()`` unlinks
   only the name the current hashing scheme produces, so drafts written
   under the older scheme, or belonging to a document since renamed or
   deleted, stayed on disk for good.

``application.py`` is mode 444 and owns the one door recovery came through,
so the missing halves live in ``DocumentController`` and ``MainWindow``.
``tests/test_session.py`` is mode 444 too, which is why the session-level
checks are here rather than beside their neighbours.
"""

import os
import time

import pytest

from presence.document_controller import DocumentController
from presence.session import (delete_untitled_recovery, is_untitled_recovery,
                              list_untitled_recoveries, prune_recovery_files,
                              recovery_dir, recovery_path_for,
                              untitled_recovery_path)


def _write(path, text: str, age_days: float = 0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age_days:
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))
    return path


# ── The draft's own identity ──────────────────────────────────────────────────

def test_two_drafts_do_not_share_a_recovery_file():
    """The collision that lost work: one file for every unsaved document."""
    assert untitled_recovery_path("aaaa") != untitled_recovery_path("bbbb")
    assert untitled_recovery_path("aaaa").parent == recovery_dir()


def test_the_old_single_slot_name_is_still_recognised():
    """A draft stranded by the previous scheme is still a draft."""
    assert is_untitled_recovery(recovery_dir() / "untitled.md")
    assert is_untitled_recovery(recovery_dir() / "untitled-9f2c.md")
    assert not is_untitled_recovery(recovery_dir() / "talk_0123456789abcdef.md")


def test_listing_drafts_finds_both_schemes_newest_first():
    old = _write(untitled_recovery_path("old"), "older draft", age_days=2)
    legacy = _write(recovery_dir() / "untitled.md", "stranded draft", age_days=1)
    new = _write(untitled_recovery_path("new"), "newest draft")

    assert list_untitled_recoveries() == [new, legacy, old]


def test_listing_drafts_skips_empty_files_and_named_documents():
    _write(untitled_recovery_path("blank"), "")
    _write(recovery_path_for(recovery_dir() / "talk.md"), "a named document")
    keep = _write(untitled_recovery_path("real"), "words")

    assert list_untitled_recoveries() == [keep]


def test_deleting_a_draft_is_by_token():
    path = _write(untitled_recovery_path("tok"), "words")

    delete_untitled_recovery("tok")

    assert not path.exists()


# ── The sweep ─────────────────────────────────────────────────────────────────

def test_the_sweep_drops_what_nothing_has_touched():
    stale  = _write(recovery_dir() / "pitch.md", "a 2019 orphan", age_days=90)
    recent = _write(untitled_recovery_path("live"), "still being typed")

    removed = prune_recovery_files(max_age_days=30)

    assert removed == [stale]
    assert not stale.exists()
    assert recent.exists()


def test_the_sweep_survives_a_directory_that_is_not_there():
    assert prune_recovery_files() == []


# ── The controller ────────────────────────────────────────────────────────────

class FakeEditor:
    def __init__(self, text: str = "") -> None:
        self._text = text
        self.base_path = None

    def get_text(self):            return self._text
    def set_text(self, text):      self._text = text
    def set_base_path(self, path): self.base_path = path


class FakeClock:
    def __init__(self):
        self.current_slide = 0
    def document_replaced(self, text, slide=None): pass
    def set_speaking_rate(self, wpm): pass


class FakeBuilds:
    def __init__(self):  self.converts = 0
    def trigger(self, *_): self.converts += 1
    def update_chip(self): pass


class FakeApplication:
    def get_windows(self):  return []


class FakeWindow:
    """What DocumentController asks a window for, plus a window factory.

    ``new_window()`` is the one name the seam gained: a restored draft is a
    second document, and a window already holding one must not lose it.
    """

    def __init__(self, text: str = "") -> None:
        self.editor  = FakeEditor(text)
        self.builds  = FakeBuilds()
        self.clock   = FakeClock()
        self.auto_convert = False
        self.title   = None
        self.errors  = []
        self.toasts  = []
        self.made    = []

    def show_error(self, message):   self.errors.append(message)
    def show_toast(self, message, timeout=None): self.toasts.append(message)
    def set_document_title(self, title): self.title = title
    def refresh_recent_actions(self): pass
    def hold_file_dialog(self, dialog): pass
    # Enough for _already_open_elsewhere(), which asks the app what other
    # windows are holding before it opens a second copy of a file.
    def get_application(self):       return FakeApplication()

    def new_window(self):
        win = FakeWindow()
        win.documents = DocumentController(win)
        self.made.append(win)
        return win


def controller(text: str = ""):
    win = FakeWindow(text)
    doc = DocumentController(win)
    win.documents = doc
    return doc, win


def test_two_windows_autosave_to_two_files():
    """The bug, at the level it bit: both drafts survive one timer tick."""
    first,  win_a = controller("the talk I am writing")
    second, win_b = controller("a different talk entirely")
    first.modified = second.modified = True

    first.autosave()
    second.autosave()

    assert (untitled_recovery_path(first._untitled_token).read_text()
            == "the talk I am writing")
    assert (untitled_recovery_path(second._untitled_token).read_text()
            == "a different talk entirely")


def test_saving_a_draft_retires_its_recovery_file(tmp_path):
    doc, win = controller("words")
    doc.modified = True
    doc.autosave()
    draft = untitled_recovery_path(doc._untitled_token)
    assert draft.exists()

    doc.file_path = tmp_path / "talk.md"
    doc.write_document()

    assert not draft.exists(), "a saved document is not a lost draft"


def test_opening_a_document_retires_the_draft_it_replaces(tmp_path):
    doc, win = controller("words")
    doc.modified = True
    doc.autosave()
    draft = untitled_recovery_path(doc._untitled_token)

    src = tmp_path / "talk.md"
    src.write_text("# Something else\n")
    doc.load_into_editor(src)

    assert not draft.exists()


def test_a_window_does_not_offer_itself_its_own_draft():
    doc, win = controller("words")
    doc.modified = True
    doc.autosave()

    assert doc.pending_untitled_draft() is None


def test_the_newest_abandoned_draft_is_the_one_offered():
    _write(untitled_recovery_path("older"), "older", age_days=3)
    newest = _write(untitled_recovery_path("newer"), "newer")
    doc, win = controller()

    assert doc.pending_untitled_draft() == newest


def test_adopting_a_draft_restores_it_and_takes_over_the_file():
    draft = _write(untitled_recovery_path("stranded"), "the lost words")
    doc, win = controller()

    doc.adopt_draft("the lost words", draft)

    assert win.editor.get_text() == "the lost words"
    assert doc.modified is True, "a restored draft is still unsaved"
    assert not draft.exists(), "the old file is not left to be offered twice"
    assert (untitled_recovery_path(doc._untitled_token).read_text()
            == "the lost words")


def test_a_draft_never_replaces_a_document_the_window_is_holding(tmp_path):
    src = tmp_path / "talk.md"
    src.write_text("# The document I actually opened\n")
    doc, win = controller()
    doc.load_into_editor(src)
    draft = _write(untitled_recovery_path("stranded"), "the lost words")

    doc._restore_draft(draft)

    assert win.editor.get_text() == "# The document I actually opened\n"
    assert len(win.made) == 1, "the draft gets a window of its own"
    assert win.made[0].editor.get_text() == "the lost words"


def test_an_empty_window_takes_the_draft_itself():
    draft = _write(untitled_recovery_path("stranded"), "the lost words")
    doc, win = controller()

    doc._restore_draft(draft)

    assert win.editor.get_text() == "the lost words"
    assert win.made == []


# ── The check application.py never makes ──────────────────────────────────────

@pytest.fixture
def dialogs(monkeypatch):
    """Capture the alert dialogs the controller builds, without a display."""
    made = []

    class FakeDialog:
        def __init__(self, heading="", body=""):
            self.heading   = heading
            self.body      = body
            self.responses = []
            self.handler   = None
            self.presented = None
            made.append(self)

        def add_response(self, key, label):  self.responses.append(key)
        def set_response_appearance(self, *a): pass
        def set_default_response(self, key): self.default = key
        def set_close_response(self, key):   self.close = key
        def connect(self, _signal, handler): self.handler = handler
        def present(self, parent):           self.presented = parent

        def answer(self, response):
            self.handler(self, response)

    class FakeAdw:
        AlertDialog = FakeDialog
        class ResponseAppearance:
            SUGGESTED = DESTRUCTIVE = None

    monkeypatch.setattr("presence.document_controller.Adw", FakeAdw)
    return made


def test_a_newer_autosave_of_an_opened_document_is_offered(tmp_path, dialogs):
    src = tmp_path / "talk.md"
    src.write_text("what is on disk")
    doc, win = controller()
    doc.load_into_editor(src)
    recovery = _write(recovery_path_for(src), "what was really typed")
    os.utime(recovery, (time.time() + 10, time.time() + 10))

    doc._check_recovery_after_open(src)

    assert len(dialogs) == 1
    dialogs[0].answer("restore")
    assert win.editor.get_text() == "what was really typed"


def test_an_older_autosave_is_not_offered(tmp_path, dialogs):
    src = tmp_path / "talk.md"
    _write(recovery_path_for(src), "stale", age_days=1)
    src.write_text("what is on disk")
    doc, win = controller()
    doc.load_into_editor(src)

    doc._check_recovery_after_open(src)

    assert dialogs == []


def test_nothing_is_offered_when_the_open_did_not_land(tmp_path, dialogs):
    src = tmp_path / "talk.md"
    src.write_text("on disk")
    recovery = _write(recovery_path_for(src), "newer")
    os.utime(recovery, (time.time() + 10, time.time() + 10))
    doc, win = controller()          # never opened anything

    doc._check_recovery_after_open(src)

    assert dialogs == []


def test_the_document_application_py_checks_is_not_asked_about_twice(
        tmp_path, monkeypatch, dialogs):
    """
    ``Application._on_activate`` opens ``load_last_file()`` and then checks it
    itself.  Checking here as well would put two identical dialogs on screen.
    """
    import presence.document_controller as dc
    scheduled = []
    monkeypatch.setattr(dc.GLib, "idle_add",
                        lambda fn, *args: scheduled.append((fn, args)))

    src = tmp_path / "talk.md"
    src.write_text("# Talk\n")
    doc, win = controller()
    monkeypatch.setattr(dc, "load_last_file", lambda: src)

    doc.open_file(src)

    # ``==`` and not ``is``: two bound methods of the same function on the
    # same instance are equal but never identical, so ``is`` here would pass
    # no matter what open_file() scheduled.
    assert not any(fn == doc._check_recovery_after_open for fn, _ in scheduled)


def test_a_document_application_py_ignores_is_checked_here(
        tmp_path, monkeypatch, dialogs):
    """A file from the file manager, Open… or the recent list."""
    import presence.document_controller as dc
    scheduled = []
    monkeypatch.setattr(dc.GLib, "idle_add",
                        lambda fn, *args: scheduled.append((fn, args)))

    src = tmp_path / "talk.md"
    src.write_text("# Talk\n")
    doc, win = controller()
    monkeypatch.setattr(dc, "load_last_file", lambda: tmp_path / "something-else.md")

    doc.open_file(src)

    assert (doc._check_recovery_after_open, (src.resolve(),)) in scheduled


# ── The three answers ─────────────────────────────────────────────────────────

def test_a_draft_can_be_kept_restored_or_discarded(dialogs):
    draft = _write(untitled_recovery_path("stranded"), "the lost words")
    doc, win = controller()

    doc.ask_restore_draft(draft)

    assert dialogs[0].responses == ["later", "discard", "restore"]
    assert dialogs[0].close == "later"


def test_not_now_leaves_the_draft_for_the_next_launch(dialogs):
    draft = _write(untitled_recovery_path("stranded"), "the lost words")
    doc, win = controller()
    settled = []

    doc.ask_restore_draft(draft, on_settled=lambda: settled.append(True))
    dialogs[0].answer("later")

    assert draft.exists()
    assert settled == [True], "the caller does what it was going to do instead"


def test_discarding_a_draft_removes_it(dialogs):
    draft = _write(untitled_recovery_path("stranded"), "the lost words")
    doc, win = controller()

    doc.ask_restore_draft(draft)
    dialogs[0].answer("discard")

    assert not draft.exists()


def test_restoring_from_the_dialog_puts_the_words_back(dialogs):
    draft = _write(untitled_recovery_path("stranded"), "the lost words")
    doc, win = controller()
    settled = []

    doc.ask_restore_draft(draft, on_settled=lambda: settled.append(True))
    dialogs[0].answer("restore")

    assert win.editor.get_text() == "the lost words"
    assert settled == [], "restoring is not 'do the other thing instead'"
