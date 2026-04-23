"""
css.py — Generate the CSS stylesheet for a slide deck.

build_css() is the single public function.  It accepts a Theme object
(or a legacy dict for backward compatibility) and returns a complete CSS
string ready to be embedded in the HTML document.
"""

import logging
import re
from pathlib import Path

from .themes import Theme
from .renderer import get_pygments_css

log = logging.getLogger(__name__)

# ── Colour validation ─────────────────────────────────────────────────────────

# Allowlist for CSS colour values sourced from theme.json.
# Accepts: #rgb, #rrggbb, #rrggbbaa, named colours (letters only),
# rgb(...) and rgba(...).  Anything else is rejected and replaced with
# a safe fallback to prevent CSS injection via a malicious theme package.
_CSS_COLOUR_RE = re.compile(
    r"^\s*("
    r"#[0-9a-fA-F]{3,8}"                          # hex: #rgb #rrggbb #rrggbbaa
    r"|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)"      # rgb(r,g,b)
    r"|rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,"      # rgba(r,g,b,a)
    r"\s*(?:0|1|0?\.\d+)\s*\)"
    r"|[a-zA-Z]{2,30}"                             # named colours: red, transparent…
    r")\s*$"
)


def _safe_colour(value: str, fallback: str = "#000000") -> str:
    """
    Return *value* if it is a valid CSS colour literal, otherwise *fallback*.

    Prevents CSS injection when colour values originate from an untrusted
    theme.json (e.g. ``"bg": "red; } body { display:none; } .x {"``).
    """
    if _CSS_COLOUR_RE.match(value):
        return value
    log.warning("Theme colour value rejected (CSS injection attempt?): %r", value)
    return fallback


def build_css(theme, width: int, height: int, logo_b64: str | None) -> str:
    """
    Build the complete CSS string for a slide deck.

    Assembles sections in order: variables, base, typography, content,
    code, image-layout, text-alignment, two-image, font-face, pygments,
    logo, callouts, then appends any per-theme custom CSS file.

    *theme* may be a Theme dataclass instance or a legacy dict.
    """
    t = _resolve(theme)

    font_face   = t._font_face_css or ""
    pygments    = get_pygments_css(t.pygments_style)
    logo_css    = _logo_css(height, width) if logo_b64 else ""

    # Icons come from the theme instance (fixes #99 — now per-theme)
    icons = t.callout_icons
    callouts = "\n".join(
        _callout_css(kind, t, width, height, icons[kind])
        for kind in icons
    )

    heading_font = t.resolved_heading_font
    body_font    = t.body_font
    mono_font    = t.mono_font
    heading_col  = t.resolved_heading_color
    title_accent = t.resolved_title_accent

    font_scale = height / 720
    body_px    = int(t.base_size * font_scale)

    css = f"""
{font_face}

:root {{
    --p-bg:           {t.bg};
    --p-fg:           {t.fg};
    --p-accent:       {t.accent};
    --p-accent2:      {t.resolved_accent2};
    --p-heading:      {heading_col};
    --p-code-bg:      {t.code_bg};
    --p-title-bg:     {t.title_bg};
    --p-title-fg:     {t.title_fg};
    --p-title-accent: {title_accent};
    --p-body-font:    {body_font};
    --p-heading-font: {heading_font};
    --p-mono-font:    {mono_font};
}}

@page {{
    size: {width}px {height}px;
    margin: 0;
}}

* {{
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}}

body {{
    font-family: var(--p-body-font);
    background: var(--p-bg);
    color: var(--p-fg);
}}

/* ── Base slide ────────────────────────────────────────────────────────── */

.slide {{
    width: {width}px;
    height: {height}px;
    padding: {int(height * 0.08)}px {int(width * 0.08)}px;
    padding-bottom: {int(height * 0.11)}px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    background: var(--p-bg);
    page-break-after: always;
    overflow: hidden;
    position: relative;
}}

.slide:last-child {{
    page-break-after: auto;
}}

.slide::before {{
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: {int(width * 0.007)}px;
    background: var(--p-accent);
}}

/* ── Progress bar ──────────────────────────────────────────────────────── */

.progress-bar-track {{
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    height: {int(height * 0.008)}px;
    background: {t.accent}33;
}}

.progress-bar-fill {{
    height: 100%;
    background: var(--p-accent);
}}

/* ── Logo watermark ────────────────────────────────────────────────────── */

{logo_css}

/* ── Title slide ───────────────────────────────────────────────────────── */

.slide.title-slide {{
    background: var(--p-title-bg);
    color: var(--p-title-fg);
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: {int(height * 0.1)}px {int(width * 0.12)}px;
}}

.slide.title-slide::before {{
    left: 0;
    right: 0;
    top: auto;
    bottom: 0;
    width: 100%;
    height: {int(height * 0.012)}px;
    background: var(--p-title-accent);
}}

.slide.title-slide::after {{
    content: '';
    position: absolute;
    left: 0;
    right: 0;
    top: 0;
    width: 100%;
    height: {int(height * 0.006)}px;
    background: var(--p-title-accent);
    opacity: 0.4;
}}

.slide.title-slide h1 {{
    font-family: var(--p-heading-font);
    font-size: {int(height * 0.11)}px;
    color: var(--p-title-accent);
    margin-bottom: {int(height * 0.04)}px;
    line-height: 1.05;
    letter-spacing: -0.03em;
}}

.slide.title-slide p,
.slide.title-slide h2,
.slide.title-slide h3 {{
    color: var(--p-title-fg);
    opacity: 0.75;
    font-size: {int(height * 0.038)}px;
    font-weight: 400;
    margin-bottom: {int(height * 0.015)}px;
}}

.slide.title-slide .slide-number,
.slide.title-slide .progress-bar-track {{
    display: none;
}}

.slide.title-slide .title-meta {{
    margin-top: {int(height * 0.05)}px;
    font-size: {int(height * 0.028)}px;
    opacity: 0.5;
    color: var(--p-title-fg);
}}

/* ── Per-slide appearance override ─────────────────────────────────────── */

.slide[data-p-theme="dark"] {{
    background: {t.title_bg};
    color: {t.title_fg};
}}

.slide[data-p-theme="dark"]::before {{
    background: var(--p-accent);
}}

.slide[data-p-theme="light"] {{
    background: {t.bg};
    color: {t.fg};
}}

/* ── Typography ────────────────────────────────────────────────────────── */

h1 {{
    font-family: var(--p-heading-font);
    font-size: {int(height * 0.075)}px;
    color: var(--p-heading);
    margin-bottom: {int(height * 0.03)}px;
    line-height: 1.1;
    font-weight: 700;
    letter-spacing: -0.02em;
}}

h2 {{
    font-family: var(--p-heading-font);
    font-size: {int(height * 0.055)}px;
    color: var(--p-heading);
    margin-bottom: {int(height * 0.025)}px;
    line-height: 1.2;
    font-weight: 600;
}}

h3 {{
    font-family: var(--p-heading-font);
    font-size: {int(height * 0.042)}px;
    color: var(--p-heading);
    margin-bottom: {int(height * 0.02)}px;
}}

p {{
    font-size: {body_px}px;
    line-height: 1.6;
    margin-bottom: {int(height * 0.02)}px;
}}

ul, ol {{
    font-size: {int(body_px * 0.94)}px;
    padding-left: {int(width * 0.04)}px;
    margin-bottom: {int(height * 0.02)}px;
    line-height: 1.7;
}}

li {{
    margin-bottom: {int(height * 0.008)}px;
}}

/* ── Two-column layout ─────────────────────────────────────────────────── */

.two-col {{
    display: flex;
    align-items: flex-start;
    width: 100%;
}}

.two-col > .col {{
    flex: 1;
    min-width: 0;
}}

.two-col > .col:first-child {{
    margin-right: {int(width * 0.04)}px;
}}

/* ── Code ──────────────────────────────────────────────────────────────── */

code {{
    font-family: var(--p-mono-font);
    font-size: 0.9em;
    background: var(--p-code-bg);
    padding: 0.1em 0.4em;
    border-radius: 3px;
}}

pre {{
    background: var(--p-code-bg);
    padding: {int(height * 0.025)}px {int(width * 0.03)}px;
    border-radius: 6px;
    overflow: hidden;
    margin: {int(height * 0.015)}px 0;
    border-left: {int(width * 0.005)}px solid var(--p-accent);
}}

pre code {{
    font-size: {int(height * 0.028)}px;
    background: none;
    padding: 0;
    line-height: 1.5;
    display: block;
}}

.highlight {{
    background: var(--p-code-bg);
    padding: {int(height * 0.025)}px {int(width * 0.03)}px;
    border-radius: 6px;
    overflow: hidden;
    margin: {int(height * 0.015)}px 0;
    border-left: {int(width * 0.005)}px solid var(--p-accent);
}}

.highlight pre {{
    background: none;
    padding: 0;
    border: none;
    margin: 0;
    border-radius: 0;
}}

.highlight pre code,
.highlight code {{
    font-size: {int(height * 0.028)}px;
    background: none;
    padding: 0;
    line-height: 1.5;
    display: block;
    border-radius: 0;
}}

{pygments}

/* ── Callout boxes ─────────────────────────────────────────────────────── */

{callouts}

/* ── Misc ──────────────────────────────────────────────────────────────── */

blockquote {{
    border-left: {int(width * 0.006)}px solid var(--p-accent);
    padding-left: {int(width * 0.03)}px;
    font-style: italic;
    opacity: 0.85;
    margin: {int(height * 0.015)}px 0;
}}

table {{
    border-collapse: collapse;
    width: 100%;
    font-size: {int(height * 0.030)}px;
    margin: {int(height * 0.015)}px 0;
}}

th {{
    background: var(--p-accent);
    color: var(--p-bg);
    padding: {int(height * 0.012)}px {int(width * 0.02)}px;
    text-align: left;
}}

td {{
    padding: {int(height * 0.01)}px {int(width * 0.02)}px;
    border-bottom: 1px solid {t.accent}44;
}}

tr:nth-child(even) td {{
    background: var(--p-code-bg);
}}

strong {{
    color: var(--p-accent);
    font-weight: 700;
}}

a {{
    color: var(--p-accent);
    text-decoration: none;
    border-bottom: 1px dashed var(--p-accent);
}}

.slide-number {{
    position: absolute;
    bottom: {int(height * 0.025)}px;
    right: {int(width * 0.04)}px;
    font-size: {int(height * 0.022)}px;
    opacity: 0.4;
}}

/* ── Image slide layout ────────────────────────────────────────────────── */
/*
 * Layout is driven by data attributes on .slide.has-image:
 *   data-img-pos  : left | right | top | bottom
 *   data-img-size : 30 | 50 | 70  (percent of slide width or height)
 *
 * The gradient is a real <div class="slide-image-gradient"> — NOT a ::after
 * pseudo-element — because WeasyPrint supports background-image gradients
 * on regular elements (as PDF Patterns) but silently drops them on ::after.
 *
 * All gradient colour values are literal (no var()) so WeasyPrint can resolve
 * them without CSS custom-property support.
 */

/* ── Image panel ── */

.slide.has-image .slide-image {{
    position: absolute;
    overflow: hidden;
    z-index: 0;
}}

.slide.has-image .slide-image img {{
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 100%;
    object-fit: cover;
    object-position: center;
    display: block;
    opacity: 0.75;
}}

/* ── Gradient overlay div ── */

.slide-image-gradient {{
    position: absolute;
    z-index: 1;
    pointer-events: none;
}}

/* ── RIGHT ── */

.slide.has-image[data-img-pos="right"][data-img-size="30"] .slide-image {{ top:0; right:0; bottom:0; width:30%; height:100%; }}
.slide.has-image[data-img-pos="right"][data-img-size="50"] .slide-image {{ top:0; right:0; bottom:0; width:50%; height:100%; }}
.slide.has-image[data-img-pos="right"][data-img-size="70"] .slide-image {{ top:0; right:0; bottom:0; width:70%; height:100%; }}

.slide.has-image[data-img-pos="right"][data-img-size="30"] .slide-text {{ padding-right: 30%; }}
.slide.has-image[data-img-pos="right"][data-img-size="50"] .slide-text {{ padding-right: 50%; }}
.slide.has-image[data-img-pos="right"][data-img-size="70"] .slide-text {{ padding-right: 70%; }}

/* Gradient covers the full image panel and fades from bg (left) → transparent (right) */
.slide.has-image[data-img-pos="right"][data-img-size="30"] .slide-image-gradient {{ top:0; right:0; bottom:0; width:30%; background-image: linear-gradient(to right, {t.bg} 0%, transparent 60%); }}
.slide.has-image[data-img-pos="right"][data-img-size="50"] .slide-image-gradient {{ top:0; right:0; bottom:0; width:50%; background-image: linear-gradient(to right, {t.bg} 0%, transparent 60%); }}
.slide.has-image[data-img-pos="right"][data-img-size="70"] .slide-image-gradient {{ top:0; right:0; bottom:0; width:70%; background-image: linear-gradient(to right, {t.bg} 0%, transparent 60%); }}

/* ── LEFT ── */

.slide.has-image[data-img-pos="left"][data-img-size="30"] .slide-image {{ top:0; left:0; bottom:0; width:30%; height:100%; }}
.slide.has-image[data-img-pos="left"][data-img-size="50"] .slide-image {{ top:0; left:0; bottom:0; width:50%; height:100%; }}
.slide.has-image[data-img-pos="left"][data-img-size="70"] .slide-image {{ top:0; left:0; bottom:0; width:70%; height:100%; }}

.slide.has-image[data-img-pos="left"][data-img-size="30"] .slide-text {{ padding-left: 30%; }}
.slide.has-image[data-img-pos="left"][data-img-size="50"] .slide-text {{ padding-left: 50%; }}
.slide.has-image[data-img-pos="left"][data-img-size="70"] .slide-text {{ padding-left: 70%; }}

.slide.has-image[data-img-pos="left"][data-img-size="30"] .slide-image-gradient {{ top:0; left:0; bottom:0; width:30%; background-image: linear-gradient(to left, {t.bg} 0%, transparent 60%); }}
.slide.has-image[data-img-pos="left"][data-img-size="50"] .slide-image-gradient {{ top:0; left:0; bottom:0; width:50%; background-image: linear-gradient(to left, {t.bg} 0%, transparent 60%); }}
.slide.has-image[data-img-pos="left"][data-img-size="70"] .slide-image-gradient {{ top:0; left:0; bottom:0; width:70%; background-image: linear-gradient(to left, {t.bg} 0%, transparent 60%); }}

/* ── TOP ── */

.slide.has-image[data-img-pos="top"][data-img-size="30"] .slide-image {{ top:0; left:0; right:0; width:100%; height:30%; }}
.slide.has-image[data-img-pos="top"][data-img-size="50"] .slide-image {{ top:0; left:0; right:0; width:100%; height:50%; }}
.slide.has-image[data-img-pos="top"][data-img-size="70"] .slide-image {{ top:0; left:0; right:0; width:100%; height:70%; }}

.slide.has-image[data-img-pos="top"][data-img-size="30"] .slide-text {{ padding-top: 30%; }}
.slide.has-image[data-img-pos="top"][data-img-size="50"] .slide-text {{ padding-top: 50%; }}
.slide.has-image[data-img-pos="top"][data-img-size="70"] .slide-text {{ padding-top: 70%; }}

.slide.has-image[data-img-pos="top"][data-img-size="30"] .slide-image-gradient {{ top:0; left:0; right:0; height:30%; background-image: linear-gradient(to top, {t.bg} 0%, transparent 60%); }}
.slide.has-image[data-img-pos="top"][data-img-size="50"] .slide-image-gradient {{ top:0; left:0; right:0; height:50%; background-image: linear-gradient(to top, {t.bg} 0%, transparent 60%); }}
.slide.has-image[data-img-pos="top"][data-img-size="70"] .slide-image-gradient {{ top:0; left:0; right:0; height:70%; background-image: linear-gradient(to top, {t.bg} 0%, transparent 60%); }}

/* ── BOTTOM ── */

.slide.has-image[data-img-pos="bottom"][data-img-size="30"] .slide-image {{ bottom:0; left:0; right:0; width:100%; height:30%; }}
.slide.has-image[data-img-pos="bottom"][data-img-size="50"] .slide-image {{ bottom:0; left:0; right:0; width:100%; height:50%; }}
.slide.has-image[data-img-pos="bottom"][data-img-size="70"] .slide-image {{ bottom:0; left:0; right:0; width:100%; height:70%; }}

.slide.has-image[data-img-pos="bottom"][data-img-size="30"] .slide-text {{ padding-bottom: 30%; }}
.slide.has-image[data-img-pos="bottom"][data-img-size="50"] .slide-text {{ padding-bottom: 50%; }}
.slide.has-image[data-img-pos="bottom"][data-img-size="70"] .slide-text {{ padding-bottom: 70%; }}

.slide.has-image[data-img-pos="bottom"][data-img-size="30"] .slide-image-gradient {{ bottom:0; left:0; right:0; height:30%; background-image: linear-gradient(to bottom, {t.bg} 0%, transparent 60%); }}
.slide.has-image[data-img-pos="bottom"][data-img-size="50"] .slide-image-gradient {{ bottom:0; left:0; right:0; height:50%; background-image: linear-gradient(to bottom, {t.bg} 0%, transparent 60%); }}
.slide.has-image[data-img-pos="bottom"][data-img-size="70"] .slide-image-gradient {{ bottom:0; left:0; right:0; height:70%; background-image: linear-gradient(to bottom, {t.bg} 0%, transparent 60%); }}

/* ── Text container ── */

.slide.has-image .slide-text {{
    position: relative;
    z-index: 2;
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    justify-content: center;
    box-sizing: border-box;
}}


/* ── Image slide — responsive text alignment ──────────────────────────────── */
/*
 * Text aligns away from the image so the composition has visual logic:
 *
 *  left image  → text pulls to the right (text-align: right, flex-end)
 *  right image → text pulls to the left  (text-align: left,  flex-start)
 *  top image   → text sits at the top of its strip, away from the image
 *                (justify-content: flex-start so text doesn't float in the
 *                middle of a large empty bottom zone)
 *  bottom image → text sits at the bottom of its strip, close to the image
 *                (justify-content: flex-end)
 *
 * align-items controls the cross-axis (horizontal for a column flex container).
 * WeasyPrint's flex support is partial; text-align is the reliable fallback
 * and works for both the PDF and the WebKit preview.
 *
 * Headings, paragraphs and list items inherit text-align from their container
 * via the explicit rules below (some theme CSS sets text-align on h1/p directly
 * which would otherwise override the container's value).
 */

/* ── LEFT image: text aligns right ── */

.slide.has-image[data-img-pos="left"] .slide-text {{
    align-items: flex-end;
    text-align: right;
}}

.slide.has-image[data-img-pos="left"] .slide-text h1,
.slide.has-image[data-img-pos="left"] .slide-text h2,
.slide.has-image[data-img-pos="left"] .slide-text h3,
.slide.has-image[data-img-pos="left"] .slide-text h4,
.slide.has-image[data-img-pos="left"] .slide-text p,
.slide.has-image[data-img-pos="left"] .slide-text li,
.slide.has-image[data-img-pos="left"] .slide-text blockquote {{
    text-align: right;
}}

/* ── RIGHT image: text aligns left (default, stated explicitly for clarity) ── */

.slide.has-image[data-img-pos="right"] .slide-text {{
    align-items: flex-start;
    text-align: left;
}}

.slide.has-image[data-img-pos="right"] .slide-text h1,
.slide.has-image[data-img-pos="right"] .slide-text h2,
.slide.has-image[data-img-pos="right"] .slide-text h3,
.slide.has-image[data-img-pos="right"] .slide-text h4,
.slide.has-image[data-img-pos="right"] .slide-text p,
.slide.has-image[data-img-pos="right"] .slide-text li,
.slide.has-image[data-img-pos="right"] .slide-text blockquote {{
    text-align: left;
}}

/* ── TOP image: text anchors to the top of its zone, centred horizontally ── */

.slide.has-image[data-img-pos="top"] .slide-text {{
    justify-content: flex-start;
    align-items: center;
    text-align: center;
}}

.slide.has-image[data-img-pos="top"] .slide-text h1,
.slide.has-image[data-img-pos="top"] .slide-text h2,
.slide.has-image[data-img-pos="top"] .slide-text h3,
.slide.has-image[data-img-pos="top"] .slide-text h4,
.slide.has-image[data-img-pos="top"] .slide-text p,
.slide.has-image[data-img-pos="top"] .slide-text li,
.slide.has-image[data-img-pos="top"] .slide-text blockquote {{
    text-align: center;
}}

/* ── BOTTOM image: text anchors to the bottom of its zone, centred ── */

.slide.has-image[data-img-pos="bottom"] .slide-text {{
    justify-content: flex-end;
    align-items: center;
    text-align: center;
}}

.slide.has-image[data-img-pos="bottom"] .slide-text h1,
.slide.has-image[data-img-pos="bottom"] .slide-text h2,
.slide.has-image[data-img-pos="bottom"] .slide-text h3,
.slide.has-image[data-img-pos="bottom"] .slide-text h4,
.slide.has-image[data-img-pos="bottom"] .slide-text p,
.slide.has-image[data-img-pos="bottom"] .slide-text li,
.slide.has-image[data-img-pos="bottom"] .slide-text blockquote {{
    text-align: center;
}}

/* ── Two-image horizontal split: text centred in the middle strip ── */

.slide.has-two-images[data-split="h"] .slide-text {{
    align-items: center;
    text-align: center;
}}

.slide.has-two-images[data-split="h"] .slide-text h1,
.slide.has-two-images[data-split="h"] .slide-text h2,
.slide.has-two-images[data-split="h"] .slide-text h3,
.slide.has-two-images[data-split="h"] .slide-text p,
.slide.has-two-images[data-split="h"] .slide-text li {{
    text-align: center;
}}

/* ── Two-image vertical split: text centred in the middle band ── */

.slide.has-two-images[data-split="v"] .slide-text {{
    align-items: center;
    text-align: center;
}}

.slide.has-two-images[data-split="v"] .slide-text h1,
.slide.has-two-images[data-split="v"] .slide-text h2,
.slide.has-two-images[data-split="v"] .slide-text h3,
.slide.has-two-images[data-split="v"] .slide-text p,
.slide.has-two-images[data-split="v"] .slide-text li {{
    text-align: center;
}}

/* ── Two-image slide layout ────────────────────────────────────────────────── */
/*
 * .slide.has-two-images uses data-split="h" (horizontal: left+right) or
 * data-split="v" (vertical: top+bottom).
 *
 * data-size-a / data-size-b : 30 | 50 | 70  (each image's share)
 * data-grad-a / data-grad-b : 1 = gradient enabled, 0 = disabled
 *
 * All percentages are literal so WeasyPrint needs no CSS custom properties.
 * Gradient divs are emitted as real elements by html.py (WeasyPrint-safe).
 */

/* ── Shared: image panels absolutely positioned, text in normal flow ── */

.slide.has-two-images .slide-image-a,
.slide.has-two-images .slide-image-b {{
    position: absolute;
    overflow: hidden;
    z-index: 0;
}}

.slide.has-two-images .slide-image-a img,
.slide.has-two-images .slide-image-b img {{
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 100%;
    object-fit: cover;
    object-position: center;
    display: block;
    opacity: 0.75;
}}

.slide.has-two-images .slide-text {{
    position: relative;
    z-index: 2;
    display: flex;
    flex-direction: column;
    justify-content: center;
    box-sizing: border-box;
    height: 100%;
}}

/* ── HORIZONTAL split (data-split="h"): left image | text | right image ── */

/* Image A on the left */
.slide.has-two-images[data-split="h"][data-size-a="30"] .slide-image-a {{ top:0; left:0;  bottom:0; width:30%; height:100%; }}
.slide.has-two-images[data-split="h"][data-size-a="50"] .slide-image-a {{ top:0; left:0;  bottom:0; width:50%; height:100%; }}
.slide.has-two-images[data-split="h"][data-size-a="70"] .slide-image-a {{ top:0; left:0;  bottom:0; width:70%; height:100%; }}

/* Image B on the right */
.slide.has-two-images[data-split="h"][data-size-b="30"] .slide-image-b {{ top:0; right:0; bottom:0; width:30%; height:100%; }}
.slide.has-two-images[data-split="h"][data-size-b="50"] .slide-image-b {{ top:0; right:0; bottom:0; width:50%; height:100%; }}
.slide.has-two-images[data-split="h"][data-size-b="70"] .slide-image-b {{ top:0; right:0; bottom:0; width:70%; height:100%; }}

/* Text occupies the middle strip — padded away from both image panels */
.slide.has-two-images[data-split="h"][data-size-a="30"][data-size-b="30"] .slide-text {{ padding-left:30%; padding-right:30%; }}
.slide.has-two-images[data-split="h"][data-size-a="30"][data-size-b="50"] .slide-text {{ padding-left:30%; padding-right:50%; }}
.slide.has-two-images[data-split="h"][data-size-a="50"][data-size-b="30"] .slide-text {{ padding-left:50%; padding-right:30%; }}
.slide.has-two-images[data-split="h"][data-size-a="30"][data-size-b="70"] .slide-text {{ padding-left:30%; padding-right:70%; }}
.slide.has-two-images[data-split="h"][data-size-a="70"][data-size-b="30"] .slide-text {{ padding-left:70%; padding-right:30%; }}
.slide.has-two-images[data-split="h"][data-size-a="50"][data-size-b="50"] .slide-text {{ padding-left:50%; padding-right:50%; }}

/* ── VERTICAL split (data-split="v"): top image / text / bottom image ── */

/* Image A on the top */
.slide.has-two-images[data-split="v"][data-size-a="30"] .slide-image-a {{ top:0; left:0; right:0; width:100%; height:30%; }}
.slide.has-two-images[data-split="v"][data-size-a="50"] .slide-image-a {{ top:0; left:0; right:0; width:100%; height:50%; }}
.slide.has-two-images[data-split="v"][data-size-a="70"] .slide-image-a {{ top:0; left:0; right:0; width:100%; height:70%; }}

/* Image B on the bottom */
.slide.has-two-images[data-split="v"][data-size-b="30"] .slide-image-b {{ bottom:0; left:0; right:0; width:100%; height:30%; }}
.slide.has-two-images[data-split="v"][data-size-b="50"] .slide-image-b {{ bottom:0; left:0; right:0; width:100%; height:50%; }}
.slide.has-two-images[data-split="v"][data-size-b="70"] .slide-image-b {{ bottom:0; left:0; right:0; width:100%; height:70%; }}

/* Text in the middle strip */
.slide.has-two-images[data-split="v"][data-size-a="30"][data-size-b="30"] .slide-text {{ padding-top:30%; padding-bottom:30%; }}
.slide.has-two-images[data-split="v"][data-size-a="30"][data-size-b="50"] .slide-text {{ padding-top:30%; padding-bottom:50%; }}
.slide.has-two-images[data-split="v"][data-size-a="50"][data-size-b="30"] .slide-text {{ padding-top:50%; padding-bottom:30%; }}
.slide.has-two-images[data-split="v"][data-size-a="30"][data-size-b="70"] .slide-text {{ padding-top:30%; padding-bottom:70%; }}
.slide.has-two-images[data-split="v"][data-size-a="70"][data-size-b="30"] .slide-text {{ padding-top:70%; padding-bottom:30%; }}
.slide.has-two-images[data-split="v"][data-size-a="50"][data-size-b="50"] .slide-text {{ padding-top:50%; padding-bottom:50%; }}

/* ── Gradient overlay divs — emitted as real elements by html.py ── */
/* The gradient div for image-a fades inward (toward the text centre). */

.slide-image-a-gradient,
.slide-image-b-gradient {{
    position: absolute;
    z-index: 1;
    pointer-events: none;
}}

/* Horizontal: image-a is left, gradient fades right toward text */
.slide.has-two-images[data-split="h"][data-size-a="30"] .slide-image-a-gradient {{ top:0; left:0;  bottom:0; width:30%; background-image: linear-gradient(to left,  {t.bg} 0%, transparent 60%); }}
.slide.has-two-images[data-split="h"][data-size-a="50"] .slide-image-a-gradient {{ top:0; left:0;  bottom:0; width:50%; background-image: linear-gradient(to left,  {t.bg} 0%, transparent 60%); }}
.slide.has-two-images[data-split="h"][data-size-a="70"] .slide-image-a-gradient {{ top:0; left:0;  bottom:0; width:70%; background-image: linear-gradient(to left,  {t.bg} 0%, transparent 60%); }}

/* Horizontal: image-b is right, gradient fades left toward text */
.slide.has-two-images[data-split="h"][data-size-b="30"] .slide-image-b-gradient {{ top:0; right:0; bottom:0; width:30%; background-image: linear-gradient(to right, {t.bg} 0%, transparent 60%); }}
.slide.has-two-images[data-split="h"][data-size-b="50"] .slide-image-b-gradient {{ top:0; right:0; bottom:0; width:50%; background-image: linear-gradient(to right, {t.bg} 0%, transparent 60%); }}
.slide.has-two-images[data-split="h"][data-size-b="70"] .slide-image-b-gradient {{ top:0; right:0; bottom:0; width:70%; background-image: linear-gradient(to right, {t.bg} 0%, transparent 60%); }}

/* Vertical: image-a is top, gradient fades downward toward text */
.slide.has-two-images[data-split="v"][data-size-a="30"] .slide-image-a-gradient {{ top:0; left:0; right:0; height:30%; background-image: linear-gradient(to top,    {t.bg} 0%, transparent 60%); }}
.slide.has-two-images[data-split="v"][data-size-a="50"] .slide-image-a-gradient {{ top:0; left:0; right:0; height:50%; background-image: linear-gradient(to top,    {t.bg} 0%, transparent 60%); }}
.slide.has-two-images[data-split="v"][data-size-a="70"] .slide-image-a-gradient {{ top:0; left:0; right:0; height:70%; background-image: linear-gradient(to top,    {t.bg} 0%, transparent 60%); }}

/* Vertical: image-b is bottom, gradient fades upward toward text */
.slide.has-two-images[data-split="v"][data-size-b="30"] .slide-image-b-gradient {{ bottom:0; left:0; right:0; height:30%; background-image: linear-gradient(to bottom, {t.bg} 0%, transparent 60%); }}
.slide.has-two-images[data-split="v"][data-size-b="50"] .slide-image-b-gradient {{ bottom:0; left:0; right:0; height:50%; background-image: linear-gradient(to bottom, {t.bg} 0%, transparent 60%); }}
.slide.has-two-images[data-split="v"][data-size-b="70"] .slide-image-b-gradient {{ bottom:0; left:0; right:0; height:70%; background-image: linear-gradient(to bottom, {t.bg} 0%, transparent 60%); }}
"""

    if t.custom_css_path:
        path = Path(t.custom_css_path)
        if path.exists():
            try:
                custom = path.read_text(encoding="utf-8")
                css += (
                    "\n\n/* ── Theme custom CSS ──────────────────── */\n"
                    + custom
                )
            except OSError as e:
                log.warning("Cannot read custom CSS '%s': %s",
                            t.custom_css_path, e)

    return css


# ── Private helpers ───────────────────────────────────────────────────────────

def _resolve(theme) -> Theme:
    """
    Accept either a Theme dataclass or a legacy dict, and return a Theme
    whose colour fields have all been validated against the CSS colour allowlist.

    Sanitising here — at the single entry point — means every downstream
    f-string interpolation in build_css() and _callout_css() is guaranteed
    to receive a safe value, even if new colour fields are added in future.
    """
    if isinstance(theme, Theme):
        t = theme
    else:
        t = Theme()
        t.bg             = theme.get("bg",            t.bg)
        t.fg             = theme.get("fg",            t.fg)
        t.accent         = theme.get("accent",        t.accent)
        t.heading_color  = theme.get("heading_color", "")
        t.code_bg        = theme.get("code_bg",       t.code_bg)
        t.title_bg       = theme.get("title_bg",      t.title_bg)
        t.title_fg       = theme.get("title_fg",      t.title_fg)
        t.pygments_style = theme.get("pygments_style", t.pygments_style)
        t.callout_tip     = theme.get("callout_tip",     t.callout_tip)
        t.callout_info    = theme.get("callout_info",    t.callout_info)
        t.callout_warning = theme.get("callout_warning", t.callout_warning)
        t.callout_danger  = theme.get("callout_danger",  t.callout_danger)

    # Sanitise every colour field so CSS interpolation is always safe.
    # _safe_colour rejects values that break out of a CSS property context.
    t.bg            = _safe_colour(t.bg,            "#ffffff")
    t.fg            = _safe_colour(t.fg,            "#1a1a2e")
    t.accent        = _safe_colour(t.accent,        "#E17000")
    t.accent2       = _safe_colour(t.accent2,       t.accent) if t.accent2 else t.accent
    t.heading_color = _safe_colour(t.heading_color, t.accent) if t.heading_color else ""
    t.code_bg       = _safe_colour(t.code_bg,       "#f0f4f8")
    t.title_bg      = _safe_colour(t.title_bg,      "#1a1a2e")
    t.title_fg      = _safe_colour(t.title_fg,      "#ffffff")
    t.title_accent  = _safe_colour(t.title_accent,  t.accent) if t.title_accent else ""

    # Sanitise callout colour triples
    for attr in ("callout_tip", "callout_info", "callout_warning", "callout_danger"):
        raw = getattr(t, attr)
        if isinstance(raw, (list, tuple)) and len(raw) == 3:
            setattr(t, attr, tuple(
                _safe_colour(c, "#888888") for c in raw
            ))

    return t


def _logo_css(height: int, width: int) -> str:
    return f"""
    .slide-logo {{
        position: absolute;
        bottom: {int(height * 0.03)}px;
        left: {int(width * 0.04)}px;
        height: {int(height * 0.06)}px;
        width: auto;
        opacity: 0.7;
        object-fit: contain;
    }}"""


def _callout_css(kind: str, t: Theme, width: int, height: int, icon: str) -> str:
    bg, fg, border = getattr(t, f"callout_{kind}")
    badge_size     = int(width * 0.024)
    # Sanitise the icon: strip single-quotes to prevent CSS content injection
    safe_icon = icon.replace("'", "")
    return f"""
    .callout-{kind} {{
        background: {bg};
        border-left: {int(width * 0.006)}px solid {border};
        border-radius: 0 6px 6px 0;
        padding: {int(height * 0.018)}px {int(width * 0.025)}px;
        margin: {int(height * 0.015)}px 0;
        position: relative;
    }}
    .callout-{kind}::before {{
        content: '{safe_icon}';
        position: absolute;
        left: -{int(width * 0.003) + int(width * 0.012)}px;
        top: 50%;
        margin-top: -{badge_size // 2}px;
        background: {border};
        color: {bg};
        width: {badge_size}px;
        height: {badge_size}px;
        border-radius: 50%;
        font-size: {int(height * 0.022)}px;
        font-weight: 700;
        line-height: {badge_size}px;
        text-align: center;
    }}
    .callout-{kind} p {{
        color: {fg};
        margin: 0;
        font-size: {int(height * 0.030)}px;
    }}
    .callout-{kind} strong {{
        color: {fg};
    }}"""
