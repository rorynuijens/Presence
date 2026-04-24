"""
frontmatter.py — Parse YAML frontmatter and per-slide directives.

Frontmatter block (top of file):
    ---
    title: My Presentation
    theme: berlin
    ratio: "16:9"
    logo: assets/logo.png
    custom_css: my-overrides.css
    ---

Per-slide directives (first line of a slide block as an HTML comment):
    <!-- theme: dark -->

extract_slide_directives() is called by html.py for each slide before rendering.
"""

import sys

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False
    print("Warning: pip install pyyaml  — frontmatter parsing disabled",
          file=sys.stderr)

import re

# YAML 1.1 (used by PyYAML's safe_load) treats bare colon-separated integers
# as sexagesimal numbers.  The formula is: each group × 60^position, summed
# left-to-right.  So:
#   16:9   → 16×60 + 9  = 969
#    4:3   →  4×60 + 3  = 243
#   16:10  → 16×60 + 10 = 970   (three-part not supported by YAML 1.1,
#                                 so PyYAML actually keeps "16:10" as a string)
# We coerce every form — integer and string — to the canonical "W:H" string.
_RATIO_COERCE: dict = {
    # Integer keys — what yaml.safe_load produces for unquoted two-part ratios
    969: "16:9",   # 16×60 + 9
    243: "4:3",    # 4×60  + 3
    # String keys — pre-stringified or already-canonical variants
    "969":  "16:9",
    "243":  "4:3",
    "16:9":  "16:9",
    "4:3":   "4:3",
    "16:10": "16:10",
}

# Matches <!-- key: value --> anywhere in a slide
_DIRECTIVE_RE = re.compile(
    r'^\s*<!--\s*([\w-]+)\s*:\s*(.+?)\s*-->', re.MULTILINE
)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    If *text* starts with a YAML frontmatter block (--- … ---), parse it and
    return (meta_dict, remaining_markdown).

    Supported keys: title, author, date, theme, ratio, logo, custom_css.

    Returns ({}, text) when no frontmatter is found or PyYAML is missing.
    """
    if not _YAML_AVAILABLE:
        return {}, text

    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return {}, text

    rest = stripped[3:]
    end  = rest.find("\n---")
    if end == -1:
        return {}, text

    yaml_block = rest[:end].strip()
    remaining  = rest[end + 4:].lstrip("\n")

    try:
        meta = yaml.safe_load(yaml_block) or {}
    except yaml.YAMLError:
        meta = {}

    meta = _normalise(meta)
    return meta, remaining


def extract_slide_directives(slide_md: str) -> dict[str, str]:
    """
    Extract HTML comment directives from a slide's Markdown.

    Returns a dict of { key: value } for every <!-- key: value --> comment
    found anywhere in the slide.

    Currently recognised keys:
        theme   — "dark" or "light" (per-slide appearance override)
    """
    return {
        m.group(1).lower(): m.group(2).strip()
        for m in _DIRECTIVE_RE.finditer(slide_md)
    }


def _normalise(meta: dict) -> dict:
    if "ratio" in meta:
        raw = meta["ratio"]
        meta["ratio"] = _RATIO_COERCE.get(raw, str(raw))
    for key in ("theme", "logo", "custom_css"):
        if key in meta:
            meta[key] = str(meta[key])
    return meta


def raw_frontmatter(text: str) -> str:
    """
    Return the raw frontmatter block (including delimiters) from *text*,
    or an empty string if no frontmatter is present.
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return ""
    rest = stripped[3:]
    end  = rest.find("\n---")
    if end == -1:
        return ""
    return stripped[:end + 7].rstrip()
