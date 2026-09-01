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
