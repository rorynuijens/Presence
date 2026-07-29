"""
test_handout.py — The talk rendered as a document.

The handout pairs each slide picture with the script written for it, so the
things that matter are that every slide appears, that the pairing is right,
and that the file stands alone once it leaves the machine.
"""

import re

import pytest

from presence.slides.handout import build_handout_html

PNG_A = b"\x89PNG\r\n\x1a\nFAKE-A"
PNG_B = b"\x89PNG\r\n\x1a\nFAKE-B"

INFO = [
    {"title": "Opening",   "notes": "Say hello, then name the problem."},
    {"title": "The point", "notes": "- first\n- second\n"},
    {"title": "Just a picture", "notes": ""},
]


def _entries(html: str) -> list[str]:
    return re.findall(r'<section class="entry.*?</section>', html, re.DOTALL)


def test_every_slide_gets_an_entry():
    html = build_handout_html(INFO, [PNG_A, PNG_B, PNG_A])
    assert len(_entries(html)) == len(INFO)


def test_images_are_inlined_so_the_file_travels():
    html = build_handout_html(INFO, [PNG_A, PNG_B, PNG_A])
    assert "data:image/png;base64," in html
    assert "src=\"http" not in html and "src=\"/" not in html


def test_each_slide_keeps_its_own_script():
    entries = _entries(build_handout_html(INFO, [PNG_A, PNG_B, PNG_A]))
    assert "name the problem" in entries[0]
    assert "name the problem" not in entries[1]
    assert "<li>first</li>" in entries[1]


def test_notes_are_rendered_as_markdown_not_dumped_raw():
    html = build_handout_html(
        [{"title": "T", "notes": "**bold** and *italic*"}], [PNG_A])
    assert "<strong>bold</strong>" in html
    assert "**bold**" not in html


def test_a_slide_without_a_script_still_appears():
    entries = _entries(build_handout_html(INFO, [PNG_A, PNG_B, PNG_A]))
    assert "Slide 3" in entries[2]
    assert "<img" in entries[2]          # the picture is the record
    assert 'class="script"' not in entries[2]
    # and it says nothing about the absence
    assert "no notes" not in entries[2].lower()
    assert "no script" not in entries[2].lower()


def test_labels_number_slides_from_one():
    html = build_handout_html(INFO, [PNG_A, PNG_B, PNG_A])
    labels = re.findall(r'<p class="label">(.*?)</p>', html)
    assert labels[0].startswith("Slide 1")
    assert labels[2].startswith("Slide 3")


def test_title_is_not_repeated_when_it_is_just_the_number():
    html = build_handout_html([{"title": "Slide 1", "notes": ""}], [PNG_A])
    assert re.search(r'<p class="label">Slide 1</p>', html)


def test_a_missing_image_leaves_the_script_rather_than_dropping_the_slide():
    html = build_handout_html(INFO, [PNG_A, None, PNG_A])
    entries = _entries(html)
    assert len(entries) == 3
    assert "<img" not in entries[1]
    assert "<li>first</li>" in entries[1]


def test_fewer_images_than_slides_is_survivable():
    html = build_handout_html(INFO, [PNG_A])
    assert len(_entries(html)) == 3


def test_masthead_carries_the_frontmatter():
    html = build_handout_html(
        INFO, [PNG_A, PNG_B, PNG_A],
        {"title": "My Talk", "author": "Someone", "date": "July 2026"})
    assert "<h1>My Talk</h1>" in html
    assert "Someone · July 2026" in html


def test_no_masthead_without_frontmatter():
    """The class is always in the stylesheet; the element must not be there."""
    html = build_handout_html(INFO, [PNG_A, PNG_B, PNG_A])
    assert '<header class="masthead">' not in html


def test_frontmatter_is_escaped():
    html = build_handout_html([], [], {"title": "<script>x</script>"})
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_entries_are_kept_whole_across_page_breaks():
    """A slide separated from its script would defeat the point."""
    html = build_handout_html(INFO, [PNG_A, PNG_B, PNG_A])
    assert "break-inside: avoid" in html
    assert "page-break-inside: avoid" in html


def test_empty_deck_produces_a_valid_document():
    html = build_handout_html([], [])
    assert html.startswith("<!DOCTYPE html>")
    assert _entries(html) == []
