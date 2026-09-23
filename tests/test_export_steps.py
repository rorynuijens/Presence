"""
test_export_steps.py — Exporting a deck that reveals in steps.

A slide that shows its content a piece at a time is presented as several
pages.  That suits the talk, but not someone reading the PDF afterwards, so
Export PDF asks which the writer wants — and only when there is something
to ask about.
"""

from pathlib import Path

import pytest

from presence.export_controller import ExportController

STEPPED = "# Title\n\n---\n\n## Points\n\nFirst\n\n+++\n\nSecond\n\n+++\n\nThird\n"
PLAIN   = "# Title\n\n---\n\n## Points\n\nFirst\n\nSecond\n"


class _Editor:
    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text


class _Window:
    def __init__(self, text):
        self.editor = _Editor(text)


def _controller(text, monkeypatch):
    exports = ExportController(_Window(text))
    asked, chosen = [], []
    monkeypatch.setattr(exports, "_ask_about_steps", lambda: asked.append(True))
    monkeypatch.setattr(exports, "_choose_pdf_path",
                        lambda with_steps: chosen.append(with_steps))
    return exports, asked, chosen


def test_a_deck_with_steps_is_asked_about(monkeypatch):
    exports, asked, chosen = _controller(STEPPED, monkeypatch)
    exports.export_pdf()
    assert asked == [True] and chosen == []


def test_a_deck_without_steps_goes_straight_to_the_file_dialog(monkeypatch):
    exports, asked, chosen = _controller(PLAIN, monkeypatch)
    exports.export_pdf()
    assert asked == [] and chosen == [True]


def test_reveal_lists_counts_as_steps(monkeypatch):
    text = "---\nreveal: lists\n---\n\n## L\n\n- a\n- b\n"
    exports, asked, _ = _controller(text, monkeypatch)
    exports.export_pdf()
    assert asked == [True]


@pytest.mark.parametrize("with_steps,handler", [
    (True, "_write_pdf"), (False, "_write_slides_pdf"),
])
def test_each_answer_writes_its_own_kind_of_pdf(monkeypatch, with_steps, handler):
    exports = ExportController(_Window(STEPPED))
    seen = {}
    monkeypatch.setattr(exports, "_initial_folder", lambda: None)
    monkeypatch.setattr(exports, "_ask_save_path",
                        lambda **kw: seen.update(kw))
    exports._win.documents = type("D", (), {"pres_path": None,
                                            "output_path": None})()
    exports._choose_pdf_path(with_steps)
    assert seen["on_chosen"].__name__ == handler


# ── The file itself ───────────────────────────────────────────────────────────

def _pages(pdf: Path) -> int:
    gi = pytest.importorskip("gi")
    gi.require_version("Poppler", "0.18")
    from gi.repository import GLib, Poppler
    return Poppler.Document.new_from_bytes(
        GLib.Bytes.new(pdf.read_bytes())).get_n_pages()


def test_one_page_per_slide_really_is(tmp_path, monkeypatch):
    pytest.importorskip("weasyprint")
    import presence.converter as converter
    from gi.repository import GLib

    monkeypatch.setattr(converter.GLib, "get_user_cache_dir",
                        lambda: str(tmp_path / "cache"))
    conv = converter.Converter()

    # The build keeps a page for every step: 1 + 3.
    conv._run(STEPPED, tmp_path, tmp_path / "presented.pdf")
    conv.discard_html_output()
    assert _pages(tmp_path / "presented.pdf") == 4

    # The export shows each slide once.
    loop, result = GLib.MainLoop(), []

    def _done(error):
        result.append(error)
        loop.quit()
        return GLib.SOURCE_REMOVE

    conv.export_slides_pdf_async(STEPPED, tmp_path, tmp_path / "read.pdf", _done)
    GLib.timeout_add_seconds(60, loop.quit)
    loop.run()

    assert result == [None]
    assert _pages(tmp_path / "read.pdf") == 2


def test_a_failed_export_says_why(tmp_path):
    import presence.converter as converter
    from gi.repository import GLib

    loop, result = GLib.MainLoop(), []

    def _done(error):
        result.append(error)
        loop.quit()
        return GLib.SOURCE_REMOVE

    converter.Converter().export_slides_pdf_async(
        "no separators and no slides? still one slide", tmp_path,
        tmp_path / "missing-folder" / "out.pdf", _done)
    GLib.timeout_add_seconds(60, loop.quit)
    loop.run()

    assert isinstance(result[0], OSError)
