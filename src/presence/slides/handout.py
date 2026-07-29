"""
handout.py — Render the talk as a document rather than as a deck.

A deck is what the room sees; the handout is what someone reads afterwards,
or instead. Each slide appears as a picture with the script written beneath
it, so the speaker notes stop being an appendix to the slides and become the
text the slides illustrate.

Deliberately not themed. A theme is display typography for a 1280x720 box
seen from the back of a room; this is a page held at arm's length, and
borrowing the deck's fonts and colours would make it harder to read, not
more of a piece.

Public API
----------
build_handout_html(slide_info, slide_pngs, meta) -> str
    A complete, self-contained HTML document (images inlined as data URIs),
    ready for WeasyPrint or a browser.
"""
from __future__ import annotations

import base64
import html as _html

from .renderer import render_slide_content

__all__ = ["build_handout_html"]


_CSS = """
@page {
    size: A4;
    margin: 22mm 20mm 20mm 20mm;
    @bottom-center {
        content: counter(page);
        font-family: sans-serif;
        font-size: 8pt;
        color: #999;
    }
}

* { box-sizing: border-box; }

body {
    margin: 0;
    font-family: Georgia, "Times New Roman", serif;
    font-size: 11pt;
    line-height: 1.55;
    color: #1a1a1a;
}

/* ── Masthead ─────────────────────────────────────────────────────────── */

.masthead {
    margin-bottom: 12mm;
    padding-bottom: 4mm;
    border-bottom: 1px solid #1a1a1a;
}

.masthead h1 {
    margin: 0;
    font-size: 20pt;
    line-height: 1.2;
    font-weight: 600;
}

.masthead .byline {
    margin: 2mm 0 0;
    font-family: sans-serif;
    font-size: 9pt;
    letter-spacing: 0.04em;
    color: #666;
}

/* ── One slide and its script ─────────────────────────────────────────── */

/* Keep a slide with the words that go with it: splitting the two across a
   page break is the one thing that would make the handout useless. */
.entry {
    break-inside: avoid;
    page-break-inside: avoid;
    margin-bottom: 11mm;
}

/* Not full width.  A slide at the full measure is tall enough that only one
   entry clears a page, which wastes half of every sheet; at 78% two fit and
   the slide's own body text still lands near 9pt on paper. */
.entry img {
    display: block;
    width: 78%;
    border: 1px solid #d8d8d8;
}

.entry .label {
    margin-top: 2.5mm;
    font-family: sans-serif;
    font-size: 8pt;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #8a8a8a;
}

.entry .script {
    margin-top: 2mm;
}

.entry .script > *:first-child { margin-top: 0; }
.entry .script > *:last-child  { margin-bottom: 0; }

.entry .script p  { margin: 0 0 3mm; }
.entry .script ul,
.entry .script ol { margin: 0 0 3mm; padding-left: 6mm; }
.entry .script li { margin-bottom: 1mm; }

.entry .script h1,
.entry .script h2,
.entry .script h3 {
    font-size: 11pt;
    margin: 0 0 2mm;
}

.entry .script code {
    font-family: monospace;
    font-size: 9.5pt;
    background: #f2f2f2;
    padding: 0 0.5mm;
}

.entry .script pre {
    background: #f6f6f6;
    padding: 3mm;
    overflow-x: auto;
    font-size: 9pt;
}

.entry .script blockquote {
    margin: 0 0 3mm;
    padding-left: 4mm;
    border-left: 2px solid #ddd;
    color: #555;
}

/* A slide nobody wrote a script for still belongs in the handout — the
   picture is the record. Saying "no notes" would only add noise. */
.entry.silent .label { margin-bottom: 0; }
"""


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _masthead(meta: dict) -> str:
    title  = str(meta.get("title", "") or "").strip()
    author = str(meta.get("author", "") or "").strip()
    date   = str(meta.get("date", "") or "").strip()

    if not (title or author or date):
        return ""

    byline = " · ".join(p for p in (author, date) if p)
    parts = ['<header class="masthead">']
    if title:
        parts.append(f"<h1>{_html.escape(title)}</h1>")
    if byline:
        parts.append(f'<p class="byline">{_html.escape(byline)}</p>')
    parts.append("</header>")
    return "".join(parts)


def build_handout_html(
    slide_info: list[dict],
    slide_pngs: "list[bytes | None]",
    meta:       dict | None = None,
) -> str:
    """
    Build a self-contained handout document.

    *slide_info* is the list the converter produces — each entry needs at
    least ``title`` and ``notes``.  *slide_pngs* holds one rendered PNG per
    slide in the same order; a missing image simply leaves that entry as
    script alone, which is better than dropping the slide.

    Images are inlined as data URIs so the result is one file that survives
    being emailed.
    """
    meta = meta or {}
    entries: list[str] = []

    for i, info in enumerate(slide_info):
        png = slide_pngs[i] if i < len(slide_pngs) else None
        notes = (info.get("notes") or "").strip()
        title = (info.get("title") or "").strip()

        label = f"Slide {i + 1}"
        if title and title != label:
            label = f"{label} · {title}"

        parts = [f'<section class="entry{"" if notes else " silent"}">']
        if png:
            alt = _html.escape(title or label)
            parts.append(f'<img src="{_data_uri(png)}" alt="{alt}">')
        parts.append(f'<p class="label">{_html.escape(label)}</p>')
        if notes:
            parts.append(f'<div class="script">{render_slide_content(notes)}</div>')
        parts.append("</section>")
        entries.append("".join(parts))

    lang = _html.escape(str(meta.get("lang", "en")) or "en")
    title = _html.escape(str(meta.get("title", "") or "Handout"))
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>{_CSS}</style>
</head>
<body>
{_masthead(meta)}
{''.join(entries)}
</body>
</html>"""
