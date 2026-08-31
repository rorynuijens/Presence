"""
test_preview_render.py — Single-slide rendering used by the strip's live row.

The strip must show exactly what the PDF will contain, so the invariant
under test is: a fragment rendered with only_index=N is byte-identical to
that slide's fragment in the full document.
"""

import re

from presence.slides.html import md_to_html_slides

# Matches the opening tag of a top-level slide element only.
_SLIDE_TAG = re.compile(r'<div class="slide[ "]')

DECK = [
    "# Title slide\n\nYour subtitle here",
    "## Slide 2\n\n- First bullet point\n- Second bullet point",
    "## Slide 3\n\nLeft column content\n\n|||\n\nRight column content",
    "## Slide 4\n\nBody text\n\n^^^\n\nSpeaker notes",
]


def _render(slides, **kwargs):
    return md_to_html_slides(slides, "/* css */", None, {}, **kwargs)


def test_full_document_unchanged_without_only_index():
    html, info = _render(DECK)
    assert len(_SLIDE_TAG.findall(html)) == len(DECK)
    assert len(info) == len(DECK)


def test_only_index_emits_one_slide():
    for i in range(len(DECK)):
        html, _ = _render(DECK, only_index=i)
        assert len(_SLIDE_TAG.findall(html)) == 1


def test_fragment_matches_the_full_document():
    """A previewed slide is the same markup the PDF gets — not an approximation."""
    full, _ = _render(DECK)
    for i in range(len(DECK)):
        html, _ = _render(DECK, only_index=i)
        fragment = html.split("<body>")[1].split("</body>")[0].strip()
        assert fragment in full


def test_slide_info_covers_the_whole_deck():
    """The strip index must not shrink just because one slide was rendered."""
    _, info = _render(DECK, only_index=2)
    assert [s["title"] for s in info] == [
        "Title slide", "Slide 2", "Slide 3", "Slide 4",
    ]
    assert "Speaker notes" in info[3]["notes"]


def test_numbering_reflects_position_in_the_deck():
    """Slide 3 is numbered 2/3 whether rendered alone or with the rest."""
    html, _ = _render(DECK, only_index=2)
    assert "2 / 3" in html or "2/3" in html.replace(" ", "")


def test_only_index_beyond_the_deck_emits_nothing():
    html, info = _render(DECK, only_index=99)
    assert not _SLIDE_TAG.findall(html)
    assert len(info) == len(DECK)
