"""
theme_panel.py — Right-sidebar theme inspector panel.

Layout (top to bottom):
  Adw.HeaderBar "Theme"
  Active theme preview (full-width thumbnail + name)
  Colour swatch grid (3 columns, instant CSS rendering)
  Slide settings (ratio, logo)
  Presentation settings (duration scale, auto-convert checkbox)

Design decisions vs the previous version:
  - Adw.HeaderBar gives the panel a proper GNOME inspector identity
  - Active theme shown large (full-width thumbnail) rather than as a card
  - Colour swatch cards replace thumbnail cards: two rectangles (bg+accent)
    render instantly with no async I/O
  - 3-column swatch grid is denser and easier to scan than 2-column thumbs
  - Gtk.Scale for duration instead of SpinRow — more spatial, less dialog-like
  - Gtk.CheckButton for auto-convert instead of SwitchRow
  - Width: 300px

Signals
-------
theme-changed  (slug: str)
ratio-changed  (ratio: str)
rebuild-needed ()
"""

import logging

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GObject, GLib, Pango, Gio, Gdk

log = logging.getLogger(__name__)

from .app_utils import png_bytes_to_texture
from pathlib import Path


class ThemePanel(Gtk.Box):
    """
    Right-sidebar theme inspector panel.
    Call attach(window, converter) once after construction.
    """

    __gsignals__ = {
        "theme-changed":  (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "ratio-changed":  (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "rebuild-needed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    _WIDTH = 300

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_size_request(self._WIDTH, -1)

        self._thumb_cancelled: list[bool] = [False]
        self._converter  = None
        self._window     = None
        self._all_themes: dict = {}
        # Strong reference to any active Gtk.FileDialog — prevents GC collection
        # before the async callback fires. Cleared in each completion callback.
        self._active_file_dialog = None

        self._build()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build(self) -> None:
        # Section header — plain label + separator per GNOME HIG inspector
        # panel pattern (Builder, Loupe etc.). Adw.HeaderBar wastes 48 px
        # of vertical space in a 300 px-wide panel.
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        title_box.set_margin_top(10)
        title_box.set_margin_bottom(10)
        title_box.set_margin_start(12)
        title_box.set_margin_end(12)
        title_lbl = Gtk.Label(label="Current theme")
        title_lbl.add_css_class("heading")
        title_lbl.set_xalign(0)
        title_lbl.set_hexpand(True)
        title_box.append(title_lbl)
        self.append(title_box)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Scrollable body
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroll.set_child(body)
        self.append(scroll)

        # Active theme preview
        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        preview_box.set_margin_top(12)
        preview_box.set_margin_start(12)
        preview_box.set_margin_end(12)
        preview_box.set_margin_bottom(8)

        self._preview_picture = Gtk.Picture()
        self._preview_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        thumb_w = self._WIDTH - 24
        thumb_h = thumb_w * 9 // 16
        self._preview_picture.set_size_request(thumb_w, thumb_h)
        self._preview_picture.add_css_class("card")
        preview_box.append(self._preview_picture)

        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_row.set_margin_top(8)
        self._preview_name = Gtk.Label()
        self._preview_name.add_css_class("title-4")
        self._preview_name.set_ellipsize(Pango.EllipsizeMode.END)
        self._preview_name.set_hexpand(True)
        self._preview_name.set_xalign(0)
        name_row.append(self._preview_name)
        preview_box.append(name_row)
        body.append(preview_box)

        # Swatch grid label
        swatch_label = Gtk.Label(label="All themes")
        swatch_label.add_css_class("caption")
        swatch_label.add_css_class("dim-label")
        swatch_label.set_xalign(0)
        swatch_label.set_margin_start(12)
        swatch_label.set_margin_end(12)
        swatch_label.set_margin_bottom(4)
        body.append(swatch_label)

        # Swatch grid
        self._swatch_flow = Gtk.FlowBox()
        self._swatch_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._swatch_flow.set_homogeneous(True)
        self._swatch_flow.set_column_spacing(6)
        self._swatch_flow.set_row_spacing(6)
        self._swatch_flow.set_margin_start(12)
        self._swatch_flow.set_margin_end(12)
        self._swatch_flow.set_margin_bottom(12)
        self._swatch_flow.set_max_children_per_line(3)
        self._swatch_flow.set_min_children_per_line(3)
        body.append(self._swatch_flow)

        slide_group = Adw.PreferencesGroup()
        slide_group.set_title("Slide settings")
        slide_group.set_margin_start(12)
        slide_group.set_margin_end(12)
        slide_group.set_margin_top(8)
        slide_group.set_margin_bottom(8)

        from .slides.themes import ASPECT_RATIOS
        self._ratios = list(ASPECT_RATIOS.keys())
        self._ratio_row = Adw.ComboRow(title="Screen ratio")
        self._ratio_row.set_model(Gtk.StringList.new(self._ratios))
        self._ratio_row.connect("notify::selected", self._on_ratio_changed)
        slide_group.add(self._ratio_row)
        body.append(slide_group)

        # ── Logo — HIG-compliant boxed-list ActionRow ─────────────────────────
        # GNOME HIG pattern: Adw.ActionRow in a boxed-list with a thumbnail
        # prefix showing the current logo (or a placeholder icon) and
        # Choose / Clear suffix buttons.  Drag-and-drop onto the row is
        # supported as a secondary affordance, not the primary one.
        # Logo rows — added to slide_group alongside the ratio row.
        # No prefix icon: the 300px panel is too narrow for icon + title + subtitle + two buttons.
        self._logo_row = Adw.ActionRow(title="Select or drop logo")
        self._logo_row.set_subtitle("None")

        logo_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        logo_btn_box.set_valign(Gtk.Align.CENTER)

        choose_btn = Gtk.Button(label="Choose…")
        choose_btn.add_css_class("flat")
        choose_btn.connect("clicked", self._on_choose_logo)
        logo_btn_box.append(choose_btn)

        self._logo_clear_btn = Gtk.Button(label="Clear")
        self._logo_clear_btn.add_css_class("flat")
        self._logo_clear_btn.set_sensitive(False)
        self._logo_clear_btn.connect("clicked", self._on_clear_logo)
        logo_btn_box.append(self._logo_clear_btn)
        self._logo_row.add_suffix(logo_btn_box)
        slide_group.add(self._logo_row)

        # Size slider row — hidden until a logo is chosen
        self._logo_size_row = Adw.ActionRow(title="Logo size")
        self._logo_size_row.set_visible(False)

        self._logo_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.5, 2.0, 0.1
        )
        self._logo_scale.set_hexpand(True)
        self._logo_scale.set_draw_value(False)
        self._logo_scale.set_value(1.0)
        self._logo_scale.set_increments(0.1, 0.5)
        self._logo_scale.set_valign(Gtk.Align.CENTER)
        self._logo_scale.connect("value-changed", self._on_logo_size_changed)

        self._logo_size_val = Gtk.Label(label="1.0×")
        self._logo_size_val.set_width_chars(4)
        self._logo_size_val.set_xalign(1)
        self._logo_size_val.add_css_class("caption")
        self._logo_size_val.set_valign(Gtk.Align.CENTER)

        size_suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        size_suffix.set_valign(Gtk.Align.CENTER)
        size_suffix.append(self._logo_scale)
        size_suffix.append(self._logo_size_val)
        self._logo_size_row.add_suffix(size_suffix)
        slide_group.add(self._logo_size_row)

        # Drag-and-drop onto the logo row as a secondary affordance.
        logo_drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        logo_drop.connect("drop",  self._on_logo_drop)
        logo_drop.connect("enter", self._on_logo_drop_enter)
        logo_drop.connect("leave", self._on_logo_drop_leave)
        self._logo_row.add_controller(logo_drop)

        pres_group = Adw.PreferencesGroup()
        pres_group.set_title("Presentation settings")
        pres_group.set_margin_start(12)
        pres_group.set_margin_end(12)
        pres_group.set_margin_bottom(8)

        # Duration as ActionRow with scale suffix
        dur_row = Adw.ActionRow(title="Duration")
        scale_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scale_box.set_valign(Gtk.Align.CENTER)
        self._dur_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 120, 5
        )
        self._dur_scale.set_size_request(120, -1)
        self._dur_scale.set_draw_value(False)
        self._dur_scale.set_increments(5, 15)
        self._dur_scale.connect("value-changed", self._on_duration_changed)
        self._dur_val_label = Gtk.Label(label="∞")
        self._dur_val_label.set_width_chars(6)
        self._dur_val_label.set_xalign(1)
        self._dur_val_label.add_css_class("numeric")
        self._dur_val_label.add_css_class("caption")
        scale_box.append(self._dur_scale)
        scale_box.append(self._dur_val_label)
        dur_row.add_suffix(scale_box)
        pres_group.add(dur_row)

        # Auto-convert as ActionRow with CheckButton suffix
        auto_row = Adw.ActionRow(title="Convert on save")
        self._auto_check = Gtk.CheckButton()
        self._auto_check.set_valign(Gtk.Align.CENTER)
        self._auto_check.connect("toggled", self._on_auto_convert_toggled)
        auto_row.add_suffix(self._auto_check)
        auto_row.set_activatable_widget(self._auto_check)
        pres_group.add(auto_row)

        body.append(pres_group)

        body.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Syntax reference ──────────────────────────────────────────────────
        # A compact cheat sheet that teaches the Markdown syntax while the
        # user works — no need to open documentation.  Inspired by iA Presenter's
        # right-sidebar reference panel.  Collapsible to save vertical space.
        syntax_expander = Gtk.Expander()
        syntax_expander.set_margin_start(12)
        syntax_expander.set_margin_end(12)
        syntax_expander.set_margin_top(8)
        syntax_expander.set_margin_bottom(12)

        exp_lbl = Gtk.Label(label="Syntax reference")
        exp_lbl.add_css_class("caption")
        syntax_expander.set_label_widget(exp_lbl)
        syntax_expander.set_expanded(False)

        ref_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        ref_box.set_margin_top(8)

        # Each entry: (display_label, monospace_syntax, tooltip)
        _SYNTAX = [
            # Structure
            ("New slide",       "---",          "Separate slides with three dashes"),
            ("Speaker notes",   "^^^",          "Notes below ^^^ are presenter-only"),
            ("Two columns",     "|||",          "Split slide into two columns"),
            # Headings
            ("Title (H1)",      "# Heading",    "Large title — use on cover slides"),
            ("Section (H2)",    "## Heading",   "Section heading"),
            ("Slide head (H3)", "### Heading",  "Main slide heading"),
            # Inline
            ("Bold",            "**text**",     "Bold text"),
            ("Italic",          "*text*",       "Italic text"),
            ("Code",            "`code`",       "Inline code"),
            ("Link",            "[text](url)",  "Hyperlink"),
            ("Comment",         "<!-- note -->","Hidden comment, not on slide"),
            # Block
            ("Bullet",          "- item",       "Unordered list item"),
            ("Numbered",        "1. item",      "Ordered list item"),
            ("Quote",           "> text",       "Blockquote"),
            ("Code block",      "```\ncode\n```", "Fenced code block"),
            # Images
            ("Image (right)",   "![|right|50](path)", "Image on right, 50% width"),
            ("Image (left)",    "![|left|50](path)",  "Image on left, 50% width"),
            ("Background",      "![|background](path)","Full-bleed background image"),
            # Callouts
            ("Tip callout",     "> [!tip]\n> text",    "Green tip callout box"),
            ("Info callout",    "> [!info]\n> text",   "Blue info callout box"),
            ("Warning",         "> [!warning]\n> text","Yellow warning callout box"),
        ]

        for label_text, syntax_text, tooltip in _SYNTAX:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_margin_top(3)
            row.set_margin_bottom(3)
            row.set_tooltip_text(tooltip)

            name_lbl = Gtk.Label(label=label_text)
            name_lbl.set_xalign(0)
            name_lbl.set_hexpand(True)
            name_lbl.add_css_class("caption")
            row.append(name_lbl)

            syn_lbl = Gtk.Label(label=syntax_text)
            syn_lbl.set_xalign(1)
            syn_lbl.add_css_class("caption")
            syn_lbl.add_css_class("monospace")
            syn_lbl.add_css_class("dim-label")
            syn_lbl.set_ellipsize(Pango.EllipsizeMode.START)
            syn_lbl.set_max_width_chars(18)
            row.append(syn_lbl)

            ref_box.append(row)

        syntax_expander.set_child(ref_box)
        body.append(syntax_expander)


    # ── Public API ────────────────────────────────────────────────────────────

    def attach(self, window, converter) -> None:
        """Bind to window and converter. Call once after construction."""
        self._window    = window
        self._converter = converter
        self.refresh()

    def refresh(self) -> None:
        """Reload themes, rebuild swatches, sync all controls."""
        if self._converter is None:
            return
        from .slides.theme_loader import load_all_themes
        self._all_themes = load_all_themes()
        self._rebuild_swatches()
        self._refresh_preview(self._converter.theme)
        self._sync_controls()

    def select_theme(self, slug: str) -> None:
        """Update display without emitting theme-changed (called by Settings)."""
        self._refresh_preview(slug)
        self._update_swatch_selection(slug)

    def select_ratio(self, ratio: str) -> None:
        """Update ratio ComboRow without triggering a rebuild."""
        if ratio in self._ratios:
            try:
                self._ratio_row.handler_block_by_func(self._on_ratio_changed)
                self._ratio_row.set_selected(self._ratios.index(ratio))
                self._ratio_row.handler_unblock_by_func(self._on_ratio_changed)
            except TypeError:
                self._ratio_row.set_selected(self._ratios.index(ratio))

    # ── Private ───────────────────────────────────────────────────────────────

    def _refresh_preview(self, slug: str) -> None:
        """Update the large top thumbnail and theme name label."""
        theme = self._all_themes.get(slug)
        if theme is None:
            return
        self._preview_name.set_label(theme.name)
        self._thumb_cancelled[0] = True
        self._thumb_cancelled = [False]
        ratio     = self._converter.ratio if self._converter else "16:9"
        cancelled = self._thumb_cancelled
        picture   = self._preview_picture
        from .theme_manager_ui import _thumb_cache

        def _on_png(png_bytes, cf=cancelled):
            if cf[0]:
                return
            if png_bytes:
                texture = png_bytes_to_texture(png_bytes)
                if texture is not None:
                    picture.set_paintable(texture)

        _thumb_cache.get_async(theme, ratio, _on_png)

    def _rebuild_swatches(self) -> None:
        """Clear and repopulate the swatch FlowBox with colour-only cards."""
        while True:
            child = self._swatch_flow.get_first_child()
            if child is None:
                break
            self._swatch_flow.remove(child)

        current_slug = self._converter.theme if self._converter else ""
        first_card = None

        for slug, theme in sorted(self._all_themes.items()):
            card = _SwatchCard(slug, theme.name, theme.bg, theme.accent)
            if first_card is None:
                first_card = card
            else:
                card.set_group(first_card)

            card.connect("toggled", self._on_swatch_toggled, slug)
            # Set active after connecting so block/unblock works
            card.handler_block_by_func(self._on_swatch_toggled)
            card.set_active(slug == current_slug)
            card.handler_unblock_by_func(self._on_swatch_toggled)

            self._swatch_flow.append(card)

    def _update_swatch_selection(self, slug: str) -> None:
        """Highlight the swatch for *slug* without firing _on_swatch_toggled."""
        child = self._swatch_flow.get_first_child()
        while child is not None:
            # FlowBoxChild wraps the actual card
            inner = child.get_child() if hasattr(child, "get_child") else child
            if isinstance(inner, _SwatchCard):
                try:
                    inner.handler_block_by_func(self._on_swatch_toggled)
                    inner.set_active(inner.slug == slug)
                    inner.handler_unblock_by_func(self._on_swatch_toggled)
                except TypeError:
                    inner.set_active(inner.slug == slug)
            child = child.get_next_sibling()

    def _sync_controls(self) -> None:
        """Sync all controls to current state without triggering handlers."""
        c = self._converter
        w = self._window
        # Ratio
        try:
            self._ratio_row.handler_block_by_func(self._on_ratio_changed)
            if c.ratio in self._ratios:
                self._ratio_row.set_selected(self._ratios.index(c.ratio))
            self._ratio_row.handler_unblock_by_func(self._on_ratio_changed)
        except TypeError:
            if c.ratio in self._ratios:
                self._ratio_row.set_selected(self._ratios.index(c.ratio))
        # Logo
        self._update_logo_ui(c.logo_path)
        # Restore size slider value if logo_scale is set
        logo_scale = getattr(c, 'logo_scale', 1.0)
        try:
            self._logo_scale.handler_block_by_func(self._on_logo_size_changed)
            self._logo_scale.set_value(logo_scale)
            self._logo_size_val.set_label(f'{logo_scale:.1f}×')
            self._logo_scale.handler_unblock_by_func(self._on_logo_size_changed)
        except TypeError:
            self._logo_scale.set_value(logo_scale)
        # Duration
        if w is not None:
            minutes = min(w._timer_minutes, 120)
            try:
                self._dur_scale.handler_block_by_func(self._on_duration_changed)
                self._dur_scale.set_value(float(minutes))
                self._dur_scale.handler_unblock_by_func(self._on_duration_changed)
            except TypeError:
                self._dur_scale.set_value(float(minutes))
            self._dur_val_label.set_label(
                f"{minutes} min" if minutes > 0 else "∞"
            )
            # Auto-convert
            try:
                self._auto_check.handler_block_by_func(self._on_auto_convert_toggled)
                self._auto_check.set_active(w._auto_convert)
                self._auto_check.handler_unblock_by_func(self._on_auto_convert_toggled)
            except TypeError:
                self._auto_check.set_active(w._auto_convert)

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _on_swatch_toggled(self, card, slug: str) -> None:
        if not card.get_active() or self._converter is None:
            return
        self._converter.theme = slug
        self._refresh_preview(slug)
        self._save_editor_prefs()
        self._sync_settings_dialog_theme(slug)
        self.emit("theme-changed", slug)
        self.emit("rebuild-needed")

    def _on_ratio_changed(self, row: Adw.ComboRow, _param) -> None:
        if self._converter is None:
            return
        ratio = self._ratios[row.get_selected()]
        self._converter.ratio = ratio
        self._save_editor_prefs()
        self._sync_settings_dialog_ratio(ratio)
        self.emit("ratio-changed", ratio)
        self.emit("rebuild-needed")


    def _update_logo_ui(self, path) -> None:
        """Update the logo row subtitle and size row visibility."""
        has_logo = path is not None
        self._logo_row.set_subtitle(path.name if has_logo else "None")
        self._logo_clear_btn.set_sensitive(has_logo)
        self._logo_size_row.set_visible(has_logo)

    def _on_logo_drop(self, target, file_list, x, y) -> bool:
        """Accept a file dropped onto the logo row."""
        self._logo_row.remove_css_class("accent")
        files = file_list.get_files()
        if not files:
            return False
        path = Path(files[0].get_path() or "")
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".svg", ".webp"}:
            return False
        if self._converter is None:
            return False
        self._converter.logo_path = path
        self._update_logo_ui(path)
        self._save_editor_prefs()
        self.emit("rebuild-needed")
        return True

    def _on_logo_drop_enter(self, target, x, y) -> Gdk.DragAction:
        self._logo_row.add_css_class("accent")
        return Gdk.DragAction.COPY

    def _on_logo_drop_leave(self, target) -> None:
        self._logo_row.remove_css_class("accent")

    def _on_logo_size_changed(self, scale: Gtk.Scale) -> None:
        """Logo size multiplier — stored on converter and triggers rebuild."""
        val = round(scale.get_value(), 1)
        self._logo_size_val.set_label(f"{val:.1f}×")
        if self._converter is not None:
            # Store on converter so build_css() can read it
            self._converter.logo_scale = val
            self._save_editor_prefs()
            self.emit("rebuild-needed")

    def _on_choose_logo(self, *_) -> None:
        if self._window is None:
            return
        from .app_utils import make_file_filter, make_filter_store
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose Logo Image")
        dialog.set_filters(make_filter_store(
            make_file_filter("Images (PNG, JPEG, SVG)",
                             "*.png", "*.jpg", "*.jpeg", "*.svg")
        ))
        # Keep a strong reference so the GC cannot collect the dialog before
        # the async callback fires. Cleared in _on_logo_chosen.
        self._active_file_dialog = dialog
        dialog.open(self._window, None, self._on_logo_chosen)

    def _on_logo_chosen(self, dialog, result) -> None:
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
            from .session import persist_logo
            path = persist_logo(path)
        except OSError:
            pass
        self._converter.logo_path = path
        self._update_logo_ui(path)
        self._save_editor_prefs()
        self.emit("rebuild-needed")

    def _on_clear_logo(self, *_) -> None:
        if self._converter is None:
            return
        self._converter.logo_path = None
        self._update_logo_ui(None)
        self._save_editor_prefs()
        self.emit("rebuild-needed")

    def _on_duration_changed(self, scale: Gtk.Scale) -> None:
        minutes = round(int(scale.get_value()) / 5) * 5
        self._dur_val_label.set_label(
            f"{minutes} min" if minutes > 0 else "∞"
        )
        if self._window is not None:
            self._window._timer_minutes = minutes
            self._save_presentation_prefs()

    def _on_auto_convert_toggled(self, btn: Gtk.CheckButton) -> None:
        if self._window is None:
            return
        self._window._auto_convert = btn.get_active()
        self._save_presentation_prefs()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_editor_prefs(self) -> None:
        if self._converter is None or self._window is None:
            return
        from .session import save_editor_prefs
        save_editor_prefs({
            "theme":     self._converter.theme,
            "ratio":     self._converter.ratio,
            "logo":      str(self._converter.logo_path or ""),
            "font_size": self._window._editor.get_font_size(),
        })

    def _save_presentation_prefs(self) -> None:
        if self._window is None:
            return
        from .session import save_presentation_prefs
        # Always write all four fields so no value is silently zeroed out
        # when only one of them changes (e.g. timer or auto-convert).
        save_presentation_prefs({
            "timer_minutes":        self._window._timer_minutes,
            "auto_convert":         self._window._auto_convert,
            "presenter_notes_font": self._window._presenter_notes_font,
            "speaking_rate":        self._window._speaking_rate,
        })

    # ── Settings dialog sync ──────────────────────────────────────────────────

    def _sync_settings_dialog_theme(self, slug: str) -> None:
        if self._window is None:
            return
        dlg = getattr(self._window, "_settings_dialog_ref", None)
        if dlg is None:
            return
        try:
            for s, card in dlg._theme_cards.items():
                card.handler_block_by_func(dlg._on_theme_card_toggled)
                card.set_active(s == slug)
                card.handler_unblock_by_func(dlg._on_theme_card_toggled)
        except Exception:
            pass

    def _sync_settings_dialog_ratio(self, ratio: str) -> None:
        if self._window is None:
            return
        dlg = getattr(self._window, "_settings_dialog_ref", None)
        if dlg is None:
            return
        try:
            if ratio in self._ratios:
                dlg._ratio_row.set_selected(self._ratios.index(ratio))
        except Exception:
            pass


# ── Colour swatch card ────────────────────────────────────────────────────────

class _SwatchCard(Gtk.ToggleButton):
    """
    Compact colour swatch — bg on left half, accent on right half.
    Renders instantly with CSS; no PNG loading required.
    Participates in a radio group via set_group().
    """

    _STRIP_H = 36

    def __init__(self, slug: str, name: str,
                 bg: str, accent: str) -> None:
        super().__init__()
        self.slug = slug
        self.add_css_class("card")
        self.set_hexpand(True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Two-colour strip
        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        strip.set_size_request(-1, self._STRIP_H)
        strip.set_overflow(Gtk.Overflow.HIDDEN)

        bg_box = Gtk.Box()
        bg_box.set_hexpand(True)
        _apply_bg(bg_box, bg)
        strip.append(bg_box)

        acc_box = Gtk.Box()
        acc_box.set_hexpand(True)
        _apply_bg(acc_box, accent)
        strip.append(acc_box)

        outer.append(strip)

        # Theme name
        lbl = Gtk.Label(label=name)
        lbl.add_css_class("caption")
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.set_max_width_chars(8)
        lbl.set_margin_top(3)
        lbl.set_margin_bottom(4)
        lbl.set_margin_start(3)
        lbl.set_margin_end(3)
        outer.append(lbl)

        self.set_child(outer)


# Module-level cache: one CssProvider per unique colour string.
# Prevents creating 40+ redundant providers for the swatch grid.
_bg_provider_cache: dict[str, Gtk.CssProvider] = {}


def _apply_bg(widget: Gtk.Widget, colour: str) -> None:
    """
    Set *colour* as the background of *widget* using a cached CSS provider.
    Providers are shared across all widgets with the same colour, so
    with 20 themes × 2 colour boxes we create at most 40 providers total
    rather than one per widget instance.
    """
    if colour not in _bg_provider_cache:
        css = Gtk.CssProvider()
        rule = f"box {{ background: {colour}; }}"
        try:
            css.load_from_string(rule)
        except AttributeError:
            css.load_from_data(rule.encode())
        _bg_provider_cache[colour] = css
    widget.get_style_context().add_provider(
        _bg_provider_cache[colour],
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
