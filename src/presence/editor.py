"""
editor.py — Markdown editor with formatting toolbar, find/replace,
and contextual image-layout editing.

When the cursor rests on a line containing a Markdown image tag, a
contextual popover appears anchored to that line, pre-populated with
the current layout tokens (position, size, gradient).  Any change in
the popover is written back to the buffer immediately — no Apply button.
This mirrors the iA Presenter UX pattern.
"""
from __future__ import annotations

import logging
import math
import re
import shutil
from pathlib import Path
from typing import Callable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, GObject, Pango, Gio

try:
    from gi.repository import PangoCairo as _PangoCairo  # used in badge drawing
except ImportError:
    _PangoCairo = None  # type: ignore[assignment]

try:
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource
    _GTKSOURCE_AVAILABLE = True
except (ValueError, ImportError):
    try:
        gi.require_version("GtkSource", "4")
        from gi.repository import GtkSource
        _GTKSOURCE_AVAILABLE = True
    except (ValueError, ImportError):
        _GTKSOURCE_AVAILABLE = False

log = logging.getLogger(__name__)

# Regex shared with md_to_slides.slides — keep in sync.
_IMAGE_RE = re.compile(
    r'!\[([^\]]*)\]'
    r'\('
    r'([^)\s"\']+)'
    r'(?:\s+"[^"]*")?'
    r'\)',
)

_IMAGE_POSITIONS = frozenset(("left", "right", "top", "bottom", "background"))
_IMAGE_FIT       = frozenset(("cover", "contain"))
_IMAGE_FOCAL     = frozenset(("focal-top", "focal-center", "focal-bottom"))

_OPACITY_TOKEN_RE   = re.compile(r'^opacity(\d{1,3})$')
_FADE_TOKEN_RE      = re.compile(r'^fade-(?:left|right|top|bottom)$')
_SIZE_TOKEN_RE      = re.compile(r'^(\d{1,3})$')
_GRAYSCALE_TOKEN_RE = re.compile(r'^grayscale(\d{1,3})$')
_BLUR_TOKEN_RE      = re.compile(r'^blur(\d{1,2})$')
_TINT_TOKEN_RE      = re.compile(r'^tint-(#[0-9a-f]{3,8}|[a-z]{2,30})$')
_ZOOM_TOKEN_RE      = re.compile(r'^zoom(\d{1,3})$')


# Default fade direction when position changes — the gradient always points
# inward (from the text side toward the image).
_POSITION_DEFAULT_FADE: dict[str, str] = {
    "left":       "right",
    "right":      "left",
    "top":        "bottom",
    "bottom":     "top",
    "background": "left",
}


def _parse_alt(alt: str) -> tuple[str, dict]:
    """
    Split an alt string into (description, layout_dict).

    Format: "Human description|position|size|gradient|opacityN|fade-dir|fit|focal"
    Tokens are order-insensitive and separated by |.
    """
    layout: dict = {
        "position": "right", "size": "50", "gradient": True,
        "opacity": 75, "fade": None,
        "fit": "cover", "focal": "focal-center",
        "grayscale": 0, "blur": 0, "tint": None,
        "flip_h": False, "flip_v": False, "zoom": 100,
    }
    desc_parts: list[str] = []
    for token in (t.strip() for t in alt.split("|")):
        tl = token.lower()
        if tl in _IMAGE_POSITIONS:
            layout["position"] = tl
        elif m2 := _SIZE_TOKEN_RE.match(tl):
            size_val = int(m2.group(1))
            if 1 <= size_val <= 100:
                layout["size"] = tl
        elif tl == "gradient":
            layout["gradient"] = True
        elif tl == "nogradient":
            layout["gradient"] = False
        elif m := _OPACITY_TOKEN_RE.match(tl):
            layout["opacity"] = max(0, min(100, int(m.group(1))))
        elif tl.startswith("fade-") and tl[5:] in ("left", "right", "top", "bottom"):
            layout["fade"] = tl[5:]
        elif tl in _IMAGE_FIT:
            layout["fit"] = tl
        elif tl in _IMAGE_FOCAL:
            layout["focal"] = tl
        elif m := _GRAYSCALE_TOKEN_RE.match(tl):
            layout["grayscale"] = max(0, min(100, int(m.group(1))))
        elif m := _BLUR_TOKEN_RE.match(tl):
            layout["blur"] = max(0, min(20, int(m.group(1))))
        elif m := _TINT_TOKEN_RE.match(tl):
            layout["tint"] = m.group(1)
        elif tl == "flip-h":
            layout["flip_h"] = True
        elif tl == "flip-v":
            layout["flip_v"] = True
        elif m := _ZOOM_TOKEN_RE.match(tl):
            zoom_val = int(m.group(1))
            if 100 <= zoom_val <= 300:
                layout["zoom"] = zoom_val
        elif token:
            desc_parts.append(token)
    return " ".join(desc_parts), layout


def _build_alt(desc: str, layout: dict) -> str:
    """Reconstruct the alt string from description + layout dict."""
    tokens = []
    if desc.strip():
        tokens.append(desc.strip())
    tokens.append(layout.get("position", "right"))
    tokens.append(layout.get("size", "50"))
    tokens.append("gradient" if layout.get("gradient", True) else "nogradient")
    opacity = layout.get("opacity", 75)
    tokens.append(f"opacity{opacity}")
    fade = layout.get("fade")
    if fade:
        tokens.append(f"fade-{fade}")
    fit = layout.get("fit", "cover")
    if fit != "cover":
        tokens.append(fit)
    focal = layout.get("focal", "focal-center")
    if focal != "focal-center" and fit == "cover":
        tokens.append(focal)
    grayscale = layout.get("grayscale", 0)
    if grayscale > 0:
        tokens.append(f"grayscale{grayscale}")
    blur = layout.get("blur", 0)
    if blur > 0:
        tokens.append(f"blur{blur}")
    tint = layout.get("tint")
    if tint:
        tokens.append(f"tint-{tint}")
    if layout.get("flip_h", False):
        tokens.append("flip-h")
    if layout.get("flip_v", False):
        tokens.append("flip-v")
    zoom = layout.get("zoom", 100)
    if zoom != 100:
        tokens.append(f"zoom{zoom}")
    return "|".join(tokens)


# ── ImageLayoutPopover ────────────────────────────────────────────────────────

class ImageLayoutPopover(Gtk.Popover):
    """
    Non-modal popover for inserting OR editing an image's layout.

    Insert mode — opened from the toolbar button; "Choose image…" action.
    Edit mode   — opened when cursor is on an image tag; changes write back
                  immediately (no Apply button needed).

    GNOME HIG compliance:
      • Flat toggle buttons for position — each shows a small SVG icon that
        illustrates the image/content split, so the choice is self-explaining.
        Selected state uses the accent colour border + tinted background.
      • Linked ToggleButtons for the three size options.
      • Adw.SwitchRow-style row (manual, no libadwaita dep) for gradient.
      • Single primary action button in insert mode only.
      • All controls keyboard-navigable via Tab.
      • No spatial diagram of checkboxes — replaced by icon buttons laid in
        a 2×2 + 1 full-width grid that is both compact and self-documenting.
    """

    # CSS injected once per display — scoped to .img-layout-popover so it
    # cannot bleed into other parts of the UI.
    _CSS_INSTALLED = False

    # Icon SVGs for each position — two rectangles showing image (filled)
    # and content (outline) side so the user sees the layout at a glance.
    _POS_ICONS: dict[str, str] = {
        "left": (
            '<svg width="18" height="14" viewBox="0 0 18 14" fill="none"'
            ' xmlns="http://www.w3.org/2000/svg">'
            '<rect x="0.5" y="0.5" width="7" height="13" rx="1.5"'
            ' fill="currentColor" opacity="0.85"/>'
            '<rect x="9.5" y="0.5" width="8" height="13" rx="1.5"'
            ' fill="currentColor" opacity="0.2"/>'
            '</svg>'
        ),
        "right": (
            '<svg width="18" height="14" viewBox="0 0 18 14" fill="none"'
            ' xmlns="http://www.w3.org/2000/svg">'
            '<rect x="0.5" y="0.5" width="8" height="13" rx="1.5"'
            ' fill="currentColor" opacity="0.2"/>'
            '<rect x="10.5" y="0.5" width="7" height="13" rx="1.5"'
            ' fill="currentColor" opacity="0.85"/>'
            '</svg>'
        ),
        "top": (
            '<svg width="18" height="14" viewBox="0 0 18 14" fill="none"'
            ' xmlns="http://www.w3.org/2000/svg">'
            '<rect x="0.5" y="0.5" width="17" height="5" rx="1.5"'
            ' fill="currentColor" opacity="0.85"/>'
            '<rect x="0.5" y="7.5" width="17" height="6" rx="1.5"'
            ' fill="currentColor" opacity="0.2"/>'
            '</svg>'
        ),
        "bottom": (
            '<svg width="18" height="14" viewBox="0 0 18 14" fill="none"'
            ' xmlns="http://www.w3.org/2000/svg">'
            '<rect x="0.5" y="0.5" width="17" height="6" rx="1.5"'
            ' fill="currentColor" opacity="0.2"/>'
            '<rect x="0.5" y="8.5" width="17" height="5" rx="1.5"'
            ' fill="currentColor" opacity="0.85"/>'
            '</svg>'
        ),
        "background": (
            '<svg width="18" height="14" viewBox="0 0 18 14" fill="none"'
            ' xmlns="http://www.w3.org/2000/svg">'
            '<rect x="0.5" y="0.5" width="17" height="13" rx="2"'
            ' fill="currentColor" opacity="0.85"/>'
            '<rect x="4" y="4" width="10" height="6" rx="1"'
            ' fill="currentColor" opacity="0.0" stroke="white"'
            ' stroke-width="1" stroke-dasharray="2 1.5"/>'
            '</svg>'
        ),
    }

    _POS_LABELS: dict[str, str] = {
        "left":       "Left",
        "right":      "Right",
        "top":        "Top",
        "bottom":     "Bottom",
        "background": "Background",
    }

    def __init__(self, parent_widget: Gtk.Widget,
                 insert_cb: Callable[[str, str], None]) -> None:
        super().__init__()
        self._insert_cb  = insert_cb
        self._edit_cb: Callable[[dict, str], None] | None = None
        self._edit_mode  = False
        self._active_pos       = "right"
        self._active_size      = "50"
        self._active_opacity   = 75
        self._active_fade      = "left"   # default for "right" position
        self._active_fit       = "cover"
        self._active_focal     = "focal-center"
        self._active_grayscale = 0
        self._active_blur      = 0
        self._active_tint: str | None = None
        self._active_flip_h    = False
        self._active_flip_v    = False
        self._active_zoom      = 100
        self._writeback_source: int | None = None
        # Holds a reference to the active Gtk.FileDialog so it is not garbage-
        # collected before the user selects a file (cleared in _on_file_chosen).
        self._active_file_dialog = None
        # The Gtk.Window that owns this popover — captured when the popover is
        # opened so that _on_insert_clicked has a valid parent for the FileDialog
        # even after popdown() has unparented the popover from the widget tree.
        self._parent_window: "Gtk.Window | None" = None

        self.set_parent(parent_widget)
        self.set_has_arrow(True)
        self.set_autohide(True)

        # Install scoped CSS once per process
        self._ensure_css()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("img-layout-popover")
        root.set_size_request(300, -1)

        def _inline_slider(lbl_text: str, scale: Gtk.Scale) -> Gtk.Box:
            """Return a compact hbox: fixed-width label + expanding scale."""
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.set_margin_start(10)
            row.set_margin_end(10)
            row.set_margin_bottom(6)
            lbl = Gtk.Label(label=lbl_text)
            lbl.set_xalign(0.0)
            lbl.set_width_chars(9)
            lbl.add_css_class("caption")
            lbl.add_css_class("dim-label")
            scale.set_hexpand(True)
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            row.append(lbl)
            row.append(scale)
            return row

        # ── Header ────────────────────────────────────────────────────────────
        header = Gtk.Label(label="Image layout")
        header.set_xalign(0.0)
        header.add_css_class("ilp-header")
        header.set_margin_top(10)
        header.set_margin_bottom(10)
        header.set_margin_start(10)
        header.set_margin_end(10)
        root.append(header)
        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Position section ──────────────────────────────────────────────────
        pos_lbl = Gtk.Label(label="Position")
        pos_lbl.set_xalign(0.0)
        pos_lbl.add_css_class("ilp-section-label")
        pos_lbl.set_margin_top(6)
        pos_lbl.set_margin_bottom(4)
        pos_lbl.set_margin_start(10)
        pos_lbl.set_margin_end(10)
        root.append(pos_lbl)

        # 2×2 grid for left/right/top/bottom + full-width background button
        self._pos_buttons: dict[str, Gtk.Button] = {}
        pos_grid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pos_grid.set_margin_start(10)
        pos_grid.set_margin_end(10)
        pos_grid.set_margin_bottom(6)

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bot_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        for token, row in (("left", top_row), ("right", top_row),
                            ("top", bot_row), ("bottom", bot_row)):
            btn = self._make_pos_button(token)
            btn.set_hexpand(True)
            row.append(btn)

        bg_btn = self._make_pos_button("background")
        bg_btn.set_hexpand(True)

        pos_grid.append(top_row)
        pos_grid.append(bot_row)
        pos_grid.append(bg_btn)
        root.append(pos_grid)

        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Size section ──────────────────────────────────────────────────────
        self._size_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 1, 100, 1
        )
        for _v, _lbl in ((30, "30%"), (50, "50%"), (70, "70%"), (100, "Full")):
            self._size_scale.add_mark(_v, Gtk.PositionType.BOTTOM, _lbl)
        self._size_scale.set_value(50)
        self._size_scale.connect("value-changed", self._on_size_changed)
        root.append(_inline_slider("Size", self._size_scale))
        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Opacity section ───────────────────────────────────────────────────
        opacity_lbl = Gtk.Label(label="Opacity")
        opacity_lbl.set_xalign(0.0)
        opacity_lbl.add_css_class("ilp-section-label")
        opacity_lbl.set_margin_top(6)
        opacity_lbl.set_margin_bottom(4)
        opacity_lbl.set_margin_start(10)
        opacity_lbl.set_margin_end(10)
        root.append(opacity_lbl)

        opacity_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        opacity_row.set_margin_start(10)
        opacity_row.set_margin_end(10)
        opacity_row.set_margin_bottom(6)
        self._opacity_buttons: dict[str, Gtk.Button] = {}

        for _lbl, _tok in (("25%", "25"), ("50%", "50"), ("75%", "75"), ("Full", "100")):
            _btn = Gtk.Button(label=_lbl)
            _btn.set_hexpand(True)
            _btn.add_css_class("ilp-btn")
            _btn.set_tooltip_text(f"{_tok}% image opacity")
            _btn.connect("clicked", self._on_opacity_clicked, _tok)
            self._opacity_buttons[_tok] = _btn
            opacity_row.append(_btn)

        root.append(opacity_row)
        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Gradient row ──────────────────────────────────────────────────────
        grad_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        grad_row.set_margin_top(6)
        grad_row.set_margin_bottom(6)
        grad_row.set_margin_start(10)
        grad_row.set_margin_end(10)

        grad_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        grad_text.set_hexpand(True)
        grad_title = Gtk.Label(label="Fade to background")
        grad_title.set_xalign(0.0)
        grad_sub = Gtk.Label(label="Gradient blend at the image edge")
        grad_sub.set_xalign(0.0)
        grad_sub.add_css_class("caption")
        grad_sub.add_css_class("dim-label")
        grad_text.append(grad_title)
        grad_text.append(grad_sub)

        self._grad_switch = Gtk.Switch()
        self._grad_switch.set_active(True)
        self._grad_switch.set_valign(Gtk.Align.CENTER)
        self._grad_switch.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Fade to background"]
        )
        self._grad_switch.connect("state-set", self._on_grad_changed)

        grad_row.append(grad_text)
        grad_row.append(self._grad_switch)
        root.append(grad_row)

        # ── Fade direction section (visible only when gradient is ON) ─────────
        # Includes its own top separator so hiding the box also hides the divider.
        self._fade_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._fade_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        fade_lbl = Gtk.Label(label="Gradient direction")
        fade_lbl.set_xalign(0.0)
        fade_lbl.add_css_class("ilp-section-label")
        fade_lbl.set_margin_top(6)
        fade_lbl.set_margin_bottom(4)
        fade_lbl.set_margin_start(10)
        fade_lbl.set_margin_end(10)
        self._fade_box.append(fade_lbl)

        fade_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        fade_row.set_margin_start(10)
        fade_row.set_margin_end(10)
        fade_row.set_margin_bottom(6)
        self._fade_buttons: dict[str, Gtk.Button] = {}

        for _lbl, _tok, _tip in (
            ("←", "left",   "Gradient from the left"),
            ("→", "right",  "Gradient from the right"),
            ("↑", "top",    "Gradient from the top"),
            ("↓", "bottom", "Gradient from the bottom"),
        ):
            _btn = Gtk.Button(label=_lbl)
            _btn.set_hexpand(True)
            _btn.add_css_class("ilp-btn")
            _btn.set_tooltip_text(_tip)
            _btn.connect("clicked", self._on_fade_clicked, _tok)
            self._fade_buttons[_tok] = _btn
            fade_row.append(_btn)

        self._fade_box.append(fade_row)
        root.append(self._fade_box)

        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Fit section ───────────────────────────────────────────────────────
        fit_lbl = Gtk.Label(label="Fit")
        fit_lbl.set_xalign(0.0)
        fit_lbl.add_css_class("ilp-section-label")
        fit_lbl.set_margin_top(6)
        fit_lbl.set_margin_bottom(4)
        fit_lbl.set_margin_start(10)
        fit_lbl.set_margin_end(10)
        root.append(fit_lbl)

        fit_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        fit_row.set_margin_start(10)
        fit_row.set_margin_end(10)
        fit_row.set_margin_bottom(6)
        self._fit_buttons: dict[str, Gtk.Button] = {}

        for _lbl, _tok, _tip in (
            ("Cover",   "cover",   "Crop image to fill the panel"),
            ("Contain", "contain", "Letterbox image inside the panel"),
        ):
            _btn = Gtk.Button(label=_lbl)
            _btn.set_hexpand(True)
            _btn.add_css_class("ilp-btn")
            _btn.set_tooltip_text(_tip)
            _btn.connect("clicked", self._on_fit_clicked, _tok)
            self._fit_buttons[_tok] = _btn
            fit_row.append(_btn)

        root.append(fit_row)

        # ── Focal point section (visible only when fit=cover, pos≠background) ─
        self._focal_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._focal_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        focal_lbl = Gtk.Label(label="Focal point")
        focal_lbl.set_xalign(0.0)
        focal_lbl.add_css_class("ilp-section-label")
        focal_lbl.set_margin_top(6)
        focal_lbl.set_margin_bottom(4)
        focal_lbl.set_margin_start(10)
        focal_lbl.set_margin_end(10)
        self._focal_box.append(focal_lbl)

        focal_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        focal_row.set_margin_start(10)
        focal_row.set_margin_end(10)
        focal_row.set_margin_bottom(6)
        self._focal_buttons: dict[str, Gtk.Button] = {}

        for _lbl, _tok, _tip in (
            ("Top",    "focal-top",    "Frame the top of the image"),
            ("Center", "focal-center", "Frame the center of the image"),
            ("Bottom", "focal-bottom", "Frame the bottom of the image"),
        ):
            _btn = Gtk.Button(label=_lbl)
            _btn.set_hexpand(True)
            _btn.add_css_class("ilp-btn")
            _btn.set_tooltip_text(_tip)
            _btn.connect("clicked", self._on_focal_clicked, _tok)
            self._focal_buttons[_tok] = _btn
            focal_row.append(_btn)

        self._focal_box.append(focal_row)
        root.append(self._focal_box)

        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Filters section: Grayscale + Blur ─────────────────────────────────
        filt_lbl = Gtk.Label(label="Filters")
        filt_lbl.set_xalign(0.0)
        filt_lbl.add_css_class("ilp-section-label")
        filt_lbl.set_margin_top(6)
        filt_lbl.set_margin_bottom(4)
        filt_lbl.set_margin_start(10)
        filt_lbl.set_margin_end(10)
        root.append(filt_lbl)

        for _attr, _label, _lo, _hi, _handler in (
            ("_grayscale_scale", "Grayscale", 0, 100, "_on_grayscale_changed"),
            ("_blur_scale",      "Blur px",   0,  20, "_on_blur_changed"),
        ):
            _sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, _lo, _hi, 1)
            _sc.set_value(_lo)
            _sc.connect("value-changed", getattr(self, _handler))
            setattr(self, _attr, _sc)
            root.append(_inline_slider(_label, _sc))

        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Tint section ──────────────────────────────────────────────────────
        tint_lbl = Gtk.Label(label="Tint")
        tint_lbl.set_xalign(0.0)
        tint_lbl.add_css_class("ilp-section-label")
        tint_lbl.set_margin_top(6)
        tint_lbl.set_margin_bottom(4)
        tint_lbl.set_margin_start(10)
        tint_lbl.set_margin_end(10)
        root.append(tint_lbl)

        tint_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tint_row.set_margin_start(10)
        tint_row.set_margin_end(10)
        tint_row.set_margin_bottom(6)

        self._tint_none_btn = Gtk.Button(label="None")
        self._tint_none_btn.set_hexpand(True)
        self._tint_none_btn.add_css_class("ilp-btn")
        self._tint_none_btn.set_tooltip_text("No colour overlay")
        self._tint_none_btn.connect("clicked", self._on_tint_none_clicked)
        tint_row.append(self._tint_none_btn)

        self._tint_color_btn = Gtk.ColorButton()
        self._tint_color_btn.set_hexpand(True)
        self._tint_color_btn.set_use_alpha(False)
        self._tint_color_btn.set_tooltip_text("Pick a tint colour")
        self._tint_color_btn.connect("color-set", self._on_tint_color_set)
        tint_row.append(self._tint_color_btn)

        root.append(tint_row)
        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Flip section ──────────────────────────────────────────────────────
        flip_lbl = Gtk.Label(label="Flip")
        flip_lbl.set_xalign(0.0)
        flip_lbl.add_css_class("ilp-section-label")
        flip_lbl.set_margin_top(6)
        flip_lbl.set_margin_bottom(4)
        flip_lbl.set_margin_start(10)
        flip_lbl.set_margin_end(10)
        root.append(flip_lbl)

        flip_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        flip_row.set_margin_start(10)
        flip_row.set_margin_end(10)
        flip_row.set_margin_bottom(6)

        self._flip_h_btn = Gtk.Button(label="Horizontal")
        self._flip_h_btn.set_hexpand(True)
        self._flip_h_btn.add_css_class("ilp-btn")
        self._flip_h_btn.set_tooltip_text("Mirror left↔right")
        self._flip_h_btn.connect("clicked", self._on_flip_h_clicked)
        flip_row.append(self._flip_h_btn)

        self._flip_v_btn = Gtk.Button(label="Vertical")
        self._flip_v_btn.set_hexpand(True)
        self._flip_v_btn.add_css_class("ilp-btn")
        self._flip_v_btn.set_tooltip_text("Mirror top↔bottom")
        self._flip_v_btn.connect("clicked", self._on_flip_v_clicked)
        flip_row.append(self._flip_v_btn)

        root.append(flip_row)
        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Zoom section ──────────────────────────────────────────────────────
        zoom_sep_lbl = Gtk.Label(label="Zoom")
        zoom_sep_lbl.set_xalign(0.0)
        zoom_sep_lbl.add_css_class("ilp-section-label")
        zoom_sep_lbl.set_margin_top(6)
        zoom_sep_lbl.set_margin_bottom(4)
        zoom_sep_lbl.set_margin_start(10)
        zoom_sep_lbl.set_margin_end(10)
        root.append(zoom_sep_lbl)

        self._zoom_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 100, 300, 5
        )
        for _v, _l in ((100, "1×"), (200, "2×"), (300, "3×")):
            self._zoom_scale.add_mark(_v, Gtk.PositionType.BOTTOM, _l)
        self._zoom_scale.set_value(100)
        self._zoom_scale.connect("value-changed", self._on_zoom_changed)
        root.append(_inline_slider("Zoom %", self._zoom_scale))

        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Alt text ──────────────────────────────────────────────────────────
        alt_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        alt_box.set_margin_top(6)
        alt_box.set_margin_bottom(6)
        alt_box.set_margin_start(10)
        alt_box.set_margin_end(10)

        self._alt_entry = Gtk.Entry()
        self._alt_entry.set_placeholder_text("Alt text / description…")
        self._alt_entry.connect("changed", self._on_alt_changed)
        alt_box.append(self._alt_entry)
        root.append(alt_box)

        # ── Primary action (insert mode only) ─────────────────────────────────
        self._insert_btn = Gtk.Button(label="Choose image…")
        self._insert_btn.add_css_class("suggested-action")
        self._insert_btn.set_margin_start(10)
        self._insert_btn.set_margin_end(10)
        self._insert_btn.set_margin_bottom(6)
        self._insert_btn.connect("clicked", self._on_insert_clicked)
        root.append(self._insert_btn)

        self._ai_btn = Gtk.Button(label="Generate with AI…")
        self._ai_btn.set_margin_start(10)
        self._ai_btn.set_margin_end(10)
        self._ai_btn.set_margin_bottom(6)
        self._ai_btn.connect("clicked", self._on_generate_ai_clicked)
        self._ai_btn.set_visible(False)
        root.append(self._ai_btn)

        self._infographic_btn = Gtk.Button(label="Generate Infographic…")
        self._infographic_btn.set_margin_start(10)
        self._infographic_btn.set_margin_end(10)
        self._infographic_btn.set_margin_bottom(10)
        self._infographic_btn.connect("clicked", self._on_infographic_clicked)
        self._infographic_btn.set_visible(False)
        root.append(self._infographic_btn)

        # Wrap in a ScrolledWindow so the popover never exceeds screen height.
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_max_content_height(560)
        scroll.set_propagate_natural_height(True)
        scroll.set_child(root)
        self.set_child(scroll)

        # Initialise visual state
        self._refresh_pos_buttons()
        self._refresh_size_scale()
        self._refresh_fit_buttons()
        self._refresh_focal_buttons()

    # ── CSS ───────────────────────────────────────────────────────────────────

    @classmethod
    def _ensure_css(cls) -> None:
        """Install scoped CSS for the popover once per process."""
        if cls._CSS_INSTALLED:
            return
        css = Gtk.CssProvider()
        rules = (
            # Section labels: small caps, muted
            ".ilp-section-label {"
            "  font-size: 11px;"
            "  font-weight: 600;"
            "  text-transform: uppercase;"
            "  letter-spacing: 0.06em;"
            "  color: alpha(@window_fg_color, 0.55);"
            "}"
            # Header: bold title
            ".ilp-header { font-weight: 600; font-size: 13px; }"
            # Generic icon button base
            ".ilp-btn {"
            "  padding: 7px 6px;"
            "  border-radius: 6px;"
            "  font-size: 12px;"
            "}"
            # Position button: icon stacked above label
            ".ilp-pos-btn {"
            "  padding: 8px 6px 7px;"
            "  border-radius: 6px;"
            "  font-size: 12px;"
            "}"
            # Selected state — accent border + tinted background
            ".ilp-selected {"
            "  background: alpha(@accent_bg_color, 0.15);"
            "  border-color: @accent_color;"
            "  color: @accent_color;"
            "  border-width: 2px;"
            "}"
        )
        try:
            css.load_from_string(rules)
        except AttributeError:
            css.load_from_data(rules.encode())
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        cls._CSS_INSTALLED = True

    # ── Position button factory ───────────────────────────────────────────────

    def _make_pos_button(self, token: str) -> Gtk.Button:
        """Build one position button: SVG icon + label stacked vertically."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_halign(Gtk.Align.CENTER)

        # SVG icon via GtkImage + Gdk.Paintable from SVG bytes
        # Fall back to a text-only button when SVG loading is unavailable.
        icon_svg = self._POS_ICONS.get(token, "")
        try:
            from gi.repository import GdkPixbuf
            loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
            loader.write(icon_svg.encode())
            loader.close()
            pb = loader.get_pixbuf()
            if pb is not None:
                from gi.repository import Gdk as _Gdk
                tex = _Gdk.Texture.new_for_pixbuf(pb)
                img = Gtk.Image.new_from_paintable(tex)
                img.set_pixel_size(18)
                box.append(img)
        except Exception:
            pass  # icon skipped gracefully — label still identifies the button

        lbl = Gtk.Label(label=self._POS_LABELS[token])
        lbl.add_css_class("caption")
        box.append(lbl)

        btn = Gtk.Button()
        btn.set_child(box)
        btn.add_css_class("flat")
        btn.add_css_class("ilp-pos-btn")
        btn.set_tooltip_text(self._POS_LABELS[token])
        btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [self._POS_LABELS[token]]
        )
        btn.connect("clicked", self._on_pos_clicked, token)
        self._pos_buttons[token] = btn
        return btn

    # ── Visual state refresh ──────────────────────────────────────────────────

    def _refresh_pos_buttons(self) -> None:
        """Update selected/unselected styling on all position buttons."""
        is_bg = self._active_pos == "background"
        for token, btn in self._pos_buttons.items():
            if token == self._active_pos:
                btn.add_css_class("ilp-selected")
            else:
                btn.remove_css_class("ilp-selected")
        # Disable size/gradient when background is chosen — not applicable
        self._size_scale.set_sensitive(not is_bg)
        self._grad_switch.set_sensitive(not is_bg)
        # Fade section: only visible when gradient is ON and not background
        if hasattr(self, "_fade_box"):
            grad_on = self._grad_switch.get_active()
            self._fade_box.set_visible(not is_bg and grad_on)
        # Focal section: visible only when fit=cover and not background
        if hasattr(self, "_focal_box"):
            self._focal_box.set_visible(
                not is_bg and self._active_fit == "cover"
            )

    def _refresh_size_scale(self) -> None:
        """Sync the size scale to _active_size without triggering writeback."""
        self._size_scale.handler_block_by_func(self._on_size_changed)
        try:
            self._size_scale.set_value(int(self._active_size))
        finally:
            self._size_scale.handler_unblock_by_func(self._on_size_changed)

    def _refresh_opacity_buttons(self) -> None:
        """Update selected/unselected styling on all opacity buttons."""
        for token, btn in self._opacity_buttons.items():
            if int(token) == self._active_opacity:
                btn.add_css_class("ilp-selected")
            else:
                btn.remove_css_class("ilp-selected")

    def _refresh_fade_buttons(self) -> None:
        """Update selected/unselected styling on all fade-direction buttons."""
        for token, btn in self._fade_buttons.items():
            if token == self._active_fade:
                btn.add_css_class("ilp-selected")
            else:
                btn.remove_css_class("ilp-selected")

    def _refresh_fit_buttons(self) -> None:
        for token, btn in self._fit_buttons.items():
            if token == self._active_fit:
                btn.add_css_class("ilp-selected")
            else:
                btn.remove_css_class("ilp-selected")

    def _refresh_focal_buttons(self) -> None:
        for token, btn in self._focal_buttons.items():
            if token == self._active_focal:
                btn.add_css_class("ilp-selected")
            else:
                btn.remove_css_class("ilp-selected")

    def _refresh_grayscale_scale(self) -> None:
        self._grayscale_scale.handler_block_by_func(self._on_grayscale_changed)
        try:
            self._grayscale_scale.set_value(self._active_grayscale)
        finally:
            self._grayscale_scale.handler_unblock_by_func(self._on_grayscale_changed)

    def _refresh_blur_scale(self) -> None:
        self._blur_scale.handler_block_by_func(self._on_blur_changed)
        try:
            self._blur_scale.set_value(self._active_blur)
        finally:
            self._blur_scale.handler_unblock_by_func(self._on_blur_changed)

    def _refresh_tint_ui(self) -> None:
        if self._active_tint is None:
            self._tint_none_btn.add_css_class("ilp-selected")
            self._tint_color_btn.remove_css_class("ilp-selected")
        else:
            self._tint_none_btn.remove_css_class("ilp-selected")
            self._tint_color_btn.add_css_class("ilp-selected")
            rgba = Gdk.RGBA()
            if rgba.parse(self._active_tint):
                self._tint_color_btn.set_rgba(rgba)

    def _refresh_flip_buttons(self) -> None:
        if self._active_flip_h:
            self._flip_h_btn.add_css_class("ilp-selected")
        else:
            self._flip_h_btn.remove_css_class("ilp-selected")
        if self._active_flip_v:
            self._flip_v_btn.add_css_class("ilp-selected")
        else:
            self._flip_v_btn.remove_css_class("ilp-selected")

    def _refresh_zoom_scale(self) -> None:
        self._zoom_scale.handler_block_by_func(self._on_zoom_changed)
        try:
            self._zoom_scale.set_value(self._active_zoom)
        finally:
            self._zoom_scale.handler_unblock_by_func(self._on_zoom_changed)

    # ── Public API ────────────────────────────────────────────────────────────

    def open_insert_mode(self) -> None:
        """Reset to defaults and open for inserting a new image."""
        self._edit_mode        = False
        self._edit_cb          = None
        self._active_pos       = "right"
        self._active_size      = "50"
        self._active_opacity   = 75
        self._active_fade      = _POSITION_DEFAULT_FADE["right"]
        self._active_fit       = "cover"
        self._active_focal     = "focal-center"
        self._active_grayscale = 0
        self._active_blur      = 0
        self._active_tint      = None
        self._active_flip_h    = False
        self._active_flip_v    = False
        self._active_zoom      = 100

        self._block_signals(True)
        self._grad_switch.set_active(True)
        self._alt_entry.set_text("")
        self._block_signals(False)

        self._refresh_pos_buttons()
        self._refresh_size_scale()
        self._refresh_opacity_buttons()
        self._refresh_fade_buttons()
        self._refresh_fit_buttons()
        self._refresh_focal_buttons()
        self._refresh_grayscale_scale()
        self._refresh_blur_scale()
        self._refresh_tint_ui()
        self._refresh_flip_buttons()
        self._refresh_zoom_scale()

        self._insert_btn.set_label("Choose image…")
        self._insert_btn.set_visible(True)

        # Show AI generation button only when a Gemini key is configured.
        try:
            from .session import load_api_keys as _load_api_keys
            _claude_key, _gemini_key = _load_api_keys()
            self._ai_btn.set_visible(bool(_gemini_key))
            self._infographic_btn.set_visible(bool(_claude_key))
        except Exception:
            self._ai_btn.set_visible(False)
            self._infographic_btn.set_visible(False)

        # Insert mode: autohide ON so clicking outside dismisses the popover.
        self.set_autohide(True)

        # Capture the parent window now, while the popover is still attached
        # to the widget tree.  After popup() + autohide fires (when the file
        # dialog steals focus), get_root() on self returns None.
        self._parent_window = self.get_root()

        self.popup()

    def open_edit_mode(self, layout: dict, description: str,
                       edit_cb: Callable[[dict, str], None]) -> None:
        """
        Pre-populate from *layout* + *description* and open in edit mode.

        *edit_cb(layout, description)* is called immediately on every change.
        """
        self._edit_mode        = True
        self._edit_cb          = edit_cb
        self._active_pos       = layout.get("position", "right")
        self._active_size      = layout.get("size", "50")
        self._active_opacity   = layout.get("opacity", 75)
        # Fall back to the position default when the document has no fade token.
        self._active_fade = (
            layout.get("fade")
            or _POSITION_DEFAULT_FADE.get(self._active_pos, "left")
        )
        self._active_fit       = layout.get("fit", "cover")
        self._active_focal     = layout.get("focal", "focal-center")
        self._active_grayscale = layout.get("grayscale", 0)
        self._active_blur      = layout.get("blur", 0)
        self._active_tint      = layout.get("tint", None)
        self._active_flip_h    = layout.get("flip_h", False)
        self._active_flip_v    = layout.get("flip_v", False)
        self._active_zoom      = layout.get("zoom", 100)

        self._block_signals(True)
        self._grad_switch.set_active(layout.get("gradient", True))
        self._alt_entry.set_text(description)
        self._block_signals(False)

        self._refresh_pos_buttons()
        self._refresh_size_scale()
        self._refresh_opacity_buttons()
        self._refresh_fade_buttons()
        self._refresh_fit_buttons()
        self._refresh_focal_buttons()
        self._refresh_grayscale_scale()
        self._refresh_blur_scale()
        self._refresh_tint_ui()
        self._refresh_flip_buttons()
        self._refresh_zoom_scale()

        self._insert_btn.set_visible(False)
        self._ai_btn.set_visible(False)
        self._infographic_btn.set_visible(False)

        # Edit mode: autohide OFF so the click on the GtkSourceView that
        # triggered this popover does not immediately dismiss it.  The popover
        # is dismissed explicitly by _dismiss_img_popover() when the cursor
        # moves off the image line (via the 300 ms cursor-polling timer).
        self.set_autohide(False)

        self.popup()

    # ── Signal helpers ────────────────────────────────────────────────────────

    def _block_signals(self, block: bool) -> None:
        fn = "handler_block_by_func" if block else "handler_unblock_by_func"
        getattr(self._grad_switch,      fn)(self._on_grad_changed)
        getattr(self._alt_entry,        fn)(self._on_alt_changed)
        getattr(self._size_scale,       fn)(self._on_size_changed)
        getattr(self._grayscale_scale,  fn)(self._on_grayscale_changed)
        getattr(self._blur_scale,       fn)(self._on_blur_changed)
        getattr(self._zoom_scale,       fn)(self._on_zoom_changed)

    def _current_layout(self) -> dict:
        grad_on = self._grad_switch.get_active()
        is_bg   = self._active_pos == "background"
        fit     = self._active_fit
        focal   = self._active_focal if (fit == "cover" and not is_bg) else "focal-center"
        return {
            "position":  self._active_pos,
            "size":      self._active_size,
            "gradient":  grad_on,
            "opacity":   self._active_opacity,
            "fade":      self._active_fade if (grad_on and not is_bg) else None,
            "fit":       fit,
            "focal":     focal,
            "grayscale": self._active_grayscale,
            "blur":      self._active_blur,
            "tint":      self._active_tint,
            "flip_h":    self._active_flip_h,
            "flip_v":    self._active_flip_v,
            "zoom":      self._active_zoom,
        }

    def _schedule_writeback(self) -> None:
        """Debounce write-back 150 ms so rapid clicks don't spam the buffer."""
        if not self._edit_mode or self._edit_cb is None:
            return
        if self._writeback_source is not None:
            GLib.source_remove(self._writeback_source)
        self._writeback_source = GLib.timeout_add(150, self._do_writeback)

    def _do_writeback(self) -> bool:
        self._writeback_source = None
        if self._edit_mode and self._edit_cb is not None:
            self._edit_cb(self._current_layout(), self._alt_entry.get_text())
        return GLib.SOURCE_REMOVE

    # ── Control signal handlers ───────────────────────────────────────────────

    def _on_pos_clicked(self, btn: Gtk.Button, token: str) -> None:
        self._active_pos  = token
        self._active_fade = _POSITION_DEFAULT_FADE.get(token, "left")
        self._refresh_pos_buttons()
        self._refresh_fade_buttons()
        self._schedule_writeback()

    def _on_size_changed(self, scale: Gtk.Scale) -> None:
        self._active_size = str(int(scale.get_value()))
        self._schedule_writeback()

    def _on_opacity_clicked(self, btn: Gtk.Button, token: str) -> None:
        self._active_opacity = int(token)
        self._refresh_opacity_buttons()
        self._schedule_writeback()

    def _on_fade_clicked(self, btn: Gtk.Button, token: str) -> None:
        self._active_fade = token
        self._refresh_fade_buttons()
        self._schedule_writeback()

    def _on_fit_clicked(self, btn: Gtk.Button, token: str) -> None:
        self._active_fit = token
        self._refresh_fit_buttons()
        if hasattr(self, "_focal_box"):
            is_bg = self._active_pos == "background"
            self._focal_box.set_visible(not is_bg and token == "cover")
        self._schedule_writeback()

    def _on_focal_clicked(self, btn: Gtk.Button, token: str) -> None:
        self._active_focal = token
        self._refresh_focal_buttons()
        self._schedule_writeback()

    def _on_grayscale_changed(self, scale: Gtk.Scale) -> None:
        self._active_grayscale = int(scale.get_value())
        self._schedule_writeback()

    def _on_blur_changed(self, scale: Gtk.Scale) -> None:
        self._active_blur = int(scale.get_value())
        self._schedule_writeback()

    def _on_tint_none_clicked(self, btn: Gtk.Button) -> None:
        self._active_tint = None
        self._refresh_tint_ui()
        self._schedule_writeback()

    def _on_tint_color_set(self, btn: Gtk.ColorButton) -> None:
        rgba = btn.get_rgba()
        r = int(rgba.red   * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue  * 255)
        self._active_tint = f"#{r:02x}{g:02x}{b:02x}"
        self._refresh_tint_ui()
        self._schedule_writeback()

    def _on_flip_h_clicked(self, btn: Gtk.Button) -> None:
        self._active_flip_h = not self._active_flip_h
        self._refresh_flip_buttons()
        self._schedule_writeback()

    def _on_flip_v_clicked(self, btn: Gtk.Button) -> None:
        self._active_flip_v = not self._active_flip_v
        self._refresh_flip_buttons()
        self._schedule_writeback()

    def _on_zoom_changed(self, scale: Gtk.Scale) -> None:
        self._active_zoom = int(scale.get_value())
        self._schedule_writeback()

    def _on_grad_changed(self, switch: Gtk.Switch, state: bool) -> bool:
        if hasattr(self, "_fade_box"):
            is_bg = self._active_pos == "background"
            self._fade_box.set_visible(not is_bg and state)
        self._schedule_writeback()
        return False   # allow GTK to apply the visual state

    def _on_alt_changed(self, entry: Gtk.Entry) -> None:
        self._schedule_writeback()

    # ── Insert mode action ────────────────────────────────────────────────────

    def _on_insert_clicked(self, *_) -> None:
        """Build alt token string, close popover, open file dialog."""
        layout = self._current_layout()
        desc   = self._alt_entry.get_text()
        alt    = _build_alt(desc, layout)

        # Use the window reference captured in open_insert_mode() — by the time
        # this button is clicked autohide may have already unparented the popover,
        # making get_root() return None.
        parent_window = self._parent_window

        self.popdown()

        dialog = Gtk.FileDialog.new()
        dialog.set_title("Choose image")
        img_filter = Gtk.FileFilter()
        img_filter.set_name("Images")
        for pat in ("*.png", "*.jpg", "*.jpeg", "*.gif",
                    "*.svg", "*.webp", "*.bmp", "*.tiff"):
            img_filter.add_pattern(pat)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(img_filter)
        dialog.set_filters(store)

        # Keep a strong reference so the GC cannot collect the dialog before
        # the async callback fires.  Cleared in the callback.
        self._active_file_dialog = dialog

        # Capture alt and insert_cb in a closure — Gtk.FileDialog.open() in
        # PyGObject does NOT support a user_data argument; the callback receives
        # only (dialog, result).  Passing extra arguments silently breaks the call.
        insert_cb = self._insert_cb

        def _on_done(dlg, result):
            self._active_file_dialog = None
            try:
                gfile = dlg.open_finish(result)
            except GLib.Error:
                return
            path = gfile.get_path() or gfile.get_uri()
            if path:
                insert_cb(alt, path)

        dialog.open(parent_window, None, _on_done)

    def _on_generate_ai_clicked(self, *_) -> None:
        """Close the popover and open the AI image generation dialog."""
        layout        = self._current_layout()
        scene         = self._alt_entry.get_text().strip()
        parent_window = self._parent_window
        insert_cb     = self._insert_cb

        # When the alt-text entry is empty, derive a scene hint from the slide
        # the cursor is currently on (headline + first sentence(s) of notes).
        if not scene and parent_window is not None:
            editor = getattr(parent_window, "_editor", None)
            if editor is not None:
                scene = editor.get_current_slide_scene_hint()

        # Validate prerequisites before opening the dialog.
        if parent_window and not getattr(parent_window, "_file_path", None):
            if hasattr(parent_window, "_show_toast"):
                parent_window._show_toast(
                    "Save the document first — AI images are written to "
                    "assets/ next to the .md file."
                )
            return

        self.popdown()

        from .ai_image_dialog import AIImageDialog
        dlg = AIImageDialog(
            parent_window, layout, insert_cb, initial_scene=scene
        )
        dlg.present(parent_window)

    def _on_infographic_clicked(self, *_) -> None:
        """Close the popover and open the AI infographic generation dialog."""
        layout        = self._current_layout()
        parent_window = self._parent_window
        insert_cb     = self._insert_cb

        if parent_window and not getattr(parent_window, "_file_path", None):
            if hasattr(parent_window, "_show_toast"):
                parent_window._show_toast(
                    "Save the document first — infographics are written to "
                    "assets/ next to the .md file."
                )
            return

        slide_md = ""
        if parent_window is not None:
            editor = getattr(parent_window, "_editor", None)
            if editor is not None:
                slide_md = editor.get_current_slide_markdown()

        self.popdown()

        from .ai_infographic_dialog import AIInfographicDialog
        dlg = AIInfographicDialog(
            parent_window, layout, insert_cb, initial_slide_md=slide_md
        )
        dlg.present(parent_window)


class _TableInsertPopover(Gtk.Popover):
    """
    Grid-picker popover for inserting a Markdown table.

    Hover over the grid to choose dimensions; click to insert.
    Maximum 6×6. Default highlighted selection updates as the
    mouse moves over cells.
    """
    _MAX_COLS = 6
    _MAX_ROWS = 6
    _CELL_PX  = 22

    def __init__(self, parent_widget: Gtk.Widget,
                 insert_cb) -> None:
        super().__init__()
        self._insert_cb = insert_cb
        self._hover_col = 0
        self._hover_row = 0
        self.set_parent(parent_widget)
        self.set_has_arrow(True)
        self.set_autohide(True)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(10)
        root.set_margin_bottom(10)
        root.set_margin_start(10)
        root.set_margin_end(10)

        self._size_label = Gtk.Label(label="Insert table")
        self._size_label.add_css_class("caption")
        self._size_label.add_css_class("dim-label")
        root.append(self._size_label)

        # Drawing area for the cell grid
        self._drawing = Gtk.DrawingArea()
        w = self._MAX_COLS * (self._CELL_PX + 3) + 3
        h = self._MAX_ROWS * (self._CELL_PX + 3) + 3
        self._drawing.set_size_request(w, h)
        self._drawing.set_draw_func(self._draw)

        # Motion tracking
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave",  self._on_leave)
        self._drawing.add_controller(motion)

        # Click to insert
        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self._drawing.add_controller(click)

        root.append(self._drawing)
        self.set_child(root)

    def _draw(self, area, cr, width, height) -> None:
        gap = 3
        sz  = self._CELL_PX
        style = area.get_style_context()
        found, accent = style.lookup_color("accent_bg_color")
        if not found:
            accent = style.get_color()
        for row in range(self._MAX_ROWS):
            for col in range(self._MAX_COLS):
                x = col * (sz + gap) + gap
                y = row * (sz + gap) + gap
                selected = (col < self._hover_col
                            and row < self._hover_row)
                if selected:
                    cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.85)
                else:
                    cr.set_source_rgba(0.28, 0.28, 0.30, 0.9)
                cr.rectangle(x, y, sz, sz)
                cr.fill()
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.08)
                cr.rectangle(x, y, sz, sz)
                cr.stroke()

    def _on_motion(self, ctrl, x, y) -> None:
        sz  = self._CELL_PX
        gap = 3
        col = min(self._MAX_COLS, int(x // (sz + gap)) + 1)
        row = min(self._MAX_ROWS, int(y // (sz + gap)) + 1)
        if col != self._hover_col or row != self._hover_row:
            self._hover_col = col
            self._hover_row = row
            self._size_label.set_label(
                f"{col} × {row} table" if col > 0 and row > 0 else "Insert table"
            )
            self._drawing.queue_draw()

    def _on_leave(self, ctrl) -> None:
        self._hover_col = 0
        self._hover_row = 0
        self._size_label.set_label("Insert table")
        self._drawing.queue_draw()

    def _on_click(self, ctrl, n_press, x, y) -> None:
        cols = self._hover_col
        rows = self._hover_row
        if cols < 1 or rows < 1:
            return
        self.popdown()
        self._insert_cb(cols, rows)


# Minimum-width strategy
# ─────────────────────────────────────────────────────────────────────────────
# PyGObject 3.x does NOT dispatch Python do_measure() overrides via the GTK4
# GTypeClass vfunc slot.  Defining do_measure in a Python subclass has no
# effect — GTK's C code never calls it.
#
# What actually works in this environment:
#
# 1. GtkTextView / GtkSourceView with wrap_mode=WORD_CHAR: the C-level
#    measure() already reports minimum_width=0 (only natural=longest-line).
#    GtkScrolledWindow(NEVER) with the text view as direct child allocates
#    the text view max(0, viewport_width) = viewport_width → text wraps.
#
# 2. The editor toolbar (20+ buttons ≈ 1050 px natural) is wrapped in a
#    GtkScrolledWindow(EXTERNAL, NEVER).  GTK's C code for EXTERNAL policy
#    reports minimum=0 to the parent while allocating the toolbar its full
#    natural width internally.  set_overflow(HIDDEN) on the scroll window
#    clips any buttons that extend beyond the window edge.  This prevents the
#    toolbar's natural width from propagating as the window's minimum, which
#    was the root cause of the AdwToastOverlay warnings and the header-bar
#    right buttons becoming inaccessible at medium window widths.


# ── Editor ────────────────────────────────────────────────────────────────────

class Editor(Gtk.Box):
    """
    Markdown editor with formatting toolbar, find/replace, and
    contextual image-layout editing (iA Presenter-style).

    Signals:
        changed (text: str)  — emitted ~400 ms after the last keystroke
    """

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    DEBOUNCE_MS = 400

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._debounce_source:  int | None = None
        self._base_path:        Path | None = None
        self._insert_image_cb = None
        # Lazily constructed (created on first use, reused thereafter).
        # _image_layout_popover is ONLY used for the contextual edit popover
        # that appears when the cursor is on an image tag (parented to the
        # invisible overlay anchor).  It must never be re-parented.
        self._image_layout_popover: ImageLayoutPopover | None = None
        # Separate popover instance exclusively for the toolbar insert button.
        # Keeping them separate avoids the re-parenting that caused the popover
        # to be parented to the invisible anchor and dismissed instantly.
        self._insert_image_popover: ImageLayoutPopover | None = None
        # The toolbar insert-image button — used as popover anchor
        self._img_toolbar_btn:  Gtk.Button | None = None
        self._font_size: int = 13
        self._line_length: int = 64  # column position for margin guide
        self._table_popover: _TableInsertPopover | None = None

        # Slide-number badge overlay (DrawingArea over the gutter)
        self._badge_draw:    Gtk.DrawingArea | None = None
        self._badge_starts:  list[tuple[int,int]] = []  # (slide_num, line_no)
        self._slide_ranges:  list[tuple[int,int]] = []  # (first, last) per slide

        # Image-edit popover state
        # Last line number that had an image tag — avoids re-scanning if
        # the cursor stays on the same line.
        self._last_img_line:    int = -1
        # The full match (src, alt) of the currently-tracked image tag
        self._current_img_match: tuple[str, str] | None = None  # (alt, src)

        if _GTKSOURCE_AVAILABLE:
            self._init_source_view()
        else:
            self._init_plain_view()

        self.connect("destroy", self._on_destroy)

        # Find bar inside a Revealer for slide-down animation (#53)
        self._find_revealer = Gtk.Revealer()
        self._find_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN
        )
        self._find_revealer.set_transition_duration(150)
        self._find_bar = self._build_find_bar()
        self._find_bar.set_visible(True)   # Revealer controls visibility
        self._find_revealer.set_child(self._find_bar)
        self.append(self._find_revealer)

        # Overlay wraps the scroll window so we can position the image-edit
        # anchor widget anywhere over the editor text area.
        # GtkScrolledWindow(NEVER) with GtkTextView/GtkSourceView as direct child:
        # the text view implements GtkScrollable with hscroll-policy=MINIMUM, and
        # GTK's C measure() reports minimum_width=0 for WORD_CHAR wrap mode.
        # The scroll window therefore also reports minimum=0 and allocates the
        # text view exactly viewport_width → text reflows correctly on resize.
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self._view)

        self._img_edit_overlay = Gtk.Overlay()
        self._img_edit_overlay.set_child(scroll)
        self._img_edit_overlay.set_vexpand(True)

        # Toolbar: wrap in a ScrolledWindow(EXTERNAL, NEVER) so that GTK's
        # C-level EXTERNAL-policy measure() reports minimum=0 to the parent.
        # The toolbar itself still gets its full natural width allocated and is
        # clipped by set_overflow(HIDDEN).  This prevents the toolbar's ~1050 px
        # natural width from propagating as the window minimum.
        toolbar_scroll = Gtk.ScrolledWindow()
        toolbar_scroll.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        toolbar_scroll.set_child(self._build_toolbar())
        toolbar_scroll.set_overflow(Gtk.Overflow.HIDDEN)

        self.append(self._img_edit_overlay)
        self.append(toolbar_scroll)

        # Slide-number badge drawing area — overlaid on the gutter column.
        # Positioned at the far left of the overlay (over the gutter).
        # pick_from(NONE) so it never intercepts mouse events.
        if _GTKSOURCE_AVAILABLE:
            # Slide-number badge drawing area — overlaid on the gutter column.
            # set_can_target(False) so it never intercepts mouse events.
            self._badge_draw = Gtk.DrawingArea()
            self._badge_draw.set_halign(Gtk.Align.START)
            self._badge_draw.set_valign(Gtk.Align.FILL)
            self._badge_draw.set_vexpand(True)
            # Width covers the full left area (gutter + text left-margin).
            # The badge X position is computed in _draw_slide_badges.
            self._badge_draw.set_size_request(120, -1)
            self._badge_draw.set_margin_start(0)
            self._badge_draw.set_can_target(False)
            self._badge_draw.set_draw_func(self._draw_slide_badges)
            self._img_edit_overlay.add_overlay(self._badge_draw)
            # Recompute slide starts whenever the buffer changes.
            self._buffer.connect('changed', self._update_badge_starts)
            GLib.idle_add(self._update_badge_starts)
            # After realize, measure the gutter width and wire up scroll.
            self._view.connect('realize', self._on_view_realize_badges)

        self._drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        self._drop_target.connect("drop",   self._on_drop)
        self._drop_target.connect("motion", self._on_drop_motion)
        # "leave" signal not connected — no visual feedback needed on drag leave
        self._view.add_controller(self._drop_target)

        # Click handler: open the image-layout popover when the user clicks
        # on a line that contains an image tag.  Use button=1, released signal
        # so it fires after the cursor has moved to the clicked position.
        _img_click = Gtk.GestureClick()
        _img_click.set_button(1)
        _img_click.connect("released", self._on_view_click_for_image)
        self._view.add_controller(_img_click)

    # ── View initialisation ───────────────────────────────────────────────────

    def _init_source_view(self) -> None:
        lang_manager = GtkSource.LanguageManager.get_default()
        lang         = lang_manager.get_language("markdown")

        self._buffer = GtkSource.Buffer()
        if lang:
            self._buffer.set_language(lang)
        self._buffer.set_highlight_syntax(True)
        self._buffer.set_highlight_matching_brackets(True)
        self._apply_style()
        self._buffer.connect("changed", self._on_buffer_changed)

        self._search_settings = GtkSource.SearchSettings()
        self._search_settings.set_wrap_around(True)
        self._search_context  = GtkSource.SearchContext.new(
            self._buffer, self._search_settings
        )
        self._search_context.set_highlight(True)

        self._view = GtkSource.View()
        self._view.set_buffer(self._buffer)
        self._view.set_monospace(True)
        self._view.set_show_line_numbers(True)
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._view.set_tab_width(4)
        self._view.set_insert_spaces_instead_of_tabs(True)
        self._view.set_auto_indent(True)
        self._view.set_highlight_current_line(True)
        self._view.set_left_margin(52)
        self._view.set_right_margin(16)
        self._view.set_top_margin(12)
        self._view.set_bottom_margin(12)
        # Use GtkSourceView's built-in column guide — this draws a visual line
        # at the configured column without inflating the widget's minimum size.
        self._view.set_show_right_margin(True)
        self._view.set_right_margin_position(self._line_length)

        self._css_provider = Gtk.CssProvider()
        self._view.get_style_context().add_provider(
            self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._apply_font_css()
        self._has_search = True

    def _init_plain_view(self) -> None:
        self._buffer = Gtk.TextBuffer()
        self._buffer.connect("changed", self._on_buffer_changed)

        self._view = Gtk.TextView()
        self._view.set_buffer(self._buffer)
        self._view.set_monospace(True)
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._view.set_left_margin(52)
        self._view.set_right_margin(16)
        self._view.set_top_margin(12)
        self._view.set_bottom_margin(12)

        self._css_provider = Gtk.CssProvider()
        self._view.get_style_context().add_provider(
            self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._apply_font_css()
        self._has_search = False

    def _apply_font_css(self) -> None:
        """Apply the current font size via CSS."""
        size = str(self._font_size)
        css = "textview { font-family: Monospace; font-size: " + size + "pt; }"
        try:
            self._css_provider.load_from_string(css)
        except AttributeError:
            self._css_provider.load_from_data(css.encode())

    # ── Public API ────────────────────────────────────────────────────────────

    def get_text(self) -> str:
        start = self._buffer.get_start_iter()
        end   = self._buffer.get_end_iter()
        return self._buffer.get_text(start, end, True)

    def get_cursor_offset(self) -> int:
        """Return the character offset of the insertion cursor in the buffer."""
        cursor = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        return cursor.get_offset()

    def set_text(self, text: str) -> None:
        if self._debounce_source is not None:
            GLib.source_remove(self._debounce_source)
            self._debounce_source = None
        self._buffer.handler_block_by_func(self._on_buffer_changed)
        try:
            self._buffer.set_text(text)
        finally:
            self._buffer.handler_unblock_by_func(self._on_buffer_changed)
        # Reset image-edit and slide state when document is replaced
        self._last_img_line     = -1
        self._current_img_match = None
        self._badge_starts      = []
        self._slide_ranges      = []
        # Schedule badge and image-tag scan now that the buffer has content
        GLib.idle_add(self._update_badge_starts)
        GLib.idle_add(self._highlight_image_tags)

    def set_text_as_user_action(self, text: str) -> None:
        """Replace buffer contents as one undoable step (e.g. slide reorder)."""
        if self._debounce_source is not None:
            GLib.source_remove(self._debounce_source)
            self._debounce_source = None
        self._buffer.handler_block_by_func(self._on_buffer_changed)
        try:
            if _GTKSOURCE_AVAILABLE:
                # GtkSource.Buffer (and Gtk.TextBuffer) always have
                # begin/end_user_action — no hasattr check required.
                self._buffer.begin_user_action()
                try:
                    start = self._buffer.get_start_iter()
                    end   = self._buffer.get_end_iter()
                    self._buffer.delete(start, end)
                    self._buffer.insert(self._buffer.get_start_iter(), text)
                finally:
                    # Always close the user action even if delete/insert raises,
                    # otherwise the buffer is left permanently inside an action.
                    self._buffer.end_user_action()
            else:
                self._buffer.set_text(text)
        finally:
            self._buffer.handler_unblock_by_func(self._on_buffer_changed)

    def scroll_to_slide(self, slide_index: int) -> None:
        from .slides.utils import compute_slide_offsets

        full_text = self.get_text()
        slide_offsets = compute_slide_offsets(full_text)
        if not slide_offsets or slide_index >= len(slide_offsets):
            return

        target_offset = slide_offsets[slide_index]
        end_offset = (
            slide_offsets[slide_index + 1]
            if slide_index + 1 < len(slide_offsets)
            else len(full_text)
        )
        target_line = full_text.count('\n', 0, target_offset)
        end_line    = full_text.count('\n', 0, end_offset)

        ok1, start_it = self._buffer.get_iter_at_line(target_line)
        ok2, end_it   = self._buffer.get_iter_at_line(end_line)
        if not ok1 or not ok2:
            return
        end_it.forward_to_line_end()
        self._buffer.select_range(start_it, end_it)
        self._view.scroll_to_iter(start_it, 0.05, True, 0.0, 0.0)
        self._view.grab_focus()

    # ── Undo / redo ───────────────────────────────────────────────────────────

    def undo(self) -> None:
        if self._buffer.get_can_undo():
            self._buffer.undo()
        self._view.grab_focus()

    def redo(self) -> None:
        if self._buffer.get_can_redo():
            self._buffer.redo()
        self._view.grab_focus()

    def connect_undo_notify(self, callback) -> None:
        def _notify(*_):
            callback(self._buffer.get_can_undo(), self._buffer.get_can_redo())
        self._buffer.connect("notify::can-undo", _notify)
        self._buffer.connect("notify::can-redo", _notify)

    def set_font_size(self, pt: int) -> None:
        pt = max(8, min(pt, 32))
        if pt == self._font_size:
            return
        self._font_size = pt
        self._apply_font_css()

    def get_font_size(self) -> int:
        return self._font_size

    def set_line_length(self, cols: int) -> None:
        """Update the column guide position."""
        self._line_length = cols
        self._update_wrap_margin()

    def get_line_length(self) -> int:
        return self._line_length

    def _update_wrap_margin(self) -> None:
        """Update the GtkSourceView column guide to _line_length columns."""
        if not _GTKSOURCE_AVAILABLE:
            return
        cols = self._line_length
        if cols > 0:
            self._view.set_show_right_margin(True)
            self._view.set_right_margin_position(cols)
        else:
            self._view.set_show_right_margin(False)


    def set_syntax_highlight(self, enabled: bool) -> None:
        if _GTKSOURCE_AVAILABLE and hasattr(self._buffer, "set_highlight_syntax"):
            self._buffer.set_highlight_syntax(enabled)

    def set_line_numbers(self, enabled: bool) -> None:
        if _GTKSOURCE_AVAILABLE and hasattr(self._view, "set_show_line_numbers"):
            self._view.set_show_line_numbers(enabled)

    def set_highlight_current_line(self, enabled: bool) -> None:
        if _GTKSOURCE_AVAILABLE and hasattr(self._view, "set_highlight_current_line"):
            self._view.set_highlight_current_line(enabled)

    def set_auto_indent(self, enabled: bool) -> None:
        if _GTKSOURCE_AVAILABLE and hasattr(self._view, "set_auto_indent"):
            self._view.set_auto_indent(enabled)

    def set_spaces_instead_of_tabs(self, enabled: bool) -> None:
        if _GTKSOURCE_AVAILABLE and hasattr(self._view,
                                            "set_insert_spaces_instead_of_tabs"):
            self._view.set_insert_spaces_instead_of_tabs(enabled)

    def insert_image_markdown(self, alt: str, rel_path: str) -> None:
        self._replace_selection(f"![{alt}]({rel_path})")

    def get_current_slide_markdown(self) -> str:
        """Return the full raw Markdown of the slide at the current cursor position."""
        buf = self._buffer
        full_text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        cursor_line = buf.get_iter_at_mark(buf.get_insert()).get_line()
        lines = full_text.splitlines()
        sep_lines = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
        slide_start = 0
        slide_end = len(lines)
        for sep in sep_lines:
            if sep <= cursor_line:
                slide_start = sep + 1
            else:
                slide_end = sep
                break
        return "\n".join(lines[slide_start:slide_end])

    def get_current_slide_scene_hint(self) -> str:
        """
        Return a scene-description hint for the slide at the current cursor.

        Finds the slide boundaries by locating the nearest '---' separator
        lines above and below the cursor, then delegates to
        slides.image_gen.scene_hint_from_slide() to combine the heading with
        the first sentence or two of speaker notes.
        """
        from .slides.image_gen import scene_hint_from_slide

        buf = self._buffer
        full_text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        cursor_line = buf.get_iter_at_mark(buf.get_insert()).get_line()

        lines = full_text.splitlines()
        sep_lines = [i for i, l in enumerate(lines) if l.strip() == "---"]

        slide_start = 0
        slide_end = len(lines)
        for sep in sep_lines:
            if sep <= cursor_line:
                slide_start = sep + 1
            else:
                slide_end = sep
                break

        return scene_hint_from_slide("\n".join(lines[slide_start:slide_end]))

    def get_img_toolbar_btn(self) -> "Gtk.Button | None":
        return self._img_toolbar_btn

    def open_image_layout_popover(self, anchor: "Gtk.Widget") -> None:
        self._open_image_layout_popover(anchor)

    # ── Contextual image editing ──────────────────────────────────────────────

    def check_cursor_for_image(self) -> None:
        """
        Called by the window's 300ms cursor-polling timer.

        Only responsible for DISMISSAL: if the cursor has moved to a line
        that does not contain an image tag, close the popover.

        Opening the popover is handled by _on_view_click_for_image(), which
        fires on an explicit mouse click — not by this timer.  This avoids
        the popover opening unexpectedly on every cursor movement and prevents
        it from interfering with scrolling.
        """
        if self._image_layout_popover is None:
            return
        if not self._image_layout_popover.get_visible():
            return   # nothing to dismiss

        cursor  = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        line_no = cursor.get_line()

        # Fast path: cursor is still on the image line — keep popover open.
        if line_no == self._last_img_line:
            return

        # Cursor moved off the image line — dismiss.
        self._dismiss_img_popover()

    def _dismiss_img_popover(self) -> None:
        """Close the image-edit popover and reset line tracking."""
        self._current_img_match = None
        self._last_img_line     = -1
        if self._image_layout_popover is not None:
            if self._image_layout_popover.get_visible():
                self._image_layout_popover.popdown()

    def _open_img_edit_popover_for_line(self, line_no: int) -> None:
        """
        Open the image-edit popover for the image tag on *line_no*.

        Called from the click handler when the user clicks on an image line.
        Does nothing if the line contains no image tag.
        """
        ok, line_start = self._buffer.get_iter_at_line(line_no)
        if not ok:
            return
        line_end = line_start.copy()
        line_end.forward_to_line_end()
        line_text = self._buffer.get_text(line_start, line_end, True)

        m = _IMAGE_RE.search(line_text)
        if not m:
            return

        alt_raw      = m.group(1)
        src          = m.group(2)
        desc, layout = _parse_alt(alt_raw)

        self._current_img_match = (alt_raw, src, line_no)
        self._last_img_line     = line_no

        # Create/retrieve the popover first, then position it — set_pointing_to
        # requires the popover to exist and be parented to the view.
        popover = self._ensure_edit_popover()
        self._position_img_anchor(line_start)
        popover.open_edit_mode(layout, desc, self._on_img_edit)

    def _position_img_anchor(self, line_iter) -> None:
        """
        Point the edit popover at the pixel rectangle of *line_iter*'s line.

        Uses Gtk.Popover.set_pointing_to() with the line's bounding rectangle
        in widget (view) coordinates.  This is the correct GTK4 approach for
        positioning a popover at a text location — the popover is parented to
        the view widget and the pointing rectangle is in view-local coords.
        """
        popover = self._image_layout_popover
        if popover is None:
            return
        try:
            buf_rect = self._view.get_iter_location(line_iter)
            # buffer_to_window_coords with WIDGET gives view-local coords,
            # which is what set_pointing_to expects when the popover is
            # parented to the view.
            tx, ty = self._view.buffer_to_window_coords(
                Gtk.TextWindowType.WIDGET, buf_rect.x, buf_rect.y
            )
            rect = Gdk.Rectangle()
            rect.x      = tx
            rect.y      = ty
            rect.width  = max(1, buf_rect.width)
            rect.height = max(1, buf_rect.height)
            popover.set_pointing_to(rect)
        except Exception:
            pass

    def _ensure_edit_popover(self) -> ImageLayoutPopover:
        """
        Return (creating if needed) the shared image-edit popover.

        The popover is parented to self._view (the GtkSourceView) rather than
        to the invisible anchor label.  Position is set via set_pointing_to()
        with the pixel rectangle of the image line so the arrow points directly
        at the tag.  This avoids the anchor-offset problem where the popover
        appeared at the top-left because a 1×1 hidden widget has a zero-size
        allocation until it becomes visible.
        """
        if self._image_layout_popover is None:
            self._image_layout_popover = ImageLayoutPopover(
                self._view, self._on_layout_insert
            )
            # Reset line cache when popover closes so re-clicking same line works
            self._image_layout_popover.connect(
                "closed", lambda *_: setattr(self, "_last_img_line", -1)
            )
        return self._image_layout_popover

    def _on_img_edit(self, layout: dict, description: str) -> None:
        """
        Write-back callback: called by the popover whenever any control changes.

        Replaces the alt-text tokens in the existing image tag in-place,
        preserving the src path.  Wrapped in begin/end_user_action so the
        change appears as one undo step.
        """
        if self._current_img_match is None:
            return
        old_alt, src, line_no = self._current_img_match

        new_alt = _build_alt(description, layout)
        new_tag = f"![{new_alt}]({src})"

        ok, line_start = self._buffer.get_iter_at_line(line_no)
        if not ok:
            return
        line_end = line_start.copy()
        line_end.forward_to_line_end()
        line_text = self._buffer.get_text(line_start, line_end, True)

        # Find and replace only the image tag within the line
        m = _IMAGE_RE.search(line_text)
        if not m:
            return

        # Compute iterators for just the tag span
        tag_start = line_start.copy()
        tag_start.forward_chars(m.start())
        tag_end = line_start.copy()
        tag_end.forward_chars(m.end())

        # Block the changed signal so this write-back doesn't trigger a
        # full re-parse and re-render cycle during live editing
        self._buffer.handler_block_by_func(self._on_buffer_changed)
        try:
            # Wrap in a user action so the alt-text change is one undo step.
            # Both Gtk.TextBuffer and GtkSource.Buffer support begin/end_user_action.
            self._buffer.begin_user_action()
            try:
                self._buffer.delete(tag_start, tag_end)
                # Re-fetch iterator after deletion — TextIters are invalidated
                # by any buffer modification.
                ok2, tag_start2 = self._buffer.get_iter_at_line(line_no)
                if ok2:
                    tag_start2.forward_chars(m.start())
                    self._buffer.insert(tag_start2, new_tag)
            finally:
                self._buffer.end_user_action()
        finally:
            self._buffer.handler_unblock_by_func(self._on_buffer_changed)

        # Update the tracked alt so the next write-back has the right old value
        self._current_img_match = (new_alt, src, line_no)
        # Re-arm the debounce so a full conversion eventually fires
        if self._debounce_source is not None:
            GLib.source_remove(self._debounce_source)
        self._debounce_source = GLib.timeout_add(
            self.DEBOUNCE_MS, self._emit_changed
        )

    # ── Find bar ──────────────────────────────────────────────────────────────

    def show_find(self) -> None:
        self._find_revealer.set_reveal_child(True)
        self._replace_row.set_visible(False)
        self._search_entry.grab_focus()
        if self._buffer.get_has_selection():
            _ok, start, end = self._buffer.get_selection_bounds()
            self._search_entry.set_text(
                self._buffer.get_text(start, end, True)
            )

    def show_find_replace(self) -> None:
        self._find_revealer.set_reveal_child(True)
        self._replace_row.set_visible(True)
        self._search_entry.grab_focus()

    def hide_find(self) -> None:
        self._find_revealer.set_reveal_child(False)
        if self._has_search:
            self._search_settings.set_search_text("")
        self._view.grab_focus()

    def _build_find_bar(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        bar.append(sep)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_row.set_margin_start(8)
        search_row.set_margin_end(8)
        search_row.set_margin_top(4)
        search_row.set_margin_bottom(4)

        search_row.append(Gtk.Image.new_from_icon_name("edit-find-symbolic"))

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_hexpand(True)
        self._search_entry.set_placeholder_text("Find…")
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("activate", self._find_next)
        search_row.append(self._search_entry)

        self._match_label = Gtk.Label(label="")
        self._match_label.add_css_class("dim-label")
        self._match_label.add_css_class("caption")
        search_row.append(self._match_label)

        def _nav_btn(icon, tip, cb):
            b = Gtk.Button()
            b.set_child(Gtk.Image.new_from_icon_name(icon))
            b.set_tooltip_text(tip)
            b.update_property([Gtk.AccessibleProperty.LABEL], [tip])
            b.add_css_class("flat")
            b.connect("clicked", cb)
            return b

        search_row.append(_nav_btn("go-up-symbolic",   "Previous match", self._find_prev))
        search_row.append(_nav_btn("go-down-symbolic",  "Next match",     self._find_next))

        self._case_btn = Gtk.ToggleButton()
        self._case_btn.set_label("Aa")
        self._case_btn.set_tooltip_text("Match case")
        self._case_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Match case"])
        self._case_btn.add_css_class("flat")
        self._case_btn.connect("toggled", self._on_case_toggled)
        search_row.append(self._case_btn)

        self._regex_btn = Gtk.ToggleButton()
        self._regex_btn.set_label(".*")
        self._regex_btn.set_tooltip_text("Regular expression")
        self._regex_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Regular expression"])
        self._regex_btn.add_css_class("flat")
        self._regex_btn.connect("toggled", self._on_regex_toggled)
        search_row.append(self._regex_btn)

        close_btn = Gtk.Button()
        close_btn.set_child(
            Gtk.Image.new_from_icon_name("window-close-symbolic"))
        close_btn.set_tooltip_text("Close find bar (Escape)")
        close_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Close find bar"])
        close_btn.add_css_class("flat")
        close_btn.connect("clicked", lambda *_: self.hide_find())
        search_row.append(close_btn)

        bar.append(search_row)

        self._replace_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._replace_row.set_margin_start(8)
        self._replace_row.set_margin_end(8)
        self._replace_row.set_margin_bottom(4)
        self._replace_row.set_visible(False)

        self._replace_row.append(
            Gtk.Image.new_from_icon_name("edit-find-replace-symbolic"))

        self._replace_entry = Gtk.Entry()
        self._replace_entry.set_hexpand(True)
        self._replace_entry.set_placeholder_text("Replace with…")
        self._replace_entry.connect("activate", self._replace_one)
        self._replace_row.append(self._replace_entry)

        for label, cb in (("Replace", self._replace_one),
                           ("Replace all", self._replace_all)):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.connect("clicked", cb)
            self._replace_row.append(b)

        bar.append(self._replace_row)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_find_key)
        bar.add_controller(key_ctrl)

        return bar

    # ── Search signal handlers ────────────────────────────────────────────────

    def _on_search_changed(self, entry):
        if self._has_search:
            self._search_settings.set_search_text(entry.get_text())
            self._update_match_label()

    def _on_case_toggled(self, btn):
        if self._has_search:
            self._search_settings.set_case_sensitive(btn.get_active())
            self._update_match_label()

    def _on_regex_toggled(self, btn):
        if self._has_search:
            self._search_settings.set_regex_enabled(btn.get_active())
            self._update_match_label()

    def _find_next(self, *_):
        if not self._has_search:
            return
        insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        found, start, end, _w = self._search_context.forward(insert)
        if found:
            self._buffer.select_range(start, end)
            self._view.scroll_to_iter(start, 0.1, True, 0.5, 0.5)
        self._update_match_label()

    def _find_prev(self, *_):
        if not self._has_search:
            return
        insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        found, start, end, _w = self._search_context.backward(insert)
        if found:
            self._buffer.select_range(start, end)
            self._view.scroll_to_iter(start, 0.1, True, 0.5, 0.5)
        self._update_match_label()

    def _replace_one(self, *_):
        if not self._has_search:
            return
        replacement = self._replace_entry.get_text()
        if self._buffer.get_has_selection():
            _ok, start, end = self._buffer.get_selection_bounds()
            try:
                self._search_context.replace(start, end, replacement, -1)
            except Exception:
                self._match_label.set_text("Replace failed")
                return
        self._find_next()

    def _replace_all(self, *_):
        if not self._has_search:
            return
        try:
            count = self._search_context.replace_all(
                self._replace_entry.get_text(), -1)
            self._match_label.set_text(f"Replaced {count}")
        except Exception:
            self._match_label.set_text("Error")

    def _update_match_label(self):
        if not self._has_search:
            return
        count = self._search_context.get_occurrences_count()
        if not self._search_settings.get_search_text():
            self._match_label.set_text("")
        elif count == 0:
            self._match_label.set_text("No results")
        elif count == -1:
            self._match_label.set_text("…")
        else:
            self._match_label.set_text(
                f"{count} result{'s' if count != 1 else ''}")

    def _on_find_key(self, ctrl, keyval, keycode, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.hide_find()
            return True
        return False

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        bar.set_margin_start(6)
        bar.set_margin_end(6)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        bar.add_css_class("toolbar")

        def icon_btn(icon, tooltip, cb):
            b = Gtk.Button()
            b.set_child(Gtk.Image.new_from_icon_name(icon))
            b.set_tooltip_text(tooltip)
            b.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
            b.add_css_class("flat")
            b.connect("clicked", cb)
            return b

        def label_btn(label, tooltip, cb):
            b = Gtk.Button(label=label)
            b.set_tooltip_text(tooltip)
            b.add_css_class("flat")
            b.connect("clicked", cb)
            return b

        def sep():
            s = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            s.set_margin_start(4)
            s.set_margin_end(4)
            return s

        # Group 1: headings
        bar.append(label_btn("H1", "Heading 1", lambda *_: self._heading(1)))
        bar.append(label_btn("H2", "Heading 2", lambda *_: self._heading(2)))
        bar.append(label_btn("H3", "Heading 3", lambda *_: self._heading(3)))
        bar.append(sep())
        # Group 2: inline formatting
        bar.append(icon_btn("format-text-bold-symbolic",
                             "Bold (Ctrl+B)", lambda *_: self._wrap("**", "**", "bold text")))
        bar.append(icon_btn("format-text-italic-symbolic",
                             "Italic (Ctrl+I)", lambda *_: self._wrap("*", "*", "italic text")))
        bar.append(icon_btn("format-text-strikethrough-symbolic",
                             "Strikethrough", lambda *_: self._wrap("~~", "~~", "text")))
        bar.append(sep())
        # Group 3: slide structure — put early so they survive toolbar clipping
        bar.append(icon_btn("list-add-symbolic",
                             "New slide (---)", lambda *_: self._new_slide()))
        bar.append(icon_btn("view-dual-symbolic",
                             "Two columns (|||)", lambda *_: self._two_columns()))
        bar.append(icon_btn("document-edit-symbolic",
                             "Speaker notes (^^^)", lambda *_: self._speaker_notes()))
        bar.append(sep())
        # Group 4: find — keyboard shortcuts exist but toolbar access matters
        bar.append(icon_btn("edit-find-symbolic",
                             "Find (Ctrl+F)", lambda *_: self.show_find()))
        bar.append(icon_btn("edit-find-replace-symbolic",
                             "Find and replace (Ctrl+H)",
                             lambda *_: self.show_find_replace()))
        bar.append(sep())
        # Group 5: insertion
        bar.append(icon_btn("insert-link-symbolic",
                             "Insert link (Ctrl+K)", lambda *_: self._link()))

        self._img_toolbar_btn = icon_btn(
            "insert-image-symbolic", "Insert image",
            lambda *_: (self._insert_image_cb() if self._insert_image_cb
                        else self._open_image_layout_popover(self._img_toolbar_btn))
        )
        bar.append(self._img_toolbar_btn)

        self._tbl_toolbar_btn = icon_btn(
            'view-grid-symbolic', 'Insert table',
            lambda *_: self._open_table_popover(self._tbl_toolbar_btn)
        )
        bar.append(self._tbl_toolbar_btn)
        bar.append(sep())
        # Group 6: block formatting
        bar.append(icon_btn("format-text-plaintext-symbolic",
                             "Inline code", lambda *_: self._wrap("`", "`", "code")))
        bar.append(label_btn("{ }", "Code block", lambda *_: self._code_block()))
        bar.append(sep())
        bar.append(icon_btn("format-indent-more-symbolic",
                             "Blockquote", lambda *_: self._line_prefix("> ")))
        bar.append(icon_btn("view-list-bullet-symbolic",
                             "Bullet list", lambda *_: self._line_prefix("- ")))
        bar.append(icon_btn("view-list-ordered-symbolic",
                             "Numbered list", lambda *_: self._line_prefix("1. ")))
        bar.append(sep())
        bar.append(icon_btn("chat-message-new-symbolic",
                             "Insert slide comment",
                             lambda *_: self.insert_comment()))
        return bar

    # ── Formatting actions ────────────────────────────────────────────────────

    def bold(self) -> None:    self._wrap("**", "**", "bold text")
    def italic(self) -> None:  self._wrap("*",  "*",  "italic text")
    def insert_link(self) -> None: self._link()

    def insert_comment(self) -> None:
        self._replace_selection("<!-- note:  -->")
        insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        insert.backward_chars(4)
        self._buffer.place_cursor(insert)
        self._view.grab_focus()

    def _get_selection(self):
        if self._buffer.get_has_selection():
            _ok, start, end = self._buffer.get_selection_bounds()
            return self._buffer.get_text(start, end, True), True
        return "", False

    def _replace_selection(self, text: str) -> None:
        self._buffer.begin_user_action()
        try:
            if self._buffer.get_has_selection():
                self._buffer.delete_selection(True, True)
            self._buffer.insert_at_cursor(text)
        finally:
            self._buffer.end_user_action()
        self._view.grab_focus()

    def _wrap(self, before, after, placeholder) -> None:
        selected, has_sel = self._get_selection()
        text = selected if has_sel else placeholder
        self._replace_selection(f"{before}{text}{after}")
        if not has_sel:
            self._select_placeholder(placeholder, before)

    def _heading(self, level: int) -> None:
        self._buffer.begin_user_action()
        try:
            insert  = self._buffer.get_iter_at_mark(self._buffer.get_insert())
            line_no = insert.get_line()
            ok1, it = self._buffer.get_iter_at_line(line_no)
            if not ok1:
                return
            end_it = it.copy()
            end_it.forward_to_line_end()
            line     = self._buffer.get_text(it, end_it, True)
            stripped = line.lstrip("#").lstrip(" ")
            prefix   = "#" * level + " "
            self._buffer.delete(it, end_it)
            ok2, it2 = self._buffer.get_iter_at_line(line_no)
            if ok2:
                self._buffer.insert(it2, prefix + stripped)
        finally:
            self._buffer.end_user_action()
        self._view.grab_focus()

    def _line_prefix(self, prefix: str) -> None:
        self._buffer.begin_user_action()
        try:
            insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
            ok, it = self._buffer.get_iter_at_line(insert.get_line())
            if ok:
                self._buffer.insert(it, prefix)
        finally:
            self._buffer.end_user_action()
        self._view.grab_focus()

    def _code_block(self) -> None:
        selected, has_sel = self._get_selection()
        code = selected if has_sel else "code here"
        self._replace_selection(f"```\n{code}\n```")
        if not has_sel:
            self._select_placeholder("code here", "```\n")

    def _new_slide(self)     -> None: self._replace_selection("\n\n---\n\n")
    def _two_columns(self)   -> None: self._replace_selection("\n\n|||\n\n")
    def _speaker_notes(self) -> None:
        self._replace_selection("\n\n^^^\n\nSpeaker notes here")

    def _link(self) -> None:
        selected, has_sel = self._get_selection()
        text = selected if has_sel else "link text"
        self._replace_selection(f"[{text}](url)")
        if not has_sel:
            self._select_placeholder("link text", "[")

    def _open_table_popover(self, anchor: Gtk.Widget) -> None:
        """Open the table-dimension picker anchored to *anchor*."""
        if self._table_popover is None:
            self._table_popover = _TableInsertPopover(
                anchor, self._insert_table
            )
        self._table_popover.popup()

    def _insert_table(self, cols: int, rows: int) -> None:
        """Insert a Markdown table skeleton with *cols* columns and *rows* rows."""
        header = '| ' + ' | '.join(f'Col {c+1}' for c in range(cols)) + ' |'
        sep    = '|' + '|'.join('-------' for _ in range(cols)) + '|'
        body   = ('| ' + ' | '.join(' ' * 5 for _ in range(cols)) + ' |\n'
                  ) * (rows - 1)   # rows-1 because header is row 1
        self._replace_selection(f'\n{header}\n{sep}\n{body}')
        self._view.grab_focus()

    def _open_image_layout_popover(self, anchor: Gtk.Widget) -> None:
        """
        Open the image-layout popover in insert mode, anchored to the toolbar
        button.

        Uses a dedicated _insert_image_popover instance that is created once
        and permanently parented to the toolbar button.  This is intentionally
        separate from _image_layout_popover (the contextual edit popover
        parented to the invisible overlay anchor) — mixing the two caused the
        insert popover to be parented to the invisible anchor, which made GTK
        dismiss it instantly when autohide fired on the originating click event.
        """
        if self._insert_image_popover is None:
            # Create once, permanently parented to the toolbar button.
            self._insert_image_popover = ImageLayoutPopover(
                anchor, self._on_layout_insert
            )
        self._insert_image_popover.open_insert_mode()

    def _on_layout_insert(self, alt: str, rel_path: str) -> None:
        src_path = Path(rel_path)
        if self._base_path and src_path.is_absolute() and src_path.exists():
            assets_dir = self._base_path.parent / "assets"
            try:
                assets_dir.mkdir(exist_ok=True)
                dst = assets_dir / src_path.name
                if not dst.exists():
                    shutil.copy2(src_path, dst)
                rel_path = f"assets/{src_path.name}"
            except OSError:
                pass
        self._replace_selection(f"![{alt}]({rel_path})")
        self._view.grab_focus()

    # ── Drag and drop ─────────────────────────────────────────────────────────

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif",
                   ".svg", ".webp", ".bmp", ".tiff"}

    def _on_drop_motion(self, target, x, y) -> Gdk.DragAction:
        files = target.get_value()
        if files is not None:
            for gfile in files.get_files():
                p = Path(gfile.get_path() or "")
                if p.suffix.lower() in self._IMAGE_EXTS:
                    return Gdk.DragAction.COPY
            return 0
        return Gdk.DragAction.COPY

    def _on_drop(self, target, file_list, x, y) -> bool:
        files = file_list.get_files()
        if not files:
            return False
        inserted = []
        for gfile in files:
            path_str = gfile.get_path()
            if not path_str:
                continue
            src    = Path(path_str)
            suffix = src.suffix.lower()
            if suffix not in self._IMAGE_EXTS:
                continue
            if self._base_path:
                assets_dir = self._base_path.parent / "assets"
                try:
                    assets_dir.mkdir(exist_ok=True)
                    dst = assets_dir / src.name
                    if not dst.exists():
                        shutil.copy2(src, dst)
                    rel = f"assets/{src.name}"
                except OSError:
                    rel = str(src)
            else:
                rel = str(src)
            alt = src.stem.replace("-", " ").replace("_", " ")
            inserted.append(f"![{alt}]({rel})")
        if not inserted:
            return False
        self._buffer.begin_user_action()
        try:
            self._buffer.insert_at_cursor("\n".join(inserted))
        finally:
            self._buffer.end_user_action()
        self._view.grab_focus()
        return True

    def _on_view_click_for_image(self, gesture, n_press, x, y) -> None:
        """
        GestureClick released handler on the GtkSourceView.

        On a single click, check whether the cursor landed on a line that
        contains an image tag.  If so, open the image-layout edit popover.
        If the popover is already open for that line, do nothing (the user
        may be clicking inside the editor to adjust the cursor while editing
        the popover).  If the cursor is on a different line, dismiss.

        We use the "released" signal (not "pressed") so GTK has already
        moved the cursor to the clicked position before we read it.
        """
        if n_press != 1:
            return   # ignore double-click (handled by slide-zoom)

        cursor  = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        line_no = cursor.get_line()

        popover_open = (
            self._image_layout_popover is not None
            and self._image_layout_popover.get_visible()
        )

        if popover_open:
            if line_no == self._last_img_line:
                return  # user clicked within the same image line — keep popover open
            # Cursor moved to a different line — dismiss before re-opening.
            self._dismiss_img_popover()

        # Try to open the popover for whichever line the cursor is on.
        self._open_img_edit_popover_for_line(line_no)

    def set_insert_image_callback(self, cb) -> None:
        self._insert_image_cb = cb

    def set_base_path(self, path) -> None:
        self._base_path = Path(path) if path else None

    def _select_placeholder(self, placeholder: str, before: str) -> None:
        insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        end    = insert.copy()
        start  = insert.copy()
        start.backward_chars(len(placeholder))
        if self._buffer.get_text(start, end, True) == placeholder:
            self._buffer.select_range(start, end)

    # ── Slide-number badge overlay ────────────────────────────────────────────

    def _get_line_text(self, ln: int) -> str:
        """Return the stripped text of line *ln*, or '' on error."""
        ok, it = self._buffer.get_iter_at_line(ln)
        if not ok:
            return ""
        end = it.copy()
        end.forward_to_line_end()
        return self._buffer.get_text(it, end, False).strip()

    def _is_slide_sep(self, ln: int) -> bool:
        """Return True if line *ln* is a slide separator (--- with optional spaces)."""
        return bool(re.match(r'^-{3,}' + r'\s*$', self._get_line_text(ln)))

    def _is_notes_sep(self, ln: int) -> bool:
        """Return True if line *ln* is a notes separator (^^^)."""
        return bool(re.match(r'^\^{3,}' + r'\s*$', self._get_line_text(ln)))

    def _on_view_realize_badges(self, view) -> None:
        """
        Called once after the GtkSourceView is realized.

        Measures the gutter width so the badge DrawingArea can be positioned
        just past it.  Also connects the vertical scroll adjustment so badges
        redraw when the user scrolls.
        """
        try:
            # Measure the left gutter width (line numbers, marks, etc.)
            gutter = view.get_gutter(Gtk.TextWindowType.LEFT)
            if gutter is not None:
                gutter_w = gutter.get_allocated_width()
            else:
                gutter_w = 0

            # margin_start stays 0; badge X is computed in _draw_slide_badges

            # Redraw badges on vertical scroll
            scroll = view.get_parent()
            if scroll is not None:
                vadj = scroll.get_vadjustment()
                if vadj is not None:
                    def _on_scroll(*_):
                        if self._badge_draw:
                            self._badge_draw.queue_draw()
                    vadj.connect("value-changed", _on_scroll)
        except Exception:
            pass

    def _update_badge_starts(self, *_) -> bool:
        """
        Single-pass scan that builds:
          _badge_starts : [(slide_num, badge_line), ...]
          _slide_ranges : [(tint_first, tint_last), ...]

        Model
        -----
        The document body (after any YAML frontmatter) is a sequence of
        slides separated by "---" lines.

        For TINTING each slide's block covers:
          • All lines from the first line of the slide (inclusive) through
            the "---" separator that ends it (inclusive), OR the end of
            the file for the last slide.
          • If a "^^^" notes separator is found within the slide body, the
            tint stops AT the "^^^" line (inclusive) — the prose notes
            below are not tinted.

        For BADGE PLACEMENT the badge appears on the first line that is
        neither blank, "---", nor "^^^".

        Returns GLib.SOURCE_REMOVE so it works as a one-shot idle callback.
        """
        n = self._buffer.get_line_count()

        # ── Skip YAML frontmatter (--- ... ---) ───────────────────────────
        body_start = 0
        if self._is_slide_sep(0):
            for ln in range(1, min(n, 50)):
                if self._is_slide_sep(ln):
                    body_start = ln + 1
                    break

        # ── Single-pass scan ──────────────────────────────────────────────
        slide_ranges: list[tuple[int, int]] = []
        starts_list:  list[int]             = []

        slide_first   = body_start   # first line of current slide
        badge_line    = -1           # first content line found so far
        in_notes      = False        # True after ^^^ within a slide
        tint_last     = body_start   # last line included in tint so far

        for ln in range(body_start, n):
            t = self._get_line_text(ln)
            is_sep   = self._is_slide_sep(ln)
            is_notes = self._is_notes_sep(ln)

            if is_sep:
                # End of current slide — only commit if there was actual content.
                if badge_line >= 0:
                    # Include the --- line in the tint only when there are no
                    # notes (notes freeze tint_last before the ---).
                    # When notes exist, tint_last is already frozen at ^^^.
                    if not in_notes:
                        tint_last = ln   # extend tint to include the ---
                    slide_ranges.append((slide_first, tint_last))
                    starts_list.append(badge_line)
                # Start next slide on the following line
                slide_first = ln + 1
                badge_line  = -1
                in_notes    = False
                tint_last   = ln + 1
                continue

            if is_notes:
                # Notes separator — tint stops here (^^^ included)
                if not in_notes:
                    tint_last = ln
                    in_notes  = True
                continue

            if in_notes:
                continue  # skip prose notes

            tint_last = ln  # extend tint to this line
            if badge_line < 0 and t:  # first non-empty, non-sep, non-notes line
                badge_line = ln

        # ── Commit last slide (no trailing ---) ───────────────────────────
        if slide_first < n:
            slide_ranges.append((slide_first, tint_last))
            starts_list.append(
                badge_line if badge_line >= 0 else slide_first
            )

        self._badge_starts = list(enumerate(starts_list, start=1))
        self._slide_ranges = slide_ranges

        if self._badge_draw is not None:
            self._badge_draw.queue_draw()
        return GLib.SOURCE_REMOVE

    def _draw_slide_badges(self, area, cr, width, height) -> None:
        """
        Cairo draw function for the slide-number badge overlay.

        Coordinate system:
          get_iter_location()  →  buffer coords  (scroll-independent)
          buffer_to_window_coords(TEXT, 0, buf_y)  →  text-window coords
            (text window = the area excluding gutter; Y=0 is the visible top)
          We convert using TEXT not WIDGET so rect.x (which is the
          horizontal text offset, potentially large) does not pollute Y.
          The DrawingArea is positioned by margin_start to sit just after
          the gutter, so we only need the Y value.
        """
        if not _GTKSOURCE_AVAILABLE:
            return
        # Guard: do nothing if the view has not been allocated yet.
        if not self._view.get_realized():
            return
        starts = self._badge_starts
        if not starts:
            return

        if _PangoCairo is None:
            return   # PangoCairo unavailable — badges cannot be drawn

        layout = _PangoCairo.create_layout(cr)
        desc = Pango.FontDescription.from_string('Sans Bold 9')
        layout.set_font_description(desc)

        # Badge geometry: wider and taller than before so numbers are
        # readable at a glance from normal viewing distance.
        bw, bh, r = 32.0, 18.0, 4.0
        # Place the badge just to the left of the text column.
        # left_margin is the blank space between the gutter and the text.
        # We centre the pill within that margin, clamped so it never overlaps text.
        left_margin = float(self._view.get_left_margin())
        gutter_w    = 0.0
        try:
            g = self._view.get_gutter(Gtk.TextWindowType.LEFT)
            if g is not None:
                gutter_w = float(g.get_allocated_width())
        except Exception:
            pass
        # bx: left edge of pill — centred in the left_margin, after the gutter
        available = left_margin
        bx = gutter_w + max(2.0, (available - bw) / 2.0)

        for slide_num, line_no in starts:
            ok, it = self._buffer.get_iter_at_line(line_no)
            if not ok:
                continue

            # get_iter_location gives the glyph rect in buffer coordinates.
            # buf_y is the Y offset from the top of the buffer (grows as you
            # scroll down).  We always pass x=0 to avoid the horizontal text
            # indent skewing the conversion.
            buf_rect = self._view.get_iter_location(it)
            buf_y    = buf_rect.y
            line_h   = buf_rect.height if buf_rect.height > 0 else 18

            # Convert buffer Y → text-window Y.
            # TEXT coords: Y=0 is the top of the visible text area.
            # Negative means scrolled above the viewport.
            try:
                _tx, ty = self._view.buffer_to_window_coords(
                    Gtk.TextWindowType.TEXT, 0, buf_y
                )
            except Exception:
                continue

            # Skip if completely outside the visible area
            if ty + line_h < 0 or ty > height:
                continue

            # The DrawingArea is an overlay child whose Y=0 aligns with
            # the ScrolledWindow top.  buffer_to_window_coords(TEXT) returns
            # coordinates relative to the text-area top, which starts
            # top_margin pixels below the scroll window top.  Add top_margin
            # to convert from text-area coords to DrawingArea coords.
            top_margin = self._view.get_top_margin()
            by = float(ty) + top_margin + (line_h - bh) / 2

            cr.new_path()
            cr.arc(bx + r,      by + r,      r, math.pi,          3 * math.pi / 2)
            cr.arc(bx + bw - r, by + r,      r, 3 * math.pi / 2,  0)
            cr.arc(bx + bw - r, by + bh - r, r, 0,                math.pi / 2)
            cr.arc(bx + r,      by + bh - r, r, math.pi / 2,      math.pi)
            cr.close_path()
            cr.set_source_rgba(0.486, 0.388, 0.780, 0.90)  # purple matching tint
            cr.fill()

            cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)
            layout.set_text(str(slide_num), -1)
            pw, ph = layout.get_pixel_size()
            cr.move_to(bx + (bw - pw) / 2, by + (bh - ph) / 2)
            _PangoCairo.show_layout(cr, layout)

    # ── Style ─────────────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        """Apply syntax highlighting scheme and separator tag."""
        if not _GTKSOURCE_AVAILABLE:
            return

        mgr = GtkSource.StyleSchemeManager.get_default()
        scheme = mgr.get_scheme("presence-markdown")
        if scheme:
            self._buffer.set_style_scheme(scheme)
        else:
            for name in ("Adwaita", "adwaita", "tango", "classic"):
                s = mgr.get_scheme(name)
                if s:
                    self._buffer.set_style_scheme(s)
                    break

        self._apply_separator_tag()
        self._apply_image_tag()

    def _apply_separator_tag(self) -> None:
        """
        Create a TextTag that highlights slide-separator lines (---).

        Uses @accent_color-equivalent orange that works in both light and
        dark mode.  The tag is re-applied on every buffer change via
        _on_buffer_changed so separators are always highlighted.
        """
        tag_table = self._buffer.get_tag_table()

        # Create the tag once; reuse on subsequent calls
        if tag_table.lookup("presence-separator") is None:
            # Colour the text only — no background fill.  A solid dark
            # background on "---" looks heavy and distracts from slide
            # content.  The orange foreground is sufficient to make
            # separators visually distinct (matches iA Presenter style).
            tag = self._buffer.create_tag(
                "presence-separator",
                foreground="#e17000",
                weight=700,       # Pango.Weight.BOLD
            )

        # Initial application — buffer may already have content
        self._highlight_separators()

        # Re-highlight on every change — connect only once using a flag on
        # the buffer to avoid duplicate connections if _apply_style is called again.
        if not getattr(self._buffer, '_separator_connected', False):
            self._buffer.connect("changed", lambda *_: self._highlight_separators())
            self._buffer._separator_connected = True

    def _highlight_separators(self) -> None:
        """Scan the buffer and apply/clear the separator tag on --- lines."""
        tag_table = self._buffer.get_tag_table()
        tag = tag_table.lookup("presence-separator")
        if tag is None:
            return

        # Remove tag from entire buffer first
        start = self._buffer.get_start_iter()
        end   = self._buffer.get_end_iter()
        self._buffer.remove_tag(tag, start, end)

        # Re-apply to every line whose stripped content is exactly "---"
        n = self._buffer.get_line_count()
        for line_no in range(n):
            ok, line_start = self._buffer.get_iter_at_line(line_no)
            if not ok:
                continue
            line_end = line_start.copy()
            line_end.forward_to_line_end()
            text = self._buffer.get_text(line_start, line_end, False)
            if text.strip() == "---":
                self._buffer.apply_tag(tag, line_start, line_end)

    # ── Image-tag syntax highlighting ─────────────────────────────────────────

    # Matches the full Markdown image tag so we can colour sub-spans:
    #   group 1 — "!["          punctuation
    #   group 2 — alt text      (may contain layout tokens separated by |)
    #   group 3 — "]("          punctuation
    #   group 4 — src path      (file path or URL)
    #   group 5 — ")"           punctuation
    _IMAGE_HIGHLIGHT_RE = re.compile(
        r'(!\[)'          # group 1: opening punctuation
        r'([^\]]*)'       # group 2: alt text (layout tokens live here)
        r'(\]\()'         # group 3: middle punctuation
        r'([^)\s"\']+)'   # group 4: src path
        r'(\))',          # group 5: closing punctuation
    )

    # Layout tokens recognised by the slide renderer — used to split the alt
    # text into "human description" and "Presence layout tokens".
    # Variable-length tokens (opacityN, fade-dir) are checked via regex below.
    _LAYOUT_TOKENS = (
        _IMAGE_POSITIONS | _IMAGE_FIT | _IMAGE_FOCAL
        | {"gradient", "nogradient", "flip-h", "flip-v"}
    )

    def _apply_image_tag(self) -> None:
        """
        Create TextTags for Presence image-layout syntax and wire them up.

        Three visual layers are applied to every ![alt](src) tag:
          • presence-img-punct  — muted colour for ![ ]( ) delimiters
          • presence-img-token  — accent colour for layout tokens (right|50|…)
          • presence-img-src    — dim italic for the file path / URL

        These are GtkTextBuffer tags, not GtkSource language rules, so they
        work even when GtkSourceView is not available and survive theme changes
        because they use fixed colours chosen to contrast on both light/dark
        editor backgrounds.

        The tags are created once and re-applied on every buffer change via
        the same 'changed' signal used by the separator highlighter.
        """
        if not _GTKSOURCE_AVAILABLE:
            return   # plain Gtk.TextBuffer has no create_tag on GtkSource.Buffer

        tag_table = self._buffer.get_tag_table()

        # Punctuation: ![ ]( ) — muted so they don't compete with content
        if tag_table.lookup("presence-img-punct") is None:
            self._buffer.create_tag(
                "presence-img-punct",
                foreground="#888888",
            )

        # Layout tokens (right, left, top, bottom, 50, gradient, …):
        # accent colour + bold so they read like "settings at a glance"
        if tag_table.lookup("presence-img-token") is None:
            self._buffer.create_tag(
                "presence-img-token",
                foreground="#e17000",   # same orange as separators
                weight=700,             # Pango.Weight.BOLD
            )

        # Description part of alt text (the human-readable words):
        # slightly muted so the tokens stand out next to it
        if tag_table.lookup("presence-img-desc") is None:
            self._buffer.create_tag(
                "presence-img-desc",
                foreground="#555577",
            )

        # Source path / URL: dim italic — important but secondary
        if tag_table.lookup("presence-img-src") is None:
            self._buffer.create_tag(
                "presence-img-src",
                foreground="#777777",
                style=2,                # Pango.Style.ITALIC
            )

        # Initial scan
        self._highlight_image_tags()

        # Re-scan on every buffer change — connect only once.
        if not getattr(self._buffer, '_image_tag_connected', False):
            self._buffer.connect("changed", lambda *_: self._highlight_image_tags())
            self._buffer._image_tag_connected = True

    def _highlight_image_tags(self) -> None:
        """
        Scan the full buffer text and apply image-layout highlighting tags.

        Runs as a single regex pass over the full buffer text, then converts
        character offsets to TextIters for each match group.  This is O(n)
        in document size but is only triggered by the debounced 'changed'
        signal so it never runs more than once per keystroke batch.
        """
        tag_table = self._buffer.get_tag_table()
        punct_tag = tag_table.lookup("presence-img-punct")
        token_tag = tag_table.lookup("presence-img-token")
        desc_tag  = tag_table.lookup("presence-img-desc")
        src_tag   = tag_table.lookup("presence-img-src")
        if not all((punct_tag, token_tag, desc_tag, src_tag)):
            return

        buf_start = self._buffer.get_start_iter()
        buf_end   = self._buffer.get_end_iter()

        # Clear all four tags from the entire buffer before reapplying.
        # This is simpler and safer than trying to diff old vs new ranges.
        for tag in (punct_tag, token_tag, desc_tag, src_tag):
            self._buffer.remove_tag(tag, buf_start, buf_end)

        full_text = self._buffer.get_text(buf_start, buf_end, False)

        for m in self._IMAGE_HIGHLIGHT_RE.finditer(full_text):
            # Helper: convert a character offset in full_text to a TextIter.
            def _iter(offset: int):
                return self._buffer.get_iter_at_offset(offset)

            open_start  = m.start(1)   # "!["
            open_end    = m.end(1)
            alt_start   = m.start(2)   # alt text content
            alt_end     = m.end(2)
            mid_start   = m.start(3)   # "]("
            mid_end     = m.end(3)
            src_start   = m.start(4)   # file path
            src_end     = m.end(4)
            close_start = m.start(5)   # ")"
            close_end   = m.end(5)

            # Colour ![ and ]( and ) as punctuation
            self._buffer.apply_tag(punct_tag, _iter(open_start),  _iter(open_end))
            self._buffer.apply_tag(punct_tag, _iter(mid_start),   _iter(mid_end))
            self._buffer.apply_tag(punct_tag, _iter(close_start), _iter(close_end))

            # Colour the src path
            self._buffer.apply_tag(src_tag, _iter(src_start), _iter(src_end))

            # Split alt text on | and colour each part individually:
            # layout tokens get accent colour, description words get dim colour.
            alt_text = full_text[alt_start:alt_end]
            pos = alt_start
            for part in alt_text.split("|"):
                part_end = pos + len(part)
                part_lower = part.strip().lower()
                if (part_lower in self._LAYOUT_TOKENS
                        or _OPACITY_TOKEN_RE.match(part_lower)
                        or _FADE_TOKEN_RE.match(part_lower)
                        or _SIZE_TOKEN_RE.match(part_lower)
                        or _GRAYSCALE_TOKEN_RE.match(part_lower)
                        or _BLUR_TOKEN_RE.match(part_lower)
                        or _TINT_TOKEN_RE.match(part_lower)
                        or _ZOOM_TOKEN_RE.match(part_lower)):
                    self._buffer.apply_tag(
                        token_tag, _iter(pos), _iter(part_end)
                    )
                elif part.strip():
                    self._buffer.apply_tag(
                        desc_tag, _iter(pos), _iter(part_end)
                    )
                # Advance past the part and the | separator
                pos = part_end + 1   # +1 for the "|" character

    # ── Debounce ──────────────────────────────────────────────────────────────

    def _on_buffer_changed(self, buffer) -> None:
        if self._debounce_source is not None:
            GLib.source_remove(self._debounce_source)
        self._debounce_source = GLib.timeout_add(
            self.DEBOUNCE_MS, self._emit_changed)

    def _emit_changed(self) -> bool:
        self._debounce_source = None
        self.emit("changed", self.get_text())
        return GLib.SOURCE_REMOVE

    def _on_destroy(self, *_) -> None:
        if self._debounce_source is not None:
            GLib.source_remove(self._debounce_source)
            self._debounce_source = None
