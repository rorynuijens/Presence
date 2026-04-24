"""
test_session.py — Unit tests for the session persistence module.

Run with:  pytest test_session.py
"""
import json
import tempfile
from pathlib import Path
import pytest

# Patch _config_dir and _data_dir to use a tmp directory


@pytest.fixture(autouse=True)
def tmp_session(monkeypatch, tmp_path):
    """Redirect all session I/O to a temporary directory."""
    import presence.session as s
    monkeypatch.setattr(s, "_config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(s, "_data_dir",   lambda: tmp_path / "data")
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()


def test_save_load_last_file(tmp_path):
    from presence.session import save_last_file, load_last_file
    p = tmp_path / "test.md"
    p.write_text("# Hello")
    save_last_file(p)
    assert load_last_file() == p.resolve()


def test_load_last_file_missing_returns_none():
    from presence.session import load_last_file
    assert load_last_file() is None


def test_save_load_window_state():
    from presence.session import save_window_state, load_window_state
    save_window_state({"width": 1200, "height": 900, "maximized": True})
    s = load_window_state()
    assert s["width"] == 1200
    assert s["height"] == 900
    assert s["maximized"] is True


def test_window_state_defaults():
    from presence.session import load_window_state
    s = load_window_state()
    assert s["width"] == 1400
    assert s["height"] == 860
    assert s["maximized"] is False


def test_save_load_editor_prefs():
    from presence.session import save_editor_prefs, load_editor_prefs
    save_editor_prefs({
        "theme": "tokyo", "ratio": "4:3", "logo": "/tmp/logo.png",
        "font_size": 16, "syntax_highlight": False, "line_numbers": True,
        "highlight_line": False, "auto_indent": True, "spaces_tabs": False,
    })
    p = load_editor_prefs()
    assert p["theme"] == "tokyo"
    assert p["ratio"] == "4:3"
    assert p["font_size"] == 16
    assert p["syntax_highlight"] is False


def test_editor_prefs_defaults():
    from presence.session import load_editor_prefs
    p = load_editor_prefs()
    assert p["theme"] == "light"
    assert p["ratio"] == "16:9"
    assert p["font_size"] == 13


def test_save_load_presentation_prefs():
    from presence.session import save_presentation_prefs, load_presentation_prefs
    save_presentation_prefs({
        "timer_minutes": 20,
        "auto_convert": True,
        "presenter_notes_font": 28,
    })
    p = load_presentation_prefs()
    assert p["timer_minutes"] == 20
    assert p["auto_convert"] is True
    assert p["presenter_notes_font"] == 28


def test_presentation_prefs_all_keys_in_fallback():
    """Fallback must include presenter_notes_font — regression for missing key bug."""
    from presence.session import load_presentation_prefs
    p = load_presentation_prefs()
    assert "timer_minutes" in p
    assert "auto_convert" in p
    assert "presenter_notes_font" in p


def test_recent_files_round_trip(tmp_path):
    from presence.session import save_recent_file, load_recent_files
    p1 = tmp_path / "a.md"; p1.write_text("a")
    p2 = tmp_path / "b.md"; p2.write_text("b")
    save_recent_file(p1)
    save_recent_file(p2)
    recents = load_recent_files()
    # p2 saved last so should be first
    assert str(p2.resolve()) == recents[0]
    assert str(p1.resolve()) == recents[1]


def test_recent_files_deduplication(tmp_path):
    from presence.session import save_recent_file, load_recent_files
    p = tmp_path / "x.md"; p.write_text("x")
    save_recent_file(p)
    save_recent_file(p)
    save_recent_file(p)
    assert load_recent_files().count(str(p.resolve())) == 1


def test_recent_files_nonexistent_filtered(tmp_path):
    """load_recent_files must not return paths that no longer exist."""
    from presence.session import save_recent_file, load_recent_files
    p = tmp_path / "gone.md"; p.write_text("x")
    save_recent_file(p)
    p.unlink()
    assert load_recent_files() == []


def test_atomic_write_does_not_corrupt(tmp_path):
    """Two rapid saves should not corrupt the session file."""
    from presence.session import save_window_state, load_window_state
    import threading
    errors = []
    def _save(w):
        try:
            save_window_state({"width": w, "height": 860, "maximized": False})
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=_save, args=(w,)) for w in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors
    s = load_window_state()
    assert s["width"] in range(10)


def test_recovery_path_deterministic(tmp_path):
    from presence.session import recovery_path_for
    p = tmp_path / "pres.md"
    r1 = recovery_path_for(p)
    r2 = recovery_path_for(p)
    assert r1 == r2


def test_recovery_path_different_files(tmp_path):
    from presence.session import recovery_path_for
    p1 = tmp_path / "a.md"
    p2 = tmp_path / "b.md"
    assert recovery_path_for(p1) != recovery_path_for(p2)
