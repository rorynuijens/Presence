"""
theme_editor.py — One editor for creating and editing themes.

Opens from the theme chooser (inspector → Theme → Change… → Manage) via
"New theme" or a theme's "Edit".  Produces a theme package directory
(theme.json + optional theme.css + optional fonts/) and either installs it
directly or exports it as a .zip.

Design
------
One dialog, one class.  There used to be two: a four-step wizard that was the
only door — including the "Edit" door, for a theme the writer had already
made — and a full-field ``ThemeEditorAdvanced`` reachable solely through a
quiet "Skip to advanced editor…" button in the wizard's footer.  Between them
they implemented ``_on_install``, ``_on_export_zip``, ``_validate_slug``,
``_render_preview``, ``_collect_theme_json``, ``_write_to_dir`` and
``_show_error`` twice, and had already drifted: the advanced editor's own
docstring claimed an entry point that nothing constructed.

A wizard is for a one-off sequential task.  Editing a theme is neither: it is
one object with a dozen properties, revisited a field at a time.  So the
steps are groups on a single ``Adw.PreferencesPage``, and what the wizard
kept behind a second dialog is behind ``Adw.ExpanderRow`` disclosures instead
— the progressive disclosure the HIG actually describes.

The wizard also reset the theme it was asked to edit.  Its presets, fonts and
title styles were ``Gtk.ToggleButton`` groups whose initial ``set_active``
fired during construction, so opening "Edit" on a user theme replaced its
palette with Classic, its font with IBM Plex and its cover with the dark one
before the writer touched anything.  Presets are plain buttons here — an
action that fills the fields in, never a mode the dialog is in — so nothing
is applied that was not clicked.

Layout
------
    ┌──────────────────────────────┬─────────────────────┐
    │ Identity                     │ Preview  [slide ▾]  │
    │ Palette      (+ more colours)│                     │
    │ Title slide                  │   ┌───────────┐     │
    │ Typography                   │   │  webview  │     │
    │ Callout boxes                │   └───────────┘     │
    │ Bundled fonts                ├─────────────────────┤
    │ Custom CSS   (disclosure)    │ Contrast check      │
    └──────────────────────────────┴─────────────────────┘

GNOME HIG compliance notes
---------------------------
- Adw.Dialog, presented against the main window.  It was an Adw.Window, for
  "its own window ID and taskbar entry", but it was also modal and transient
  for its parent — so it never behaved like the independent document window
  that claim describes, and modal-and-transient is exactly what GNOME 45
  replaced with Adw.Dialog.
- Adw.ToolbarView with Adw.HeaderBar — Cancel leading, [Export zip] then
  [Install] trailing (two separate pack_end calls, primary rightmost).  The
  header's own close button is hidden where Cancel is shown; Escape closes
  either way.
- One Adw.PreferencesPage of Adw.PreferencesGroups, scrolled.  No step list,
  no Back/Next, no completion checkmarks: nothing here has an order.
- Colour pickers: Gtk.ColorDialogButton (GTK 4.10+) with Gtk.ColorButton
  fallback.  Both picker and hex Entry are always visible and in sync.
- Preset cards are plain Gtk.Buttons that fill the fields in, so the form
  always shows what the theme actually is.
- All interactive widgets have accessible labels via update_property().
- Contrast ratios are reported once, on the preview panel, for all three
  pairings that matter.
- Alerts name what failed ("Could not install the theme") rather than
  heading themselves "Error".
- Built-in themes open with the form insensitive and an Adw.Banner offering
  "Fork as new theme".
- Destroy handler cancels pending GLib timeouts and cleans up temp files.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, Pango, PangoCairo, Gio

log = logging.getLogger(__name__)

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
    _WEBKIT_VERSION = 6
except (ValueError, ImportError):
    try:
        gi.require_version("WebKit2", "4.1")
        from gi.repository import WebKit2 as WebKit
        _WEBKIT_VERSION = 4
    except (ValueError, ImportError):
        WebKit = None
        _WEBKIT_VERSION = None

try:
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource
    _GTKSOURCE = True
except (ValueError, ImportError):
    GtkSource = None
    _GTKSOURCE = False

_GTK_VERSION      = (Gtk.get_major_version(), Gtk.get_minor_version())
_HAS_COLOR_DIALOG = _GTK_VERSION >= (4, 10)

from .slides.themes import Theme, BUILTIN_THEMES
from .slides.theme_loader import load_all_themes, install_theme, user_themes_dir
from .slides.css import build_css
from .slides.themes import ASPECT_RATIOS

# ── Constants ─────────────────────────────────────────────────────────────────

_SLUG_RE             = re.compile(r'^[a-z0-9_-]+$')
_PREVIEW_DEBOUNCE_MS = 300

# WCAG's thresholds for text.  The accent is held to CONTRAST_AA because
# css.py gives it `strong` and `a` at body size, not only headings — which
# is why relaxing it to the large-text bar would have been the wrong way to
# make a failing palette pass.  theme_manager_ui imports CONTRAST_AA rather
# than restating it.
CONTRAST_AA  = 4.5
CONTRAST_AAA = 7.0

_PREVIEW_MD = """\
# Slide heading

Body text with **bold** and *italic* words.

- First bullet point
- Second bullet point
- Third bullet point

`inline code` and a blockquote:

> This is a blockquote spanning multiple lines.

> [!tip]
> This is a tip callout box.
"""

_CALLOUT_KINDS = ("tip", "info", "warning", "danger")

# Palette presets — each entry is (label, bg, fg, accent, title_bg, title_fg)
#
# Every accent clears 4.5:1 against its background, which is the bar the
# contrast strip holds them to and the one that matters: the accent is not
# only heading colour, it is also `strong` and `a` at body size (css.py).
# Classic and Parchment did not — 3.2:1 and 3.8:1 — so a new theme started
# on a palette the editor then marked as failing.  Both were darkened along
# their own hue by the least that clears the bar.
_PRESETS: list[tuple[str, str, str, str, str, str]] = [
    ("Classic",   "#ffffff", "#1a1a2e", "#ba5d00", "#1a1a2e", "#ffffff"),
    ("Midnight",  "#1a1a2e", "#e8e8f0", "#5b8dee", "#0d0d1a", "#ffffff"),
    ("Sage",      "#f5f0e8", "#1c2b1e", "#2d6a4f", "#1c2b1e", "#f5f0e8"),
    ("Carbon",    "#111111", "#f0f0f0", "#ff6b6b", "#000000", "#f0f0f0"),
    ("Parchment", "#fefae0", "#3a2a1a", "#a96121", "#3a2a1a", "#fefae0"),
    ("Arctic",    "#e8f4f8", "#0d2b3e", "#185fa5", "#0d2b3e", "#e8f4f8"),
    ("Violet",    "#f5f0ff", "#1e0a3c", "#7c3aed", "#1e0a3c", "#f5f0ff"),
    ("Dusk",      "#2d1b3d", "#f0e8ff", "#c084fc", "#1a0d2e", "#f0e8ff"),
]

# Font options — (label, body_font CSS stack, sample text)
_FONTS: list[tuple[str, str, str]] = [
    ("System sans",  "'Liberation Sans', 'Cantarell', Arial, sans-serif",
     "Clean and readable"),
    ("IBM Plex",     "'IBM Plex Sans', 'Liberation Sans', Arial, sans-serif",
     "Modern and professional"),
    ("Serif",        "'Liberation Serif', Georgia, serif",
     "Traditional and elegant"),
    ("Rounded",      "'Cantarell', 'Liberation Sans', Arial, sans-serif",
     "Friendly and approachable"),
    ("Mono",         "'Fira Code', 'Liberation Mono', monospace",
     "Technical and precise"),
]


class ThemeEditor(Adw.Dialog):
    """
    Create or edit one theme.

    Parameters
    ----------
    parent_window:
        The window the dialog is presented against (MainWindow).
    existing_theme:
        Pre-populate from this theme when editing.  None for a new theme.
    on_installed:
        Called on the main thread after a successful install.
    """

    def __init__(
        self,
        parent_window,
        existing_theme: Theme | None = None,
        on_installed:   Callable[[Theme], None] | None = None,
    ) -> None:
        super().__init__()

        self._parent_window = parent_window
        self._on_installed  = on_installed
        self._editing       = existing_theme
        self._is_builtin    = (
            existing_theme is not None
            and existing_theme.slug in BUILTIN_THEMES
        )

        self._theme           = self._initial_theme(existing_theme)
        self._custom_css: str = self._load_custom_css(existing_theme)
        # A new theme's identifier follows its name until the writer types
        # one; an existing theme's is left alone, because it names the
        # directory the theme is installed in.  This used to be a comparison
        # against the slug the dialog opened with, which stopped following
        # after the very first keystroke: typing "My Theme" into a new theme
        # left the identifier at "m".
        self._slug_touched  = existing_theme is not None
        self._syncing_slug  = False
        self._font_files: list[Path] = []

        self._preview_debounce: int | None = None
        self._preview_tmp_css:  str | None = None
        self._slug_error_icon              = None
        self._fonts_group: Adw.PreferencesGroup | None = None
        self._font_rows: dict[Path, Adw.ActionRow] = {}

        self.set_content_width(1040)
        self.set_content_height(700)
        self.set_title(
            f"Edit theme — {existing_theme.name}" if existing_theme else "New theme"
        )

        self._build_ui()
        # "closed" rather than "destroy": a dialog that is closed is
        # unparented, not disposed, so the destroy handler may never run.
        self.connect("closed", self._on_destroy)
        GLib.idle_add(self._schedule_preview)

    # ── Static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _initial_theme(existing: Theme | None) -> Theme:
        """
        The theme the form opens on.

        A new theme starts on the first preset so the preview has something
        to draw; an existing one is copied verbatim.  Nothing else applies a
        preset — see the class docstring on why the wizard's toggle groups
        overwrote the theme they were asked to edit.
        """
        if existing is None:
            t = Theme(name="New Theme", slug="new-theme", author="")
            _, bg, fg, accent, title_bg, title_fg = _PRESETS[0]
            return dataclasses.replace(
                t, bg=bg, fg=fg, accent=accent,
                title_bg=title_bg, title_fg=title_fg,
            )
        return dataclasses.replace(existing)

    @staticmethod
    def _load_custom_css(existing: Theme | None) -> str:
        if not existing or not existing.custom_css_path:
            return ""
        p = Path(existing.custom_css_path)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_child(toolbar_view)
        toolbar_view.add_top_bar(self._build_header())

        if self._is_builtin:
            banner = Adw.Banner(
                title="Built-in themes are read-only",
                button_label="Fork as new theme",
            )
            banner.set_revealed(True)
            banner.connect("button-clicked", self._on_fork)
            toolbar_view.add_top_bar(banner)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        form_scroll = Gtk.ScrolledWindow()
        form_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        form_scroll.set_size_request(480, -1)
        form_scroll.set_hexpand(True)
        form_scroll.set_vexpand(True)
        self._form = self._build_form()
        if self._is_builtin:
            self._form.set_sensitive(False)
        form_scroll.set_child(self._form)
        outer.append(form_scroll)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        preview = self._build_preview_panel()
        preview.set_size_request(420, -1)
        outer.append(preview)

        toolbar_view.set_content(outer)

    def _build_header(self) -> Adw.HeaderBar:
        bar = Adw.HeaderBar()

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        bar.pack_start(cancel_btn)
        # A dialog's header draws a close button of its own, which beside an
        # explicit Cancel is the same door twice.  Escape still closes.
        bar.set_show_end_title_buttons(False)

        if not self._is_builtin:
            # Primary action rightmost; pack_end inserts right-to-left.
            install_label = "Save" if self._editing else "Install"
            self._install_btn = Gtk.Button(label=install_label)
            self._install_btn.add_css_class("suggested-action")
            self._install_btn.connect("clicked", self._on_install)
            bar.pack_end(self._install_btn)

            export_btn = Gtk.Button(label="Export as zip…")
            export_btn.connect("clicked", self._on_export_zip)
            bar.pack_end(export_btn)

        return bar

    # ── The form ──────────────────────────────────────────────────────────────

    def _build_form(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.add(self._build_identity_group())
        page.add(self._build_palette_group())
        page.add(self._build_title_group())
        page.add(self._build_typography_group())
        page.add(self._build_callouts_group())
        page.add(self._build_fonts_group())
        page.add(self._build_css_group())
        return page

    def _build_identity_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Identity",
            description="Shown in the theme list and when sharing the theme",
        )

        self._name_row   = Adw.EntryRow(title="Name",        text=self._theme.name)
        self._slug_row   = Adw.EntryRow(title="Identifier",  text=self._theme.slug)
        self._author_row = Adw.EntryRow(title="Author",      text=self._theme.author)
        self._desc_row   = Adw.EntryRow(title="Description", text=self._theme.description)

        self._name_row.connect("changed",   self._on_name_changed)
        self._slug_row.connect("changed",   self._on_slug_changed)
        self._author_row.connect("changed", self._on_meta_changed)
        self._desc_row.connect("changed",   self._on_meta_changed)

        for row in (self._name_row, self._slug_row,
                    self._author_row, self._desc_row):
            group.add(row)
        return group

    def _build_palette_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Palette",
            description="Pick a preset to fill everything in, then adjust",
        )
        group.add(self._preset_row())

        self._bg_row = _ColourRow(
            "Background", self._theme.bg,
            callback=lambda v: self._on_colour_changed("bg", v),
            description="The main slide background colour",
        )
        self._fg_row = _ColourRow(
            "Text", self._theme.fg,
            callback=lambda v: self._on_colour_changed("fg", v),
            description="Body text, bullets, and captions",
        )
        self._accent_row = _ColourRow(
            "Accent", self._theme.accent,
            callback=lambda v: self._on_colour_changed("accent", v),
            description="Headings, links, and highlighted elements",
        )
        for row in (self._bg_row, self._fg_row, self._accent_row):
            group.add(row)

        more = Adw.ExpanderRow(
            title="More colours",
            subtitle="Second accent, heading colour, code background",
        )
        self._extra_rows: dict[str, _ColourRow] = {}
        for field, label, hint, nullable in (
            ("accent2",       "Accent 2",
             "Optional second accent — leave blank to use the accent", True),
            ("heading_color", "Heading",
             "Overrides the accent for headings — leave blank to use it", True),
            ("code_bg",       "Code background",
             "Behind inline code and fenced code blocks", False),
        ):
            row = _ColourRow(
                label, getattr(self._theme, field, "") or "",
                callback=lambda v, f=field: self._on_colour_changed(f, v),
                description=hint, nullable=nullable,
            )
            self._extra_rows[field] = row
            more.add_row(row)
        group.add(more)
        return group

    def _preset_row(self) -> Adw.ActionRow:
        """
        The palette presets, as buttons rather than a selection.

        A preset is something you apply, not a mode the theme is in: change
        one colour afterwards and no preset describes the theme any more.
        Toggles claimed otherwise, and their initial ``set_active`` fired
        during construction — which is what overwrote an edited theme.
        """
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_column_spacing(8)
        flow.set_row_spacing(8)
        flow.set_max_children_per_line(4)
        flow.set_min_children_per_line(2)
        flow.set_margin_top(6)
        flow.set_margin_bottom(6)

        self._preset_btns: list[Gtk.Button] = []
        for i, (label, bg, fg, accent, _tbg, _tfg) in enumerate(_PRESETS):
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.set_tooltip_text(f"Apply the {label} palette")
            btn.update_property(
                [Gtk.AccessibleProperty.LABEL], [f"Apply the {label} palette"]
            )

            btn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            swatch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            swatch.set_size_request(-1, 36)
            swatch.set_overflow(Gtk.Overflow.HIDDEN)
            for colour in (bg, accent):
                half = Gtk.Box()
                half.set_hexpand(True)
                _apply_swatch_colour(half, colour)
                swatch.append(half)

            name_lbl = Gtk.Label(label=label)
            name_lbl.add_css_class("caption")
            name_lbl.set_margin_top(4)
            name_lbl.set_margin_bottom(4)

            btn_box.append(swatch)
            btn_box.append(name_lbl)
            btn.set_child(btn_box)
            btn.connect("clicked", self._on_preset_clicked, i)
            self._preset_btns.append(btn)

            child = Gtk.FlowBoxChild()
            child.set_child(btn)
            flow.append(child)

        row = Adw.ActionRow()
        row.set_activatable(False)
        row.set_child(flow)
        return row

    def _build_title_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Title slide",
            description="The cover — usually the deck's name and yours",
        )
        group.add(self._title_shortcut_row())

        self._title_rows: dict[str, _ColourRow] = {}
        for field, label, nullable in (
            ("title_bg",     "Background", False),
            ("title_fg",     "Text",       False),
            ("title_accent", "Accent",     True),
        ):
            row = _ColourRow(
                label, getattr(self._theme, field, "") or "",
                callback=lambda v, f=field: self._on_colour_changed(f, v),
                nullable=nullable,
            )
            self._title_rows[field] = row
            group.add(row)
        return group

    def _title_shortcut_row(self) -> Adw.ActionRow:
        """
        Two ways to derive the cover from the palette you already chose.

        This replaces three "cover style" cards, one of which existed only to
        say that the real controls were in the other dialog.  Both buttons
        are pure functions of the current palette, so what they do is
        visible in the colour rows directly beneath them.
        """
        row = Adw.ActionRow(
            title="Derive from the palette",
            subtitle="Fill the three colours below from the slide palette",
        )
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_valign(Gtk.Align.CENTER)

        match_btn = Gtk.Button(label="Match slides")
        match_btn.set_tooltip_text(
            "Use the slide background and text colours for the cover"
        )
        match_btn.connect("clicked", lambda *_: self._apply_title_style(False))

        invert_btn = Gtk.Button(label="Invert")
        invert_btn.set_tooltip_text(
            "Swap the slide background and text colours for the cover"
        )
        invert_btn.connect("clicked", lambda *_: self._apply_title_style(True))

        box.append(match_btn)
        box.append(invert_btn)
        row.add_suffix(box)
        return row

    def _build_typography_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Typography",
            description="One font carries the whole deck unless you say otherwise",
        )
        group.add(self._font_preset_row())

        self._font_rows_by_field: dict[str, Adw.EntryRow] = {}
        for field, title, placeholder in (
            ("body_font",    "Body font",    ""),
            ("heading_font", "Heading font", "Same as body font"),
            ("mono_font",    "Mono font",    ""),
        ):
            row = self._font_entry_row(
                title, getattr(self._theme, field), field, placeholder
            )
            self._font_rows_by_field[field] = row
            group.add(row)

        size_row = Adw.ActionRow(
            title="Base font size",
            subtitle="Pixels at 1280 × 720 — heading sizes scale from this",
        )
        self._size_spin = Gtk.SpinButton.new_with_range(20, 60, 1)
        self._size_spin.set_value(self._theme.base_size)
        self._size_spin.set_valign(Gtk.Align.CENTER)
        self._size_spin.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Base font size in pixels"]
        )
        self._size_spin.connect(
            "value-changed",
            lambda s: self._on_simple_change("base_size", int(s.get_value())),
        )
        size_row.add_suffix(self._size_spin)
        size_row.set_activatable_widget(self._size_spin)
        group.add(size_row)

        try:
            from pygments.styles import get_all_styles
            styles = sorted(get_all_styles())
        except Exception:
            styles = ["friendly", "monokai", "vs", "tango"]
        pgy_row = Adw.ActionRow(title="Code highlighting style")
        pgy_drop = Gtk.DropDown(model=Gtk.StringList.new(styles))
        if self._theme.pygments_style in styles:
            pgy_drop.set_selected(styles.index(self._theme.pygments_style))
        pgy_drop.set_valign(Gtk.Align.CENTER)
        pgy_drop.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Code highlighting style"]
        )
        pgy_drop.connect(
            "notify::selected",
            lambda d, _: self._on_simple_change(
                "pygments_style", styles[d.get_selected()]
            ),
        )
        pgy_row.add_suffix(pgy_drop)
        group.add(pgy_row)
        return group

    def _font_preset_row(self) -> Adw.ActionRow:
        """Five sample cards; clicking one fills in the body font."""
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_column_spacing(8)
        flow.set_row_spacing(8)
        flow.set_max_children_per_line(3)
        flow.set_min_children_per_line(2)
        flow.set_margin_top(6)
        flow.set_margin_bottom(6)

        self._font_btns: list[Gtk.Button] = []
        for i, (label, stack, sample) in enumerate(_FONTS):
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.set_tooltip_text(f"Use {label} for the body font")
            btn.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [f"Use {label} for the body font — {sample}"],
            )

            btn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            btn_box.set_margin_top(10)
            btn_box.set_margin_bottom(10)
            btn_box.set_margin_start(12)
            btn_box.set_margin_end(12)

            name_lbl = Gtk.Label(label=label)
            name_lbl.set_xalign(0.0)
            name_lbl.add_css_class("caption")
            name_lbl.add_css_class("dim-label")

            sample_lbl = Gtk.Label(label=sample)
            sample_lbl.set_xalign(0.0)
            sample_lbl.set_wrap(True)

            # Apply the font via a Pango attribute list — set_font() is GTK 3.
            first_family = stack.split(",")[0].strip().strip("'\"")
            try:
                font_desc = Pango.FontDescription.from_string(f"{first_family} 14")
                attr_list = Pango.AttrList()
                attr_list.insert(Pango.AttrFontDesc.new(font_desc))
                sample_lbl.set_attributes(attr_list)
            except Exception:
                pass

            btn_box.append(name_lbl)
            btn_box.append(sample_lbl)
            btn.set_child(btn_box)
            btn.connect("clicked", self._on_font_preset_clicked, i)
            self._font_btns.append(btn)

            child = Gtk.FlowBoxChild()
            child.set_child(btn)
            flow.append(child)

        row = Adw.ActionRow()
        row.set_activatable(False)
        row.set_child(flow)
        return row

    def _build_callouts_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Callout boxes",
            description="Each callout's three colours and its badge character",
        )
        self._callout_rows: dict[str, _CalloutRow] = {}
        for kind in _CALLOUT_KINDS:
            row = _CalloutRow(
                kind.capitalize(),
                getattr(self._theme, f"callout_{kind}"),
                getattr(self._theme, f"callout_icon_{kind}"),
                on_colours=lambda t, k=kind: self._on_callout_changed(k, t),
                on_icon=lambda v, k=kind: self._on_icon_changed(k, v),
            )
            self._callout_rows[kind] = row
            group.add(row)
        return group

    def _build_fonts_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Bundled fonts",
            description="Font files copied into the theme package",
        )
        self._fonts_group = group

        add_row = Adw.ActionRow(
            title="Add font file",
            subtitle="woff2, ttf, otf  ·  name as FamilyName-WeightStyle.woff2",
        )
        add_btn = Gtk.Button(label="Browse…")
        add_btn.add_css_class("flat")
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.connect("clicked", self._on_add_font)
        add_row.add_suffix(add_btn)
        add_row.set_activatable_widget(add_btn)
        group.add(add_row)

        if self._editing and self._editing.custom_css_path:
            fonts_dir = Path(self._editing.custom_css_path).parent / "fonts"
            if fonts_dir.is_dir():
                for f in sorted(fonts_dir.iterdir()):
                    if f.suffix.lower() in (".woff2", ".woff", ".ttf", ".otf"):
                        self._add_font_row(f)
        return group

    def _build_css_group(self) -> Adw.PreferencesGroup:
        """
        Custom CSS, behind a disclosure.

        This is the one part of a theme most writers never touch, and it was
        the reason a whole second dialog existed.  An ExpanderRow is the
        HIG's answer to "most people don't need this".
        """
        group = Adw.PreferencesGroup(title="Custom CSS")
        expander = Adw.ExpanderRow(
            title="Custom CSS",
            subtitle="Appended after the generated stylesheet",
        )
        expander.set_expanded(bool(self._custom_css.strip()))

        frame = Gtk.Frame()
        frame.set_margin_top(4)
        frame.set_margin_bottom(4)
        frame.set_margin_start(8)
        frame.set_margin_end(8)
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        editor = self._build_css_editor()
        editor.set_size_request(-1, 220)
        frame.set_child(editor)

        holder = Adw.ActionRow()
        holder.set_activatable(False)
        holder.set_child(frame)
        expander.add_row(holder)
        group.add(expander)
        return group

    def _build_css_editor(self) -> Gtk.Widget:
        if _GTKSOURCE:
            lang = GtkSource.LanguageManager.get_default().get_language("css")
            buf  = GtkSource.Buffer()
            if lang:
                buf.set_language(lang)
            buf.set_highlight_syntax(True)
            buf.set_text(self._custom_css)
            buf.connect("changed", self._on_css_changed)
            view = GtkSource.View.new_with_buffer(buf)
            view.set_show_line_numbers(True)
            view.set_tab_width(2)
            view.set_insert_spaces_instead_of_tabs(True)
        else:
            buf = Gtk.TextBuffer()
            buf.set_text(self._custom_css)
            buf.connect("changed", self._on_css_changed)
            view = Gtk.TextView.new_with_buffer(buf)

        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_left_margin(8)
        view.set_right_margin(8)
        view.set_top_margin(8)
        view.set_bottom_margin(8)
        view.update_property([Gtk.AccessibleProperty.LABEL], ["Custom CSS"])

        scroll = Gtk.ScrolledWindow()
        scroll.set_child(view)
        scroll.set_vexpand(False)
        self._css_buffer = buf
        return scroll

    def _font_entry_row(self, title: str, current: str,
                        field: str, placeholder: str) -> Adw.EntryRow:
        row = Adw.EntryRow(title=title)
        row.set_text(current)
        if placeholder:
            row.connect(
                "realize",
                lambda r, ph=placeholder: self._set_placeholder(r, ph),
            )
        row.connect("changed",
                    lambda r, f=field: self._on_simple_change(f, r.get_text()))
        pick_btn = Gtk.Button()
        pick_btn.set_child(Gtk.Image.new_from_icon_name("font-select-symbolic"))
        pick_btn.set_tooltip_text("Browse system fonts")
        pick_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Browse system fonts"]
        )
        pick_btn.add_css_class("flat")
        pick_btn.set_valign(Gtk.Align.CENTER)
        pick_btn.connect("clicked",
                         lambda btn, r=row: self._open_font_popover(btn, r))
        row.add_suffix(pick_btn)
        return row

    @staticmethod
    def _set_placeholder(row: Adw.EntryRow, placeholder: str) -> None:
        def _walk(widget):
            if isinstance(widget, Gtk.Text):
                widget.set_placeholder_text(placeholder)
                return True
            child = widget.get_first_child()
            while child:
                if _walk(child):
                    return True
                child = child.get_next_sibling()
            return False
        _walk(row)

    def _open_font_popover(self, anchor: Gtk.Widget,
                           row: Adw.EntryRow) -> None:
        families = sorted(
            {f.get_name()
             for f in PangoCairo.FontMap.get_default().list_families()},
            key=str.lower,
        )
        popover = Gtk.Popover()
        popover.set_parent(anchor)
        popover.set_has_arrow(True)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_margin_top(8)
        vbox.set_margin_bottom(8)
        vbox.set_margin_start(8)
        vbox.set_margin_end(8)
        vbox.set_size_request(220, -1)

        search = Gtk.SearchEntry()
        search.set_placeholder_text("Filter fonts…")
        vbox.append(search)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(-1, 260)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        list_box.add_css_class("boxed-list")

        for family in families:
            lbl = Gtk.Label(label=family, xalign=0.0)
            lbl.set_margin_start(8)
            lbl.set_margin_end(8)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            r = Gtk.ListBoxRow()
            r.set_child(lbl)
            r._family = family
            list_box.append(r)

        list_box.set_filter_func(
            lambda r, _: not search.get_text() or
            search.get_text().lower() in r._family.lower()
        )
        search.connect("search-changed", lambda *_: list_box.invalidate_filter())
        list_box.connect(
            "row-activated",
            lambda lb, r, p=popover, er=row: (er.set_text(r._family), p.popdown()),
        )

        scroll.set_child(list_box)
        vbox.append(scroll)
        popover.set_child(vbox)
        popover.connect("closed", lambda p: p.unparent())
        popover.popup()
        search.grab_focus()

    # ── Preview panel ─────────────────────────────────────────────────────────

    def _build_preview_panel(self) -> Gtk.Box:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        panel.set_vexpand(True)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(12)
        header.set_margin_end(12)
        header.set_margin_top(8)
        header.set_margin_bottom(8)

        title = Gtk.Label(label="Live preview")
        title.add_css_class("heading")
        title.set_xalign(0.0)
        title.set_hexpand(True)
        header.append(title)

        self._preview_model = Gtk.StringList.new(
            ["Content slide", "Title slide", "Callout slide"]
        )
        self._preview_slide_drop = Gtk.DropDown(model=self._preview_model)
        self._preview_slide_drop.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Slide shown in the preview"]
        )
        self._preview_slide_drop.connect("notify::selected", self._schedule_preview)
        header.append(self._preview_slide_drop)

        panel.append(header)
        panel.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        if WebKit is not None:
            settings = WebKit.Settings()
            settings.set_allow_file_access_from_file_urls(True)
            settings.set_enable_javascript(False)
            settings.set_enable_page_cache(False)

            if _WEBKIT_VERSION == 6:
                net = WebKit.NetworkSession.new_ephemeral()
                self._webview = WebKit.WebView(
                    settings=settings, network_session=net
                )
            else:
                ctx = WebKit.WebContext.new_ephemeral()
                self._webview = WebKit.WebView.new_with_context(ctx)
                self._webview.set_settings(settings)

            self._webview.set_hexpand(True)
            self._webview.set_vexpand(True)
            panel.append(self._webview)
            self._has_webkit = True
        else:
            lbl = Gtk.Label(label="Install WebKitGTK for a live preview.")
            lbl.add_css_class("dim-label")
            lbl.set_valign(Gtk.Align.CENTER)
            lbl.set_vexpand(True)
            panel.append(lbl)
            self._has_webkit = False

        panel.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        panel.append(self._build_contrast_strip())
        return panel

    def _build_contrast_strip(self) -> Gtk.Box:
        """
        The one contrast readout.

        There were two — a single accent-against-background line in the
        wizard's palette step and this three-line strip in the other dialog —
        so the same theme could be graded in two places at once.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Contrast check results"]
        )

        hdr = Gtk.Label(label="Contrast check")
        hdr.set_xalign(0.0)
        hdr.add_css_class("caption")
        hdr.add_css_class("dim-label")
        box.append(hdr)

        self._contrast_labels: list[Gtk.Label] = []
        for _ in range(3):
            lbl = Gtk.Label()
            lbl.set_xalign(0.0)
            lbl.add_css_class("caption")
            box.append(lbl)
            self._contrast_labels.append(lbl)

        self._update_contrast_labels()
        return box

    def _update_contrast_labels(self) -> None:
        checks = [
            ("Body text on background",   self._theme.fg,       self._theme.bg),
            ("Accent on background",      self._theme.accent,   self._theme.bg),
            ("Title text on title slide", self._theme.title_fg, self._theme.title_bg),
        ]
        for lbl, (name, fg, bg) in zip(self._contrast_labels, checks):
            lbl.remove_css_class("error")
            lbl.remove_css_class("success")
            lbl.remove_css_class("dim-label")
            try:
                ratio = _contrast_ratio(fg, bg)
                if ratio >= CONTRAST_AAA:
                    level, icon = "AAA", "✓"
                    lbl.add_css_class("success")
                elif ratio >= CONTRAST_AA:
                    level, icon = "AA", "✓"
                    lbl.add_css_class("dim-label")
                else:
                    level, icon = "fail", "✗"
                    lbl.add_css_class("error")
                lbl.set_text(f"{icon}  {name}: {ratio:.1f}:1 ({level})")
                lbl.update_property(
                    [Gtk.AccessibleProperty.LABEL],
                    [f"{name}: {ratio:.1f} to 1, {level}"],
                )
            except Exception:
                lbl.add_css_class("dim-label")
                lbl.set_text(f"—  {name}: could not compute")

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _on_name_changed(self, row: Adw.EntryRow) -> None:
        name = row.get_text()
        if self._slug_touched:
            self._theme = dataclasses.replace(self._theme, name=name)
        else:
            slug = _auto_slug(name)
            self._syncing_slug = True
            try:
                self._slug_row.set_text(slug)
            finally:
                self._syncing_slug = False
            self._theme = dataclasses.replace(self._theme, name=name, slug=slug)
        self._validate_slug(self._theme.slug)
        self._schedule_preview()

    def _on_slug_changed(self, row: Adw.EntryRow) -> None:
        if not self._syncing_slug:
            self._slug_touched = True
        slug = row.get_text().strip()
        self._theme = dataclasses.replace(self._theme, slug=slug)
        self._validate_slug(slug)

    def _on_meta_changed(self, *_) -> None:
        self._theme = dataclasses.replace(
            self._theme,
            author=self._author_row.get_text(),
            description=self._desc_row.get_text(),
        )

    def _on_colour_changed(self, field: str, value: str) -> None:
        self._theme = dataclasses.replace(self._theme, **{field: value})
        self._update_contrast_labels()
        self._schedule_preview()

    def _on_preset_clicked(self, _btn: Gtk.Button, idx: int) -> None:
        """Fill every colour the preset owns, and show it in the rows."""
        _label, bg, fg, accent, title_bg, title_fg = _PRESETS[idx]
        self._theme = dataclasses.replace(
            self._theme,
            bg=bg, fg=fg, accent=accent,
            title_bg=title_bg, title_fg=title_fg,
        )
        self._bg_row.set_colour(bg)
        self._fg_row.set_colour(fg)
        self._accent_row.set_colour(accent)
        self._title_rows["title_bg"].set_colour(title_bg)
        self._title_rows["title_fg"].set_colour(title_fg)
        self._update_contrast_labels()
        self._schedule_preview()

    def _apply_title_style(self, invert: bool) -> None:
        bg = self._theme.fg if invert else self._theme.bg
        fg = self._theme.bg if invert else self._theme.fg
        self._theme = dataclasses.replace(self._theme, title_bg=bg, title_fg=fg)
        self._title_rows["title_bg"].set_colour(bg)
        self._title_rows["title_fg"].set_colour(fg)
        self._update_contrast_labels()
        self._schedule_preview()

    def _on_font_preset_clicked(self, _btn: Gtk.Button, idx: int) -> None:
        _label, stack, _sample = _FONTS[idx]
        self._theme = dataclasses.replace(self._theme, body_font=stack)
        # The entry row's own "changed" handler writes the same value back.
        self._font_rows_by_field["body_font"].set_text(stack)
        self._schedule_preview()

    def _on_callout_changed(self, kind: str, triple: tuple) -> None:
        self._theme = dataclasses.replace(
            self._theme, **{f"callout_{kind}": triple}
        )
        self._schedule_preview()

    def _on_icon_changed(self, kind: str, value: str) -> None:
        icon = value.replace("'", "")[:4]
        self._theme = dataclasses.replace(
            self._theme, **{f"callout_icon_{kind}": icon}
        )
        self._schedule_preview()

    def _on_simple_change(self, field: str, value) -> None:
        self._theme = dataclasses.replace(self._theme, **{field: value})
        self._schedule_preview()

    def _on_css_changed(self, buf) -> None:
        self._custom_css = buf.get_text(
            buf.get_start_iter(), buf.get_end_iter(), True
        )
        self._schedule_preview()

    def _on_add_font(self, *_) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Add font file")
        f = Gtk.FileFilter()
        f.set_name("Font files (woff2, woff, ttf, otf)")
        for pat in ("*.woff2", "*.woff", "*.ttf", "*.otf"):
            f.add_pattern(pat)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dialog.set_filters(store)
        dialog.open(self.get_root(), None, self._on_font_chosen)

    def _on_font_chosen(self, dialog, result) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        path_str = gfile.get_path()
        if not path_str:
            return
        self._add_font_row(Path(path_str))

    def _add_font_row(self, path: Path) -> None:
        if path in self._font_files:
            return
        self._font_files.append(path)
        valid    = "-" in path.stem
        subtitle = (
            "Will be bundled on export" if valid
            else "⚠ Rename to FamilyName-WeightStyle.woff2"
        )
        row = Adw.ActionRow(title=path.name, subtitle=subtitle)
        if not valid:
            row.add_css_class("error")
        remove_btn = Gtk.Button()
        remove_btn.set_child(Gtk.Image.new_from_icon_name("edit-delete-symbolic"))
        remove_btn.add_css_class("flat")
        remove_btn.set_tooltip_text("Remove from bundle")
        remove_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"Remove {path.name}"]
        )
        remove_btn.set_valign(Gtk.Align.CENTER)
        remove_btn.connect(
            "clicked", lambda *_, p=path, r=row: self._remove_font(p, r)
        )
        row.add_suffix(remove_btn)
        self._fonts_group.add(row)
        self._font_rows[path] = row

    def _remove_font(self, path: Path, row: Adw.ActionRow) -> None:
        if path in self._font_files:
            self._font_files.remove(path)
        self._fonts_group.remove(row)
        self._font_rows.pop(path, None)

    def _on_fork(self, *_) -> None:
        forked = dataclasses.replace(
            self._theme,
            name=f"{self._theme.name} (copy)",
            slug=f"{self._theme.slug}-copy",
            author="",
        )
        editor = ThemeEditor(
            self._parent_window,
            existing_theme=forked,
            on_installed=self._on_installed,
        )
        editor.present(self._parent_window)
        self.close()

    # ── Slug validation ───────────────────────────────────────────────────────

    def _validate_slug(self, slug: str) -> bool:
        def _set_error(msg: str) -> bool:
            self._slug_row.add_css_class("error")
            self._slug_row.set_tooltip_text(msg)
            if not self._slug_error_icon:
                icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                icon.add_css_class("error")
                icon.set_tooltip_text(msg)
                icon.update_property(
                    [Gtk.AccessibleProperty.LABEL], [f"Error: {msg}"]
                )
                self._slug_row.add_suffix(icon)
                self._slug_error_icon = icon
            else:
                self._slug_error_icon.set_tooltip_text(msg)
                self._slug_error_icon.set_visible(True)
            return False

        def _clear_error() -> bool:
            self._slug_row.remove_css_class("error")
            self._slug_row.set_tooltip_text("")
            if self._slug_error_icon:
                self._slug_error_icon.set_visible(False)
            return True

        if not slug:
            return _set_error("The identifier cannot be empty")
        if not _SLUG_RE.match(slug):
            return _set_error(
                "Only lowercase letters, digits, hyphens, and underscores"
            )
        editing_slug = self._editing.slug if self._editing else None
        if slug in load_all_themes() and slug != editing_slug:
            return _set_error(f"'{slug}' is already used by an installed theme")
        return _clear_error()

    # ── Live preview ──────────────────────────────────────────────────────────

    def _schedule_preview(self, *_) -> bool:
        if self._preview_debounce is not None:
            GLib.source_remove(self._preview_debounce)
        self._preview_debounce = GLib.timeout_add(
            _PREVIEW_DEBOUNCE_MS, self._render_preview
        )
        return GLib.SOURCE_REMOVE

    def _render_preview(self) -> bool:
        self._preview_debounce = None
        if not self._has_webkit:
            return GLib.SOURCE_REMOVE

        slide_type = self._preview_slide_drop.get_selected()
        try:
            theme = dataclasses.replace(self._theme)
            if self._preview_tmp_css is not None:
                Path(self._preview_tmp_css).unlink(missing_ok=True)
                self._preview_tmp_css = None

            if self._custom_css.strip():
                fd, tmp_path = tempfile.mkstemp(suffix=".css")
                try:
                    os.write(fd, self._custom_css.encode("utf-8"))
                finally:
                    os.close(fd)
                theme = dataclasses.replace(theme, custom_css_path=tmp_path)
                self._preview_tmp_css = tmp_path
            else:
                theme = dataclasses.replace(theme, custom_css_path="")

            width, height = ASPECT_RATIOS["16:9"]
            css = build_css(theme, width, height, logo_b64=None)

            if slide_type == 1:
                body = (
                    "<div class='slide title-slide'>"
                    "<h1>Presentation title</h1>"
                    "<h2>A subtitle or tagline</h2>"
                    "<p class='title-meta'>Author · 2025</p>"
                    "</div>"
                )
            elif slide_type == 2:
                body = (
                    "<div class='slide'>"
                    "<h2>Callout types</h2>"
                    "<div class='callout-tip'><p>Tip — useful hints</p></div>"
                    "<div class='callout-info'><p>Info — neutral info</p></div>"
                    "<div class='callout-warning'><p>Warning</p></div>"
                    "<div class='callout-danger'><p>Danger</p></div>"
                    "</div>"
                )
            else:
                from .slides.renderer import render_slide_content
                rendered = render_slide_content(_PREVIEW_MD)
                pg_num = (
                    "<div class='slide-number'>1 / 4</div>"
                    "<div class='progress-bar-track'>"
                    "<div class='progress-bar-fill' style='width:25%'></div>"
                    "</div>"
                )
                body = f"<div class='slide'>{rendered}{pg_num}</div>"

            SCALE = 0.3125
            scaled_w = int(1280 * SCALE)
            scaled_h = int(720  * SCALE)
            scale_css = (
                "<style>"
                "html,body{margin:0;padding:0;height:100%;background:#111;"
                "display:flex;align-items:center;justify-content:center;}"
                f".slide-wrapper{{width:{scaled_w}px;height:{scaled_h}px;"
                f"overflow:hidden;flex-shrink:0;}}"
                f".slide-wrapper .slide{{transform-origin:top left;"
                f"transform:scale({SCALE});width:1280px;height:720px;"
                "page-break-after:unset !important;}}"
                "</style>"
            )
            html = (
                "<!DOCTYPE html><html lang='en'><head>"
                "<meta charset='UTF-8'>"
                f"<style>{css}</style>{scale_css}"
                "</head><body>"
                f"<div class='slide-wrapper'>{body}</div>"
                "</body></html>"
            )
            self._webview.load_html(html, "about:blank")
        except Exception as exc:
            log.warning("Preview render failed: %s", exc)
        return GLib.SOURCE_REMOVE

    # ── Export / install ──────────────────────────────────────────────────────

    def _collect_theme_json(self) -> dict:
        t = self._theme
        data: dict = {
            "name":        t.name,
            "slug":        t.slug,
            "author":      t.author,
            "version":     t.version,
            "description": t.description,
            "bg":          t.bg,
            "fg":          t.fg,
            "accent":      t.accent,
        }
        if t.accent2:
            data["accent2"] = t.accent2
        if t.heading_color:
            data["heading_color"] = t.heading_color
        data["code_bg"]  = t.code_bg
        data["title_bg"] = t.title_bg
        data["title_fg"] = t.title_fg
        if t.title_accent:
            data["title_accent"] = t.title_accent
        data["body_font"] = t.body_font
        if t.heading_font:
            data["heading_font"] = t.heading_font
        data["mono_font"]      = t.mono_font
        data["base_size"]      = t.base_size
        data["pygments_style"] = t.pygments_style
        for kind in _CALLOUT_KINDS:
            data[f"callout_{kind}"]      = list(getattr(t, f"callout_{kind}"))
            data[f"callout_icon_{kind}"] = getattr(t, f"callout_icon_{kind}")
        return data

    def _write_to_dir(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "theme.json").write_text(
            json.dumps(self._collect_theme_json(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if self._custom_css.strip():
            (dest / "theme.css").write_text(self._custom_css, encoding="utf-8")
        if self._font_files:
            fonts_dest = dest / "fonts"
            fonts_dest.mkdir(exist_ok=True)
            for src in self._font_files:
                if src.exists():
                    shutil.copy2(src, fonts_dest / src.name)

    def _on_install(self, *_) -> None:
        slug = self._theme.slug.strip()
        if not self._theme.name.strip():
            self._show_error(
                "The theme needs a name",
                "Give the theme a name so it can be told apart from the others.",
            )
            return
        if not self._validate_slug(slug):
            return
        try:
            with tempfile.TemporaryDirectory() as tmp_root:
                theme_dir = Path(tmp_root) / slug
                self._write_to_dir(theme_dir)
                installed = install_theme(theme_dir)

            if self._on_installed:
                self._on_installed(installed)

            self._toast(f"Theme '{installed.name}' installed", 3)
            self.close()

        except (ValueError, OSError) as exc:
            self._show_error("Could not install the theme", str(exc))

    def _on_export_zip(self, *_) -> None:
        slug = self._theme.slug.strip() or "theme"
        dialog = Gtk.FileDialog()
        dialog.set_title("Export theme as zip")
        dialog.set_initial_name(f"{slug}.zip")
        store = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter()
        f.set_name("Zip archives (*.zip)")
        f.add_pattern("*.zip")
        store.append(f)
        dialog.set_filters(store)
        dialog.save(self.get_root(), None, self._on_export_zip_chosen)

    def _on_export_zip_chosen(self, dialog, result) -> None:
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        path_str = gfile.get_path()
        if not path_str:
            return
        zip_path = Path(path_str)
        slug     = self._theme.slug.strip() or "theme"

        def _do():
            try:
                with tempfile.TemporaryDirectory() as tmp_root:
                    theme_dir = Path(tmp_root) / slug
                    self._write_to_dir(theme_dir)
                    with zipfile.ZipFile(zip_path, "w",
                                         compression=zipfile.ZIP_DEFLATED) as zf:
                        for f in sorted(theme_dir.rglob("*")):
                            if f.is_file():
                                zf.write(f, f.relative_to(tmp_root))
                GLib.idle_add(_done)
            except (OSError, zipfile.BadZipFile) as exc:
                GLib.idle_add(_err, str(exc))

        def _done():
            self._toast(f"Exported to {zip_path.name}", 4)
            return GLib.SOURCE_REMOVE

        def _err(msg):
            self._show_error("Could not export the theme", msg)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=_do, daemon=True).start()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _toast(self, message: str, timeout: int) -> None:
        toast = Adw.Toast(title=message)
        toast.set_timeout(timeout)
        if hasattr(self._parent_window, "_toast_overlay"):
            self._parent_window._toast_overlay.add_toast(toast)

    def _show_error(self, heading: str, message: str) -> None:
        """
        An alert names what failed.

        Both editors used to head every one of these "Error", which tells the
        reader only that they are reading an error dialog.
        """
        dialog = Adw.AlertDialog(heading=heading, body=message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.present(self)

    def _on_destroy(self, *_) -> None:
        if self._preview_debounce is not None:
            GLib.source_remove(self._preview_debounce)
            self._preview_debounce = None
        if self._preview_tmp_css is not None:
            Path(self._preview_tmp_css).unlink(missing_ok=True)
            self._preview_tmp_css = None


# ── Colour row widget ─────────────────────────────────────────────────────────

class _ColourRow(Adw.ActionRow):
    """
    Adw.ActionRow with a colour swatch button + hex Entry, synced in both
    directions.  Uses Gtk.ColorDialogButton (GTK 4.10+) with ColorButton
    fallback.

    HIG compliance:
    - Picker has accessible label and tooltip.
    - Hex entry has fixed width (set_width_chars + set_hexpand(False)).
    - Empty nullable fields show a diagonal-line swatch (not a white square).
    - Optional *description* shown as the row subtitle.
    """

    _PLACEHOLDER_CSS_LOADED: bool = False
    _PLACEHOLDER_CSS = """
        .colour-row-empty-swatch {
            background-image: linear-gradient(
                to bottom right,
                transparent calc(50% - 1px),
                @borders calc(50% - 1px),
                @borders calc(50% + 1px),
                transparent calc(50% + 1px)
            );
            background-color: @card_bg_color;
        }
    """

    def __init__(
        self,
        title:       str,
        initial:     str,
        callback:    Callable[[str], None],
        nullable:    bool = False,
        description: str  = "",
    ) -> None:
        super().__init__(title=title)
        if description:
            self.set_subtitle(description)
        self._callback = callback
        self._nullable = nullable
        self._updating = False

        _ColourRow._ensure_placeholder_css()

        self._entry = Gtk.Entry()
        self._entry.set_placeholder_text("#rrggbb" if not nullable else "inherit")
        self._entry.set_max_length(7)
        self._entry.set_width_chars(8)
        self._entry.set_hexpand(False)
        self._entry.set_valign(Gtk.Align.CENTER)
        self._entry.set_input_purpose(Gtk.InputPurpose.FREE_FORM)
        self._entry.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"{title} colour, hex value"]
        )
        if initial:
            self._entry.set_text(initial)
        self._entry.connect("changed", self._on_entry_changed)

        rgba = _hex_to_rgba(initial) if initial else Gdk.RGBA()

        if _HAS_COLOR_DIALOG:
            self._picker = Gtk.ColorDialogButton(
                dialog=Gtk.ColorDialog(with_alpha=False), rgba=rgba
            )
            self._picker.connect("notify::rgba", self._on_picker_changed)
        else:
            self._picker = Gtk.ColorButton(rgba=rgba, use_alpha=False)
            self._picker.connect("color-set", self._on_picker_changed)

        self._picker.set_valign(Gtk.Align.CENTER)
        self._picker.set_tooltip_text(f"Choose {title.lower()} colour")
        self._picker.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"{title} colour"]
        )

        if nullable and not initial:
            self._picker.add_css_class("colour-row-empty-swatch")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_valign(Gtk.Align.CENTER)
        box.append(self._picker)
        box.append(self._entry)
        self.add_suffix(box)

    def set_colour(self, hex_val: str) -> None:
        """Programmatically update both picker and entry without re-entrancy."""
        self._updating = True
        try:
            self._entry.set_text(hex_val)
            self._picker.set_rgba(_hex_to_rgba(hex_val))
            self._clear_empty_swatch()
        finally:
            self._updating = False

    @classmethod
    def _ensure_placeholder_css(cls) -> None:
        if cls._PLACEHOLDER_CSS_LOADED:
            return
        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(cls._PLACEHOLDER_CSS)
        except AttributeError:
            provider.load_from_data(cls._PLACEHOLDER_CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        cls._PLACEHOLDER_CSS_LOADED = True

    def _set_empty_swatch(self) -> None:
        self._picker.add_css_class("colour-row-empty-swatch")

    def _clear_empty_swatch(self) -> None:
        self._picker.remove_css_class("colour-row-empty-swatch")

    def _on_entry_changed(self, entry: Gtk.Entry) -> None:
        if self._updating:
            return
        text = entry.get_text().strip()
        if not text and self._nullable:
            self._set_empty_swatch()
            self._callback("")
            return
        if re.match(r'^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$', text):
            self._updating = True
            try:
                self._picker.set_rgba(_hex_to_rgba(text))
                self._clear_empty_swatch()
            finally:
                self._updating = False
            self._callback(text)

    def _on_picker_changed(self, picker, *_) -> None:
        if self._updating:
            return
        hex_val = _rgba_to_hex(picker.get_rgba())
        self._updating = True
        try:
            self._entry.set_text(hex_val)
            self._clear_empty_swatch()
        finally:
            self._updating = False
        self._callback(hex_val)


# ── Callout row ───────────────────────────────────────────────────────────────

class _CalloutRow(Adw.ExpanderRow):
    """
    One callout's three colours and its badge character, together.

    The icon used to live in a separate "Callout icons" group four rows
    further down, so setting up a callout meant editing it in two places.
    """

    def __init__(
        self,
        title:      str,
        triple:     tuple,
        icon:       str,
        on_colours: Callable[[tuple], None],
        on_icon:    Callable[[str], None],
    ) -> None:
        super().__init__(title=title)
        self._on_colours = on_colours
        self._triple     = list(triple)

        for i, (label, initial) in enumerate(
            zip(("Background", "Text", "Border"), triple)
        ):
            self.add_row(_ColourRow(
                label, initial,
                callback=lambda v, idx=i: self._on_changed(idx, v),
            ))

        icon_row = Adw.EntryRow(title="Badge character", text=icon)
        icon_row.connect("changed", lambda r: on_icon(r.get_text()))
        self.add_row(icon_row)
        self._icon_row = icon_row

    def _on_changed(self, idx: int, value: str) -> None:
        self._triple[idx] = value
        self._on_colours(tuple(self._triple))


# ── Colour utilities ──────────────────────────────────────────────────────────

def _apply_swatch_colour(widget: Gtk.Widget, colour: str) -> None:
    provider = Gtk.CssProvider()
    rule = f"box {{ background-color: {colour}; }}"
    try:
        provider.load_from_string(rule)
    except AttributeError:
        provider.load_from_data(rule.encode())
    widget.get_style_context().add_provider(
        provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


def _hex_to_rgba(hex_colour: str) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    if hex_colour and not rgba.parse(hex_colour):
        rgba.parse("#808080")
    return rgba


def _rgba_to_hex(rgba: Gdk.RGBA) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255)
    )


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    def _lum(h: str) -> float:
        h = h.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = (int(h[i:i+2], 16) / 255 for i in (0, 2, 4))
        def _lin(c):
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
    l1, l2 = _lum(fg_hex), _lum(bg_hex)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _auto_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", name.lower().strip()).strip("-") or "theme"
