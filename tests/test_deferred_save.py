"""
test_deferred_save.py — A save that has to ask for a filename first.

Saving a document that has never been saved is not one statement: it opens a
Save As chooser and finishes later, through a GTK callback.  Anything waiting
on that save — closing the window, opening another file — has to wait for the
write, not for the chooser to open.  Running it alongside instead destroyed
the window while the draft was still an unanswered dialog, or replaced the
buffer so the chooser wrote the wrong document under the chosen name.

No widgets here: GTK 4 has no offscreen backend, so constructing one segfaults
(see conftest.py).  Gtk.FileDialog is not a widget, but presenting one needs a
real parent window, so it is stood in for — which also lets a test answer the
chooser, and cancel it, on demand.
"""

import ast
import inspect
import textwrap

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib          # noqa: E402

import presence.document_controller as dc    # noqa: E402
from presence.document_controller import DocumentController  # noqa: E402

from test_document_controller import FakeWindow              # noqa: E402


@pytest.fixture(autouse=True)
def tmp_session(monkeypatch, tmp_path):
    """Redirect session I/O (last file, recents, recovery) to a tmp dir."""
    import presence.session as s
    monkeypatch.setattr(s, "_config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(s, "_data_dir",   lambda: tmp_path / "data")
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()


class FakeFileDialog:
    """A Save As chooser that answers when a test tells it to, not before."""

    opened: list = []

    def __init__(self) -> None:
        FakeFileDialog.opened.append(self)
        self._callback = None
        self._chosen = None

    # Gtk.FileDialog's setters, all uninteresting here.
    def set_title(self, title) -> None:         pass
    def set_filters(self, filters) -> None:     pass
    def set_initial_file(self, gfile) -> None:  pass

    def save(self, parent, cancellable, callback) -> None:
        self._callback = callback

    def save_finish(self, result):
        if self._chosen is None:
            raise GLib.Error("cancelled")
        return Gio.File.new_for_path(str(self._chosen))

    # ── What a test does to it ────────────────────────────────────────────────

    def answer(self, path) -> None:
        """The writer picked *path* and confirmed."""
        self._chosen = path
        self._callback(self, object())

    def cancel(self) -> None:
        """The writer dismissed the chooser."""
        self._chosen = None
        self._callback(self, object())


@pytest.fixture(autouse=True)
def fake_chooser(monkeypatch):
    FakeFileDialog.opened = []
    monkeypatch.setattr(dc.Gtk, "FileDialog", FakeFileDialog)
    return FakeFileDialog


def controller(text: str = "") -> tuple:
    """A controller over a never-saved document holding *text*."""
    from test_document_controller import controller as _make
    doc, win = _make(text)
    doc.modified = True
    return doc, win


def chooser() -> FakeFileDialog:
    assert FakeFileDialog.opened, "no Save As chooser was opened"
    return FakeFileDialog.opened[-1]


# ── The wait ──────────────────────────────────────────────────────────────────

def test_a_never_saved_document_opens_a_chooser_rather_than_writing():
    doc, win = controller("draft")

    doc.save()

    assert len(FakeFileDialog.opened) == 1
    assert doc.file_path is None


def test_nothing_waiting_on_the_save_runs_while_the_chooser_is_open():
    doc, win = controller("draft")
    closed = []

    doc.save(on_done=lambda: closed.append(True))

    assert closed == []          # the window must still be here to save from


def test_what_waits_on_the_save_runs_once_the_file_lands(tmp_path):
    doc, win = controller("draft")
    closed = []

    doc.save(on_done=lambda: closed.append(True))
    chooser().answer(tmp_path / "talk.md")

    assert (tmp_path / "talk.md").read_text() == "draft"
    assert closed == [True]


def test_the_draft_is_written_before_the_thing_that_was_waiting_runs(tmp_path):
    """Ordering is the whole point: closing must not outrun the write."""
    dest = tmp_path / "talk.md"
    doc, win = controller("draft")
    seen = []

    doc.save(on_done=lambda: seen.append(dest.exists()))
    chooser().answer(dest)

    assert seen == [True]


# ── Cancelling ────────────────────────────────────────────────────────────────

def test_cancelling_the_chooser_never_runs_what_was_waiting():
    doc, win = controller("draft")
    closed = []

    doc.save(on_done=lambda: closed.append(True))
    chooser().cancel()

    assert closed == []
    assert doc.modified is True


def test_an_unwritable_folder_stops_the_save_and_what_waited_on_it(tmp_path):
    doc, win = controller("draft")
    closed = []

    doc.save(on_done=lambda: closed.append(True))
    chooser().answer(tmp_path / "no-such-folder" / "talk.md")

    assert closed == []
    assert win.errors and "permission denied" in win.errors[0]


def test_a_cancelled_save_leaves_nothing_behind_for_the_next_one(tmp_path):
    """A dropped continuation must not fire on somebody else's save."""
    doc, win = controller("draft")
    closed = []

    doc.save(on_done=lambda: closed.append(True))
    chooser().cancel()

    doc.save()                              # no continuation this time
    chooser().answer(tmp_path / "talk.md")

    assert closed == []


# ── Bundles ───────────────────────────────────────────────────────────────────

def test_saving_as_a_bundle_also_waits_for_the_write(tmp_path):
    doc, win = controller("draft")
    closed = []

    doc.save(on_done=lambda: closed.append(True))
    chooser().answer(tmp_path / "talk.pres")

    assert doc.pres_saves == [tmp_path / "talk.pres"]
    assert closed == [True]


def test_a_name_without_a_suffix_becomes_a_bundle(tmp_path):
    doc, win = controller("draft")

    doc.save()
    chooser().answer(tmp_path / "talk")

    assert doc.pres_saves == [tmp_path / "talk.pres"]


# ── The document that has a path already ──────────────────────────────────────

def test_a_saved_document_still_finishes_before_it_returns(tmp_path):
    """The common case must not have grown a wait it does not need."""
    dest = tmp_path / "talk.md"
    dest.write_text("old")
    doc, win = controller("new")
    doc.file_path = dest
    closed = []

    assert doc.save(on_done=lambda: closed.append(True)) is True
    assert dest.read_text() == "new"
    assert closed == [True]
    assert FakeFileDialog.opened == []


def test_a_failed_write_does_not_run_what_waited_on_it(tmp_path):
    doc, win = controller("new")
    doc.file_path = tmp_path / "no-such-folder" / "talk.md"
    closed = []

    assert doc.save(on_done=lambda: closed.append(True)) is False
    assert closed == []


# ── The call site the bug lived at ────────────────────────────────────────────

def test_the_unsaved_question_hands_its_continuation_to_the_save():
    """
    Adw.AlertDialog is a widget, so the wiring is read rather than run.

    The bug was two sibling statements — ``self.save()`` then ``on_save()`` —
    which is exactly what a reader would write again, so the shape is worth
    pinning down.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(
        DocumentController.show_unsaved_dialog)))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)]

    saves = [c for c in calls
             if isinstance(c.func, ast.Attribute) and c.func.attr == "save"]
    assert saves, "the save branch no longer calls save()"
    assert all(any(kw.arg == "on_done" for kw in c.keywords) for c in saves), \
        "save() must be given the continuation, not followed by it"

    bare = [c for c in calls
            if isinstance(c.func, ast.Name) and c.func.id == "on_save"]
    assert not bare, "on_save() must not be called alongside the save"
