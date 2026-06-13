"""
splitter.py — Markdown-level operations on raw slide text.

All functions here work purely on strings; none produce HTML or CSS.
They form the pre-processing stage before anything is rendered.
"""

import re


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fence_aware_split(text: str, pattern: re.Pattern) -> list[str]:
    """
    Split *text* on every match of *pattern* that is NOT inside a fenced
    code block (``` or ~~~).  Returns the list of parts, which always has
    at least one element.

    Fences may be indented up to 3 spaces (CommonMark §4.5).  The closing
    fence must use the same character as the opening fence and be at least
    as long — matching real CommonMark semantics (fixes #32, #33).
    """
    fence_re = re.compile(r'^[ \t]{0,3}(`{3,}|~{3,})', re.MULTILINE)

    lines = text.splitlines(keepends=True)
    inside: set[int] = set()
    depth = 0
    open_char: str = ""
    open_len: int = 0

    for lineno, line in enumerate(lines):
        m = fence_re.match(line)
        if m:
            fence_str = m.group(1)
            ch = fence_str[0]
            length = len(fence_str)
            if depth == 0:
                depth = 1
                open_char = ch
                open_len = length
            elif ch == open_char and length >= open_len:
                depth = 0
                open_char = ""
                open_len = 0
            # else: mismatched fence char or too-short — stay inside block
        if depth > 0:
            inside.add(lineno)

    parts: list[str] = []
    last = 0
    for m in pattern.finditer(text):
        lineno = text.count('\n', 0, m.start())
        if lineno in inside:
            continue
        parts.append(text[last:m.start()])
        last = m.end()
    parts.append(text[last:])
    return parts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_SLIDE_SEP = re.compile(r'\n[ \t]*---[ \t]*\n')
_COL_SEP   = re.compile(r'\n[ \t]*\|\|\|[ \t]*\n')
_NOTES_SEP = re.compile(r'\n[ \t]*\^\^\^[ \t]*\n')

# Compiled once at module level so is_title_slide() does not recompile on
# every call (fixes #78).
_TITLE_DISQUALIFIERS = re.compile(
    r'^#{4,}'              # h4 or deeper
    r'|^[-*+]\s'           # unordered list
    r'|^\d+\.\s'           # ordered list
    r'|^[ \t]{0,3}```'    # fenced code block (backtick, up to 3 spaces indent)
    r'|^[ \t]{0,3}~~~'    # fenced code block (tilde, up to 3 spaces indent)
    r'|^\|'                # table row
    r'|^>'                 # blockquote
    r'|!\[',               # image
    re.MULTILINE,
)


def split_slides(markdown_text: str) -> list[str]:
    """
    Split a Markdown document into individual slide strings on ``---``
    horizontal rules.  Leading/trailing whitespace is stripped from each
    slide.  Separators that fall inside fenced code blocks are ignored.
    """
    parts = _fence_aware_split(markdown_text, _SLIDE_SEP)
    return [p.strip() for p in parts if p.strip()]


def split_two_columns(slide_md: str) -> list[str] | None:
    """
    If *slide_md* contains ``|||`` on its own line (outside a code fence),
    return a two-element list [left, right].  Otherwise return None.
    """
    parts = _fence_aware_split(slide_md, _COL_SEP)
    if len(parts) == 2:
        return [parts[0].strip(), parts[1].strip()]
    return None


def is_title_slide(slide_md: str, slide_index: int) -> bool:
    """
    Return True when this slide should receive the full-bleed hero layout.

    Rules:
      - Must be the first slide (index 0).
      - Must contain exactly one h1 (ATX ``# …`` or setext ``===`` style).
      - May contain a subtitle (h2, h3, or a plain paragraph) but nothing
        else: no lists, fenced code, tables, blockquotes, or images.
    """
    if slide_index != 0:
        return False

    lines = slide_md.strip().splitlines()

    atx_h1 = sum(1 for line in lines if re.match(r'^#\s', line))

    setext_h1 = sum(
        1 for i, line in enumerate(lines)
        if i > 0 and re.match(r'^=+\s*$', line) and lines[i - 1].strip()
    )

    if atx_h1 + setext_h1 != 1:
        return False

    return not _TITLE_DISQUALIFIERS.search(slide_md)


def extract_speaker_notes(slide_md: str) -> tuple[str, str]:
    """
    Split a slide on ``^^^`` (on its own line, outside code fences).

    Returns (slide_content, notes).
    """
    parts = _fence_aware_split(slide_md, _NOTES_SEP)
    if len(parts) >= 2:
        return parts[0].strip(), '\n'.join(parts[1:]).strip()
    return slide_md, ""


_IMAGE_RE = re.compile(
    r'!\[([^\]]*)\]'
    r'\('
    r'([^)\s"\']+)'
    r'(?:\s+"[^"]*")?'
    r'\)',
)

# Valid layout token sets — used by both parser and CSS generator.
IMAGE_POSITIONS  = frozenset(("left", "right", "top", "bottom", "background"))
IMAGE_FADE_DIRS  = frozenset(("left", "right", "top", "bottom"))
IMAGE_FIT        = frozenset(("cover", "contain"))
IMAGE_FOCAL      = frozenset(("focal-top", "focal-center", "focal-bottom"))

_OPACITY_TOKEN_RE   = re.compile(r'^opacity(\d{1,3})$')
_SIZE_TOKEN_RE      = re.compile(r'^(\d{1,3})$')
_GRAYSCALE_TOKEN_RE = re.compile(r'^grayscale(\d{1,3})$')
_BLUR_TOKEN_RE      = re.compile(r'^blur(\d{1,2})$')
_TINT_TOKEN_RE      = re.compile(r'^tint-(#[0-9a-f]{3,8}|[a-z]{2,30})$')
_ZOOM_TOKEN_RE      = re.compile(r'^zoom(\d{1,3})$')


def parse_image_layout(alt: str) -> dict:
    """
    Parse layout tokens embedded in an image alt-text string.

    Syntax (tokens separated by ``|``, order-insensitive):
        position    — one of: left, right, top, bottom, background  (default: right)
        size        — any integer 1–100  (percent)                   (default: 50)
        gradient    — literal word "gradient"                        (default: True)
        nogradient  — literal word "nogradient"
        opacityN    — e.g. opacity75, opacity50  (0–100)             (default: 75)
        fade-dir    — one of: fade-left, fade-right, fade-top,
                      fade-bottom  (gradient direction)               (default: None)
        fit         — one of: cover, contain                         (default: cover)
        focal       — one of: focal-top, focal-center, focal-bottom  (default: focal-center)
        grayscaleN  — e.g. grayscale100  (0–100, CSS grayscale filter)(default: 0)
        blurN       — e.g. blur5  (0–20 px, CSS blur filter)         (default: 0)
        tint-COLOR  — e.g. tint-black, tint-navy  (named CSS colour)  (default: None)
        flip-h      — mirror image horizontally                       (default: False)
        flip-v      — mirror image vertically                         (default: False)
        zoomN       — e.g. zoom150  (100–300, CSS scale transform)    (default: 100)

    Any unrecognised tokens are silently ignored so the alt text can carry a
    human-readable description alongside layout tokens.

    Returns a dict:
        {"position": str, "size": str, "gradient": bool,
         "opacity": int, "fade": str | None,
         "fit": str, "focal": str,
         "grayscale": int, "blur": int, "tint": str | None,
         "flip_h": bool, "flip_v": bool, "zoom": int}
    """
    result: dict = {
        "position": "right", "size": "50", "gradient": True,
        "opacity": 75, "fade": None,
        "fit": "cover", "focal": "focal-center",
        "grayscale": 0, "blur": 0, "tint": None,
        "flip_h": False, "flip_v": False, "zoom": 100,
    }
    for token in (t.strip().lower() for t in alt.split("|")):
        if token in IMAGE_POSITIONS:
            result["position"] = token
            if token == "background":
                result["gradient"] = False
        elif m2 := _SIZE_TOKEN_RE.match(token):
            size_val = int(m2.group(1))
            if 1 <= size_val <= 100:
                result["size"] = token
        elif token == "gradient":
            result["gradient"] = True
        elif token == "nogradient":
            result["gradient"] = False
        elif m := _OPACITY_TOKEN_RE.match(token):
            result["opacity"] = max(0, min(100, int(m.group(1))))
        elif token.startswith("fade-"):
            direction = token[5:]
            if direction in IMAGE_FADE_DIRS:
                result["fade"] = direction
        elif token in IMAGE_FIT:
            result["fit"] = token
        elif token in IMAGE_FOCAL:
            result["focal"] = token
        elif m := _GRAYSCALE_TOKEN_RE.match(token):
            result["grayscale"] = max(0, min(100, int(m.group(1))))
        elif m := _BLUR_TOKEN_RE.match(token):
            result["blur"] = max(0, min(20, int(m.group(1))))
        elif m := _TINT_TOKEN_RE.match(token):
            result["tint"] = m.group(1)
        elif token == "flip-h":
            result["flip_h"] = True
        elif token == "flip-v":
            result["flip_v"] = True
        elif m := _ZOOM_TOKEN_RE.match(token):
            zoom_val = int(m.group(1))
            if 100 <= zoom_val <= 300:
                result["zoom"] = zoom_val
    return result


def extract_images(slide_md: str) -> tuple[str, list[dict]]:
    """
    Remove all Markdown image tags from *slide_md* and collect their
    source paths together with layout metadata parsed from the alt text.

    Returns (cleaned_markdown, [{"src": str, "layout": dict}, …]).
    """
    images: list[dict] = []

    def _collect(m: re.Match) -> str:
        images.append({"src": m.group(2), "layout": parse_image_layout(m.group(1))})
        return ""

    cleaned = _IMAGE_RE.sub(_collect, slide_md)
    return cleaned.strip(), images


def infer_slide_title(slide_md: str, fallback: str) -> str:
    """
    Extract a plain-text title from the first heading in *slide_md*,
    stripping inline Markdown markers so the result is display-safe.
    """
    m = re.search(r'^#{1,3}\s+(.+)$', slide_md, re.MULTILINE)
    if not m:
        lines = slide_md.strip().splitlines()
        for i, line in enumerate(lines):
            if i > 0 and re.match(r'^[=-]+\s*$', line) and lines[i - 1].strip():
                return _strip_inline_markdown(lines[i - 1].strip())
        return fallback

    return _strip_inline_markdown(m.group(1).strip())


def _strip_inline_markdown(text: str) -> str:
    """Remove common inline Markdown markers and HTML comments from *text*."""
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    # Bold+italic combined (***…*** / ___…___) — must precede bold/italic (#61)
    text = re.sub(r'\*{3}([^*]+)\*{3}', r'\1', text)
    text = re.sub(r'_{3}([^_]+)_{3}', r'\1', text)
    # Bold/italic
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    text = re.sub(r'_{1,2}([^_]+)_{1,2}', r'\1', text)
    # Inline code
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Strikethrough
    text = re.sub(r'~~([^~]+)~~', r'\1', text)
    # Links: [text](url) and [text][ref]  (fixes #60)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\[[^\]]*\]', r'\1', text)
    # Custom id attributes {#id}
    text = re.sub(r'\{#[^}]+\}', '', text)
    return text.strip()
