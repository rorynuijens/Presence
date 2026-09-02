"""
script.py — The talk's spoken script, as blocks a teleprompter can paint.

The presenter view reads the `^^^` notes of every slide as one running
document rather than one note box at a time.  That needs the script as
structured text with emphasis recorded as character ranges, not as HTML:
the pane is a GtkTextView, where emphasis is a tag applied over a range.

Pure — no GTK — so the parsing and the timing arithmetic can be tested
without a display, and so the presenter has nothing to reimplement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Block kinds a script can contain.  Deliberately few: a script is read
# aloud, so the distinctions that matter are "is this a section title",
# "is this one of a list of points" and "is this a literal quotation".
HEADING = "heading"
PARA    = "para"
BULLET  = "bullet"
QUOTE   = "quote"
CODE    = "code"

# Inline styles carried as spans over a block's plain text.
BOLD   = "bold"
ITALIC = "italic"
MONO   = "mono"


@dataclass(frozen=True)
class Span:
    """An inline style over ``text[start:end]`` of the block that holds it."""

    start: int
    end:   int
    style: str


@dataclass
class Block:
    """One paragraph, bullet, heading or quotation of a script."""

    kind:  str
    text:  str
    spans: list[Span] = field(default_factory=list)
    # Heading depth for HEADING blocks, list depth for BULLET; 0 otherwise.
    level: int = 0


# Image before link: "![alt](src)" also matches the link pattern, and the
# alternation takes the first branch that fits.
_INLINE_RE = re.compile(
    r'!\[(?P<img>[^\]]*)\]\([^)]*\)'
    r'|(?P<fence>`+)(?P<mono>.+?)(?P=fence)'
    r'|\*\*(?P<bold>.+?)\*\*'
    r'|__(?P<bold_u>.+?)__'
    r'|(?<![\w*])\*(?P<ital>[^*\n]+?)\*(?![\w*])'
    r'|(?<![\w_])_(?P<ital_u>[^_\n]+?)_(?![\w_])'
    r'|\[(?P<link>[^\]]*)\]\([^)]*\)',
    re.DOTALL,
)

_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)$')
_BULLET_RE  = re.compile(r'^(\s*)[-*+]\s+(.*)$')
_NUMBER_RE  = re.compile(r'^(\s*)(\d{1,3})[.)]\s+(.*)$')
_QUOTE_RE   = re.compile(r'^>\s?(.*)$')
_FENCE_RE   = re.compile(r'^\s*(`{3,}|~{3,})')


def parse_inline(md: str) -> tuple[str, list[Span]]:
    """
    Reduce inline Markdown to the words spoken, plus where the emphasis is.

    A link becomes its text — the URL is not something anyone reads out —
    and an image becomes its description for the same reason.  Emphasis
    nests one level at a time by re-parsing what it wrapped, so
    ``**a *b* c**`` keeps both marks instead of leaving asterisks in the
    middle of a sentence being read aloud.
    """
    out:   list[str]  = []
    spans: list[Span] = []
    pos = 0
    length = 0

    for m in _INLINE_RE.finditer(md):
        if m.start() < pos:
            continue                        # inside a match already consumed
        literal = md[pos:m.start()]
        out.append(literal)
        length += len(literal)

        if m.group("img") is not None:
            inner, style = m.group("img"), None
        elif m.group("mono") is not None:
            inner, style = m.group("mono"), MONO
        elif m.group("bold") is not None:
            inner, style = m.group("bold"), BOLD
        elif m.group("bold_u") is not None:
            inner, style = m.group("bold_u"), BOLD
        elif m.group("ital") is not None:
            inner, style = m.group("ital"), ITALIC
        elif m.group("ital_u") is not None:
            inner, style = m.group("ital_u"), ITALIC
        else:
            inner, style = m.group("link") or "", None

        if style == MONO:
            # Code is literal by definition: nothing inside it is markup.
            inner_text, inner_spans = inner, []
        else:
            inner_text, inner_spans = parse_inline(inner)

        if style is not None and inner_text:
            spans.append(Span(length, length + len(inner_text), style))
        spans.extend(
            Span(s.start + length, s.end + length, s.style) for s in inner_spans
        )
        out.append(inner_text)
        length += len(inner_text)
        pos = m.end()

    tail = md[pos:]
    out.append(tail)
    return "".join(out), spans


def _bulleted(prefix: str, text: str, spans: list[Span],
              level: int) -> Block:
    """
    A list item carrying its own marker, with the spans moved to suit.

    The marker is part of the text rather than something the caller draws,
    so that an ordered list's spoken figures and an unordered list's bullet
    are the same kind of thing and neither can drift from its spans.
    """
    off = len(prefix)
    return Block(
        BULLET,
        prefix + text,
        [Span(s.start + off, s.end + off, s.style) for s in spans],
        level=level,
    )


def parse_script(notes_md: str) -> list[Block]:
    """
    Split a slide's `^^^` notes into the blocks a teleprompter shows.

    Soft-wrapped lines are joined into the block they continue, as Markdown
    means them: a script wraps at whatever width it was typed at, and a
    list item typed across three lines is one thing said, not three.  That
    is why the loop carries a pending block rather than emitting a block
    per line — the continuation only makes sense once it has somewhere to
    go, and its inline markup can span the join.
    """
    blocks: list[Block] = []
    # (kind, marker, raw lines, level) of the block still being gathered.
    pending: tuple[str, str, list[str], int] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        kind, marker, raw, level = pending
        pending = None
        text, spans = parse_inline(" ".join(raw).strip())
        if not text.strip():
            return
        if marker:
            blocks.append(_bulleted(marker, text, spans, level))
        else:
            blocks.append(Block(kind, text, spans, level=level))

    lines = notes_md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        fence = _FENCE_RE.match(line)
        if fence:
            flush()
            marker = fence.group(1)[0]
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith(marker * 3):
                body.append(lines[i])
                i += 1
            i += 1                          # step over the closing fence
            if body:
                blocks.append(Block(CODE, "\n".join(body)))
            continue

        if not line.strip():
            flush()
            i += 1
            continue

        m = _HEADING_RE.match(line)
        if m:
            flush()
            text, spans = parse_inline(m.group(2).strip())
            blocks.append(Block(HEADING, text, spans, level=len(m.group(1))))
            i += 1
            continue

        m = _QUOTE_RE.match(line)
        if m:
            if pending is None or pending[0] != QUOTE:
                flush()
                pending = (QUOTE, "", [], 0)
            pending[2].append(m.group(1).strip())
            i += 1
            continue

        m = _BULLET_RE.match(line)
        if m:
            flush()
            pending = (BULLET, "• ", [m.group(2).strip()],
                       len(m.group(1)) // 2)
            i += 1
            continue

        m = _NUMBER_RE.match(line)
        if m:
            flush()
            # Keep the number: in a script an ordered list is usually being
            # counted out loud ("three things"), so the figures are spoken.
            pending = (BULLET, f"{m.group(2)}. ", [m.group(3).strip()],
                       len(m.group(1)) // 2)
            i += 1
            continue

        # A plain line continues whatever is open, and opens a paragraph
        # when nothing is.
        if pending is None:
            pending = (PARA, "", [], 0)
        pending[2].append(line.strip())
        i += 1

    flush()
    return blocks


def script_words(notes_md: str) -> int:
    """Words a presenter would actually say, markup excluded."""
    return sum(
        len(re.findall(r'\S+', b.text))
        for b in parse_script(notes_md)
        if b.kind != CODE
    )


def speaking_seconds(words: int, wpm: int = 110) -> int:
    """Seconds *words* takes to say at *wpm*, rounded to the nearest second."""
    if words <= 0 or wpm <= 0:
        return 0
    return max(1, round(words / wpm * 60))


@dataclass(frozen=True)
class Timing:
    """How long a slide takes, and which words that came from."""

    seconds:     int
    words:       int
    from_script: bool


def slide_timing(notes_md: str, body_md: str = "", wpm: int = 110) -> Timing:
    """
    How long one slide takes to deliver, and what the estimate counted.

    Measured from the script where the writer wrote one, because what
    takes time is what you say, not the words standing behind you.  A
    slide with no script falls back to its own text — the only estimate
    left.

    The word count comes back with the duration so a caller showing both
    shows the same two numbers: a count that did not feed the estimate
    would contradict it on every slide carrying a script.
    """
    words = script_words(notes_md)
    from_script = words > 0
    if not from_script:
        words = len(re.findall(r'\S+', body_md or ""))
    return Timing(speaking_seconds(words, wpm), words, from_script)


def slide_seconds(notes_md: str, body_md: str = "", wpm: int = 110) -> int:
    """Seconds one slide takes to deliver; see :func:`slide_timing`."""
    return slide_timing(notes_md, body_md, wpm).seconds


def document_timing(markdown_text: str, wpm: int = 110) -> Timing:
    """
    How long a whole document takes to deliver, slide by slide.

    The header counted every non-space token in the file, which made the
    separators, the frontmatter and each ``#`` a word the speaker would
    say.  Summing the per-slide estimates instead means the header, the
    thumbnail strip and the presenter's pace all quote one number.

    ``from_script`` is true when any slide in the deck carries one, since
    that is what the total is mostly made of.
    """
    from .frontmatter import parse_frontmatter, strip_slide_directives
    from .splitter import extract_speaker_notes, split_slides
    from .image_attrs import extract_images_with_attrs
    from .reveal import strip_markers

    _, body_text = parse_frontmatter(markdown_text)
    per = []
    for slide_md in split_slides(body_text):
        body, notes = extract_speaker_notes(slide_md)
        # Images out, as the strip's own body count has them: a picture is
        # not read aloud, and "![A chart](chart.png)" is not three words.
        # Step markers and directives out for the same reason: nobody says
        # "+++" from the stage.
        cleaned, _images = extract_images_with_attrs(body)
        cleaned = strip_markers(strip_slide_directives(cleaned))
        per.append(slide_timing(notes, cleaned, wpm))
    return Timing(
        sum(t.seconds for t in per),
        sum(t.words for t in per),
        any(t.from_script for t in per),
    )


def deck_schedule(slide_info: list[dict], wpm: int = 110,
                  target_secs: int = 0) -> list[int]:
    """
    The second each slide is due to end, cumulatively from the start.

    With a target duration set, the estimates are scaled to it rather than
    used raw: the script says how the time divides between slides, and the
    duration the speaker committed to says how much time there is.  A
    scriptless deck has nothing to divide, so it is spread evenly.
    """
    per = [
        slide_seconds(s.get("notes", ""), s.get("body", ""), wpm)
        for s in slide_info
    ]
    total = sum(per)

    if target_secs > 0:
        if total > 0:
            per = [round(p / total * target_secs) for p in per]
        elif per:
            share = target_secs / len(per)
            per = [round(share)] * len(per)

    out, running = [], 0
    for p in per:
        running += p
        out.append(running)
    return out
