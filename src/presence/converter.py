"""
converter.py — Bridge between the GTK UI and the md_to_slides library.
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
from .slides.utils import encode_logo
from .slides.thumbnails_render import render_thumbnails

log = logging.getLogger(__name__)


def _safe_subpath(base: Path, untrusted: str) -> "Path | None":
    """
    Resolve *untrusted* relative to *base* and return the Path only if it
    stays within *base*.  Returns None if the resolved path escapes *base*,
    preventing path-traversal via frontmatter (e.g. logo: ../../../etc/passwd).
    Absolute paths in *untrusted* are always rejected.
    """
    try:
        candidate     = (base / untrusted).resolve()
        base_resolved = base.resolve()
        if str(candidate).startswith(str(base_resolved) + "/") or candidate == base_resolved:
            return candidate
    except Exception:
        pass
    log.warning("Frontmatter path '%s' escapes document directory — ignored", untrusted)
    return None


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

    # ── Background thread ─────────────────────────────────────────────────────

    def _run(self, input_path: Path, output_path: Path) -> None:
        t0 = time.monotonic()
        tmp_css_path: str | None = None
        try:
            text = input_path.read_text(encoding="utf-8")
            meta, text = parse_frontmatter(text)

            theme_name = meta.get("theme", self.theme)
            ratio      = meta.get("ratio", self.ratio)
            # Logo from frontmatter: restrict to the document directory to
            # prevent an untrusted .md file from exfiltrating arbitrary files.
            logo_path  = self.logo_path
            if not logo_path and meta.get("logo"):
                safe = _safe_subpath(input_path.parent, meta["logo"])
                if safe:
                    logo_path = safe

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
            if meta.get("custom_css"):
                extra_css = _safe_subpath(input_path.parent, meta["custom_css"])
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

            css = build_css(theme, width, height, logo_b64)

            slides = split_slides(text)
            if not slides:
                raise ValueError("No slides found — separate slides with ---")

            html, slide_info = md_to_html_slides(slides, css, logo_b64, meta)

            html_path = output_path.with_suffix(".html")
            html_path.write_text(html, encoding="utf-8")

            if _weasyprint is None:
                raise ImportError(
                    "WeasyPrint is not installed — cannot generate PDF."
                )
            wp_doc    = _weasyprint.HTML(
                string=html, base_url=str(input_path.parent)
            )
            pdf_bytes = wp_doc.write_pdf()
            output_path.write_bytes(pdf_bytes)

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
        finally:
            if tmp_css_path is not None:
                try:
                    Path(tmp_css_path).unlink(missing_ok=True)
                except OSError:
                    pass

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
