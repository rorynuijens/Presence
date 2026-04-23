"""
presenter.py — Presenter mode window + companion slideshow window.
"""

import logging

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, GdkPixbuf, Gdk, GObject, Pango

log = logging.getLogger(__name__)

from .app_utils import png_bytes_to_texture

# Imported at module level so _set_notes does not pay import overhead
# on every slide navigation keystroke.
try:
    from md_to_slides.renderer import render_slide_content as _render_slide_content
except ImportError:
    _render_slide_content = None

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
    _WEBKIT = True
    _WEBKIT_VERSION = 6
except (ValueError, ImportError):
    try:
        gi.require_version("WebKit2", "4.1")
        from gi.repository import WebKit2 as WebKit
        _WEBKIT = True
        _WEBKIT_VERSION = 4
    except (ValueError, ImportError):
        _WEBKIT = False
        _WEBKIT_VERSION = None


def _make_show_slide_js(index: int) -> str:
    return f"""
    (function() {{
        var slides = document.querySelectorAll('.slide');
        var target = null;
        slides.forEach(function(s, i) {{
            if (i === {index}) {{
                s.style.display = 'flex';
                target = s;
            }} else {{
                s.style.display = 'none';
            }}
        }});
        document.body.style.margin     = '0';
        document.body.style.padding    = '0';
        document.body.style.background = '#000';
        document.documentElement.style.background = '#000';
        document.documentElement.style.margin  = '0';
        document.documentElement.style.padding = '0';
        if (target) {{
            var vw    = window.innerWidth;
            var vh    = window.innerHeight;
            var sw    = target.offsetWidth  || 1280;
            var sh    = target.offsetHeight || 720;
            var scale = Math.min(vw / sw, vh / sh);
            target.style.transformOrigin = 'top left';
            target.style.transform = 'scale(' + scale + ')';
            target.style.position  = 'absolute';
            target.style.top  = Math.max(0, (vh - sh * scale) / 2) + 'px';
            target.style.left = Math.max(0, (vw - sw * scale) / 2) + 'px';
        }}
    }})();
    """


def _make_webkit_view(allow_local: bool = True) -> "WebKit.WebView | None":
    if not _WEBKIT:
        return None
    settings = WebKit.Settings()
    settings.set_allow_file_access_from_file_urls(allow_local)
    # JavaScript IS required in presenter/slideshow views — slide navigation
    # is driven by evaluate_javascript() calls from Python.
    settings.set_enable_javascript(True)
    if _WEBKIT_VERSION == 6:
        ns = WebKit.NetworkSession.new_ephemeral()
        view = WebKit.WebView(settings=settings, network_session=ns)
    else:
        ctx = WebKit.WebContext.new_ephemeral()
        view = WebKit.WebView.new_with_context(ctx)
        view.set_settings(settings)
    return view


# ── SlideshowWindow ───────────────────────────────────────────────────────────

class SlideshowWindow(Adw.Window):
    """
    Chromeless fullscreen window that shows a single slide (audience view).

    Monitor placement on Wayland
    ----------------------------
    Wayland does not allow applications to programmatically choose which
    output a window appears on.  The only reliable approach is:

      1. Present the window (it appears on whatever output the compositor
         chooses, typically the most recently focused one).
      2. Use Gtk.Window.fullscreen() — NOT fullscreen_on_monitor(), which
         is X11-only and silently does nothing on Wayland.
      3. Provide a "Move to other screen" action the presenter can trigger
         if the slideshow landed on the wrong monitor.

    When the slideshow window is first shown we attempt to position it on
    the monitor that does NOT contain the presenter window, by moving the
    window to the centre of that monitor's geometry before fullscreening.
    This is a best-effort hint to the compositor and is not guaranteed, but
    works reliably on most Wayland compositors (Mutter/GNOME Shell).
    """

    def __init__(self, html_uri: str, n_slides: int,
                 presenter_window: "PresenterWindow",
                 parent_window, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("Presence — Slideshow")
        self.set_default_size(1280, 720)
        # Not transient — a transient window is constrained to its parent's
        # output on some compositors, which prevents multi-monitor placement.
        # We set application instead so it still appears in the task switcher.

        self._html_uri         = html_uri
        self._n_slides         = n_slides
        self._presenter        = presenter_window
        self._html_loaded      = False
        self._pending          = 0
        self._closing              = False   # True when close_by_presenter() called
        self._monitors_handler     = None    # GLib signal handler id

        if _WEBKIT:
            self._view = _make_webkit_view()
            self._view.set_hexpand(True)
            self._view.set_vexpand(True)
            self._load_id = self._view.connect(
                "load-changed", self._on_load_changed
            )
            self.set_content(self._view)
        else:
            lbl = Gtk.Label(
                label="WebKit not available — cannot show slideshow"
            )
            lbl.set_hexpand(True)
            lbl.set_vexpand(True)
            self.set_content(lbl)
            self._view    = None
            self._load_id = None

        # Connect to monitor list changes. Per GTK docs we need BOTH:
        #   GListModel::items-changed  — monitor added or removed from list
        #   GdkMonitor::invalidate     — individual monitor going away
        monitors_model = Gdk.Display.get_default().get_monitors()
        self._monitors_handler = monitors_model.connect(
            "items-changed", self._on_monitors_changed
        )
        self._monitors_model = monitors_model   # keep a reference
        for i in range(monitors_model.get_n_items()):
            try:
                mon = monitors_model.get_item(i)
                if mon is not None:
                    mon.connect("invalidate", self._on_monitor_invalidate)
            except Exception:
                pass

        # Defer fullscreen placement so the window surface exists first.
        # Must be idle_add (not direct call) so the window is realized
        # and has a Wayland surface before we try to fullscreen it.
        GLib.idle_add(self._place_on_other_monitor)

    # ── Monitor placement ─────────────────────────────────────────────────────

    def _on_monitors_changed(self, model, pos, removed, added) -> None:
        """
        Called when a monitor is connected or removed (GListModel::items-changed).

        We intentionally do NOT attempt to re-seat the slideshow window here.
        Calling unfullscreen()/fullscreen() while a WebKit view is rendering
        races with the GPU subprocess and causes SIGABRT — this is the root
        cause of all hotplug crashes (LibreOffice Impress has the same bug,
        see RHBZ #1386948/#1390607).

        What we do instead:
          - Connect GdkMonitor::invalidate on any newly added monitors.
            Per GTK lead developer Emmanuele Bassi, BOTH signals are needed:
            items-changed for add/remove, invalidate per-monitor for disconnect.
          - Let the PresenterWindow indicator update separately.
          - Let the user use the swap button to move the window manually.
        """
        if self._closing:
            return
        for i in range(added):
            try:
                mon = model.get_item(pos + i)
                if mon is not None:
                    mon.connect("invalidate", self._on_monitor_invalidate)
            except Exception:
                pass

    def _on_monitor_invalidate(self, monitor) -> None:
        """
        Called when a GdkMonitor is about to be removed (GdkMonitor::invalidate).
        Fires before items-changed. We do nothing — just log for diagnostics.
        The user uses the swap button if the remaining monitor needs reseating.
        """
        if self._closing:
            return
        log.debug("Monitor disconnected: %s", monitor.get_connector()
                  if hasattr(monitor, "get_connector") else monitor)

    def _place_on_other_monitor(self) -> bool:
        """
        Place the slideshow on the monitor that does NOT contain the
        presenter window, then fullscreen it. Called once at startup.

        Wayland strategy (GNOME Shell / Mutter)
        ----------------------------------------
        Wayland does not let applications choose an output directly.
        GNOME Shell fullscreens a window on the output that contains
        the majority of the window's current geometry, so the sequence is:
          1. Set default size to match the target monitor geometry.
          2. Wait one compositor frame (~50 ms) for the size hint to land.
          3. Call fullscreen() — compositor seats it on the correct output.

        X11 strategy
        ------------
        fullscreen_on_monitor() is reliable and used directly.
        """
        if self._closing:
            return GLib.SOURCE_REMOVE

        display  = Gdk.Display.get_default()
        monitors = display.get_monitors()
        n        = monitors.get_n_items()

        if n < 2:
            # Single monitor — fullscreen on whatever output we have
            self.fullscreen()
            return GLib.SOURCE_REMOVE

        # Find which monitor the presenter window is on
        presenter_monitor = None
        if self._presenter is not None:
            try:
                psurface = self._presenter.get_surface()
                if psurface is not None:
                    presenter_monitor = display.get_monitor_at_surface(psurface)
            except Exception:
                pass

        # Pick the first monitor that is not the presenter's
        target_monitor = None
        for i in range(n):
            m = monitors.get_item(i)
            if m is not presenter_monitor:
                target_monitor = m
                break

        if target_monitor is None:
            self.fullscreen()
            return GLib.SOURCE_REMOVE

        # Detect X11 vs Wayland
        is_x11 = False
        try:
            from gi.repository import GdkX11
            is_x11 = isinstance(display, GdkX11.X11Display)
        except ImportError:
            pass

        if is_x11:
            try:
                self.fullscreen_on_monitor(target_monitor)
            except (AttributeError, TypeError):
                self.fullscreen()
        else:
            # Wayland: hint size = target monitor geometry, then fullscreen
            # after one frame so the compositor maps us on the right output.
            geom = target_monitor.get_geometry()
            self.set_default_size(geom.width, geom.height)
            GLib.timeout_add(50, self._do_fullscreen)

        return GLib.SOURCE_REMOVE

    def _do_fullscreen(self) -> bool:
        """Fullscreen the slideshow (used for initial placement only)."""
        if self._closing or not self.get_visible():
            return GLib.SOURCE_REMOVE
        try:
            self.fullscreen()
        except Exception as e:
            log.debug("fullscreen() raised: %s", e)
        return GLib.SOURCE_REMOVE

    def move_to_other_monitor(self) -> None:
        """
        Move the slideshow to the other monitor (triggered by swap button).

        Unfullscreens, waits 200 ms for the compositor to finish surface
        teardown, then re-fullscreens. GNOME Shell cycles the window to the
        next output on each unfullscreen/fullscreen cycle.

        The 200 ms delay is intentional — it gives the WebKit GPU process
        time to complete any in-progress frame before the surface is
        reconfigured. 150 ms was insufficient on some hardware.
        """
        if self._closing or self._view is None:
            return
        log.debug("Moving slideshow to other monitor")
        try:
            self.unfullscreen()
            GLib.timeout_add(200, self._do_fullscreen)
        except Exception as e:
            log.warning("move_to_other_monitor failed: %s", e)

    # ── Load / slide control ──────────────────────────────────────────────────

    def _on_load_changed(self, view, load_event) -> None:
        finished = WebKit.LoadEvent.FINISHED if hasattr(WebKit, "LoadEvent") else 3
        if load_event == finished:
            self._html_loaded = True
            if self._load_id is not None:
                self._view.disconnect(self._load_id)
                self._load_id = None
            self._run_js(self._pending)

    def load(self) -> None:
        if self._view is not None:
            self._view.load_uri(self._html_uri)

    def show_slide(self, index: int) -> None:
        index = max(0, min(index, self._n_slides - 1))
        self._pending = index
        if self._html_loaded and self._view is not None:
            self._run_js(index)

    def _run_js(self, index: int) -> None:
        """Run slide-navigation JS.  Delegates to _run_js_safe."""
        self._run_js_safe(_make_show_slide_js(index))

    def _run_blank_js(self, js: str) -> None:
        """Run arbitrary JS.  Delegates to _run_js_safe."""
        self._run_js_safe(js)

    def _run_js_safe(self, js: str) -> None:
        """
        Execute *js* on the WebView with full safety guards:
          - _closing flag checked first (set synchronously by close_by_presenter)
          - get_realized() guards against a destroyed Wayland surface
        Only catches Exception, not BaseException, so SIGABRT propagates.
        """
        if self._closing or self._view is None:
            return
        try:
            if not self._view.get_realized():
                return
        except Exception:
            return
        try:
            self._view.evaluate_javascript(js, -1, None, None, None, None)
        except Exception:
            pass   # WebKit GPU subprocess may be shutting down

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close_by_presenter(self) -> None:
        """
        Tear down this window.  Must be called by PresenterWindow.
        Sets _closing=True FIRST so all pending timeout callbacks
        return immediately when they fire.
        """
        self._closing = True          # guards all pending callbacks
        self._presenter = None        # break reference cycle
        # Disconnect monitor handler synchronously so no further
        # _on_monitors_changed calls can be queued.
        if self._monitors_handler is not None:
            try:
                self._monitors_model.disconnect(self._monitors_handler)
            except Exception:
                pass
            self._monitors_handler = None
        # Give WebKit a clean shutdown — try_close() tells the GPU
        # subprocess to exit gracefully rather than being SIGKILL'd.
        if self._view is not None:
            try:
                self._view.try_close()
            except AttributeError:
                pass
            self._view = None         # prevent any further _run_js calls
        self.destroy()

    def do_close_request(self) -> bool:
        if self._closing:
            return False   # presenter is driving the close — allow destroy
        # User closed via taskbar: hide instead of destroying so the
        # presenter can still drive the slideshow (fixes #70).
        self.hide()
        return True


# ── PresenterWindow ───────────────────────────────────────────────────────────

class PresenterWindow(Adw.Window):
    """Full-screen presenter view (main screen)."""

    __gsignals__ = {
        # Emitted every second with (elapsed_secs, target_secs)
        # so MainWindow can show a live timer in its status bar.
        "timer-tick": (
            GObject.SignalFlags.RUN_FIRST, None, (int, int)
        ),
    }

    # Maximum number of dots shown in the progress bar. Beyond this the
    # dots compress so the bar always fits without overflow.
    _PROGRESS_MAX_DOTS = 60

    def __init__(self, html_uri: str, slide_info: list[dict],
                 thumbnails: list, parent_window, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("Presence — Presenter Mode")
        self.set_default_size(1280, 800)
        self.set_transient_for(parent_window)

        self._html_uri    = html_uri
        self._slide_info  = slide_info
        self._thumbnails  = thumbnails
        self._current     = 0
        self._n_slides    = len(slide_info)
        self._elapsed     = 0
        self._timer_src   = None
        self._blank_color: str = ""   # "" = not blanked, "black" or "white" (#91)
        self._monitors_handler = None
        self._monitors_model   = None
        # Target duration in minutes (0 = count-up only), read from window prefs
        self._target_secs: int = getattr(parent_window, '_timer_minutes', 0) * 60

        # Notes font size — persisted between sessions
        self._notes_font_size: int = getattr(
            parent_window, '_presenter_notes_font', 22
        )
        self._parent_window = parent_window

        self._build_ui()

        self._slideshow = SlideshowWindow(
            html_uri=html_uri,
            n_slides=self._n_slides,
            presenter_window=self,
            parent_window=parent_window,
        )
        self._slideshow.present()
        self._slideshow.load()

        key_ctrl = Gtk.EventControllerKey()
        # CAPTURE phase: intercept key events before they reach the
        # WebKit notes view, which would otherwise consume them.
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key)
        self.add_controller(key_ctrl)

        # Defer initial slide and timer until after the first layout pass
        # so all Picture widgets have been allocated before we try to
        # paint thumbnails into them (prevents GtkGizmo snapshot warning).
        GLib.idle_add(self._enter_fullscreen)
        GLib.idle_add(self._deferred_start)

    def _enter_fullscreen(self) -> bool:
        # Maximize the presenter window rather than fullscreening —
        # fullscreen hides the window from the taskbar making it
        # impossible to restore after minimizing.  The presenter
        # window needs to remain accessible at all times.
        self.maximize()
        return GLib.SOURCE_REMOVE

    def _deferred_start(self) -> bool:
        """Populate the first slide and start the timer after allocation."""
        self._go_to(0)
        self._start_timer()
        return GLib.SOURCE_REMOVE

    def _build_ui(self) -> None:
        """
        Build the teleprompter-style presenter layout.

        Layout:
          Top bar  : [← slide →] [counter]  [timer] [+/-] [blank] [✕]
          Body     : [left column]  |  [notes area]
                      current thumb      slide title (bold)
                      next thumb         notes text (large, readable)
          No bottom bar — all controls are in the top bar.

        The notes area takes ~70% of the width.  The left column holds the
        two thumbnails and the slide counter.  The entire window background
        is forced dark so the presenter's eyes are not strained.
        """
        # ── Force dark background on this window only ────────────────────────
        # Use add_provider_for_display ONLY for classes that must be dark
        # (presenter-left, presenter-topbar etc.) — NOT for 'window' which
        # would darken every other window on the display.
        # The window background is set via a name-scoped rule instead.
        self.set_name("presenter-window")
        css = Gtk.CssProvider()
        _dark = (
            "#presenter-window { background: #111111; color: #e8e8e8; }"
            ".presenter-left { background: #1a1a1a; border-right: 1px solid #333; }"
            ".notes-title { color: #ffffff; font-weight: bold; }"
            ".notes-hint { color: #666666; }"
            ".thumb-label { color: #888888; font-size: 11px; }"
            ".counter-label { color: #aaaaaa; }"
            ".presenter-topbar { background: #0d0d0d; "
            "  border-bottom: 1px solid #2a2a2a; }"
        )
        try:
            css.load_from_string(_dark)
        except AttributeError:
            css.load_from_data(_dark.encode())
        # Scope to this widget only — never pollute other windows
        self.get_style_context().add_provider(
            css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(root)

        # ── Top bar ───────────────────────────────────────────────────────────
        topbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        topbar.add_css_class("presenter-topbar")
        topbar.set_margin_start(12)
        topbar.set_margin_end(12)
        topbar.set_margin_top(6)
        topbar.set_margin_bottom(6)

        # Left zone: navigation
        prev_btn = Gtk.Button()
        prev_btn.set_child(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        prev_btn.add_css_class("flat")
        prev_btn.set_tooltip_text("Previous slide (←)")
        prev_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Previous slide"])
        prev_btn.connect("clicked", lambda *_: self._prev())
        topbar.append(prev_btn)

        self._counter_label = Gtk.Label(label="1 / 1")
        self._counter_label.set_width_chars(7)
        self._counter_label.add_css_class("counter-label")
        self._counter_label.add_css_class("monospace")
        topbar.append(self._counter_label)

        # Progress dot bar — one dot per slide, filled up to current position.
        # Drawn via Cairo so it scales cleanly and costs nothing at runtime.
        # Capped at _PROGRESS_MAX_DOTS dots; beyond that the bar compresses
        # evenly so it always fits in the top bar without wrapping.
        self._progress_bar = Gtk.DrawingArea()
        self._progress_bar.set_content_height(20)
        self._progress_bar.set_hexpand(True)
        self._progress_bar.set_valign(Gtk.Align.CENTER)
        self._progress_bar.set_tooltip_text("Slide progress")
        self._progress_bar.set_draw_func(self._draw_progress)
        topbar.append(self._progress_bar)

        next_btn = Gtk.Button()
        next_btn.set_child(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        next_btn.add_css_class("flat")
        next_btn.set_tooltip_text("Next slide (→)")
        next_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Next slide"])
        next_btn.connect("clicked", lambda *_: self._next())
        topbar.append(next_btn)

        # Monitor indicator (left zone, after nav)
        self._monitor_icon = Gtk.Image.new_from_icon_name("video-display-symbolic")
        self._monitor_icon.set_tooltip_text("Checking for second monitor…")
        self._monitor_icon.set_margin_start(8)
        topbar.append(self._monitor_icon)

        # Centre spacer → timer
        spacer_l = Gtk.Box()
        spacer_l.set_hexpand(True)
        topbar.append(spacer_l)

        # Timer — large, centred, readable from across the room
        timer_icon = Gtk.Image.new_from_icon_name("alarm-symbolic")
        topbar.append(timer_icon)

        self._timer_label = Gtk.Label(label="00:00")
        self._timer_label.add_css_class("monospace")
        self._timer_label.add_css_class("title-2")
        self._timer_label.set_width_chars(7)
        self._timer_label.set_margin_start(4)
        self._timer_label.set_margin_end(4)
        topbar.append(self._timer_label)

        reset_btn = Gtk.Button()
        reset_btn.set_child(Gtk.Image.new_from_icon_name("view-refresh-symbolic"))
        reset_btn.set_tooltip_text("Reset timer")
        reset_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Reset timer"])
        reset_btn.add_css_class("flat")
        reset_btn.connect("clicked", self._reset_timer)
        topbar.append(reset_btn)

        spacer_r = Gtk.Box()
        spacer_r.set_hexpand(True)
        topbar.append(spacer_r)

        # Right zone: font size, blank, close
        font_down_btn = Gtk.Button()
        font_down_btn.set_child(
            Gtk.Image.new_from_icon_name("zoom-out-symbolic")
        )
        font_down_btn.set_tooltip_text("Decrease notes font size")
        font_down_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Decrease notes font size"]
        )
        font_down_btn.add_css_class("flat")
        font_down_btn.connect("clicked", self._on_font_decrease)
        topbar.append(font_down_btn)

        font_up_btn = Gtk.Button()
        font_up_btn.set_child(
            Gtk.Image.new_from_icon_name("zoom-in-symbolic")
        )
        font_up_btn.set_tooltip_text("Increase notes font size")
        font_up_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Increase notes font size"]
        )
        font_up_btn.add_css_class("flat")
        font_up_btn.connect("clicked", self._on_font_increase)
        topbar.append(font_up_btn)

        # Swap screens: move slideshow to the other monitor
        swap_btn = Gtk.Button()
        swap_btn.set_child(
            Gtk.Image.new_from_icon_name("video-joined-displays-symbolic")
        )
        swap_btn.set_tooltip_text("Move slideshow to other screen")
        swap_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Move slideshow to other screen"]
        )
        swap_btn.add_css_class("flat")
        swap_btn.connect("clicked", lambda *_: self._swap_screens())
        topbar.append(swap_btn)

        blank_btn = Gtk.Button()
        blank_btn.set_child(
            Gtk.Image.new_from_icon_name("display-projector-symbolic")
        )
        blank_btn.set_tooltip_text("Blank audience screen (B)")
        blank_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Blank audience screen"]
        )
        blank_btn.add_css_class("flat")
        blank_btn.connect("clicked", lambda *_: self._toggle_blank("black"))
        topbar.append(blank_btn)

        close_btn = Gtk.Button(label="End")
        close_btn.set_tooltip_text("End presentation (Escape)")
        close_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["End presentation"]
        )
        close_btn.add_css_class("destructive-action")
        close_btn.connect("clicked", lambda *_: self.close())
        topbar.append(close_btn)

        root.append(topbar)

        # ── Body: left column + notes area ────────────────────────────────────
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_vexpand(True)
        body.set_hexpand(True)
        root.append(body)

        # ── Left column: thumbnails + slide counter ───────────────────────────
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left.add_css_class("presenter-left")
        left.set_size_request(240, -1)
        left.set_vexpand(True)
        body.append(left)

        # Current slide thumbnail
        cur_lbl = Gtk.Label(label="Current slide")
        cur_lbl.add_css_class("thumb-label")
        cur_lbl.set_xalign(0)
        cur_lbl.set_margin_top(12)
        cur_lbl.set_margin_start(12)
        cur_lbl.set_margin_bottom(4)
        left.append(cur_lbl)

        self._current_picture = Gtk.Picture()
        self._current_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._current_picture.set_size_request(216, 122)
        self._current_picture.set_margin_start(12)
        self._current_picture.set_margin_end(12)
        self._current_picture.add_css_class("card")
        left.append(self._current_picture)

        # Separator
        left.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Next slide thumbnail
        next_lbl = Gtk.Label(label="Next slide")
        next_lbl.add_css_class("thumb-label")
        next_lbl.set_xalign(0)
        next_lbl.set_margin_top(12)
        next_lbl.set_margin_start(12)
        next_lbl.set_margin_bottom(4)
        left.append(next_lbl)

        self._next_picture = Gtk.Picture()
        self._next_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._next_picture.set_size_request(216, 122)
        self._next_picture.set_margin_start(12)
        self._next_picture.set_margin_end(12)
        self._next_picture.add_css_class("card")
        left.append(self._next_picture)

        # Spacer to push counter to bottom
        left_spacer = Gtk.Box()
        left_spacer.set_vexpand(True)
        left.append(left_spacer)

        left.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # ── Notes area: title + large scrollable notes ────────────────────────
        notes_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        notes_area.set_hexpand(True)
        notes_area.set_vexpand(True)
        body.append(notes_area)

        # Slide title — bold, prominent, readable
        self._slide_title_label = Gtk.Label(label="")
        self._slide_title_label.add_css_class("notes-title")
        self._slide_title_label.add_css_class("title-3")
        self._slide_title_label.set_xalign(0)
        self._slide_title_label.set_wrap(True)
        self._slide_title_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._slide_title_label.set_margin_top(20)
        self._slide_title_label.set_margin_start(28)
        self._slide_title_label.set_margin_end(28)
        self._slide_title_label.set_margin_bottom(12)
        notes_area.append(self._slide_title_label)

        notes_area.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Notes — WebKit for Markdown rendering, falls back to TextView
        notes_scroll = Gtk.ScrolledWindow()
        notes_scroll.set_vexpand(True)
        notes_scroll.set_hexpand(True)
        notes_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        if _WEBKIT:
            notes_settings = WebKit.Settings()
            notes_settings.set_enable_javascript(False)
            notes_settings.set_allow_file_access_from_file_urls(False)
            if _WEBKIT_VERSION == 6:
                ns = WebKit.NetworkSession.new_ephemeral()
                self._notes_view = WebKit.WebView(
                    settings=notes_settings, network_session=ns
                )
            else:
                ctx = WebKit.WebContext.new_ephemeral()
                self._notes_view = WebKit.WebView.new_with_context(ctx)
                self._notes_view.set_settings(notes_settings)
            self._notes_view.set_vexpand(True)
            self._notes_view.set_hexpand(True)
            notes_scroll.set_child(self._notes_view)
        else:
            self._notes_view = Gtk.TextView()
            self._notes_view.set_editable(False)
            self._notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            self._notes_view.set_top_margin(20)
            self._notes_view.set_left_margin(28)
            self._notes_view.set_right_margin(28)
            self._notes_view.add_css_class("body")
            notes_scroll.set_child(self._notes_view)

        notes_area.append(notes_scroll)

        GLib.idle_add(self._update_monitor_indicator)

        # Track monitor changes to update the indicator — store handler
        # id so we can disconnect it safely when the window closes.
        monitors_model = Gdk.Display.get_default().get_monitors()
        self._monitors_model   = monitors_model
        self._monitors_handler = monitors_model.connect(
            "items-changed", self._on_monitors_changed_presenter
        )

    def _on_monitors_changed_presenter(self, model, pos, removed, added) -> None:
        """Update the monitor indicator when display topology changes."""
        GLib.idle_add(self._update_monitor_indicator)

    def _update_monitor_indicator(self) -> bool:
        display  = Gdk.Display.get_default()
        monitors = display.get_monitors()
        n        = monitors.get_n_items()
        if n >= 2:
            self._monitor_icon.set_from_icon_name("video-display-symbolic")
            self._monitor_icon.set_tooltip_text(
                f"Slideshow on second monitor ({n} screens detected)"
            )
        else:
            self._monitor_icon.set_from_icon_name("dialog-warning-symbolic")
            self._monitor_icon.set_tooltip_text(
                "No second monitor detected — slideshow window is on the same screen"
            )
        return GLib.SOURCE_REMOVE

    def _go_to(self, index: int) -> None:
        if not self._slide_info:
            return
        index = max(0, min(index, self._n_slides - 1))
        self._current = index

        # Drive the audience screen through SlideshowWindow — the
        # presenter screen shows thumbnails and notes, not a WebKit view.
        self._slideshow.show_slide(index)

        # Current slide thumbnail
        if index < len(self._thumbnails) and self._thumbnails[index]:
            self._set_thumbnail(self._current_picture, self._thumbnails[index])
        else:
            self._current_picture.set_paintable(None)

        # Next slide thumbnail
        next_idx = index + 1
        if next_idx < len(self._thumbnails) and self._thumbnails[next_idx]:
            self._set_thumbnail(self._next_picture, self._thumbnails[next_idx])
        else:
            self._next_picture.set_paintable(None)

        # Slide title above notes
        title = self._slide_info[index].get("title", f"Slide {index + 1}")
        self._slide_title_label.set_label(title)

        # Render speaker notes as Markdown HTML (#73)
        notes_md = self._slide_info[index].get("notes", "")
        self._set_notes(notes_md)

        self._counter_label.set_text(f"{index + 1} / {self._n_slides}")
        # Guard against drawing before the widget has been allocated
        if self._progress_bar.get_realized():
            self._progress_bar.queue_draw()

    def _draw_progress(self, area: Gtk.DrawingArea, cr,
                       width: int, height: int) -> None:
        """
        Cairo draw function for the slide progress bar.

        Renders one dot per slide in a single horizontal row.  The current
        slide and all previous slides are filled with the accent colour;
        future slides are drawn as outlines.  When the deck has more slides
        than _PROGRESS_MAX_DOTS the dots compress to fit, otherwise each dot
        is 8 px wide with 4 px gaps.

        Designed to be readable at a glance from 2–3 metres:
          ● ● ● ○ ○ ○ ○ ○   ← clear at-a-glance progress
        """
        n = self._n_slides
        if n < 2:
            return   # single-slide deck — no progress bar needed

        # ── Geometry ──────────────────────────────────────────────────────────
        n_dots   = min(n, self._PROGRESS_MAX_DOTS)
        # Dot diameter: 8 px normally, compressed if many slides
        dot_d    = min(8, max(4, (width - 8) // (n_dots * 2)))
        gap      = max(2, dot_d // 2)
        total_w  = n_dots * dot_d + (n_dots - 1) * gap
        x0       = (width - total_w) / 2
        cy       = height / 2
        r        = dot_d / 2

        # ── Colours from GTK style context ────────────────────────────────────
        # Use the Adwaita accent colour so the bar inherits any user accent.
        style = self.get_style_context()
        # Filled dots: accent colour (bright, prominent)
        fg_rgba = style.lookup_color("accent_bg_color")
        if fg_rgba[0]:   # (found, Gdk.RGBA)
            fc = fg_rgba[1]
            fill_r, fill_g, fill_b, fill_a = fc.red, fc.green, fc.blue, 1.0
        else:
            fill_r, fill_g, fill_b, fill_a = 0.85, 0.85, 0.85, 1.0

        # Empty dots: dim colour
        dim_rgba = style.lookup_color("window_fg_color")
        if dim_rgba[0]:
            dc = dim_rgba[1]
            dim_r, dim_g, dim_b = dc.red, dc.green, dc.blue
        else:
            dim_r, dim_g, dim_b = 0.5, 0.5, 0.5

        import math

        # When n > _PROGRESS_MAX_DOTS each dot represents multiple slides.
        # The "filled" boundary is at the proportional position.
        filled_dots = round((self._current + 1) / n * n_dots)

        for i in range(n_dots):
            cx = x0 + i * (dot_d + gap) + r
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            if i < filled_dots:
                cr.set_source_rgba(fill_r, fill_g, fill_b, fill_a)
                cr.fill()
            else:
                cr.set_source_rgba(dim_r, dim_g, dim_b, 0.4)
                cr.arc(cx, cy, r - 0.5, 0, 2 * math.pi)
                cr.set_line_width(1.0)
                cr.stroke()

    def _set_notes(self, notes_md: str) -> None:
        """Render *notes_md* as Markdown HTML into the notes view (#73)."""
        import html as _html
        fs = self._notes_font_size
        if _WEBKIT and hasattr(self._notes_view, "load_html"):
            try:
                if notes_md.strip() and _render_slide_content is not None:
                    html_body = _render_slide_content(notes_md)
                elif notes_md.strip():
                    # Escape raw notes so a slide containing </pre><script>…
                    # cannot inject HTML into the notes WebKit view.
                    html_body = f"<pre>{_html.escape(notes_md)}</pre>"
                else:
                    html_body = (
                        "<p style='color:#555555;font-style:italic'>"
                        "No speaker notes for this slide.</p>"
                    )
                html = (
                    "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
                    f"<style>"
                    f"body{{font-family:sans-serif;font-size:{fs}px;"
                    f"line-height:1.65;padding:20px 28px;margin:0;"
                    f"color:#e8e8e8;background:#111111}}"
                    f"h1,h2,h3{{color:#ffffff;margin-top:0.8em}}"
                    f"strong{{color:#ffffff}}"
                    f"em{{color:#aaaaaa}}"
                    f"code{{background:#2a2a2a;color:#88ccff;"
                    f"padding:2px 6px;border-radius:3px}}"
                    f"ul,ol{{padding-left:1.4em}}"
                    f"li{{margin-bottom:0.3em}}"
                    f"blockquote{{border-left:3px solid #444;"
                    f"margin-left:0;padding-left:1em;color:#aaaaaa}}"
                    f"</style></head>"
                    f"<body>{html_body}</body></html>"
                )
                self._notes_view.load_html(html, "about:blank")
            except Exception:
                # Exception fallback: also escape to be safe.
                self._notes_view.load_html(
                    f"<html><body style='background:#111;color:#e8e8e8;"
                    f"font-family:sans-serif;font-size:{fs}px;padding:20px'>"
                    f"<pre style='white-space:pre-wrap'>"
                    f"{_html.escape(notes_md)}</pre>"
                    f"</body></html>",
                    "about:blank",
                )
        else:
            buf = self._notes_view.get_buffer()
            buf.set_text(
                notes_md if notes_md.strip()
                else "No speaker notes for this slide."
            )

    def _next(self) -> None:
        self._go_to(self._current + 1)

    def _prev(self) -> None:
        self._go_to(self._current - 1)

    def _toggle_blank(self, color: str) -> None:
        """Toggle a solid black or white screen on the slideshow (#91).

        Pressing B blanks to black, W to white; pressing the same key again
        restores the slide.  The slideshow window shows the blank; the
        presenter view keeps showing the slide content.
        """
        if self._blank_color == color:
            # Un-blank: restore current slide
            self._blank_color = ""
            self._slideshow.show_slide(self._current)
        else:
            self._blank_color = color
            bg = "#000000" if color == "black" else "#ffffff"
            # Use a lookup rather than f-string interpolation to ensure
            # the colour value can never be anything other than a safe hex.
            js = (
                "(function(){"
                "document.querySelectorAll('.slide').forEach(s=>s.style.display='none');"
                f"document.body.style.background='{bg}';"
                f"document.documentElement.style.background='{bg}';"
                "})()"
            )
            # Route through _run_blank_js which has the same safety
            # guards as _run_js — never call evaluate_javascript directly.
            self._slideshow._run_blank_js(js)

    def _start_timer(self) -> None:
        self._timer_src = GLib.timeout_add_seconds(1, self._tick)

    def _tick(self) -> bool:
        self._elapsed += 1

        if self._target_secs > 0:
            # Countdown mode: show time remaining, negative when overrun
            remaining = self._target_secs - self._elapsed
            sign = "-" if remaining < 0 else ""
            m, s = divmod(abs(remaining), 60)
            h, m = divmod(m, 60)
            if h:
                self._timer_label.set_text(f"{sign}{h:02d}:{m:02d}:{s:02d}")
            else:
                self._timer_label.set_text(f"{sign}{m:02d}:{s:02d}")
            # Warn at 5 min remaining, alert when overrun
            warn_secs  = self._target_secs - 5 * 60
            alert_secs = self._target_secs
        else:
            # Count-up mode with fixed thresholds
            m, s = divmod(self._elapsed, 60)
            h, m = divmod(m, 60)
            if h:
                self._timer_label.set_text(f"{h:02d}:{m:02d}:{s:02d}")
            else:
                self._timer_label.set_text(f"{m:02d}:{s:02d}")
            warn_secs  = 25 * 60
            alert_secs = 35 * 60

        # Colour coding: yellow warning, red alert
        if self._elapsed >= alert_secs:
            self._timer_label.add_css_class("error")
            self._timer_label.remove_css_class("warning")
        elif self._elapsed >= warn_secs:
            self._timer_label.add_css_class("warning")
            self._timer_label.remove_css_class("error")
        else:
            self._timer_label.remove_css_class("warning")
            self._timer_label.remove_css_class("error")

        # Notify MainWindow so it can show the timer in its status bar
        self.emit("timer-tick", self._elapsed, self._target_secs)
        return GLib.SOURCE_CONTINUE


    def _swap_screens(self) -> None:
        """Move the slideshow to the other monitor."""
        self._slideshow.move_to_other_monitor()

    def _on_font_increase(self, *_) -> None:
        self._notes_font_size = min(self._notes_font_size + 2, 40)
        self._persist_notes_font()
        self._rerender_notes()

    def _on_font_decrease(self, *_) -> None:
        self._notes_font_size = max(self._notes_font_size - 2, 14)
        self._persist_notes_font()
        self._rerender_notes()

    def _rerender_notes(self) -> None:
        """Re-render the current slide's notes at the current font size."""
        if self._slide_info:
            self._set_notes(self._slide_info[self._current].get("notes", ""))

    def _persist_notes_font(self) -> None:
        """Save the notes font size to the window and to session.json."""
        try:
            if self._parent_window is not None:
                self._parent_window._presenter_notes_font = self._notes_font_size
            from .session import load_presentation_prefs, save_presentation_prefs
            prefs = load_presentation_prefs()
            prefs['presenter_notes_font'] = self._notes_font_size
            save_presentation_prefs(prefs)
        except Exception:
            pass

    def _reset_timer(self, *_) -> None:
        self._elapsed = 0
        self._timer_label.set_text("00:00")
        self._timer_label.remove_css_class("warning")
        self._timer_label.remove_css_class("error")
        self.emit("timer-tick", 0, self._target_secs)

    def _on_key(self, ctrl, keyval, keycode, state) -> bool:
        # Blank screen toggle: B = black, W = white (#91)
        if keyval in (Gdk.KEY_b, Gdk.KEY_B):
            self._toggle_blank("black")
            return True
        if keyval in (Gdk.KEY_w, Gdk.KEY_W):
            self._toggle_blank("white")
            return True
        # If screen is blanked, any navigation key un-blanks first
        if self._blank_color:
            self._toggle_blank(self._blank_color)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Page_Down, Gdk.KEY_space):
            self._next()
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Page_Up, Gdk.KEY_BackSpace):
            self._prev()
            return True
        if keyval == Gdk.KEY_Escape:
            # Presenter uses maximize(), not fullscreen(). Escape always closes.
            self.close()
            return True
        return False

    def _set_thumbnail(self, picture: Gtk.Picture,
                        png_bytes: bytes) -> None:
        """Load *png_bytes* into *picture*, clearing on failure."""
        texture = png_bytes_to_texture(png_bytes)
        picture.set_paintable(texture)  # None clears the picture on failure

    def do_close_request(self) -> bool:
        # Stop the timer
        if self._timer_src:
            GLib.source_remove(self._timer_src)
            self._timer_src = None
        # Disconnect monitor handler — must happen before any surface ops
        # to prevent _on_monitors_changed_presenter firing on a dead object
        if self._monitors_handler is not None:
            try:
                self._monitors_model.disconnect(self._monitors_handler)
            except Exception:
                pass
            self._monitors_handler = None
        # Release WebKit notes view
        if _WEBKIT:
            for view in (self._notes_view,):
                if view is not None and hasattr(view, "try_close"):
                    try:
                        view.try_close()
                    except AttributeError:
                        pass
        # Fully destroy the slideshow window via the controlled path
        self._slideshow.close_by_presenter()
        return False
