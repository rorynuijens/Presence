"""
test_build_outputs.py — A build writes the PDF, and nothing else, next to it.

The folder a deck lives in belongs to the writer.  A build that leaves a
file called talk.html beside talk.pdf replaces whatever talk.html was already
there — an exported deck, or something unrelated with the same name.  So the
HTML a build needs on the way to the PDF goes to the cache folder instead.
"""

from pathlib import Path

import pytest

DECK = "---\ntitle: Talk\n---\n\n# Talk\n\n---\n\n## Two\n\n- a\n- b\n"


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Keep the build's cache file out of the real ~/.cache."""
    import presence.converter as converter
    folder = tmp_path / "cache"
    monkeypatch.setattr(converter.GLib, "get_user_cache_dir", lambda: str(folder))
    return folder


def test_a_build_leaves_the_writers_html_alone(tmp_path, cache):
    pytest.importorskip("weasyprint")
    from presence.converter import Converter

    mine = tmp_path / "talk.html"
    mine.write_text("<p>my own page</p>", encoding="utf-8")

    conv = Converter()
    conv._run(DECK, tmp_path, tmp_path / "talk.pdf")

    assert (tmp_path / "talk.pdf").is_file()
    assert mine.read_text(encoding="utf-8") == "<p>my own page</p>"
    built = conv._html_output()
    assert built.parent == cache / "presence"
    assert "<h2" in built.read_text(encoding="utf-8")

    conv.discard_html_output()
    assert not built.exists()


def test_the_cli_writes_only_the_pdf(tmp_path):
    pytest.importorskip("weasyprint")
    from presence.slides.convert import convert

    folder = tmp_path / "deck"            # its own folder, nothing else in it
    folder.mkdir()
    source = folder / "talk.md"
    source.write_text(DECK, encoding="utf-8")
    mine = folder / "talk.html"
    mine.write_text("mine", encoding="utf-8")

    convert(source, folder / "talk.pdf", "light", "16:9", None, False)

    assert (folder / "talk.pdf").is_file()
    assert mine.read_text(encoding="utf-8") == "mine"
    assert sorted(p.name for p in folder.iterdir()) == [
        "talk.html", "talk.md", "talk.pdf"]


def test_export_html_to_the_suggested_name_works(tmp_path, cache):
    """
    Export HTML suggests talk.html beside the document.  When the build wrote
    that same file, accepting the suggestion copied it onto itself and failed.
    """
    pytest.importorskip("weasyprint")
    from presence.converter import Converter
    from presence.export_controller import ExportController

    conv = Converter()
    conv._run(DECK, tmp_path, tmp_path / "talk.pdf")

    toasts = []

    class Win:
        builds = type("B", (), {"html_uri": conv._html_output().as_uri()})()
        def show_toast(self, message, timeout=4):
            toasts.append(message)

    ExportController(Win())._copy_built_html(tmp_path / "talk.html")
    assert toasts == ["HTML exported → talk.html"]
    assert "<h2" in (tmp_path / "talk.html").read_text(encoding="utf-8")
    conv.discard_html_output()
