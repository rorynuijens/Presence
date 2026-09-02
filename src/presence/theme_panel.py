"""
theme_panel.py — The deck's own settings, in the right-hand inspector.

Everything here is a property of the document rather than of the app: the
theme, the aspect ratio and the logo, which the window writes back into the
frontmatter of any deck that pins them.  The app's own preferences —
target duration, speaking rate, convert on save — are in Preferences, and
are not repeated here; two homes for one setting is how a control ends up
lying about what it does.

Layout (top to bottom):
  Active theme preview (full-width thumbnail + name)
  Appearance (theme, ratio, logo, logo size)
  Syntax reference (collapsed)

The heading above it all belongs to the Inspector, not to this panel.

Design decisions vs the previous version:
  - Active theme shown large (full-width thumbnail) rather than as a card
  - Colour swatch cards replace thumbnail cards: two rectangles (bg+accent)
    render instantly with no async I/O
  - 3-column swatch grid is denser and easier to scan than 2-column thumbs
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
        # No heading of its own.  The Inspector that holds this panel draws
        # one — a label and a separator, the GNOME inspector pattern — and
        # for a while both were drawn, stacking "Deck" above "Current theme"
        # over two rules.  Left over from when the panel stood alone.

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

        # Swatch grid — lives in the chooser dialog, not the panel.  Browsing
        # 27 themes is a once-per-deck task and does not earn permanent space
        # beside the document.
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
        self._theme_dialog: Adw.Dialog | None = None
        self._theme_nav: Adw.NavigationView | None = None

        slide_group = Adw.PreferencesGroup()
        slide_group.set_title("Appearance")
        slide_group.set_margin_start(12)
        slide_group.set_margin_end(12)
        slide_group.set_margin_top(8)
        slide_group.set_margin_bottom(8)

        self._theme_row = Adw.ActionRow(title="Theme")
        change_btn = Gtk.Button(label="Change…")
        change_btn.add_css_class("flat")
        change_btn.set_valign(Gtk.Align.CENTER)
        change_btn.connect("clicked", self._on_change_theme)
        self._theme_row.add_suffix(change_btn)
        self._theme_row.set_activatable_widget(change_btn)
        slide_group.add(self._theme_row)

        from .slides.themes import ASPECT_RATIOS
        self._ratios = list(ASPECT_RATIOS.keys())
        self._ratio_row = Adw.ComboRow(title="Screen ratio")
        self._ratio_row.set_model(Gtk.StringList.new(self._ratios))
        self._ratio_row.connect("notify::selected", self._on_ratio_changed)
        slide_group.add(self._ratio_row)
        body.append(slide_group)

        # ── Logo ──────────────────────────────────────────────────────────────
        # Adw.ActionRow in the boxed list: what it is in the title, which
        # file in the subtitle, the two actions as suffixes.
        #
        # The title was "Select or drop logo" beside a "Choose…" and a
        # "Clear" button, which left it about eighty pixels of a 300px panel
        # to wrap an instruction across four lines.  A row's title names the
        # thing; the instruction was teaching the drag-and-drop, which is a
        # secondary affordance and belongs in the tooltip.  Clear is an icon
        # now — the label was the wider half of the pair and said the least.
        self._logo_row = Adw.ActionRow(title="Logo")
        self._logo_row.set_subtitle("None")
        # One line each, ellipsized.  A row this narrow cannot wrap a file
        # name without pushing the buttons around; the whole name goes in
        # the tooltip, which is where the row already explains itself.
        for setter, lines in (("set_title_lines", 1), ("set_subtitle_lines", 1)):
            if hasattr(self._logo_row, setter):
                getattr(self._logo_row, setter)(lines)

        logo_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        logo_btn_box.set_valign(Gtk.Align.CENTER)

        choose_btn = Gtk.Button(label="Choose…")
        choose_btn.add_css_class("flat")
        choose_btn.connect("clicked", self._on_choose_logo)
        logo_btn_box.append(choose_btn)

        self._logo_clear_btn = Gtk.Button()
        self._logo_clear_btn.set_child(
            Gtk.Image.new_from_icon_name("edit-clear-symbolic")
        )
        self._logo_clear_btn.set_tooltip_text("Remove the logo")
        self._logo_clear_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Remove the logo"]
        )
        self._logo_clear_btn.add_css_class("flat")
        self._logo_clear_btn.set_sensitive(False)
        self._logo_clear_btn.connect("clicked", self._on_clear_logo)
        logo_btn_box.append(self._logo_clear_btn)
        self._logo_row.add_suffix(logo_btn_box)
        self._logo_row.set_activatable_widget(choose_btn)
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

        # Target duration and "Convert on save" used to sit here as well as
        # in Preferences — the same two settings, with a Gtk.Scale and a
        # Gtk.CheckButton against Preferences' SpinRow and SwitchRow.  Both
        # are app preferences: they are stored in presentation_prefs, not in
        # the document, and apply to whatever deck is open.  Preferences is
        # their one home; what stays here belongs to the document.

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
            # Images.  These named the retired alt-text tokens for a long
            # time after they stopped doing anything, so the panel was
            # documenting a syntax the renderer ignored.
            ("Image",           "![description](path)",
             "The slide decides where it goes"),
            ("Place it",        "![…](path){left}",
             "left, right, top, bottom, background, full"),
            ("Treat it",        "![…](path){sepia}",
             "bw, greyscale, sepia, blur, lighten, darken, tint-navy"),
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

    def has_theme(self, slug: str) -> bool:
        """True when *slug* names a theme this panel can display."""
        return slug in self._all_themes

    def select_theme(self, slug: str) -> None:
        """
        Show *slug* as the current theme without emitting theme-changed.

        The window calls this with the theme the document will actually
        render at — its frontmatter's, where it pins one, and the app's
        default otherwise.  A deck carrying `theme: berlin` used to leave
        this panel showing whatever this machine last chose.
        """
        self._refresh_preview(slug)
        self._update_swatch_selection(slug)

    def select_ratio(self, ratio: str) -> None:
        """Show *ratio* — the document's, where it pins one — without rebuilding."""
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
        self._theme_row.set_subtitle(theme.name)
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

    def _on_change_theme(self, *_) -> None:
        """Open the theme chooser."""
        if self._theme_dialog is None:
            self._theme_dialog = self._build_theme_dialog()
        self._theme_dialog.present(self)

    def _build_theme_dialog(self) -> Adw.Dialog:
        """
        One dialog for everything about themes.

        Choosing one is the front page; making, installing and removing them
        is a page pushed onto the same Adw.NavigationView.  That page used to
        be the third page of Preferences, which is for settings rather than
        for a library of content — and it meant a writer chose a theme here
        and made one in a different dialog under a different menu.
        """
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(self._swatch_flow)

        bar = Adw.HeaderBar()
        manage_btn = Gtk.Button(label="Manage")
        manage_btn.set_tooltip_text("Add, edit or remove themes")
        manage_btn.connect("clicked", self._on_manage_themes)
        bar.pack_end(manage_btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(bar)
        toolbar.set_content(scroll)

        self._theme_nav = Adw.NavigationView()
        self._theme_nav.add(
            Adw.NavigationPage(child=toolbar, title="Themes")
        )

        dialog = Adw.Dialog()
        dialog.set_title("Themes")
        dialog.set_content_width(560)
        dialog.set_content_height(640)
        dialog.set_child(self._theme_nav)
        return dialog

    def _on_manage_themes(self, *_) -> None:
        """
        Push the manage page.

        Built fresh each time rather than kept: it lists what is installed,
        and the writer may have installed something since.
        """
        from .theme_manager_ui import build_themes_page

        parent = self._window or self.get_root()
        page   = build_themes_page(parent, self._converter, refresher=self)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(page)
        self._theme_nav.push(
            Adw.NavigationPage(child=toolbar, title="Manage Themes")
        )

    def refresh_themes(self) -> None:
        """
        Told by the manage page whenever the installed set changes.

        The name is the manage page's contract, not this panel's — it used to
        be answered by the Preferences dialog, which forwarded it here.
        """
        self.refresh()

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

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _on_swatch_toggled(self, card, slug: str) -> None:
        if not card.get_active() or self._converter is None:
            return
        self._converter.theme = slug
        self._refresh_preview(slug)
        self._save_editor_prefs()
        self.emit("theme-changed", slug)
        self.emit("rebuild-needed")

    def _on_ratio_changed(self, row: Adw.ComboRow, _param) -> None:
        if self._converter is None:
            return
        ratio = self._ratios[row.get_selected()]
        self._converter.ratio = ratio
        self._save_editor_prefs()
        self.emit("ratio-changed", ratio)
        self.emit("rebuild-needed")


    _LOGO_HINT = "Choose a picture, or drop one on this row"

    def _update_logo_ui(self, path) -> None:
        """Update the logo row subtitle, tooltip and size row visibility."""
        has_logo = path is not None
        self._logo_row.set_subtitle(path.name if has_logo else "None")
        # The subtitle is ellipsized to one line, so the tooltip carries the
        # name in full — and the drop hint when there is nothing to name.
        self._logo_row.set_tooltip_text(
            f"{path}\n\n{self._LOGO_HINT}" if has_logo else self._LOGO_HINT
        )
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
        dialog.set_title("Choose logo image")
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

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_editor_prefs(self) -> None:
        if self._converter is None or self._window is None:
            return
        from .session import save_editor_prefs
        save_editor_prefs({
            "theme":     self._converter.theme,
            "ratio":     self._converter.ratio,
            "logo":      str(self._converter.logo_path or ""),
            "font_size": self._window.editor.get_font_size(),
        })


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
