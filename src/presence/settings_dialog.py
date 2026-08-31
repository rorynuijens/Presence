"""
settings_dialog.py — Application preferences dialog.

Contains SettingsDialog (Adw.PreferencesDialog with three pages:
Presentation, Editor, Manage Themes).

Kept separate from window.py to honour single-responsibility and make each
class independently testable.
"""
from __future__ import annotations

import logging

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

from .session import (load_editor_prefs, save_editor_prefs,
                      load_presentation_prefs, save_presentation_prefs)

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .window import MainWindow

log = logging.getLogger(__name__)

# ── Settings dialog ───────────────────────────────────────────────────────────

class SettingsDialog(Adw.PreferencesDialog):
    """
    Three-page Preferences dialog — the app's settings, not the document's.

    Presentation  — target duration, speaking rate
    Editor        — font size, GtkSource toggles, convert on save
    Manage themes — install / uninstall / open folder

    Theme, aspect ratio and logo are properties of the deck and live in the
    inspector, which writes them into the document's frontmatter.
    """

    def __init__(self, parent: MainWindow) -> None:
        super().__init__()
        self._parent = parent

        from .theme_manager_ui import build_themes_page

        prefs = load_editor_prefs()

        # ── Page 1: Presentation ──────────────────────────────────────────────
        # Theme, aspect ratio and logo used to live here as well as in the
        # inspector.  They belong to the document — the inspector writes them
        # into its frontmatter — and a second copy in Preferences could only
        # set this machine's default, so on any deck that pinned `theme:` the
        # grid moved, a build ran, and the slides came back unchanged.  One
        # setting, one home: the document's own properties are the
        # inspector's, and what is left here is the app's.
        prefs_page = Adw.PreferencesPage()
        prefs_page.set_title("Presentation")
        prefs_page.set_icon_name("view-paged-symbolic")
        self.add(prefs_page)

        pres_group = Adw.PreferencesGroup(title="Timing")
        pres_group.set_description(
            "Used by the presenter timer and the strip's per-slide estimates"
        )
        prefs_page.add(pres_group)

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

    def refresh_themes(self) -> None:
        """Reflect an installed or uninstalled theme in the inspector."""
        self._parent._theme_panel.refresh()

    # ── Signal handlers ───────────────────────────────────────────────────────

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
