# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
2. `splitter.py` — splits Markdown into per-slide strings on `---` separators; also handles `|||` (two-column split), `^^^` (speaker notes), image layout tokens in alt-text, and title-slide detection. All operations work on raw strings only.
3. `renderer.py` — converts Markdown fragments to HTML using `markdown-it-py` with optional Pygments syntax highlighting.
4. `css.py` — builds the full CSS string from a `Theme` object via `build_css(theme, width, height, logo_b64)`. Colour values from theme files are validated against an allowlist to prevent CSS injection.
5. `html.py` — assembles the complete HTML document; dispatches to per-slide-type renderers (`_render_title_slide`, `_render_normal_slide`, `_render_image_slide`, `_render_two_image_slide`).
6. `themes.py` / `theme_loader.py` — `Theme` dataclass and discovery of theme directories.
7. `thumbnails.py` / `thumbnails_render.py` — PDF-to-thumbnail rendering.

**`src/presence/`** (GTK 4 / Libadwaita frontend):

- `application.py` / `window.py` — app lifecycle and main window.
- `editor.py` — GtkSourceView-based Markdown editor with slide-number badges in the gutter.
- `converter.py` — `Converter(GObject.Object)`, the two-speed render pipeline. `build_preview()` is the fast path: Markdown → HTML for one slide, synchronous, milliseconds, drives the live canvas. `convert()` is the slow path: Markdown → HTML → PDF → thumbnails on a background thread, emitting `conversion-started / conversion-complete / conversion-failed`; it also handles watch-mode polling. Both share the cached theme/CSS resolution in `_render_context()`.
- `preview.py` — `SlideCanvas`, the live pane showing whichever slide the cursor is in. Renders HTML in a WebKit view (JavaScript disabled) and fits it with the view's zoom level; a `Gtk.DrawingArea` acts as a size sentinel because GTK 4 has no widget resize signal and the `size_allocate` vfunc is not delivered to Python subclasses of `Gtk.Box`.
- `sidebar.py` — thumbnail strip (rendered from the PDF, so it updates on build rather than on keystroke).
- `presenter.py` — presenter view (current slide + notes + timer).
- `theme_panel.py` / `theme_editor.py` / `theme_manager_ui.py` — theme browser and editor UI.
- `session.py` — persistence: window state, recent files, editor prefs, recovery files.

**When a PDF build runs.** Saving does not build. A build happens when the document is opened, when it is explicitly asked for (the header status chip, Ctrl+Return), before anything that consumes the output (Present, Export PDF/HTML/Images, Open PDF — all routed through `MainWindow._with_current_build()`), and on every save only if the user enables "Convert on save" in Settings. The header chip reports whether the built PDF still matches the document, comparing text rather than tracking a modified flag.

## Slide syntax (key separators)

| Syntax | Effect |
|---|---|
| `---` on its own line | Slide separator |
| `\|\|\|` on its own line | Two-column split within a slide |
| `^^^` on its own line | Speaker notes separator |
| `![alt\|pos\|size](src)` | Image with layout tokens in alt-text |

## Theme system

Themes live in `~/.local/share/presence/themes/` (user) or the system data dir. Each theme is a directory containing `theme.json` and optionally font files and a `custom.css`. Colour values in `theme.json` are validated in `css.py` before interpolation into CSS.
