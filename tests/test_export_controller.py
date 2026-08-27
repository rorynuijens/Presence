"""
test_export_controller.py — Handing a deck to someone else.

The four export formats all end in a file being written somewhere the writer
chose, and three of the four are made out of the built PDF rather than out of
the Markdown.  These drive the part after the file dialog — which is where
everything that can go wrong lives — against a stand-in window, because GTK 4
widgets cannot be built under this suite's conftest.

The dialog itself is exercised through _ask_save_path's response handler,
driven with a fake Gtk.FileDialog result, so the suffix defaulting and the
writability refusal are covered without a display.
"""

import pytest

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

from presence.export_controller import ExportController


class FakeEditor:
    def __init__(self, text: str = "") -> None:
        self._text = text

    def get_text(self) -> str:
        return self._text


class FakeWindow:
    def __init__(self, text: str = "") -> None:
        self._editor = FakeEditor(text)
        self._file_path = None
        self._pres_path = None
        self._output_path = None
        self._html_uri = None
        self._slide_info = []
        self._active_file_dialog = None
        self.toasts = []
        self.errors = []
        self.converts = 0
        self.deferred = []

    def _show_toast(self, message, timeout=None): self.toasts.append(message)
    def _show_error(self, message):              self.errors.append(message)
    def _trigger_convert(self):                  self.converts += 1

    def _with_current_build(self, action):
        # The real one may defer; here it runs, so the test sees the result.
        self.deferred.append(action)
        action()


class FakeGFile:
    def __init__(self, path): self._path = path
    def get_path(self):       return str(self._path) if self._path else None


class FakeDialog:
    """Stands in for Gtk.FileDialog once the writer has answered it."""

    def __init__(self, path=None, cancelled: bool = False):
        self._path = path
        self._cancelled = cancelled

    def save_finish(self, _result):
        if self._cancelled:
            raise GLib.Error("cancelled")
        return FakeGFile(self._path)


def controller(text: str = "") -> tuple:
    win = FakeWindow(text)
    return ExportController(win), win


def answer(exports, chosen, suffix=".pdf", **kwargs) -> list:
    """
    Run the real _ask_save_path, then answer its dialog with *chosen*.

    Gtk.FileDialog is not a widget, so it constructs fine without a display;
    only its save() needs standing in for, to hand back the callback that the
    writer's answer would have triggered.
    """
    captured = {}
    original_save = Gtk.FileDialog.save
    Gtk.FileDialog.save = lambda self, parent, cancellable, cb: captured.setdefault("cb", cb)
    try:
        got = []
        exports._ask_save_path(
            title="Export", suffix=suffix, filter_label="files",
            on_chosen=got.append, **kwargs,
        )
        captured["cb"](FakeDialog(chosen), None)
        return got
    finally:
        Gtk.FileDialog.save = original_save


# ── Choosing where to save ────────────────────────────────────────────────────

def test_a_missing_suffix_is_filled_in(tmp_path):
    exports, _win = controller()

    got = answer(exports, tmp_path / "deck", suffix=".pdf")

    assert got == [tmp_path / "deck.pdf"]


def test_a_suffix_the_writer_typed_is_left_alone(tmp_path):
    exports, _win = controller()

    got = answer(exports, tmp_path / "deck.PDF", suffix=".pdf")

    assert got == [tmp_path / "deck.PDF"]


def test_cancelling_the_dialog_does_nothing(tmp_path):
    exports, win = controller()
    captured = {}
    original_save = Gtk.FileDialog.save
    Gtk.FileDialog.save = lambda self, p, c, cb: captured.setdefault("cb", cb)
    try:
        got = []
        exports._ask_save_path(title="Export", suffix=".pdf",
                               filter_label="files", on_chosen=got.append)
        captured["cb"](FakeDialog(cancelled=True), None)
    finally:
        Gtk.FileDialog.save = original_save

    assert got == []
    assert win.toasts == [] and win.errors == []


def test_an_unwritable_folder_is_refused_before_any_work(tmp_path):
    """Better to say so now than to fail after a build (#67)."""
    exports, win = controller()

    got = answer(exports, tmp_path / "nowhere" / "deck.pdf")

    assert got == []
    assert any("permission denied" in t for t in win.toasts)


def test_the_pdf_export_reports_a_refusal_in_the_banner_not_a_toast(tmp_path):
    """PDF export is the build, so its failure belongs where build errors go."""
    exports, win = controller()

    got = answer(exports, tmp_path / "nowhere" / "deck.pdf", transient=False)

    assert got == []
    assert any("permission denied" in e for e in win.errors)
    assert win.toasts == []


def test_the_dialog_clears_the_window_s_handle_on_it(tmp_path):
    """A stale _active_file_dialog would keep the dialog alive after it closed."""
    exports, win = controller()

    answer(exports, tmp_path / "deck.pdf")

    assert win._active_file_dialog is None


# ── Reading the built PDF ─────────────────────────────────────────────────────

def test_an_export_without_a_build_says_so_rather_than_failing():
    exports, win = controller()

    assert exports._read_built_pdf("No PDF found — convert first.") is None
    assert win.toasts == ["No PDF found — convert first."]


def test_an_export_whose_pdf_vanished_says_so(tmp_path):
    exports, win = controller()
    win._output_path = tmp_path / "gone.pdf"      # never written

    assert exports._read_built_pdf("No build.") is None
    assert win.toasts == ["No build."]


def test_the_built_pdf_is_read_when_it_is_there(tmp_path):
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")
    exports, win = controller()
    win._output_path = pdf

    assert exports._read_built_pdf("missing") == b"%PDF-1.7 fake"
    assert win.toasts == []


# ── PDF ───────────────────────────────────────────────────────────────────────

def test_exporting_a_pdf_repoints_the_build_and_converts(tmp_path):
    """The PDF *is* the build's output, so exporting one is building elsewhere."""
    exports, win = controller()
    dest = tmp_path / "handed-over.pdf"

    exports._write_pdf(dest)

    assert win._output_path == dest
    assert win.converts == 1


# ── HTML ──────────────────────────────────────────────────────────────────────

def test_exporting_html_copies_the_built_file(tmp_path):
    built = tmp_path / "built.html"
    built.write_text("<html>the deck</html>")
    dest = tmp_path / "out.html"
    exports, win = controller()
    win._html_uri = built.as_uri()

    exports._copy_built_html(dest)

    assert dest.read_text() == "<html>the deck</html>"
    assert any("HTML exported" in t for t in win.toasts)


def test_exporting_html_reports_a_failure_instead_of_raising(tmp_path):
    exports, win = controller()
    win._html_uri = (tmp_path / "never-built.html").as_uri()

    exports._copy_built_html(tmp_path / "out.html")

    assert any("Could not export HTML" in t for t in win.toasts)


# ── Images ────────────────────────────────────────────────────────────────────

def test_exporting_images_writes_one_png_per_slide(tmp_path, monkeypatch):
    import presence.slides.thumbnails_render as tr
    monkeypatch.setattr(tr, "render_slides_hires",
                        lambda pdf, width_px, pages=None: [b"one", b"two", b"three"])

    pdf = tmp_path / "talk.pdf"
    pdf.write_bytes(b"%PDF")
    folder = tmp_path / "out"
    folder.mkdir()

    exports, win = controller()
    win._output_path = pdf
    win._file_path = tmp_path / "talk.md"

    exports.render_slide_images(folder)
    _drain_idle()

    names = sorted(p.name for p in folder.iterdir())
    assert names == ["talk_01.png", "talk_02.png", "talk_03.png"]
    assert any("3 images exported" in t for t in win.toasts)


def test_a_slide_that_failed_to_render_is_skipped_not_written(tmp_path, monkeypatch):
    import presence.slides.thumbnails_render as tr
    monkeypatch.setattr(tr, "render_slides_hires",
                        lambda pdf, width_px, pages=None: [b"one", None, b"three"])
    pdf = tmp_path / "talk.pdf"
    pdf.write_bytes(b"%PDF")
    folder = tmp_path / "out"
    folder.mkdir()

    exports, win = controller()
    win._output_path = pdf
    win._file_path = tmp_path / "talk.md"

    exports.render_slide_images(folder)
    _drain_idle()

    assert sorted(p.name for p in folder.iterdir()) == ["talk_01.png", "talk_03.png"]
    assert any("2 images exported" in t for t in win.toasts)


def test_image_names_follow_the_bundle_rather_than_the_markdown(tmp_path, monkeypatch):
    """A .pres is what the writer thinks they are editing, so it names the files."""
    import presence.slides.thumbnails_render as tr
    monkeypatch.setattr(tr, "render_slides_hires", lambda pdf, width_px, pages=None: [b"one"])
    pdf = tmp_path / "slides.pdf"
    pdf.write_bytes(b"%PDF")
    folder = tmp_path / "out"
    folder.mkdir()

    exports, win = controller()
    win._output_path = pdf
    win._file_path = tmp_path / "slides.md"
    win._pres_path = tmp_path / "My Talk.pres"

    exports.render_slide_images(folder)
    _drain_idle()

    assert [p.name for p in folder.iterdir()] == ["My Talk_01.png"]


# ── Handout ───────────────────────────────────────────────────────────────────

def test_a_handout_needs_a_build_first():
    exports, win = controller()

    exports.write_handout(None)

    assert win.toasts == ["No build to make a handout from."]


def _drain_idle(timeout: float = 10.0) -> None:
    """
    Wait for the export's worker thread, then run what it queued back.

    The exports do their writing on a background thread and report on the
    main loop, so a test has to give both a turn.
    """
    import threading, time
    deadline = time.monotonic() + timeout
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


# ── Overflow pages ────────────────────────────────────────────────────────────

def test_an_export_asks_for_the_pages_the_build_measured(tmp_path, monkeypatch):
    """
    A slide whose text overflows leaves a continuation page behind it, so the
    exports must ask for the pages the build recorded rather than assume one
    page per slide.
    """
    import presence.slides.thumbnails_render as tr
    seen = {}

    def spy(pdf, width_px, pages=None):
        seen["pages"] = pages
        return [b"a", b"b"]

    monkeypatch.setattr(tr, "render_slides_hires", spy)
    pdf = tmp_path / "talk.pdf"
    pdf.write_bytes(b"%PDF")
    folder = tmp_path / "out"
    folder.mkdir()

    exports, win = controller()
    win._output_path = pdf
    win._file_path = tmp_path / "talk.md"
    win._slide_info = [{"page_index": 0}, {"page_index": 2}]   # slide 1 spilled

    exports.render_slide_images(folder)
    _drain_idle()

    assert seen["pages"] == [0, 2]


def test_a_build_that_never_recorded_pages_falls_back(tmp_path, monkeypatch):
    import presence.slides.thumbnails_render as tr
    seen = {}

    def spy(pdf, width_px, pages=None):
        seen["pages"] = pages
        return [b"a"]

    monkeypatch.setattr(tr, "render_slides_hires", spy)
    pdf = tmp_path / "talk.pdf"
    pdf.write_bytes(b"%PDF")
    folder = tmp_path / "out"
    folder.mkdir()

    exports, win = controller()
    win._output_path = pdf
    win._file_path = tmp_path / "talk.md"
    win._slide_info = [{}]                       # an older build

    exports.render_slide_images(folder)
    _drain_idle()

    assert seen["pages"] is None
