"""
inspector.py — Right panel holding the deck's own settings.

The panel used to be a permanent theme browser: 27 swatches occupying the
window's third column whether or not you were choosing a theme.  Theme
browsing moved to a chooser dialog, which is where a once-per-deck task
belongs, and the panel became the slide's own settings.

It briefly carried a second context — the layout of the image under the
cursor — but images arrange themselves now, so there is nothing to set and
the stack that switched between the two is gone with it.  Restoring a
second context means bringing that stack back.

The heading says "Deck" rather than "Slide": theme, aspect ratio and logo
apply to every slide in the document, and the panel writes them into its
frontmatter.  This is the one heading — ThemePanel used to draw a second,
"Current theme", immediately below it.
"""

import logging

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

log = logging.getLogger(__name__)


class Inspector(Gtk.Box):
    """
    The deck-settings page plus the one header naming it.

    The caller owns the page widget and keeps talking to it directly (it is
    still the ThemePanel); the Inspector supplies the width, the title and
    the separator under it.
    """

    _WIDTH = 300

    def __init__(self, slide_page: Gtk.Widget) -> None:
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

        slide_page.set_vexpand(True)
        self.append(slide_page)

        self._slide_page = slide_page
