"""
test_reveal.py — Holding part of a slide back.

Two things are pinned here and they matter in different ways.

The first is what a step *is*: a boundary read off the document, and the same
laid-out fragment shown with more of itself painted each time.  Nothing is
re-rendered between steps, which is why nothing can move between them.

The second is that a deck which reveals nothing is untouched.  A feature that
rewrites every document to make room for itself is not worth having, so the
identity check is here as a test rather than as a claim in a docstring.
"""

import pytest

from presence.slides import reveal
from presence.slides.html import md_to_html_slides


# ── Reading the steps off the document ───────────────────────────────────────

def test_a_slide_without_markers_is_one_step_and_is_not_touched():
    md = "# Head\n\nA paragraph\n\n- a\n- b\n"
    assert reveal.plan(md) == (md, [])


def test_a_marker_becomes_a_boundary_and_a_blank_line():
    text, steps = reveal.plan("one\n+++\ntwo\n")
    # The marker line stays a line: every line number after it is a fold
    # measurement and an editor position.
    assert text == "one\n\ntwo\n"
    assert steps == [1]


def test_every_marker_is_a_step():
    text, steps = reveal.plan("a\n+++\nb\n+++\nc\n")
    assert steps == [1, 3]
    assert text.splitlines() == ["a", "", "b", "", "c"]


def test_a_marker_inside_a_code_fence_is_code():
    md = "```\n+++\n```\n"
    assert reveal.plan(md) == (md, [])


def test_a_marker_on_the_first_line_opens_nothing():
    # There is no slide before it, so it can only be removed — a first step
    # showing an empty slide is never what was meant.
    text, steps = reveal.plan("+++\nbody\n")
    assert steps == []
    assert text == "\nbody\n"


def test_lists_step_item_by_item_when_asked():
    md = "# H\n\n- alpha\n- beta\n- gamma\n"
    text, steps = reveal.plan(md, {"reveal": "lists"})
    # Untouched: this is the reason the directive exists.  A "+++" between
    # two items would make the list loose and space it differently.
    assert text == md
    assert steps == [2, 3, 4]


def test_nested_items_belong_to_the_item_above_them():
    md = "- alpha\n  - one\n  - two\n- beta\n"
    _text, steps = reveal.plan(md, {"reveal": "lists"})
    assert steps == [3]          # only the second top-level item; 0 is dropped


def test_a_slide_may_refuse_what_the_deck_asked_for():
    md = "- a\n- b\n"
    assert reveal.plan(md, {"reveal": "none"}, {"reveal": "lists"})[1] == []
    assert reveal.plan(md, {}, {"reveal": "lists"})[1] == [1]


def test_markers_are_blanked_for_anything_that_only_counts():
    assert reveal.strip_markers("a\n+++\nb") == "a\n\nb"
    assert reveal.strip_markers("nothing here") == "nothing here"


# ── Marking a rendered fragment ──────────────────────────────────────────────

FRAG = ('<p data-src-line="1">one</p>\n'
        '<ul data-src-line="3"><li data-src-line="3">a</li>'
        '<li data-src-line="4">b</li></ul>')


def test_marking_hides_from_the_boundary_down():
    marked = reveal.mark_hidden(FRAG, 4)
    assert '<li data-src-line="4" class="not-yet">b</li>' in marked
    assert '<li data-src-line="3">a</li>' in marked
    assert '<p data-src-line="1">one</p>' in marked


def test_marking_keeps_the_class_a_block_already_had():
    marked = reveal.mark_hidden(
        '<div class="highlight" data-src-line="9">code</div>', 9)
    assert 'class="highlight not-yet"' in marked


def test_the_last_step_is_the_fragment_itself():
    steps = reveal.expand(FRAG, [3, 4])
    assert len(steps) == 3
    assert steps[-1] == FRAG
    # Nothing is re-rendered between steps: every step is the same string
    # with a class added, which is what keeps the blocks from moving.
    assert all(s.replace(' class="not-yet"', "").replace(
        ' not-yet"', '"') == FRAG for s in steps)


def test_nothing_to_expand_is_one_step():
    assert reveal.expand(FRAG, []) == [FRAG]


# ── What the engine does with them ───────────────────────────────────────────

def _render(slides, **kw):
    return md_to_html_slides(slides, css="", logo_b64=None, meta={},
                             line_offsets=[0] * len(slides), **kw)


def test_a_revealed_slide_is_written_out_once_per_step():
    html, info = _render(["## H\n\none\n+++\ntwo\n"], reveal=True)
    assert html.count('<div class="slide"') == 2
    assert 'data-step="0" data-steps="2"' in html
    assert info[0]["steps"] == 2


def test_the_steps_of_a_slide_differ_only_by_what_is_hidden():
    html, _ = _render(["## H\n\none\n+++\ntwo\n"], reveal=True)
    steps = html.split('<div class="slide"')[1:]
    steps[-1] = steps[-1].split("</body>")[0]
    bare = [s.replace(' class="not-yet"', "").strip() for s in steps]
    assert bare[0].replace('data-step="0"', "") == \
           bare[-1].replace('data-step="1"', "")


def test_the_last_step_shows_the_whole_slide():
    html, _ = _render(["## H\n\none\n+++\ntwo\n"], reveal=True)
    last = html.split('<div class="slide"')[-1]
    assert "not-yet" not in last


@pytest.mark.parametrize("markdown", [
    "# A cover\n\nwith a subtitle",
    "## Words\n\nand more words\n\n- a\n- b",
    "## Code\n\n```python\nx = 1\n```",
])
def test_a_deck_that_reveals_nothing_renders_exactly_as_before(markdown):
    """
    The guarantee the feature is worth having on.

    Reveal is a flag on the paged engine; a document with no markers and no
    directive has to come out the same either way, byte for byte, or every
    deck ever written has been changed by a feature it does not use.
    """
    off, _ = _render([markdown], reveal=False)
    on,  _ = _render([markdown], reveal=True)
    assert on == off
    assert "not-yet" not in on
    assert "data-step" not in on


def test_the_markers_never_reach_the_slide():
    # Even unpaged, where nothing acts on the steps: an unremoved marker is
    # a paragraph reading "+++" in the middle of the slide, and a word in
    # the count that picks the layout.
    html, _ = _render(["## H\n\none\n+++\ntwo\n"], reveal=False)
    assert "+++" not in html
    assert html.count('<div class="slide"') == 1


def test_a_directive_is_an_instruction_and_not_content():
    html, _ = _render(["## H\n\n<!-- reveal: lists -->\n\n- a\n- b\n"],
                      reveal=True)
    assert "reveal: lists" not in html
    assert html.count('<div class="slide"') == 3
