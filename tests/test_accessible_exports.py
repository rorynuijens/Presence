"""
test_accessible_exports.py — A picture's description reaches the reader.

The words in ``![a red barn](barn.jpg)`` describe the picture.  Someone who
cannot see the slide hears them through a screen reader, so they have to
end up in the HTML as alt text and in the PDF as tagged alt text.  The deck's
title and author go into the PDF too, where viewers show them.
"""

import pytest
from PIL import Image

from presence.slides.html import md_to_html_slides
from presence.slides.image_attrs import description_of

PROSE = "## Heading\n\n" + "word " * 30


def _html(md, meta=None, base_url=None):
    return md_to_html_slides([md], "", None, meta or {}, base_url=base_url)[0]


# ── Alt text in the markup ────────────────────────────────────────────────────

def test_a_lone_picture_is_described():
    assert 'alt="a red barn"' in _html(PROSE + "\n\n![a red barn](barn.png)")


def test_both_pictures_of_a_gallery_are_described():
    html = _html("## T\n\n![a barn](a.png)\n\n![a field](b.png)")
    assert 'alt="a barn"' in html and 'alt="a field"' in html


def test_a_flanking_pair_is_described(tmp_path):
    for name in ("left.png", "right.png"):
        Image.new("RGB", (300, 600)).save(tmp_path / name)
    html = _html("## T\n\nwords between\n\n![one](left.png)\n\n![two](right.png)",
                 base_url=str(tmp_path))
    assert "has-two-images" in html
    assert 'alt="one"' in html and 'alt="two"' in html


def test_quotes_in_a_description_cannot_break_the_tag():
    html = _html(PROSE + '\n\n![say "hi" & <wave>](a.png)')
    assert 'alt="say &quot;hi&quot; &amp; &lt;wave&gt;"' in html


def test_a_picture_with_no_description_says_nothing():
    assert 'alt=""' in _html(PROSE + "\n\n![](a.png)")


@pytest.mark.parametrize("alt,words", [
    ("a barn|left|30", "a barn"),
    ("left|a barn|opacity70|sepia", "a barn sepia"),   # sepia was never a token
    ("background|nogradient", ""),
    ("a|b", "a b"),
    ("plain words", "plain words"),
])
def test_retired_tokens_are_not_read_aloud(alt, words):
    assert description_of(alt) == words


def test_a_tokened_picture_still_renders_like_a_plain_one():
    """The promise test_layout_render.py makes, kept with alt text on."""
    assert (_html(PROSE + "\n\n![a barn|left|30|opacity20](a.png)")
            == _html(PROSE + "\n\n![a barn](a.png)"))


# ── The document's own name ───────────────────────────────────────────────────

def test_the_title_and_author_go_in_the_head():
    html = _html("# Hello", {"title": "Q3 <review>", "author": "Rory"})
    assert "<title>Q3 &lt;review&gt;</title>" in html
    assert '<meta name="author" content="Rory">' in html


def test_no_title_means_no_empty_title_tag():
    assert "<title>" not in _html("# Hello")


# ── In the PDF itself ─────────────────────────────────────────────────────────

def _read_pdf(pdf_bytes):
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Poppler", "0.18")
    except ValueError:
        pytest.skip("Poppler is not installed")
    from gi.repository import GLib, Poppler

    doc = Poppler.Document.new_from_bytes(GLib.Bytes.new(pdf_bytes))
    alts = []
    try:
        top = Poppler.StructureElementIter.new(doc)
    except TypeError:                      # NULL: the PDF has no tags
        top = None

    def walk(it):
        while True:
            element = it.get_element()
            if element is not None and element.get_alt_text():
                alts.append(element.get_alt_text())
            child = it.get_child()
            if child is not None:
                walk(child)
            if not it.next():
                break

    if top is not None:
        walk(top)
    return doc, top is not None, alts


def test_the_built_pdf_is_tagged_and_carries_alt_text(tmp_path, monkeypatch):
    pytest.importorskip("weasyprint")
    import presence.converter as converter

    monkeypatch.setattr(converter.GLib, "get_user_cache_dir",
                        lambda: str(tmp_path / "cache"))
    (tmp_path / "assets").mkdir()
    Image.new("RGB", (400, 300), (30, 120, 60)).save(tmp_path / "assets" / "f.png")
    deck = ("---\ntitle: Harvest\nauthor: Rory\n---\n\n# Harvest\n\n---\n\n"
            + PROSE + "\n\n![a green field](assets/f.png)\n")

    conv = converter.Converter()
    conv._run(deck, tmp_path, tmp_path / "talk.pdf")
    conv.discard_html_output()

    doc, tagged, alts = _read_pdf((tmp_path / "talk.pdf").read_bytes())
    assert tagged
    assert "a green field" in alts
    assert doc.get_title() == "Harvest"
    assert doc.get_author() == "Rory"
