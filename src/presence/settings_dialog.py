"""
settings_dialog.py — Application preferences dialog.

Contains SettingsDialog (Adw.PreferencesDialog with three pages: Slides,
Editor, Manage Themes) and its supporting _ThemeCard widget.

Kept separate from window.py to honour single-responsibility and make each
class independently testable.
"""
from __future__ import annotations

import logging
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, GdkPixbuf, Pango

from .session import (load_editor_prefs, save_editor_prefs,
                      load_presentation_prefs, save_presentation_prefs,
                      persist_logo)
from .app_utils import png_bytes_to_texture, make_file_filter, make_filter_store
from .slides.themes import ASPECT_RATIOS

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .window import MainWindow

log = logging.getLogger(__name__)

# ── Settings dialog ───────────────────────────────────────────────────────────

class SettingsDialog(Adw.PreferencesDialog):
    """
    Three-page Preferences dialog.

    Slides        — theme grid, aspect ratio, logo
    Editor        — font size, GtkSource toggles
    Manage themes — install / uninstall / open folder
    """

    def __init__(self, parent: MainWindow) -> None:
        super().__init__()
        self._parent = parent
        # Strong reference to any active Gtk.FileDialog — prevents GC collection
        # before the async open/save callback fires. Cleared in each callback.
        self._active_file_dialog = None
        # Flag list used to cancel in-flight ThumbCache renders (#3)
        self._render_cancelled: list[bool] = [False]

        from .slides.theme_loader import load_all_themes
        from .slides.themes import ASPECT_RATIOS
        from .theme_manager_ui import build_themes_page

        all_themes = load_all_themes()
        prefs = load_editor_prefs()

        # ── Page 1: Slides ────────────────────────────────────────────────────
        slides_page = Adw.PreferencesPage()
        slides_page.set_title("Slides")
        slides_page.set_icon_name("view-paged-symbolic")
        self.add(slides_page)

        # Theme group — visual grid of swatches (replaces ComboRow + separate
        # Preview row; merges the old Conversion tab and Themes tab selection)
        theme_group = Adw.PreferencesGroup(title="Theme")
        theme_group.set_description(
            "Colour scheme applied to every generated slide"
        )
        slides_page.add(theme_group)

        # FlowBox holds the theme cards so they wrap on narrow dialogs
        self._theme_flow = Gtk.FlowBox()
        self._theme_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._theme_flow.set_homogeneous(True)
        self._theme_flow.set_column_spacing(8)
        self._theme_flow.set_row_spacing(8)
        self._theme_flow.set_margin_top(4)
        self._theme_flow.set_margin_bottom(4)
        self._theme_flow.set_max_children_per_line(4)
        self._theme_flow.set_min_children_per_line(2)

        # Wrap FlowBox in an ActionRow so it sits inside the group card
        theme_flow_row = Adw.ActionRow()
        theme_flow_row.set_activatable(False)
        theme_flow_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        theme_flow_box.set_margin_top(8)
        theme_flow_box.set_margin_bottom(8)
        theme_flow_box.append(self._theme_flow)
        theme_flow_row.set_child(theme_flow_box)
        theme_group.add(theme_flow_row)

        # Build the cards — track them so refresh_theme_grid() can update
        self._theme_cards: dict[str, "_ThemeCard"] = {}
        self._first_card: "_ThemeCard | None" = None
        self._build_theme_grid(all_themes, parent._converter.theme,
                               parent._converter.ratio)

        # Layout group
        layout_group = Adw.PreferencesGroup(title="Layout")
        layout_group.set_description("Dimensions of each generated slide")
        slides_page.add(layout_group)

        ratios = list(ASPECT_RATIOS.keys())
        self._ratio_row = Adw.ComboRow(title="Aspect ratio")
        self._ratio_row.set_subtitle("16:9 suits most projectors and screens")
        self._ratio_row.set_model(Gtk.StringList.new(ratios))
        if parent._converter.ratio in ratios:
            self._ratio_row.set_selected(ratios.index(parent._converter.ratio))
        self._ratio_row.connect("notify::selected", self._on_ratio_changed, ratios)
        layout_group.add(self._ratio_row)

        # Logo group
        logo_group = Adw.PreferencesGroup(title="Logo")
        logo_group.set_description(
            "Appears bottom-left on every slide — PNG, JPEG or SVG"
        )
        slides_page.add(logo_group)

        current_logo = str(parent._converter.logo_path or "")
        logo_subtitle = (
            Path(current_logo).name if current_logo else "No logo selected"
        )
        self._logo_row = Adw.ActionRow(
            title="Logo image",
            subtitle=logo_subtitle,
        )
        choose_btn = Gtk.Button(label="Choose…")
        choose_btn.add_css_class("flat")
        choose_btn.set_valign(Gtk.Align.CENTER)
        choose_btn.connect("clicked", self._on_choose_logo)
        self._logo_row.add_suffix(choose_btn)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.add_css_class("flat")
        clear_btn.set_valign(Gtk.Align.CENTER)
        clear_btn.connect("clicked", self._on_clear_logo)
        self._logo_row.add_suffix(clear_btn)

        logo_group.add(self._logo_row)

        # Presentation group — timer target
        pres_group = Adw.PreferencesGroup(title="Presentation")
        pres_group.set_description(
            "Settings applied during presenter mode"
        )
        slides_page.add(pres_group)

        pres_prefs = load_presentation_prefs()

        # Target duration — 0 means no target (count-up only)
        try:
            timer_row = Adw.SpinRow.new(
                Gtk.Adjustment(
                    value=float(pres_prefs.get("timer_minutes", 0)),
                    lower=0, upper=180, step_increment=1, page_increment=5,
                ),
                climb_rate=1, digits=0,
            )
            timer_row.set_title("Target duration")
            timer_row.set_subtitle(
                "Minutes — presenter timer counts down from this target (0 = count up)"
            )
            timer_row.connect("notify::value", self._on_timer_target_changed)
            self._timer_spin_row = timer_row
            self._timer_spin_btn = None
            pres_group.add(timer_row)
        except AttributeError:
            timer_row = Adw.ActionRow(
                title="Target duration",
                subtitle="Minutes for this presentation (0 = count up only)",
            )
            adj = Gtk.Adjustment(
                value=float(pres_prefs.get("timer_minutes", 0)),
                lower=0, upper=180, step_increment=1, page_increment=5,
            )
            spin = Gtk.SpinButton(adjustment=adj, climb_rate=1, digits=0)
            spin.set_valign(Gtk.Align.CENTER)
            spin.connect("value-changed", self._on_timer_target_changed_spin)
            timer_row.add_suffix(spin)
            self._timer_spin_row = None
            self._timer_spin_btn = spin
            pres_group.add(timer_row)

        # Speaking rate SpinRow
        try:
            rate_row = Adw.SpinRow.new(
                Gtk.Adjustment(
                    value=float(pres_prefs.get('speaking_rate', 110)),
                    lower=60, upper=250, step_increment=5, page_increment=20,
                ),
                climb_rate=1, digits=0,
            )
            rate_row.set_title('Speaking rate')
            rate_row.set_subtitle('Words per minute used to estimate presentation time')
            rate_row.connect('notify::value', self._on_speaking_rate_changed)
            self._rate_spin_row = rate_row
            self._rate_spin_btn = None
            pres_group.add(rate_row)
        except AttributeError:
            rate_row = Adw.ActionRow(
                title='Speaking rate',
                subtitle='Words per minute',
            )
            adj = Gtk.Adjustment(
                value=float(pres_prefs.get('speaking_rate', 110)),
                lower=60, upper=250, step_increment=5, page_increment=20,
            )
            spin = Gtk.SpinButton(adjustment=adj, climb_rate=1, digits=0)
            spin.set_valign(Gtk.Align.CENTER)
            spin.connect('value-changed', self._on_speaking_rate_changed_spin)
            rate_row.add_suffix(spin)
            self._rate_spin_row = None
            self._rate_spin_btn = spin
            pres_group.add(rate_row)

        # ── Page 2: Editor ────────────────────────────────────────────────────
        editor_page = Adw.PreferencesPage()
        editor_page.set_title("Editor")
        editor_page.set_icon_name("text-editor-symbolic")
        self.add(editor_page)

        # Appearance group
        appearance_group = Adw.PreferencesGroup(title="Appearance")
        appearance_group.set_description(
            "Visual style of the editor — does not affect generated slides"
        )
        editor_page.add(appearance_group)

        # Font size — Adw.SpinRow (libadwaita ≥ 1.4) with fallback to ActionRow
        try:
            font_row = Adw.SpinRow.new(
                Gtk.Adjustment(
                    value=parent._editor.get_font_size(),
                    lower=8, upper=32, step_increment=1, page_increment=2,
                ),
                climb_rate=1, digits=0,
            )
            font_row.set_title("Font size")
            font_row.set_subtitle("Point size, 8–32 pt")
            font_row.connect("notify::value", self._on_font_size_spin_row)
            self._font_spin_row = font_row
            self._font_spin_btn = None
            appearance_group.add(font_row)
        except AttributeError:
            # Fallback for older libadwaita: bare ActionRow + SpinButton
            font_row = Adw.ActionRow(
                title="Font size",
                subtitle="Point size, 8–32 pt",
            )
            adj = Gtk.Adjustment(
                value=parent._editor.get_font_size(),
                lower=8, upper=32, step_increment=1, page_increment=2,
            )
            spin = Gtk.SpinButton(adjustment=adj, climb_rate=1, digits=0)
            spin.set_valign(Gtk.Align.CENTER)
            spin.connect("value-changed", self._on_font_size_changed)
            font_row.add_suffix(spin)
            self._font_spin_row = None
            self._font_spin_btn = spin
            appearance_group.add(font_row)

        # Line length guide
        try:
            ll_row = Adw.SpinRow.new(
                Gtk.Adjustment(
                    value=float(prefs.get('line_length', 64)),
                    lower=0, upper=120, step_increment=1, page_increment=10,
                ),
                climb_rate=1, digits=0,
            )
            ll_row.set_title('Line length guide')
            ll_row.set_subtitle('Column ruler in editor — 0 to hide')
            ll_row.connect('notify::value', self._on_line_length_spin_row)
            self._ll_spin_row = ll_row
            self._ll_spin_btn = None
            appearance_group.add(ll_row)
        except AttributeError:
            ll_row = Adw.ActionRow(title='Line length guide',
                                   subtitle='Column ruler — 0 to hide')
            adj = Gtk.Adjustment(value=float(prefs.get('line_length', 64)),
                                  lower=0, upper=120,
                                  step_increment=1, page_increment=10)
            spin = Gtk.SpinButton(adjustment=adj, climb_rate=1, digits=0)
            spin.set_valign(Gtk.Align.CENTER)
            spin.connect('value-changed', self._on_line_length_changed)
            ll_row.add_suffix(spin)
            self._ll_spin_row = None
            self._ll_spin_btn = spin
            appearance_group.add(ll_row)

        # Behaviour group
        behaviour_group = Adw.PreferencesGroup(title="Behaviour")
        behaviour_group.set_description(
            "How the editor responds while you write"
        )
        editor_page.add(behaviour_group)

        self._switch_rows: dict[str, Adw.SwitchRow] = {}
        editor_toggles = [
            ("syntax_highlight", "Syntax highlighting",
             "Colour-code Markdown as you type",
             prefs.get("syntax_highlight", True)),
            ("line_numbers", "Line numbers",
             "Show line numbers in the left margin",
             prefs.get("line_numbers", True)),
            ("highlight_line", "Highlight current line",
             "Subtle background on the line containing the cursor",
             prefs.get("highlight_line", True)),
            ("auto_indent", "Auto-indent",
             "Match indentation of the previous line on Enter",
             prefs.get("auto_indent", True)),
            ("spaces_tabs", "Spaces instead of tabs",
             "Insert spaces when Tab is pressed",
             prefs.get("spaces_tabs", True)),
        ]
        for key, title, subtitle, initial in editor_toggles:
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.set_active(initial)
            row.connect("notify::active", self._on_editor_toggle, key)
            self._switch_rows[key] = row
            behaviour_group.add(row)

        # Auto-convert on save (not in editor_toggles because it writes to
        # pres_prefs rather than editor_prefs and updates the window directly)
        auto_row = Adw.SwitchRow(
            title="Convert on save",
            subtitle="Rebuild the PDF automatically every time the file is saved",
        )
        auto_row.set_active(pres_prefs.get("auto_convert", False))
        auto_row.connect("notify::active", self._on_auto_convert_toggled)
        behaviour_group.add(auto_row)

        # ── Page 3: Manage themes ─────────────────────────────────────────────
        themes_page = build_themes_page(
            parent, parent._converter, settings_dialog=self
        )
        self.add(themes_page)

        self.connect("closed", self._on_closed)

    # ── Theme grid ────────────────────────────────────────────────────────────

    def _build_theme_grid(self, all_themes: dict, current_slug: str,
                          ratio: str) -> None:
        """Populate self._theme_flow with one card per available theme."""
        from .theme_manager_ui import _thumb_cache

        # Clear existing cards
        while True:
            child = self._theme_flow.get_first_child()
            if child is None:
                break
            self._theme_flow.remove(child)
        self._theme_cards.clear()
        self._first_card = None

        for slug, theme in sorted(all_themes.items()):
            card = _ThemeCard(slug, theme.name)
            self._theme_flow.append(card)
            self._theme_cards[slug] = card

            if self._first_card is None:
                self._first_card = card
            else:
                card.set_group(self._first_card)

            if slug == current_slug:
                card.set_active(True)

            # Load thumbnail asynchronously — cancel flag prevents writes to
            # a destroyed widget if the dialog is closed mid-render (#3)
            cancelled = self._render_cancelled

            def _on_png(png_bytes, c=card, cf=cancelled):
                if cf[0]:
                    return
                if png_bytes:
                    texture = png_bytes_to_texture(png_bytes)
                    if texture is not None:
                        c.set_thumbnail(texture)

            _thumb_cache.get_async(theme, ratio, _on_png)

            # Connect toggle — only fire when card becomes active to avoid
            # double-firing (one deactivation + one activation per click)
            card.connect("toggled", self._on_theme_card_toggled, slug)

    def refresh_theme_grid(self) -> None:
        """Rebuild the theme grid after a theme is installed or uninstalled."""
        from .slides.theme_loader import load_all_themes
        # Cancel any pending thumbnail callbacks before rebuilding
        self._render_cancelled[0] = True
        self._render_cancelled = [False]
        all_themes = load_all_themes()
        self._build_theme_grid(
            all_themes,
            self._parent._converter.theme,
            self._parent._converter.ratio,
        )
        # Also refresh the persistent theme panel
        self._parent._theme_panel.refresh()

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _on_closed(self, *_) -> None:
        self._render_cancelled[0] = True

    def _on_theme_card_toggled(self, card: "Gtk.ToggleButton",
                               slug: str) -> None:
        if not card.get_active():
            return   # ignore the deactivation of the previously selected card
        self._parent._converter.theme = slug
        self._save_prefs()
        # Keep the theme panel in sync
        self._parent._theme_panel.select_theme(slug)

    def _on_ratio_changed(self, row: Adw.ComboRow, _param,
                          ratios: list) -> None:
        ratio = ratios[row.get_selected()]
        self._parent._converter.ratio = ratio
        self._save_prefs()
        self._parent._theme_panel.select_ratio(ratio)

    def _on_timer_target_changed(self, row: "Adw.SpinRow", _param) -> None:
        """SpinRow handler — update the window's timer_minutes pref."""
        minutes = int(row.get_value())
        self._parent._timer_minutes = minutes
        save_presentation_prefs({
            "timer_minutes":        minutes,
            "auto_convert":         self._parent._auto_convert,
            "presenter_notes_font": self._parent._presenter_notes_font,
            "speaking_rate":        self._parent._speaking_rate,
        })

    def _on_timer_target_changed_spin(self, spin: Gtk.SpinButton) -> None:
        """Fallback SpinButton handler."""
        minutes = int(spin.get_value())
        self._parent._timer_minutes = minutes
        save_presentation_prefs({
            "timer_minutes":        minutes,
            "auto_convert":         self._parent._auto_convert,
            "presenter_notes_font": self._parent._presenter_notes_font,
            "speaking_rate":        self._parent._speaking_rate,
        })

    def _on_auto_convert_toggled(self, row: Adw.SwitchRow, _param) -> None:
        """Toggle auto-convert-on-save."""
        enabled = row.get_active()
        self._parent._auto_convert = enabled
        save_presentation_prefs({
            "timer_minutes":        self._parent._timer_minutes,
            "auto_convert":         enabled,
            "presenter_notes_font": self._parent._presenter_notes_font,
            "speaking_rate":        self._parent._speaking_rate,
        })

    def _on_speaking_rate_changed(self, row: "Adw.SpinRow", _param) -> None:
        rate = int(row.get_value())
        self._parent._speaking_rate = rate
        # The strip quotes a per-slide time, so it has to be told too.
        self._parent._sidebar.set_speaking_rate(rate)
        save_presentation_prefs({
            "timer_minutes":        self._parent._timer_minutes,
            "auto_convert":         self._parent._auto_convert,
            "presenter_notes_font": self._parent._presenter_notes_font,
            "speaking_rate":        rate,
        })

    def _on_speaking_rate_changed_spin(self, spin: Gtk.SpinButton) -> None:
        rate = int(spin.get_value())
        self._parent._speaking_rate = rate
        # The strip quotes a per-slide time, so it has to be told too.
        self._parent._sidebar.set_speaking_rate(rate)
        save_presentation_prefs({
            "timer_minutes":        self._parent._timer_minutes,
            "auto_convert":         self._parent._auto_convert,
            "presenter_notes_font": self._parent._presenter_notes_font,
            "speaking_rate":        rate,
        })

    def _on_font_size_spin_row(self, row: "Adw.SpinRow", _param) -> None:
        """Handler for Adw.SpinRow (libadwaita ≥ 1.4)."""
        pt = int(row.get_value())
        self._parent._editor.set_font_size(pt)
        self._save_prefs()

    def _on_font_size_changed(self, spin: Gtk.SpinButton) -> None:
        """Fallback handler for plain Gtk.SpinButton."""
        pt = int(spin.get_value())
        self._parent._editor.set_font_size(pt)
        self._save_prefs()

    def _on_line_length_spin_row(self, row: "Adw.SpinRow", _param) -> None:
        cols = int(row.get_value())
        self._parent._editor.set_line_length(cols)
        self._save_prefs()

    def _on_line_length_changed(self, spin: Gtk.SpinButton) -> None:
        cols = int(spin.get_value())
        self._parent._editor.set_line_length(cols)
        self._save_prefs()

    def _on_editor_toggle(self, row: Adw.SwitchRow, _param,
                          key: str) -> None:
        enabled = row.get_active()
        editor = self._parent._editor
        {
            "syntax_highlight": editor.set_syntax_highlight,
            "line_numbers":     editor.set_line_numbers,
            "highlight_line":   editor.set_highlight_current_line,
            "auto_indent":      editor.set_auto_indent,
            "spaces_tabs":      editor.set_spaces_instead_of_tabs,
        }[key](enabled)
        self._save_prefs()

    def _on_choose_logo(self, *_) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose Logo Image")
        dialog.set_filters(make_filter_store(
            make_file_filter("Images (PNG, JPEG, SVG)",
                             "*.png", "*.jpg", "*.jpeg", "*.svg")
        ))
        self._active_file_dialog = dialog
        dialog.open(self, None, self._on_logo_chosen)

    def _on_logo_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        self._active_file_dialog = None
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        path_str = gfile.get_path()
        if not path_str:
            return
        path = Path(path_str)
        if not path.is_file():
            return
        try:
            path = persist_logo(path)
        except OSError:
            pass
        self._parent._converter.logo_path = path
        self._logo_row.set_subtitle(path.name)
        self._save_prefs()

    def _on_clear_logo(self, *_) -> None:
        self._parent._converter.logo_path = None
        self._logo_row.set_subtitle("No logo selected")
        self._save_prefs()

    def _save_prefs(self) -> None:
        sw = self._switch_rows
        save_editor_prefs({
            "theme":            self._parent._converter.theme,
            "ratio":            self._parent._converter.ratio,
            "logo":             str(self._parent._converter.logo_path or ""),
            "font_size":        self._parent._editor.get_font_size(),
            "line_length":      self._parent._editor.get_line_length(),
            "syntax_highlight": sw["syntax_highlight"].get_active(),
            "line_numbers":     sw["line_numbers"].get_active(),
            "highlight_line":   sw["highlight_line"].get_active(),
            "auto_indent":      sw["auto_indent"].get_active(),
            "spaces_tabs":      sw["spaces_tabs"].get_active(),
        })

# ── Theme card widget ─────────────────────────────────────────────────────────

class _ThemeCard(Gtk.ToggleButton):
    """
    A selectable card displaying a theme thumbnail and name.

    Used in the theme grid on the Slides preferences page.  Cards are
    grouped via set_group() so selecting one deselects the others.
    """

    def __init__(self, slug: str, name: str) -> None:
        super().__init__()
        self.slug = slug
        self.add_css_class("card")
        self.set_hexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_size_request(120, 68)
        box.append(self._picture)

        label = Gtk.Label(label=name)
        label.add_css_class("caption")
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_margin_top(4)
        label.set_margin_bottom(6)
        label.set_margin_start(4)
        label.set_margin_end(4)
        box.append(label)

        self.set_child(box)

    def set_thumbnail(self, texture: "Gdk.Texture") -> None:
        self._picture.set_paintable(texture)
