"""
thumbnails.py — Generate a standalone HTML slide thumbnail index.

build_thumbnail_index() produces a *_index.html file alongside the PDF.
Each card shows the slide number, title, and speaker notes (if any).
This file has no dependency on weasyprint or any PDF machinery.
"""

import html as _html
from pathlib import Path

from .css import _safe_colour   # reuse the colour allowlist validator


def build_thumbnail_index(
    slide_info:  list[dict],
    output_path: Path,
    theme,
    meta:        dict,
) -> Path:
    """
    Write a *_index.html overview page and return its path.

    *slide_info* is the list of {'title', 'notes'} dicts produced by
    html.md_to_html_slides().

    *theme* may be a Theme dataclass instance or a legacy dict.

    Colour values from the theme are validated before CSS interpolation to
    prevent injection when the theme originates from an untrusted package.
    _index.html is a standalone file opened in the user's default browser
    (with JavaScript enabled), making CSS injection more dangerous here
    than in the slide HTML, which only ever reaches WeasyPrint.
    """
    deck_title = _html.escape(str(meta.get("title", output_path.stem)))
    # Accept both Theme dataclass and legacy dict
    if hasattr(theme, "accent"):
        accent   = _safe_colour(theme.accent,   "#ba5d00")
        bg       = _safe_colour(theme.bg,       "#ffffff")
        fg       = _safe_colour(theme.fg,       "#1a1a2e")
        title_bg = _safe_colour(theme.title_bg, "#1a1a2e")
    else:
        accent   = _safe_colour(theme["accent"],   "#ba5d00")
        bg       = _safe_colour(theme["bg"],       "#ffffff")
        fg       = _safe_colour(theme["fg"],       "#1a1a2e")
        title_bg = _safe_colour(theme["title_bg"], "#1a1a2e")

    subtitle_parts = [f"{len(slide_info)} slides"]
    if meta.get("author"):
        subtitle_parts.append(_html.escape(str(meta["author"])))
    subtitle = "  ·  ".join(subtitle_parts)

    cards_html = "".join(
        _card(i + 1, info, fg, accent) for i, info in enumerate(slide_info)
    )

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{deck_title} — Slide Index</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'IBM Plex Sans', 'Liberation Sans', Arial, sans-serif;
    background: {title_bg};
    color: {fg};
    padding: 40px;
  }}
  h1 {{
    font-size: 28px;
    color: {accent};
    margin-bottom: 8px;
    letter-spacing: -0.02em;
  }}
  .subtitle {{
    font-size: 14px;
    color: {fg};
    opacity: 0.5;
    margin-bottom: 32px;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 16px;
  }}
  .card {{
    background: {bg};
    border-radius: 8px;
    padding: 16px 20px;
    border-left: 4px solid {accent};
    border-top: 0.5px solid {accent}44;
  }}
  .num {{
    font-size: 11px;
    color: {accent};
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 6px;
  }}
  .title {{
    font-size: 15px;
    color: {fg};
    font-weight: 600;
    line-height: 1.3;
    margin-bottom: 8px;
  }}
  .notes {{
    font-size: 12px;
    color: {fg};
    opacity: 0.55;
    line-height: 1.5;
    border-top: 0.5px solid {accent}33;
    padding-top: 8px;
    margin-top: 4px;
    font-style: italic;
  }}
</style>
</head>
<body>
  <h1>{deck_title}</h1>
  <p class="subtitle">{subtitle}</p>
  <div class="grid">
    {cards_html}
  </div>
</body>
</html>"""

    index_path = output_path.with_name(output_path.stem + "_index.html")
    index_path.write_text(index_html, encoding="utf-8")
    return index_path


def _card(number: int, info: dict, fg: str, accent: str) -> str:
    # Escape user-authored content before inserting into HTML
    safe_title = _html.escape(info["title"])
    notes_html = (
        f'<p class="notes">{_html.escape(info["notes"])}</p>'
        if info["notes"] else ""
    )
    return (
        f'<div class="card">'
        f'<div class="num">Slide {number}</div>'
        f'<div class="title">{safe_title}</div>'
        f'{notes_html}'
        f'</div>'
    )
