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
5. `layout.py` — decides how a slide is laid out, as a pure function of its content: `choose_layout(cleaned_md, images, aspects)` returns one of `text`, `bleed`, `caption`, `single`, `pair`, `gallery`. It picks a width from how much text shares the slide (`auto_size`), whether each gallery cell crops or letterboxes (`cell_fit`), whether the text is short and plain enough to sit on the picture (`is_caption`), and whether two portraits should flank it. Shapes come from `utils.image_aspect()`, which reads the header via Pillow cached on mtime, so the live canvas can afford it. `AUTO_IMAGE_LAYOUT` is the single owner of what an automatically placed image looks like — no other module may read a treatment value off a parsed image.
6. `html.py` — assembles the complete HTML document; dispatches to per-slide-type renderers (`_render_title_slide`, `_render_normal_slide`, `_render_image_slide`, `_render_two_image_slide`, `_render_gallery_slide`) according to the layout plan. Gallery rows carry an inline pixel height: indefinite rows collapse images set to `height:100%`, which renders the whole grid empty in WeasyPrint.
7. `themes.py` / `theme_loader.py` — `Theme` dataclass and discovery of theme directories.
8. `thumbnails.py` / `thumbnails_render.py` — PDF-to-thumbnail rendering.
9. `handout.py` — the talk as a document: each slide's picture with the `^^^` script beneath it, images inlined as data URIs. Deliberately unthemed — a theme is display type for a room, a handout is read at arm's length.

**`src/presence/`** (GTK 4 / Libadwaita frontend):

- `application.py` / `window.py` — app lifecycle and main window.
- `editor.py` — GtkSourceView-based Markdown editor. The toolbar carries slide-structure inserts only; formatting lives in its overflow menu and the text context menu, both driven by the `editor.*` action group. The image button opens a file chooser directly — there is nothing to configure on the way in. Alt text highlights as one description.
- `converter.py` — `Converter(GObject.Object)`, the two-speed render pipeline. `build_preview()` is the fast path: Markdown → HTML for one slide, synchronous, milliseconds, drives the live canvas. `convert()` is the slow path: Markdown → HTML → PDF → thumbnails on a background thread, emitting `conversion-started / conversion-complete / conversion-failed`; it also handles watch-mode polling. Both share the cached theme/CSS resolution in `_render_context()`.
- `preview.py` — `SlideCanvas`, the live pane showing whichever slide the cursor is in. Renders HTML in a WebKit view (JavaScript disabled) and fits it with the view's zoom level; a `Gtk.DrawingArea` acts as a size sentinel because GTK 4 has no widget resize signal and the `size_allocate` vfunc is not delivered to Python subclasses of `Gtk.Box`.
- `sidebar.py` — thumbnail strip (rendered from the PDF, so it updates on build rather than on keystroke).
- `presenter.py` — presenter view (current slide + notes + timer).
- `inspector.py` — right panel holding `ThemePanel` (slide settings) under a title. It briefly carried a second context for the image under the cursor; that page and the stack that switched to it are gone, so adding a context means bringing the stack back.
- `theme_panel.py` / `theme_editor.py` / `theme_manager_ui.py` — slide settings, theme chooser dialog and editor UI.
- `session.py` — persistence: window state, recent files, editor prefs, recovery files.

**Slide bands.** Separator lines are the document's real structure, so the editor draws a rule at each one naming the slide it opens (`_draw_slide_bands`), and the `presence-separator` tag opens the vertical space that rule floats in. The `---` stays visible and editable — hiding it would mean invisible text the cursor can fall into. This replaced the gutter number badges, which said the same thing twice.

**Overlay coordinates.** `buffer_to_window_coords(TEXT, …)` already accounts for the view's top margin and the view sits at the overlay's origin, so `_line_y()` needs no further adjustment. The deleted badge code added `top_margin` here and drew a margin too low.

**The fold marker.** A slide is a fixed 1280x720 box with `overflow: hidden`, so a build knows exactly where each slide runs out of room. `renderer.py` stamps every block with `data-src-line` (slide start line from `compute_slide_start_lines()`, plus the block's line within the slide), and after layout `converter._measure_folds()` walks WeasyPrint's box tree for the topmost block crossing the slide's text area. The editor draws a rule there, anchored to a `Gtk.TextMark` so it follows the content while you edit. Measured against the content box, not the page edge, because themes reserve the lower padding for the slide number and progress bar.

**Two verbs.** Save writes the Markdown; Export writes a deck for someone else, with the format (PDF / HTML / images) as the choice inside it. The build between them is not a user-facing concept — the header chip is the only place it surfaces. Opening or revealing the working PDF is about the build rather than about handing a deck over, so it lives in the menu, not under Export.

**When a PDF build runs.** Saving does not build. A build happens when the document is opened, when it is explicitly asked for (the header status chip, Ctrl+Return), before anything that consumes the output (Present, Export PDF/HTML/Images, Open PDF — all routed through `MainWindow._with_current_build()`), and on every save only if the user enables "Convert on save" in Settings. The header chip reports whether the built PDF still matches the document, comparing text rather than tracking a modified flag.

## Slide syntax (key separators)

| Syntax | Effect |
|---|---|
| `---` on its own line | Slide separator |
| `\|\|\|` on its own line | Two-column split within a slide |
| `^^^` on its own line | Speaker notes separator |
| `![description](src)` | Image; the slide decides where it goes |

## Theme system

Themes live in `~/.local/share/presence/themes/` (user) or the system data dir. Each theme is a directory containing `theme.json` and optionally font files and a `custom.css`. Colour values in `theme.json` are validated in `css.py` before interpolation into CSS.
