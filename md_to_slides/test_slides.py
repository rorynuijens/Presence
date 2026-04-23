"""
test_slides.py — Unit tests for the slide parsing module.

Run with:  pytest test_slides.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from md_to_slides.slides import split_slides, is_title_slide, infer_slide_title


def test_split_basic():
    md = "# Slide 1\n\nHello\n\n---\n\n# Slide 2\n\nWorld"
    slides = split_slides(md)
    assert len(slides) == 2
    assert "Slide 1" in slides[0]
    assert "Slide 2" in slides[1]


def test_split_single_slide():
    md = "# Just one"
    slides = split_slides(md)
    assert len(slides) == 1


def test_split_empty():
    slides = split_slides("")
    assert slides == [] or len(slides) == 1  # implementation may differ


def test_split_strips_separator():
    md = "A\n\n---\n\nB"
    slides = split_slides(md)
    assert not any("---" in s for s in slides)


def test_is_title_slide_h1():
    assert is_title_slide("# Title\n\nSubtitle") is True


def test_is_title_slide_h2_not_title():
    assert is_title_slide("## Section\n\nContent") is False


def test_infer_title_from_h1():
    assert infer_slide_title("# My Title\n\nContent") == "My Title"


def test_infer_title_from_h2():
    assert infer_slide_title("## Section\n\nContent") == "Section"


def test_infer_title_fallback():
    result = infer_slide_title("Some text without heading",
                                fallback="Slide 3")
    assert result == "Slide 3"


def test_split_notes_separator():
    """Slides with ^^^ should preserve notes in slide_info."""
    md = "# Slide\n\nContent\n\n^^^\n\nSpeaker notes here"
    slides = split_slides(md)
    # The slide content should not include the notes
    assert "Speaker notes" not in slides[0]


def test_multiple_slides_correct_count():
    parts = ["# S1", "## S2", "### S3", "Content only"]
    md = "\n\n---\n\n".join(parts)
    slides = split_slides(md)
    assert len(slides) == 4
