# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
python3 -m app          # from the repo root
```

## Running tests

```bash
pytest md_to_slides/     # all tests
pytest md_to_slides/test_slides.py   # single file
pytest md_to_slides/test_slides.py::test_split_basic  # single test
```

## Installing (Flatpak / Meson)

```bash
meson setup build
meson install -C build
```

## Architecture

The codebase has two distinct layers:

**`md_to_slides/`** — pure library, no GTK dependency. Pipeline:

1. `frontmatter.py` — strips and parses YAML frontmatter (`---` block at top).
2. `slides.py` — splits Markdown into per-slide strings on `---` separators; also handles `|||` (two-column split), `^^^` (speaker notes), image layout tokens in alt-text, and title-slide detection.
3. `renderer.py` — converts Markdown fragments to HTML using `markdown-it-py` with optional Pygments syntax highlighting.
4. `css.py` — builds the full CSS string from a `Theme` object; uses `build_css(theme, width, height, logo_b64)`.
5. `html.py` — assembles the complete HTML document; calls the steps above and dispatches to per-slide-type renderers (`_render_title_slide`, `_render_normal_slide`, `_render_image_slide`, `_render_two_image_slide`).
6. `converter.py` — `Converter(GObject.Object)` bridges the library to GTK: runs conversion on a background thread, emits `conversion-started / conversion-complete / conversion-failed` GObject signals, and handles watch-mode polling.

**`app/`** — GTK 4 / Libadwaita frontend:

- `application.py` / `window.py` — app lifecycle and main window.
- `editor.py` — GtkSourceView-based Markdown editor with slide-number badges in the gutter.
- `sidebar.py` — thumbnail strip.
- `presenter.py` — presenter view (current slide + notes + timer).
- `theme_panel.py` / `theme_editor.py` / `theme_manager_ui.py` — theme browser and editor UI.
- `session.py` — persistence: window state, recent files, editor prefs, recovery files.
- `converter.py` (app layer) — thin wrapper around `md_to_slides.converter.Converter`, wired to GTK signals.

## Slide syntax (key separators)

| Syntax | Effect |
|---|---|
| `---` on its own line | Slide separator |
| `\|\|\|` on its own line | Two-column split within a slide |
| `^^^` on its own line | Speaker notes separator |
| `![alt\|pos\|size](src)` | Image with layout tokens in alt-text |

## Theme system

Themes live in `~/.local/share/presence/themes/` (user) or the system data dir. Each theme is a directory containing `theme.json` and optionally font files and a `custom.css`. `theme_loader.py` discovers and loads them; `themes.py` defines the `Theme` dataclass. Colour values sourced from `theme.json` are validated against a CSS allowlist in `css.py` to prevent injection.
