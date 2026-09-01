"""
test_cli.py — The headless door.

``presence-cli talk.md`` is the invocation the README and CLAUDE.md both
document, and it used to die on an AttributeError: the output argument was
optional and undefaulted, so the first thing a new user typed produced a
traceback.  The other half of this file is the CLI agreeing with the window
about a theme that is not installed — it used to refuse to render at all
while the window silently picked whichever theme sorted first.
"""

import logging
import sys

import pytest

from presence.slides.convert import convert

pytestmark = pytest.mark.skipif(
    __import__("importlib").util.find_spec("weasyprint") is None,
    reason="WeasyPrint is needed to produce a PDF",
)

DECK = "# Title\n\nsub\n\n---\n\n## Second\n\n- one\n- two\n"


def _said(caplog) -> list[str]:
    """What the build said, minus WeasyPrint's own commentary on theme CSS."""
    return [r.getMessage() for r in caplog.records
            if r.name.startswith("presence")]


def _run_cli(*argv: str) -> None:
    from presence.slides.cli import main
    old, sys.argv = sys.argv, ["presence-cli", *argv]
    try:
        main()
    finally:
        sys.argv = old


# ── The documented invocation ─────────────────────────────────────────────────

def test_the_output_path_defaults_beside_the_document(tmp_path):
    source = tmp_path / "talk.md"
    source.write_text(DECK)

    _run_cli(str(source))

    assert (tmp_path / "talk.pdf").is_file()


def test_an_explicit_output_path_still_wins(tmp_path):
    source = tmp_path / "talk.md"
    source.write_text(DECK)
    target = tmp_path / "elsewhere.pdf"

    _run_cli(str(source), str(target))

    assert target.is_file()
    assert not (tmp_path / "talk.pdf").exists()


def test_a_missing_input_is_still_refused(tmp_path):
    with pytest.raises(SystemExit):
        _run_cli(str(tmp_path / "nothing.md"))


# ── A theme that is not installed ─────────────────────────────────────────────

def test_an_unknown_theme_still_produces_a_deck(tmp_path, caplog):
    source = tmp_path / "talk.md"
    source.write_text("---\ntheme: clasic\n---\n\n" + DECK)
    target = tmp_path / "talk.pdf"

    with caplog.at_level(logging.WARNING):
        convert(source, target, "light", "16:9", None, False)

    assert target.is_file()
    assert any("clasic" in message for message in _said(caplog))


def test_no_themes_at_all_is_still_an_error(tmp_path, monkeypatch):
    """The fallback needs something to fall back to."""
    # `presence.slides.convert` is the re-exported function, not the
    # module — slides/__init__.py shadows it.
    convert_mod = sys.modules["presence.slides.convert"]
    monkeypatch.setattr(convert_mod, "load_all_themes", lambda **kw: {})

    source = tmp_path / "talk.md"
    source.write_text(DECK)
    with pytest.raises(ValueError):
        convert(source, tmp_path / "talk.pdf", "light", "16:9", None, False)


# ── The warnings a build cannot show in the slides ────────────────────────────

def test_a_broken_picture_is_reported_to_stderr(tmp_path, caplog):
    source = tmp_path / "talk.md"
    source.write_text("# One\n\ntext\n\n---\n\n![a chart](nope.png)\n\n## Two")
    target = tmp_path / "talk.pdf"

    with caplog.at_level(logging.WARNING):
        convert(source, target, "light", "16:9", None, False)

    assert target.is_file()
    assert any("nope.png" in message for message in _said(caplog))


def test_a_clean_deck_warns_about_nothing(tmp_path, caplog):
    source = tmp_path / "talk.md"
    source.write_text(DECK)

    with caplog.at_level(logging.WARNING):
        convert(source, tmp_path / "talk.pdf", "light", "16:9", None, False)

    assert _said(caplog) == []
