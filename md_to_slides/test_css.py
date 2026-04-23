"""
test_css.py — Unit tests for CSS generation.

Run with:  pytest test_css.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from md_to_slides.themes import Theme
from md_to_slides.css import build_css


def _css(theme=None, width=1280, height=720, logo=None):
    t = theme or Theme()
    return build_css(t, width, height, logo)


def test_build_css_returns_string():
    assert isinstance(_css(), str)
    assert len(_css()) > 100


def test_css_contains_root_variables():
    css = _css()
    assert "--p-bg:" in css
    assert "--p-accent:" in css
    assert "--p-fg:" in css


def test_css_slide_dimensions():
    css = _css(width=1024, height=768)
    assert "1024px" in css
    assert "768px" in css


def test_css_no_logo_no_logo_class():
    css = _css(logo=None)
    assert ".slide-logo" not in css


def test_css_with_logo_has_logo_class():
    css = _css(logo="data:image/png;base64,abc")
    assert ".slide-logo" in css


def test_css_accent_colour_in_output():
    t = Theme(accent="#ff0000")
    css = build_css(t, 1280, 720, None)
    assert "#ff0000" in css


def test_css_bg_colour_in_output():
    t = Theme(bg="#123456")
    css = build_css(t, 1280, 720, None)
    assert "#123456" in css


def test_css_callout_types_present():
    css = _css()
    for kind in ("tip", "info", "warning", "danger"):
        assert f".callout-{kind}" in css


def test_css_no_xss_in_callout_icon():
    """Callout icon must have single-quotes stripped to prevent CSS injection."""
    t = Theme(callout_icon_tip="'injected'")
    css = build_css(t, 1280, 720, None)
    # The quotes must be stripped
    assert "injected" not in css or "'" not in css.split("injected")[0][-5:]


def test_css_image_layout_present():
    css = _css()
    assert "has-image" in css
    assert "data-img-pos" in css


def test_css_legacy_dict_accepted():
    """build_css must accept old-style dicts for backward compatibility."""
    d = {"bg": "#fff", "fg": "#000", "accent": "#f00",
         "code_bg": "#eee", "heading_color": "#f00",
         "title_bg": "#000", "title_fg": "#fff",
         "pygments_style": "friendly",
         "callout_tip":     ("#e6f4ea", "#2d6a4f", "#52b788"),
         "callout_info":    ("#e8f0fe", "#1a56db", "#3b82f6"),
         "callout_warning": ("#fff8e1", "#b45309", "#f59e0b"),
         "callout_danger":  ("#fdecea", "#b91c1c", "#ef4444")}
    css = build_css(d, 1280, 720, None)
    assert isinstance(css, str)
    assert "#fff" in css
