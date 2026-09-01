"""
conftest.py — pytest configuration for the Presence test suite.

Widget tests are possible.  They were not, for a long time, because this file
pinned ``GDK_BACKEND=offscreen`` — a backend GTK 4 removed.  GDK logged "No
such backend", ``Gtk.init_check()`` still returned True, and the first widget
constructed then segfaulted.  That read as a GTK 4 limitation; it was this
file's doing.  So the pin is gone.

What the pin was there for — a test run must never throw a window onto the
desktop — is handled instead by never presenting one.  A GTK widget, toplevels
included, is neither mapped nor visible until something calls ``present()``.
Tests here construct and drive; they do not present.  A class that presents a
window of its own from ``__init__`` (``PresenterWindow`` does, for the
slideshow) is tested with that collaborator replaced.

Use the ``gtk`` fixture for anything that builds a widget.  It skips rather
than letting GDK fail deep inside a constructor when there is no display to
build against.  To run those tests where no compositor exists, start a
headless one first — ``broadwayd`` or ``mutter --headless`` — and point
``WAYLAND_DISPLAY`` or ``DISPLAY`` at it.
"""
import os

import pytest


def _display_available() -> bool:
    """True when GDK has something to build widgets against."""
    # The backend GTK 4 dropped.  Honoured rather than overridden, so that
    # setting it deliberately still skips the widget tests instead of
    # segfaulting the run.
    if os.environ.get("GDK_BACKEND") == "offscreen":
        return False
    return any(os.environ.get(var) for var in
               ("WAYLAND_DISPLAY", "DISPLAY", "BROADWAY_DISPLAY"))


@pytest.fixture(scope="session")
def gtk():
    """
    An initialised GTK and Libadwaita, or a skip when there is no display.

    Returns the ``Gtk`` module so a test can say ``gtk.Box()`` without
    repeating the ``require_version`` dance.
    """
    if not _display_available():
        pytest.skip(
            "no display for widget tests — run under a compositor "
            "(broadwayd, mutter --headless) or unset GDK_BACKEND=offscreen"
        )
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    if not Gtk.init_check():
        pytest.skip("Gtk.init_check() failed — no usable GDK backend")
    Adw.init()
    return Gtk


@pytest.fixture(autouse=True)
def isolated_session(tmp_path, monkeypatch):
    """
    Keep every test out of the user's real session file.

    Building a ``MainWindow`` writes one: ``__init__`` sets the theme-panel
    button active, which runs its toggle handler, which saves the window
    state.  So a test that merely constructs a window rewrote
    ``~/.config/presence/session.json`` — harmless in content, but it is the
    user's file and a Presence they had open would have its state clobbered.

    Autouse, so no future test has to remember.  ``test_session.py`` redirects
    these two the same way for itself; applying it twice is harmless.
    """
    try:
        import presence.session as session
    except ImportError:                      # gi missing; nothing to isolate
        return
    config = tmp_path / "session-config"
    data   = tmp_path / "session-data"
    config.mkdir()
    data.mkdir()
    monkeypatch.setattr(session, "_config_dir", lambda: config)
    monkeypatch.setattr(session, "_data_dir", lambda: data)


# Serial number for test application ids — see app_factory.
_app_serial = 0


@pytest.fixture
def app_factory(gtk):
    """
    Build registered ``Application`` instances for tests that need real actions.

    An application only reports its actions and accelerators once it is
    registered, and registering exports an object on the session bus — so each
    one gets a unique id, and NON_UNIQUE so a test run never talks to a
    Presence the user has open.  Windows made here are never presented.

    The id comes from a counter that outlives the fixture.  It used to be
    ``len(made)`` plus ``id(made) & 0xffff``, and both restart with every
    test: ``made`` is a fresh list each time, and CPython hands a freed
    address straight back, so two tests in one run could be given the same
    id and the second would fail to register with "An object is already
    exported".  It struck perhaps one run in three, wherever the collision
    happened to land.
    """
    from gi.repository import Gio

    from presence.application import APP_ID, Application

    made = []

    def build():
        global _app_serial
        _app_serial += 1
        app = Application()
        app.set_application_id(f"{APP_ID}.Test{_app_serial}")
        app.set_flags(Gio.ApplicationFlags.NON_UNIQUE
                      | Gio.ApplicationFlags.HANDLES_OPEN)
        app.register(None)
        made.append(app)
        return app

    yield build
    for app in made:
        for window in list(app.get_windows()):
            window.destroy()
