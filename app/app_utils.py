"""
app/utils.py — Shared GTK/GDK utility helpers for the app package.

All functions here are pure utilities with no side effects on application
state. They exist to eliminate repeated boilerplate across the codebase.
"""

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, Gio


def png_bytes_to_texture(png: bytes) -> "Gdk.Texture | None":
    """
    Decode *png* bytes into a GDK texture ready for use in Gtk.Picture.

    Returns None on any decoding failure rather than raising, so callers
    can simply check the return value and fall back to clearing the picture.
    """
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(png)
        loader.close()
        pixbuf = loader.get_pixbuf()
        if pixbuf is None:
            return None
        return Gdk.Texture.new_for_pixbuf(pixbuf)
    except Exception:
        return None


def make_file_filter(name: str, *patterns: str) -> Gtk.FileFilter:
    """
    Build a Gtk.FileFilter with *name* and the given glob *patterns*.

    Example::

        f = make_file_filter("Images", "*.png", "*.jpg", "*.svg")
    """
    f = Gtk.FileFilter()
    f.set_name(name)
    for pat in patterns:
        f.add_pattern(pat)
    return f


def make_filter_store(*filters: Gtk.FileFilter) -> Gio.ListStore:
    """
    Wrap one or more Gtk.FileFilter objects in a Gio.ListStore as required
    by Gtk.FileDialog.set_filters().

    Example::

        dialog.set_filters(make_filter_store(
            make_file_filter("Markdown", "*.md"),
        ))
    """
    store = Gio.ListStore.new(Gtk.FileFilter)
    for f in filters:
        store.append(f)
    return store
