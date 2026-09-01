"""
presenter.py — Presenter mode window + companion slideshow window.
"""

import logging
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, GdkPixbuf, Gdk, GObject, Pango

log = logging.getLogger(__name__)

from .app_utils import png_bytes_to_texture

from .slides.script import (
    BOLD, BULLET, CODE, HEADING, ITALIC, MONO, PARA, QUOTE,
    deck_schedule, parse_script,
)

from .slides.thumbnails_render import render_page_png, render_slides_hires


def _stops_from(slide_info: list) -> "tuple[list[tuple[int, int]], list[int]]":
    """
    Every stop of the talk: which slide it belongs to, and its PDF page.

    A stop is a press of the advance key.  Nearly always that is a slide,
    but a slide with reveal steps has one stop per step and the audience
    screen has a page for each, so navigation counts stops and everything
    that describes the talk — the script, the schedule, the dots — keeps
    counting slides.

    A build made before slides carried a page index gives no pages at all;
    the slideshow then falls back to one page per slide, which is what that
    build assumed anyway.
    """
    if not all(info.get("page_index") is not None for info in slide_info):
        return ([(index, 0) for index in range(len(slide_info))],
                list(range(len(slide_info))))

    stops: list[tuple[int, int]] = []
    pages: list[int] = []
    for index, info in enumerate(slide_info):
        for step, page in enumerate(info.get("step_pages")
                                    or [info["page_index"]]):
            stops.append((index, step))
            pages.append(page)
    return stops, pages


# ── SlideshowWindow ───────────────────────────────────────────────────────────

class SlideshowWindow(Adw.Window):
    """
    Chromeless fullscreen window that shows a single slide (audience view).

    Monitor placement
    -----------------
    **Name the output; never try to move the window.**  Wayland gives a
    client no way to position a window, and a bare `fullscreen()` passes a
    NULL output — "compositor, you choose", which means wherever the window
    already is.  But `xdg_toplevel.set_fullscreen` takes an optional output
    precisely so a client can say which screen, and GTK 4 hands a
    `GdkMonitor` straight through to it
    (`gdk_wayland_toplevel_fullscreen_on_monitor`).  So
    `Gtk.Window.fullscreen_on_monitor()` is the one call that works, on
    both backends, and it is the only way this class ever fullscreens.

    This file used to assert the opposite — that `fullscreen_on_monitor()`
    was "X11-only and silently does nothing on Wayland" — and fenced it
    behind an `is_x11` check, so on Wayland the placement was a
    `set_default_size()` to the target monitor's dimensions followed by
    `fullscreen()`.  That sets a size, not a position, and the compositor
    re-seated the window where it already was.  The swap button had the
    same hole: unfullscreen, wait, `fullscreen()` again, on the belief that
    GNOME Shell cycles outputs on each such cycle.  It does not, and the
    button did nothing.

    Which output is the target
    --------------------------
    The one the *main window* is not on — read from `parent_window`, not
    from the presenter.  The presenter is constructed microseconds before
    this window and has not been mapped, so it has no output association
    yet and `get_monitor_at_surface()` answers None for it; the main window
    has been mapped all session and answers reliably.  If it answers None
    anyway we take the last monitor in the list rather than the first,
    because the main window is far likelier to be on the first.
    """

    def __init__(self, pdf_path, n_steps: int,
                 presenter_window: "PresenterWindow",
                 parent_window, step_pages=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("Slideshow")
        self.set_default_size(1280, 720)
        # Not transient — a transient window is constrained to its parent's
        # output on some compositors, which prevents multi-monitor placement.
        # We set application instead so it still appears in the task switcher.

        self._pdf_path         = Path(pdf_path) if pdf_path else None
        self._pdf_bytes: bytes | None = None
        # Stops, not slides: a slide that reveals in steps has one of these
        # per step, and this window shows one page for each of them.
        self._n_steps          = n_steps
        self._presenter        = presenter_window
        self._parent_window    = parent_window
        self._pending          = 0
        # Index into the display's monitor list of the output this window is
        # fullscreened on, so the swap button knows what "the other one" is.
        self._monitor_index: int | None = None
        self._closing              = False   # True when close_by_presenter() called
        self._monitors_handler     = None    # GLib signal handler id
        # One rendered page per stop, filled in by load(); None until the
        # background pass reaches it.
        self._pages: list = [None] * max(0, n_steps)
        # Which page of the built PDF each stop is on.  The build drops the
        # continuation pages an overflowing slide leaves behind, so this is
        # normally the stop's own index — but it is still passed in rather
        # than assumed, because a trim that could not be taken falls back to
        # the whole document.  See slides/pagination.slide_pages_pdf.
        self._step_pages: list = (
            list(step_pages) if step_pages
            else list(range(max(0, n_steps)))
        )
        self._render_w: int = 1920
        self._blanked: bool = False

        # The audience sees a picture of the very page the PDF holds, so the
        # room and the handout cannot disagree.  Black surround, because a
        # slide narrower than the screen should fall away into the dark.
        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(True)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)

        self._surround = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._surround.set_hexpand(True)
        self._surround.set_vexpand(True)
        self._surround.append(self._picture)
        self._surround_css = Gtk.CssProvider()
        self._apply_surround("#000000")
        self._surround.get_style_context().add_provider(
            self._surround_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.set_content(self._surround)

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
        Re-fullscreening a window whose output has just changed underneath it
        races with the compositor's surface teardown — this was the root cause
        of the hotplug crashes (LibreOffice Impress has the same bug, see
        RHBZ #1386948/#1390607).  The window now draws a plain texture rather
        than hosting a browser, which removes the GPU subprocess from that
        race, but re-seating on hotplug is still not worth the risk.

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

    def _monitors(self):
        """The display's monitor list, or None if there is no display."""
        display = Gdk.Display.get_default()
        return display.get_monitors() if display is not None else None

    def _monitor_of_main_window(self) -> int | None:
        """
        Index of the monitor holding the main window, or None if it cannot
        be determined.  Read from the main window rather than the presenter
        because the presenter has not been mapped yet — see the class
        docstring.
        """
        if self._parent_window is None:
            return None
        display  = Gdk.Display.get_default()
        monitors = self._monitors()
        if display is None or monitors is None:
            return None
        try:
            surface = self._parent_window.get_surface()
            if surface is None:
                return None
            here = display.get_monitor_at_surface(surface)
        except Exception:
            return None
        if here is None:
            return None
        for i in range(monitors.get_n_items()):
            if monitors.get_item(i) is here:
                return i
        return None

    def _fullscreen_on(self, index: int) -> None:
        """
        Fullscreen the slideshow on the monitor at `index`, naming that
        output to the compositor.  Falls back to an unplaced fullscreen if
        the index has gone stale — a monitor can be unplugged between the
        choice and the call.
        """
        monitors = self._monitors()
        monitor  = monitors.get_item(index) if monitors is not None else None
        if monitor is None:
            self.fullscreen()
            return
        try:
            self.fullscreen_on_monitor(monitor)
            self._monitor_index = index
        except Exception as e:
            log.debug("fullscreen_on_monitor() raised: %s", e)
            self.fullscreen()

    def _place_on_other_monitor(self) -> bool:
        """
        Fullscreen the slideshow on the monitor the main window is not on.
        Called once at startup, from an idle so the surface exists first.
        """
        if self._closing:
            return GLib.SOURCE_REMOVE

        monitors = self._monitors()
        n = monitors.get_n_items() if monitors is not None else 0

        if n < 2:
            # One screen — there is no "other" to move to.
            self.fullscreen()
            return GLib.SOURCE_REMOVE

        here = self._monitor_of_main_window()
        if here is None:
            # Unknown: the last monitor is the better guess, because the
            # main window is far likelier to be on the first.
            target = n - 1
        else:
            target = next(i for i in range(n) if i != here)

        self._fullscreen_on(target)
        return GLib.SOURCE_REMOVE

    def move_to_other_monitor(self) -> None:
        """
        Move the slideshow to the next monitor (triggered by swap button).

        Naming the output is the whole mechanism: with more than two screens
        this walks around the list, so pressing the button repeatedly always
        reaches every one of them.
        """
        if self._closing:
            return

        monitors = self._monitors()
        n = monitors.get_n_items() if monitors is not None else 0
        if n < 2:
            return

        current = self._monitor_index
        if current is None:
            # Never placed, or placement fell back to an unnamed output.
            # Start from wherever the main window is not.
            here = self._monitor_of_main_window()
            target = 0 if here is None else next(
                i for i in range(n) if i != here
            )
        else:
            target = (current + 1) % n

        log.debug("Moving slideshow to monitor %d of %d", target, n)
        self._fullscreen_on(target)

    # ── Load / slide control ──────────────────────────────────────────────────

    def load(self) -> None:
        """
        Read the built PDF and get the first slide on screen.

        Slide one is rasterized synchronously so the audience never sees an
        empty window; the rest of the deck follows on a background thread, in
        order, so that stepping forward normally finds the next page already
        waiting.
        """
        if self._closing or self._pdf_path is None:
            return
        try:
            self._pdf_bytes = self._pdf_path.read_bytes()
        except OSError as e:
            log.error("Could not read the built PDF for the slideshow: %s", e)
            return

        self._render_w = self._slideshow_width()

        first = render_page_png(self._pdf_bytes,
                                self._page_for(self._pending), self._render_w)
        if first is not None and self._pending < len(self._pages):
            self._pages[self._pending] = first
        self._show_page(self._pending)

        threading.Thread(target=self._prerender_deck, daemon=True).start()

    def _slideshow_width(self) -> int:
        """Pixel width to rasterize pages at: the monitor's, within reason."""
        try:
            monitor = self.get_display().get_monitor_at_surface(self.get_surface())
            width = monitor.get_geometry().width * monitor.get_scale_factor()
        except Exception:
            width = 1920
        return int(min(3840, max(1280, width)))

    def _prerender_deck(self) -> None:
        """Rasterize every page once, off the main thread."""
        pdf_bytes = self._pdf_bytes
        if pdf_bytes is None:
            return
        try:
            pages = render_slides_hires(pdf_bytes, self._render_w,
                                        pages=self._step_pages)
        except Exception as e:
            log.warning("Slideshow pre-render failed: %s", e)
            return
        GLib.idle_add(self._store_pages, pages)

    def _store_pages(self, pages: list) -> bool:
        """Adopt the pre-rendered deck and refresh what is on screen."""
        if self._closing:
            return GLib.SOURCE_REMOVE
        for i, png in enumerate(pages):
            if i < len(self._pages) and png is not None:
                self._pages[i] = png
        if not self._blanked:
            self._show_page(self._pending)
        return GLib.SOURCE_REMOVE

    def show_step(self, index: int) -> None:
        """
        Show stop *index*, rendering it on the spot if the prefetch has not.

        A stop, not a slide: a slide revealed in three steps is three of
        these, and the presenter counts them so this window does not have to.
        """
        index = max(0, min(index, self._n_steps - 1))
        self._pending = index
        self._blanked = False
        self._apply_surround("#000000")
        self._show_page(index)

    def _show_page(self, index: int) -> None:
        if self._closing or not 0 <= index < len(self._pages):
            return
        png = self._pages[index]
        if png is None and self._pdf_bytes is not None:
            # The presenter jumped ahead of the prefetch — pay for it now
            # rather than showing the room a stale slide.
            png = render_page_png(self._pdf_bytes, self._page_for(index),
                                  self._render_w)
            self._pages[index] = png
        if png is None:
            return
        texture = png_bytes_to_texture(png)
        if texture is not None:
            self._picture.set_paintable(texture)

    def _page_for(self, index: int) -> int:
        """The PDF page holding stop *index*."""
        if 0 <= index < len(self._step_pages):
            return self._step_pages[index]
        return index

    def set_blank(self, colour: str | None) -> None:
        """
        Blank the audience screen to *colour*, or restore the slide.

        Passing None puts the current slide back.  This replaced injecting
        JavaScript into the page: there is no page any more, just a picture
        to take down and a background to leave showing.
        """
        if self._closing:
            return
        if colour is None:
            self._blanked = False
            self._apply_surround("#000000")
            self._show_page(self._pending)
            return
        self._blanked = True
        self._apply_surround(colour)
        self._picture.set_paintable(None)

    def _apply_surround(self, colour: str) -> None:
        """Set the colour behind (and, when blanked, instead of) the slide."""
        style = f"box {{ background-color: {colour}; }}"
        try:
            self._surround_css.load_from_string(style)
        except AttributeError:
            self._surround_css.load_from_data(style.encode())

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
        # Drop the rendered deck; a 60-slide deck at 4K is a few tens of
        # megabytes of PNG and nothing else references it.
        self._pages = []
        self._pdf_bytes = None
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
                 thumbnails: list, parent_window, pdf_path=None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("Presenter")
        self.set_default_size(1280, 800)
        self.set_transient_for(parent_window)

        self._html_uri    = html_uri
        self._pdf_path    = pdf_path
        self._slide_info  = slide_info
        self._thumbnails  = thumbnails
        self._current     = 0
        self._n_slides    = len(slide_info)
        self._elapsed     = 0
        # Where the advance key stops, and which of them belongs to which
        # slide.  One stop per slide until a slide reveals in steps.
        self._stops, self._step_pages = _stops_from(slide_info)
        self._pos         = 0
        self._first_stop  = [self._stops.index((index, 0))
                             if (index, 0) in self._stops else index
                             for index in range(len(slide_info))]
        # Step pictures are rendered from the built PDF the first time they
        # are wanted; the per-slide thumbnails cannot answer for a step.
        self._step_pngs: dict[int, bytes] = {}
        self._pdf_bytes: bytes | None = None
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

        # Speaking rate the sidebar's estimates already use, so the pace
        # readout and the thumbnail strip do not disagree about the deck.
        self._wpm: int = getattr(parent_window, 'speaking_rate', 110)
        # Second each slide is due to end, cumulative from the start.
        self._schedule: list[int] = deck_schedule(
            slide_info, self._wpm, self._target_secs
        )
        # (start, end) character offsets of each slide's section of the
        # script, and a mark at each section's first line to scroll to.
        self._script_ranges: list[tuple[int, int]] = []
        self._script_marks:  list = []

        self._build_ui()

        self._slideshow = SlideshowWindow(
            pdf_path=pdf_path,
            step_pages=self._step_pages,
            n_steps=len(self._stops),
            presenter_window=self,
            parent_window=parent_window,
        )
        self._slideshow.present()
        self._slideshow.load()

        self._hold_the_session_awake()

        key_ctrl = Gtk.EventControllerKey()
        # CAPTURE phase: intercept key events before they reach the
        # script view, which would otherwise consume them.
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key)
        self.add_controller(key_ctrl)

        # Defer initial slide and timer until after the first layout pass
        # so all Picture widgets have been allocated before we try to
        # paint thumbnails into them (prevents GtkGizmo snapshot warning).
        GLib.idle_add(self._enter_fullscreen)
        GLib.idle_add(self._deferred_start)

    def _hold_the_session_awake(self) -> None:
        """
        Ask the session not to blank or lock while the talk is running.

        A speaker is talking, not typing, and GNOME blanks the screen after
        five minutes of no input — so a slide discussed for longer than that
        took the audience screen down with it.  Nothing here inhibited idle,
        even though the app has a target duration measured in minutes.

        The cookie is the release token; 0 means the session manager refused
        or is not there, and there is nothing to release.
        """
        self._inhibit_cookie = 0
        get_app = getattr(self._parent_window, "get_application", None)
        app = get_app() if callable(get_app) else None
        if app is None:
            return
        try:
            self._inhibit_cookie = app.inhibit(
                self, Gtk.ApplicationInhibitFlags.IDLE, "Presenting a slideshow"
            )
        except Exception as e:                       # no session manager
            log.debug("could not inhibit idle: %s", e)

    def _let_the_session_sleep(self) -> None:
        """Give the idle inhibit back, with the window that took it."""
        cookie = getattr(self, "_inhibit_cookie", 0)
        if not cookie:
            return
        self._inhibit_cookie = 0
        get_app = getattr(self._parent_window, "get_application", None)
        app = get_app() if callable(get_app) else None
        if app is None:
            return
        try:
            app.uninhibit(cookie)
        except Exception as e:
            log.debug("could not release idle inhibit: %s", e)

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
          Header   : [← counter →] [screen]  timer/pace  [End −A +A ⇄ ▣] [□✕]
          Progress : one dot per slide, filled to here
          Body     : [left column]  |  [notes area]
                      current thumb      slide title (bold)
                      next thumb         notes text (large, readable)
          No bottom bar — all controls are in the header.

        The notes area takes ~70% of the width.  The left column holds the
        two thumbnails and the slide counter.  The entire window background
        is forced dark so the presenter's eyes are not strained — a stage
        view, not a document window, which is the one thing here that does
        not follow the system style.

        The controls used to live in a plain Gtk.Box, and this window had no
        header bar at all: no title, no close button, nothing to drag, and no
        way out but the End button or Escape.  They are in an Adw.HeaderBar
        now, so the window has the controls every window is supposed to have,
        and the dark stylesheet below is extended to cover it rather than
        leaving a light strip pasted across the top.
        """
        # ── Force dark colours on this window only ───────────────────────────
        # Installed for the whole display, with every selector scoped under
        # #presenter-window so nothing outside this window can match.
        #
        # It used to be added to the window's own style context, which is
        # why every class rule below was dead: in GTK 4 a provider on a
        # widget's style context styles that widget and not its children, so
        # only the #presenter-window rule ever applied.  The window looked
        # right anyway — its background covers everything and its colour is
        # inherited — which is what hid it.  Verified by asking the widgets:
        # the counter, the thumbnail captions and the pace readout all
        # reported the inherited #e8e8e8 rather than their own greys.
        self.set_name("presenter-window")
        self._window_css = Gtk.CssProvider()
        _dark = (
            "#presenter-window { background: #111111; color: #e8e8e8; }"
            "#presenter-window .presenter-left "
            "  { background: #1a1a1a; border-right: 1px solid #333; }"
            "#presenter-window .notes-title { color: #ffffff; font-weight: bold; }"
            "#presenter-window .notes-hint { color: #666666; }"
            "#presenter-window .thumb-label { color: #888888; font-size: 11px; }"
            "#presenter-window .counter-label { color: #aaaaaa; }"
            # The header bar and the progress strip under it are one surface,
            # so they carry the same fill and only the strip draws the edge.
            "#presenter-window headerbar "
            "  { background: #0d0d0d; color: #e8e8e8; border: none;"
            "    box-shadow: none; }"
            "#presenter-window headerbar button { color: #e8e8e8; }"
            "#presenter-window .presenter-progress "
            "  { background: #0d0d0d; border-bottom: 1px solid #2a2a2a; }"
            "#presenter-window .pace-label { color: #8a8a8a; }"
            "#presenter-window .pace-label.ahead  { color: #78c078; }"
            "#presenter-window .pace-label.behind { color: #e0a45c; }"
        )
        try:
            self._window_css.load_from_string(_dark)
        except AttributeError:
            self._window_css.load_from_data(_dark.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._window_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        root = Adw.ToolbarView()
        self.set_content(root)

        # ── Header bar ────────────────────────────────────────────────────────
        # An Adw.HeaderBar rather than a Gtk.Box, so this window has a title,
        # window controls and something to drag, like every other window.
        header = Adw.HeaderBar()
        root.add_top_bar(header)

        # Start zone: where you are in the deck, and which screen it is on.
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        nav.add_css_class("linked")

        prev_btn = Gtk.Button()
        prev_btn.set_child(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        prev_btn.add_css_class("flat")
        prev_btn.set_tooltip_text("Previous slide (←)")
        prev_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Previous slide"])
        prev_btn.connect("clicked", lambda *_: self._prev())
        nav.append(prev_btn)

        self._counter_label = Gtk.Label(label="1 / 1")
        self._counter_label.set_width_chars(7)
        self._counter_label.add_css_class("counter-label")
        self._counter_label.add_css_class("monospace")
        nav.append(self._counter_label)

        next_btn = Gtk.Button()
        next_btn.set_child(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        next_btn.add_css_class("flat")
        next_btn.set_tooltip_text("Next slide (→)")
        next_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Next slide"])
        next_btn.connect("clicked", lambda *_: self._next())
        nav.append(next_btn)
        header.pack_start(nav)

        self._monitor_icon = Gtk.Image.new_from_icon_name("video-display-symbolic")
        self._monitor_icon.set_tooltip_text("Checking for second monitor…")
        self._monitor_icon.set_margin_start(8)
        header.pack_start(self._monitor_icon)

        # Title zone: the clock, and underneath it whether the clock is the
        # right number for where you have got to.  A header bar's title is
        # what the window is about, and during a talk that is the time.
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title_box.set_valign(Gtk.Align.CENTER)

        timer_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        timer_row.set_halign(Gtk.Align.CENTER)
        timer_row.append(Gtk.Image.new_from_icon_name("alarm-symbolic"))

        # Timer — large, centred, readable from across the room
        self._timer_label = Gtk.Label(label="00:00")
        self._timer_label.add_css_class("monospace")
        self._timer_label.add_css_class("title-2")
        self._timer_label.set_width_chars(7)
        timer_row.append(self._timer_label)
        title_box.append(timer_row)

        # Pace: the clock says how long you have been talking, this says
        # whether that is the right amount for where you have got to.
        self._pace_label = Gtk.Label(label="")
        self._pace_label.add_css_class("pace-label")
        self._pace_label.add_css_class("monospace")
        self._pace_label.add_css_class("caption")
        self._pace_label.set_tooltip_text(
            "How your elapsed time compares with the script up to this slide"
        )
        title_box.append(self._pace_label)
        header.set_title_widget(title_box)

        # End zone, right to left as packed: the ways out and the ways to
        # change what the room sees.  "End" is packed last so it sits at the
        # far left of the group rather than beside the window's own close
        # button, which does the same thing.
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
        header.pack_end(blank_btn)

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
        header.pack_end(swap_btn)

        reset_btn = Gtk.Button()
        reset_btn.set_child(Gtk.Image.new_from_icon_name("view-refresh-symbolic"))
        reset_btn.set_tooltip_text("Reset timer")
        reset_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Reset timer"])
        reset_btn.add_css_class("flat")
        reset_btn.connect("clicked", self._reset_timer)
        header.pack_end(reset_btn)

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
        header.pack_end(font_up_btn)

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
        header.pack_end(font_down_btn)

        # Not destructive-action.  That styling is a warning that something
        # will be lost, and ending a talk loses nothing — the deck, the file
        # and the window behind it are all still there.
        close_btn = Gtk.Button(label="End")
        close_btn.set_tooltip_text("End presentation (Escape)")
        close_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], ["End presentation"]
        )
        close_btn.connect("clicked", lambda *_: self.close())
        header.pack_end(close_btn)

        # ── Progress strip ────────────────────────────────────────────────────
        # One dot per slide, filled up to the current position.  Drawn via
        # Cairo so it scales cleanly and costs nothing at runtime.  Capped at
        # _PROGRESS_MAX_DOTS dots; beyond that the bar compresses evenly so it
        # always fits without wrapping.  Its own bar under the header, where it
        # gets the full width it wants instead of fighting the timer for it.
        progress_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        progress_row.add_css_class("presenter-progress")

        self._progress_bar = Gtk.DrawingArea()
        self._progress_bar.set_margin_start(12)
        self._progress_bar.set_margin_end(12)
        self._progress_bar.set_content_height(20)
        self._progress_bar.set_hexpand(True)
        self._progress_bar.set_valign(Gtk.Align.CENTER)
        self._progress_bar.set_tooltip_text("Slide progress")
        self._progress_bar.set_draw_func(self._draw_progress)
        progress_row.append(self._progress_bar)
        root.add_top_bar(progress_row)

        # ── Body: left column + notes area ────────────────────────────────────
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_vexpand(True)
        body.set_hexpand(True)
        root.set_content(body)

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

        # What this slide is scheduled to take, so the pace figure above can
        # be read as "and this slide still owes me a minute".
        self._slide_time_label = Gtk.Label(label="")
        self._slide_time_label.add_css_class("thumb-label")
        self._slide_time_label.set_xalign(0)
        self._slide_time_label.set_margin_start(12)
        self._slide_time_label.set_margin_top(6)
        self._slide_time_label.set_margin_bottom(10)
        left.append(self._slide_time_label)

        # Separator
        left.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Next slide thumbnail
        # Named on every move rather than fixed: on a slide that reveals,
        # what this pane shows is the next *step* of the slide already on the
        # screen, and calling that the next slide would be a lie the speaker
        # reads mid-sentence.
        self._next_label = Gtk.Label(label="Next slide")
        self._next_label.add_css_class("thumb-label")
        self._next_label.set_xalign(0)
        self._next_label.set_margin_top(12)
        self._next_label.set_margin_start(12)
        self._next_label.set_margin_bottom(4)
        left.append(self._next_label)

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

        # ── Script area: title + the running script of the whole talk ─────────
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

        # The script — a GtkTextView rather than the browser view this pane
        # used to be.  A teleprompter has to scroll itself to the section
        # being spoken and take the contrast out of the rest, and a WebView
        # can do neither without JavaScript, which the app switches off
        # everywhere it renders the user's own document.  A text buffer does
        # both natively: a mark to scroll to, and a tag to dim with.
        self._script_scroll = Gtk.ScrolledWindow()
        self._script_scroll.set_vexpand(True)
        self._script_scroll.set_hexpand(True)
        self._script_scroll.set_policy(Gtk.PolicyType.NEVER,
                                       Gtk.PolicyType.AUTOMATIC)

        self._notes_view = Gtk.TextView()
        self._notes_view.set_name("presenter-script")
        self._notes_view.set_editable(False)
        self._notes_view.set_cursor_visible(False)
        self._notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._notes_view.set_top_margin(24)
        self._notes_view.set_bottom_margin(240)   # let the last slide scroll up
        self._notes_view.set_left_margin(28)
        self._notes_view.set_right_margin(28)
        # Space between the wrapped lines of one block, kept smaller than
        # the space each block's tag puts above itself — otherwise every
        # gap is the same size and the blocks stop being visible as blocks.
        self._notes_view.set_pixels_inside_wrap(4)
        self._notes_view.set_vexpand(True)
        self._notes_view.set_hexpand(True)
        self._script_scroll.set_child(self._notes_view)

        self._script_css = Gtk.CssProvider()
        self._notes_view.get_style_context().add_provider(
            self._script_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._apply_script_font()

        self._build_script_tags()
        self._build_script_buffer()

        notes_area.append(self._script_scroll)

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
        """Go to the beginning of slide *index*, whatever it reveals."""
        if not self._slide_info:
            return
        index = max(0, min(index, self._n_slides - 1))
        self._go_to_stop(self._first_stop[index])

    def _go_to_stop(self, pos: int) -> None:
        if not self._stops:
            return
        pos = max(0, min(pos, len(self._stops) - 1))
        self._pos = pos
        index, step = self._stops[pos]
        self._current = index

        # Drive the audience screen through SlideshowWindow — the
        # presenter screen shows thumbnails and notes, not a rendered page.
        self._slideshow.show_step(pos)

        # What is on the screen now, and what the next press will put there.
        # Not "the next slide" when this one is still arriving: what the
        # speaker needs to see is what comes next, whatever it is part of.
        self._set_thumbnail(self._current_picture, self._picture_for(pos))
        self._set_thumbnail(self._next_picture, self._picture_for(pos + 1))
        coming = (self._stops[pos + 1][0] if pos + 1 < len(self._stops)
                  else None)
        self._next_label.set_label("Next step" if coming == index
                                   else "Next slide")

        # Slide title above the script — the section heading says the same
        # thing, but it scrolls away and this does not.
        title = self._slide_info[index].get("title", f"Slide {index + 1}")
        self._slide_title_label.set_label(title)

        self._focus_script(index)
        self._update_slide_time()
        self._update_pace()

        # The deck is counted in slides; the steps of one are said beside it,
        # because "4 / 20" jumping nowhere on three presses reads as broken.
        steps = self._steps_of(index)
        counter = f"{index + 1} / {self._n_slides}"
        if steps > 1:
            counter += f" · {step + 1} of {steps}"
        self._counter_label.set_text(counter)
        # Guard against drawing before the widget has been allocated
        if self._progress_bar.get_realized():
            self._progress_bar.queue_draw()

    def _steps_of(self, index: int) -> int:
        """How many stops slide *index* takes."""
        return sum(1 for slide, _step in self._stops if slide == index)

    def _picture_for(self, pos: int) -> "bytes | None":
        """
        The picture of stop *pos*, or None when there is nothing after the end.

        A slide that does not reveal is its build thumbnail, which is already
        rasterized.  A step has no thumbnail of its own — the strip shows
        slides — so it is rendered off the built PDF the first time it is
        asked for and kept.
        """
        if not 0 <= pos < len(self._stops):
            return None
        index, _step = self._stops[pos]
        if self._steps_of(index) == 1:
            return (self._thumbnails[index]
                    if index < len(self._thumbnails) else None)

        if pos not in self._step_pngs:
            if self._pdf_bytes is None and self._pdf_path is not None:
                try:
                    self._pdf_bytes = Path(self._pdf_path).read_bytes()
                except OSError as exc:
                    log.warning("Presenter could not read the built PDF: %s", exc)
                    self._pdf_path = None
            png = None
            if self._pdf_bytes is not None and pos < len(self._step_pages):
                png = render_page_png(self._pdf_bytes,
                                      self._step_pages[pos], 432)
            self._step_pngs[pos] = png
        return self._step_pngs[pos]

    def _draw_progress(self, area: Gtk.DrawingArea, cr,
                       width: int, height: int) -> None:
        """
        Cairo draw function for the slide progress bar.

        Renders one dot per slide in a single horizontal row.  The current
        slide and all previous slides are filled with the accent colour;
        future slides are drawn as outlines.  When the deck has more slides
        than _PROGRESS_MAX_DOTS the dots compress to fit, otherwise each dot
        is 8 px wide.

        The dots are spread across the whole strip rather than packed at a
        fixed pitch, so how far along the row a dot sits is how far into the
        deck it is — which is the thing being read from the back of a room.
        A fixed pitch was right while this was a slot between two buttons in
        the old top bar; in a strip of its own it left a short cluster
        marooned in the middle of the window.

        Designed to be readable at a glance from 2–3 metres:
          ●   ●   ●   ○   ○   ○   ← clear at-a-glance progress
        """
        n = self._n_slides
        if n < 2:
            return   # single-slide deck — no progress bar needed

        # ── Geometry ──────────────────────────────────────────────────────────
        n_dots   = min(n, self._PROGRESS_MAX_DOTS)
        # Dot diameter: 8 px normally, compressed if many slides
        dot_d    = min(8, max(4, (width - 8) // (n_dots * 2)))
        cy       = height / 2
        r        = dot_d / 2
        # First and last dot sit against the ends, so the row measures the
        # whole strip.  A one-dot row would divide by zero, hence the guard.
        step     = (width - dot_d) / (n_dots - 1) if n_dots > 1 else 0.0

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
            cx = r + i * step
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            if i < filled_dots:
                cr.set_source_rgba(fill_r, fill_g, fill_b, fill_a)
                cr.fill()
            else:
                cr.set_source_rgba(dim_r, dim_g, dim_b, 0.4)
                cr.arc(cx, cy, r - 0.5, 0, 2 * math.pi)
                cr.set_line_width(1.0)
                cr.stroke()

    # ── The script ────────────────────────────────────────────────────────────

    def _apply_script_font(self) -> None:
        """
        Colour and size the script pane; its tags scale relative to this.

        Carried by the pane's own provider rather than the window-wide one
        that dresses the rest of this window.  A GtkTextView paints its
        text on a child "text" node whose background comes from the theme,
        and reaching that node from a provider hung on the window left the
        pane white — with white headings on it, which is to say no
        headings at all.
        """
        css = (
            f"#presenter-script, #presenter-script text {{"
            f"  background-color: #111111; color: #e8e8e8;"
            f"  font-size: {self._notes_font_size}px; }}"
        )
        try:
            self._script_css.load_from_string(css)
        except AttributeError:
            self._script_css.load_from_data(css.encode())

    def _build_script_tags(self) -> None:
        """
        The tags the script is painted with.

        Order matters: between two tags on the same characters the higher
        priority wins, and priority follows the order tags were added to the
        table.  "dim" is created last so that it beats every colour above
        it — a dimmed heading has to actually go grey.
        """
        buf = self._notes_view.get_buffer()

        buf.create_tag("heading", weight=Pango.Weight.BOLD, scale=1.12,
                       foreground="#ffffff",
                       pixels_above_lines=28, pixels_below_lines=10)
        buf.create_tag("para", pixels_above_lines=18)
        # Negative indent hangs the marker to the left of the text, so a
        # wrapped item lines up under its own first word, not under the dot.
        buf.create_tag("bullet", left_margin=76, indent=-24,
                       pixels_above_lines=12)
        buf.create_tag("quote", left_margin=60, style=Pango.Style.ITALIC,
                       foreground="#b4b4b4", pixels_above_lines=18)
        buf.create_tag("code", family="monospace", scale=0.88,
                       foreground="#88ccff", left_margin=60,
                       pixels_above_lines=18)
        buf.create_tag("placeholder", style=Pango.Style.ITALIC,
                       foreground="#5a5a5a", pixels_above_lines=18)

        buf.create_tag(BOLD,   weight=Pango.Weight.BOLD, foreground="#ffffff")
        buf.create_tag(ITALIC, style=Pango.Style.ITALIC)
        buf.create_tag(MONO,   family="monospace", foreground="#88ccff")

        # Last, and therefore top of the pile.  Dark enough to drop out of
        # the way, light enough to read on purpose — a speaker glancing at
        # what is coming next should not have to lean in.
        buf.create_tag("dim", foreground="#606060")

    _BLOCK_TAGS = {
        HEADING: "heading",
        PARA:    "para",
        BULLET:  "bullet",
        QUOTE:   "quote",
        CODE:    "code",
    }

    def _build_script_buffer(self) -> None:
        """
        Lay the whole talk out as one document, section by section.

        Built once rather than per slide: the point of a running script is
        that the words either side of the current section stay on screen,
        and rebuilding the buffer on every keypress would throw away the
        scroll position that makes it readable.
        """
        buf = self._notes_view.get_buffer()
        buf.set_text("")
        self._script_ranges = []
        self._script_marks  = []

        for i, info in enumerate(self._slide_info):
            start = buf.get_end_iter().get_offset()
            title = info.get("title") or f"Slide {i + 1}"
            self._insert_block(buf, f"{i + 1} · {title}", "heading")
            self._script_marks.append(
                buf.create_mark(None, buf.get_iter_at_offset(start), True)
            )

            blocks = parse_script(info.get("notes", ""))
            if not blocks:
                # Named, not left blank.  A slide with nothing to say is a
                # fact about the talk, and in a running script it reads as
                # one line rather than as an empty pane.
                self._insert_block(buf, "— nothing scripted —", "placeholder")
            for block in blocks:
                self._insert_block(buf, block.text,
                                   self._BLOCK_TAGS.get(block.kind, "para"),
                                   block.spans)

            self._script_ranges.append(
                (start, buf.get_end_iter().get_offset())
            )

    def _insert_block(self, buf, text: str, tag: str, spans=()) -> None:
        """Append one block, tagged as itself and under its emphasis spans."""
        off = buf.get_end_iter().get_offset()
        buf.insert(buf.get_end_iter(), text + "\n")
        buf.apply_tag_by_name(
            tag, buf.get_iter_at_offset(off),
            buf.get_iter_at_offset(off + len(text)),
        )
        for span in spans:
            buf.apply_tag_by_name(
                span.style,
                buf.get_iter_at_offset(off + span.start),
                buf.get_iter_at_offset(off + span.end),
            )

    def _focus_script(self, index: int) -> None:
        """Light this slide's section of the script and dim the rest."""
        if not self._script_ranges or not (0 <= index < len(self._script_ranges)):
            return
        buf = self._notes_view.get_buffer()
        dim = buf.get_tag_table().lookup("dim")
        buf.remove_tag(dim, buf.get_start_iter(), buf.get_end_iter())

        lo, hi = self._script_ranges[index]
        if lo > 0:
            buf.apply_tag(dim, buf.get_start_iter(),
                          buf.get_iter_at_offset(lo))
        end = buf.get_end_iter()
        if hi < end.get_offset():
            buf.apply_tag(dim, buf.get_iter_at_offset(hi), end)

        # Near the top, but not at it: a line or two of what was just said
        # is the cheapest way to know you are in the right place.
        self._notes_view.scroll_to_mark(
            self._script_marks[index], 0.0, True, 0.0, 0.10
        )

    # ── Pace ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _mmss(secs: int) -> str:
        m, s = divmod(max(0, int(secs)), 60)
        return f"{m}:{s:02d}"

    def _update_pace(self) -> None:
        """
        Say whether the clock and the script agree about where you are.

        Compared against a window rather than a point: a slide is due to
        run from one second to another, and anywhere inside that is on
        pace.  Being told you are four seconds adrift is noise.
        """
        self._pace_label.remove_css_class("ahead")
        self._pace_label.remove_css_class("behind")

        if not self._schedule or self._schedule[-1] <= 0:
            self._pace_label.set_text("")     # nothing to be measured against
            return

        i         = self._current
        due_end   = self._schedule[i]
        due_start = self._schedule[i - 1] if i > 0 else 0

        if self._elapsed < due_start:
            self._pace_label.set_text(f"{self._mmss(due_start - self._elapsed)} ahead")
            self._pace_label.add_css_class("ahead")
        elif self._elapsed > due_end:
            self._pace_label.set_text(f"{self._mmss(self._elapsed - due_end)} behind")
            self._pace_label.add_css_class("behind")
        else:
            self._pace_label.set_text("on pace")

    def _update_slide_time(self) -> None:
        """Show what this slide is scheduled to take."""
        i = self._current
        if not self._schedule or not (0 <= i < len(self._schedule)):
            self._slide_time_label.set_text("")
            return
        secs = self._schedule[i] - (self._schedule[i - 1] if i > 0 else 0)
        if secs <= 0:
            self._slide_time_label.set_text("")
            return
        scripted = bool(self._slide_info[i].get("notes", "").strip())
        self._slide_time_label.set_text(
            f"{self._mmss(secs)} of script" if scripted
            else f"{self._mmss(secs)} estimated"
        )

    def _next(self) -> None:
        """The next thing to show: the rest of this slide, or the next one."""
        self._go_to_stop(self._pos + 1)

    def _prev(self) -> None:
        self._go_to_stop(self._pos - 1)

    def _next_slide(self) -> None:
        """Skip whatever this slide is still holding back."""
        self._go_to(self._current + 1)

    def _prev_slide(self) -> None:
        """
        Back to the top of this slide, or to the one before it.

        Where Left walks back through the reveals, Up leaves them: a speaker
        going back a slide wants the slide, not its last step.
        """
        self._go_to(self._current - 1 if self._pos == self._first_stop[self._current]
                    else self._current)

    def _toggle_blank(self, color: str) -> None:
        """Toggle a solid black or white screen on the slideshow (#91).

        Pressing B blanks to black, W to white; pressing the same key again
        restores the slide.  The slideshow window shows the blank; the
        presenter view keeps showing the slide content.
        """
        if self._blank_color == color:
            # Un-blank: restore current slide
            self._blank_color = ""
            self._slideshow.set_blank(None)
        else:
            self._blank_color = color
            # A lookup rather than interpolation, so the colour reaching the
            # stylesheet can only ever be one of these two.
            self._slideshow.set_blank(
                "#000000" if color == "black" else "#ffffff"
            )

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

        self._update_pace()

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
        """
        Resize the script pane, keeping the reader's place.

        Only the CSS changes — the buffer is the same text at a different
        size — but the section that was at the top has moved, so scroll
        back to it rather than leaving the speaker to find their line.
        """
        self._apply_script_font()
        self._focus_script(self._current)

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
        self._update_pace()
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
        # Down and Up move by slide, past whatever it is still revealing —
        # the way out when the room has read ahead of the build anyway.
        if keyval == Gdk.KEY_Down:
            self._next_slide()
            return True
        if keyval == Gdk.KEY_Up:
            self._prev_slide()
            return True
        if keyval == Gdk.KEY_Escape:
            # Presenter uses maximize(), not fullscreen(). Escape always closes.
            self.close()
            return True
        return False

    def _set_thumbnail(self, picture: Gtk.Picture,
                        png_bytes: "bytes | None") -> None:
        """Load *png_bytes* into *picture*, clearing on failure or on None."""
        texture = png_bytes_to_texture(png_bytes) if png_bytes else None
        picture.set_paintable(texture)  # None clears the picture on failure

    def do_close_request(self) -> bool:
        # Stop the timer
        if self._timer_src:
            GLib.source_remove(self._timer_src)
            self._timer_src = None
        # The talk is over, so the session may blank and lock again.
        self._let_the_session_sleep()
        # Disconnect monitor handler — must happen before any surface ops
        # to prevent _on_monitors_changed_presenter firing on a dead object
        if self._monitors_handler is not None:
            try:
                self._monitors_model.disconnect(self._monitors_handler)
            except Exception:
                pass
            self._monitors_handler = None
        # The window's stylesheet lives on the display, so take it back down
        # with the window rather than leaving it behind for the next one.
        if getattr(self, "_window_css", None) is not None:
            try:
                Gtk.StyleContext.remove_provider_for_display(
                    Gdk.Display.get_default(), self._window_css
                )
            except Exception:
                pass
            self._window_css = None
        # The script pane is a GtkTextView now, so there is no browser view
        # left on this window to close — only the slideshow holds one.
        # Fully destroy the slideshow window via the controlled path
        self._slideshow.close_by_presenter()
        return False
