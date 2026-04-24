"""
preview.py — HTML preview pane using WebKit.
"""

import logging
import re
from pathlib import Path
import html as _html

log = logging.getLogger(__name__)
from urllib.parse import urlparse
from urllib.request import url2pathname

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
    _WEBKIT_VERSION = 6
except (ValueError, ImportError):
    try:
        gi.require_version("WebKit2", "4.1")
        from gi.repository import WebKit2 as WebKit
        _WEBKIT_VERSION = 4
    except (ValueError, ImportError):
        WebKit = None
        _WEBKIT_VERSION = None


class Preview(Gtk.Box):
    """
    Wraps a WebKit WebView to show the rendered slide HTML.
    """

    _SLIDE_W = 1280
    _SLIDE_H = 720

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)

        if WebKit is not None:
            settings = WebKit.Settings()
            settings.set_allow_file_access_from_file_urls(True)
            # JavaScript is not needed for the static HTML preview and
            # disabling it prevents any script tags in user Markdown from
            # executing in the WebKit context (fixes #43 / #7).
            settings.set_enable_javascript(False)
            settings.set_enable_page_cache(False)

            if _WEBKIT_VERSION == 6:
                network_session = WebKit.NetworkSession.new_ephemeral()
                self._webview = WebKit.WebView(
                    settings=settings,
                    network_session=network_session,
                )
            else:
                ctx = WebKit.WebContext.new_ephemeral()
                # Do NOT disable the sandbox — file access is granted via
                # set_allow_file_access_from_file_urls above (fixes #7).
                self._webview = WebKit.WebView.new_with_context(ctx)
                self._webview.set_settings(settings)

            self._webview.set_hexpand(True)
            self._webview.set_vexpand(True)
            self.append(self._webview)
            self._available = True
        else:
            self._available = False
            placeholder = Gtk.Label(
                label="Install WebKitGTK to enable the live preview.\n\n"
                      "The PDF is still generated normally."
            )
            placeholder.add_css_class("dim-label")
            placeholder.set_wrap(True)
            placeholder.set_justify(Gtk.Justification.CENTER)
            placeholder.set_valign(Gtk.Align.CENTER)
            placeholder.set_halign(Gtk.Align.CENTER)
            self.append(placeholder)

    # ── Public API ────────────────────────────────────────────────────────────

    _PREVIEW_CSS = """
        <style>
        html, body {
            margin: 0;
            padding: 16px 0;
            background: #1a1a1a;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 16px;
        }
        @page { size: auto; margin: 0; }
        .slide {
            transform-origin: top center;
            transform: scale(var(--preview-scale, 0.4));
            margin-bottom: calc((var(--slide-height, 720px) * var(--preview-scale, 0.4)) - var(--slide-height, 720px));
            box-shadow: 0 4px 24px rgba(0,0,0,0.5);
            page-break-after: unset !important;
        }
        </style>
    """

    def set_slide_ratio(self, width: int, height: int) -> None:
        """Record the current slide dimensions for scaling calculations."""
        self._SLIDE_W = width
        self._SLIDE_H = height

    def load_html_file(self, html_uri: str, slide_w: int = 1280, slide_h: int = 720) -> None:
        """
        Read the HTML file, inject preview scaling CSS, and load via load_html().
        """
        if not self._available:
            return

        parsed = urlparse(html_uri)
        if parsed.scheme != "file":
            return

        try:
            file_path = Path(url2pathname(parsed.path))
            html_content = file_path.read_text(encoding="utf-8")

            scale = round(500 / slide_w, 4)

            css_vars = (
                f"<style>:root {{"
                f"--preview-scale: {scale};"
                f"--slide-height: {slide_h}px;"
                f"}}</style>"
            )
            injected = html_content.replace(
                "</head>",
                css_vars + self._PREVIEW_CSS + "</head>",
                1,
            )
            self._webview.load_html(injected, html_uri)
        except Exception as exc:
            log.warning("CSS injection failed, loading URI directly: %s", exc)
            self._webview.load_uri(html_uri)

    def show_placeholder(self, message: str = "Convert a file to see the preview.") -> None:
        if self._available:
            # Escape the message — it may contain user-controlled content
            # (e.g. a theme name from frontmatter that failed validation).
            safe_msg = _html.escape(message)
            self._webview.load_html(
                "<html><body style='"
                "font-family: sans-serif; color: #888;"
                "display:flex; align-items:center; justify-content:center;"
                "height:100vh; margin:0; text-align:center;"
                f"'><p>{safe_msg}</p></body></html>",
                "about:blank",
            )
