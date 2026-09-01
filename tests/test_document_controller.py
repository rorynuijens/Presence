"""
test_document_controller.py — Opening, saving and autosaving the Markdown.

GTK 4 dropped the offscreen backend, so constructing a widget under this
suite's conftest segfaults (see conftest.py).  DocumentController reaches the
window only through a handful of named attributes, so these drive the real
controller against a stand-in that has exactly those — which also keeps the
coupling visible: if the controller starts needing something new, FakeWindow
has to say so.
"""

import pytest

from presence.document_controller import DocumentController, UNTITLED


@pytest.fixture(autouse=True)
def tmp_session(monkeypatch, tmp_path):
    """Redirect session I/O (last file, recents, recovery) to a tmp dir."""
    import presence.session as s
    monkeypatch.setattr(s, "_config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(s, "_data_dir",   lambda: tmp_path / "data")
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()


class FakeEditor:
    def __init__(self, text: str = "") -> None:
        self._text = text
        self.base_path = None

    def get_text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text

    def set_base_path(self, path) -> None:
        self.base_path = path


class FakeSidebar:
    def __init__(self) -> None:
        self.texts = []
        self.wpm = None

    def update_from_text(self, text) -> None:
        self.texts.append(text)

    def set_speaking_rate(self, wpm) -> None:
        self.wpm = wpm


class FakeBuilds:
    """The build coordinator, as far as the document controller can see it."""

    def __init__(self) -> None:
        self.converts = 0

    def trigger(self, *_):  self.converts += 1
    def update_chip(self):  pass


class FakeWindow:
    """
    Everything DocumentController still asks a window for.

    Where the document is used to live here — file_path, pres_path,
    pres_temp_dir, output_path, modified — and the controller reached in and
    wrote all five.  They are the controller's own now, so the tests below
    set and read them on ``doc`` rather than on the window.
    """

    def __init__(self, text: str = "") -> None:
        self.editor  = FakeEditor(text)
        self.sidebar = FakeSidebar()
        self.builds  = FakeBuilds()
        self.auto_convert = False
        self.current_slide = 7          # so we can see it reset to 0
        self.title = None
        self.errors = []
        self.toasts = []
        self.dialogs = []
        self.panel_syncs = []

    # collaborators the controller calls back into
    def show_error(self, message):  self.errors.append(message)
    def show_toast(self, message, timeout=None): self.toasts.append(message)
    def set_document_title(self, title):     self.title = title
    def refresh_live_slide(self, text=None): pass
    def sync_panel_to_document(self, text): self.panel_syncs.append(text)
    def update_word_count(self, text):   pass
    def refresh_recent_actions(self):    pass
    def hold_file_dialog(self, dialog):  self.dialogs.append(dialog)
    def get_application(self):       return None

    @property
    def converts(self) -> int:
        return self.builds.converts


def controller(text: str = "") -> tuple:
    win = FakeWindow(text)
    doc = DocumentController(win)
    # Packing a bundle and switching to one both touch the filesystem; these
    # tests are about what the controller decides, not about zipfile.
    doc.packed = 0
    doc.pres_saves = []
    doc.pack_pres = lambda: setattr(doc, "packed", doc.packed + 1)

    def _setup(pres_path, on_done=None):
        doc.pres_saves.append(pres_path)
        doc.pres_path = pres_path
        if on_done is not None:
            on_done()
    doc.setup_pres_save = _setup
    return doc, win


# ── Opening ───────────────────────────────────────────────────────────────────

def test_opening_a_file_loads_it_into_the_editor(tmp_path):
    src = tmp_path / "talk.md"
    src.write_text("# Hello\n\nBody.\n")
    doc, win = controller()

    doc.load_into_editor(src)

    assert win.editor.get_text() == "# Hello\n\nBody.\n"
    assert doc.file_path == src
    assert doc.modified is False
    assert win.title == "talk.md"


def test_opening_points_the_build_at_a_matching_pdf(tmp_path):
    src = tmp_path / "talk.md"
    src.write_text("# Hello\n")
    doc, win = controller()

    doc.load_into_editor(src)

    assert doc.output_path == tmp_path / "talk.pdf"


def test_opening_starts_at_the_first_slide(tmp_path):
    """A freshly opened file shows slide 1, not wherever the last one was."""
    src = tmp_path / "talk.md"
    src.write_text("# One\n\n---\n\n# Two\n")
    doc, win = controller()

    doc.load_into_editor(src)

    assert win.current_slide == 0


def test_opening_an_unreadable_file_reports_rather_than_raises(tmp_path):
    doc, win = controller()

    doc.load_into_editor(tmp_path / "does-not-exist.md")

    assert win.errors and "Could not open file" in win.errors[0]
    assert doc.file_path is None


def test_open_file_refuses_a_path_that_is_not_there(tmp_path):
    doc, win = controller()

    doc.open_file(tmp_path / "missing.md")

    assert win.errors and "Cannot open file" in win.errors[0]


# ── Saving ────────────────────────────────────────────────────────────────────

def test_saving_writes_the_editor_text_to_disk(tmp_path):
    dest = tmp_path / "talk.md"
    dest.write_text("old")
    doc, win = controller("new content")
    doc.file_path = dest
    doc.modified = True

    assert doc.write_document() is True
    assert dest.read_text() == "new content"
    assert doc.modified is False


def test_saving_does_not_build(tmp_path):
    """Save writes the Markdown; the build is a separate verb."""
    dest = tmp_path / "talk.md"
    dest.write_text("")
    doc, win = controller("text")
    doc.file_path = dest

    doc.save()

    assert win.converts == 0


def test_saving_builds_when_the_writer_asked_for_that(tmp_path):
    dest = tmp_path / "talk.md"
    dest.write_text("")
    doc, win = controller("text")
    doc.file_path = dest
    win.auto_convert = True

    doc.save()

    assert win.converts == 1


def test_a_failed_write_reports_and_stays_modified(tmp_path):
    unwritable = tmp_path / "nope" / "talk.md"       # parent does not exist
    doc, win = controller("text")
    doc.file_path = unwritable
    doc.modified = True

    assert doc.write_document() is False
    assert doc.modified is True
    assert win.errors and "Could not save" in win.errors[0]


def test_saving_a_bundle_repacks_it(tmp_path):
    dest = tmp_path / "slides.md"
    dest.write_text("")
    doc, win = controller("text")
    doc.file_path = dest
    doc.pres_path = tmp_path / "talk.pres"

    doc.write_document()

    assert doc.packed == 1
    assert win.title == "talk.pres"     # the bundle is what the writer sees


# ── Unsaved changes ───────────────────────────────────────────────────────────

def test_an_unmodified_document_runs_the_action_without_asking():
    doc, win = controller("text")
    doc.modified = False
    ran = []

    doc.check_unsaved(lambda: ran.append(True))

    assert ran == [True]


# ── Autosave ──────────────────────────────────────────────────────────────────

def test_autosave_writes_a_recovery_copy(tmp_path):
    from presence.session import recovery_path_for
    src = tmp_path / "talk.md"
    src.write_text("saved version")
    doc, win = controller("edited but not saved")
    doc.file_path = src
    doc.modified = True

    doc.autosave()

    assert recovery_path_for(src).read_text() == "edited but not saved"


def test_autosave_does_nothing_for_an_unmodified_document(tmp_path):
    from presence.session import recovery_path_for
    src = tmp_path / "talk.md"
    src.write_text("saved")
    doc, win = controller("saved")
    doc.file_path = src
    doc.modified = False

    doc.autosave()

    assert not recovery_path_for(src).exists()
    assert win.toasts == []


def test_autosave_keeps_an_untitled_draft_too(tmp_path):
    from presence.session import recovery_dir
    doc, win = controller("a draft with no home yet")
    doc.modified = True

    doc.autosave()

    assert (recovery_dir() / "untitled.md").read_text() == "a draft with no home yet"


# ── Recovery ──────────────────────────────────────────────────────────────────

def test_restoring_a_draft_leaves_it_unsaved():
    """A recovered draft has not been written anywhere, and must say so."""
    doc, win = controller()

    doc.restore_autosave("recovered text")

    assert win.editor.get_text() == "recovered text"
    assert doc.modified is True
    assert win.title == UNTITLED + " •"
    assert win.converts == 1
