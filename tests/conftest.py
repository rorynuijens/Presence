"""
conftest.py — pytest configuration for the Presence test suite.

Pins GDK_BACKEND before any GTK import so a test run never opens a window on
the developer's desktop.

Note that GTK 4 dropped the offscreen backend, so this does NOT make
widget-level tests possible: GDK logs "No such backend", Gtk.init_check()
still returns True, and constructing any widget then segfaults.  Non-widget
GObjects such as GtkTextBuffer work fine, so test widget logic by calling the
methods against those rather than by building the widget.
"""
import os
os.environ.setdefault("GDK_BACKEND", "offscreen")
