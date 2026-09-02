"""
image_panel.py — What one picture was told, in the right-hand inspector.

Placement is automatic by default, and this panel is where a writer overrules
it for the picture they are looking at.  Everything it sets is written into
an attribute block on that image's own tag — see ``slides/image_attrs.py``,
which owns the vocabulary and is the only code that formats it.

**Every row's first choice is "Automatic".**  That is not a null option, it
is the product's actual default: a picture with nothing pinned is arranged
from the slide's content, and returning a row to Automatic has to be able to
give that back.  So the panel writes only what differs, and a picture with
every row on Automatic carries no block at all rather than a block spelling
out the defaults.

**Being told is not choosing.**  ``select_attrs()`` fills the rows in from
the picture the writer just clicked, and must not read as that writer
setting seven values — it would write the block back, mark the document
modified and start a build, on nothing but a click.  Every setter here is
wrapped in ``handler_block_by_func``, and the rows are ``Adw.ComboRow``
rather than toggle groups precisely because a toggle group's initial
``set_active`` fires during construction: that is the bug that had the theme
wizard resetting the theme it was opened on.

Signals
-------
attrs-changed  (block: str)   — the formatted block, "" for automatic
"""

import logging

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GObject, Gdk

from .slides.image_attrs import (IMAGE_ALIGNMENTS, IMAGE_FILTERS,
                                 format_attrs)

log = logging.getLogger(__name__)

# Each row is (token written to the document, label shown to the writer).
# "" is Automatic — the absence of a token, not a token meaning "auto".
_PLACEMENTS = [
    ("",           "Automatic"),
    ("left",       "Left of the text"),
    ("right",      "Right of the text"),
    ("top",        "Above the text"),
    ("bottom",     "Below the text"),
    ("background", "Fill slide, behind text"),
    ("full",       "Fill slide, text hidden"),
]

_FITS = [
    ("",        "Automatic"),
    ("cover",   "Cover"),
    ("contain", "Contain"),
]

_ALIGNMENTS = [("", "Automatic")] + [
    (a, a.capitalize()) for a in IMAGE_ALIGNMENTS
]

_FILTERS = [
    ("",          "None"),
    ("bw",        "Black & white"),
    ("greyscale", "Greyscale"),
    ("sepia",     "Sepia"),
    ("blur",      "Blur"),
    ("lighten",   "Lighten"),
    ("darken",    "Darken"),
]


# What the tint button shows when there is no tint.  A colour button always
# paints something, and what it painted unset was an arbitrary red — which
# reads as a tint that is switched on.
_NO_TINT_SWATCH = Gdk.RGBA()
_NO_TINT_SWATCH.parse("#b5b5b5")


def _combo(title: str, options: list) -> Adw.ComboRow:
    row = Adw.ComboRow(title=title)
    row.set_model(Gtk.StringList.new([label for _, label in options]))
    return row


class ImagePanel(Gtk.Box):
    """
    The controls for the picture under the writer's cursor.

    Holds no reference to the document: it is told what the picture says with
    ``select_attrs()`` and emits what it should now say.  The window owns the
    write, because the editor owns the buffer.
    """

    __gsignals__ = {
        "attrs-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    _WIDTH = 300

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_size_request(self._WIDTH, -1)

        # What the picture currently says. Rebuilt from the rows on every
        # change, so the block written back is always the whole answer rather
        # than a patch onto whatever was there.
        self._attrs: dict = {}
        self._loading = False

        self._build()

    # ── Construction ─────────────────────────────────────────────────────

    def _build(self) -> None:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # The picture's description, so the writer can tell which one this is
        # without looking back at the text. Not editable here: alt text is a
        # sentence about the picture and belongs in the document, where it
        # reads in context.
        self._desc = Gtk.Label(label="")
        self._desc.set_xalign(0)
        self._desc.set_wrap(True)
        self._desc.set_margin_start(12)
        self._desc.set_margin_end(12)
        self._desc.set_margin_top(12)
        self._desc.set_margin_bottom(4)
        self._desc.add_css_class("dim-label")
        self._desc.add_css_class("caption")
        body.append(self._desc)

        place_group = Adw.PreferencesGroup(title="Placement")
        place_group.set_margin_start(12)
        place_group.set_margin_end(12)
        place_group.set_margin_top(8)

        self._pos_row = _combo("Position", _PLACEMENTS)
        self._pos_row.connect("notify::selected", self._on_changed)
        place_group.add(self._pos_row)

        self._fit_row = _combo("Fit", _FITS)
        self._fit_row.connect("notify::selected", self._on_changed)
        place_group.add(self._fit_row)

        # No subtitles on these rows: the panel is 300px and a two-line
        # title squeezes the value into "Autom…", which is the half the
        # writer is actually reading.
        self._align_row = _combo("Alignment", _ALIGNMENTS)
        self._align_row.connect("notify::selected", self._on_changed)
        place_group.add(self._align_row)
        body.append(place_group)

        look_group = Adw.PreferencesGroup(title="Appearance")
        look_group.set_margin_start(12)
        look_group.set_margin_end(12)
        look_group.set_margin_top(12)

        self._filter_row = _combo("Filter", _FILTERS)
        self._filter_row.connect("notify::selected", self._on_changed)
        look_group.add(self._filter_row)

        # Opacity — the "Logo size" row's shape: a scale and its value as the
        # row's suffix, so the number is readable while dragging.
        self._opacity_row = Adw.ActionRow(title="Opacity")
        self._opacity = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self._opacity.set_hexpand(True)
        self._opacity.set_draw_value(False)
        self._opacity.set_value(100)
        self._opacity.set_increments(5, 25)
        self._opacity.set_valign(Gtk.Align.CENTER)
        self._opacity.connect("value-changed", self._on_changed)

        self._opacity_val = Gtk.Label(label="100%")
        self._opacity_val.set_width_chars(5)
        self._opacity_val.set_xalign(1)
        self._opacity_val.add_css_class("caption")
        self._opacity_val.set_valign(Gtk.Align.CENTER)

        op_suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        op_suffix.set_valign(Gtk.Align.CENTER)
        op_suffix.append(self._opacity)
        op_suffix.append(self._opacity_val)
        self._opacity_row.add_suffix(op_suffix)
        look_group.add(self._opacity_row)

        # Tint — the "Logo" row's shape: a chooser and a clear button, since
        # "no tint" has to be reachable in one click once one is set.
        self._tint_row = Adw.ActionRow(title="Tint")
        self._tint_row.set_subtitle("None")
        self._tint_btn = Gtk.ColorDialogButton()
        self._tint_btn.set_dialog(Gtk.ColorDialog(with_alpha=False))
        self._tint_btn.set_valign(Gtk.Align.CENTER)
        self._tint_btn.set_rgba(_NO_TINT_SWATCH)
        self._tint_btn.connect("notify::rgba", self._on_tint_picked)

        self._tint_clear = Gtk.Button(icon_name="edit-clear-symbolic")
        self._tint_clear.add_css_class("flat")
        self._tint_clear.set_valign(Gtk.Align.CENTER)
        self._tint_clear.set_tooltip_text("Remove tint")
        self._tint_clear.set_visible(False)
        self._tint_clear.connect("clicked", self._on_tint_cleared)

        tint_suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tint_suffix.set_valign(Gtk.Align.CENTER)
        tint_suffix.append(self._tint_btn)
        tint_suffix.append(self._tint_clear)
        self._tint_row.add_suffix(tint_suffix)
        look_group.add(self._tint_row)
        body.append(look_group)

        # One button back to the default, because seven rows individually
        # returned to Automatic is a chore and the writer's actual intent
        # ("never mind") is a single thought.
        self._reset = Gtk.Button(label="Reset to automatic")
        self._reset.add_css_class("flat")
        self._reset.set_margin_start(12)
        self._reset.set_margin_end(12)
        self._reset.set_margin_top(16)
        self._reset.set_margin_bottom(12)
        self._reset.connect("clicked", self._on_reset)
        body.append(self._reset)

        scroller.set_child(body)
        self.append(scroller)

    # ── Being told ───────────────────────────────────────────────────────

    def select_attrs(self, attrs: dict, description: str = "") -> None:
        """
        Show what *attrs* pins, without emitting.

        Called every time the writer moves onto a picture, including onto the
        one already showing, so it must be idempotent and must never read as
        a choice — see the class docstring.
        """
        self._attrs = dict(attrs or {})
        self._loading = True
        try:
            self._desc.set_label(description or "This picture")

            self._select(self._pos_row, _PLACEMENTS,
                         self._attrs.get("position", ""))
            self._select(self._fit_row, _FITS, self._attrs.get("fit", ""))

            focal = self._attrs.get("focal") or ""
            align = focal[6:] if focal.startswith("focal-") else ""
            self._select(self._align_row, _ALIGNMENTS, align)

            filt = self._attrs.get("filter") or ""
            self._select(self._filter_row, _FILTERS, filt)

            opacity = self._attrs.get("opacity", 100)
            self._opacity.handler_block_by_func(self._on_changed)
            self._opacity.set_value(opacity)
            self._opacity.handler_unblock_by_func(self._on_changed)
            self._opacity_val.set_label(f"{int(opacity)}%")

            self._show_tint(self._attrs.get("tint"))
        finally:
            self._loading = False

    def _select(self, row: Adw.ComboRow, options: list, token: str) -> None:
        """Set *row* to *token* without the change reading as a choice."""
        index = next((i for i, (tok, _) in enumerate(options) if tok == token), 0)
        try:
            row.handler_block_by_func(self._on_changed)
            row.set_selected(index)
            row.handler_unblock_by_func(self._on_changed)
        except TypeError:                      # not connected yet
            row.set_selected(index)

    def _show_tint(self, tint: "str | None") -> None:
        rgba = Gdk.RGBA()
        if tint and rgba.parse(tint):
            try:
                self._tint_btn.handler_block_by_func(self._on_tint_picked)
                self._tint_btn.set_rgba(rgba)
                self._tint_btn.handler_unblock_by_func(self._on_tint_picked)
            except TypeError:
                self._tint_btn.set_rgba(rgba)
            self._tint_row.set_subtitle(tint)
            self._tint_clear.set_visible(True)
        else:
            # Back to the neutral swatch: leaving the last colour showing
            # would say this picture is tinted when it is not.
            try:
                self._tint_btn.handler_block_by_func(self._on_tint_picked)
                self._tint_btn.set_rgba(_NO_TINT_SWATCH)
                self._tint_btn.handler_unblock_by_func(self._on_tint_picked)
            except TypeError:
                self._tint_btn.set_rgba(_NO_TINT_SWATCH)
            self._tint_row.set_subtitle("None")
            self._tint_clear.set_visible(False)

    # ── Choosing ─────────────────────────────────────────────────────────

    def _token(self, row: Adw.ComboRow, options: list) -> str:
        return options[row.get_selected()][0] if row.get_selected() >= 0 else ""

    def _on_changed(self, *_args) -> None:
        """Any row moved: rebuild the whole block and say so."""
        if self._loading:
            return

        attrs: dict = {}

        if pos := self._token(self._pos_row, _PLACEMENTS):
            attrs["position"] = pos
        if fit := self._token(self._fit_row, _FITS):
            attrs["fit"] = fit
        if align := self._token(self._align_row, _ALIGNMENTS):
            attrs["focal"] = f"focal-{align}"
        if filt := self._token(self._filter_row, _FILTERS):
            attrs["filter"] = filt
            if filt == "blur":
                # Keep whatever strength the document already carried; the
                # token's default fills in for a picture that had none.
                blur = self._attrs.get("blur")
                if blur:
                    attrs["blur"] = blur

        opacity = int(round(self._opacity.get_value()))
        self._opacity_val.set_label(f"{opacity}%")
        if opacity < 100:
            attrs["opacity"] = opacity

        if self._tint_clear.get_visible():
            rgba = self._tint_btn.get_rgba()
            tint = "#%02x%02x%02x" % (round(rgba.red * 255),
                                      round(rgba.green * 255),
                                      round(rgba.blue * 255))
            attrs["tint"] = tint
            self._tint_row.set_subtitle(tint)

        self._attrs = attrs
        self.emit("attrs-changed", format_attrs(attrs))

    def _on_tint_picked(self, *_args) -> None:
        """
        A colour was picked, which is also how a tint gets turned *on*.

        The clear button is the panel's record of whether there is a tint at
        all — Gdk.RGBA has no "unset" — so it is shown here rather than only
        when a tinted picture is loaded.
        """
        if self._loading:
            return
        self._tint_clear.set_visible(True)
        self._on_changed()

    def _on_tint_cleared(self, _btn) -> None:
        self._show_tint(None)
        self._on_changed()

    def _on_reset(self, _btn) -> None:
        """Give the picture back to the layout."""
        self.select_attrs({}, self._desc.get_label())
        self.emit("attrs-changed", "")
