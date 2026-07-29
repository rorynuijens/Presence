"""
ai_infographic_dialog.py — Generate a structured SVG infographic via the Claude API.

Opened from the image controls, from "Make an infographic for this slide".
Presents: editable slide content area, infographic type picker, layout controls
(position/size/gradient — shared with image insertion), Generate button, SVG
preview, and an Insert button that writes the Markdown image tag.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from .session import load_api_keys, load_ai_prefs
from .slides.infographic_gen import (
    INFOGRAPHIC_TYPES, generate_single_infographic,
    INFOGRAPHIC_MODEL_LABELS, INFOGRAPHIC_MODEL_IDS, INFOGRAPHIC_MODEL_PROVIDERS,
)

log = logging.getLogger(__name__)


_RASTER_W = 1920
_RASTER_H = 1080


def _svg_to_png(svg_path: str) -> str:
    """
    Rasterize an SVG to PNG at 1920×1080 using librsvg + cairo.

    Rendering at full-HD resolution ensures the infographic stays crisp when
    the slide is displayed full-screen or in the presenter view.  The SVG
    viewBox (800×450) would produce a blurry result when upscaled to slide
    dimensions, so we drive the viewport to the target raster size instead.

    Returns the PNG path on success, or the original svg_path on failure so
    the caller always gets a usable file path back.
    """
    png_path = str(Path(svg_path).with_suffix(".png"))
    try:
        gi.require_version("Rsvg", "2.0")
        from gi.repository import Rsvg
        import cairo

        handle = Rsvg.Handle.new_from_file(svg_path)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, _RASTER_W, _RASTER_H)
        ctx = cairo.Context(surface)
        vp = Rsvg.Rectangle()
        vp.x, vp.y, vp.width, vp.height = 0, 0, _RASTER_W, _RASTER_H
        handle.render_document(ctx, vp)
        surface.write_to_png(png_path)
        log.debug("SVG→PNG: %s (%dx%d)", png_path, _RASTER_W, _RASTER_H)
        return png_path
    except Exception as e:
        log.debug("SVG→PNG conversion failed, keeping SVG: %s", e)
        return svg_path


def _build_alt(desc: str, layout: dict) -> str:
    """Reconstruct the alt token string from description + layout dict."""
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
    return "|".join(tokens)


def _get_theme_info(parent_window) -> dict:
    """Resolve colours and fonts from the current theme for use in the prompt."""
    defaults = {
        "accent":       "#E17000",
        "accent2":      "",
        "fg":           "#1a1a2e",
        "body_font":    "sans-serif",
        "heading_font": "",
    }
    try:
        from .slides.theme_loader import load_all_themes
        converter = getattr(parent_window, "_converter", None)
        if converter is None:
            return defaults
        theme_name = getattr(converter, "theme", "")
        themes = load_all_themes()
        theme = themes.get(theme_name) or next(iter(themes.values()), None)
        if theme is not None:
            return {
                "accent":       theme.accent,
                "accent2":      theme.resolved_accent2,
                "fg":           theme.fg,
                "body_font":    theme.body_font,
                "heading_font": theme.resolved_heading_font,
            }
    except Exception as e:
        log.debug("Could not resolve theme info: %s", e)
    return defaults


class AIInfographicDialog(Adw.Dialog):
    """
    Modal dialog for generating a structured SVG infographic via Claude.

    Lifecycle:
      1. User reviews/edits the auto-filled slide content and picks a type.
      2. "Generate" triggers a background Claude call; spinner is shown.
      3. On success an SVG preview is revealed; "Regenerate" and "Insert" appear.
      4. "Insert" calls insert_cb(alt_tokens, rel_path) and closes the dialog.

    Parameters
    ----------
    parent_window : MainWindow
        Owning window — used to locate the assets/ directory and current theme.
    layout : dict
        Image layout tokens from ImageLayoutPopover: {position, size, gradient, …}.
    insert_cb : (alt: str, rel_path: str) -> None
        Called when the user confirms insertion.
    initial_slide_md : str
        Pre-filled slide Markdown (full current slide content).
    """

    def __init__(
        self,
        parent_window,
        layout: dict,
        insert_cb,
        initial_slide_md: str = "",
    ) -> None:
        super().__init__()
        self.set_title("Make an infographic")
        self.set_content_width(480)
        self._parent_window  = parent_window
        self._layout         = layout
        self._insert_cb      = insert_cb
        self._generated_path: str | None = None
        self._generating     = False
        self._theme_info     = _get_theme_info(parent_window)

        self._build_ui(initial_slide_md)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self, initial_slide_md: str) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_child(toolbar_view)

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel_btn)
        toolbar_view.add_top_bar(header)

        self._toast_overlay = Adw.ToastOverlay()
        toolbar_view.set_content(self._toast_overlay)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(12)
        content.set_margin_bottom(16)
        content.set_margin_start(12)
        content.set_margin_end(12)
        self._toast_overlay.set_child(content)

        # ── Form ──────────────────────────────────────────────────────────────
        group = Adw.PreferencesGroup()
        content.append(group)

        # Infographic type picker
        prefs = load_ai_prefs()
        saved_type = prefs.get("infographic_type", INFOGRAPHIC_TYPES[0])
        type_idx = (
            INFOGRAPHIC_TYPES.index(saved_type)
            if saved_type in INFOGRAPHIC_TYPES else 0
        )
        self._type_row = Adw.ComboRow(title="Infographic type")
        self._type_row.set_model(Gtk.StringList.new(INFOGRAPHIC_TYPES))
        self._type_row.set_selected(type_idx)
        group.add(self._type_row)

        saved_model = prefs.get("infographic_model", INFOGRAPHIC_MODEL_LABELS[0])
        model_idx = (
            INFOGRAPHIC_MODEL_LABELS.index(saved_model)
            if saved_model in INFOGRAPHIC_MODEL_LABELS else 0
        )
        self._model_row = Adw.ComboRow(title="Model")
        self._model_row.set_model(Gtk.StringList.new(INFOGRAPHIC_MODEL_LABELS))
        self._model_row.set_selected(model_idx)
        group.add(self._model_row)

        # Slide content — editable multiline area so user can focus/trim input
        content_group = Adw.PreferencesGroup(title="Slide content")
        content_group.set_description(
            "The AI will base the infographic on this text. Edit to focus on the data you want visualised."
        )
        content.append(content_group)

        content_frame = Gtk.Frame()
        content_frame.add_css_class("card")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(120)
        scroll.set_max_content_height(200)

        self._content_view = Gtk.TextView()
        self._content_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._content_view.set_monospace(True)
        self._content_view.set_top_margin(8)
        self._content_view.set_bottom_margin(8)
        self._content_view.set_left_margin(10)
        self._content_view.set_right_margin(10)
        self._content_view.get_buffer().set_text(initial_slide_md)
        scroll.set_child(self._content_view)
        content_frame.set_child(scroll)

        content_group_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        content_group_box.append(content_group)
        content_group_box.append(content_frame)
        content.append(content_group_box)

        # ── Progress ──────────────────────────────────────────────────────────
        self._progress_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        self._progress_box.set_visible(False)

        self._spinner = Gtk.Spinner()
        self._spinner.set_halign(Gtk.Align.CENTER)
        self._progress_box.append(self._spinner)

        self._progress_label = Gtk.Label(label="Generating infographic…")
        self._progress_label.add_css_class("caption")
        self._progress_label.add_css_class("dim-label")
        self._progress_label.set_halign(Gtk.Align.CENTER)
        self._progress_box.append(self._progress_label)
        content.append(self._progress_box)

        # ── Preview ───────────────────────────────────────────────────────────
        self._preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._preview_box.set_visible(False)

        preview_frame = Gtk.AspectFrame(ratio=16.0 / 9.0, obey_child=False)
        preview_frame.add_css_class("card")

        self._preview_picture = Gtk.Picture()
        self._preview_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._preview_picture.set_size_request(-1, 190)
        preview_frame.set_child(self._preview_picture)
        self._preview_box.append(preview_frame)

        content.append(self._preview_box)

        # ── Buttons ───────────────────────────────────────────────────────────
        self._generate_btn = Gtk.Button(label="Generate")
        self._generate_btn.add_css_class("suggested-action")
        self._generate_btn.set_halign(Gtk.Align.FILL)
        self._generate_btn.connect("clicked", self._on_generate_clicked)
        content.append(self._generate_btn)

        self._post_gen_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        self._post_gen_box.set_visible(False)

        self._regenerate_btn = Gtk.Button(label="Regenerate")
        self._regenerate_btn.set_hexpand(True)
        self._regenerate_btn.connect("clicked", self._on_generate_clicked)
        self._post_gen_box.append(self._regenerate_btn)

        self._insert_btn = Gtk.Button(label="Insert")
        self._insert_btn.add_css_class("suggested-action")
        self._insert_btn.set_hexpand(True)
        self._insert_btn.connect("clicked", self._on_insert_clicked)
        self._post_gen_box.append(self._insert_btn)

        content.append(self._post_gen_box)

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _on_generate_clicked(self, *_) -> None:
        if self._generating:
            return

        buf = self._content_view.get_buffer()
        slide_md = buf.get_text(
            buf.get_start_iter(), buf.get_end_iter(), False
        ).strip()
        if not slide_md:
            self._show_toast("Add some slide content first.")
            return

        model_idx = self._model_row.get_selected()
        provider = INFOGRAPHIC_MODEL_PROVIDERS[model_idx]
        model_id = INFOGRAPHIC_MODEL_IDS[model_idx]

        claude_key, gemini_key = load_api_keys()
        if provider == "gemini":
            api_key = gemini_key
            if not api_key:
                self._show_toast("Gemini API key not set — open Settings to add it.")
                return
        else:
            api_key = claude_key
            if not api_key:
                self._show_toast("Claude API key not set — open Settings to add it.")
                return

        if not self._parent_window._file_path:
            self._show_toast(
                "Save the document first — infographics are written to "
                "assets/ next to the .md file."
            )
            return

        assets_dir = str(self._parent_window._file_path.parent / "assets")
        infographic_type = INFOGRAPHIC_TYPES[self._type_row.get_selected()]

        self._enter_generating_state()

        threading.Thread(
            target=self._run_generation,
            args=(slide_md, infographic_type, api_key, model_id, provider,
                  assets_dir, self._theme_info),
            daemon=True,
        ).start()

    def _on_insert_clicked(self, *_) -> None:
        if not self._generated_path:
            return

        infographic_type = INFOGRAPHIC_TYPES[self._type_row.get_selected()]
        alt  = _build_alt(infographic_type, self._layout)
        rel  = "assets/" + Path(self._generated_path).name

        prefs = load_ai_prefs()
        prefs["infographic_type"] = infographic_type
        prefs["infographic_model"] = INFOGRAPHIC_MODEL_LABELS[self._model_row.get_selected()]
        from .session import save_ai_prefs
        save_ai_prefs(prefs)

        self._insert_cb(alt, rel)
        self.close()

    # ── State machine ─────────────────────────────────────────────────────────

    def _enter_generating_state(self) -> None:
        self._generating = True
        self._generate_btn.set_sensitive(False)
        self._post_gen_box.set_visible(False)
        self._type_row.set_sensitive(False)
        self._model_row.set_sensitive(False)
        self._content_view.set_sensitive(False)
        self._progress_box.set_visible(True)
        self._spinner.start()

    def _enter_idle_state(self) -> None:
        self._generating = False
        self._generate_btn.set_sensitive(True)
        self._type_row.set_sensitive(True)
        self._model_row.set_sensitive(True)
        self._content_view.set_sensitive(True)
        self._progress_box.set_visible(False)
        self._spinner.stop()

    # ── Background work ───────────────────────────────────────────────────────

    def _run_generation(
        self,
        slide_md: str,
        infographic_type: str,
        api_key: str,
        model_id: str,
        provider: str,
        output_dir: str,
        theme_info: dict,
    ) -> None:
        try:
            svg_path = generate_single_infographic(
                api_key, slide_md, infographic_type,
                output_dir, theme_info, model_id, provider,
            )
        except Exception as e:
            log.warning("Infographic generation failed: %s", e)
            GLib.idle_add(self._on_generation_error, str(e))
            return
        # Rasterize SVG→PNG so WebKit renders it reliably as a slide image.
        path = _svg_to_png(svg_path)
        GLib.idle_add(self._on_generation_done, path)

    # ── GLib main-thread callbacks ────────────────────────────────────────────

    def _on_generation_done(self, svg_path: str) -> bool:
        self._generated_path = svg_path
        self._enter_idle_state()
        self._show_preview(svg_path)
        self._generate_btn.set_visible(False)
        self._post_gen_box.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _on_generation_error(self, message: str) -> bool:
        self._enter_idle_state()
        dlg = Adw.AlertDialog(heading="Generation failed", body=message)
        dlg.add_response("ok", "OK")
        dlg.set_default_response("ok")
        dlg.set_close_response("ok")
        dlg.present(self._parent_window)
        return GLib.SOURCE_REMOVE

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _show_preview(self, image_path: str) -> None:
        self._preview_box.set_visible(True)
        try:
            from gi.repository import GdkPixbuf, Gdk as _Gdk
            pb = GdkPixbuf.Pixbuf.new_from_file(image_path)
            texture = _Gdk.Texture.new_for_pixbuf(pb)
            self._preview_picture.set_paintable(texture)
        except Exception as e:
            log.warning("Could not load preview for %s: %s", image_path, e)

    def _show_toast(self, message: str, timeout: int = 5) -> None:
        toast = Adw.Toast(title=message)
        toast.set_timeout(timeout)
        self._toast_overlay.add_toast(toast)
