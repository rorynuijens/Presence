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
presence-cli input.md        # headless CLI, no GTK required
```

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
display — but it is no longer the only option.

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
5. `layout.py` — decides how a slide is laid out: `choose_layout(cleaned_md, images, aspects, side_ordinal)` returns one of `text`, `bleed`, `single`, `pair`, `gallery`. It picks a width from how much text shares the slide (`auto_size`: half and half until the words stop fitting in half a slide), whether each gallery cell crops or letterboxes (`cell_fit`), and whether two portraits should flank it. **A lone picture goes beside the words, never behind them** — only a slide with no text at all gets a full-bleed image, because there is nothing to set it beside. A picture is shown as it is: the gradient that used to wash the theme's background across its edge, and the 75% opacity that faded the whole thing, both existed to keep a caption legible on a photograph and went when the caption did. `AUTO_IMAGE_LAYOUT` has no `gradient` or `fade` key, `css.py` no rule to honour one, and `opacity` is 100 — which is what the gallery always rendered at, since it never read the value. Which side it takes alternates down the deck (`single_side`), and that is the one thing here that is not a function of the slide's own content: `side_ordinal` counts the lone pictures before it, so `html.py` must count them over *every* slide, before its `only_index` skip, or a single-slide render and the build would put the same picture on opposite sides. Shapes come from `utils.image_aspect()`, which reads the header via Pillow cached on mtime, so the live render can afford it. `AUTO_IMAGE_LAYOUT` is the single owner of what an automatically placed image looks like — no other module may read a treatment value off a parsed image.
6. `html.py` — assembles the complete HTML document; dispatches to per-slide-type renderers (`_render_title_slide`, `_render_normal_slide`, `_render_image_slide`, `_render_two_image_slide`, `_render_gallery_slide`) according to the layout plan. Gallery rows carry an inline pixel height: indefinite rows collapse images set to `height:100%`, which renders the whole grid empty. Every slide div is stamped with `data-slide-index` by `_stamp_slide_index()` at the dispatch site, not in the five renderers, so a new layout cannot be added without it.
7. `themes.py` / `theme_loader.py` — `Theme` dataclass and discovery of theme directories.
8. `thumbnails.py` / `thumbnails_render.py` — PDF-to-picture rendering, by Poppler and Cairo. `render_thumbnails()` (strip), `render_slides_hires()` (image and handout export) and `render_page_png()` (one page, for the strip's live row and the slideshow) all take an optional `pages` list saying which PDF page each slide starts on.
9. `handout.py` — the talk as a document: each slide's picture with the `^^^` script beneath it, images inlined as data URIs. Deliberately unthemed — a theme is display type for a room, a handout is read at arm's length.

**`src/presence/`** (GTK 4 / Libadwaita frontend):

- `application.py` / `window.py` — app lifecycle and main window.
- `editor.py` — GtkSourceView-based Markdown editor. The toolbar carries slide-structure inserts only; formatting lives in its overflow menu and the text context menu, both driven by the `editor.*` action group. The image button opens a file chooser directly — there is nothing to configure on the way in. Alt text highlights as one description. Both the chooser and drag-and-drop go through `_copy_into_assets()`, which copies the picture next to the document under a name `splitter._IMAGE_RE` can carry: that pattern ends an image source at whitespace or a closing paren, so `_asset_name()` turns "My Holiday Photo.png" into `My-Holiday-Photo.png` and "shot(1).png" into `shot-1.png`. Without it those tags parse as no image, or as a truncated path. A different picture of the same name gets a numeric suffix rather than replacing what an existing tag points at.
- `converter.py` — `Converter(GObject.Object)`, the render pipeline. One engine, three speeds: `build_preview()` is Markdown → HTML for one slide, synchronous and sub-millisecond, with no layout — the cheapest way to ask what a slide's markup is. `render_slide_async()` is the live path: one slide → HTML → WeasyPrint → PDF → PNG plus its fold line, on a worker thread. `convert()` is the whole deck → HTML → PDF → thumbnails, emitting `conversion-started / conversion-complete / conversion-failed`; it also handles watch-mode polling. All three share the cached theme/CSS resolution in `_render_context()`.
- `sidebar.py` — thumbnail strip. Two sources feed it and they are kept apart on purpose: everything the document alone can answer — title, number, script indicator, per-slide timing — is read out of the text by `read_slides()` on every pause in typing, so it is never a build behind; only the picture and the overflow badge need a build. Pictures are matched to slides by content, never by index (`carry_over()`), and a row whose own slide has changed since the build is marked out of date rather than showing a stale picture silently. How big it draws them is the writer's, set by a slider in the strip's own toolbar between `THUMBNAIL_MIN` and `THUMBNAIL_MAX` and persisted as `thumbnail_size` in the window state.
- `presenter.py` — presenter view (current slide + notes + timer). `SlideshowWindow` — the audience screen — shows pages of the built PDF as textures, pre-rendered on a thread when it opens. Navigation is a texture swap and blanking is `set_blank()`; there is no browser and no JavaScript anywhere in the app's own output.
- `export_controller.py` — the four export flows (PDF / HTML / images / handout) behind one `_ask_save_path()`: dialog, suffix defaulting, writability check. PDF export re-points the build; the other three consume one via `_with_current_build()`.
- `document_controller.py` — the document as a file: open, save, save-as, autosave, the unsaved-changes question, and the `.pres` bundle's Markdown-inside-a-directory arrangement. Owns `UNTITLED`. **A save does not always finish before it returns:** a document with no path has to ask for a filename, and that answer arrives through a GTK callback. `save()`, `write_document()` and `save_as_dialog()` therefore take an `on_done` callback, and anything that must not happen until the file exists — closing the window, replacing the buffer — goes there rather than on the line after the call. The return value only says the save has not *already* failed.
- `build_coordinator.py` — when a build runs, what the header chip says, and what runs once it lands (`with_current_build`). Also owns fold lines, merging the build's answer for every slide with the live render's fresher one for the slide being edited, and the waits held by whoever asked for the build (`wait_for_build`), released together on success and on failure.
- `inspector.py` — right panel holding `ThemePanel` under the one title it draws ("Deck"). `ThemePanel` used to draw a second heading of its own directly beneath it. It briefly carried a second context for the image under the cursor; that page and the stack that switched to it are gone, so adding a context means bringing the stack back.
- `theme_panel.py` / `theme_editor.py` / `theme_manager_ui.py` — the deck's settings, the theme chooser dialog and the editor UI. **One setting, one home.** Theme, aspect ratio and logo belong to the document: the panel changes them and `window._sync_frontmatter_key()` writes them into the frontmatter of any deck that pins one. Target duration, speaking rate and "Convert on save" belong to the app and live only in Preferences. Both sets used to appear in both places, in different widgets, and only one of each pair worked: frontmatter outranks the app setting (`converter._render_context()` reads `meta.get("theme", self.theme)`), so on a deck pinning `theme:` the Preferences grid moved, a build ran, and the slides came back unchanged. For the same reason the panel is told what the *document* renders at, not what the app is set to — `window._sync_panel_to_document()`, on open and on every settled edit.
- **Themes have one dialog and one editor.** `ThemePanel`'s chooser is an `Adw.NavigationView`: "Themes" picks one, and "Manage Themes" — `theme_manager_ui.build_themes_page()`, which used to be the third page of Preferences — is pushed onto it. Preferences is for settings; a library of content is not one, and while it lived there a writer chose a theme in the inspector and made one two dialogs away under a different menu. The manage page is handed a *refresher* (anything with `refresh_themes()`; in the app, the panel), rather than reaching back through a reference to the open Preferences dialog that the window used to stash for it.

  `theme_editor.py` holds one class. There were two — a four-step wizard that was the only door, *including* the Edit door, and a full-field `ThemeEditorAdvanced` reachable solely through a "Skip to advanced editor…" button in the wizard's footer — implementing `_on_install`, `_on_export_zip`, `_validate_slug`, `_render_preview`, `_collect_theme_json`, `_write_to_dir` and `_show_error` twice, and already drifted: the advanced editor's docstring claimed an entry point nothing constructed. A wizard is for a one-off sequential task; editing a theme is one object with a dozen properties, revisited a field at a time. The steps are groups on one `Adw.PreferencesPage` and what the second dialog held is behind `Adw.ExpanderRow` disclosures.

  **The wizard reset the theme it was asked to edit.** Its presets, fonts and cover styles were `Gtk.ToggleButton` groups whose initial `set_active` fired during construction, so opening Edit on a user theme replaced its palette with Classic, its body font with IBM Plex and its cover with the dark one before anything was touched. Presets are plain `Gtk.Button`s now — an action that fills the fields in, never a mode the dialog is in. For the same reason a new theme's identifier follows its name through a `_slug_touched` flag rather than a comparison against the slug the dialog opened with, which stopped following after one keystroke: typing "My Theme" left the identifier at `m`.

- The theme editor is an `Adw.Dialog`. It was an `Adw.Window` for a taskbar entry, but it was also modal and transient, so it never behaved like the independent window that justified it.
- `session.py` — persistence: window state, recent files, editor prefs, recovery files.

**Slide bands.** Separator lines are the document's real structure, so the editor draws a rule at each one naming the slide it opens (`_draw_slide_bands`), and the `presence-separator` tag opens the vertical space that rule floats in. The `---` stays visible and editable — hiding it would mean invisible text the cursor can fall into. This replaced the gutter number badges, which said the same thing twice.

**Overlay coordinates.** `buffer_to_window_coords(TEXT, …)` already accounts for the view's top margin and the view sits at the overlay's origin, so `_line_y()` needs no further adjustment. The deleted badge code added `top_margin` here and drew a margin too low.

**The fold marker.** A slide is a fixed 1280x720 box with `overflow: hidden`, so a layout knows exactly where each slide runs out of room. `renderer.py` stamps every block with `data-src-line` (slide start line from `compute_slide_start_lines()`, plus the block's line within the slide), and after layout `converter._measure_folds()` walks WeasyPrint's box tree for the topmost block crossing the slide's text area. Both the build and the live render measure this, from the same engine, so they agree; `BuildCoordinator` keeps the live answer for the slide being edited and the build's for the rest, which is why the rule now moves as you type rather than waiting for a build. The editor draws it anchored to a `Gtk.TextMark` so it follows the content while you edit. Measured against the content box, not the page edge, because themes reserve the lower padding for the slide number and progress bar.

**A PDF page is not a slide.** WeasyPrint fragments a block that does not fit rather than clipping it, so a slide with too much text emits a continuation page and the deck's page count exceeds its slide count. `converter._slide_page_indices()` reads the `data-slide-index` stamps back off the laid-out box tree and records, per slide, the page it starts on; that list lands in `slide_info["page_index"]` and is what the thumbnail strip, the image export, the handout and the slideshow all index by — and what `_measure_folds()` reads each slide's fold off, since walking the pages in order would blame a slide's overrun on the next slide and leave the one that really overflowed unmarked. No CSS fixes the fragmentation — `overflow: clip`, `break-inside: avoid` and a `max-height` wrapper were all tried.

**The keys GNOME reserves.** Ctrl+P is Print, and the nearest thing this app has to Print is Export PDF — which writes exactly the file Print-to-file would — so that is what it runs; Ctrl+Shift+E still works because it is what this app taught. Present moved to F5, which is what every other deck tool starts a slideshow with. The shortcuts reference is on Ctrl+? (F1 stays bound only because Presence ships no help manual for it to open), and Settings on Ctrl+comma — bound from `window._setup_actions()` rather than from `application.py`, which is mode `444`. `shortcuts.py` lists a row's alternates space-separated, which is GTK's own accelerator syntax; `test_shortcuts.py` asserts every bound key appears there and `test_header_controls.py` asserts each key reaches the right action, because a reference will happily document a wrong binding.

**Two verbs.** Save writes the Markdown; Export writes a deck for someone else, with the format (PDF / HTML / images) as the choice inside it. The build between them is not a user-facing concept — the header chip is the only place it surfaces. Opening or revealing the working PDF is about the build rather than about handing a deck over, so it lives in the menu, not under Export.

**When a PDF build runs.** Saving does not build. A build happens when the document is opened, when it is explicitly asked for (the header status chip, Ctrl+Return), before anything that consumes the output (Present — the button and Ctrl+P alike — Export HTML/Images/Handout, Open PDF, all routed through `MainWindow._with_current_build()`; Export PDF goes through `_build_for_export()`, which always builds because the deck may be current and still need writing to the chosen path), and on every save only if the user enables "Convert on save" in Settings. The header chip reports whether the built PDF still matches the document, comparing text rather than tracking a modified flag. Nothing is greyed out waiting for a build: Present and Export build on demand.

**Waiting shows where it was asked for.** A control that starts work it cannot finish on the click says so in place — `_BusyIndicator` swaps the button's icon for a spinner — rather than leaving the writer to find a spinner in the header chip at the other end of the bar. Waits are counted, not flagged, because an export holds one for the build and another for the rasterizing thread and the two overlap. Every path that takes a wait must release it, including the one where `trigger()` never starts a build because the save was refused.

**Two clocks on the strip.** The row under the cursor does not wait for a build: `converter.render_slide_async()` lays that one slide out on a worker thread and `_on_live_frame()` hands the picture straight to the strip. Order matters there — the strip's text update is debounced by the editor and again by the window, so it lands *after* a render; the frame flushes it first, with the text the frame was laid out from, or the words catching up behind would mark a picture out of date that is in fact newer than they are.

**There is no slide canvas.** A second pane used to show the slide under the cursor at reading size. The strip does that now, at whatever size the slider is set to, so the pane, its F8 toggle, its persisted divider position and `preview.py` are all gone and the editor has the width back. What outlived it is the render behind it: `render_slide_async()` still feeds the strip's live row and the editor's fold marker, and `_live_render_width()` is now simply the thumbnail size in device pixels — `None` when the strip is put away or Poppler and Cairo are missing, which is the only thing that stops the render happening.

**Why the size can be a slider.** Thumbnails are rasterized once per build at `thumbnails_render.THUMB_W`, which is twice `THUMBNAIL_MAX`, and scaled down by `GtkPicture`. So moving the slider is a relayout, not a re-render of the deck — and the doubling is also what a 2x display needs at full size. Break that inequality and dragging the slider either blurs the strip or starts rebuilding on every move; `test_slide_strip.py` pins it. The strip's own width follows through `sidebar_width()`, which the window feeds to *both* `min_sidebar_width` and `max_sidebar_width` — an `AdwOverlaySplitView` otherwise sizes its sidebar as a fraction of the window and the maximum alone will not widen it — and to the breakpoint that collapses the strip to a drawer.

## Slide syntax (key separators)

| Syntax | Effect |
|---|---|
| `---` on its own line | Slide separator |
| `\|\|\|` on its own line | Two-column split within a slide |
| `^^^` on its own line | Speaker notes separator |
| `![description](src)` | Image; the slide decides where it goes |

## Theme system

Themes live in `~/.local/share/presence/themes/` (user) or the system data dir. Each theme is a directory containing `theme.json` and optionally font files and a `custom.css`. Colour values in `theme.json` are validated in `css.py` before interpolation into CSS.

**The contrast bar is not one number, because the fields do not paint the same things.** `accent` reaches `strong` and `a` at body size as well as h1–h3 and the table header fill, so it is held to `theme_editor.CONTRAST_AA` (4.5:1) — relaxing it to WCAG's large-text bar to make a failing palette pass would have been wrong. `heading_color` paints only h1–h3 (54/39/30px at weight 800–900) and `title_accent` only the 79px cover h1, so both are large text at 3:1. `accent2` is declared in the generated CSS and used by nothing there — it is live only in themes' own `theme.css`, for borders in most and for `em` in some, so it is graded only where a stylesheet sets a `color:` from it. The editor's strip reports three pairings; `theme_manager_ui` flags a sub-AA accent on the theme's own row in the manage list, importing `CONTRAST_AA` and `_contrast_ratio` from `theme_editor` rather than restating them. `tests/test_theme_editor.py` pins every preset and every built-in.
