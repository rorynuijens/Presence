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

THUMB_W = 320
THUMB_H = 180


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
) -> list[bytes | None]:
    """
    Convert each page of *pdf_bytes* to a PNG thumbnail (THUMB_W × THUMB_H).

    *n_slides* is used only for progress reporting; the actual page count
    comes from Poppler so it is always correct even if the PDF was trimmed.

    Returns one PNG bytes blob per page, or an empty list when Cairo or
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

    results: list[bytes | None] = []
    for i in range(doc.get_n_pages()):
        try:
            results.append(_render_page(doc, i, cairo))
        except Exception as e:
            log.warning("Thumbnail render failed for page %d: %s", i + 1, e)
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
) -> list[bytes | None]:
    """
    Render each page of *pdf_bytes* to a high-resolution PNG.

    *width_px* sets the output width in pixels; height is derived from the
    page aspect ratio so the image is never distorted.  Use 1920 for full-HD
    quality suitable for PNG export (sharp at any normal screen size).

    Returns one PNG bytes blob per page (None on render failure), using the
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

    results: list[bytes | None] = []
    for i in range(doc.get_n_pages()):
        try:
            results.append(_render_page_hires(doc, i, cairo, width_px))
        except Exception as e:
            log.warning("Hires render failed for page %d: %s", i + 1, e)
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
