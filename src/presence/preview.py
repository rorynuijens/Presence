"""
preview.py — Live slide canvas.

Shows the one slide the cursor is currently in, at a readable size, rendered
from HTML rather than from the PDF.  This is the fast half of the two-speed
pipeline described in converter.py: the canvas updates as you type, while the
PDF (and the thumbnails derived from it) stays an on-demand build.

Fitting is done with WebKit's own zoom level rather than a CSS transform, so
resizing the pane rescales the slide without reloading the document.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

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


class SlideCanvas(Gtk.Box):
    """
    WebKit-backed view of a single slide.

    Call :meth:`show_slide` with a one-slide HTML document; call
    :meth:`show_message` for empty and error states.
    """

    # Space left between the slide edge and the pane edge, in device pixels.
    _MARGIN_PX = 24

    # Injected into every document: strips paged-media artefacts, centres the
    # slide in the viewport and sets it on a neutral artboard so that both
    # white and black slides keep a visible edge.
    _CANVAS_CSS = """
        <style>
        html {
            background: #2b2b2b;
            height: 100%;
        }
        body {
            margin: 0;
            padding: 0;
            min-height: 100vh;
            background: #2b2b2b;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .slide {
            flex: none;
            page-break-after: unset !important;
            box-shadow: 0 2px 16px rgba(0, 0, 0, 0.45);
            outline: 1px solid rgba(127, 127, 127, 0.35);
            outline-offset: -1px;
        }
        </style>
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._slide_w: int = 1280
        self._slide_h: int = 720
        self._view_w:  int = 0
        self._view_h:  int = 0
        self._loaded_html: str | None = None
        self._fit_source: int | None = None

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        self.append(overlay)

        self._stack = Gtk.Stack()
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        overlay.set_child(self._stack)

        # GtkWidget has no resize signal in GTK 4 and the size_allocate vfunc
        # is not delivered to Python subclasses of GtkBox, so an inert
        # DrawingArea rides along as a size sentinel: it paints nothing,
        # takes no input, and reports the pane's size whenever it changes.
        self._sentinel = Gtk.DrawingArea()
        self._sentinel.set_can_target(False)
        self._sentinel.set_hexpand(True)
        self._sentinel.set_vexpand(True)
        self._sentinel.connect("resize", self._on_resize)
        overlay.add_overlay(self._sentinel)

        self._status = Adw.StatusPage()
        self._status.set_icon_name("view-paged-symbolic")
        self._stack.add_named(self._status, "status")

        self._webview = self._make_webview()
        if self._webview is not None:
            self._webview.set_hexpand(True)
            self._webview.set_vexpand(True)
            self._stack.add_named(self._webview, "slide")
            self._available = True
        else:
            self._available = False

        if self._available:
            self.show_message(
                "No slide yet",
                "Start typing to see the slide you are editing.",
            )
        else:
            self.show_message(
                "Live canvas unavailable",
                "Install WebKitGTK to preview slides while you edit. "
                "Building and presenting still work.",
            )

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True when WebKit is present and the canvas can render slides."""
        return self._available

    def show_slide(self, html: str, base_dir: "Path | str | None",
                   slide_w: int, slide_h: int) -> None:
        """
        Display a one-slide HTML document, scaled to fit the pane.

        Reloading is skipped when *html* is byte-identical to what is already
        on screen, so moving the cursor within a slide costs nothing.
        """
        if not self._available:
            return

        self._slide_w = max(1, slide_w)
        self._slide_h = max(1, slide_h)

        if html == self._loaded_html:
            self._stack.set_visible_child_name("slide")
            self._apply_fit()
            return

        base_uri = None
        if base_dir is not None:
            try:
                base_uri = Path(base_dir).as_uri() + "/"
            except (ValueError, OSError):
                base_uri = None

        injected = html.replace("</head>", self._CANVAS_CSS + "</head>", 1)
        self._loaded_html = html
        self._webview.load_html(injected, base_uri)
        self._stack.set_visible_child_name("slide")
        self._apply_fit()

    def show_message(self, title: str, description: str = "") -> None:
        """Show an empty or error state instead of a slide."""
        self._status.set_title(title)
        self._status.set_description(description or None)
        self._stack.set_visible_child_name("status")
        self._loaded_html = None

    def clear(self) -> None:
        """Drop the cached document so the next show_slide() reloads."""
        self._loaded_html = None

    # ── Fitting ───────────────────────────────────────────────────────────────

    def _on_resize(self, _area, width: int, height: int) -> None:
        self._view_w = width
        self._view_h = height
        self._schedule_fit()

    def _schedule_fit(self) -> None:
        """Refit off the allocation cycle — setting zoom mid-allocate re-lays out."""
        if not self._available or self._fit_source is not None:
            return
        self._fit_source = GLib.idle_add(self._idle_fit)

    def _idle_fit(self) -> bool:
        self._fit_source = None
        self._apply_fit()
        return GLib.SOURCE_REMOVE

    def _apply_fit(self) -> None:
        if not self._available:
            return
        avail_w = self._view_w - self._MARGIN_PX
        avail_h = self._view_h - self._MARGIN_PX
        if avail_w <= 0 or avail_h <= 0:
            return
        scale = min(avail_w / self._slide_w, avail_h / self._slide_h)
        # WebKit refuses non-positive zoom; keep a floor for very small panes.
        scale = max(0.05, scale)
        if abs(self._webview.get_zoom_level() - scale) > 0.001:
            self._webview.set_zoom_level(scale)

    # ── Construction helpers ──────────────────────────────────────────────────

    @staticmethod
    def _make_webview():
        if WebKit is None:
            return None
        settings = WebKit.Settings()
        settings.set_allow_file_access_from_file_urls(True)
        # JavaScript is not needed — the canvas shows one static slide and
        # fitting is done host-side via zoom level.  Keeping it off means a
        # <script> tag in user Markdown cannot execute here (fixes #43 / #7).
        settings.set_enable_javascript(False)
        settings.set_enable_page_cache(False)

        if _WEBKIT_VERSION == 6:
            network_session = WebKit.NetworkSession.new_ephemeral()
            return WebKit.WebView(settings=settings,
                                  network_session=network_session)

        ctx = WebKit.WebContext.new_ephemeral()
        # Do NOT disable the sandbox — file access is granted via
        # set_allow_file_access_from_file_urls above (fixes #7).
        view = WebKit.WebView.new_with_context(ctx)
        view.set_settings(settings)
        return view
