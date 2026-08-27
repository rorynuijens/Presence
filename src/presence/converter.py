"""
converter.py — Bridge between the GTK UI and the md_to_slides library.

One engine, three speeds
------------------------
Everything the writer, the room and the reader see is laid out by WeasyPrint,
so the canvas, the slideshow, the thumbnails and the PDF are the same picture.
What differs is only how much of the document each pass covers:

*  ``build_preview()``      — Markdown → HTML for a single slide.  Sub-millisecond,
   synchronous.  No layout: this is the cheapest way to ask what a slide's
   markup is, and whether the document has any slides at all.
*  ``render_slide_async()`` — Markdown → HTML → PDF → PNG for a single slide.
   Tens of milliseconds for text, a few hundred for a gallery; runs on a
   background thread and drives the live canvas and the live fold marker.
*  ``convert()``            — the whole deck → HTML → PDF → thumbnail bitmaps.
   Seconds, background thread, drives the sidebar, export and presenter.

All three share the theme/CSS resolution in ``_render_context()``, which is
cached so no pass rebuilds a stylesheet it already has.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject

try:
    import weasyprint as _weasyprint
except ImportError:
    _weasyprint = None  # type: ignore[assignment]

from .slides.frontmatter import parse_frontmatter
from .slides.splitter import split_slides
from .slides.css import build_css
from .slides.html import md_to_html_slides
from .slides.themes import ASPECT_RATIOS
from .slides.theme_loader import load_all_themes
from .slides.utils import (encode_logo, safe_subpath,
                            compute_slide_start_lines)
from .slides.thumbnails_render import render_thumbnails, render_page_png

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class RenderContext:
    """Everything needed to turn slide Markdown into HTML, minus the Markdown."""
    css:      str
    width:    int
    height:   int
    theme_bg: str
    logo_b64: str | None


@dataclasses.dataclass(frozen=True)
class Preview:
    """One rendered slide, ready for the canvas."""
    html:       str
    index:      int
    n_slides:   int
    slide_info: list
    width:      int
    height:     int


@dataclasses.dataclass(frozen=True)
class SlideFrame:
    """One slide laid out by WeasyPrint and rasterized, ready for the canvas."""
    png:        bytes
    fold_line:  int | None
    index:      int
    n_slides:   int
    width:      int
    height:     int


def _measure_folds(document, n_slides: int) -> list[int | None]:
    """
    Find, per slide, the first source line whose block runs past the slide.

    Slides are a fixed box with overflow:hidden, so WeasyPrint lays every
    block out and simply clips what does not fit.  Walking the box tree after
    layout therefore says exactly where a slide runs out of room, which a
    word count can only guess at.  The data-src-line attributes put there by
    the renderer turn a y coordinate back into a line the writer can edit.

    Returns one entry per slide: the line number, or None when it all fits.
    Any failure yields None rather than a wrong line — the box tree is
    WeasyPrint's internal representation and may change between versions.
    """
    folds: list[int | None] = []
    try:
        pages = list(document.pages)
    except Exception:
        return [None] * n_slides

    for page in pages[:n_slides]:
        folds.append(_fold_line_for_page(page))
    folds.extend([None] * (n_slides - len(folds)))
    return folds


def _fold_line_for_page(page) -> "int | None":
    """First line that runs past the slide's text area on *page*, or None."""
    try:
        boxes = list(_walk_boxes(page._page_box))
        limit = _content_bottom(boxes, page.height)

        best_y: float | None = None
        best_line: int | None = None

        for box in boxes:
            line = _box_line(box)
            if line is None:
                continue
            # Skip containers: a <ul> is stamped with its first item's line,
            # so letting it compete would fold the list at an item that fits.
            # A box counts only when its whole subtree comes from one line.
            if _subtree_lines(box) != {line}:
                continue
            # Ignore zero-height boxes, which carry no visible content.
            if box.height <= 0 or box.position_y + box.height <= limit:
                continue
            # Topmost box that crosses: where the slide runs out of room.
            if best_y is None or box.position_y < best_y:
                best_y = box.position_y
                best_line = line
        return best_line
    except Exception:
        log.debug("Fold measurement failed", exc_info=True)
        return None


def _box_line(box) -> "int | None":
    """The source line stamped on *box*, if any."""
    element = getattr(box, "element", None)
    if element is None or not hasattr(element, "get"):
        return None
    raw = element.get("data-src-line")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _subtree_lines(box) -> set:
    """Every source line appearing in *box* and its descendants."""
    return {line for line in (_box_line(b) for b in _walk_boxes(box))
            if line is not None}


def _walk_boxes(box):
    """Yield *box* and every descendant."""
    stack = [box]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(getattr(current, "children", ()) or ())


def _content_bottom(boxes, page_height: float) -> float:
    """
    Bottom of the slide's text area.

    Themes reserve the lower padding for the slide number and progress bar,
    so content reaching into it collides with them — the slide has run out of
    room even though overflow:hidden would not clip until the page edge.
    Measuring against the text area warns at the point the design intends.
    """
    for box in boxes:
        element = getattr(box, "element", None)
        if element is None or not hasattr(element, "get"):
            continue
        classes = (element.get("class") or "").split()
        if "slide" in classes:
            return box.content_box_y() + box.height
    return page_height


class Converter(GObject.Object):
    __gsignals__ = {
        "conversion-started": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "conversion-complete": (
            GObject.SignalFlags.RUN_FIRST, None,
            (int, float, str, str),
        ),
        "conversion-failed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(
        self,
        theme:     str       = "light",
        ratio:     str       = "16:9",
        logo_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.theme     = theme
        self.ratio     = ratio
        self.logo_path = logo_path

        self._lock:         threading.Lock    = threading.Lock()
        self._converting:   bool             = False
        self._pending:      bool             = False
        self._watch_source: int | None       = None
        self._watch_mtime:  float            = 0.0
        self._watch_path:   Path | None      = None
        self._last_input:   Path | None      = None
        self._last_output:  Path | None      = None
        self._slide_info:   list             = []
        self._thumbnails:   list             = []
        # Cache for _render_context(); keyed by everything that affects the
        # stylesheet.  Guarded because the background thread reads it too.
        self._ctx_lock:  threading.Lock       = threading.Lock()
        self._ctx_key:   tuple | None         = None
        self._ctx_value: RenderContext | None = None
        # Canvas frames.  One worker at a time and at most one waiting
        # request: a keystroke arriving mid-render replaces the pending
        # request rather than queueing behind it, so the canvas can never
        # fall a burst of stale frames behind the cursor.
        self._frame_lock:    threading.Lock = threading.Lock()
        self._frame_serial:  int            = 0
        self._frame_pending: tuple | None   = None
        self._frame_busy:    bool           = False

    # ── Public API ────────────────────────────────────────────────────────────

    def convert(self, input_path: Path, output_path: Path) -> None:
        with self._lock:
            self._last_input  = input_path
            self._last_output = output_path
            if self._converting:
                self._pending = True
                return
            self._converting = True

        self.emit("conversion-started")
        threading.Thread(
            target=self._run,
            args=(input_path, output_path),
            daemon=True,
        ).start()

    def start_watch(self, input_path: Path, output_path: Path,
                    interval_ms: int = 500) -> None:
        """Poll *input_path* for changes and re-convert on every modification."""
        self.stop_watch()
        self._watch_path  = input_path
        self._watch_mtime = self._mtime(input_path)

        def _poll() -> bool:
            mtime = self._mtime(self._watch_path)
            if mtime != self._watch_mtime:
                self._watch_mtime = mtime
                self.convert(input_path, output_path)
            return GLib.SOURCE_CONTINUE

        self._watch_source = GLib.timeout_add(interval_ms, _poll)

    def stop_watch(self) -> None:
        if self._watch_source is not None:
            GLib.source_remove(self._watch_source)
            self._watch_source = None

    @property
    def watching(self) -> bool:
        return self._watch_source is not None

    @property
    def slide_info(self) -> list:
        return list(self._slide_info)

    @property
    def thumbnails(self) -> list:
        return list(self._thumbnails)

    def build_preview(
        self,
        text:        str,
        base_dir:    Path,
        slide_index: int,
    ) -> Preview:
        """
        Render a single slide to a standalone HTML document.

        Fast enough to call on every keystroke: no PDF, no rasterization, and
        the stylesheet comes from cache unless the theme changed.  Raises
        ValueError when the document contains no slides; other failures
        propagate as-is so the caller can show them.
        """
        meta, body = parse_frontmatter(text)
        slides = split_slides(body)
        if not slides:
            raise ValueError("No slides found — separate slides with ---")

        index = max(0, min(slide_index, len(slides) - 1))
        ctx = self._render_context(meta, base_dir)

        html, slide_info = md_to_html_slides(
            slides, ctx.css, ctx.logo_b64, meta,
            width=ctx.width, height=ctx.height, theme_bg=ctx.theme_bg,
            base_url=str(base_dir),
            only_index=index,
        )
        return Preview(
            html=html,
            index=index,
            n_slides=len(slides),
            slide_info=slide_info,
            width=ctx.width,
            height=ctx.height,
        )

    # ── Medium path: one slide, laid out and rasterized ───────────────────────

    def render_slide_async(
        self,
        text:      str,
        base_dir:  Path,
        index:     int,
        width_px:  int,
        callback,
    ) -> None:
        """
        Lay out and rasterize a single slide, off the main thread.

        This is the canvas's path, and it goes through the same WeasyPrint
        layout the PDF does — so what the writer sees is what the deck will
        be, down to where the slide runs out of room.

        *callback* is invoked on the main thread as ``callback(frame, error)``
        with exactly one of them set: a :class:`SlideFrame`, or the exception
        that stopped it.  A request that is superseded before it finishes is
        dropped silently and its callback never runs.
        """
        with self._frame_lock:
            self._frame_serial += 1
            self._frame_pending = (
                self._frame_serial, text, base_dir, index, width_px, callback,
            )
            if self._frame_busy:
                return          # the running worker will pick this up
            self._frame_busy = True

        threading.Thread(target=self._frame_worker, daemon=True).start()

    def _frame_worker(self) -> None:
        """Render pending canvas frames until none is waiting, then retire."""
        while True:
            with self._frame_lock:
                request = self._frame_pending
                self._frame_pending = None
                if request is None:
                    self._frame_busy = False
                    return

            serial, text, base_dir, index, width_px, callback = request
            frame: SlideFrame | None = None
            error: Exception | None  = None
            try:
                frame = self._render_frame(text, base_dir, index, width_px)
            except Exception as exc:              # surfaced to the canvas
                error = exc

            with self._frame_lock:
                superseded = serial != self._frame_serial

            # A newer keystroke has already been asked for; this frame would
            # only flash the wrong slide on its way to being replaced.
            if not superseded:
                GLib.idle_add(callback, frame, error)

    def _render_frame(
        self,
        text:     str,
        base_dir: Path,
        index:    int,
        width_px: int,
    ) -> SlideFrame:
        """Markdown → HTML → one-page PDF → PNG, plus the fold line."""
        meta, body = parse_frontmatter(text)
        slides = split_slides(body)
        if not slides:
            raise ValueError("No slides found — separate slides with ---")

        index = max(0, min(index, len(slides) - 1))
        ctx = self._render_context(meta, base_dir)

        # line_offsets is what stamps data-src-line onto every block, and
        # that is what turns the laid-out box tree back into a line the
        # writer can edit.  Without it there is no fold to measure.
        html, _info = md_to_html_slides(
            slides, ctx.css, ctx.logo_b64, meta,
            width=ctx.width, height=ctx.height, theme_bg=ctx.theme_bg,
            base_url=str(base_dir),
            only_index=index,
            line_offsets=compute_slide_start_lines(text),
        )

        if _weasyprint is None:
            raise ImportError("WeasyPrint is not installed — cannot render.")

        document = _weasyprint.HTML(string=html, base_url=str(base_dir)).render()
        fold = _measure_folds(document, 1)[0]

        png = render_page_png(document.write_pdf(), 0, width_px)
        if png is None:
            raise RuntimeError(
                "Could not rasterize the slide — Poppler or Cairo is missing."
            )

        return SlideFrame(
            png=png,
            fold_line=fold,
            index=index,
            n_slides=len(slides),
            width=ctx.width,
            height=ctx.height,
        )

    def invalidate_render_cache(self) -> None:
        """Drop the cached stylesheet — call after editing a theme on disk."""
        with self._ctx_lock:
            self._ctx_key = None
            self._ctx_value = None

    # ── Shared rendering setup ────────────────────────────────────────────────

    def _render_context(self, meta: dict, base_dir: Path) -> RenderContext:
        """
        Resolve theme, geometry, logo and stylesheet for a document.

        The result is cached on the cache key below, so repeated preview
        renders of the same document reuse one stylesheet string rather than
        re-reading fonts and re-running Pygments on every keystroke.
        """
        theme_name = meta.get("theme", self.theme)
        ratio      = meta.get("ratio", self.ratio)

        # Logo from frontmatter: restrict to the document directory to
        # prevent an untrusted .md file from exfiltrating arbitrary files.
        logo_path = self.logo_path
        if not logo_path and meta.get("logo"):
            safe = safe_subpath(base_dir, meta["logo"])
            if safe:
                logo_path = safe

        key = (theme_name, ratio, str(logo_path) if logo_path else None,
               meta.get("custom_css"), str(base_dir))
        with self._ctx_lock:
            if self._ctx_key == key and self._ctx_value is not None:
                return self._ctx_value

        all_themes = load_all_themes()
        # Fall back to any available theme if the requested one is missing.
        theme = all_themes.get(theme_name)
        if theme is None:
            theme = next(iter(all_themes.values()), None)
        if theme is None:
            raise ValueError(
                f"Theme '{theme_name}' not found and no themes are installed."
            )

        width, height = ASPECT_RATIOS.get(ratio, ASPECT_RATIOS["16:9"])
        logo_b64 = encode_logo(logo_path) if logo_path else None

        # Per-presentation custom CSS — copy theme before mutating to
        # avoid corrupting the cached Theme object (fixes #31).
        tmp_css_path: str | None = None
        if meta.get("custom_css"):
            extra_css = safe_subpath(base_dir, meta["custom_css"])
            if extra_css and extra_css.exists():
                if theme.custom_css_path:
                    combined = (
                        Path(theme.custom_css_path).read_text(encoding="utf-8")
                        + "\n\n/* Presentation override */\n"
                        + extra_css.read_text(encoding="utf-8")
                    )
                    fd, tmp = tempfile.mkstemp(suffix=".css")
                    try:
                        os.write(fd, combined.encode("utf-8"))
                    finally:
                        os.close(fd)
                    tmp_css_path = tmp
                    theme = dataclasses.replace(theme, custom_css_path=tmp)
                else:
                    theme = dataclasses.replace(
                        theme, custom_css_path=str(extra_css)
                    )

        try:
            # build_css() inlines the custom stylesheet into the returned
            # string, so the temp file is not needed once it returns.
            ctx = RenderContext(
                css=build_css(theme, width, height, logo_b64),
                width=width,
                height=height,
                theme_bg=theme.bg,
                logo_b64=logo_b64,
            )
        finally:
            if tmp_css_path is not None:
                try:
                    Path(tmp_css_path).unlink(missing_ok=True)
                except OSError:
                    pass

        with self._ctx_lock:
            self._ctx_key   = key
            self._ctx_value = ctx
        return ctx

    # ── Background thread ─────────────────────────────────────────────────────

    def _run(self, input_path: Path, output_path: Path) -> None:
        t0 = time.monotonic()
        try:
            raw_text = input_path.read_text(encoding="utf-8")
            meta, text = parse_frontmatter(raw_text)

            ctx = self._render_context(meta, input_path.parent)

            slides = split_slides(text)
            if not slides:
                raise ValueError("No slides found — separate slides with ---")

            html, slide_info = md_to_html_slides(
                slides, ctx.css, ctx.logo_b64, meta,
                width=ctx.width, height=ctx.height, theme_bg=ctx.theme_bg,
                base_url=str(input_path.parent),
                line_offsets=compute_slide_start_lines(raw_text),
            )

            html_path = output_path.with_suffix(".html")
            html_path.write_text(html, encoding="utf-8")

            if _weasyprint is None:
                raise ImportError(
                    "WeasyPrint is not installed — cannot generate PDF."
                )
            wp_doc    = _weasyprint.HTML(
                string=html, base_url=str(input_path.parent)
            ).render()
            pdf_bytes = wp_doc.write_pdf()
            output_path.write_bytes(pdf_bytes)

            # Same laid-out document the PDF came from, so the folds describe
            # the file the writer will actually hand out.
            for info, fold in zip(slide_info, _measure_folds(wp_doc, len(slides))):
                info["fold_line"] = fold

            duration   = time.monotonic() - t0
            n_slides   = len(slides)
            html_uri   = html_path.as_uri()
            thumbnails = render_thumbnails(pdf_bytes, n_slides)

            # Pass results as idle_add arguments — avoids writing to shared
            # state from the background thread (safe under free-threaded Python).
            GLib.idle_add(
                self._on_success,
                n_slides, duration, str(output_path), html_uri,
                slide_info, thumbnails,
            )

        except BaseException as exc:
            msg = str(exc) if str(exc) else repr(exc)
            log.exception("Conversion failed: %s", msg)
            GLib.idle_add(self._on_failure, msg)

    # ── Main-thread callbacks ─────────────────────────────────────────────────

    def _on_success(self, n_slides: int, duration: float,
                    pdf_path: str, html_uri: str,
                    slide_info: list, thumbnails: list) -> bool:
        # Store results on the main thread — no cross-thread data race.
        self._slide_info = slide_info
        self._thumbnails = thumbnails
        with self._lock:
            self._converting = False
            pending          = self._pending
            self._pending    = False
            pending_in       = self._last_input
            pending_out      = self._last_output

        self.emit("conversion-complete", n_slides, duration, pdf_path, html_uri)

        if pending and pending_in and pending_out:
            self.convert(pending_in, pending_out)
        return GLib.SOURCE_REMOVE

    def _on_failure(self, message: str) -> bool:
        with self._lock:
            self._converting = False
            pending          = self._pending
            self._pending    = False
            pending_in       = self._last_input
            pending_out      = self._last_output

        self.emit("conversion-failed", message)

        # Run the queued conversion even after failure so watch mode recovers
        # automatically on the next save (fixes #76).
        if pending and pending_in and pending_out:
            self.convert(pending_in, pending_out)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return 0.0
