"""
inspector.py — Right panel whose contents follow the cursor.

The panel used to be a permanent theme browser: 27 swatches occupying the
window's third column whether or not you were choosing a theme, while the
controls for the image under your cursor lived in a popover that covered the
text you were editing.

Now the panel shows what the cursor is touching.  In body text that is the
slide's own settings; on an image line it is that image's layout.  Theme
browsing moved to a chooser dialog, which is where a once-per-deck task
belongs.

Adding a context means adding a page and a matching show_*_context() method.
"""

import logging

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

log = logging.getLogger(__name__)


class Inspector(Gtk.Box):
    """
    Stack of context pages plus a header naming the current one.

    The caller owns the page widgets and keeps talking to them directly
    (the slide page is still the ThemePanel); the Inspector only decides
    which one is on screen.
    """

    _WIDTH = 300

    def __init__(self, slide_page: Gtk.Widget,
                 image_page: Gtk.Widget) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_size_request(self._WIDTH, -1)

        self._title = Gtk.Label(label="Slide")
        self._title.add_css_class("heading")
        self._title.set_xalign(0)
        self._title.set_hexpand(True)

        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        title_box.set_margin_top(10)
        title_box.set_margin_bottom(10)
        title_box.set_margin_start(12)
        title_box.set_margin_end(12)
        title_box.append(self._title)
        self.append(title_box)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(120)
        self._stack.set_vexpand(True)
        self._stack.add_named(slide_page, "slide")
        self._stack.add_named(image_page, "image")
        self.append(self._stack)

        self._slide_page = slide_page
        self._image_page = image_page

    # ── Contexts ──────────────────────────────────────────────────────────────

    @property
    def context(self) -> str:
        """Which page is showing: 'slide' or 'image'."""
        return self._stack.get_visible_child_name() or "slide"

    def show_slide_context(self) -> None:
        if self.context != "slide":
            self._stack.set_visible_child_name("slide")
        self._title.set_label("Slide")

    def show_image_context(self, layout: dict, description: str,
                           edit_cb) -> None:
        """
        Show the image controls, populated from the image under the cursor.

        Re-populating an already-visible page is what happens when the cursor
        moves between two images, so this is safe to call repeatedly.
        """
        self._image_page.open_edit_mode(layout, description, edit_cb)
        if self.context != "image":
            self._stack.set_visible_child_name("image")
        self._title.set_label("Image")
