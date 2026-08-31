"""
thumbnails_render.py — Render slide thumbnails from an already-generated PDF.

Pipeline (fast path):
  1. Receive the PDF bytes that WeasyPrint already produced for the final output
  2. Open it with Poppler — one call, all pages available immediately
  3. Render each page to a THUMB_W × THUMB_H PNG via Cairo

Public API
----------
render_thumbnails(pdf_bytes, n_slides) -> list[bytes | None]
    Returns one PNG bytes blob per slide (None on render failure).
"""

import io
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Twice the widest the strip's size slider goes (sidebar.THUMBNAIL_MAX).
#
# Two reasons for the doubling, and both are needed: a picture shown at 480
# logical pixels needs 960 device pixels on a 2x display, and the slider has
# to be able to move without re-rendering the deck.  Rasterizing once at the
# ceiling and letting GtkPicture scale down is what makes dragging it a
# relayout rather than a rebuild.
THUMB_W = 960
THUMB_H = 540


def rasterizer_available() -> bool:
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


def _load_pdf_doc(pdf_bytes: bytes):
    """
    Load *pdf_bytes* into a Poppler Document.

    Tries the in-memory API (Poppler ≥ 0.82) first to avoid any disk I/O.
    Falls back to writing a temp file for older Poppler versions.

    Returns the Document on success, or None on any failure.
    The caller is responsible for importing Poppler and GLib before calling.
    """
    from gi.repository import Poppler, GLib  # noqa: PLC0415 — caller ensures version

    # Fast path: load directly from bytes (Poppler >= 0.82).
    try:
        return Poppler.Document.new_from_bytes(GLib.Bytes.new(pdf_bytes))
    except Exception:
        pass

    # Slow path: write to a temp file and load via URI.
    # The fd is closed inside the inner try/finally before new_from_file reads
    # the file, so the file is fully flushed before Poppler opens it.
    fd, tmp_str = tempfile.mkstemp(suffix=".pdf")
    tmp = Path(tmp_str)
    try:
        try:
            os.write(fd, pdf_bytes)
        finally:
            os.close(fd)  # close before Poppler reads so data is flushed
        return Poppler.Document.new_from_file(tmp.as_uri())
    except Exception as e:
        log.error("Failed to load PDF into Poppler: %s", e)
        return None
    finally:
        # Always remove the temp file, whether load succeeded or not.
        tmp.unlink(missing_ok=True)


def render_thumbnails(
    pdf_bytes: bytes,
    n_slides:  int,
    pages:     "list[int] | None" = None,
) -> list[bytes | None]:
    """
    Convert slides of *pdf_bytes* to PNG thumbnails (THUMB_W × THUMB_H).

    *pages* gives the PDF page each slide starts on.  A slide whose content
    overflows makes WeasyPrint emit a continuation page, so page number and
    slide number part company as soon as that happens and the strip would
    otherwise show a slide's spill as if it were the next slide.  Without
    *pages* every page is rendered, which is the same thing whenever nothing
    overflows.

    Returns one PNG bytes blob per slide, or an empty list when Cairo or
    Poppler are unavailable.
    """
    try:
        import cairo
    except Exception as e:
        log.warning("Cairo not available: %s", e)
        return []

    try:
        import gi
        gi.require_version("Poppler", "0.18")
    except Exception as e:
        log.warning("Poppler not available: %s", e)
        return []

    doc = _load_pdf_doc(pdf_bytes)
    if doc is None:
        return []

    wanted = pages if pages is not None else list(range(doc.get_n_pages()))

    results: list[bytes | None] = []
    for page_index in wanted:
        if not 0 <= page_index < doc.get_n_pages():
            results.append(_placeholder_png(cairo))
            continue
        try:
            results.append(_render_page(doc, page_index, cairo))
        except Exception as e:
            log.warning("Thumbnail render failed for page %d: %s",
                        page_index + 1, e)
            results.append(_placeholder_png(cairo))

    return results


# ── Internal helpers ──────────────────────────────────────────────────────────

def _render_page(doc, page_index: int, cairo) -> bytes:
    """Render one Poppler page to PNG bytes at THUMB_W × THUMB_H."""
    page   = doc.get_page(page_index)
    pw, ph = page.get_size()            # size in PDF points

    scale = min(THUMB_W / pw, THUMB_H / ph)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, THUMB_W, THUMB_H)
    ctx     = cairo.Context(surface)

    # Fill white background (PDF backgrounds are transparent by default)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()

    ctx.scale(scale, scale)
    # render() is correct for screen/thumbnail output; render_for_printing()
    # applies print-specific colour calibration unsuitable for thumbnails.
    page.render(ctx)

    buf = io.BytesIO()
    surface.write_to_png(buf)
    return buf.getvalue()


def _placeholder_png(cairo) -> bytes:
    """Return a dark grey placeholder PNG for pages that fail to render."""
    try:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, THUMB_W, THUMB_H)
        ctx     = cairo.Context(surface)
        ctx.set_source_rgb(0.15, 0.15, 0.15)
        ctx.paint()
        buf = io.BytesIO()
        surface.write_to_png(buf)
        return buf.getvalue()
    except Exception:
        return b""


def render_slides_hires(
    pdf_bytes: bytes,
    width_px:  int = 1920,
    pages:     "list[int] | None" = None,
) -> list[bytes | None]:
    """
    Render slides of *pdf_bytes* to high-resolution PNGs.

    *width_px* sets the output width in pixels; height is derived from the
    page aspect ratio so the image is never distorted.  Use 1920 for full-HD
    quality suitable for PNG export (sharp at any normal screen size).

    *pages* gives the PDF page each slide starts on; see render_thumbnails().
    Without it every page is rendered.

    Returns one PNG bytes blob per slide (None on render failure), using the
    same Poppler+Cairo pipeline as render_thumbnails().
    """
    try:
        import cairo
    except Exception as e:
        log.warning("Cairo not available for hires render: %s", e)
        return []

    try:
        import gi
        gi.require_version("Poppler", "0.18")
    except Exception as e:
        log.warning("Poppler not available for hires render: %s", e)
        return []

    doc = _load_pdf_doc(pdf_bytes)
    if doc is None:
        return []

    wanted = pages if pages is not None else list(range(doc.get_n_pages()))

    results: list[bytes | None] = []
    for page_index in wanted:
        if not 0 <= page_index < doc.get_n_pages():
            results.append(None)
            continue
        try:
            results.append(_render_page_hires(doc, page_index, cairo, width_px))
        except Exception as e:
            log.warning("Hires render failed for page %d: %s", page_index + 1, e)
            results.append(None)

    return results


def _render_page_hires(doc, page_index: int, cairo, width_px: int) -> bytes:
    """Render one Poppler page to PNG at *width_px* wide."""
    page   = doc.get_page(page_index)
    pw, ph = page.get_size()           # PDF points (72 pt = 1 inch)

    # Scale so the output is exactly width_px wide
    scale  = width_px / pw
    height_px = int(ph * scale)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width_px, height_px)
    ctx     = cairo.Context(surface)

    # Fill white — PDF backgrounds are transparent
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()

    ctx.scale(scale, scale)
    page.render(ctx)

    buf = io.BytesIO()
    surface.write_to_png(buf)
    return buf.getvalue()


def render_page_png(
    pdf_bytes: bytes,
    index:     int = 0,
    width_px:  int = 960,
) -> "bytes | None":
    """
    Render a single page of *pdf_bytes* to a PNG *width_px* pixels wide.

    The live render needs one page at display resolution rather than the whole
    deck, so this is the one-page sibling of render_slides_hires(): same
    Poppler+Cairo pipeline, one page, no list.

    Returns None when Cairo or Poppler are missing, when the PDF will not
    load, when *index* is out of range, or when the page fails to render.
    """
    try:
        import cairo
    except Exception as e:
        log.warning("Cairo not available for page render: %s", e)
        return None

    try:
        import gi
        gi.require_version("Poppler", "0.18")
    except Exception as e:
        log.warning("Poppler not available for page render: %s", e)
        return None

    doc = _load_pdf_doc(pdf_bytes)
    if doc is None:
        return None

    if not 0 <= index < doc.get_n_pages():
        log.warning("Page %d out of range (%d pages)", index, doc.get_n_pages())
        return None

    try:
        return _render_page_hires(doc, index, cairo, max(1, width_px))
    except Exception as e:
        log.warning("Page render failed for page %d: %s", index + 1, e)
        return None
