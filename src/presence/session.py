"""
session.py — Persist simple session state between launches.

Stores a JSON file at:
    $XDG_CONFIG_HOME/presence/session.json

All writes are atomic (write to tempfile → os.replace) and protected by a
per-process lock so that multiple windows cannot corrupt each other's
session data (fixes #95 / #96).
"""

import configparser
import logging

log = logging.getLogger(__name__)
import json
import os
import tempfile
import threading
from pathlib import Path

try:
    import gi
    gi.require_version("Secret", "1")
except Exception:
    pass

# Process-level lock for all session reads and writes.
# This serialises concurrent access from multiple MainWindow instances
# within the same process, eliminating the TOCTOU race (fixes #95).
_session_lock = threading.Lock()


def _config_dir() -> Path:
    try:
        from gi.repository import GLib
        return Path(GLib.get_user_config_dir()) / "presence"
    except Exception:
        base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        return Path(base) / "presence"


def _data_dir() -> Path:
    try:
        from gi.repository import GLib
        return Path(GLib.get_user_data_dir()) / "presence"
    except Exception:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
        return Path(base) / "presence"


def _session_file() -> Path:
    return _config_dir() / "session.json"


_MAX_RECENT = 10   # maximum number of recent files to persist


# ── Last file ─────────────────────────────────────────────────────────────────

def save_last_file(path: Path) -> None:
    _update({"last_file": str(path.resolve())})


def load_last_file() -> Path | None:
    try:
        raw = _load_raw().get("last_file")
        if not raw:
            return None
        path = Path(raw)
        return path if (path.is_absolute() and path.exists() and path.is_file()) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


# ── Window state ──────────────────────────────────────────────────────────────

def save_window_state(state: dict) -> None:
    _update({
        "window_width":        int(state.get("width",              1400)),
        "window_height":       int(state.get("height",             860)),
        "window_maximized":    bool(state.get("maximized",         False)),
        "window_theme_panel":  bool(state.get("theme_panel_visible", False)),
        "window_canvas":       bool(state.get("canvas_visible",    True)),
        "window_canvas_pos":   int(state.get("canvas_position",    0)),
    })


def load_window_state() -> dict:
    try:
        data = _load_raw()
        return {
            "width":               int(data.get("window_width",       1400)),
            "height":              int(data.get("window_height",      860)),
            "maximized":           bool(data.get("window_maximized",  False)),
            "theme_panel_visible": bool(data.get("window_theme_panel", False)),
            "canvas_visible":      bool(data.get("window_canvas",     True)),
            "canvas_position":     int(data.get("window_canvas_pos",  0)),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"width": 1400, "height": 860, "maximized": False,
                "theme_panel_visible": False, "canvas_visible": True,
                "canvas_position": 0}


# ── Editor preferences ────────────────────────────────────────────────────────

# Callers may pass any subset of these keys; only provided keys are written.
# This lets ThemePanel and SettingsDialog each own their fields without
# needing to pre-read the fields they don't control.
_EDITOR_KEY_MAP: dict[str, str] = {
    "theme":            "editor_theme",
    "ratio":            "editor_ratio",
    "logo":             "editor_logo",
    "font_size":        "editor_font_size",
    "syntax_highlight": "editor_syntax_highlight",
    "line_numbers":     "editor_line_numbers",
    "highlight_line":   "editor_highlight_line",
    "auto_indent":      "editor_auto_indent",
    "spaces_tabs":      "editor_spaces_tabs",
    "line_length":      "editor_line_length",
}


def save_editor_prefs(prefs: dict) -> None:
    _update({_EDITOR_KEY_MAP[k]: v for k, v in prefs.items() if k in _EDITOR_KEY_MAP})


def load_editor_prefs() -> dict:
    try:
        data = _load_raw()
        return {
            "theme":            str(data.get("editor_theme",            "light")),
            "ratio":            str(data.get("editor_ratio",            "16:9")),
            "logo":             str(data.get("editor_logo",             "")),
            "font_size":        int(data.get("editor_font_size",        13)),
            # line_length must be in the try branch too — omitting it here
            # caused the saved value to be ignored whenever the session file
            # existed, silently falling back to the except-branch default.
            "line_length":      int(data.get("editor_line_length",      64)),
            "syntax_highlight": bool(data.get("editor_syntax_highlight", True)),
            "line_numbers":     bool(data.get("editor_line_numbers",     True)),
            "highlight_line":   bool(data.get("editor_highlight_line",   True)),
            "auto_indent":      bool(data.get("editor_auto_indent",      True)),
            "spaces_tabs":      bool(data.get("editor_spaces_tabs",      True)),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {
            "theme": "light", "ratio": "16:9", "logo": "", "font_size": 13,
            "syntax_highlight": True, "line_numbers": True,
            "highlight_line": True, "auto_indent": True, "spaces_tabs": True,
            "line_length": 64,
        }


# ── Recent files ──────────────────────────────────────────────────────────────

def save_recent_file(path: Path) -> None:
    p = str(Path(path).resolve())
    try:
        with _session_lock:
            data    = _load_raw_unlocked()
            recents = data.get("recent_files", [])
            recents = [r for r in recents if r != p]
            recents.insert(0, p)
            _write_unlocked(data | {"recent_files": recents[:_MAX_RECENT]})
    except (OSError, json.JSONDecodeError):
        pass


def load_recent_files() -> list[str]:
    """
    Return recent file paths that are absolute and still exist on disk.
    A single existence check here avoids the TOCTOU inconsistency where
    _rebuild_recent_menu did a second Path.exists() call after this one.
    """
    try:
        data = _load_raw()
        recents = data.get("recent_files", [])
        return [
            r for r in recents
            if isinstance(r, str)
            and Path(r).is_absolute()
            and Path(r).exists()
        ][:_MAX_RECENT]
    except (OSError, json.JSONDecodeError):
        return []


# ── Recovery ──────────────────────────────────────────────────────────────────

def persist_logo(src: Path) -> Path:
    """Copy *src* into the app data dir and return the copy's path.

    Logos selected via the portal may come from outside the Flatpak sandbox
    boundary and their paths won't be accessible in future sessions.  Keeping
    a private copy inside the app data dir ensures the logo always loads.
    """
    logo_dir = _data_dir() / "logo"
    logo_dir.mkdir(parents=True, exist_ok=True)
    dest = logo_dir / src.name
    import shutil
    shutil.copy2(src, dest)
    return dest


def recovery_dir() -> Path:
    return _data_dir() / "recovery"


def recovery_path_for(original: Path) -> Path:
    """
    Return a collision-resistant recovery path for *original*.

    Uses SHA-256 (truncated) instead of SHA-1 (fixes #66).
    """
    import hashlib
    digest = hashlib.sha256(str(original.resolve()).encode()).hexdigest()[:16]
    safe_name = original.stem[:40]
    return recovery_dir() / f"{safe_name}_{digest}.md"


def delete_recovery_file(original: Path) -> None:
    """
    Delete the recovery file for *original* if it exists.

    Called by _save() after a successful write so orphaned recovery files
    do not accumulate on disk (fixes #59).
    """
    try:
        recovery_path_for(original).unlink(missing_ok=True)
    except OSError:
        pass


# ── Presentation preferences ──────────────────────────────────────────────────

def save_presentation_prefs(prefs: dict) -> None:
    """Persist presentation-level preferences (timer target, auto-convert)."""
    _update({
        "pres_timer_minutes":      int(prefs.get("timer_minutes",        0)),
        "pres_auto_convert":       bool(prefs.get("auto_convert",        False)),
        "pres_notes_font":         int(prefs.get("presenter_notes_font", 22)),
        "pres_speaking_rate":      int(prefs.get("speaking_rate",        110)),
    })


def load_presentation_prefs() -> dict:
    """Return presentation preferences with safe defaults."""
    try:
        data = _load_raw()
        return {
            # 0 = no target (count-up only)
            "timer_minutes":        int(data.get("pres_timer_minutes",      0)),
            "auto_convert":         bool(data.get("pres_auto_convert",      False)),
            "presenter_notes_font": int(data.get("pres_notes_font",         22)),
            "speaking_rate":        int(data.get("pres_speaking_rate",      110)),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"timer_minutes": 0, "auto_convert": False,
                "presenter_notes_font": 22, "speaking_rate": 110}


# ── AI preferences ───────────────────────────────────────────────────────────

def save_ai_prefs(prefs: dict) -> None:
    _update({
        "ai_image_style": str(prefs.get("image_style", "Photorealistic")),
        "ai_gemini_model": str(prefs.get("gemini_model", "gemini-2.5-flash-image")),
    })


def load_ai_prefs() -> dict:
    try:
        data = _load_raw()
        return {
            "image_style":  str(data.get("ai_image_style", "Photorealistic")),
            "gemini_model": str(data.get("ai_gemini_model", "gemini-2.5-flash-image")),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"image_style": "Photorealistic", "gemini_model": "gemini-2.5-flash-image"}


# ── API keys ──────────────────────────────────────────────────────────────────

def _config_ini_file() -> Path:
    return _config_dir() / "config.ini"


_SECRET_SCHEMA = None


def _get_secret_schema():
    global _SECRET_SCHEMA
    if _SECRET_SCHEMA is None:
        from gi.repository import Secret
        _SECRET_SCHEMA = Secret.Schema.new(
            "io.gitlab.gtk4_apps1.Presence",
            Secret.SchemaFlags.NONE,
            {"key_name": Secret.SchemaAttributeType.STRING},
        )
    return _SECRET_SCHEMA


def save_api_keys(claude_key: str, gemini_key: str) -> bool:
    """Store API keys in GNOME Keyring (called from Settings — user-initiated).

    Falls back to config.ini if the keyring is unavailable.  Any brief block
    here is expected UX: the user just clicked Save, so a keyring-unlock
    dialog is appropriate.

    Returns True if the keys were stored in the keyring, False if the plaintext
    config.ini fallback was used (caller should warn the user).
    """
    try:
        from gi.repository import Secret
        schema = _get_secret_schema()
        Secret.password_store_sync(
            schema, {"key_name": "claude_api_key"},
            Secret.COLLECTION_DEFAULT, "Presence Claude API Key", claude_key, None,
        )
        Secret.password_store_sync(
            schema, {"key_name": "gemini_api_key"},
            Secret.COLLECTION_DEFAULT, "Presence Gemini API Key", gemini_key, None,
        )
        _remove_api_keys_ini()
        return True
    except Exception as e:
        log.warning("Keyring unavailable, falling back to config.ini: %s", e)
        _save_api_keys_ini(claude_key, gemini_key)
        return False


def load_api_keys() -> tuple[str, str]:
    """Return (claude_key, gemini_key) from GNOME Keyring, falling back to config.ini.

    Reads only — never writes.  In a normal logged-in GNOME session the
    default keyring is already unlocked so password_lookup_sync returns in
    milliseconds without showing any dialog.
    """
    try:
        from gi.repository import Secret
        schema = _get_secret_schema()
        claude_key = Secret.password_lookup_sync(
            schema, {"key_name": "claude_api_key"}, None)
        gemini_key = Secret.password_lookup_sync(
            schema, {"key_name": "gemini_api_key"}, None)
        if claude_key is not None or gemini_key is not None:
            return claude_key or "", gemini_key or ""
    except Exception as e:
        log.warning("Keyring unavailable, falling back to config.ini: %s", e)
    return _load_api_keys_ini()


def _remove_api_keys_ini() -> None:
    """Remove the [api_keys] section from config.ini after a successful keyring save."""
    config = configparser.ConfigParser()
    ini_path = _config_ini_file()
    if not ini_path.exists():
        return
    try:
        config.read(str(ini_path), encoding="utf-8")
        if config.has_section("api_keys"):
            config.remove_section("api_keys")
            with open(ini_path, "w", encoding="utf-8") as f:
                config.write(f)
    except OSError as e:
        log.warning("Failed to clean up config.ini after keyring save: %s", e)


def _save_api_keys_ini(claude_key: str, gemini_key: str) -> None:
    config = configparser.ConfigParser()
    ini_path = _config_ini_file()
    if ini_path.exists():
        try:
            config.read(str(ini_path), encoding="utf-8")
        except Exception:
            pass
    if not config.has_section("api_keys"):
        config.add_section("api_keys")
    config.set("api_keys", "claude_api_key", claude_key)
    config.set("api_keys", "gemini_api_key", gemini_key)
    try:
        cfg_dir = _config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        # Write atomically to a temp file, set restrictive permissions, then
        # rename — prevents a partial write being read and keeps the file
        # owner-readable only (0o600) since it contains plaintext API keys.
        fd, tmp_path = tempfile.mkstemp(dir=cfg_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                config.write(f)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, ini_path)
    except OSError as e:
        log.warning("Failed to save API keys to config.ini: %s", e)


def _load_api_keys_ini() -> tuple[str, str]:
    config = configparser.ConfigParser()
    ini_path = _config_ini_file()
    if ini_path.exists():
        try:
            config.read(str(ini_path), encoding="utf-8")
        except Exception:
            pass
    return (
        config.get("api_keys", "claude_api_key", fallback=""),
        config.get("api_keys", "gemini_api_key", fallback=""),
    )



# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_raw() -> dict:
    with _session_lock:
        return _load_raw_unlocked()


def _load_raw_unlocked() -> dict:
    """Read session JSON without acquiring the lock (caller must hold it)."""
    sf = _session_file()
    if sf.exists():
        try:
            return json.loads(sf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _update(changes: dict) -> None:
    """Atomically merge *changes* into the session file (thread-safe)."""
    with _session_lock:
        try:
            data = _load_raw_unlocked()
            _write_unlocked(data | changes)
        except OSError as e:
            log.warning("Session update failed: %s", e)


def _write_unlocked(data: dict) -> None:
    """Write *data* to the session file atomically (caller must hold lock)."""
    try:
        cfg_dir = _config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2).encode("utf-8")
        sf = _session_file()
        fd, tmp_path = tempfile.mkstemp(dir=cfg_dir, suffix=".tmp")
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(tmp_path, sf)
    except OSError:
        pass
