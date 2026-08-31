"""
utils.py — Small shared helpers with no domain-specific dependencies.
"""

import base64
import logging
from functools import lru_cache
from urllib.parse import urlparse
from pathlib import Path

log = logging.getLogger(__name__)


def safe_subpath(base: Path, untrusted: str) -> "Path | None":
    """
    Resolve *untrusted* relative to *base* and return the Path only if it
    stays within *base*.  Returns None if the resolved path escapes *base*,
    preventing path-traversal via frontmatter (e.g. logo: ../../../etc/passwd).
    Absolute paths in *untrusted* are always rejected.
    """
    if Path(untrusted).is_absolute():
        log.warning("Frontmatter path '%s' is absolute — ignored", untrusted)
        return None
    try:
        candidate = (base / untrusted).resolve()
        base_resolved = base.resolve()
        if candidate == base_resolved or candidate.is_relative_to(base_resolved):
            return candidate
    except Exception:
        pass
    log.warning("Frontmatter path '%s' escapes document directory — ignored", untrusted)
    return None


# Supported logo MIME types; unknown extensions are rejected rather than
# silently mis-labelled as image/png.
_MIME_MAP: dict[str, str] = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg":  "image/svg+xml",
}

# Maximum logo file size (5 MB).  Prevents memory exhaustion when a
# frontmatter logo path points at a large file (e.g. a video file with a
# .png extension, or /dev/urandom on a system with a permissive /dev).
_MAX_LOGO_BYTES = 5 * 1024 * 1024


def encode_logo(logo_path: Path) -> str | None:
    """
    Read *logo_path* and return a base64 data-URI string suitable for
    embedding directly in HTML/CSS.

    Returns None if the file does not exist, has an unsupported extension,
    or exceeds the 5 MB size limit.
    """
    if not logo_path or not logo_path.exists():
        return None

    mime = _MIME_MAP.get(logo_path.suffix.lower())
    if mime is None:
        log.warning(
            "Unsupported logo format '%s'. Supported: %s",
            logo_path.suffix, list(_MIME_MAP),
        )
        return None

    # Check size before reading to avoid allocating a huge buffer.
    try:
        size = logo_path.stat().st_size
    except OSError:
        return None
    if size > _MAX_LOGO_BYTES:
        log.warning(
            "Logo file too large (%d KB > %d KB limit): %s",
            size // 1024, _MAX_LOGO_BYTES // 1024, logo_path,
        )
        return None

    data = base64.b64encode(logo_path.read_bytes()).decode()
    return f"data:{mime};base64,{data}"


def logo_img_tag(logo_b64: str | None) -> str:
    """Return an <img> tag for the logo, or an empty string if no logo."""
    if not logo_b64:
        return ""
    return f'<img class="slide-logo" src="{logo_b64}" alt="logo">'


def progress_bar_html(current: int, total: int) -> str:
    """Return a progress bar HTML fragment for slide *current* of *total*."""
    pct = int(current / total * 100) if total else 0
    return (
        f'<div class="progress-bar-track">'
        f'<div class="progress-bar-fill" style="width:{pct}%"></div>'
        f'</div>'
    )


def timestamp() -> str:
    """Return the current local time as HH:MM:SS (used in watch-mode output)."""
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


def compute_slide_offsets(text: str) -> list[int]:
    """
    Return the character offset into *text* where each slide begins.

    Offsets span the complete document (including any YAML frontmatter) and
    point at the first character of each slide's content (after the ``---``
    separator).  The result has one entry per slide returned by split_slides().
    """
    from .frontmatter import parse_frontmatter
    from .splitter import split_slides

    _meta, body = parse_frontmatter(text)
    fm_end = text.find(body) if body else 0
    if fm_end == -1:
        fm_end = 0

    slides = split_slides(body)
    offsets: list[int] = []
    search_pos = 0
    for slide_text in slides:
        idx = body.find(slide_text.rstrip(), search_pos)
        if idx == -1:
            idx = search_pos
        offsets.append(fm_end + idx)
        search_pos = idx + len(slide_text)
    return offsets


def compute_slide_start_lines(text: str) -> list[int]:
    """
    Return the 0-based line in *text* where each slide's content begins.

    Lines are counted over the whole document, frontmatter included, so the
    numbers can be handed straight to the editor.  One entry per slide, in
    the order split_slides() returns them.
    """
    return [text.count("\n", 0, offset) for offset in compute_slide_offsets(text)]


# ── Intrinsic image size ──────────────────────────────────────────────────────

@lru_cache(maxsize=512)
def _size_for(path: str, mtime: float, nbytes: int) -> "tuple[int, int] | None":
    """Cached header read. Keyed on mtime and size so edits are picked up."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


def image_aspect(src: str, base_url: "str | None") -> "float | None":
    """
    Width / height of *src*, or None when it cannot be determined.

    Pillow reads only the header for .size, and the result is cached against
    the file's mtime, so this stays cheap enough for the live render — which
    re-renders on a 150 ms debounce and would otherwise reopen every image on
    every keystroke.
    """
    if not src or src.startswith("data:"):
        return None
    parsed = urlparse(src)
    if parsed.scheme in ("http", "https"):
        return None      # not worth a network round trip mid-render
    try:
        path = Path(src) if Path(src).is_absolute() else (
            (Path(base_url) / src).resolve() if base_url else Path(src)
        )
        stat = path.stat()
        size = _size_for(str(path), stat.st_mtime, stat.st_size)
    except OSError:
        return None
    if not size or size[1] == 0:
        return None
    return size[0] / size[1]
