"""
utils.py — Small shared helpers with no domain-specific dependencies.
"""

import base64
import sys
from pathlib import Path

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
        print(
            f"  Warning: unsupported logo format '{logo_path.suffix}'. "
            f"Supported: {list(_MIME_MAP)}",
            file=sys.stderr,
        )
        return None

    # Check size before reading to avoid allocating a huge buffer.
    try:
        size = logo_path.stat().st_size
    except OSError:
        return None
    if size > _MAX_LOGO_BYTES:
        print(
            f"  Warning: logo file too large ({size // 1024} KB > "
            f"{_MAX_LOGO_BYTES // 1024} KB limit): {logo_path}",
            file=sys.stderr,
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
