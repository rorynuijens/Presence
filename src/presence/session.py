"""
session.py — Persist simple session state between launches.

Stores a JSON file at:
    $XDG_CONFIG_HOME/presence/session.json

All writes are atomic (write to tempfile → os.replace) and protected by a
per-process lock so that multiple windows cannot corrupt each other's
session data (fixes #95 / #96).
"""

import fcntl
import logging

log = logging.getLogger(__name__)
import json
import os
import tempfile
import threading
from pathlib import Path

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
        "window_width":     int(state.get("width",     1400)),
        "window_height":    int(state.get("height",    860)),
        "window_maximized": bool(state.get("maximized", False)),
    })


def load_window_state() -> dict:
    try:
        data = _load_raw()
        return {
            "width":     int(data.get("window_width",     1400)),
            "height":    int(data.get("window_height",    860)),
            "maximized": bool(data.get("window_maximized", False)),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"width": 1400, "height": 860, "maximized": False}


# ── Editor preferences ────────────────────────────────────────────────────────

def save_editor_prefs(prefs: dict) -> None:
    _update({
        "editor_theme":            prefs.get("theme",            "light"),
        "editor_ratio":            prefs.get("ratio",            "16:9"),
        "editor_logo":             prefs.get("logo",             ""),
        "editor_font_size":        int(prefs.get("font_size",    13)),
        "editor_syntax_highlight": bool(prefs.get("syntax_highlight", True)),
        "editor_line_numbers":     bool(prefs.get("line_numbers",     True)),
        "editor_highlight_line":   bool(prefs.get("highlight_line",   True)),
        "editor_auto_indent":      bool(prefs.get("auto_indent",      True)),
        "editor_spaces_tabs":      bool(prefs.get("spaces_tabs",      True)),
        "editor_line_length":      int(prefs.get("line_length",       64)),
    })


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
