# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Scope rules

Two standing constraints on what Presence is. They are decisions, not open
questions — do not propose work that reverses them.

1. **No AI features.** Presence converts Markdown to slides. It does not
   generate images, infographics, or slides from documents, and it stores no
   API keys.
2. **Image layout is automatic only.** Alt text is a description, not a
   token string: `![a red barn](assets/barn.jpg)`. `slides/layout.py` decides
   placement, size and fit from the slide's own content. There are no manual
   position, size, fit, focal, gradient, opacity, fade, grayscale, blur,
   tint, flip or zoom controls.

Note that `splitter.py::parse_image_layout()` is mode `444` and still parses
the old tokens; they are simply not honoured downstream, so documents written
against the old syntax keep opening and lay themselves out.

## Running the app

```bash
python3 -m presence          # from the repo root (requires GTK4)
presence-cli input.md        # headless CLI, no GTK required — writes input.pdf
presence-cli input.md out.pdf
```

The output argument is optional and defaults to the input with a `.pdf`
suffix. It used to be optional and *undefaulted*, so the documented command
above dereferenced `None` and died in a traceback on the first line that
touched it. `cli.py` is mode `444`; that fix is the only thing in it.

## Running tests

```bash
pytest                                      # all tests (runs tests/)
pytest tests/test_slides.py                 # single file
pytest tests/test_slides.py::test_split_basic  # single test
```

**Widget tests work.** They did not for a long time, because `conftest.py`
pinned `GDK_BACKEND=offscreen` — a backend GTK 4 removed. `Gtk.init_check()`
still returned True and the first widget constructed then segfaulted, which
read as a GTK 4 limitation and was in fact the pin's doing. The pin is gone.

Anything that builds a widget takes the `gtk` fixture, which skips when there
is no display rather than failing inside a constructor; without one, run a
headless compositor (`broadwayd`, `mutter --headless`) and point
`WAYLAND_DISPLAY` or `DISPLAY` at it. What the pin was there for — a test run
must never throw a window onto the desktop — holds because **nothing presents
a window**: a GTK widget, toplevels included, is neither mapped nor visible
until something calls `present()`. A class that presents one of its own from
`__init__` is tested with that collaborator replaced, which is how
`test_presenter.py` builds `PresenterWindow` without fullscreening a slideshow
onto the developer's screen.

Two more fixtures come from `conftest.py`. `app_factory` builds registered
`Application` instances — an app reports its actions and accelerators only
once registered, and registering exports an object on the session bus, so each
gets a unique id and `NON_UNIQUE`. `isolated_session` is **autouse**: it
redirects `session._config_dir` and `_data_dir` to a temp directory, because
building a `MainWindow` writes the user's session file (`__init__` sets the
theme-panel button active, whose toggle handler saves the window state), and a
Presence the user had open would have its state clobbered.

The older controller tests still drive the real class against a stand-in
window (`test_build_status.py` established that pattern, and
`test_document_controller.py`, `test_export_controller.py` and
`test_build_coordinator.py` follow it). That is still the right shape for
logic that only needs the window's few attributes — it is faster and needs no
display — but it is no longer the only option. Those stand-ins are also the
coupling made visible: each one is a re-declaration of what its controller
asks the window for, so a stand-in that has to grow a field is a controller
that has grown a reach. They shrank when the state moved to its owner —
`FakeWindow` in `test_build_coordinator.py` went from fourteen attributes to
six, and what it dropped is now set on the coordinator itself.

Three test files are mode `444` (`test_slides.py`, `test_css.py`,
`test_session.py`), so new tests go in new files.

## Installing

```bash
pip install -e ".[dev]"      # editable install with dev dependencies
```


## Code Style
- Follow PEP 8 style guidelines
- Use type hints for function parameters and return types
- Use dataclasses for data models
- Prefer f-strings for string formatting
- Use pathlib for file paths

## Conventions
- Use snake_case for variables and functions
- Use PascalCase for classes
- Use UPPER_CASE for constants
- Keep functions small and focused
- Write docstrings for public functions
- Strict adherence to GNOME HIG for UI/UX decisions

## Architecture

The codebase lives entirely under `src/presence/` and splits into two layers:

**`src/presence/slides/`** — pure library, no GTK dependency. Pipeline:

1. `frontmatter.py` — strips and parses YAML frontmatter from the top of the document.
2. `splitter.py` — splits Markdown into per-slide strings on `---` separators; also handles `|||` (two-column split), `^^^` (speaker notes), and title-slide detection. All operations work on raw strings only. `parse_image_layout()` still parses the retired alt-text tokens — the file is mode `444` — but nothing downstream reads its answer; see the scope rules.
3. `renderer.py` — converts Markdown fragments to HTML using `markdown-it-py` with optional Pygments syntax highlighting.
4. `css.py` — builds the full CSS string from a `Theme` object via `build_css(theme, width, height, logo_b64)`. Colour values from theme files are validated against an allowlist to prevent CSS injection.
5. `layout.py` — decides how a slide is laid out: `choose_layout(cleaned_md, images, aspects, side_ordinal)` returns one of `text`, `bleed`, `single`, `pair`, `gallery`. It picks a width from how much text shares the slide (`auto_size`: half and half until the words stop fitting in half a slide), whether each gallery cell crops or letterboxes (`cell_fit`), and whether two portraits should flank it. **A lone picture goes beside the words, never behind them** — only a slide with no text at all gets a full-bleed image, because there is nothing to set it beside. A picture is shown as it is: the gradient that used to wash the theme's background across its edge, and the 75% opacity that faded the whole thing, both existed to keep a caption legible on a photograph and went when the caption did. `AUTO_IMAGE_LAYOUT` has no `gradient` or `fade` key, `css.py` no rule to honour one, and `opacity` is 100 — which is what the gallery always rendered at, since it never read the value. Which side it takes alternates down the deck (`single_side`), and that is the one thing here that is not a function of the slide's own content: `side_ordinal` counts the lone pictures before it, so `html.py` must count them over *every* slide, before its `only_index` skip, or a single-slide render and the build would put the same picture on opposite sides. Shapes come from `utils.image_aspect()`, which reads the header via Pillow cached on mtime, so the live render can afford it. `AUTO_IMAGE_LAYOUT` is the single owner of what an automatically placed image looks like — no other module may read a treatment value off a parsed image. **Every plan also carries what it was decided from** — `text` (`none`/`short`/`long`, from `text_weight()` over the same 40-word threshold `auto_size()` uses), `images`, and `shapes` (`shape_of()` per picture) — because a stylesheet can neither count words nor read an image header, and without them a theme could not arrive at an arrangement of its own. Those are facts, not decisions: nothing here branches on `shapes` except the portrait test that earns the pair.
6. `html.py` — assembles the complete HTML document; dispatches to per-slide-type renderers (`_render_title_slide`, `_render_normal_slide`, `_render_image_slide`, `_render_two_image_slide`, `_render_gallery_slide`) according to the layout plan. Gallery rows carry an inline pixel height: indefinite rows collapse images set to `height:100%`, which renders the whole grid empty. Every slide div is stamped with `data-slide-index` by `_stamp_slide_index()` at the dispatch site, not in the five renderers, so a new layout cannot be added without it — and with `data-layout`, `data-text`, `data-images` and `data-shapes` by `_stamp_layout()` beside it, for the same reason. `choose_layout()` is therefore called for *every* slide, including a cover and a slide of pure text, since the plan is what gets published as much as what gets dispatched on.

**A measurement is a custom property; an arrangement is a rule.** Panel geometry, the gallery's tracks, a cell's `object-fit` and the progress bar's width used to be written onto the element as inline styles, which outrank every selector — so the arrangement was not a theme's to change, whatever the stylesheet said. `_image_geometry()` now returns only `--p-img-size` and `--p-img-pad`, and `css.py` holds the rules that read them, keyed on `data-img-pos` and `data-split`. The remaining inline styles in a rendered deck are custom properties and nothing else; `test_layout_attrs.py` fails if a raw `width`, `padding`, `object-fit`, `opacity` or grid track goes back inline. The one value a theme must leave definite is `grid-auto-rows`: an image at `height:100%` in an indefinite row collapses and empties the grid.
7. `themes.py` / `theme_loader.py` — `Theme` dataclass and discovery of theme directories.
8. `pagination.py` — what can be read back off a page WeasyPrint has already laid out: which page each slide starts on, which slides were fragmented, where each slide's fold falls, and the PDF built from the slide pages alone. Pure WeasyPrint, no GTK, because both engines need it.
9. `diagnostics.py` — the sentences a finished build has to say. Overflowing slides, pictures that did not resolve, a theme that is not installed: each produces a slide that looks deliberate, so none can be left to the rendering to convey. One function over one set of facts, so the CLI's stderr and the window's banner agree about the same deck.
10. `thumbnails.py` / `thumbnails_render.py` — PDF-to-picture rendering, by Poppler and Cairo. `render_thumbnails()` (strip), `render_slides_hires()` (image and handout export) and `render_page_png()` (one page, for the strip's live row and the slideshow) all take an optional `pages` list saying which PDF page each slide starts on.
11. `handout.py` — the talk as a document: each slide's picture with the `^^^` script beneath it, images inlined as data URIs. Deliberately unthemed — a theme is display type for a room, a handout is read at arm's length.

**`src/presence/`** (GTK 4 / Libadwaita frontend):

- `application.py` / `window.py` — app lifecycle and main window.
- `editor.py` — GtkSourceView-based Markdown editor. The toolbar carries slide-structure inserts only; formatting lives in its overflow menu and the text context menu, both driven by the `editor.*` action group. The image button opens a file chooser directly — there is nothing to configure on the way in. Alt text highlights as one description. Both the chooser and drag-and-drop go through `_copy_into_assets()`, which copies the picture next to the document under a name `splitter._IMAGE_RE` can carry: that pattern ends an image source at whitespace or a closing paren, so `_asset_name()` turns "My Holiday Photo.png" into `My-Holiday-Photo.png` and "shot(1).png" into `shot-1.png`. Without it those tags parse as no image, or as a truncated path. A different picture of the same name gets a numeric suffix rather than replacing what an existing tag points at.

  **The editor follows the system style.** Two colour schemes ship — `presence-markdown-light.xml` and `presence-markdown-dark.xml` — and `_apply_style()` picks between them off `Adw.StyleManager.get_dark()`, re-picking on every `notify::dark`. There used to be one, `presence-markdown.xml`, pinned to a white background, so a writer in GNOME's dark style got dark chrome around a white sheet with no preference anywhere to change it. That file is mode `444`, so it was left frozen and installed, sitting behind the new pair in the fallback chain (which ends at GtkSourceView's own `Adwaita`/`Adwaita-dark`). Neither new scheme defines `selection` or `cursor`: leaving them out is how the user's accent reaches the editor, where the old scheme pinned GTK 3's `#4a90d9`. The tags the *buffer* colours rather than the scheme — separators, the three image-tag layers, focus mode's wash — are re-set from `_TAG_COLOURS` on every re-pick, because a scheme cannot reach a `GtkTextTag`. `Editor` holds a handler on the style manager, which outlives every window, and drops it on `notify::root` so a closed window's editor is not kept alive by it. `test_editor_style.py` pins all of this. Registration matters too: `_register_style_schemes()` appends the package directory to the manager's search path, because meson installs the schemes somewhere already searched and a pip install does not — there the editor silently fell back to plain Adwaita.
- `converter.py` — `Converter(GObject.Object)`, the render pipeline. One engine, three speeds: `build_preview()` is Markdown → HTML for one slide, synchronous and sub-millisecond, with no layout — the cheapest way to ask what a slide's markup is. `render_slide_async()` is the live path: one slide → HTML → WeasyPrint → PDF → PNG plus its fold line, on a worker thread. `convert()` is the whole deck → HTML → PDF → thumbnails, emitting `conversion-started / conversion-complete / conversion-failed`; it also handles watch-mode polling. **All three take the document as text** — `convert(text, base_dir, output_path)`. It used to take a path and read it, which forced every build to put the buffer on disk first; watch mode, where the file genuinely is the document, is the only caller that reads one now. All three share the cached theme/CSS resolution in `_render_context()`.
- `sidebar.py` — thumbnail strip. Two sources feed it and they are kept apart on purpose: everything the document alone can answer — title, number, script indicator, per-slide timing — is read out of the text by `read_slides()` on every pause in typing, so it is never a build behind; only the picture and the overflow badge need a build. Pictures are matched to slides by content, never by index (`carry_over()`), and a row whose own slide has changed since the build is marked out of date rather than showing a stale picture silently. How big it draws them is the writer's, set by a slider in the strip's own toolbar between `THUMBNAIL_MIN` and `THUMBNAIL_MAX` and persisted as `thumbnail_size` in the window state.
- `presenter.py` — presenter view (current slide + notes + timer). `SlideshowWindow` — the audience screen — shows pages of the built PDF as textures, pre-rendered on a thread when it opens. Navigation is a texture swap and blanking is `set_blank()`; there is no browser and no JavaScript anywhere in the app's own output.

  **The presenter is a window, not a canvas.** Its controls are an `Adw.HeaderBar` in an `Adw.ToolbarView`, with the slide-progress dots as a second top bar beneath it. They were a plain `Gtk.Box`, and the window had no header bar at all: no title, no close button, nothing to drag, and no way out but the End button or Escape. End is a plain button — `destructive-action` warns that something will be lost, and ending a talk loses nothing. The progress dots span the full strip now rather than sitting at a fixed pitch, so how far along the row a dot is is how far into the deck it is; a fixed pitch was right while this was a slot between two buttons and left a marooned cluster once it had the width. The forced-dark stylesheet is the one thing here that does not follow the system style — a stage view, read in a dark room — and it now covers the header bar too, rather than leaving a light strip pasted across the top.

  **A talk holds the session awake.** `Gtk.Application.inhibit(IDLE)` is taken when the presenter opens and released in `do_close_request()`. Nothing did this before, so a slide discussed for longer than GNOME's five-minute blank took the audience screen down with it — in an app whose settings measure a target duration in minutes. A cookie of 0 means the session manager refused or is not there, and there is nothing to release. `test_presenter_chrome.py` pins the chrome and the inhibit.

  **Name the output; never try to move the window.** The audience screen picks its monitor with `fullscreen_on_monitor()` on both backends, because `xdg_toplevel.set_fullscreen` takes an optional output and GTK 4 hands a `GdkMonitor` straight through to it. A bare `fullscreen()` passes NULL — "compositor, you choose" — which is the screen the window is already on. The file used to claim `fullscreen_on_monitor()` was "X11-only and silently does nothing on Wayland" and fenced it behind an `is_x11` check, leaving Wayland with a `set_default_size()` to the target monitor's dimensions and then a bare `fullscreen()`: that sets a size, not a position, so the slideshow opened on the editor's screen. The swap button had the same hole — unfullscreen, wait 200 ms, bare `fullscreen()` again, on the belief that GNOME Shell cycles outputs on each such cycle. It does not, and the button moved nothing. **The target is read from the main window, not the presenter**, which is built microseconds earlier and has no output association yet, so `get_monitor_at_surface()` answers None for it. Swap now walks the whole list, so a third screen is reachable.
- `export_controller.py` — the four export flows (PDF / HTML / images / handout) behind one `_ask_save_path()`: dialog, suffix defaulting, writability check. PDF export re-points the build; the other three consume one via `builds.with_current_build()`.
- `document_controller.py` — the document as a file: **where it is** (`file_path`, `output_path`, `pres_path`, `pres_temp_dir`, `modified`, and `display_path` / `display_name` / `base_dir` over them), open, save, save-as, autosave, the unsaved-changes question, and the whole `.pres` bundle — unpacking, re-packing, the temp directory. Owns `UNTITLED`. **A save does not always finish before it returns:** a document with no path has to ask for a filename, and that answer arrives through a GTK callback. `save()`, `write_document()` and `save_as_dialog()` therefore take an `on_done` callback, and anything that must not happen until the file exists — closing the window, replacing the buffer — goes there rather than on the line after the call. The return value only says the save has not *already* failed.
- `settle_clock.py` — **when a keystroke becomes a picture.** One owner for the timing of the typing pipeline: the pause after which the document counts as settled (`SETTLE_MS`), the shorter fuse that sends the slide under the cursor to be laid out (`LIVE_MS`), the cursor poll that decides which slide that is, and `current_slide` itself. The editor reports a buffer change the moment it happens and holds no timer of its own.
- `build_coordinator.py` — **the build's own state** (`built_text`, `building_text`, `converting`, `slide_info`, `thumbnails`, `html_uri`, and the scratch PDF an unsaved deck builds to), when a build runs, what the header chip says, and what runs once it lands (`with_current_build`, `build_for_export`). Also owns fold lines, merging the build's answer for every slide with the live render's fresher one for the slide being edited, and the waits held by whoever asked for the build (`wait_for_build`), released together on success and on failure.
- `inspector.py` — right panel holding `ThemePanel` under the one title it draws ("Deck"). `ThemePanel` used to draw a second heading of its own directly beneath it. It briefly carried a second context for the image under the cursor; that page and the stack that switched to it are gone, so adding a context means bringing the stack back.
- `theme_panel.py` / `theme_editor.py` / `theme_manager_ui.py` — the deck's settings, the theme chooser dialog and the editor UI. **One setting, one home.** Theme, aspect ratio and logo belong to the document: the panel changes them and `window._sync_frontmatter_key()` writes them into the frontmatter of any deck that pins one. Target duration, speaking rate and "Convert on save" belong to the app and live only in Preferences. Both sets used to appear in both places, in different widgets, and only one of each pair worked: frontmatter outranks the app setting (`converter._render_context()` reads `meta.get("theme", self.theme)`), so on a deck pinning `theme:` the Preferences grid moved, a build ran, and the slides came back unchanged. For the same reason the panel is told what the *document* renders at, not what the app is set to — `window._sync_panel_to_document()`, on open and on every settled edit.
- **Themes have one dialog and one editor.** `ThemePanel`'s chooser is an `Adw.NavigationView`: "Themes" picks one, and "Manage Themes" — `theme_manager_ui.build_themes_page()`, which used to be the third page of Preferences — is pushed onto it. Preferences is for settings; a library of content is not one, and while it lived there a writer chose a theme in the inspector and made one two dialogs away under a different menu. The manage page is handed a *refresher* (anything with `refresh_themes()`; in the app, the panel), rather than reaching back through a reference to the open Preferences dialog that the window used to stash for it.

  `theme_editor.py` holds one class. There were two — a four-step wizard that was the only door, *including* the Edit door, and a full-field `ThemeEditorAdvanced` reachable solely through a "Skip to advanced editor…" button in the wizard's footer — implementing `_on_install`, `_on_export_zip`, `_validate_slug`, `_render_preview`, `_collect_theme_json`, `_write_to_dir` and `_show_error` twice, and already drifted: the advanced editor's docstring claimed an entry point nothing constructed. A wizard is for a one-off sequential task; editing a theme is one object with a dozen properties, revisited a field at a time. The steps are groups on one `Adw.PreferencesPage` and what the second dialog held is behind `Adw.ExpanderRow` disclosures.

  **The wizard reset the theme it was asked to edit.** Its presets, fonts and cover styles were `Gtk.ToggleButton` groups whose initial `set_active` fired during construction, so opening Edit on a user theme replaced its palette with Classic, its body font with IBM Plex and its cover with the dark one before anything was touched. Presets are plain `Gtk.Button`s now — an action that fills the fields in, never a mode the dialog is in. For the same reason a new theme's identifier follows its name through a `_slug_touched` flag rather than a comparison against the slug the dialog opened with, which stopped following after one keystroke: typing "My Theme" left the identifier at `m`.

- The theme editor is an `Adw.Dialog`. It was an `Adw.Window` for a taskbar entry, but it was also modal and transient, so it never behaved like the independent window that justified it.
- `session.py` — persistence: window state, recent files, editor prefs, recovery files.

**Slide bands.** Separator lines are the document's real structure, so the editor draws a rule at each one naming the slide it opens (`_draw_slide_bands`), and the `presence-separator` tag opens the vertical space that rule floats in. The `---` stays visible and editable — hiding it would mean invisible text the cursor can fall into. This replaced the gutter number badges, which said the same thing twice.

**Overlay coordinates.** `buffer_to_window_coords(TEXT, …)` already accounts for the view's top margin and the view sits at the overlay's origin, so `_line_y()` needs no further adjustment. The deleted badge code added `top_margin` here and drew a margin too low.

**The fold marker.** A slide is a fixed 1280x720 box with `overflow: hidden`, so a layout knows exactly where each slide runs out of room. `renderer.py` stamps every block with `data-src-line` (slide start line from `compute_slide_start_lines()`, plus the block's line within the slide), and after layout `pagination.measure_folds()` walks WeasyPrint's box tree for the topmost block crossing the slide's text area. Both the build and the live render measure this, from the same engine, so they agree; `BuildCoordinator` keeps the live answer for the slide being edited and the build's for the rest, which is why the rule now moves as you type rather than waiting for a build. The editor draws it anchored to a `Gtk.TextMark` so it follows the content while you edit. Measured against the content box, not the page edge, because themes reserve the lower padding for the slide number and progress bar.

**A PDF page is not a slide, so the PDF drops the pages that are not slides.** WeasyPrint fragments a block that does not fit rather than clipping it, so a slide with too much text emits a continuation page — a headerless remainder starting mid-sentence, sometimes followed by a page carrying nothing but the theme's footer — and the deck's page count exceeds its slide count. No CSS fixes the fragmentation: `overflow: clip`, `break-inside: avoid` and a `max-height` wrapper were all tried.

`slides/pagination.py` owns what is read back off a laid-out page, because both engines need it and the CLI must not import GTK. `slide_page_indices()` reads the `data-slide-index` stamps off the box tree and records, per slide, the page it starts on. `slide_pages_pdf()` then writes the PDF from exactly those pages and returns the bytes **together with the page numbering inside them** — `range(n_slides)` when the trim succeeded, the original list when it could not, so nothing downstream has to assume which it got. That list lands in `slide_info["page_index"]` and is what the thumbnail strip, the image export, the handout and the slideshow all index by.

The trim is the fix for the one artifact that used to ship the fragments. Every other consumer already skipped them; `export_controller._write_pdf()` re-points the build's output and hands the file over as-is, so the deck a writer mailed to somebody was the only place the half-sliced remainders and the blank footer page showed up. A slide is `overflow: hidden` — it shows what fits — and the file now says the same thing.

**Two ways a slide runs out of room, and neither implies the other.** `measure_folds()` (also in `pagination.py`) finds the topmost block crossing the slide's *text area*, which is above the page edge because themes reserve the lower padding for the slide number and progress bar. `fragmented_slides()` finds the slides that needed a second page. A long paragraph usually trips both; **a long list trips only the second** — it breaks cleanly between items at the page edge, so nothing on the slide's own first page crosses anything, and the fold is silent about the one slide that actually lost content. `slide_info` therefore carries both `fold_line` and `clipped`, and the strip's badge and the warning are built from the union. `test_build_warnings.py` pins the list case; if it ever starts failing, the fold has become sufficient and the union can go.

Folds are still read per slide rather than per page, off each slide's own first page, since walking the pages in order would blame a slide's overrun on the next slide and leave the one that really overflowed unmarked — and they are measured on the untrimmed document, the only place the overrun is still visible.

**What a build says out loud.** Three things go wrong without changing how the slide looks: a slide overflows, a picture's path does not resolve, and a theme named in the frontmatter is not installed. All three used to be silent somewhere. `slides/diagnostics.build_warnings()` composes one sentence per fact from `slide_info`, which both engines fill the same way; the CLI logs them (Python's `lastResort` handler puts a `WARNING` on stderr with no logging configured, which is why `cli.py` needs no change to print them) and `BuildCoordinator.on_complete()` puts them in the window's banner. A missing picture is asked about separately from `image_aspect()`, which answers `None` for a broken path and a perfectly good remote URL alike; `utils.image_is_missing()` reports local files only.

**An uninstalled theme is a typo, not a refusal.** Both engines fall back — to the app's default, then to whatever is installed — and say which palette they used. They used to disagree completely: `converter._render_context()` silently took `next(iter(all_themes.values()))`, which is alphabetical, so a deck pinning `theme: clasic` came back in Academic with nothing said, while `slides/convert.py` raised and produced no deck at all. Same document, same typo, two answers.

**The keys GNOME reserves.** Ctrl+P is Print, and the nearest thing this app has to Print is Export PDF — which writes exactly the file Print-to-file would — so that is what it runs; Ctrl+Shift+E still works because it is what this app taught. Present moved to F5, which is what every other deck tool starts a slideshow with. The shortcuts reference is on Ctrl+? (F1 stays bound only because Presence ships no help manual for it to open), and Settings on Ctrl+comma — bound from `window._setup_actions()` rather than from `application.py`, which is mode `444`. `shortcuts.py` lists a row's alternates space-separated, which is GTK's own accelerator syntax; `test_shortcuts.py` asserts every bound key appears there and `test_header_controls.py` asserts each key reaches the right action, because a reference will happily document a wrong binding.

**One owner per fact.** The three controllers used to keep their state on `MainWindow` and reach in for it — 47 distinct `win._private` names between them, `BuildCoordinator` alone touching 29, all through an untyped `def __init__(self, window)`. That was not a separation; it was the same god object with the code moved to other files, and it is why `_slide_w`/`_slide_h` could be written on every build and read by nothing for as long as they were. Each fact now lives with the class that changes it, and the window *asks* (`self.documents.file_path`, `self.builds.html_uri`). What the controllers get back is a small public surface — `editor`, `sidebar`, `converter`, `banner`, `present_button`, `export_busy`, `documents`, `builds`, `exports`, `clock`, `auto_convert`, `speaking_rate`, plus `show_toast`, `show_error`, `set_document_title`, `hold_file_dialog`, `sync_panel_to_document`, `update_word_count`, `mark_modified`, `live_render_width`, `refresh_recent_actions` — and nothing private. `grep -ho "win\._[a-z_]*"` across the four controllers must come back empty; `test_window_wiring.py` checks the other direction, that every `self.documents.X` and `self.builds.X` in `window.py` resolves on the class behind it.

The two exceptions are deliberate. `MainWindow._file_path` and `._modified` survive as **read-only properties** forwarding to the document controller, because `application.py` is mode `444` and reads both when deciding whether to reuse a window. Read-only is the point: a write raises rather than silently shadowing the owner's copy, which is exactly how the stale `win._modified = False` in File > New was caught.

The header chip is the window's widget — the window builds and packs it — but everything it *says* comes from the coordinator, so its parts are handed over once with `attach_chip()`.

**The seam is written down.** Each controller declares a Protocol for the window it takes — `BuildHost` (7 names), `DocumentHost` (11), `ExportHost` (7), `ClockHost` (9) — at the top of its own module, so what it needs is next to what needs it. `tests/test_host_protocols.py` checks both directions: everything a Host promises, `MainWindow` provides; everything a controller reaches for, its Host declares. The second direction is the one that stops the seam growing back, one perfectly reasonable attribute at a time.

**Nothing forwards.** The window used to carry nineteen one-line methods that existed only so it could hand a call to a controller — `_trigger_convert`, `_update_build_chip`, `_with_current_build`, `_on_export*`, `_save`, `_autosave` and the rest — and twelve of them had no caller outside `window.py` at all. Actions, accelerators, the header chip's click, the Export popover's rows, the converter's three signals and the autosave timer are all bound straight to the controller method now. Nine one-liners remain and each has a reason: `open_file` and `restore_autosave` are called by `application.py`, which is mode `444`; `_file_path` and `_modified` are the read-only properties that same file reads; `_on_destroy` is a GTK signal handler; and `_on_save`, `_on_save_as`, `_on_open` and `_on_open_pdf_clicked` **absorb arguments** — a `Gio.SimpleAction` calls its handler with `(action, parameter)`, and `save(on_done=None)` bound directly would take the action as its continuation and call it once the file landed. Where the controller's own method already ends in `(self, *_)` — `trigger()`, `export_pdf()` — the action is bound to it and there is no window method at all.

**Not a type checker, and here is why.** `MainWindow` inherits from `Adw.ApplicationWindow` and PyGObject ships no stubs, so mypy sees the base as `Any` and `MainWindow` satisfies *every* Protocol vacuously — a bogus member added to `BuildHost` produces no error at `BuildCoordinator(self)`. The Protocols are the statement of the contract; the test is its enforcement, and it is mutation-checked in both directions. (A one-off mypy run over the seam was still worth it: it found five places where `display_path` — `Path | None` — was passed where a `Path` was required, all now narrowed at the point the path is known.)

**Sentence case, and the document's own name.** Dialog titles are sentence case ("Save as", "Open file", "Choose export folder"), which several were not; the primary menu's About item names the app ("About Presence") and is the only place that does. A window title is the document and nothing else — `set_document_title()` sets just the name, where it used to append " — Presence", and the presenter and audience windows are "Presenter" and "Slideshow". The shell already says which application a window belongs to.

**Two verbs.** Save writes the Markdown; Export writes a deck for someone else, with the format (PDF / HTML / images) as the choice inside it. The build between them is not a user-facing concept — the header chip is the only place it surfaces. Opening or revealing the working PDF is about the build rather than about handing a deck over, so it lives in the menu, not under Export.

**When a PDF build runs.** Saving does not build, and building does not save. A build happens when the document is opened, when it is explicitly asked for (the header status chip, Ctrl+Return), before anything that consumes the output (Present — the button and Ctrl+P alike — Export HTML/Images/Handout, Open PDF, all routed through `MainWindow._with_current_build()`; Export PDF goes through `_build_for_export()`, which always builds because the deck may be current and still need writing to the chosen path), and on every save only if the user enables "Convert on save" in Settings. The header chip reports whether the built PDF still matches the document, comparing text rather than tracking a modified flag. Nothing is greyed out waiting for a build: Present and Export build on demand.

**What a build reads.** The editor's buffer, always — `BuildCoordinator.trigger()` hands `converter.convert()` the text and gets out of the way. It used to hand over a *path*, and so had to write the buffer over the file on disk before every build: pressing Present, or exporting an HTML deck, silently committed the writer's unsaved edits. A deck with no path got a temporary copy of itself written out to be read straight back. Neither exists now; an unsaved deck gets one scratch `.pdf` (`_temp_pdf`, reused until the deck is saved) and nothing else. Two consequences worth keeping straight: `trigger()` can no longer decline to start a build, so callers taking a busy indicator just take it and go (`_build_for_export()`); and the base directory relative image sources resolve against comes from `_file_path.parent`, never from the output — Export PDF re-points the output at wherever the writer chose to save, and the pictures are still next to the Markdown.

**Waiting shows where it was asked for.** A control that starts work it cannot finish on the click says so in place — `_BusyIndicator` swaps the button's icon for a spinner — rather than leaving the writer to find a spinner in the header chip at the other end of the bar. Waits are counted, not flagged, because an export holds one for the build and another for the rasterizing thread and the two overlap. Every path that takes a wait must release it, including the one where `trigger()` never starts a build because the save was refused.

**One clock, and words and picture published together.** The row under the cursor does not wait for a build: `converter.render_slide_async()` lays that one slide out on a worker thread and the picture goes straight to the strip. What decides *when* any of that happens is `SettleClock`, and it is the only thing that does — a keystroke used to fan out across five timers in two files, two of them racing, and the race was settled by `_on_live_frame()` reaching into the window's pending sidebar source, cancelling it and running its callback early with the frame's own, older text.

The invariant now is that **a frame is only ever handed to a strip whose rows were read from the very text that frame was laid out from** — the frame's slide indices have to be the rows the strip is showing. So a frame does one of three things: its text is what the strip already shows, and the picture goes over; its text is the newest there is, and the words go first and the settle timer has nothing left to do; or it is neither, the writer typed on while it rendered, and it is dropped, because that same keystroke armed a render of the newer text `LIVE_MS` away. The third case is what the old ordering got wrong — it forced the strip *back* to the frame's older words — and it healed itself on the next frame, except when that frame failed to render, which left the titles and timings one edit stale with nothing on screen saying so. `test_settle_clock.py` pins all three, and the mutation that restores the old ordering fails it.

A buffer replaced wholesale is not an edit and is not debounced: `Editor.set_text()` blocks its own change handler, so opening, recovering, reordering and rewriting a frontmatter key all say `clock.document_replaced(text)` and the strip, the inspector, the word count and the header chip come up at once. The cursor poll is the backstop for a caller that forgets — it reads the buffer rather than trusting what it was last told.

**There is no slide canvas.** A second pane used to show the slide under the cursor at reading size. The strip does that now, at whatever size the slider is set to, so the pane, its F8 toggle, its persisted divider position and `preview.py` are all gone and the editor has the width back. What outlived it is the render behind it: `render_slide_async()` still feeds the strip's live row and the editor's fold marker, and `live_render_width()` is now simply the thumbnail size in device pixels — `None` when the strip is put away or Poppler and Cairo are missing, which is the only thing that stops the render happening.

**Why the size can be a slider.** Thumbnails are rasterized once per build at `thumbnails_render.THUMB_W`, which is twice `THUMBNAIL_MAX`, and scaled down by `GtkPicture`. So moving the slider is a relayout, not a re-render of the deck — and the doubling is also what a 2x display needs at full size. Break that inequality and dragging the slider either blurs the strip or starts rebuilding on every move; `test_slide_strip.py` pins it. The strip's own width follows through `sidebar_width()`, which the window feeds to *both* `min_sidebar_width` and `max_sidebar_width` — an `AdwOverlaySplitView` otherwise sizes its sidebar as a fraction of the window and the maximum alone will not widen it — and to the breakpoint that collapses the strip to a drawer.

## Slide syntax (key separators)

| Syntax | Effect |
|---|---|
| `---` on its own line | Slide separator |
| `\|\|\|` on its own line | Two-column split within a slide |
| `^^^` on its own line | Speaker notes separator |
| `![description](src)` | Image; the slide decides where it goes |

## Theme system

Themes live in `~/.local/share/presence/themes/` (user) or the system data dir. Each theme is a directory containing `theme.json` and optionally font files and a `theme.css`. Colour values in `theme.json` are validated in `css.py` before interpolation into CSS.

**The contract is written down, in `THEME-CONTRACT.md`.** What a theme owns (everything the generated stylesheet sets — its `theme.css` is appended last and wins ties), what it does not (the layout *classification* in `layout.py`, which needs a word count and an image's aspect ratio; and the six things `html.py` writes as inline styles), the four invariants that keep `pagination.py` able to read a slide back off the page, and the measured list of what WeasyPrint 68 does and does not understand. That last part exists because the alternative is folklore: Tokyo's `theme.css` blames WeasyPrint for dropping a gradient that renders fine.

**The contrast bar is not one number, because the fields do not paint the same things.** `accent` reaches `strong` and `a` at body size as well as h1–h3 and the table header fill, so it is held to `theme_editor.CONTRAST_AA` (4.5:1) — relaxing it to WCAG's large-text bar to make a failing palette pass would have been wrong. `heading_color` paints only h1–h3 (54/39/30px at weight 800–900) and `title_accent` only the 79px cover h1, so both are large text at 3:1. `accent2` is declared in the generated CSS and used by nothing there — it is live only in themes' own `theme.css`, for borders in most and for `em` in some, so it is graded only where a stylesheet sets a `color:` from it. The editor's strip reports three pairings; `theme_manager_ui` flags a sub-AA accent on the theme's own row in the manage list, importing `CONTRAST_AA` and `_contrast_ratio` from `theme_editor` rather than restating them. `tests/test_theme_editor.py` pins every preset and every built-in.
