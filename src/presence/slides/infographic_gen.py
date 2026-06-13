"""
slides/infographic_gen.py — Claude-powered SVG infographic generation (no GTK dependency).

Provides type constants and an async function for generating structured SVG
infographics from slide Markdown content via the Claude API.
"""
from __future__ import annotations

import logging
import os
import re
import uuid

log = logging.getLogger(__name__)

INFOGRAPHIC_TYPES: list[str] = [
    "Timeline",
    "Process Flow",
    "Comparison Table",
    "Stats Highlight",
    "Bar Chart",
    "Donut Chart",
    "Org / Hierarchy Chart",
]

_TYPE_HINTS: dict[str, str] = {
    "Timeline": (
        "a horizontal timeline with evenly-spaced event nodes connected by a line; "
        "each node has a date/label above and a brief description below"
    ),
    "Process Flow": (
        "a left-to-right flow diagram with numbered step boxes connected by arrows; "
        "each box has a short title and one-line description"
    ),
    "Comparison Table": (
        "a clean comparison grid with a header row and alternating-shade rows; "
        "at least two columns with a clear label column on the left"
    ),
    "Stats Highlight": (
        "2–4 large bold stat cards arranged in a row; each card shows a prominent "
        "number/value and a short label beneath it"
    ),
    "Bar Chart": (
        "a horizontal bar chart with labelled bars and a value axis; "
        "bars use the accent colour with varying lightness for distinction"
    ),
    "Donut Chart": (
        "a donut/ring chart with labelled segments in a legend; "
        "a short summary text or total sits in the centre ring"
    ),
    "Org / Hierarchy Chart": (
        "a top-down hierarchy tree with rectangular boxes connected by vertical lines; "
        "root node at the top, leaf nodes at the bottom"
    ),
}

DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

# Model menu shown in the infographic dialog and settings.
# Each entry: (display label, model ID, provider)
INFOGRAPHIC_MODEL_LABELS:    list[str] = ["Claude Sonnet", "Gemini 2.5 Flash", "Gemini 2.5 Pro"]
INFOGRAPHIC_MODEL_IDS:       list[str] = ["claude-sonnet-4-6", "gemini-2.5-flash", "gemini-2.5-pro"]
INFOGRAPHIC_MODEL_PROVIDERS: list[str] = ["claude", "gemini", "gemini"]


def build_claude_prompt(
    slide_md: str,
    infographic_type: str,
    accent: str = "#E17000",
    accent2: str = "",
    fg: str = "#1a1a2e",
    body_font: str = "sans-serif",
    heading_font: str = "",
) -> str:
    type_hint = _TYPE_HINTS.get(infographic_type, "a clean structured infographic")
    _accent2 = accent2 or accent
    _heading_font = heading_font or body_font
    return f"""You are an expert data-visualisation designer. Produce a valid SVG infographic from the slide content below.

INFOGRAPHIC TYPE: {infographic_type}
VISUAL STRUCTURE: {type_hint}
PRIMARY ACCENT: {accent}
SECONDARY ACCENT: {_accent2}
TEXT COLOUR: {fg}
BODY FONT: {body_font}
HEADING FONT: {_heading_font}
CANVAS: 800 × 450 px — leave at least 24 px padding on all four edges.

SLIDE CONTENT:
{slide_md}

STRICT OUTPUT RULES — follow exactly:
1. Output ONLY the SVG element — no markdown fences, no XML declaration, no explanation.
2. First line must be: <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 450" width="800" height="450">
3. Last line must be: </svg>
4. Background: transparent — do NOT add a background <rect>; the slide theme shows through.
5. Fonts: use font-family="{_heading_font}" for headings/titles and font-family="{body_font}" for body text; no @font-face in <defs>.
6. No <image> elements, no <use xlink:href>, no external references of any kind.
7. Use {accent} for primary headings, key shapes, and highlights; use {_accent2} for secondary elements, alternating chart bars, or chart segments; derive lighter tints from either by adding opacity.
8. Body text colour: {fg}; text on filled shapes: #ffffff or a high-contrast equivalent.
9. Keep it clean — no decorative noise; maximise information-to-ink ratio.
10. Represent ONLY information present in the slide content — never invent data or numbers.

TEXT LAYOUT — SVG text does NOT wrap automatically. You MUST follow these rules to prevent overlapping:
- Use font-size 13–15 for body labels; 16–20 for sub-headings; 22–28 for titles. Never larger.
- Truncate any label that would exceed its allocated column/cell width. Abbreviate ruthlessly.
- Each label must fit on ONE line unless you explicitly split it with two <text> elements at different y values, each 18–20 px apart.
- Compute y positions arithmetically: if the first item starts at y=80 and each row is 48 px tall, then item N starts at y = 80 + N*48. Never place two text elements within 16 px of each other vertically unless they are intentionally stacked (title + subtitle pair).
- If there are more than 6 items to show, reduce font-size to 12 and row height to 38 px, or omit the least important items.
- For horizontal layouts (timelines, process flows), allocate equal x-width per node = 800 / N nodes, and keep each label within that width.
- Test: mentally walk through every <text> y coordinate and confirm no two independent labels share the same or adjacent y value in the same x column."""


def extract_svg(response_text: str) -> str:
    """Strip any markdown fences and return just the SVG element."""
    text = response_text.strip()
    # Strip ```svg ... ``` or ``` ... ``` fences
    text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()
    # Ensure it starts with <svg
    if not text.startswith("<svg"):
        m = re.search(r'<svg[\s>]', text)
        if m:
            text = text[m.start():]
    return text


def generate_infographic(
    claude_key: str,
    slide_md: str,
    infographic_type: str,
    output_path: str,
    accent: str = "#E17000",
    claude_model: str = DEFAULT_CLAUDE_MODEL,
    accent2: str = "",
    fg: str = "#1a1a2e",
    body_font: str = "sans-serif",
    heading_font: str = "",
) -> str:
    """
    Generate an SVG infographic from slide Markdown via the Claude API.

    Writes the SVG to *output_path* and returns the path.
    Raises on API errors or if Claude produces no usable SVG.
    """
    try:
        import anthropic
    except Exception as exc:
        raise ImportError(
            f"anthropic package not installed or failed to import: {exc}"
        ) from exc

    prompt = build_claude_prompt(
        slide_md, infographic_type, accent, accent2, fg, body_font, heading_font,
    )
    client = anthropic.Anthropic(api_key=claude_key, timeout=60.0)
    message = client.messages.create(
        model=claude_model,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text
    svg = extract_svg(raw)

    if not svg.startswith("<svg"):
        raise ValueError(
            f"Claude did not return a valid SVG element for type '{infographic_type}'"
        )

    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(svg)

    log.debug("Infographic written → %s (%d bytes)", output_path, len(svg))
    return output_path


def generate_infographic_gemini(
    gemini_key: str,
    slide_md: str,
    infographic_type: str,
    output_path: str,
    accent: str = "#E17000",
    gemini_model: str = "gemini-2.5-flash",
    accent2: str = "",
    fg: str = "#1a1a2e",
    body_font: str = "sans-serif",
    heading_font: str = "",
) -> str:
    """
    Generate an SVG infographic from slide Markdown via the Gemini text API.

    Writes the SVG to *output_path* and returns the path.
    Raises on API errors or if Gemini produces no usable SVG.
    """
    try:
        from google import genai
    except Exception as exc:
        raise ImportError(
            f"google-genai not installed or failed to import: {exc}"
        ) from exc

    prompt = build_claude_prompt(
        slide_md, infographic_type, accent, accent2, fg, body_font, heading_font,
    )
    client = genai.Client(api_key=gemini_key)
    response = client.models.generate_content(model=gemini_model, contents=prompt)
    raw = response.text or ""
    svg = extract_svg(raw)

    if not svg.startswith("<svg"):
        raise ValueError(
            f"Gemini did not return a valid SVG element for type '{infographic_type}'"
        )

    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(svg)

    log.debug("Infographic written → %s (%d bytes)", output_path, len(svg))
    return output_path


def generate_single_infographic(
    api_key: str,
    slide_md: str,
    infographic_type: str,
    output_dir: str,
    theme_info: dict | None = None,
    model_id: str = DEFAULT_CLAUDE_MODEL,
    provider: str = "claude",
) -> str:
    """
    Generate one infographic with a UUID-based filename.

    Dispatches to Claude or Gemini based on *provider* ("claude" | "gemini").
    *theme_info* carries resolved theme colours and fonts:
    ``{"accent", "accent2", "fg", "body_font", "heading_font"}``.
    Returns the full path to the saved SVG.
    Used by AIInfographicDialog.
    """
    ti           = theme_info or {}
    accent       = ti.get("accent",       "#E17000")
    accent2      = ti.get("accent2",      "")
    fg           = ti.get("fg",           "#1a1a2e")
    body_font    = ti.get("body_font",    "sans-serif")
    heading_font = ti.get("heading_font", "")

    filename = f"infographic_{uuid.uuid4().hex[:12]}.svg"
    output_path = os.path.join(output_dir, filename)
    if provider == "gemini":
        return generate_infographic_gemini(
            api_key, slide_md, infographic_type, output_path,
            accent, model_id, accent2, fg, body_font, heading_font,
        )
    return generate_infographic(
        api_key, slide_md, infographic_type, output_path,
        accent, model_id, accent2, fg, body_font, heading_font,
    )
