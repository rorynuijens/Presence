"""
html.py — Assemble the full HTML document from rendered slide fragments.
"""

import base64 as _base64
import html as _html
import re as _re
from io import BytesIO as _BytesIO
from pathlib import Path as _Path
from urllib.parse import quote as _urlquote, urlparse as _urlparse

_CSS_COLOUR_RE = _re.compile(
    r'^#[0-9a-fA-F]{3,8}$'
    r'|^rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)$'
    r'|^rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*(?:0|1|0?\.\d+)\s*\)$'
    r'|^[a-zA-Z]{2,30}$'
)

# Schemes that must never appear in an <img src> attribute.
_UNSAFE_IMG_SCHEMES = frozenset(("javascript", "vbscript"))


def _safe_bg(colour: str) -> str:
    """Return colour if it is a safe CSS colour literal, else white."""
    return colour if _CSS_COLOUR_RE.match(colour) else "#ffffff"


def _apply_img_effects(
    src: str,
    base_url: "str | None",
    grayscale: int,
    blur: int,
) -> str:
    """
    Apply grayscale and/or blur to an image with Pillow and return a data URI.

    Falls back to the original *src* string if Pillow is not installed, the
    file cannot be resolved/opened, or *src* is already a data URI or remote URL.
    This ensures WeasyPrint (which ignores CSS filter) also shows the effects.
    """
    if not (grayscale > 0 or blur > 0):
        return src
    if src.startswith("data:"):
        return src

    parsed = _urlparse(src)
    if parsed.scheme in ("http", "https"):
        return src

    try:
        from PIL import Image, ImageEnhance, ImageFilter  # type: ignore[import]
    except ImportError:
        return src

    try:
        if parsed.scheme == "file":
            img_path = _Path(parsed.path)
        elif _Path(src).is_absolute():
            img_path = _Path(src)
        elif base_url:
            img_path = (_Path(base_url) / src).resolve()
        else:
            img_path = _Path(src)

        if not img_path.exists():
            return src

        img = Image.open(img_path)

        # Preserve transparency channel when present
        has_alpha = img.mode in ("RGBA", "LA", "PA")
        img = img.convert("RGBA" if has_alpha else "RGB")

        if grayscale > 0:
            img = ImageEnhance.Color(img).enhance(1.0 - grayscale / 100.0)

        if blur > 0:
            img = img.filter(ImageFilter.GaussianBlur(radius=blur))

        buf = _BytesIO()
        fmt = "PNG" if has_alpha else "JPEG"
        save_kw = {} if fmt == "PNG" else {"quality": 85, "optimize": True}
        img.save(buf, format=fmt, **save_kw)
        b64 = _base64.b64encode(buf.getvalue()).decode()
        mime = "image/png" if fmt == "PNG" else "image/jpeg"
        return f"data:{mime};base64,{b64}"

    except Exception:
        return src


from .splitter import (is_title_slide, extract_speaker_notes,
                          extract_images, infer_slide_title, split_two_columns)
from .renderer    import render_slide_content
from .layout      import AUTO_IMAGE_LAYOUT, choose_layout, cell_fit
from .utils       import image_aspect
from .frontmatter import extract_slide_directives
from .utils       import logo_img_tag, progress_bar_html


def md_to_html_slides(
    slides:   list[str],
    css:      str,
    logo_b64: str | None,
    meta:     dict,
    *,
    width:    int = 1280,
    height:   int = 720,
    theme_bg: str = "#ffffff",
    base_url: "str | None" = None,
    only_index: "int | None" = None,
    line_offsets: "list[int] | None" = None,
) -> tuple[str, list[dict]]:
    """
    Render all slides to a complete HTML string.

    Returns (html, slide_info) where slide_info is a list of
    {'title': str, 'notes': str} dicts used by the thumbnail index.

    When *only_index* is given, the returned document contains just that one
    slide's markup — slide numbering and title-slide detection still consider
    the whole deck, so the fragment is identical to the one the full document
    would contain.  Used by the live canvas, which only ever shows one slide
    and should not pay for markup it will not display.  *slide_info* always
    covers every slide.

    *line_offsets* gives each slide's starting line in the source document.
    When present, blocks carry ``data-src-line``, so a layout measurement of
    the rendered page can name the line that overflows.
    """
    slide_htmls = []
    slide_info  = []

    first_is_title = bool(slides) and is_title_slide(slides[0], 0)
    total_numbered = len(slides) - (1 if first_is_title else 0)

    bg = _safe_bg(theme_bg)

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
            "body":  cleaned_md,
        })

        page_num = i if first_is_title else i + 1
        line_offset = (line_offsets[i]
                       if line_offsets is not None and i < len(line_offsets)
                       else None)

        # Markdown rendering is the only costly step here; skip it entirely
        # for slides the caller will not display.
        if only_index is not None and i != only_index:
            continue

        if title_slide:
            html_frag = _render_title_slide(cleaned_md, meta, logo_b64,
                                            line_offset=line_offset)
        elif images:
            # Ask what the slide should be rather than branching on how many
            # images it happens to have; see layout.py for the rules.
            plan = choose_layout(
                bool(cleaned_md.strip()), images,
                word_count=len(cleaned_md.split()),
            )
            if plan.kind == "gallery":
                html_frag = _render_gallery_slide(
                    cleaned_md, images, plan,
                    page_num, total_numbered, logo_b64, theme_override,
                    width=width, height=height,
                    base_url=base_url, line_offset=line_offset,
                )
            elif plan.kind == "pair":
                html_frag = _render_two_image_slide(
                    cleaned_md, images[0], images[1],
                    page_num, total_numbered, logo_b64, theme_override,
                    height=height, theme_bg=bg, base_url=base_url,
                    line_offset=line_offset,
                )
            else:
                # Never read the parsed tokens: what the picture looks like
                # is AUTO_IMAGE_LAYOUT's business, and where it goes is the
                # plan's.
                layout = dict(AUTO_IMAGE_LAYOUT)
                if plan.size is not None:
                    layout["size"] = plan.size
                if plan.kind == "bleed":
                    # An image alone on a slide fills it.
                    layout.update(position="background",
                                  size="100", gradient=False)
                html_frag = _render_image_slide(
                    cleaned_md, images[0]["src"], layout,
                    page_num, total_numbered, logo_b64, theme_override,
                    height=height, base_url=base_url,
                    line_offset=line_offset,
                )
        else:
            html_frag = _render_normal_slide(cleaned_md, page_num,
                                             total_numbered, logo_b64,
                                             theme_override,
                                             line_offset=line_offset)

        slide_htmls.append(html_frag)

    lang = _html.escape(str(meta.get("lang", "en")) or "en")
    document = f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'self' data:; style-src 'unsafe-inline'; script-src 'none';">
<style>{css}</style>
</head>
<body>
{''.join(slide_htmls)}
</body>
</html>"""

    return document, slide_info


# ── Geometry helpers ─────────────────────────────────────────────────────────

def _image_geometry(pos: str, size: int, height: int) -> tuple[str, str, str]:
    """
    Return (img_style, grad_style, text_pad_style) inline CSS strings for a
    single-image slide panel.  All size-dependent layout is expressed as
    inline styles so any integer size 1-100 works without fixed CSS rules.
    """
    if pos == "background" or size >= 100:
        img_style  = "top:0;left:0;right:0;bottom:0;width:100%;height:100%;"
        grad_style = "top:0;left:0;right:0;bottom:0;width:100%;height:100%;"
        text_pad   = ""
    elif pos == "right":
        img_style  = f"top:0;right:0;bottom:0;width:{size}%;height:100%;"
        grad_style = f"top:0;right:0;bottom:0;width:{size}%;"
        text_pad   = f"padding-right:{size}%;"
    elif pos == "left":
        img_style  = f"top:0;left:0;bottom:0;width:{size}%;height:100%;"
        grad_style = f"top:0;left:0;bottom:0;width:{size}%;"
        text_pad   = f"padding-left:{size}%;"
    elif pos == "top":
        img_style  = f"top:0;left:0;right:0;width:100%;height:{size}%;"
        grad_style = f"top:0;left:0;right:0;height:{size}%;"
        text_pad   = f"padding-top:{int(height * size / 100)}px;"
    elif pos == "bottom":
        img_style  = f"bottom:0;left:0;right:0;width:100%;height:{size}%;"
        grad_style = f"bottom:0;left:0;right:0;height:{size}%;"
        text_pad   = f"padding-bottom:{int(height * size / 100)}px;"
    else:
        img_style = grad_style = text_pad = ""
    return img_style, grad_style, text_pad


# ── Slide type renderers ──────────────────────────────────────────────────────

def _theme_attr(theme_override: str) -> str:
    """Return a data-p-theme attribute string if an override is set."""
    if theme_override in ("dark", "light"):
        return f' data-p-theme="{theme_override}"'
    return ""


def _render_title_slide(slide_md: str, meta: dict, logo_b64: str | None,
                        line_offset: int | None = None) -> str:
    content = render_slide_content(slide_md, line_offset)

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
    *,
    height:         int = 720,
    base_url:       "str | None" = None,
    line_offset:    int | None = None,
) -> str:
    """
    Render a slide that contains an image with flexible layout.

    The gradient is rendered as an absolutely-positioned <div> with
    background-image: linear-gradient(...).  WeasyPrint supports
    background-image gradients on regular elements (stored as PDF Patterns)
    but NOT on ::after pseudo-elements, so we use a real div here.

    Geometry (position/size) is expressed via inline styles so any integer
    size 1-100 works.  data-img-pos and data-img-fade still drive gradient
    direction in CSS; data-img-fit and data-img-focal drive object-fit/position.
    """
    content   = render_slide_content(slide_md, line_offset)
    t_attr    = _theme_attr(theme_override)

    pos       = layout.get("position", "right")
    size      = int(layout.get("size", "50"))
    opacity   = layout.get("opacity", 75)
    fade      = layout.get("fade")
    fit       = layout.get("fit", "cover")
    focal     = layout.get("focal", "focal-center")
    grayscale = layout.get("grayscale", 0)
    blur      = layout.get("blur", 0)

    # Reject javascript: / vbscript: URIs unconditionally before any processing.
    if _urlparse(img_src).scheme.lower() in _UNSAFE_IMG_SCHEMES:
        img_src = ""

    # Bake grayscale/blur into the image with PIL so WeasyPrint (which does
    # not support CSS filter) shows the effect in the PDF and thumbnails.
    # Falls back to the original path if PIL is unavailable or src is remote.
    effective_src = _apply_img_effects(img_src, base_url, grayscale, blur)
    # Only emit CSS filter for effects that weren't successfully pre-processed
    # (i.e. PIL fallback path — still works in the WebKit live preview).
    css_grayscale = 0 if effective_src != img_src else grayscale
    css_blur      = 0 if effective_src != img_src else blur
    escaped_src   = _html.escape(_urlquote(effective_src, safe="+/=:;,"))
    tint      = layout.get("tint")
    flip_h    = layout.get("flip_h", False)
    flip_v    = layout.get("flip_v", False)
    zoom      = layout.get("zoom", 100)

    fade_attr  = f' data-img-fade="{_html.escape(fade)}"' if fade else ""
    fit_attr   = f' data-img-fit="{_html.escape(fit)}"' if fit != "cover" else ""
    focal_attr = (f' data-img-focal="{_html.escape(focal)}"'
                  if focal != "focal-center" and fit == "cover" else "")

    img_style, grad_style, text_pad = _image_geometry(pos, size, height)

    # Build img inline style (opacity + transforms only — filter handled below)
    img_css_parts = [f"opacity:{opacity / 100:.2f}"]
    transforms = []
    if zoom != 100:
        transforms.append(f"scale({zoom / 100:.2f})")
    if flip_h:
        transforms.append("scaleX(-1)")
    if flip_v:
        transforms.append("scaleY(-1)")
    if transforms:
        focal_origin = {
            "focal-top":    "center top",
            "focal-bottom": "center bottom",
        }.get(focal, "center center")
        img_css_parts.append(f"transform:{' '.join(transforms)}")
        img_css_parts.append(f"transform-origin:{focal_origin}")
    img_inline = ";".join(img_css_parts)

    # CSS filter fallback (PIL unavailable / remote src): blur needs a
    # negative-inset wrapper so overflow:hidden on .slide-image doesn't clip
    # blurred edges. Grayscale-only goes directly on the img.
    if css_blur > 0:
        wrap_filters = []
        if css_grayscale > 0:
            wrap_filters.append(f"grayscale({css_grayscale}%)")
        wrap_filters.append(f"blur({css_blur}px)")
        n = css_blur
        wrap_style = (
            f"position:absolute;top:-{n}px;left:-{n}px;"
            f"right:-{n}px;bottom:-{n}px;"
            f"filter:{' '.join(wrap_filters)}"
        )
        img_el = (
            f'<div style="{wrap_style}">'
            f'<img src="{escaped_src}" style="{img_inline}" alt="">'
            f'</div>'
        )
    else:
        if css_grayscale > 0:
            img_inline += f";filter:grayscale({css_grayscale}%)"
        img_el = f'<img src="{escaped_src}" style="{img_inline}" alt="">'

    tint_div = ""
    if tint:
        tint_div = (f'<div class="slide-image-tint"'
                    f' style="background:{_html.escape(tint)};"'
                    f' aria-hidden="true"></div>')

    grad_div = (f'<div class="slide-image-gradient" style="{grad_style}"'
                f' aria-hidden="true"></div>'
                if layout.get("gradient", True) else "")

    text_style = f' style="{text_pad}"' if text_pad else ""

    return (
        f'<div class="slide has-image"'
        f' data-img-pos="{_html.escape(pos)}"{fade_attr}{fit_attr}{focal_attr}{t_attr}>'
        f'  <div class="slide-image" style="{img_style}">'
        f'    {img_el}'
        f'    {tint_div}'
        f'  </div>'
        f'  {grad_div}'
        f'  <div class="slide-text"{text_style}>'
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
    line_offset:    int | None = None,
) -> str:
    cols = split_two_columns(slide_md)
    if cols:
        # The right column starts after the ||| line.  Without that shift its
        # blocks would claim the left column's line numbers, so when the
        # column cannot be located it goes unstamped rather than lying.
        right_offset = None
        if line_offset is not None and cols[1]:
            marker = slide_md.find(cols[1])
            if marker != -1:
                right_offset = line_offset + slide_md.count("\n", 0, marker)
        content = (
            '<div class="two-col">'
            f'<div class="col">{render_slide_content(cols[0], line_offset)}</div>'
            f'<div class="col">{render_slide_content(cols[1], right_offset)}</div>'
            '</div>'
        )
    else:
        content = render_slide_content(slide_md, line_offset)
    t_attr  = _theme_attr(theme_override)
    return (
        f'<div class="slide"{t_attr}>'
        f'{content}'
        f'<div class="slide-number">{page_num} / {total}</div>'
        f'{progress_bar_html(page_num, total)}'
        f'{logo_img_tag(logo_b64)}'
        f'</div>'
    )


def _render_gallery_slide(
    slide_md:       str,
    images:         list,
    plan,
    page_num:       int,
    total:          int,
    logo_b64:       str | None,
    theme_override: str = "",
    *,
    width:          int = 1280,
    height:         int = 720,
    base_url:       "str | None" = None,
    line_offset:    int | None = None,
) -> str:
    """
    Render every image in a grid, with any text above it.

    This is the only arrangement that can hold an arbitrary number of
    pictures, and it exists because the alternative was dropping them: a
    slide with three images used to show one.
    """
    text = render_slide_content(slide_md, line_offset)

    # Geometry first: the cells need their own size before they can be asked
    # whether cropping suits the picture in them.
    #
    # .slide reserves 8% of the width each side, 8% above and 11% below.
    # Text, when there is any, takes a quarter of the remaining height. Rows
    # must end up definite: an image at height:100% inside an indefinite row
    # collapses, and WeasyPrint then renders the whole grid empty.
    gap        = int(height * 0.025)
    content_w  = width * (1 - 0.16)
    content_h  = height * (1 - 0.08 - 0.11)
    cell_w     = (content_w - gap * (plan.columns - 1)) / plan.columns
    tracks     = len(images) + sum(1 for i in range(len(images))
                                   if (i + 1) in plan.spans)
    rows       = max(1, -(-tracks // plan.columns))
    reserved   = content_h * 0.24 if text else 0
    row_h      = max(40, int((content_h - reserved - gap * (rows - 1)) / rows))

    cells = []
    for index, image in enumerate(images):
        raw_src = image.get("src", "")
        if _urlparse(raw_src).scheme.lower() in _UNSAFE_IMG_SCHEMES:
            raw_src = ""
        effective = _apply_img_effects(raw_src, base_url,
                                       AUTO_IMAGE_LAYOUT["grayscale"],
                                       AUTO_IMAGE_LAYOUT["blur"])
        src = _html.escape(_urlquote(effective, safe="+/=:;,"))
        spans_two = (index + 1) in plan.spans
        span = ' data-span="2"' if spans_two else ""
        this_w = cell_w * 2 + gap if spans_two else cell_w
        fit = cell_fit(image_aspect(raw_src, base_url),
                       this_w / row_h if row_h else 0)
        cells.append(
            f'<div class="gallery-cell"{span}>'
            f'<img src="{src}" alt="" style="object-fit:{fit}">'
            f'</div>'
        )

    t_attr = _theme_attr(theme_override)
    style = (f"grid-template-columns: repeat({plan.columns}, 1fr);"
             f"grid-auto-rows: {row_h}px;")
    text_html = f'<div class="gallery-text">{text}</div>' if text else ""
    return (
        f'<div class="slide has-gallery"{t_attr}>'
        f'{text_html}'
        f'<div class="gallery" style="{style}">{"".join(cells)}</div>'
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
    *,
    height:         int = 720,
    theme_bg:       str = "#ffffff",
    base_url:       "str | None" = None,
    line_offset:    int | None = None,
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
    content = render_slide_content(slide_md, line_offset)
    t_attr  = _theme_attr(theme_override)

    layout_a = img_a.get("layout", {})
    layout_b = img_b.get("layout", {})
    pos_a     = layout_a.get("position", "left")
    pos_b     = layout_b.get("position", "right")
    size_a    = int(layout_a.get("size", "30"))
    size_b    = int(layout_b.get("size", "30"))
    grad_a    = layout_a.get("gradient", True)
    grad_b    = layout_b.get("gradient", True)
    opacity_a = layout_a.get("opacity", 75)
    opacity_b = layout_b.get("opacity", 75)
    grayscale_a = layout_a.get("grayscale", 0)
    blur_a      = layout_a.get("blur", 0)
    grayscale_b = layout_b.get("grayscale", 0)
    blur_b      = layout_b.get("blur", 0)
    raw_src_a   = img_a.get("src", "")
    raw_src_b   = img_b.get("src", "")
    # Reject javascript: / vbscript: URIs unconditionally.
    if _urlparse(raw_src_a).scheme.lower() in _UNSAFE_IMG_SCHEMES:
        raw_src_a = ""
    if _urlparse(raw_src_b).scheme.lower() in _UNSAFE_IMG_SCHEMES:
        raw_src_b = ""
    eff_src_a   = _apply_img_effects(raw_src_a, base_url, grayscale_a, blur_a)
    eff_src_b   = _apply_img_effects(raw_src_b, base_url, grayscale_b, blur_b)
    css_gs_a    = 0 if eff_src_a != raw_src_a else grayscale_a
    css_blur_a  = 0 if eff_src_a != raw_src_a else blur_a
    css_gs_b    = 0 if eff_src_b != raw_src_b else grayscale_b
    css_blur_b  = 0 if eff_src_b != raw_src_b else blur_b
    src_a  = _html.escape(_urlquote(eff_src_a, safe="+/=:;,"))
    src_b  = _html.escape(_urlquote(eff_src_b, safe="+/=:;,"))

    horizontal = {pos_a, pos_b} == {"left", "right"}
    vertical   = {pos_a, pos_b} == {"top",  "bottom"}

    if not horizontal and not vertical:
        # Unsupported position combination — degrade to single image
        return _render_image_slide(
            slide_md, img_a["src"], layout_a,
            page_num, total, logo_b64, theme_override,
            height=height, base_url=base_url,
        )

    axis = "h" if horizontal else "v"

    # Build inline geometry styles; gradient background-image stays in CSS
    # (direction is always inward, independent of size).
    if horizontal:
        img_a_style  = f"top:0;left:0;bottom:0;width:{size_a}%;height:100%;"
        img_b_style  = f"top:0;right:0;bottom:0;width:{size_b}%;height:100%;"
        grad_a_style = f"top:0;left:0;bottom:0;width:{size_a}%;"
        grad_b_style = f"top:0;right:0;bottom:0;width:{size_b}%;"
        text_style   = f"padding-left:{size_a}%;padding-right:{size_b}%;"
    else:
        img_a_style  = f"top:0;left:0;right:0;width:100%;height:{size_a}%;"
        img_b_style  = f"bottom:0;left:0;right:0;width:100%;height:{size_b}%;"
        grad_a_style = f"top:0;left:0;right:0;height:{size_a}%;"
        grad_b_style = f"bottom:0;left:0;right:0;height:{size_b}%;"
        pad_top      = int(height * size_a / 100)
        pad_bot      = int(height * size_b / 100)
        text_style   = f"padding-top:{pad_top}px;padding-bottom:{pad_bot}px;"

    # Gradient divs — real elements so WeasyPrint renders them correctly.
    grad_a_div = (f'<div class="slide-image-a-gradient" style="{grad_a_style}"'
                  f' aria-hidden="true"></div>'
                  if grad_a else "")
    grad_b_div = (f'<div class="slide-image-b-gradient" style="{grad_b_style}"'
                  f' aria-hidden="true"></div>'
                  if grad_b else "")

    # CSS filter fallback for two-image slides (PIL unavailable / remote src)
    def _two_img_el(src: str, opacity: float, css_gs: int, css_bl: int) -> str:
        style = f"opacity:{opacity:.2f}"
        if css_bl > 0:
            fparts = []
            if css_gs > 0:
                fparts.append(f"grayscale({css_gs}%)")
            fparts.append(f"blur({css_bl}px)")
            n = css_bl
            wrap = (
                f'<div style="position:absolute;top:-{n}px;left:-{n}px;'
                f'right:-{n}px;bottom:-{n}px;filter:{" ".join(fparts)}">'
                f'<img src="{src}" style="{style}" alt=""></div>'
            )
            return wrap
        if css_gs > 0:
            style += f";filter:grayscale({css_gs}%)"
        return f'<img src="{src}" style="{style}" alt="">'

    return (
        f'<div class="slide has-two-images" data-split="{axis}"{t_attr}>'
        f'  <div class="slide-image-a" style="{img_a_style}">'
        f'    {_two_img_el(src_a, opacity_a / 100, css_gs_a, css_blur_a)}'
        f'  </div>'
        f'  {grad_a_div}'
        f'  <div class="slide-image-b" style="{img_b_style}">'
        f'    {_two_img_el(src_b, opacity_b / 100, css_gs_b, css_blur_b)}'
        f'  </div>'
        f'  {grad_b_div}'
        f'  <div class="slide-text" style="{text_style}">'
        f'    {content}'
        f'    <div class="slide-number">{page_num} / {total}</div>'
        f'  </div>'
        f'  {progress_bar_html(page_num, total)}'
        f'  {logo_img_tag(logo_b64)}'
        f'</div>'
    )
