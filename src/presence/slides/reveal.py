"""
reveal.py — Holding part of a slide back until it has been said.

A slide arrives complete: the room reads all six bullets in two seconds and
stops listening while the speaker is still on the first.  A *step* is the
smallest thing this app can do about that — the same slide, laid out once,
shown several times with the later blocks not yet painted.

Two ways to ask for one, both ending in the same answer: a list of source
lines where a new step begins.

``+++`` on its own line
    A step break between blocks.  The marker is replaced by a *blank* line
    rather than deleted, so every line after it keeps its number and the
    fold marker still points at the line the writer has to edit.  Between
    blocks a blank line is already there, which is why a slide with ``+++``
    renders exactly like the same slide with the markers taken out.

``reveal: lists``
    Frontmatter for the deck, or ``<!-- reveal: lists -->`` for one slide:
    every top-level list item begins a step.  It exists because that is the
    common case, and because it never touches the Markdown at all — a list
    written this way cannot pick up the item spacing a blank line between
    two items would give it.

What a step is *not* is a re-render.  Every step of a slide is the one
fragment the renderer produced, with a class added to the blocks that have
not arrived yet, so the blocks that have arrived cannot move underneath
them.  ``visibility: hidden`` is what the class does in the generated
stylesheet: WeasyPrint keeps the box and paints nothing, and the hidden text
is not in the PDF either.

Pure — no GTK, no HTML knowledge beyond the ``data-src-line`` stamps
``renderer.py`` already writes — so both engines can use it.
"""

from __future__ import annotations

import re

# Imported rather than restated: what counts as "inside a fenced code block"
# is the splitter's rule, and a second copy of it here could only drift.
from .splitter import _fence_aware_split

# The marker, on a line of its own.  One of the family: --- splits slides,
# ||| splits columns, ^^^ starts the script, +++ holds the rest back.
STEP_MARKER = "+++"

# What the stylesheet hides.  A theme can redefine it — dimming rather than
# hiding is a legitimate house style — which is why it is a class and not an
# inline style.
HIDDEN_CLASS = "not-yet"

# Every line start.  Matched against the fence-aware splitter to find which
# lines of a slide are ordinary text and which are inside a code block.
_LINE_START = re.compile(r'^', re.MULTILINE)

# A list item at the left margin.  Nested items belong to the step their
# parent item opens, so the indentation is deliberately not allowed for.
_TOP_ITEM_RE = re.compile(r'^(?:[-*+]|\d{1,3}[.)])[ \t]')

# Values of the reveal key that mean "one step per list item", and the ones
# that mean "not on this slide" — a per-slide directive has to be able to
# say no to a deck-wide frontmatter key.
_LIST_MODES = frozenset(("list", "lists", "items", "bullets"))
_OFF_MODES  = frozenset(("none", "off", "no", "false"))


def open_lines(text: str) -> list[int]:
    """
    The lines of *text* that are not inside a fenced code block.

    Derived from the splitter's own fence handling by splitting on a
    zero-width match at every line start: the parts it hands back are the
    text between consecutive *open* line starts, so their lengths add up to
    exactly the offsets of those lines and nothing has to re-implement what
    a fence is.
    """
    parts = _fence_aware_split(text, _LINE_START)
    offsets: list[int] = []
    running = 0
    for part in parts[:-1]:
        running += len(part)
        offsets.append(running)
    return [text.count("\n", 0, offset) for offset in offsets]


def wants_lists(directives: dict | None, meta: dict | None) -> bool:
    """
    Whether this slide reveals its lists item by item.

    The slide's own directive outranks the deck's frontmatter, in both
    directions: a deck can ask for stepped lists throughout and one slide
    can say ``<!-- reveal: none -->``.
    """
    for source in (directives or {}, meta or {}):
        raw = source.get("reveal")
        if raw is None:
            continue
        mode = str(raw).strip().lower()
        if mode in _LIST_MODES:
            return True
        if mode in _OFF_MODES:
            return False
    return False


def plan(slide_md: str, directives: dict | None = None,
         meta: dict | None = None) -> tuple[str, list[int]]:
    """
    Read a slide's steps off it, and hand back the Markdown to render.

    Returns ``(markdown, boundaries)`` — the slide with every ``+++`` line
    blanked, and the line *within that Markdown* at which each step after
    the first begins.  An empty list means the slide is shown all at once,
    which is nearly every slide.

    The blanking happens whether or not anything will act on the steps: a
    marker that reached the renderer would be a paragraph reading "+++" on
    the slide, and a word in the count that decides the layout.
    """
    if not slide_md:
        return slide_md, []

    lines  = slide_md.splitlines()
    ends_nl = slide_md.endswith("\n")
    opened = open_lines(slide_md)

    marker_lines = [
        ln for ln in opened
        if ln < len(lines) and lines[ln].strip() == STEP_MARKER
    ]
    boundaries = set(marker_lines)

    if wants_lists(directives, meta):
        boundaries.update(
            ln for ln in opened
            if ln < len(lines) and _TOP_ITEM_RE.match(lines[ln])
        )

    if not boundaries:
        return slide_md, []

    for ln in marker_lines:
        lines[ln] = ""
    text = "\n".join(lines) + ("\n" if ends_nl else "")

    # A boundary at the very top would open with a blank slide, which is
    # never what was meant; one past the last line can never hide anything.
    return text, sorted(b for b in boundaries if 0 < b < len(lines))


def strip_markers(text: str) -> str:
    """
    *text* with every step marker blanked, for anything that only counts.

    The strip's titles and timings and the deck's word count all read the
    document directly rather than through a build, and none of them should
    see "+++" as a word of the talk.
    """
    if STEP_MARKER not in text:
        return text
    lines = text.splitlines()
    for ln in open_lines(text):
        if ln < len(lines) and lines[ln].strip() == STEP_MARKER:
            lines[ln] = ""
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


# ── Marking a fragment ───────────────────────────────────────────────────────

_TAG_RE = re.compile(
    r'<([a-zA-Z][\w-]*)((?:[^>"]|"[^"]*")*?\bdata-src-line="(\d+)"(?:[^>"]|"[^"]*")*)>'
)
_CLASS_RE = re.compile(r'\bclass="([^"]*)"')


def _with_class(attrs: str, cls: str) -> str:
    """*attrs* with *cls* added to its class attribute, making one if needed."""
    if _CLASS_RE.search(attrs):
        return _CLASS_RE.sub(lambda m: f'class="{m.group(1)} {cls}"', attrs, count=1)
    return f'{attrs} class="{cls}"'


def mark_hidden(html_frag: str, from_line: int,
                cls: str = HIDDEN_CLASS) -> str:
    """
    Mark every block of *html_frag* that starts at or after *from_line*.

    Works off the ``data-src-line`` stamps ``renderer.py`` writes for the
    fold, which is why a step needs no wrapper element of its own: nothing
    is added to the tree, so a theme's selectors see exactly the document
    they saw before.
    """
    def _mark(m: "re.Match[str]") -> str:
        if int(m.group(3)) < from_line:
            return m.group(0)
        return f'<{m.group(1)}{_with_class(m.group(2), cls)}>'

    return _TAG_RE.sub(_mark, html_frag)


def expand(html_frag: str, boundaries: list[int]) -> list[str]:
    """
    One fragment per step: the same slide, revealed a little further each time.

    The last step is the fragment itself, untouched, so a stepped slide ends
    exactly as the unstepped one would have.
    """
    if not boundaries:
        return [html_frag]
    return [mark_hidden(html_frag, line) for line in boundaries] + [html_frag]
