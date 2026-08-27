"""
preview.py — Live slide canvas.

Shows the one slide the cursor is currently in, at a readable size, rendered
by the same engine that makes the deck.  The canvas is a picture of a page
WeasyPrint laid out and Poppler rasterized — the identical pipeline behind the
sidebar thumbnails and the exported PDF — so what the writer sees here is what
the audience and the reader get, down to where a slide runs out of room.

The rasterization happens in ``Converter.render_slide_async``; this module only
displays the result.  Fitting is left to ``Gtk.Picture``'s content fit, so
dragging the pane rescales the existing texture immediately and a fresh render
at the new width follows only when the change is big enough to matter.
"""

import logging

log = logging.getLogger(__name__)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, GObject

from .app_utils import png_bytes_to_texture

_css_provider_registered = False


def _ensure_css_provider(display) -> None:
    """Register the artboard styling once per display."""
    global _css_provider_registered
    if _css_provider_registered or display is None:
        return
    css = Gtk.CssProvider()
    style = (
        ".slide-artboard {"
        "  background: #2b2b2b;"
        "}"
        ".slide-paper {"
        "  box-shadow: 0 2px 16px rgba(0, 0, 0, 0.45);"
        "  outline: 1px solid rgba(127, 127, 127, 0.35);"
        "}"
    )
    try:
        css.load_from_string(style)
    except AttributeError:
        css.load_from_data(style.encode())
    Gtk.StyleContext.add_provider_for_display(
        display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _css_provider_registered = True


class SlideCanvas(Gtk.Box):
    """
    Picture of a single slide, as WeasyPrint laid it out.

    Call :meth:`show_slide` with the PNG bytes of one rendered slide; call
    :meth:`show_message` for empty and error states.  The canvas asks for a
    fresh render by emitting ``render-size-changed`` when the pane has been
    resized enough that the current texture would visibly soften.
    """

    __gsignals__ = {
        # The pane wants a re-render at a new pixel width.
        "render-size-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    # Space left between the slide edge and the pane edge, in device pixels.
    _MARGIN_PX = 24

    # Re-render only when the pane width has moved by more than this fraction
    # of the width the current texture was rasterized at.  Below it, scaling
    # the existing texture is indistinguishable and costs nothing, which is
    # what keeps a splitter drag smooth.
    _RERENDER_TOLERANCE = 0.25

    # Bounds on the rasterization width, in device pixels.  The lower bound
    # keeps a collapsed pane from asking for a one-pixel page; the upper one
    # stops a maximised 4K pane from rendering far past the slide's own
    # resolution for no visible gain.
    _MIN_RENDER_W = 320
    _MAX_RENDER_W = 2560

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._slide_w: int = 1280
        self._slide_h: int = 720
        self._view_w:  int = 0
        self._view_h:  int = 0
        self._loaded_png: bytes | None = None
        self._rendered_at_w: int = 0
        self._resize_source: int | None = None

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

        # The artboard sits behind the slide so that both white and black
        # slides keep a visible edge against the pane.
        artboard = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        artboard.add_css_class("slide-artboard")
        artboard.set_hexpand(True)
        artboard.set_vexpand(True)

        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(True)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.add_css_class("slide-paper")
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.set_margin_top(self._MARGIN_PX // 2)
        self._picture.set_margin_bottom(self._MARGIN_PX // 2)
        self._picture.set_margin_start(self._MARGIN_PX // 2)
        self._picture.set_margin_end(self._MARGIN_PX // 2)
        artboard.append(self._picture)

        self._stack.add_named(artboard, "slide")

        self.connect("realize", self._on_realize)

        self._available = _rasterizer_available()
        if self._available:
            self.show_message(
                "No slide yet",
                "Start typing to see the slide you are editing.",
            )
        else:
            self.show_message(
                "Live canvas unavailable",
                "Install Poppler and Cairo to preview slides while you edit. "
                "Building and presenting still work.",
            )

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True when the slide rasterizer is present and the canvas can draw."""
        return self._available

    @property
    def render_width(self) -> int:
        """
        Pixel width to rasterize the next slide at.

        Accounts for the display scale factor, so a slide stays sharp on a
        HiDPI screen rather than being upscaled from logical pixels.
        """
        logical = max(1, self._view_w - self._MARGIN_PX)
        scaled = logical * max(1, self.get_scale_factor())
        return int(min(self._MAX_RENDER_W, max(self._MIN_RENDER_W, scaled)))

    def show_slide(self, png: bytes, slide_w: int, slide_h: int) -> None:
        """
        Display a rendered slide, scaled to fit the pane.

        Decoding is skipped when *png* is byte-identical to what is already on
        screen, so moving the cursor within a slide costs nothing.
        """
        if not self._available or not png:
            return

        self._slide_w = max(1, slide_w)
        self._slide_h = max(1, slide_h)
        self._rendered_at_w = self.render_width

        if png == self._loaded_png:
            self._stack.set_visible_child_name("slide")
            return

        texture = png_bytes_to_texture(png)
        if texture is None:
            log.debug("Slide PNG could not be decoded")
            return

        self._loaded_png = png
        self._picture.set_paintable(texture)
        self._stack.set_visible_child_name("slide")

    def show_message(self, title: str, description: str = "") -> None:
        """Show an empty or error state instead of a slide."""
        self._status.set_title(title)
        self._status.set_description(description or None)
        self._stack.set_visible_child_name("status")
        self._loaded_png = None

    def clear(self) -> None:
        """Drop the cached image so the next show_slide() redraws."""
        self._loaded_png = None

    # ── Sizing ────────────────────────────────────────────────────────────────

    def _on_realize(self, _widget) -> None:
        _ensure_css_provider(self.get_display())

    def _on_resize(self, _area, width: int, height: int) -> None:
        self._view_w = width
        self._view_h = height
        self._schedule_rerender_check()

    def _schedule_rerender_check(self) -> None:
        """
        Ask for a fresh render off the allocation cycle, and only if needed.

        Gtk.Picture rescales the texture it already has for free, so a drag
        only needs new pixels once the pane has moved far enough that the
        rescaling would show.  Deferring to idle also keeps a re-render from
        being requested in the middle of an allocation.
        """
        if not self._available or self._resize_source is not None:
            return
        self._resize_source = GLib.idle_add(self._idle_rerender_check)

    def _idle_rerender_check(self) -> bool:
        self._resize_source = None
        if self._loaded_png is None or self._rendered_at_w <= 0:
            return GLib.SOURCE_REMOVE

        wanted = self.render_width
        drift = abs(wanted - self._rendered_at_w) / self._rendered_at_w
        if drift > self._RERENDER_TOLERANCE:
            self._rendered_at_w = wanted   # claim it now so a burst of resize
                                           # events asks for exactly one render
            self.emit("render-size-changed")
        return GLib.SOURCE_REMOVE


def _rasterizer_available() -> bool:
    """True when Poppler and Cairo can be loaded, so slides can be drawn."""
    try:
        import cairo  # noqa: F401
        import gi as _gi
        _gi.require_version("Poppler", "0.18")
        from gi.repository import Poppler  # noqa: F401
    except Exception as e:
        log.warning("Slide rasterizer unavailable: %s", e)
        return False
    return True
