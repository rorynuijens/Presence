"""
ai_image_dialog.py — Generate a single AI image via the Gemini API.

Opened from the image controls, from "Make an image for this slide".
Presents: scene description entry, image style picker, Generate button, preview
thumbnail, and an Insert button that writes the Markdown tag.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from .session import load_api_keys, load_ai_prefs, save_ai_prefs
from .slides.image_gen import IMAGE_STYLE_LABELS, generate_single_image

log = logging.getLogger(__name__)


def _build_alt(desc: str, layout: dict) -> str:
    """Reconstruct the alt token string from a scene description and layout dict."""
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


class AIImageDialog(Adw.Dialog):
    """
    Modal dialog for generating a single slide image via Gemini.

    Lifecycle:
      1. User enters/edits a scene description and picks a style.
      2. "Generate" triggers a background Gemini call; spinner is shown.
      3. On success a 16:9 preview is revealed; "Regenerate" and "Insert" appear.
      4. "Insert" calls insert_cb(alt_tokens, rel_path) and closes the dialog.

    Parameters
    ----------
    parent_window : MainWindow
        Owning window — used to locate the assets/ directory.
    layout : dict
        Image layout tokens from ImageLayoutPopover: {position, size, gradient}.
    insert_cb : (alt: str, rel_path: str) -> None
        Called when the user confirms insertion.  Identical signature to the
        file-picker path so _on_layout_insert in editor.py handles both.
    initial_scene : str
        Pre-filled scene description (from the popover's alt-text entry).
    """

    def __init__(
        self,
        parent_window,
        layout: dict,
        insert_cb,
        initial_scene: str = "",
    ) -> None:
        super().__init__()
        self.set_title("Generate AI Image")
        self.set_content_width(440)
        self._parent_window  = parent_window
        self._layout         = layout
        self._insert_cb      = insert_cb
        self._generated_path: str | None = None
        self._generating     = False

        self._build_ui(initial_scene)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self, initial_scene: str) -> None:
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

        self._scene_row = Adw.EntryRow(title="Scene description")
        self._scene_row.set_text(initial_scene)
        self._scene_row.set_tooltip_text(
            'Describe a concrete visual scene — e.g. '
            '"rush-hour commuters packed onto a Tokyo metro platform"'
        )
        group.add(self._scene_row)

        prefs = load_ai_prefs()
        saved_style = prefs.get("image_style", IMAGE_STYLE_LABELS[0])
        style_idx = (
            IMAGE_STYLE_LABELS.index(saved_style)
            if saved_style in IMAGE_STYLE_LABELS else 0
        )

        self._style_row = Adw.ComboRow(title="Image style")
        self._style_row.set_model(Gtk.StringList.new(IMAGE_STYLE_LABELS))
        self._style_row.set_selected(style_idx)
        group.add(self._style_row)

        # ── Progress ──────────────────────────────────────────────────────────
        self._progress_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        self._progress_box.set_visible(False)

        self._spinner = Gtk.Spinner()
        self._spinner.set_halign(Gtk.Align.CENTER)
        self._progress_box.append(self._spinner)

        self._progress_label = Gtk.Label(label="Generating image…")
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
        # Row 1: Generate (only visible before/during first generation)
        self._generate_btn = Gtk.Button(label="Generate")
        self._generate_btn.add_css_class("suggested-action")
        self._generate_btn.set_halign(Gtk.Align.FILL)
        self._generate_btn.connect("clicked", self._on_generate_clicked)
        content.append(self._generate_btn)

        # Row 2: Regenerate + Insert (visible after successful generation)
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

        scene = self._scene_row.get_text().strip()
        if not scene:
            self._show_toast("Enter a scene description first.")
            return

        _, gemini_key = load_api_keys()
        if not gemini_key:
            self._show_toast("Gemini API key not set — open Settings to add it.")
            return

        if not self._parent_window._file_path:
            self._show_toast(
                "Save the document first — images are written to assets/ "
                "next to the .md file."
            )
            return

        assets_dir = str(self._parent_window._file_path.parent / "assets")
        style = IMAGE_STYLE_LABELS[self._style_row.get_selected()]
        prefs = load_ai_prefs()
        gemini_model = prefs.get("gemini_model", "gemini-2.5-flash-image")

        self._enter_generating_state()

        threading.Thread(
            target=self._run_generation,
            args=(scene, style, gemini_key, gemini_model, assets_dir),
            daemon=True,
        ).start()

    def _on_insert_clicked(self, *_) -> None:
        if not self._generated_path:
            return
        scene = self._scene_row.get_text().strip()
        alt   = _build_alt(scene, self._layout)
        rel   = "assets/" + Path(self._generated_path).name

        # Save the chosen style as the new default
        style = IMAGE_STYLE_LABELS[self._style_row.get_selected()]
        prefs = load_ai_prefs()
        prefs["image_style"] = style
        save_ai_prefs(prefs)

        self._insert_cb(alt, rel)
        self.close()

    # ── State machine ─────────────────────────────────────────────────────────

    def _enter_generating_state(self) -> None:
        self._generating = True
        self._generate_btn.set_sensitive(False)
        self._post_gen_box.set_visible(False)
        self._scene_row.set_sensitive(False)
        self._style_row.set_sensitive(False)
        self._progress_box.set_visible(True)
        self._spinner.start()

    def _enter_idle_state(self) -> None:
        self._generating = False
        self._generate_btn.set_sensitive(True)
        self._scene_row.set_sensitive(True)
        self._style_row.set_sensitive(True)
        self._progress_box.set_visible(False)
        self._spinner.stop()

    # ── Background work ───────────────────────────────────────────────────────

    def _run_generation(
        self, scene: str, style: str,
        gemini_key: str, gemini_model: str, output_dir: str,
    ) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                path = loop.run_until_complete(
                    generate_single_image(
                        gemini_key, scene, output_dir, style, gemini_model
                    )
                )
            finally:
                loop.close()
                asyncio.set_event_loop(None)
        except Exception as e:
            log.warning("AI image generation failed: %s", e)
            GLib.idle_add(self._on_generation_error, str(e))
            return

        GLib.idle_add(self._on_generation_done, path)

    # ── GLib main-thread callbacks ────────────────────────────────────────────

    def _on_generation_done(self, image_path: str) -> bool:
        self._generated_path = image_path
        self._enter_idle_state()
        self._show_preview(image_path)
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
        try:
            from gi.repository import GdkPixbuf, Gdk as _Gdk
            pb = GdkPixbuf.Pixbuf.new_from_file(image_path)
            texture = _Gdk.Texture.new_for_pixbuf(pb)
            self._preview_picture.set_paintable(texture)
            self._preview_box.set_visible(True)
        except Exception as e:
            log.warning("Could not load preview for %s: %s", image_path, e)

    def _show_toast(self, message: str, timeout: int = 5) -> None:
        toast = Adw.Toast(title=message)
        toast.set_timeout(timeout)
        self._toast_overlay.add_toast(toast)
