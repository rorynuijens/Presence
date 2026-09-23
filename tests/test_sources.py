"""
test_sources.py — A deck can only load what it is allowed to.

People send each other decks, and Presence builds a deck as soon as it is
opened.  So these tests check that a deck cannot reach outside its own
folder, or onto the web, unless it says so — and that when something is
held back, the writer is told.
"""

import os
from pathlib import Path

import pytest
from PIL import Image

from presence.slides.diagnostics import build_warnings
from presence.slides.html import md_to_html_slides
from presence.slides.sources import (SourcePolicy, wants_remote_images,
                                     INLINE, LOCAL, REMOTE, OUTSIDE,
                                     REMOTE_OFF, UNSAFE)

PIXEL = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
         "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture
def places(tmp_path):
    """A deck folder with a picture, and a private folder next to it."""
    deck = tmp_path / "deck"
    (deck / "assets").mkdir(parents=True)
    private = tmp_path / "private"
    private.mkdir()
    Image.new("RGB", (8, 8), (200, 0, 0)).save(deck / "assets" / "ok.png")
    Image.new("RGB", (8, 8), (0, 0, 200)).save(private / "secret.png")
    return deck, private


# ── What the policy says ──────────────────────────────────────────────────────

def test_a_picture_in_the_decks_own_folder_is_allowed(places):
    deck, _ = places
    verdict, path = SourcePolicy(deck).check("assets/ok.png")
    assert verdict == LOCAL
    assert path == (deck / "assets" / "ok.png").resolve()


@pytest.mark.parametrize("src", [
    "../private/secret.png",                 # climbing out
    "{private}/secret.png",                  # a full path
    "file://{private}/secret.png",           # the same, as a URL
])
def test_a_picture_outside_the_folder_is_not(places, src):
    deck, private = places
    verdict, _ = SourcePolicy(deck).check(src.format(private=private))
    assert verdict == OUTSIDE


def test_a_link_inside_the_folder_cannot_point_out_of_it(places):
    deck, private = places
    os.symlink(private / "secret.png", deck / "assets" / "sneaky.png")
    verdict, _ = SourcePolicy(deck).check("assets/sneaky.png")
    assert verdict == OUTSIDE


def test_the_web_is_off_unless_the_deck_asks(places):
    deck, _ = places
    assert SourcePolicy(deck).check("https://example.com/x.png")[0] == REMOTE_OFF
    assert SourcePolicy(deck, allow_remote=True).check(
        "https://example.com/x.png")[0] == REMOTE


def test_inline_pictures_are_always_fine(places):
    deck, _ = places
    assert SourcePolicy(deck).check(PIXEL)[0] == INLINE
    assert SourcePolicy.data_only().check(PIXEL)[0] == INLINE


@pytest.mark.parametrize("src", ["javascript:alert(1)", "vbscript:x", "ftp://h/x.png", ""])
def test_odd_schemes_are_refused(places, src):
    deck, _ = places
    assert SourcePolicy(deck).check(src)[0] == UNSAFE


def test_a_draft_may_use_pictures_from_anywhere(places):
    """Every picture in an unsaved draft was put there by the writer."""
    _, private = places
    assert SourcePolicy(None).check(str(private / "secret.png"))[0] == LOCAL


def test_data_only_refuses_every_file(places):
    _, private = places
    assert SourcePolicy.data_only().check(str(private / "secret.png"))[0] == OUTSIDE


def test_theme_folders_are_readable(places, tmp_path):
    deck, _ = places
    themes = tmp_path / "themes"
    (themes / "mine" / "fonts").mkdir(parents=True)
    font = themes / "mine" / "fonts" / "Face-Regular.woff2"
    font.write_bytes(b"")
    policy = SourcePolicy(deck, extra_roots=[themes])
    assert policy.allows_url(font.as_uri())


@pytest.mark.parametrize("value,expected", [
    (True, True), ("true", True), ("yes", True), (False, False),
    ("no", False), (None, False),
])
def test_remote_images_frontmatter_key(value, expected):
    assert wants_remote_images({"remote_images": value}) is expected


# ── What reaches the slide ────────────────────────────────────────────────────

def _render(md, deck, meta=None):
    return md_to_html_slides([md], "", None, meta or {}, base_url=str(deck))


def test_an_outside_picture_never_reaches_the_markup(places):
    deck, private = places
    html, info = _render(f"## T\n\n![secret]({private}/secret.png)", deck)
    assert "secret.png" not in html
    assert info[0]["outside_images"] == [f"{private}/secret.png"]


def test_a_web_picture_waits_for_permission(places):
    deck, _ = places
    url = "https://example.com/pixel.png"
    html, info = _render(f"## T\n\n![p]({url})", deck)
    assert url not in html
    assert info[0]["remote_images"] == [url]

    html, info = _render(f"## T\n\n![p]({url})", deck, {"remote_images": True})
    assert "example.com" in html
    assert info[0]["remote_images"] == []


def test_the_writer_is_told_what_was_held_back(places):
    deck, private = places
    _html, info = _render(f"![a]({private}/secret.png)\n\n"
                          "![b](https://example.com/b.png)\n\n## T", deck)
    said = " ".join(build_warnings(info))
    assert "outside the presentation's folder" in said
    assert "remote_images: true" in said


def test_an_allowed_picture_is_untouched(places):
    deck, _ = places
    html, info = _render("## T\n\n![ok](assets/ok.png)", deck)
    assert "assets/ok.png" in html
    assert info[0]["outside_images"] == info[0]["missing_images"] == []


# ── WeasyPrint itself cannot be talked round ──────────────────────────────────

def test_a_stylesheet_cannot_fetch_what_a_picture_could_not(places):
    """The markup is checked; a stylesheet's url() is caught by the fetcher."""
    weasyprint = pytest.importorskip("weasyprint")
    deck, private = places
    policy = SourcePolicy(deck)
    fetcher = policy.fetcher()
    html = (f"<style>body{{background:url('{(private / 'secret.png').as_uri()}')}}"
            f"h1{{background:url('https://127.0.0.1:9/p.png')}}</style>"
            f"<h1>x</h1><img src='assets/ok.png'>")
    weasyprint.HTML(string=html, base_url=str(deck),
                    url_fetcher=fetcher).render()
    assert (private / "secret.png").as_uri() in fetcher.refused
    assert "https://127.0.0.1:9/p.png" in fetcher.refused
    assert not any("ok.png" in url for url in fetcher.refused)


# ── A draft keeps its pictures when it is first saved ─────────────────────────

def test_saving_a_draft_copies_its_pictures_in(gtk, places, tmp_path):
    from presence.editor import Editor

    deck, private = places
    editor = Editor()
    editor.set_text(f"## T\n\n![secret]({private}/secret.png){{left}}\n")
    editor.set_base_path(deck / "talk.md")

    assert editor.adopt_outside_pictures() == 1
    assert editor.get_text() == "## T\n\n![secret](assets/secret.png){left}\n"
    assert (deck / "assets" / "secret.png").is_file()
    assert SourcePolicy(deck).check("assets/secret.png")[0] == LOCAL


def test_pictures_already_in_the_folder_are_left_alone(gtk, places):
    from presence.editor import Editor

    deck, _ = places
    editor = Editor()
    text = f"![ok]({deck}/assets/ok.png)\n\n![ok](assets/ok.png)"
    editor.set_text(text)
    editor.set_base_path(deck / "talk.md")

    assert editor.adopt_outside_pictures() == 0
    assert editor.get_text() == text
