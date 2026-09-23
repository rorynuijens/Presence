"""
test_build_from_buffer.py — A build renders the buffer and writes nothing else.

Building used to save.  The converter took a path and read it, so
``BuildCoordinator.trigger()`` had to put the editor's text on disk first —
which meant Present, Ctrl+Return, the header chip, Export HTML, Export
Images, Export Handout and Open PDF all committed the writer's unsaved edits
over the file, with no prompt and nothing said about it.  That is the exact
inverse of the rule the app documents ("saving does not build"), and it is
the one place where a build could destroy work.

The converter now takes text, like the live render and the preview already
did.  What must hold:

*  A build never writes the document, however modified it is.
*  What gets rendered is the buffer, not whatever the file last said.
*  A deck that has never been saved builds to a scratch PDF and no scratch
   Markdown, because there is no longer anything to be read back.
*  Relative image sources resolve against the *document's* directory even
   when Export PDF has re-pointed the build's output somewhere else.

Widgets cannot be built under this suite's conftest, so this drives the real
coordinator against the stand-in window test_build_coordinator.py uses.
"""

from pathlib import Path

import pytest

from presence.build_coordinator import BuildCoordinator
from tests.test_build_coordinator import FakeWindow


class RecordingConverter:
    """Remembers the one call the coordinator is supposed to make."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.ratio = "16:9"

    def convert(self, text, base_dir, output_path) -> None:
        self.calls.append((text, Path(base_dir) if base_dir else None,
                           Path(output_path)))


class ExplodingDocuments:
    """
    Where the document is, plus every way of writing it wired to explode.

    A build reads the three paths and must write none of them; anything that
    reaches for a save is the regression this file exists to catch.
    """

    def __init__(self) -> None:
        self.file_path   = None
        self.output_path = None
        self.pres_path   = None
        self.modified    = True

    @property
    def base_dir(self):
        return self.file_path.parent if self.file_path else None

    def write_document(self, on_done=None) -> bool:
        raise AssertionError("a build must not write the document")

    def save(self, on_done=None) -> bool:
        raise AssertionError("a build must not save the document")

    def save_as_dialog(self, on_done=None) -> bool:
        raise AssertionError("a build must not open a save dialog")


_made: list = []


@pytest.fixture(autouse=True)
def _clean_scratch():
    """Delete the scratch PDFs mkstemp handed out for unsaved decks."""
    _made.clear()
    yield
    for coord in _made:
        if coord.temp_pdf is not None:
            coord.temp_pdf.unlink(missing_ok=True)
    _made.clear()


def coordinator(tmp_path, *, text="# Live", saved=True):
    win = FakeWindow(current=text)
    win.converter = RecordingConverter()
    win.documents = ExplodingDocuments()
    if saved:
        win.documents.file_path = tmp_path / "talk.md"
        win.documents.file_path.write_text("# On disk", encoding="utf-8")
        win.documents.output_path = tmp_path / "talk.pdf"
    coord = BuildCoordinator(win)
    _made.append(coord)
    return coord, win


# ── The document is left alone ────────────────────────────────────────────────

def test_a_build_does_not_write_the_document(tmp_path):
    """The whole point: Present must not commit what the writer has not saved."""
    coord, win = coordinator(tmp_path)

    coord.trigger()

    assert win.documents.file_path.read_text(encoding="utf-8") == "# On disk"
    assert win.documents.modified is True, "a build must not clear the modified flag"


def test_a_build_of_an_unsaved_deck_asks_for_no_filename(tmp_path):
    """There is nothing to write, so there is nothing to name."""
    coord, win = coordinator(tmp_path, saved=False)

    coord.trigger()          # ExplodingDocuments raises if a dialog opens

    assert len(win.converter.calls) == 1


# ── The buffer is what gets rendered ──────────────────────────────────────────

def test_the_build_renders_the_buffer_not_the_file(tmp_path):
    coord, win = coordinator(tmp_path, text="# Live")

    coord.trigger()

    text, _base, _out = win.converter.calls[0]
    assert text == "# Live"


def test_the_pdf_goes_where_the_document_points_it(tmp_path):
    coord, win = coordinator(tmp_path)

    coord.trigger()

    _text, _base, out = win.converter.calls[0]
    assert out == tmp_path / "talk.pdf"


def test_images_resolve_against_the_document_not_the_export_destination(tmp_path):
    """Export PDF re-points the output; the deck's pictures have not moved."""
    coord, win = coordinator(tmp_path)
    elsewhere = tmp_path / "usb"
    elsewhere.mkdir()
    win.documents.output_path = elsewhere / "handed-over.pdf"

    coord.trigger()

    _text, base, out = win.converter.calls[0]
    assert base == tmp_path, "assets live next to the Markdown"
    assert out == elsewhere / "handed-over.pdf"


def test_a_document_with_no_output_path_still_builds(tmp_path):
    coord, win = coordinator(tmp_path)
    win.documents.output_path = None

    coord.trigger()

    _text, _base, out = win.converter.calls[0]
    assert out == tmp_path / "talk.pdf"


# ── The unsaved deck's scratch file ───────────────────────────────────────────

def test_an_unsaved_deck_builds_to_a_scratch_pdf(tmp_path):
    coord, win = coordinator(tmp_path, saved=False)

    coord.trigger()

    _text, base, out = win.converter.calls[0]
    assert out.suffix == ".pdf"
    # No folder: that is how the converter knows this is a draft, whose
    # pictures may come from anywhere on this machine (see sources.py).
    assert base is None
    assert coord.temp_pdf == out


def test_no_scratch_markdown_is_written(tmp_path):
    """The temporary copy of the document existed only to be read back."""
    coord, win = coordinator(tmp_path, saved=False)

    coord.trigger()

    _text, _base, out = win.converter.calls[0]
    assert not out.with_suffix(".md").exists()


def test_the_scratch_pdf_is_reused_across_builds(tmp_path):
    """One unsaved deck, one scratch file — not one per keystroke of Ctrl+Return."""
    coord, win = coordinator(tmp_path, saved=False)

    coord.trigger()
    coord.trigger()

    first, second = (call[2] for call in win.converter.calls)
    assert first == second
