"""
theme_loader.py — Discover and load Theme objects from the filesystem.

Theme packages are directories containing a theme.json file:

    MyTheme/
    ├── theme.json
    ├── theme.css
    ├── fonts/
    │   └── MyFont-Regular.woff2
    └── assets/

User themes live in $XDG_DATA_HOME/presence/themes/.
Built-in themes are defined in themes.py.
"""

import dataclasses
import json
import re
import shutil
import sys
import threading
import zipfile
from pathlib import Path

from gi.repository import GLib

from .themes import BUILTIN_THEMES, Theme


# ── Paths ─────────────────────────────────────────────────────────────────────

def user_themes_dir() -> Path:
    """Return $XDG_DATA_HOME/presence/themes/, creating it if needed."""
    d = Path(GLib.get_user_data_dir()) / "presence" / "themes"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Theme cache (#39) ─────────────────────────────────────────────────────────
# Cache the result of load_all_themes() keyed by (extra_dir, user_dir_mtime).
# Invalidated automatically when the user themes directory is modified (e.g.
# after install/uninstall).  The lock makes it safe for concurrent background
# threads (converter, thumbnail renderer) to call load_all_themes() in parallel.

_cache_lock    = threading.Lock()
_cached_result: dict | None = None
_cached_mtime:  float       = -1.0
_cached_extra:  Path | None = None


def _user_dir_mtime() -> float:
    """Return the mtime of the user themes directory (0.0 if absent)."""
    try:
        return user_themes_dir().stat().st_mtime
    except OSError:
        return 0.0


def invalidate_theme_cache() -> None:
    """Force the next call to load_all_themes() to re-scan the filesystem.

    Called automatically by install_theme() and uninstall_theme().
    """
    global _cached_result
    with _cache_lock:
        _cached_result = None


# ── Public API ────────────────────────────────────────────────────────────────

def load_all_themes(extra_dir: Path | None = None) -> dict[str, Theme]:
    """
    Return a merged dict of all available themes, keyed by slug.

    Results are cached and only re-scanned when the user themes directory
    mtime changes or extra_dir changes (fixes #39).  Each returned value is
    a distinct Theme instance (via dataclasses.replace) so callers may
    safely mutate it without affecting BUILTIN_THEMES or the cache.

    Thread safety: the cache check is done under the lock.  The expensive
    filesystem scan runs outside the lock (so other threads are not blocked).
    Before writing, we re-check under the lock so that if two threads both
    missed the cache simultaneously, only one scan result is kept and neither
    is discarded silently.
    """
    global _cached_result, _cached_mtime, _cached_extra

    current_mtime = _user_dir_mtime()

    # Fast path: return cached copies if still valid
    with _cache_lock:
        if (
            _cached_result is not None
            and _cached_mtime == current_mtime
            and _cached_extra == extra_dir
        ):
            return {
                slug: dataclasses.replace(t)
                for slug, t in _cached_result.items()
            }

    # ── Build fresh result (outside lock so other threads are not blocked) ────
    result: dict[str, Theme] = {
        slug: dataclasses.replace(t) for slug, t in BUILTIN_THEMES.items()
    }

    for theme_dir in user_themes_dir().iterdir():
        if theme_dir.is_dir():
            try:
                t = load_theme_from_dir(theme_dir)
                result[t.slug] = t
            except Exception as exc:
                print(f"Theme loader: skipping '{theme_dir.name}': {exc}",
                      file=sys.stderr)

    if extra_dir and extra_dir.is_dir():
        for theme_dir in extra_dir.iterdir():
            if theme_dir.is_dir():
                try:
                    t = load_theme_from_dir(theme_dir)
                    result[t.slug] = t
                except Exception as exc:
                    print(f"Theme loader: skipping '{theme_dir.name}': {exc}",
                          file=sys.stderr)

    # ── Commit to cache only if no newer result was written while we scanned ──
    with _cache_lock:
        # Re-check: another thread may have written a fresher result already
        if (
            _cached_result is None
            or _cached_mtime != current_mtime
            or _cached_extra != extra_dir
        ):
            _cached_result = result
            _cached_mtime  = current_mtime
            _cached_extra  = extra_dir
        else:
            # A concurrent scan finished first; use its (identical) result
            result = _cached_result

    # Return copies so callers cannot mutate the cache entries
    return {slug: dataclasses.replace(t) for slug, t in result.items()}


def load_theme_from_dir(theme_dir: Path) -> Theme:
    """
    Parse a theme package directory and return a Theme object.

    Raises ValueError if theme.json is missing or malformed.
    """
    json_path = theme_dir / "theme.json"
    if not json_path.exists():
        raise ValueError(f"No theme.json in {theme_dir}")

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in theme.json: {exc}") from exc

    slug = _slugify(data.get("slug") or data.get("name", theme_dir.name))

    def _callout(key: str, default: tuple) -> tuple:
        v = data.get(key, default)
        if isinstance(v, (list, tuple)) and len(v) == 3:
            return tuple(v)
        return default

    theme = Theme(
        name=        data.get("name",        theme_dir.name),
        slug=        slug,
        author=      data.get("author",      "Unknown"),
        version=     str(data.get("version", "1.0")),
        description= data.get("description", ""),
        bg=          data.get("bg",          "#ffffff"),
        fg=          data.get("fg",          "#1a1a2e"),
        accent=      data.get("accent",      "#E17000"),
        accent2=     data.get("accent2",     ""),
        heading_color=data.get("heading_color", ""),
        code_bg=     data.get("code_bg",     "#f0f4f8"),
        title_bg=    data.get("title_bg",    "#1a1a2e"),
        title_fg=    data.get("title_fg",    "#ffffff"),
        title_accent=data.get("title_accent",""),
        body_font=   data.get("body_font",   Theme.body_font),
        heading_font=data.get("heading_font",""),
        mono_font=   data.get("mono_font",   Theme.mono_font),
        base_size=   int(data.get("base_size", 34)),
        pygments_style=data.get("pygments_style", "friendly"),
        callout_tip=    _callout("callout_tip",     Theme.callout_tip),
        callout_info=   _callout("callout_info",    Theme.callout_info),
        callout_warning=_callout("callout_warning", Theme.callout_warning),
        callout_danger= _callout("callout_danger",  Theme.callout_danger),
        # Per-theme callout icons (fixes #99)
        callout_icon_tip=    data.get("callout_icon_tip",     Theme.callout_icon_tip),
        callout_icon_info=   data.get("callout_icon_info",    Theme.callout_icon_info),
        callout_icon_warning=data.get("callout_icon_warning", Theme.callout_icon_warning),
        callout_icon_danger= data.get("callout_icon_danger",  Theme.callout_icon_danger),
    )

    css_path = theme_dir / "theme.css"
    if css_path.exists():
        theme.custom_css_path = str(css_path.resolve())

    fonts_dir = theme_dir / "fonts"
    if fonts_dir.is_dir():
        theme._font_face_css = _build_font_face_css(
            fonts_dir,
            data.get("body_font", ""),
            data.get("heading_font", ""),
        )

    return theme


def install_theme(source: Path, themes_dir: Path | None = None) -> Theme:
    """
    Install a theme from *source* (directory or .zip) into *themes_dir*.
    """
    if themes_dir is None:
        themes_dir = user_themes_dir()

    if source.suffix.lower() == ".zip":
        result = _install_from_zip(source, themes_dir)
    elif source.is_dir():
        result = _install_from_dir(source, themes_dir)
    else:
        raise ValueError(f"Source must be a directory or .zip file: {source}")

    invalidate_theme_cache()
    return result


def uninstall_theme(slug: str, themes_dir: Path | None = None) -> None:
    """Remove the theme directory for *slug* from *themes_dir*."""
    if slug in BUILTIN_THEMES:
        raise ValueError(f"Cannot uninstall built-in theme '{slug}'")

    if themes_dir is None:
        themes_dir = user_themes_dir()

    for theme_dir in themes_dir.iterdir():
        if not theme_dir.is_dir():
            continue
        try:
            t = load_theme_from_dir(theme_dir)
            if t.slug == slug:
                shutil.rmtree(theme_dir)
                invalidate_theme_cache()
                return
        except Exception:
            continue

    raise ValueError(f"Theme '{slug}' not found in {themes_dir}")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _install_from_dir(source: Path, themes_dir: Path) -> Theme:
    theme = load_theme_from_dir(source)
    dest  = themes_dir / source.name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    return load_theme_from_dir(dest)


def _install_from_zip(source: Path, themes_dir: Path) -> Theme:
    with zipfile.ZipFile(source, "r") as zf:
        names = zf.namelist()
        if not names:
            raise ValueError("Empty zip file")
        top = Path(names[0]).parts[0]
        dest = themes_dir / top
        if dest.exists():
            shutil.rmtree(dest)
        zf.extractall(themes_dir)
    return load_theme_from_dir(dest)


def _build_font_face_css(fonts_dir: Path,
                          body_font_name: str,
                          heading_font_name: str) -> str:
    """
    Generate @font-face rules for font files in *fonts_dir*.

    Supported formats: .woff2, .woff, .ttf, .otf

    File naming: FontName-WeightStyle.ext  e.g. MyFont-BoldItalic.woff2
    Combined variant names like "BoldItalic" are parsed by checking for
    known substrings (longest-match first) rather than splitting on
    whitespace, fixing the silent weight=400/style=normal bug (#58).
    """
    # Check for known substrings longest-first to handle combined names
    # like "BoldItalic", "SemiBold", "ExtraLight", etc.
    _WEIGHT_TOKENS = [
        ("extralight", 200), ("extrabold", 800),
        ("semibold", 600),   ("medium", 500),
        ("light", 300),      ("black", 900),
        ("bold", 700),       ("thin", 100),
        ("regular", 400),
    ]
    _STYLE_TOKENS = [("oblique", "oblique"), ("italic", "italic")]

    _fmt_map = {".woff2": "woff2", ".woff": "woff",
                ".ttf": "truetype", ".otf": "opentype"}

    rules = []
    for font_file in sorted(fonts_dir.iterdir()):
        if font_file.suffix.lower() not in _fmt_map:
            continue

        stem  = font_file.stem
        parts = stem.rsplit("-", 1)
        raw_family = parts[0].replace("-", " ") if len(parts) == 2 else stem
        variant    = parts[1].lower() if len(parts) == 2 else "regular"

        # Sanitise the family name: allow only alphanumerics, spaces, and
        # hyphens.  A font file named with quotes, semicolons, or braces
        # would otherwise inject arbitrary CSS into the @font-face block.
        family = re.sub(r"[^a-zA-Z0-9 \-]", "", raw_family).strip()
        if not family:
            family = "UnknownFont"

        weight = 400
        style  = "normal"

        # Longest-match substring search (fixes #58)
        for token, w in _WEIGHT_TOKENS:
            if token in variant:
                weight = w
                break

        for token, s in _STYLE_TOKENS:
            if token in variant:
                style = s
                break

        fmt = _fmt_map[font_file.suffix.lower()]
        url = font_file.resolve().as_uri()

        rules.append(
            f"@font-face {{\n"
            f"  font-family: '{family}';\n"
            f"  font-style: {style};\n"
            f"  font-weight: {weight};\n"
            f"  src: url({url}) format('{fmt}');\n"
            f"}}"
        )

    return "\n".join(rules)


def _slugify(text: str) -> str:
    """Convert a display name to a lowercase filesystem-safe slug."""
    return re.sub(r"[^a-z0-9_-]", "-",
                  text.lower().strip()).strip("-") or "theme"
