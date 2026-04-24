"""
renderer.py — Markdown-to-HTML renderer and Pygments CSS helper.

Public API
----------
render_slide_content(markdown_text: str) -> str
    Convert Markdown to an HTML fragment with table support.

get_pygments_css(style_name: str) -> str
    Return the Pygments CSS for the named style.
    Called by css.py at import time — must never raise.
"""
from __future__ import annotations

import html as _html
import re

from markdown_it import MarkdownIt

# ── Pygments (optional) ───────────────────────────────────────────────────────

try:
    from pygments import highlight as _highlight
    from pygments.formatters import HtmlFormatter as _HtmlFormatter
    from pygments.lexers import get_lexer_by_name as _get_lexer
    from pygments.lexers import TextLexer as _TextLexer
    _PYGMENTS = True
except ImportError:
    _PYGMENTS = False


def get_pygments_css(style_name: str = "friendly") -> str:
    """
    Return the Pygments CSS for *style_name*.

    Called by css.py to embed code-block highlighting in the slide CSS.
    Returns an empty string if Pygments is not installed.

    *style_name* is validated against the Pygments style registry before use
    to prevent unexpected file-system resolution via Pygments' plugin discovery
    mechanism.  Unknown styles fall back to "friendly".
    """
    if not _PYGMENTS:
        return ""
    try:
        from pygments.styles import get_all_styles
        valid_styles = frozenset(get_all_styles())
        safe_style = style_name if style_name in valid_styles else "friendly"
        return _HtmlFormatter(style=safe_style).get_style_defs(".highlight")
    except Exception:
        try:
            return _HtmlFormatter(style="default").get_style_defs(".highlight")
        except Exception:
            return ""


# ── markdown-it renderer with table support ───────────────────────────────────

# Construct once at import time.
# "commonmark" preset with html=False (the default, but stated explicitly so
# a future markdown-it-py version change cannot silently enable raw HTML
# pass-through, which would allow <script> tags in user Markdown to reach
# the generated HTML and PDF).
# enable("table") adds GFM-style pipe table support.
_md = MarkdownIt("commonmark", {"html": False}).enable("table")


def render_slide_content(markdown_text: str) -> str:
    """
    Convert *markdown_text* to an HTML fragment (no <html>/<body> wrapper).

    Supports GFM-style pipe tables and all standard CommonMark Markdown.
    Fenced code blocks are syntax-highlighted by Pygments when available.

    Returns an empty string for empty/whitespace-only input.
    Falls back to a <pre> block on any error so slides never go blank.
    """
    if not markdown_text or not markdown_text.strip():
        return ""
    try:
        html = _md.render(markdown_text)
        # Apply Pygments highlighting to fenced code blocks when available.
        # markdown-it emits <code class="language-X"> inside <pre> — rewrite.
        if _PYGMENTS:
            html = _highlight_code_blocks(html)
        return html
    except Exception:
        return f"<pre>{_html.escape(markdown_text)}</pre>"


def _highlight_code_blocks(html: str) -> str:
    """
    Replace <pre><code class="language-X">...</code></pre> blocks with
    Pygments-highlighted equivalents.
    """
    # Match <pre><code class="language-LANG">CONTENT</code></pre>
    pattern = re.compile(
        r'<pre><code class="language-([^"]+)">(.*?)</code></pre>',
        re.DOTALL,
    )

    def _replace(m: "re.Match[str]") -> str:
        lang = m.group(1)
        # markdown-it HTML-escapes the code content; unescape before highlighting
        code = _html.unescape(m.group(2))
        try:
            lexer = _get_lexer(lang, stripall=True)
        except Exception:
            lexer = _TextLexer()
        formatter = _HtmlFormatter(nowrap=True, cssclass="highlight")
        highlighted = _highlight(code, lexer, formatter)
        safe_lang = _html.escape(lang)
        return (
            f'<div class="highlight">'
            f'<pre><code class="language-{safe_lang}">'
            f"{highlighted}"
            f"</code></pre></div>"
        )

    return pattern.sub(_replace, html)
