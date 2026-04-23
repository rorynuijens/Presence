"""
html.py — Assemble the full HTML document from rendered slide fragments.
"""

import html as _html

from .slides      import (is_title_slide, extract_speaker_notes,
                          extract_images, infer_slide_title, split_two_columns)
from .renderer    import render_slide_content
from .frontmatter import extract_slide_directives
from .utils       import logo_img_tag, progress_bar_html


def md_to_html_slides(
    slides:   list[str],
    css:      str,
    logo_b64: str | None,
    meta:     dict,
) -> tuple[str, list[dict]]:
    """
    Render all slides to a complete HTML string.

    Returns (html, slide_info) where slide_info is a list of
    {'title': str, 'notes': str} dicts used by the thumbnail index.
    """
    slide_htmls = []
    slide_info  = []

    first_is_title = bool(slides) and is_title_slide(slides[0], 0)
    total_numbered = len(slides) - (1 if first_is_title else 0)

    for i, slide_md in enumerate(slides):
        slide_body, notes = extract_speaker_notes(slide_md)
        cleaned_md, images = extract_images(slide_body)
        title_slide = is_title_slide(slide_body, i)

        # Extract per-slide directives (e.g. <!-- theme: dark -->)
        directives = extract_slide_directives(slide_body)
        theme_override = directives.get("theme", "")

        slide_info.append({
            "title": infer_slide_title(slide_body, fallback=f"Slide {i + 1}"),
            "notes": notes,
        })

        if title_slide:
            html_frag = _render_title_slide(cleaned_md, meta, logo_b64)
        elif images:
            page_num = i if first_is_title else i + 1
            if len(images) >= 2:
                # Two images: render side-by-side or top/bottom split
                html_frag = _render_two_image_slide(
                    cleaned_md, images[0], images[1],
                    page_num, total_numbered, logo_b64, theme_override,
                )
            else:
                html_frag = _render_image_slide(
                    cleaned_md, images[0]["src"], images[0]["layout"],
                    page_num, total_numbered, logo_b64, theme_override,
                )
        else:
            page_num  = i if first_is_title else i + 1
            html_frag = _render_normal_slide(cleaned_md, page_num,
                                             total_numbered, logo_b64,
                                             theme_override)

        slide_htmls.append(html_frag)

    document = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>{css}</style>
</head>
<body>
{''.join(slide_htmls)}
</body>
</html>"""

    return document, slide_info


# ── Slide type renderers ──────────────────────────────────────────────────────

def _theme_attr(theme_override: str) -> str:
    """Return a data-p-theme attribute string if an override is set."""
    if theme_override in ("dark", "light"):
        return f' data-p-theme="{theme_override}"'
    return ""


def _render_title_slide(slide_md: str, meta: dict, logo_b64: str | None) -> str:
    content = render_slide_content(slide_md)

    meta_parts = []
    if meta.get("author"):
        meta_parts.append(_html.escape(str(meta["author"])))
    if meta.get("date"):
        meta_parts.append(_html.escape(str(meta["date"])))
    meta_html = (
        f'<p class="title-meta">{" · ".join(meta_parts)}</p>'
        if meta_parts else ""
    )

    return (
        f'<div class="slide title-slide">'
        f'{content}'
        f'{meta_html}'
        f'{logo_img_tag(logo_b64)}'
        f'</div>'
    )


def _render_image_slide(
    slide_md:       str,
    img_src:        str,
    layout:         dict,
    page_num:       int,
    total:          int,
    logo_b64:       str | None,
    theme_override: str = "",
) -> str:
    """
    Render a slide that contains an image with flexible layout.

    The gradient is rendered as an absolutely-positioned <div> with
    background-image: linear-gradient(...).  WeasyPrint supports
    background-image gradients on regular elements (stored as PDF Patterns)
    but NOT on ::after pseudo-elements, so we use a real div here.

    data-img-pos  : left | right | top | bottom  (drives CSS positioning)
    data-img-size : 30 | 50 | 70                 (drives CSS sizing)
    """
    content     = render_slide_content(slide_md)
    escaped_src = _html.escape(img_src)
    t_attr      = _theme_attr(theme_override)

    pos      = _html.escape(layout.get("position", "right"))
    size     = _html.escape(layout.get("size",     "50"))
    # Gradient div: a real element so WeasyPrint renders it as a PDF Pattern.
    # The CSS class slide-image-gradient handles direction per data-img-pos.
    grad_div = '<div class="slide-image-gradient" aria-hidden="true"></div>' \
               if layout.get("gradient", True) else ""

    return (
        f'<div class="slide has-image"'
        f' data-img-pos="{pos}" data-img-size="{size}"{t_attr}>'
        f'  <div class="slide-image"><img src="{escaped_src}" alt=""></div>'
        f'  {grad_div}'
        f'  <div class="slide-text">'
        f'    {content}'
        f'    <div class="slide-number">{page_num} / {total}</div>'
        f'  </div>'
        f'  {progress_bar_html(page_num, total)}'
        f'  {logo_img_tag(logo_b64)}'
        f'</div>'
    )


def _render_normal_slide(
    slide_md:       str,
    page_num:       int,
    total:          int,
    logo_b64:       str | None,
    theme_override: str = "",
) -> str:
    cols = split_two_columns(slide_md)
    if cols:
        content = (
            '<div class="two-col">'
            f'<div class="col">{render_slide_content(cols[0])}</div>'
            f'<div class="col">{render_slide_content(cols[1])}</div>'
            '</div>'
        )
    else:
        content = render_slide_content(slide_md)
    t_attr  = _theme_attr(theme_override)
    return (
        f'<div class="slide"{t_attr}>'
        f'{content}'
        f'<div class="slide-number">{page_num} / {total}</div>'
        f'{progress_bar_html(page_num, total)}'
        f'{logo_img_tag(logo_b64)}'
        f'</div>'
    )


def _render_two_image_slide(
    slide_md:       str,
    img_a:          dict,
    img_b:          dict,
    page_num:       int,
    total:          int,
    logo_b64:       str | None,
    theme_override: str = "",
) -> str:
    """
    Render a slide with two images.

    Layout is determined by the positions of the two images:
      left + right  → horizontal split: [img] [text] [img]
      top  + bottom → vertical split:   [img] / [text] / [img]
      other mix     → falls back to the first image only

    The text content sits in the centre between the two images.
    Each image panel uses its own size token (default 30% each,
    leaving 40% for text).  Gradients face inward toward the text.
    """
    content = render_slide_content(slide_md)
    t_attr  = _theme_attr(theme_override)

    layout_a = img_a.get("layout", {})
    layout_b = img_b.get("layout", {})
    pos_a  = layout_a.get("position", "left")
    pos_b  = layout_b.get("position", "right")
    size_a = _html.escape(layout_a.get("size", "30"))
    size_b = _html.escape(layout_b.get("size", "30"))
    grad_a = "1" if layout_a.get("gradient", True) else "0"
    grad_b = "1" if layout_b.get("gradient", True) else "0"
    src_a  = _html.escape(img_a.get("src", ""))
    src_b  = _html.escape(img_b.get("src", ""))

    horizontal = {pos_a, pos_b} == {"left", "right"}
    vertical   = {pos_a, pos_b} == {"top",  "bottom"}

    if not horizontal and not vertical:
        # Unsupported position combination — degrade to single image
        return _render_image_slide(
            slide_md, img_a["src"], layout_a,
            page_num, total, logo_b64, theme_override,
        )

    axis = "h" if horizontal else "v"

    # Gradient divs — real elements so WeasyPrint renders them correctly
    grad_a_div = ('<div class="slide-image-a-gradient" aria-hidden="true"></div>'
                  if grad_a == '1' else '')
    grad_b_div = ('<div class="slide-image-b-gradient" aria-hidden="true"></div>'
                  if grad_b == '1' else '')

    return (
        f'<div class="slide has-two-images" data-split="{axis}"'
        f' data-size-a="{size_a}" data-size-b="{size_b}"'
        f' data-grad-a="{grad_a}" data-grad-b="{grad_b}"'
        f'{t_attr}>'
        f'  <div class="slide-image-a"><img src="{src_a}" alt=""></div>'
        f'  {grad_a_div}'
        f'  <div class="slide-image-b"><img src="{src_b}" alt=""></div>'
        f'  {grad_b_div}'
        f'  <div class="slide-text">'
        f'    {content}'
        f'    <div class="slide-number">{page_num} / {total}</div>'
        f'  </div>'
        f'  {progress_bar_html(page_num, total)}'
        f'  {logo_img_tag(logo_b64)}'
        f'</div>'
    )
