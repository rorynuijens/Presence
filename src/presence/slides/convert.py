"""
convert.py — Top-level orchestration for a single conversion pass.

All error conditions raise exceptions rather than calling sys.exit() so that
this module is safe to call from a background thread or library context.
"""

import dataclasses
import logging
import tempfile
import os
from pathlib import Path

log = logging.getLogger(__name__)

from .themes       import ASPECT_RATIOS
from .theme_loader import load_all_themes
from .frontmatter  import parse_frontmatter
from .css          import build_css
from .splitter import split_slides, is_title_slide
from .html         import md_to_html_slides
from .thumbnails   import build_thumbnail_index
from .pagination   import (slide_page_indices, slide_pages_pdf,
                           measure_folds, fragmented_slides)
from .diagnostics  import build_warnings
from .utils        import (encode_logo, safe_subpath,
                           compute_slide_start_lines)



def convert(
    input_path:  Path,
    output_path: Path,
    theme_name:  str,
    ratio:       str,
    logo_path:   Path | None,
    thumbnails:  bool,
    theme_dir:   Path | None = None,
) -> None:
    """
    Convert *input_path* (Markdown) to *output_path* (PDF).

    Raises ValueError for invalid arguments, ImportError for missing
    dependencies, and OSError for I/O failures.
    """
    raw_text = input_path.read_text(encoding="utf-8")

    # ── 1. Frontmatter ────────────────────────────────────────────────────────
    meta, markdown_text = parse_frontmatter(raw_text)

    if meta.get("theme") and theme_name == "light":
        theme_name = meta["theme"]
    if meta.get("ratio") and ratio == "16:9":
        ratio = meta["ratio"]
    # Logo from frontmatter: restrict to paths within the document directory.
    # Absolute paths are rejected to prevent exfiltrating arbitrary files
    # (e.g. ~/.ssh/id_rsa) into the generated PDF.
    if meta.get("logo") and not logo_path:
        safe = safe_subpath(input_path.parent, meta["logo"])
        if safe:
            logo_path = safe

    # ── 2. Load themes ────────────────────────────────────────────────────────
    all_themes = load_all_themes(extra_dir=theme_dir)

    # A theme named in the frontmatter that is not installed is a typo, not
    # a reason to produce no deck at all.  Fall back the way the window does
    # — to the requested default, then to whatever is installed — and warn,
    # because the slides come back in a palette nobody chose and nothing
    # about them looks wrong.
    theme_warning: str | None = None
    if theme_name not in all_themes:
        if not all_themes:
            raise ValueError(
                f"Unknown theme '{theme_name}' and no themes are installed."
            )
        fallback = ("light" if "light" in all_themes
                    else sorted(all_themes)[0])
        theme_warning = (
            f"Theme '{theme_name}' is not installed — using "
            f"{all_themes[fallback].name}. "
            f"Available: {sorted(all_themes)}"
        )
        theme_name = fallback
    if ratio not in ASPECT_RATIOS:
        raise ValueError(
            f"Unknown ratio '{ratio}'. Choices: {list(ASPECT_RATIOS)}"
        )

    theme         = all_themes[theme_name]
    width, height = ASPECT_RATIOS[ratio]

    # ── 3. Logo ───────────────────────────────────────────────────────────────
    logo_b64 = encode_logo(logo_path) if logo_path else None
    if logo_path and not logo_b64:
        log.warning("Logo not found or unsupported: %s", logo_path)

    # ── 4. Per-presentation custom CSS ────────────────────────────────────────
    # Use a local variable for the temp path so cleanup is explicit and
    # does not rely on `dir()` or `locals()` inspection (fixes #4).
    tmp_css_path: str | None = None
    try:
        if meta.get("custom_css"):
            extra_css_path = safe_subpath(input_path.parent, meta["custom_css"])
            if extra_css_path and extra_css_path.exists():
                if theme.custom_css_path:
                    combined = (
                        Path(theme.custom_css_path).read_text(encoding="utf-8")
                        + "\n\n/* Presentation override */\n"
                        + extra_css_path.read_text(encoding="utf-8")
                    )
                    fd, tmp = tempfile.mkstemp(suffix=".css")
                    try:
                        os.write(fd, combined.encode("utf-8"))
                    finally:
                        os.close(fd)
                    tmp_css_path = tmp
                    # Never mutate the cached Theme object — use a copy (#31, #5)
                    theme = dataclasses.replace(theme, custom_css_path=tmp)
                else:
                    # Same: copy instead of mutating the cached object (#31)
                    theme = dataclasses.replace(
                        theme, custom_css_path=str(extra_css_path)
                    )
            else:
                log.warning("custom_css '%s' not found", meta["custom_css"])

        # ── 5. CSS ────────────────────────────────────────────────────────────
        css = build_css(theme, width, height, logo_b64)

        # ── 6. Slides ─────────────────────────────────────────────────────────
        slides = split_slides(markdown_text)
        if not slides:
            raise ValueError(
                "No slides found. Separate slides with `---` in your Markdown."
            )

        log.info("Found %d slide(s)", len(slides))
        if slides and is_title_slide(slides[0], 0):
            log.info("Slide 1 detected as title slide")

        html, slide_info = md_to_html_slides(
            slides, css, logo_b64, meta,
            width=width, height=height, theme_bg=theme.bg,
            base_url=str(input_path.parent),
            # Counted over the whole document, frontmatter included, which is
            # what the window does — the numbers name lines in the file the
            # writer has open.
            line_offsets=compute_slide_start_lines(raw_text),
        )

        # ── 7. Write HTML ──────────────────────────────────────────────────────
        html_path = output_path.with_suffix(".html")
        html_path.write_text(html, encoding="utf-8")
        log.debug("Intermediate HTML: %s", html_path)

        # ── 8. Write PDF ───────────────────────────────────────────────────────
        try:
            import weasyprint
        except ImportError:
            raise ImportError(
                "Missing weasyprint: sudo dnf install python3-weasyprint  "
                "(or pip install weasyprint)"
            )
        document = weasyprint.HTML(
            string=html, base_url=str(input_path.parent)
        ).render()

        n_slides = len(slides)
        # Where each slide landed before anything is dropped: a slide that
        # overflows leaves a continuation page behind it, so from there on
        # the n-th page is no longer the n-th slide.
        laid_out = slide_page_indices(document, n_slides)

        for info, fold in zip(slide_info,
                              measure_folds(document, n_slides, laid_out)):
            info["fold_line"] = fold
        # Measured before the trim, which is the only moment the extra pages
        # still exist to be counted.
        for index in fragmented_slides(document, laid_out):
            slide_info[index]["clipped"] = True

        # The continuation pages are not slides — they are a headerless
        # remainder starting mid-sentence, and sometimes a page holding
        # nothing but the footer.  A slide is overflow:hidden, so the deck
        # shows what fits; the file handed to a reader now says the same.
        pdf_bytes, pages = slide_pages_pdf(document, laid_out)
        output_path.write_bytes(pdf_bytes)
        for info, page in zip(slide_info, pages):
            info["page_index"] = page
        log.info("PDF written: %s", output_path)

        # Nothing here stops a deck being produced, and none of it is
        # visible in the slides themselves, so it has to be said out loud.
        for warning in build_warnings(slide_info, theme_warning):
            log.warning("%s", warning)

        # ── 9. Thumbnail index ─────────────────────────────────────────────────
        if thumbnails:
            index_path = build_thumbnail_index(
                slide_info, output_path, theme, meta
            )
            log.info("Thumbnail index: %s", index_path)

    finally:
        # Clean up any temporary combined CSS file (#4)
        if tmp_css_path is not None:
            try:
                Path(tmp_css_path).unlink(missing_ok=True)
            except OSError:
                pass
