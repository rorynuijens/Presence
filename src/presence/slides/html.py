"""
html.py — Assemble the full HTML document from rendered slide fragments.
"""

import base64 as _base64
import html as _html
from dataclasses import replace as _replace
from functools import lru_cache
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


# The filters that have to be baked into the pixels. WeasyPrint drops CSS
# `filter` at parse time — measured against 68.1, and recorded in
# THEME-CONTRACT.md — so there is no stylesheet answer to any of these. The
# same is true of `mix-blend-mode`, which is why tint is a blend done here
# rather than an overlay div: an overlay only ever covered the single-image
# layout, and washing a colour over the picture is the same operation
# whether the picture is alone, one of a pair, or a cell in a gallery.
_PIXEL_FILTERS = ("bw", "greyscale", "sepia", "blur", "lighten", "darken")

# What each named filter does, in the amount the name implies. A writer picks
# an effect, not a number — the one exception is blur, whose right strength
# depends on the picture, and which therefore carries its own token argument.
_SEPIA_DARK  = "#2b1a08"
_SEPIA_LIGHT = "#ffe8c0"
_BRIGHTEN    = 1.35
_DARKEN      = 0.62
_TINT_BLEND  = 0.38


def _treatment_key(layout: dict) -> tuple:
    """
    The part of *layout* that changes the picture's pixels, as a cache key.

    Placement, fit and opacity are not in here: those are the stylesheet's to
    apply and a theme's to argue with, so the same file baked once serves
    every arrangement it appears in.
    """
    filt = layout.get("filter")
    return (filt if filt in _PIXEL_FILTERS else None,
            int(layout.get("blur", 0) or 0),
            layout.get("tint") or None)


def _apply_img_effects(src: str, base_url: "str | None",
                       layout: dict) -> str:
    """
    Bake this picture's colour treatment into its pixels; return a data URI.

    Falls back to *src* unchanged when there is nothing to do, when Pillow is
    missing, when the file cannot be opened, or when the source is remote or
    already inline — a network round trip mid-render is not worth a filter,
    and a data URI carries bytes this cannot second-guess.

    This is the only place a treatment becomes visible, so it is also the
    only place that needs to know a filter is exclusive: one named effect,
    optionally tinted, per picture.
    """
    key = _treatment_key(layout)
    if key == (None, 0, None):
        return src
    if src.startswith("data:"):
        return src

    parsed = _urlparse(src)
    if parsed.scheme in ("http", "https"):
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
        stat = img_path.stat()
    except OSError:
        return src

    return _baked(str(img_path), stat.st_mtime, stat.st_size, *key) or src


@lru_cache(maxsize=256)
def _baked(path: str, mtime: float, nbytes: int,
           filt: "str | None", blur: int, tint: "str | None") -> "str | None":
    """
    The cached bake. Keyed on the file's mtime and size as well as its name,
    so editing a picture in place is picked up, and on the treatment, so the
    live render — which re-renders the slide under the cursor on every pause
    in typing — re-encodes a photograph once rather than once a keystroke.
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    except ImportError:
        return None

    try:
        img = Image.open(path)
        has_alpha = img.mode in ("RGBA", "LA", "PA")
        img = img.convert("RGBA" if has_alpha else "RGB")

        # Alpha does not survive the grade operations below, so it is set
        # aside and put back afterwards rather than being flattened onto a
        # background this function cannot know the colour of.
        alpha = img.getchannel("A") if has_alpha else None
        rgb   = img.convert("RGB")

        if filt == "greyscale":
            rgb = ImageOps.grayscale(rgb).convert("RGB")
        elif filt == "bw":
            rgb = (ImageOps.grayscale(rgb)
                   .point(lambda px: 255 if px > 127 else 0)
                   .convert("RGB"))
        elif filt == "sepia":
            rgb = ImageOps.colorize(ImageOps.grayscale(rgb),
                                    _SEPIA_DARK, _SEPIA_LIGHT).convert("RGB")
        elif filt == "lighten":
            rgb = ImageEnhance.Brightness(rgb).enhance(_BRIGHTEN)
        elif filt == "darken":
            rgb = ImageEnhance.Brightness(rgb).enhance(_DARKEN)
        elif filt == "blur" and blur > 0:
            rgb = rgb.filter(ImageFilter.GaussianBlur(radius=blur))

        if tint:
            try:
                wash = Image.new("RGB", rgb.size, tint)
            except (ValueError, OSError):
                wash = None          # a colour Pillow does not know
            if wash is not None:
                rgb = Image.blend(rgb, wash, _TINT_BLEND)

        if alpha is not None:
            rgb = rgb.convert("RGBA")
            rgb.putalpha(alpha)

        buf = _BytesIO()
        fmt = "PNG" if alpha is not None else "JPEG"
        save_kw = {} if fmt == "PNG" else {"quality": 85, "optimize": True}
        rgb.save(buf, format=fmt, **save_kw)
        b64 = _base64.b64encode(buf.getvalue()).decode()
        mime = "image/png" if fmt == "PNG" else "image/jpeg"
        return f"data:{mime};base64,{b64}"

    except Exception:
        return None


from .splitter import (is_title_slide, extract_speaker_notes,
                          infer_slide_title, split_two_columns)
from .image_attrs import extract_images_with_attrs
from .renderer    import render_slide_content
from .layout      import (AUTO_IMAGE_LAYOUT, PAIR_SIZE, choose_layout,
                          cell_fit, shape_of)
from .utils       import image_aspect, image_is_missing
from .frontmatter import extract_slide_directives, strip_slide_directives
from .            import reveal as _reveal
from .utils       import logo_img_tag, progress_bar_html


def _stamp_slide_attrs(html_frag: str, attrs: str) -> str:
    """Add *attrs* to the fragment's outermost slide div."""
    marker = '<div class="slide'
    at = html_frag.find(marker)
    if at == -1:
        return html_frag
    close = html_frag.find(">", at)
    if close == -1:
        return html_frag
    return html_frag[:close] + attrs + html_frag[close:]


def _stamp_slide_index(html_frag: str, index: int) -> str:
    """
    Add data-slide-index to the fragment's outermost slide div.

    Done here rather than in each of the five per-layout renderers, so a new
    layout cannot be added without it.
    """
    return _stamp_slide_attrs(html_frag, f' data-slide-index="{index}"')


def _stamp_layout(html_frag: str, plan) -> str:
    """
    Publish what layout.py measured, on the slide div.

    These are the facts the arrangement was chosen from — how many pictures,
    what shape each one is, how much text shares the slide — rather than the
    arrangement itself. A stylesheet cannot count words or read an image
    header, so it cannot arrive at its own arrangement unless the engine says
    what it found. Stamped at the dispatch site for the same reason the slide
    index is: a sixth layout cannot be added without them.
    """
    attrs = (f' data-layout="{_html.escape(plan.kind)}"'
             f' data-text="{_html.escape(plan.text)}"'
             f' data-images="{plan.images}"')
    if plan.shapes:
        attrs += f' data-shapes="{_html.escape(" ".join(plan.shapes))}"'
    return _stamp_slide_attrs(html_frag, attrs)


def _stamp_step(html_frag: str, step: int, steps: int) -> str:
    """
    Say which of a slide's reveal steps this copy is.

    Only ever written on a slide that has more than one, so a deck that
    reveals nothing carries not one attribute it did not carry before.  A
    theme can read it — ``[data-step] .not-yet`` is how a house style dims
    what has not arrived instead of hiding it.
    """
    return _stamp_slide_attrs(
        html_frag, f' data-step="{step}" data-steps="{steps}"')


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
    reveal: bool = False,
) -> tuple[str, list[dict]]:
    """
    Render all slides to a complete HTML string.

    Returns (html, slide_info) where slide_info is a list of
    {'title': str, 'notes': str} dicts used by the thumbnail index.

    When *only_index* is given, the returned document contains just that one
    slide's markup — slide numbering and title-slide detection still consider
    the whole deck, so the fragment is identical to the one the full document
    would contain.  Used by the live render, which only ever draws one slide
    and should not pay for markup it will not display.  *slide_info* always
    covers every slide.

    *line_offsets* gives each slide's starting line in the source document.
    When present, blocks carry ``data-src-line``, so a layout measurement of
    the rendered page can name the line that overflows.

    With *reveal* set, a slide holding step markers is written out once per
    step — the same fragment each time, with the blocks that have not
    arrived yet marked — so the paged engine can give the room one page per
    step.  Off by default, and off for anything unpaged: the live render and
    the exported HTML show each slide complete, and a deck that reveals
    nothing produces byte-identical markup either way.  Steps need
    *line_offsets*, which is what the blocks are marked by.
    """
    slide_htmls = []
    slide_info  = []
    # How many lone pictures have been laid out so far, which is what decides
    # the side the next one takes.
    lone_images = 0

    first_is_title = bool(slides) and is_title_slide(slides[0], 0)
    total_numbered = len(slides) - (1 if first_is_title else 0)

    bg = _safe_bg(theme_bg)

    for i, slide_md in enumerate(slides):
        slide_body, notes = extract_speaker_notes(slide_md)
        cleaned_md, images = extract_images_with_attrs(slide_body)
        title_slide = is_title_slide(slide_body, i)

        # Extract per-slide directives (e.g. <!-- theme: dark -->)
        directives = extract_slide_directives(slide_body)
        theme_override = directives.get("theme", "")

        # Read before anything else looks at the text: a step marker left in
        # would be a paragraph reading "+++" on the slide and a word in the
        # count that picks the layout.  The lines come back numbered within
        # the very string the renderer is about to be handed.
        cleaned_md, step_lines = _reveal.plan(
            strip_slide_directives(cleaned_md), directives, meta)

        slide_info.append({
            "title": infer_slide_title(slide_body, fallback=f"Slide {i + 1}"),
            "notes": notes,
            "body":  cleaned_md,
            # Recorded for every slide, before the only_index skip below, so
            # a single-slide render still reports the whole deck's broken
            # picture paths rather than only the one under the cursor.
            "missing_images": [src for src in
                               (img.get("src", "") for img in images)
                               if image_is_missing(src, base_url)],
        })

        page_num = i if first_is_title else i + 1
        line_offset = (line_offsets[i]
                       if line_offsets is not None and i < len(line_offsets)
                       else None)

        # Counted before the skip below, and over every slide rather than
        # the rendered one: which side a lone picture takes depends on how
        # many came before it, so a single-slide render has to arrive at the
        # same answer the whole deck does or the strip and the PDF would put
        # the same picture on opposite sides.  The condition is exactly
        # choose_layout()'s test for a "single", and needs no image shapes.
        side_ordinal = lone_images
        if len(images) == 1 and cleaned_md.strip():
            lone_images += 1

        # Markdown rendering is the only costly step here; skip it entirely
        # for slides the caller will not display.
        if only_index is not None and i != only_index:
            continue

        # Ask what the slide should be rather than branching on how many
        # images it happens to have; see layout.py for the rules. Shapes are
        # read from the file headers (cached on mtime), which is what lets
        # the pair layout be chosen rather than written.
        #
        # Asked for every slide, not only the ones with pictures, because the
        # plan is also what gets published on the slide div — a slide of pure
        # text still says so.
        aspects = [image_aspect(img.get("src", ""), base_url)
                   for img in images] if images else []
        plan = choose_layout(cleaned_md, images, aspects, side_ordinal)

        if title_slide:
            # The cover is dispatched on its own before any of this, so name
            # it as the arrangement it is rather than the one its content
            # would otherwise have earned.
            plan = _replace(plan, kind="title")
            html_frag = _render_title_slide(cleaned_md, meta, logo_b64,
                                            line_offset=line_offset)
        elif images:
            if plan.kind == "gallery":
                html_frag = _render_gallery_slide(
                    cleaned_md, images, plan,
                    page_num, total_numbered, logo_b64, theme_override,
                    width=width, height=height,
                    base_url=base_url, line_offset=line_offset,
                )
            elif plan.kind == "pair":
                # The flanking positions are the plan's, not the document's —
                # but each picture's own treatment is still its own.
                layout_a = {**images[0]["layout"], "position": "left",
                            "size": PAIR_SIZE}
                layout_b = {**images[1]["layout"], "position": "right",
                            "size": PAIR_SIZE}
                html_frag = _render_two_image_slide(
                    cleaned_md, images[0]["src"], layout_a,
                    images[1]["src"], layout_b,
                    page_num, total_numbered, logo_b64, theme_override,
                    height=height, theme_bg=bg, base_url=base_url,
                    line_offset=line_offset,
                )
            else:
                # What the picture looks like was resolved once, in
                # image_attrs: AUTO_IMAGE_LAYOUT under whatever the writer
                # pinned. Where it goes is still the plan's answer, which has
                # already taken any pinned placement into account.
                layout = dict(images[0]["layout"])
                if plan.size is not None:
                    layout["size"] = plan.size
                if plan.position is not None:
                    layout["position"] = plan.position
                if plan.kind == "bleed":
                    # An image with nothing to sit beside fills the slide.
                    layout.update(position="background", size="100")
                # `full` means the slide *is* the picture. The words are not
                # rendered rather than being hidden behind it, so nothing is
                # left to select, to read out of the PDF, or to overflow.
                body = "" if plan.text_dropped else cleaned_md
                html_frag = _render_image_slide(
                    body, images[0]["src"], layout,
                    page_num, total_numbered, logo_b64, theme_override,
                    height=height, base_url=base_url,
                    line_offset=line_offset,
                )
        else:
            html_frag = _render_normal_slide(cleaned_md, page_num,
                                             total_numbered, logo_b64,
                                             theme_override,
                                             line_offset=line_offset)

        # Stamp the slide's own index on its outermost div.  A slide whose
        # content overflows its box makes WeasyPrint emit a continuation
        # page, so a PDF page number is not a slide number; this is what
        # lets the build say which page each slide actually starts on.
        stamped = _stamp_slide_index(_stamp_layout(html_frag, plan), i)

        # One copy per step, each hiding what has not been reached yet.  The
        # boundaries are moved into the document's own line numbering, which
        # is what the blocks were stamped with.
        steps = ([line_offset + line for line in step_lines]
                 if reveal and line_offset is not None else [])
        if steps:
            slide_info[i]["steps"] = len(steps) + 1
            for step, frag in enumerate(_reveal.expand(stamped, steps)):
                slide_htmls.append(_stamp_step(frag, step, len(steps) + 1))
        else:
            slide_htmls.append(stamped)

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

def _style_attr(declarations: str) -> str:
    """Return a style attribute for *declarations*, or nothing when empty."""
    return f' style="{declarations}"' if declarations else ""

def _image_geometry(pos: str, size: int, height: int) -> tuple[str, str]:
    """
    Return (img_style, text_style): what the panel *measured*, as custom
    properties, for the stylesheet to arrange with.

    Which edge the picture takes is already on the slide div as data-img-pos,
    and every rule that positions the panel is in css.py keyed on it. Only the
    width layout.py chose has to be passed through, because it is a
    measurement — auto_size() reads it off the word count — and no stylesheet
    can arrive at it alone. Writing `width` here instead of `--p-img-size`
    would put the whole arrangement beyond a theme's reach: an inline style
    outranks every selector.

    The padding is given separately from the width because a percentage means
    different things on the two axes — CSS resolves a vertical padding against
    the container's *width* — so a top or bottom panel needs its own value in
    pixels to keep the text clear of the picture.
    """
    if pos == "background" or size >= 100:
        return "", ""
    if pos in ("left", "right"):
        return f"--p-img-size:{size}%;", f"--p-img-pad:{size}%;"
    if pos in ("top", "bottom"):
        return (f"--p-img-size:{size}%;",
                f"--p-img-pad:{int(height * size / 100)}px;")
    return "", ""


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

    Placement and size arrive as the ``--p-img-*`` measurements css.py reads;
    ``data-img-fit`` and ``data-img-focal`` drive object-fit and
    object-position there. Nothing an arrangement depends on is written
    inline — see _image_geometry() for why that matters.

    Colour treatment is not here at all: it is baked into the picture's own
    pixels by _apply_img_effects(), because WeasyPrint has no CSS filter and
    no blend modes to do it with.
    """
    content   = render_slide_content(slide_md, line_offset)
    t_attr    = _theme_attr(theme_override)

    pos       = layout.get("position", "right")
    size      = int(layout.get("size", "50"))
    opacity   = layout.get("opacity", 100)
    fit       = layout.get("fit", "cover")
    focal     = layout.get("focal", "focal-center")

    # Reject javascript: / vbscript: URIs unconditionally before any processing.
    if _urlparse(img_src).scheme.lower() in _UNSAFE_IMG_SCHEMES:
        img_src = ""

    effective_src = _apply_img_effects(img_src, base_url, layout)
    escaped_src   = _html.escape(_urlquote(effective_src, safe="+/=:;,"))

    flip_h    = layout.get("flip_h", False)
    flip_v    = layout.get("flip_v", False)
    zoom      = layout.get("zoom", 100)

    fit_attr   = f' data-img-fit="{_html.escape(fit)}"' if fit != "cover" else ""
    focal_attr = (f' data-img-focal="{_html.escape(focal)}"'
                  if focal != "focal-center" and fit == "cover" else "")

    # A panel at full width is a full-bleed picture however it was asked for,
    # and the markup now says which arrangement it is rather than leaving the
    # attribute pointing at a side the picture does not take.
    if size >= 100:
        pos = "background"

    img_style, text_pad = _image_geometry(pos, size, height)

    # Opacity is published as a measurement, not applied as a declaration.
    # Written inline it would outrank every selector, which is the one thing
    # a theme must be able to disagree with — and it is on the reserved list
    # test_layout_attrs.py enforces.
    if opacity < 100:
        img_style += f"--p-img-opacity:{opacity / 100:.2f};"

    transforms = []
    if zoom != 100:
        transforms.append(f"scale({zoom / 100:.2f})")
    if flip_h:
        transforms.append("scaleX(-1)")
    if flip_v:
        transforms.append("scaleY(-1)")
    img_inline = ""
    if transforms:
        focal_origin = {
            "focal-top":    "center top",
            "focal-bottom": "center bottom",
        }.get(focal, "center center")
        img_inline = (f"transform:{' '.join(transforms)};"
                      f"transform-origin:{focal_origin}")

    img_style_attr = f' style="{img_inline}"' if img_inline else ""
    img_el = f'<img src="{escaped_src}"{img_style_attr} alt="">'

    text_style = _style_attr(text_pad)

    return (
        f'<div class="slide has-image"'
        f' data-img-pos="{_html.escape(pos)}"{fit_attr}{focal_attr}{t_attr}>'
        f'  <div class="slide-image"{_style_attr(img_style)}>'
        f'    {img_el}'
        f'  </div>'
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
        cell_layout = image.get("layout") or AUTO_IMAGE_LAYOUT
        effective = _apply_img_effects(raw_src, base_url, cell_layout)
        src = _html.escape(_urlquote(effective, safe="+/=:;,"))
        spans_two = (index + 1) in plan.spans
        span = ' data-span="2"' if spans_two else ""
        this_w = cell_w * 2 + gap if spans_two else cell_w
        aspect = image_aspect(raw_src, base_url)
        # The grid measures a fit for every cell, and a picture that pinned
        # one says so instead: the writer is answering the same question the
        # measurement answers, about the one cell they were looking at.
        pinned_fit = (image.get("attrs") or {}).get("fit")
        fit = pinned_fit or cell_fit(aspect, this_w / row_h if row_h else 0)
        focal = cell_layout.get("focal", "focal-center")
        focal_attr = (f' data-img-focal="{_html.escape(focal)}"'
                      if focal != "focal-center" and fit == "cover" else "")
        # Opacity is a measurement here too, so a cell the writer faded is
        # faded by the stylesheet rather than by an inline declaration no
        # theme could reach.
        opacity = cell_layout.get("opacity", 100)
        cell_style = (f' style="--p-img-opacity:{opacity / 100:.2f};"'
                      if opacity < 100 else "")
        # Both are the cell's own facts: what shape the picture is, and
        # whether that shape fits its cell closely enough to be cropped. The
        # slide's data-shapes cannot address one cell, so each carries its
        # own; object-fit itself is set from data-fit in css.py.
        cells.append(
            f'<div class="gallery-cell"{span}'
            f' data-shape="{shape_of(aspect)}" data-fit="{fit}"'
            f'{focal_attr}{cell_style}>'
            f'<img src="{src}" alt="">'
            f'</div>'
        )

    t_attr = _theme_attr(theme_override)
    # The grid's shape is measured — the row height especially, which must
    # end up definite — so it is published for the stylesheet to build the
    # tracks from rather than written onto the element as the tracks
    # themselves.
    style = (f"--p-gallery-columns: repeat({plan.columns}, 1fr);"
             f"--p-gallery-row: {row_h}px;")
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
    src_a:          str,
    layout_a:       dict,
    src_b:          str,
    layout_b:       dict,
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
    Render a slide with two images flanking the text between them.

    *layout_a* and *layout_b* come from the caller, which built them from
    each picture's own resolved appearance and then overruled the positions —
    the flanking arrangement is the plan's answer, the treatments are each
    picture's own. Their positions give the split:
      left + right  → horizontal: [img] [text] [img]
      top  + bottom → vertical:   [img] / [text] / [img]

    The text sits in the centre, between the two pictures.
    """
    content = render_slide_content(slide_md, line_offset)
    t_attr  = _theme_attr(theme_override)

    pos_a     = layout_a["position"]
    pos_b     = layout_b["position"]
    size_a    = int(layout_a["size"])
    size_b    = int(layout_b["size"])
    opacity_a = layout_a["opacity"]
    opacity_b = layout_b["opacity"]
    fit_a     = layout_a.get("fit", "cover")
    fit_b     = layout_b.get("fit", "cover")
    focal_a   = layout_a.get("focal", "focal-center")
    focal_b   = layout_b.get("focal", "focal-center")
    raw_src_a   = src_a
    raw_src_b   = src_b
    # Reject javascript: / vbscript: URIs unconditionally.
    if _urlparse(raw_src_a).scheme.lower() in _UNSAFE_IMG_SCHEMES:
        raw_src_a = ""
    if _urlparse(raw_src_b).scheme.lower() in _UNSAFE_IMG_SCHEMES:
        raw_src_b = ""
    src_a = _html.escape(_urlquote(
        _apply_img_effects(raw_src_a, base_url, layout_a), safe="+/=:;,"))
    src_b = _html.escape(_urlquote(
        _apply_img_effects(raw_src_b, base_url, layout_b), safe="+/=:;,"))

    horizontal = {pos_a, pos_b} == {"left", "right"}
    vertical   = {pos_a, pos_b} == {"top",  "bottom"}

    if not horizontal and not vertical:
        # Unreachable while choose_layout only ever asks for left+right, but
        # a wrong pair must still show a slide rather than raise.
        return _render_image_slide(
            slide_md, raw_src_a, layout_a,
            page_num, total, logo_b64, theme_override,
            height=height, base_url=base_url,
        )

    axis = "h" if horizontal else "v"

    # Only the widths are measured here; where the panels sit is in css.py,
    # keyed on data-split. See _image_geometry() for why the padding is
    # carried separately from the size.
    img_a_style = f"--p-img-size:{size_a}%;"
    img_b_style = f"--p-img-size:{size_b}%;"
    if opacity_a < 100:
        img_a_style += f"--p-img-opacity:{opacity_a / 100:.2f};"
    if opacity_b < 100:
        img_b_style += f"--p-img-opacity:{opacity_b / 100:.2f};"
    if horizontal:
        text_style = f"--p-img-pad-a:{size_a}%;--p-img-pad-b:{size_b}%;"
    else:
        text_style = (f"--p-img-pad-a:{int(height * size_a / 100)}px;"
                      f"--p-img-pad-b:{int(height * size_b / 100)}px;")

    # Fit and alignment reach the panel as attributes, exactly as they do on
    # a lone picture, so a theme keys off the same names in both layouts.
    def _panel_attrs(fit: str, focal: str) -> str:
        attrs = f' data-img-fit="{_html.escape(fit)}"' if fit != "cover" else ""
        if focal != "focal-center" and fit == "cover":
            attrs += f' data-img-focal="{_html.escape(focal)}"'
        return attrs

    return (
        f'<div class="slide has-two-images" data-split="{axis}"{t_attr}>'
        f'  <div class="slide-image-a"{_panel_attrs(fit_a, focal_a)}'
        f' style="{img_a_style}">'
        f'    <img src="{src_a}" alt="">'
        f'  </div>'
        f'  <div class="slide-image-b"{_panel_attrs(fit_b, focal_b)}'
        f' style="{img_b_style}">'
        f'    <img src="{src_b}" alt="">'
        f'  </div>'
        f'  <div class="slide-text" style="{text_style}">'
        f'    {content}'
        f'    <div class="slide-number">{page_num} / {total}</div>'
        f'  </div>'
        f'  {progress_bar_html(page_num, total)}'
        f'  {logo_img_tag(logo_b64)}'
        f'</div>'
    )
