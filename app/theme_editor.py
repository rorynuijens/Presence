"""
theme_editor.py — Beginner-friendly wizard for creating and editing themes.

Opens from Settings → Manage Themes → "New theme" or "Edit".
Produces a theme package directory (theme.json + optional theme.css +
optional fonts/) and either installs it directly or exports it as a .zip.

Design
------
A four-step wizard organised around what the user wants rather than the
Theme dataclass fields:

  Step 1  Name        — one text field, nothing else
  Step 2  Palette     — preset swatches + three colour pickers (bg, fg, accent)
                        with an optional "show advanced colours" disclosure
  Step 3  Font        — five font cards showing live samples; one tap selects
  Step 4  Title slide — three layout cards (dark/light/custom cover)

A persistent two-thumbnail live preview sits at the bottom of the right
panel at all times.  "Skip to advanced editor" is always visible in the
footer and opens the full-field Adw.PreferencesPage in a second window.

GNOME HIG compliance notes
---------------------------
- Adw.Window (not Adw.Dialog) so it gets its own window ID and taskbar entry
  for a document-like workflow.
- Adw.ToolbarView with Adw.HeaderBar — Cancel leading, [Export zip] then
  [Install] trailing (two separate pack_end calls, primary rightmost).
- Navigation bar uses Gtk.ListBox (boxed-list style) rather than custom
  widgets — accessible roles come for free.
- Each wizard page is a Gtk.ScrolledWindow wrapping a Gtk.Box of
  Adw.PreferencesGroup widgets.  This gives the correct Adwaita card
  appearance without fighting the preferences page's centering constraints.
- Colour pickers: Gtk.ColorDialogButton (GTK 4.10+) with Gtk.ColorButton
  fallback.  Both picker and hex Entry are always visible and in sync.
- Font cards are Gtk.ToggleButton in a linked Gtk.FlowBox — correct HIG
  pattern for multi-option selection with spatial previews.
- Preset cards are Gtk.ToggleButton with radio-group mutual exclusion via
  Gtk.ToggleButton.set_group().
- All interactive widgets have accessible labels via update_property().
- Contrast ratios are displayed inline next to the accent colour picker with
  Adwaita semantic CSS classes (success / warning / error).
- The "show advanced colours" section is a Gtk.Revealer — not display:none.
- Slug is auto-derived from the name and validated on every change.
- "Skip to advanced editor" opens a second ThemeEditorAdvanced window
  pre-populated from the current wizard state.
- Built-in themes open with the entire form insensitive and an Adw.Banner
  in the ToolbarView header region offering "Fork as new theme".
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

from md_to_slides.themes       import Theme, BUILTIN_THEMES
from md_to_slides.theme_loader import load_all_themes, install_theme, user_themes_dir
from md_to_slides.css          import build_css
from md_to_slides.themes       import ASPECT_RATIOS

# ── Constants ─────────────────────────────────────────────────────────────────

_SLUG_RE             = re.compile(r'^[a-z0-9_-]+$')
_PREVIEW_DEBOUNCE_MS = 300

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
_PRESETS: list[tuple[str, str, str, str, str, str]] = [
    ("Classic",   "#ffffff", "#1a1a2e", "#E17000", "#1a1a2e", "#ffffff"),
    ("Midnight",  "#1a1a2e", "#e8e8f0", "#5b8dee", "#0d0d1a", "#ffffff"),
    ("Sage",      "#f5f0e8", "#1c2b1e", "#2d6a4f", "#1c2b1e", "#f5f0e8"),
    ("Carbon",    "#111111", "#f0f0f0", "#ff6b6b", "#000000", "#f0f0f0"),
    ("Parchment", "#fefae0", "#3a2a1a", "#bc6c25", "#3a2a1a", "#fefae0"),
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

# Title slide styles — (label, description, use dark cover)
_TITLE_STYLES: list[tuple[str, str, bool]] = [
    ("Dark cover",  "Title slide uses a dark background with your accent colour", True),
    ("Light cover", "Title slide uses the same background as your content slides", False),
    ("Custom",      "Set title slide colours independently in the advanced editor", None),
]


# ── Wizard window ─────────────────────────────────────────────────────────────

class ThemeEditor(Adw.Window):
    """
    Four-step wizard for creating or editing a theme.

    Parameters
    ----------
    parent_window:
        Transient parent (MainWindow).
    existing_theme:
        Pre-populate from this theme when editing.  None for a new theme.
    on_installed:
        Called on the main thread after a successful install.
    """

    _N_STEPS = 4

    def __init__(
        self,
        parent_window,
        existing_theme: Theme | None = None,
        on_installed:   Callable[[Theme], None] | None = None,
    ) -> None:
        super().__init__()

        self._parent_window  = parent_window
        self._on_installed   = on_installed
        self._editing        = existing_theme
        self._is_builtin     = (
            existing_theme is not None
            and existing_theme.slug in BUILTIN_THEMES
        )

        self._theme              = self._initial_theme(existing_theme)
        self._initial_name_slug  = _auto_slug(self._theme.name)
        self._custom_css: str    = self._load_custom_css(existing_theme)
        self._font_files: list[Path] = []

        # Wizard state
        self._step: int = 0         # 0-based current step index
        self._preset_idx: int = 0   # selected preset index
        self._font_idx: int   = 1   # selected font index (IBM Plex default)
        self._title_style: int = 0  # 0=dark, 1=light, 2=custom

        # Preview
        self._preview_debounce: int | None  = None
        self._preview_tmp_css:  str | None  = None

        # Slug validation error icon (lazily added to the name/slug row)
        self._slug_error_icon = None

        self.set_transient_for(parent_window)
        self.set_modal(True)
        self.set_default_size(960, 640)
        self.set_title(
            f"Edit theme — {existing_theme.name}" if existing_theme else "New theme"
        )

        self._build_ui()
        self.connect("destroy", self._on_destroy)
        GLib.idle_add(self._schedule_preview)

    # ── Static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _initial_theme(existing: Theme | None) -> Theme:
        if existing is None:
            t = Theme(name="New Theme", slug="new-theme", author="")
            # Apply the first preset as default values
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
        self.set_content(toolbar_view)
        toolbar_view.add_top_bar(self._build_header())

        if self._is_builtin:
            banner = Adw.Banner(
                title="Built-in themes are read-only",
                button_label="Fork as new theme",
            )
            banner.set_revealed(True)
            banner.connect("button-clicked", self._on_fork)
            toolbar_view.add_top_bar(banner)

        # Outer split: nav sidebar | content + preview
        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        # Left nav
        nav = self._build_nav()
        split.append(nav)
        split.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # Right: stacked page area + persistent preview strip
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right.set_hexpand(True)
        right.set_vexpand(True)

        # Page stack — one child per step
        self._page_stack = Gtk.Stack()
        self._page_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self._page_stack.set_transition_duration(200)
        self._page_stack.set_hexpand(True)
        self._page_stack.set_vexpand(True)

        self._pages: list[Gtk.Widget] = [
            self._build_step_name(),
            self._build_step_palette(),
            self._build_step_font(),
            self._build_step_title(),
        ]
        for i, page in enumerate(self._pages):
            self._page_stack.add_named(page, f"step-{i}")

        right.append(self._page_stack)
        right.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        right.append(self._build_preview_strip())

        split.append(right)

        if self._is_builtin:
            split.set_sensitive(False)

        toolbar_view.set_content(split)
        self._go_to_step(0)

    def _build_header(self) -> Adw.HeaderBar:
        bar = Adw.HeaderBar()

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        bar.pack_start(cancel_btn)

        if not self._is_builtin:
            # Primary action rightmost; pack_end inserts right-to-left
            install_label = "Save" if self._editing else "Install"
            self._install_btn = Gtk.Button(label=install_label)
            self._install_btn.add_css_class("suggested-action")
            self._install_btn.connect("clicked", self._on_install)
            bar.pack_end(self._install_btn)

            export_btn = Gtk.Button(label="Export as zip…")
            export_btn.connect("clicked", self._on_export_zip)
            bar.pack_end(export_btn)

        return bar

    # ── Navigation sidebar ────────────────────────────────────────────────────

    def _build_nav(self) -> Gtk.Box:
        """
        Left navigation panel.

        Uses a Gtk.ListBox (boxed-list style) for the step list so that GTK
        handles selection state and accessible roles automatically.  A plain
        Gtk.Box footer holds the navigation buttons and the advanced editor
        escape hatch.
        """
        nav = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        nav.set_size_request(220, -1)

        # Title area
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_box.set_margin_start(16)
        title_box.set_margin_end(16)
        title_box.set_margin_top(16)
        title_box.set_margin_bottom(12)

        self._nav_title = Gtk.Label(label="New theme")
        self._nav_title.set_xalign(0.0)
        self._nav_title.add_css_class("title-4")
        title_box.append(self._nav_title)

        subtitle = Gtk.Label(label=f"{self._N_STEPS} steps · about 2 minutes")
        subtitle.set_xalign(0.0)
        subtitle.add_css_class("caption")
        subtitle.add_css_class("dim-label")
        title_box.append(subtitle)

        nav.append(title_box)
        nav.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Step list — Gtk.ListBox gives free accessible navigation
        self._nav_list = Gtk.ListBox()
        self._nav_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._nav_list.add_css_class("navigation-sidebar")
        self._nav_list.set_margin_top(6)
        self._nav_list.set_margin_bottom(6)
        self._nav_list.set_vexpand(True)
        self._nav_list.connect("row-activated", self._on_nav_row_activated)

        step_defs = [
            ("Name your theme",     "Pick a name for your theme"),
            ("Colour palette",      "Background, text, and accent"),
            ("Font",                "One font for everything"),
            ("Title slide",         "How your cover page looks"),
        ]
        self._nav_rows: list[Gtk.ListBoxRow] = []
        for i, (label, hint) in enumerate(step_defs):
            row = self._build_nav_row(i + 1, label, hint)
            self._nav_list.append(row)
            self._nav_rows.append(row)

        nav.append(self._nav_list)
        nav.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Footer: Back / Next + advanced escape hatch
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        footer.set_margin_top(10)
        footer.set_margin_bottom(10)

        nav_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        nav_btns.add_css_class("linked")

        self._back_btn = Gtk.Button(label="← Back")
        self._back_btn.set_hexpand(True)
        self._back_btn.connect("clicked", self._on_back)
        nav_btns.append(self._back_btn)

        self._next_btn = Gtk.Button(label="Next →")
        self._next_btn.set_hexpand(True)
        self._next_btn.add_css_class("suggested-action")
        self._next_btn.connect("clicked", self._on_next)
        nav_btns.append(self._next_btn)

        footer.append(nav_btns)

        advanced_btn = Gtk.Button(label="Skip to advanced editor…")
        advanced_btn.add_css_class("flat")
        advanced_btn.connect("clicked", self._on_open_advanced)
        footer.append(advanced_btn)

        nav.append(footer)
        return nav

    def _build_nav_row(self, number: int, label: str, hint: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(True)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        # Step number badge
        num_lbl = Gtk.Label(label=str(number))
        num_lbl.set_size_request(22, 22)
        num_lbl.add_css_class("caption")
        num_lbl.add_css_class("dim-label")
        box.append(num_lbl)
        row._num_label = num_lbl

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        step_lbl = Gtk.Label(label=label)
        step_lbl.set_xalign(0.0)
        step_lbl.add_css_class("body")
        hint_lbl = Gtk.Label(label=hint)
        hint_lbl.set_xalign(0.0)
        hint_lbl.add_css_class("caption")
        hint_lbl.add_css_class("dim-label")
        text_box.append(step_lbl)
        text_box.append(hint_lbl)
        box.append(text_box)

        # Done checkmark (hidden until step is completed)
        done_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        done_icon.set_visible(False)
        done_icon.set_halign(Gtk.Align.END)
        done_icon.set_hexpand(True)
        done_icon.add_css_class("success")
        box.append(done_icon)
        row._done_icon = done_icon

        row.set_child(box)
        return row

    def _mark_step_done(self, step_idx: int) -> None:
        row = self._nav_rows[step_idx]
        row._done_icon.set_visible(True)
        row._num_label.add_css_class("success")

    def _go_to_step(self, step: int) -> None:
        self._step = step
        self._page_stack.set_visible_child_name(f"step-{step}")
        self._nav_list.select_row(self._nav_rows[step])
        self._back_btn.set_sensitive(step > 0)
        is_last = (step == self._N_STEPS - 1)
        self._next_btn.set_label("Done ✓" if is_last else "Next →")
        self._next_btn.set_sensitive(True)

    def _on_nav_row_activated(self, list_box: Gtk.ListBox,
                               row: Gtk.ListBoxRow) -> None:
        idx = self._nav_rows.index(row)
        self._go_to_step(idx)

    def _on_back(self, *_) -> None:
        if self._step > 0:
            self._go_to_step(self._step - 1)

    def _on_next(self, *_) -> None:
        if self._step < self._N_STEPS - 1:
            self._mark_step_done(self._step)
            self._go_to_step(self._step + 1)
        else:
            # Final step — trigger install
            self._mark_step_done(self._step)
            self._on_install()

    def _on_open_advanced(self, *_) -> None:
        """Open the full-field editor pre-populated from the current state."""
        adv = ThemeEditorAdvanced(
            self._parent_window,
            existing_theme=dataclasses.replace(self._theme),
            custom_css=self._custom_css,
            font_files=list(self._font_files),
            on_installed=self._on_installed,
            wizard=self,          # so the advanced editor can return here
        )
        adv.present()
        # Keep the wizard alive but hide it — the advanced editor may return
        self.set_visible(False)

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
        editor.present()
        self.close()

    # ── Step 1: Name ──────────────────────────────────────────────────────────

    def _build_step_name(self) -> Gtk.ScrolledWindow:
        scroll, box = self._step_scroll()

        group = Adw.PreferencesGroup(title="Name your theme")
        group.set_description(
            "This is shown in the theme list and when sharing your theme with others."
        )

        self._name_row = Adw.EntryRow(title="Theme name", text=self._theme.name)
        self._name_row.connect("changed", self._on_name_changed)
        group.add(self._name_row)

        self._author_row = Adw.EntryRow(title="Author (optional)", text=self._theme.author)
        self._author_row.connect("changed", self._on_author_changed)
        group.add(self._author_row)

        self._desc_row = Adw.EntryRow(
            title="Description (optional)", text=self._theme.description
        )
        self._desc_row.connect("changed", self._on_desc_changed)
        group.add(self._desc_row)

        box.append(group)
        return scroll

    def _on_name_changed(self, row: Adw.EntryRow) -> None:
        name = row.get_text()
        if self._theme.slug in ("", "theme", self._initial_name_slug):
            slug = _auto_slug(name)
            self._theme = dataclasses.replace(self._theme, name=name, slug=slug)
        else:
            self._theme = dataclasses.replace(self._theme, name=name)
        self._nav_title.set_label(name or "New theme")
        self._validate_slug(self._theme.slug)
        self._schedule_preview()

    def _on_author_changed(self, row: Adw.EntryRow) -> None:
        self._theme = dataclasses.replace(self._theme, author=row.get_text())

    def _on_desc_changed(self, row: Adw.EntryRow) -> None:
        self._theme = dataclasses.replace(self._theme, description=row.get_text())

    # ── Step 2: Palette ───────────────────────────────────────────────────────

    def _build_step_palette(self) -> Gtk.ScrolledWindow:
        scroll, box = self._step_scroll()

        # ── Preset swatches ───────────────────────────────────────────────────
        preset_group = Adw.PreferencesGroup(title="Start with a preset")
        preset_group.set_description(
            "Pick one to fill in all colour values automatically."
        )

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_column_spacing(8)
        flow.set_row_spacing(8)
        flow.set_max_children_per_line(4)
        flow.set_min_children_per_line(2)
        flow.set_margin_top(6)
        flow.set_margin_bottom(6)

        self._preset_btns: list[Gtk.ToggleButton] = []
        first_btn: Gtk.ToggleButton | None = None

        for i, (label, bg, fg, accent, tbg, tfg) in enumerate(_PRESETS):
            btn = Gtk.ToggleButton()
            btn.set_tooltip_text(label)
            btn.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [f"{label} colour preset"],
            )
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)

            btn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            swatch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            swatch.set_size_request(-1, 36)
            swatch.set_overflow(Gtk.Overflow.HIDDEN)

            # CSS provider for the two swatch halves
            for color in (bg, accent):
                half = Gtk.Box()
                half.set_hexpand(True)
                provider = Gtk.CssProvider()
                try:
                    provider.load_from_string(
                        f"box {{ background-color: {color}; }}"
                    )
                except AttributeError:
                    provider.load_from_data(
                        f"box {{ background-color: {color}; }}".encode()
                    )
                half.get_style_context().add_provider(
                    provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
                swatch.append(half)

            name_lbl = Gtk.Label(label=label)
            name_lbl.add_css_class("caption")
            name_lbl.set_margin_top(4)
            name_lbl.set_margin_bottom(4)

            btn_box.append(swatch)
            btn_box.append(name_lbl)
            btn.set_child(btn_box)
            btn.connect("toggled", self._on_preset_toggled, i)
            self._preset_btns.append(btn)

            child = Gtk.FlowBoxChild()
            child.set_child(btn)
            flow.append(child)

        flow_row = Adw.ActionRow()
        flow_row.set_activatable(False)
        flow_row.set_child(flow)
        preset_group.add(flow_row)
        box.append(preset_group)

        # ── Three key colour pickers ──────────────────────────────────────────
        colours_group = Adw.PreferencesGroup(title="Fine-tune")
        colours_group.set_description(
            "Click any swatch to open the colour picker, or type a hex value."
        )

        self._bg_row = _ColourRow(
            "Background",
            self._theme.bg,
            callback=lambda v: self._on_palette_colour("bg", v),
            description="The main slide background colour",
        )
        self._fg_row = _ColourRow(
            "Text",
            self._theme.fg,
            callback=lambda v: self._on_palette_colour("fg", v),
            description="Body text, bullets, and captions",
        )
        self._accent_row = _ColourRow(
            "Accent",
            self._theme.accent,
            callback=lambda v: self._on_palette_colour("accent", v),
            description="Headings, links, and highlighted elements",
        )
        colours_group.add(self._bg_row)
        colours_group.add(self._fg_row)
        colours_group.add(self._accent_row)
        box.append(colours_group)

        # Contrast feedback row — shown beneath the colour group
        self._contrast_row = Adw.ActionRow()
        self._contrast_row.set_activatable(False)
        self._contrast_lbl = Gtk.Label()
        self._contrast_lbl.set_xalign(0.0)
        self._contrast_lbl.add_css_class("caption")
        self._contrast_lbl.set_valign(Gtk.Align.CENTER)
        self._contrast_lbl.set_hexpand(True)
        self._contrast_row.add_prefix(self._contrast_lbl)
        colours_group.add(self._contrast_row)
        self._update_contrast()

        # ── Advanced disclosure ───────────────────────────────────────────────
        adv_group = Adw.PreferencesGroup()
        box.append(adv_group)

        self._adv_expander = Adw.ExpanderRow(
            title="Advanced colours",
            subtitle="Accent 2, heading colour, code background",
        )
        adv_group.add(self._adv_expander)

        for field, label, hint in (
            ("accent2",       "Accent 2",        "Optional second accent colour — leave blank to use accent"),
            ("heading_color", "Heading colour",  "Overrides accent for headings — leave blank to use accent"),
            ("code_bg",       "Code background", "Background for inline code and fenced code blocks"),
        ):
            row = _ColourRow(
                label,
                getattr(self._theme, field, "") or "",
                callback=lambda v, f=field: self._on_advanced_colour(f, v),
                description=hint,
                nullable=(field != "code_bg"),
            )
            self._adv_expander.add_row(row)

        # Activate the initial preset now that all colour rows exist.
        # Doing this earlier would crash because _bg_row etc. don't exist yet.
        self._preset_btns[self._preset_idx].set_active(True)

        return scroll

    def _on_preset_toggled(self, btn: Gtk.ToggleButton, idx: int) -> None:
        if not btn.get_active():
            return
        self._preset_idx = idx
        label, bg, fg, accent, title_bg, title_fg = _PRESETS[idx]
        self._theme = dataclasses.replace(
            self._theme,
            bg=bg, fg=fg, accent=accent,
            title_bg=title_bg, title_fg=title_fg,
        )
        # Colour rows may not exist yet if called during widget construction
        if not hasattr(self, "_bg_row"):
            return
        self._bg_row.set_colour(bg)
        self._fg_row.set_colour(fg)
        self._accent_row.set_colour(accent)
        self._update_contrast()
        self._schedule_preview()

    def _on_palette_colour(self, field: str, value: str) -> None:
        self._theme = dataclasses.replace(self._theme, **{field: value})
        self._update_contrast()
        self._schedule_preview()

    def _on_advanced_colour(self, field: str, value: str) -> None:
        self._theme = dataclasses.replace(self._theme, **{field: value})
        self._schedule_preview()

    def _update_contrast(self) -> None:
        try:
            ratio = _contrast_ratio(self._theme.accent, self._theme.bg)
            if ratio >= 4.5:
                self._contrast_lbl.set_text(
                    f"✓  Accent contrast {ratio:.1f}:1 — good"
                )
                self._contrast_lbl.remove_css_class("error")
                self._contrast_lbl.remove_css_class("warning")
                self._contrast_lbl.add_css_class("success")
            elif ratio >= 3.0:
                self._contrast_lbl.set_text(
                    f"  Accent contrast {ratio:.1f}:1 — try darkening slightly"
                )
                self._contrast_lbl.remove_css_class("error")
                self._contrast_lbl.remove_css_class("success")
                self._contrast_lbl.add_css_class("warning")
            else:
                self._contrast_lbl.set_text(
                    f"✗  Accent contrast {ratio:.1f}:1 — too low, headings may be hard to read"
                )
                self._contrast_lbl.remove_css_class("warning")
                self._contrast_lbl.remove_css_class("success")
                self._contrast_lbl.add_css_class("error")
            self._contrast_lbl.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [f"Accent contrast ratio: {ratio:.1f} to 1"],
            )
        except Exception:
            self._contrast_lbl.set_text("")

    # ── Step 3: Font ──────────────────────────────────────────────────────────

    def _build_step_font(self) -> Gtk.ScrolledWindow:
        scroll, box = self._step_scroll()

        group = Adw.PreferencesGroup(title="Choose a font")
        group.set_description(
            "The same font is used for headings, body text, and the title slide. "
            "You can set heading and mono fonts separately in the advanced editor."
        )

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_column_spacing(8)
        flow.set_row_spacing(8)
        flow.set_max_children_per_line(3)
        flow.set_min_children_per_line(2)
        flow.set_margin_top(6)
        flow.set_margin_bottom(6)

        self._font_btns: list[Gtk.ToggleButton] = []
        first_font_btn: Gtk.ToggleButton | None = None

        for i, (label, stack, sample) in enumerate(_FONTS):
            btn = Gtk.ToggleButton()
            btn.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [f"Font: {label} — {sample}"],
            )
            if first_font_btn is None:
                first_font_btn = btn
            else:
                btn.set_group(first_font_btn)

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

            # Apply font via Pango attribute list (GTK4 — set_font() is GTK3)
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
            btn.connect("toggled", self._on_font_toggled, i)
            self._font_btns.append(btn)

            child = Gtk.FlowBoxChild()
            child.set_child(btn)
            flow.append(child)

        self._font_btns[self._font_idx].set_active(True)

        flow_row = Adw.ActionRow()
        flow_row.set_activatable(False)
        flow_row.set_child(flow)
        group.add(flow_row)
        box.append(group)

        # Base size spinner
        size_group = Adw.PreferencesGroup(title="Font size")
        size_group.set_description(
            "Base size in pixels at 1280 × 720. Heading sizes scale from this."
        )
        size_row = Adw.ActionRow(title="Base size", subtitle="Default is 34 px")
        self._size_spin = Gtk.SpinButton.new_with_range(20, 60, 1)
        self._size_spin.set_value(self._theme.base_size)
        self._size_spin.set_valign(Gtk.Align.CENTER)
        self._size_spin.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Base font size in pixels"]
        )
        self._size_spin.connect("value-changed", self._on_base_size_changed)
        size_row.add_suffix(self._size_spin)
        size_row.set_activatable_widget(self._size_spin)
        size_group.add(size_row)
        box.append(size_group)

        return scroll

    def _on_font_toggled(self, btn: Gtk.ToggleButton, idx: int) -> None:
        if not btn.get_active():
            return
        self._font_idx = idx
        _, stack, _ = _FONTS[idx]
        self._theme = dataclasses.replace(self._theme, body_font=stack)
        self._schedule_preview()

    def _on_base_size_changed(self, spin: Gtk.SpinButton) -> None:
        self._theme = dataclasses.replace(
            self._theme, base_size=int(spin.get_value())
        )
        self._schedule_preview()

    # ── Step 4: Title slide ───────────────────────────────────────────────────

    def _build_step_title(self) -> Gtk.ScrolledWindow:
        scroll, box = self._step_scroll()

        group = Adw.PreferencesGroup(title="Title slide style")
        group.set_description(
            "The title slide is the first slide in your presentation — "
            "usually your deck name and your name."
        )

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_column_spacing(8)
        flow.set_row_spacing(8)
        flow.set_max_children_per_line(3)
        flow.set_min_children_per_line(1)
        flow.set_margin_top(6)
        flow.set_margin_bottom(6)

        self._title_btns: list[Gtk.ToggleButton] = []
        first_title_btn: Gtk.ToggleButton | None = None

        for i, (label, desc, dark) in enumerate(_TITLE_STYLES):
            btn = Gtk.ToggleButton()
            btn.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [f"Title slide style: {label}"],
            )
            if first_title_btn is None:
                first_title_btn = btn
            else:
                btn.set_group(first_title_btn)

            btn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            btn_box.set_margin_top(10)
            btn_box.set_margin_bottom(10)
            btn_box.set_margin_start(12)
            btn_box.set_margin_end(12)

            # Mini slide preview for this style
            mini = self._build_title_mini(i)
            btn_box.append(mini)

            style_lbl = Gtk.Label(label=label)
            style_lbl.set_xalign(0.0)
            style_lbl.add_css_class("body")

            desc_lbl = Gtk.Label(label=desc)
            desc_lbl.set_xalign(0.0)
            desc_lbl.set_wrap(True)
            desc_lbl.add_css_class("caption")
            desc_lbl.add_css_class("dim-label")

            btn_box.append(style_lbl)
            btn_box.append(desc_lbl)
            btn.set_child(btn_box)
            btn.connect("toggled", self._on_title_style_toggled, i)
            self._title_btns.append(btn)

            child = Gtk.FlowBoxChild()
            child.set_child(btn)
            flow.append(child)

        self._title_btns[self._title_style].set_active(True)

        flow_row = Adw.ActionRow()
        flow_row.set_activatable(False)
        flow_row.set_child(flow)
        group.add(flow_row)
        box.append(group)

        # Hint about the advanced editor for the custom path
        hint_group = Adw.PreferencesGroup()
        hint_row = Adw.ActionRow(
            title="Want more control?",
            subtitle='Use "Skip to advanced editor" to set title slide colours independently.',
        )
        hint_row.set_activatable(False)
        hint_group.add(hint_row)
        box.append(hint_group)

        return scroll

    def _build_title_mini(self, style_idx: int) -> Gtk.Frame:
        """Build a tiny illustrative slide thumbnail for the title style card."""
        frame = Gtk.Frame()
        frame.set_size_request(180, 90)
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        frame.update_property([Gtk.AccessibleProperty.LABEL], ["Slide preview"])

        drawing = Gtk.DrawingArea()
        drawing.set_size_request(180, 90)
        drawing.set_draw_func(self._draw_title_mini, style_idx)
        frame.set_child(drawing)
        return frame

    def _draw_title_mini(self, area, cr, w, h, style_idx) -> None:
        """Cairo draw function for the mini title slide previews."""
        import math

        t = self._theme
        _, _, dark = _TITLE_STYLES[style_idx]

        if dark is True:
            bg_hex = t.title_bg or "#1a1a2e"
            fg_hex = t.title_fg or "#ffffff"
            ac_hex = t.accent
        elif dark is False:
            bg_hex = t.bg
            fg_hex = t.fg
            ac_hex = t.accent
        else:
            # Custom — show a split for illustration
            bg_hex = t.title_bg or "#1a1a2e"
            fg_hex = t.title_fg or "#ffffff"
            ac_hex = t.accent

        def _parse(h6):
            h6 = h6.lstrip("#")
            if len(h6) == 3:
                h6 = "".join(c*2 for c in h6)
            try:
                return tuple(int(h6[i:i+2], 16)/255 for i in (0, 2, 4))
            except Exception:
                return (0.5, 0.5, 0.5)

        bg = _parse(bg_hex)
        fg = _parse(fg_hex)
        ac = _parse(ac_hex)

        # Background
        cr.set_source_rgb(*bg)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        # Accent bottom bar
        cr.set_source_rgb(*ac)
        cr.rectangle(0, h - 4, w, 4)
        cr.fill()

        # Title text block (approximate)
        cr.set_source_rgb(*ac)
        cr.rectangle(20, 28, 100, 10)
        cr.fill()

        cr.set_source_rgba(*fg, 0.6)
        cr.rectangle(20, 44, 60, 6)
        cr.fill()

    def _on_title_style_toggled(self, btn: Gtk.ToggleButton, idx: int) -> None:
        if not btn.get_active():
            return
        self._title_style = idx
        _, _, dark = _TITLE_STYLES[idx]
        if dark is True:
            self._theme = dataclasses.replace(
                self._theme,
                title_bg=_PRESETS[self._preset_idx][4],
                title_fg=_PRESETS[self._preset_idx][5],
            )
        elif dark is False:
            self._theme = dataclasses.replace(
                self._theme,
                title_bg=self._theme.bg,
                title_fg=self._theme.fg,
            )
        # Custom: leave as-is — user will edit in advanced editor
        self._schedule_preview()

    # ── Preview strip ─────────────────────────────────────────────────────────

    def _build_preview_strip(self) -> Gtk.Box:
        """
        Persistent two-thumbnail preview at the bottom of the right panel.
        A Gtk.DropDown lets the user switch between content/title/callout.
        """
        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        strip.set_margin_start(16)
        strip.set_margin_end(16)
        strip.set_margin_top(10)
        strip.set_margin_bottom(10)

        lbl = Gtk.Label(label="Preview")
        lbl.add_css_class("dim-label")
        lbl.add_css_class("caption")
        strip.append(lbl)

        # Slide type selector
        self._preview_model = Gtk.StringList.new(
            ["Content slide", "Title slide", "Callout slide"]
        )
        self._preview_slide_drop = Gtk.DropDown(model=self._preview_model)
        self._preview_slide_drop.set_selected(1)  # default: title slide
        self._preview_slide_drop.connect("notify::selected", self._schedule_preview)
        strip.append(self._preview_slide_drop)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        strip.append(spacer)

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

            self._webview.set_size_request(320, 112)
            self._webview.set_vexpand(False)
            strip.append(self._webview)
            self._has_webkit = True
        else:
            placeholder = Gtk.Label(label="Install WebKitGTK for preview")
            placeholder.add_css_class("dim-label")
            placeholder.add_css_class("caption")
            strip.append(placeholder)
            self._has_webkit = False

        return strip

    # ── Slug validation ───────────────────────────────────────────────────────

    def _validate_slug(self, slug: str) -> bool:
        def _set_error(msg: str) -> bool:
            self._name_row.add_css_class("error")
            self._name_row.set_tooltip_text(msg)
            if not self._slug_error_icon:
                icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                icon.add_css_class("error")
                icon.set_tooltip_text(msg)
                icon.update_property(
                    [Gtk.AccessibleProperty.LABEL], [f"Error: {msg}"]
                )
                self._name_row.add_suffix(icon)
                self._slug_error_icon = icon
            else:
                self._slug_error_icon.set_tooltip_text(msg)
                self._slug_error_icon.set_visible(True)
            return False

        def _clear_error() -> bool:
            self._name_row.remove_css_class("error")
            self._name_row.set_tooltip_text("")
            if self._slug_error_icon:
                self._slug_error_icon.set_visible(False)
            return True

        if not slug:
            return _set_error("Theme name cannot be empty")
        if not _SLUG_RE.match(slug):
            return _set_error(
                "Theme name produces an invalid identifier — "
                "use letters, numbers, or hyphens"
            )
        editing_slug = self._editing.slug if self._editing else None
        if slug in load_all_themes() and slug != editing_slug:
            return _set_error(
                f"A theme named '{slug}' is already installed"
            )
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
                    "<div class='callout-tip'><p>Tip — useful hints go here</p></div>"
                    "<div class='callout-info'><p>Info — neutral information</p></div>"
                    "<div class='callout-warning'><p>Warning — take care</p></div>"
                    "<div class='callout-danger'><p>Danger — critical issue</p></div>"
                    "</div>"
                )
            else:
                from md_to_slides.renderer import render_slide_content
                rendered = render_slide_content(_PREVIEW_MD)
                pg_num = (
                    "<div class='slide-number'>1 / 4</div>"
                    "<div class='progress-bar-track'>"
                    "<div class='progress-bar-fill' style='width:25%'></div>"
                    "</div>"
                )
                body = f"<div class='slide'>{rendered}{pg_num}</div>"

            # Scale 1280×720 to fit ~320 px wide preview strip
            SCALE = 0.25
            scaled_w = int(1280 * SCALE)   # 320 px
            scaled_h = int(720  * SCALE)   # 180 px
            scale_css = (
                "<style>"
                "html,body{margin:0;padding:0;background:#111;"
                "display:flex;align-items:flex-start;justify-content:center;}"
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
        data["code_bg"]        = t.code_bg
        data["title_bg"]       = t.title_bg
        data["title_fg"]       = t.title_fg
        if t.title_accent:
            data["title_accent"] = t.title_accent
        data["body_font"]      = t.body_font
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
        if not self._validate_slug(slug):
            return
        if not self._theme.name.strip():
            self._show_error("Please enter a theme name.")
            return
        try:
            with tempfile.TemporaryDirectory() as tmp_root:
                theme_dir = Path(tmp_root) / slug
                self._write_to_dir(theme_dir)
                installed = install_theme(theme_dir)

            if self._on_installed:
                self._on_installed(installed)

            toast = Adw.Toast(title=f"Theme '{installed.name}' installed")
            toast.set_timeout(3)
            if hasattr(self._parent_window, "_toast_overlay"):
                self._parent_window._toast_overlay.add_toast(toast)
            self.close()

        except (ValueError, OSError) as exc:
            self._show_error(str(exc))

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
        dialog.save(self, None, self._on_export_zip_chosen)

    def _on_export_zip_chosen(self, dialog, result) -> None:
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        zip_path = Path(gfile.get_path())
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
            toast = Adw.Toast(title=f"Exported to {zip_path.name}")
            toast.set_timeout(4)
            if hasattr(self._parent_window, "_toast_overlay"):
                self._parent_window._toast_overlay.add_toast(toast)
            return GLib.SOURCE_REMOVE

        def _err(msg):
            self._show_error(msg)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=_do, daemon=True).start()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _step_scroll(self) -> tuple[Gtk.ScrolledWindow, Gtk.Box]:
        """Return a (ScrolledWindow, inner Box) pair for a wizard step page."""
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        scroll.set_child(box)
        return scroll, box

    def _on_destroy(self, *_) -> None:
        if self._preview_debounce is not None:
            GLib.source_remove(self._preview_debounce)
            self._preview_debounce = None
        if self._preview_tmp_css is not None:
            Path(self._preview_tmp_css).unlink(missing_ok=True)
            self._preview_tmp_css = None

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(heading="Error", body=message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.present(self)


# ── Advanced editor (escape hatch from the wizard) ────────────────────────────

class ThemeEditorAdvanced(Adw.Window):
    """
    Full-field editor opened via "Skip to advanced editor" in the wizard,
    or via "Edit" on an installed user theme.

    All the same backend methods as ThemeEditor; different UI layer.
    Accepts a pre-populated theme + custom_css string so the wizard's
    progress is not lost when the user switches modes.
    """

    def __init__(
        self,
        parent_window,
        existing_theme: Theme | None = None,
        custom_css:     str          = "",
        font_files:     list[Path]   | None = None,
        on_installed:   Callable[[Theme], None] | None = None,
        wizard:         "ThemeEditor | None" = None,
    ) -> None:
        super().__init__()

        self._parent_window  = parent_window
        self._on_installed   = on_installed
        self._editing        = existing_theme
        self._wizard         = wizard       # wizard to return to, if any
        self._is_builtin     = (
            existing_theme is not None
            and existing_theme.slug in BUILTIN_THEMES
        )

        self._theme             = (
            dataclasses.replace(existing_theme)
            if existing_theme else Theme(name="New Theme", slug="new-theme", author="")
        )
        self._initial_name_slug = _auto_slug(self._theme.name)
        self._custom_css        = custom_css or self._load_custom_css(existing_theme)
        self._font_files        = list(font_files or [])

        self._preview_debounce: int | None = None
        self._preview_tmp_css:  str | None = None
        self._slug_error_icon              = None
        self._fonts_group_ref: Adw.PreferencesGroup | None = None
        self._font_rows: dict[Path, Adw.ActionRow] = {}

        self.set_transient_for(parent_window)
        self.set_modal(True)
        self.set_default_size(1100, 720)
        self.set_title(
            f"Edit theme — {existing_theme.name}" if existing_theme
            else "Advanced theme editor"
        )
        self._build_ui()
        self.connect("destroy", self._on_destroy)
        GLib.idle_add(self._schedule_preview)

    @staticmethod
    def _load_custom_css(existing: Theme | None) -> str:
        if not existing or not existing.custom_css_path:
            return ""
        p = Path(existing.custom_css_path)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)
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

        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_size_request(480, -1)
        left_scroll.set_hexpand(True)
        left_scroll.set_vexpand(True)
        left_page = self._build_left_panel()
        if self._is_builtin:
            left_page.set_sensitive(False)
        left_scroll.set_child(left_page)
        outer.append(left_scroll)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        right = self._build_right_panel()
        right.set_size_request(420, -1)
        outer.append(right)

        toolbar_view.set_content(outer)

    def _build_header(self) -> Adw.HeaderBar:
        bar = Adw.HeaderBar()

        if self._wizard is not None:
            # Came from the wizard — offer a way back
            back_btn = Gtk.Button(label="← Wizard")
            back_btn.set_tooltip_text("Return to the step-by-step wizard")
            back_btn.connect("clicked", self._on_back_to_wizard)
            bar.pack_start(back_btn)
        else:
            cancel_btn = Gtk.Button(label="Cancel")
            cancel_btn.connect("clicked", lambda *_: self.close())
            bar.pack_start(cancel_btn)

        if not self._is_builtin:
            install_label = "Save" if self._editing else "Install"
            self._install_btn = Gtk.Button(label=install_label)
            self._install_btn.add_css_class("suggested-action")
            self._install_btn.connect("clicked", self._on_install)
            bar.pack_end(self._install_btn)

            export_btn = Gtk.Button(label="Export as zip…")
            export_btn.connect("clicked", self._on_export_zip)
            bar.pack_end(export_btn)

        return bar

    def _on_back_to_wizard(self, *_) -> None:
        """Return to the wizard, carrying any edits made here back."""
        if self._wizard is not None:
            # Sync the wizard's theme state with any changes made in this editor
            self._wizard._theme      = dataclasses.replace(self._theme)
            self._wizard._custom_css = self._custom_css
            self._wizard._font_files = list(self._font_files)
            self._wizard.set_visible(True)
            self._wizard.present()
        self.close()

    def _build_left_panel(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()

        # Identity
        identity_group = Adw.PreferencesGroup(title="Identity")
        page.add(identity_group)

        self._name_row   = Adw.EntryRow(title="Name",        text=self._theme.name)
        self._slug_row   = Adw.EntryRow(title="Slug",        text=self._theme.slug)
        self._author_row = Adw.EntryRow(title="Author",      text=self._theme.author)
        self._desc_row   = Adw.EntryRow(title="Description", text=self._theme.description)

        self._name_row.connect("changed",   self._on_name_changed)
        self._slug_row.connect("changed",   self._on_slug_changed)
        self._author_row.connect("changed", self._on_meta_changed)
        self._desc_row.connect("changed",   self._on_meta_changed)

        identity_group.add(self._name_row)
        identity_group.add(self._slug_row)
        identity_group.add(self._author_row)
        identity_group.add(self._desc_row)

        # Colour groups
        _COLOUR_FIELDS = [
            ("bg",            "Background",      "Palette",    False),
            ("fg",            "Text",            None,         False),
            ("accent",        "Accent",          None,         False),
            ("accent2",       "Accent 2",        None,         True),
            ("heading_color", "Heading",         None,         True),
            ("code_bg",       "Code background", None,         False),
            ("title_bg",      "Background",      "Title slide",False),
            ("title_fg",      "Text",            None,         False),
            ("title_accent",  "Accent",          None,         True),
        ]
        current_group = None
        for field_key, label, group_label, nullable in _COLOUR_FIELDS:
            if group_label is not None:
                current_group = Adw.PreferencesGroup(title=group_label)
                page.add(current_group)
            initial = getattr(self._theme, field_key, "") or ""
            row = _ColourRow(
                label, initial,
                callback=lambda v, k=field_key: self._on_colour_changed(k, v),
                nullable=nullable,
            )
            current_group.add(row)

        # Callouts
        callout_group = Adw.PreferencesGroup(
            title="Callout boxes",
            description="Each callout has a background, text, and border colour",
        )
        page.add(callout_group)
        for kind in _CALLOUT_KINDS:
            triple = getattr(self._theme, f"callout_{kind}")
            row = _CalloutRow(
                kind.capitalize(), triple,
                callback=lambda t, k=kind: self._on_callout_changed(k, t),
            )
            callout_group.add(row)

        # Callout icons
        icons_group = Adw.PreferencesGroup(
            title="Callout icons",
            description="Single character shown in the badge",
        )
        page.add(icons_group)
        self._icon_rows: dict[str, Adw.EntryRow] = {}
        for kind in _CALLOUT_KINDS:
            val = getattr(self._theme, f"callout_icon_{kind}")
            r = Adw.EntryRow(title=kind.capitalize(), text=val)
            r.connect("changed", lambda row, k=kind: self._on_icon_changed(k, row))
            icons_group.add(r)
            self._icon_rows[kind] = r

        # Typography
        typo_group = Adw.PreferencesGroup(title="Typography")
        page.add(typo_group)

        for field, title, placeholder in (
            ("body_font",    "Body font",    ""),
            ("heading_font", "Heading font", "Same as body font"),
            ("mono_font",    "Mono font",    ""),
        ):
            row = self._font_entry_row(title, getattr(self._theme, field), field, placeholder)
            typo_group.add(row)

        size_row = Adw.ActionRow(title="Base font size")
        size_spin = Gtk.SpinButton.new_with_range(20, 60, 1)
        size_spin.set_value(self._theme.base_size)
        size_spin.set_valign(Gtk.Align.CENTER)
        size_spin.update_property([Gtk.AccessibleProperty.LABEL], ["Base font size"])
        size_spin.connect("value-changed",
                          lambda s: self._on_simple_change("base_size", int(s.get_value())))
        size_row.add_suffix(size_spin)
        size_row.set_activatable_widget(size_spin)
        typo_group.add(size_row)

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
        pgy_drop.connect("notify::selected",
                         lambda d, _: self._on_simple_change(
                             "pygments_style", styles[d.get_selected()]))
        pgy_row.add_suffix(pgy_drop)
        typo_group.add(pgy_row)

        # Font bundling
        fonts_group = Adw.PreferencesGroup(
            title="Bundled fonts",
            description="Font files copied into the theme package",
        )
        page.add(fonts_group)
        self._fonts_group_ref = fonts_group
        add_font_row = Adw.ActionRow(
            title="Add font file",
            subtitle="woff2, ttf, otf  ·  name as FamilyName-WeightStyle.woff2",
        )
        add_btn = Gtk.Button(label="Browse…")
        add_btn.add_css_class("flat")
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.connect("clicked", self._on_add_font)
        add_font_row.add_suffix(add_btn)
        add_font_row.set_activatable_widget(add_btn)
        fonts_group.add(add_font_row)

        if self._editing and self._editing.custom_css_path:
            theme_dir = Path(self._editing.custom_css_path).parent
            fonts_dir = theme_dir / "fonts"
            if fonts_dir.is_dir():
                for f in sorted(fonts_dir.iterdir()):
                    if f.suffix.lower() in (".woff2", ".woff", ".ttf", ".otf"):
                        self._add_font_row(f)

        # Custom CSS
        css_group = Adw.PreferencesGroup(
            title="Custom CSS",
            description="Appended after the generated stylesheet",
        )
        page.add(css_group)
        css_frame = Gtk.Frame()
        css_frame.set_margin_top(4)
        css_frame.set_margin_bottom(4)
        css_frame.set_overflow(Gtk.Overflow.HIDDEN)
        css_editor = self._build_css_editor()
        css_editor.set_size_request(-1, 200)
        css_frame.set_child(css_editor)
        css_frame_row = Adw.ActionRow()
        css_frame_row.set_activatable(False)
        css_frame_row.set_child(css_frame)
        css_group.add(css_frame_row)

        return page

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

    def _build_right_panel(self) -> Gtk.Box:
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
        panel.append(self._build_validation_strip())
        return panel

    def _build_validation_strip(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.update_property([Gtk.AccessibleProperty.LABEL], ["Contrast check results"])

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

    def _build_css_editor(self) -> Gtk.Widget:
        if _GTKSOURCE:
            lang_mgr = GtkSource.LanguageManager.get_default()
            lang     = lang_mgr.get_language("css")
            buf      = GtkSource.Buffer()
            if lang:
                buf.set_language(lang)
            buf.set_highlight_syntax(True)
            buf.set_text(self._custom_css)
            buf.connect("changed", self._on_css_changed)
            view = GtkSource.View.new_with_buffer(buf)
            view.set_monospace(True)
            view.set_show_line_numbers(True)
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            view.set_tab_width(2)
            view.set_insert_spaces_instead_of_tabs(True)
            view.set_left_margin(8)
            view.set_right_margin(8)
            view.set_top_margin(8)
            view.set_bottom_margin(8)
            scroll = Gtk.ScrolledWindow()
            scroll.set_child(view)
            scroll.set_vexpand(False)
            self._css_buffer = buf
            return scroll
        else:
            buf = Gtk.TextBuffer()
            buf.set_text(self._custom_css)
            buf.connect("changed", self._on_css_changed)
            view = Gtk.TextView.new_with_buffer(buf)
            view.set_monospace(True)
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            scroll = Gtk.ScrolledWindow()
            scroll.set_child(view)
            scroll.set_vexpand(False)
            self._css_buffer = buf
            return scroll

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _on_name_changed(self, row: Adw.EntryRow) -> None:
        name = row.get_text()
        if self._theme.slug in ("", "theme", self._initial_name_slug):
            slug = _auto_slug(name)
            self._slug_row.set_text(slug)
            self._theme = dataclasses.replace(self._theme, name=name, slug=slug)
        else:
            self._theme = dataclasses.replace(self._theme, name=name)
        self._validate_slug(self._theme.slug)
        self._schedule_preview()

    def _on_slug_changed(self, row: Adw.EntryRow) -> None:
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

    def _on_callout_changed(self, kind: str, triple: tuple) -> None:
        self._theme = dataclasses.replace(self._theme, **{f"callout_{kind}": triple})
        self._schedule_preview()

    def _on_icon_changed(self, kind: str, row: Adw.EntryRow) -> None:
        icon = row.get_text().replace("'", "")[:4]
        self._theme = dataclasses.replace(self._theme, **{f"callout_icon_{kind}": icon})
        self._schedule_preview()

    def _on_simple_change(self, field: str, value) -> None:
        self._theme = dataclasses.replace(self._theme, **{field: value})
        self._schedule_preview()

    def _on_css_changed(self, buf) -> None:
        start = buf.get_start_iter()
        end   = buf.get_end_iter()
        self._custom_css = buf.get_text(start, end, True)
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
        dialog.open(self, None, self._on_font_chosen)

    def _on_font_chosen(self, dialog, result) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        self._add_font_row(Path(gfile.get_path()))

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
        self._fonts_group_ref.add(row)
        self._font_rows[path] = row

    def _remove_font(self, path: Path, row: Adw.ActionRow) -> None:
        if path in self._font_files:
            self._font_files.remove(path)
        self._fonts_group_ref.remove(row)
        self._font_rows.pop(path, None)

    def _on_fork(self, *_) -> None:
        forked = dataclasses.replace(
            self._theme,
            name=f"{self._theme.name} (copy)",
            slug=f"{self._theme.slug}-copy",
            author="",
        )
        editor = ThemeEditorAdvanced(
            self._parent_window,
            existing_theme=forked,
            on_installed=self._on_installed,
        )
        editor.present()
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
            return _set_error("Slug cannot be empty")
        if not _SLUG_RE.match(slug):
            return _set_error(
                "Only lowercase letters, digits, hyphens, and underscores"
            )
        editing_slug = self._editing.slug if self._editing else None
        if slug in load_all_themes() and slug != editing_slug:
            return _set_error(f"'{slug}' is already used by an installed theme")
        return _clear_error()

    # ── Contrast ──────────────────────────────────────────────────────────────

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
                if ratio >= 7:
                    level, icon = "AAA", "✓"
                    lbl.add_css_class("success")
                elif ratio >= 4.5:
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

    # ── Preview ───────────────────────────────────────────────────────────────

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
                from md_to_slides.renderer import render_slide_content
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
                "html,body{margin:0;padding:0;background:#111;"
                "display:flex;align-items:flex-start;justify-content:center;}"
                f".slide-wrapper{{width:{scaled_w}px;height:{scaled_h}px;"
                f"overflow:hidden;flex-shrink:0;margin:12px auto;}}"
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

    # ── Export / install (same as wizard) ─────────────────────────────────────

    def _collect_theme_json(self) -> dict:
        t = self._theme
        data: dict = {
            "name": t.name, "slug": t.slug, "author": t.author,
            "version": t.version, "description": t.description,
            "bg": t.bg, "fg": t.fg, "accent": t.accent,
        }
        if t.accent2:         data["accent2"]       = t.accent2
        if t.heading_color:   data["heading_color"]  = t.heading_color
        data["code_bg"]   = t.code_bg
        data["title_bg"]  = t.title_bg
        data["title_fg"]  = t.title_fg
        if t.title_accent:    data["title_accent"]   = t.title_accent
        data["body_font"] = t.body_font
        if t.heading_font:    data["heading_font"]   = t.heading_font
        data["mono_font"] = t.mono_font
        data["base_size"] = t.base_size
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
        if not self._validate_slug(slug):
            return
        if not self._theme.name.strip():
            self._show_error("Please enter a theme name.")
            return
        try:
            with tempfile.TemporaryDirectory() as tmp_root:
                theme_dir = Path(tmp_root) / slug
                self._write_to_dir(theme_dir)
                installed = install_theme(theme_dir)
            if self._on_installed:
                self._on_installed(installed)
            toast = Adw.Toast(title=f"Theme '{installed.name}' installed")
            toast.set_timeout(3)
            if hasattr(self._parent_window, "_toast_overlay"):
                self._parent_window._toast_overlay.add_toast(toast)
            self.close()
        except (ValueError, OSError) as exc:
            self._show_error(str(exc))

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
        dialog.save(self, None, self._on_export_zip_chosen)

    def _on_export_zip_chosen(self, dialog, result) -> None:
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        zip_path = Path(gfile.get_path())
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
            toast = Adw.Toast(title=f"Exported to {zip_path.name}")
            toast.set_timeout(4)
            if hasattr(self._parent_window, "_toast_overlay"):
                self._parent_window._toast_overlay.add_toast(toast)
            return GLib.SOURCE_REMOVE

        def _err(msg):
            self._show_error(msg)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=_do, daemon=True).start()

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(heading="Error", body=message)
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
        # If the user closes this window without returning to the wizard,
        # close the hidden wizard too so it doesn't linger invisibly.
        if self._wizard is not None and not self._wizard.get_visible():
            self._wizard.close()


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


# ── Callout triple row ────────────────────────────────────────────────────────

class _CalloutRow(Adw.ExpanderRow):
    def __init__(
        self, title: str, triple: tuple, callback: Callable[[tuple], None]
    ) -> None:
        super().__init__(title=title)
        self._callback = callback
        self._triple   = list(triple)
        for i, (label, initial) in enumerate(
            zip(("Background", "Text", "Border"), triple)
        ):
            row = _ColourRow(
                label, initial,
                callback=lambda v, idx=i: self._on_changed(idx, v),
            )
            self.add_row(row)

    def _on_changed(self, idx: int, value: str) -> None:
        self._triple[idx] = value
        self._callback(tuple(self._triple))


# ── Colour utilities ──────────────────────────────────────────────────────────

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
