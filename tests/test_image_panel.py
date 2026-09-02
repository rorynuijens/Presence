"""
test_image_panel.py — Being told about a picture is not choosing for it.

The panel is filled in every time the writer clicks a picture, including one
they were already on. If that read as seven choices it would write a block
back, mark the document modified and start a build, on nothing but a click —
and a picture with nothing pinned would acquire a block spelling out the
defaults the moment it was looked at.

That is not hypothetical: the theme editor's wizard had exactly this bug, in
the toggle-group form, and reset the theme it was opened on. These rows are
Adw.ComboRow for that reason, and every setter here is blocked.

The other half is the inverse — a real move must emit, and must emit the
whole block rather than a patch onto whatever was there before.
"""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from presence.image_panel import ImagePanel   # noqa: E402


@pytest.fixture
def panel(gtk):
    made = []

    def build():
        p = ImagePanel()
        made.append(p)
        return p

    yield build
    for p in made:
        if (parent := p.get_parent()) is not None:
            parent.set_child(None) if hasattr(parent, "set_child") else None


def _signals(p):
    seen = []
    p.connect("attrs-changed", lambda _p, block: seen.append(block))
    return seen


# ── Being told ───────────────────────────────────────────────────────────────

def test_being_shown_a_picture_is_not_a_choice(panel):
    p = panel()
    seen = _signals(p)
    p.select_attrs({"position": "left", "filter": "sepia", "opacity": 40},
                   "a red barn")
    assert seen == []


def test_a_picture_with_nothing_pinned_reads_as_automatic(panel):
    p = panel()
    seen = _signals(p)
    p.select_attrs({}, "a red barn")
    assert seen == []
    assert p._pos_row.get_selected() == 0
    assert p._fit_row.get_selected() == 0
    assert p._align_row.get_selected() == 0
    assert p._filter_row.get_selected() == 0
    assert p._opacity.get_value() == 100


def test_the_rows_show_what_the_picture_pinned(panel):
    p = panel()
    p.select_attrs({"position": "background", "fit": "contain",
                    "focal": "focal-top", "filter": "sepia",
                    "opacity": 40, "tint": "#0a3d62"}, "a barn")
    assert p._pos_row.get_selected() != 0
    assert p._fit_row.get_selected() != 0
    assert p._align_row.get_selected() != 0
    assert p._filter_row.get_selected() != 0
    assert p._opacity.get_value() == 40
    assert p._tint_row.get_subtitle() == "#0a3d62"


def test_showing_the_same_picture_twice_stays_silent(panel):
    """Clicking a picture you are already on must not rewrite it."""
    p = panel()
    p.select_attrs({"position": "left"}, "a barn")
    seen = _signals(p)
    p.select_attrs({"position": "left"}, "a barn")
    assert seen == []


def test_moving_to_a_plainer_picture_clears_the_rows(panel):
    """The panel is filled from the new picture, never left holding the last
    one's values — which would then be written onto this one."""
    p = panel()
    p.select_attrs({"position": "left", "filter": "sepia"}, "first")
    p.select_attrs({}, "second")
    assert p._pos_row.get_selected() == 0
    assert p._filter_row.get_selected() == 0
    assert p._tint_row.get_subtitle() == "None"


def test_the_description_names_the_picture_being_edited(panel):
    p = panel()
    p.select_attrs({}, "a red barn")
    assert "a red barn" in p._desc.get_label()


# ── Choosing ─────────────────────────────────────────────────────────────────

def test_moving_a_row_emits_the_whole_block(panel):
    p = panel()
    p.select_attrs({}, "a barn")
    seen = _signals(p)
    p._pos_row.set_selected(1)          # left
    assert seen == ["left"]


def test_the_block_carries_every_row_not_only_the_one_that_moved(panel):
    p = panel()
    p.select_attrs({"position": "left", "filter": "sepia"}, "a barn")
    seen = _signals(p)
    p._opacity.set_value(40)
    assert seen and "left" in seen[-1] and "sepia" in seen[-1]
    assert "opacity40" in seen[-1]


def test_returning_every_row_to_automatic_writes_no_block(panel):
    """The picture goes back to the layout, rather than being pinned to
    whatever the layout happened to choose today."""
    p = panel()
    p.select_attrs({"position": "left"}, "a barn")
    seen = _signals(p)
    p._pos_row.set_selected(0)
    assert seen == [""]


def test_reset_hands_the_picture_back(panel):
    p = panel()
    p.select_attrs({"position": "left", "filter": "sepia", "opacity": 30},
                   "a barn")
    seen = _signals(p)
    p._on_reset(None)
    assert seen == [""]
    assert p._pos_row.get_selected() == 0
    assert p._opacity.get_value() == 100


def test_full_opacity_is_not_worth_saying(panel):
    p = panel()
    p.select_attrs({}, "a barn")
    seen = _signals(p)
    p._opacity.set_value(100)
    assert all("opacity" not in block for block in seen)


def test_clearing_a_tint_removes_it_from_the_block(panel):
    p = panel()
    p.select_attrs({"tint": "#0a3d62"}, "a barn")
    seen = _signals(p)
    p._on_tint_cleared(None)
    assert seen == [""]
    assert p._tint_row.get_subtitle() == "None"
