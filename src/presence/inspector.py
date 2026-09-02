"""
inspector.py — Right panel holding the deck's settings, and one picture's.

The panel used to be a permanent theme browser: 27 swatches occupying the
window's third column whether or not you were choosing a theme.  Theme
browsing moved to a chooser dialog, which is where a once-per-deck task
belongs, and the panel became the slide's own settings.

It carries two contexts, and the stack that switches between them is why
this class exists rather than the pages being packed directly.  "Deck" is
theme, aspect ratio and logo — everything that applies to every slide, which
the panel writes into the document's frontmatter.  "Image" is the picture
the writer just clicked, whose placement and treatment are written into an
attribute block on its own tag.

Which context is showing is a consequence of where the writer is looking,
never a mode they have to put the panel into: clicking a picture opens the
image context, and moving off it goes back to the deck.  Adding a third
means adding a page and a matching ``show_*_context()`` method.
"""

import logging

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

log = logging.getLogger(__name__)


class Inspector(Gtk.Box):
    """
    The two settings pages, plus the one header naming whichever is showing.

    The caller owns both page widgets and keeps talking to them directly (the
    ThemePanel and the ImagePanel); the Inspector supplies the width, the
    title, the separator under it, and the switch between the two.
    """

    _WIDTH = 300

    def __init__(self, slide_page: Gtk.Widget,
                 image_page: "Gtk.Widget | None" = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_size_request(self._WIDTH, -1)

        self._title = Gtk.Label(label="Deck")
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

        slide_page.set_vexpand(True)
        self._stack.add_named(slide_page, "slide")
        if image_page is not None:
            image_page.set_vexpand(True)
            self._stack.add_named(image_page, "image")
        self.append(self._stack)

        self._slide_page = slide_page
        self._image_page = image_page

    # ── Contexts ─────────────────────────────────────────────────────────

    @property
    def context(self) -> str:
        """Which page is showing: 'slide' or 'image'."""
        return self._stack.get_visible_child_name() or "slide"

    def show_slide_context(self) -> None:
        """Go back to the deck's own settings."""
        if self.context != "slide":
            self._stack.set_visible_child_name("slide")
        self._title.set_label("Deck")

    def show_image_context(self, attrs: dict, description: str) -> None:
        """
        Show the image controls, filled in from the picture just clicked.

        Re-filling an already-visible page is what happens when the writer
        moves from one picture to another, so this is safe to call over and
        over.  *attrs* is what that picture pinned — the empty dict where it
        pinned nothing, which reads as "Automatic" in every row.
        """
        if self._image_page is None:
            return
        self._image_page.select_attrs(attrs, description)
        if self.context != "image":
            self._stack.set_visible_child_name("image")
        self._title.set_label("Image")
