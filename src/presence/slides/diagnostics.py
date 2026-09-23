"""
diagnostics.py — What a finished build has to tell the writer.

Some problems don't show on the slide itself. The slide just looks a bit
off, and nobody can tell why:

* the text runs past the bottom of the slide,
* a picture's file isn't there,
* a picture is somewhere the deck isn't allowed to read, or on the web,
* the theme named in the frontmatter isn't installed.

So the build says each one in words. The sentences are written here, once,
so the command line and the window say exactly the same thing.
"""
from __future__ import annotations

from pathlib import PurePath


def _join(names: list[str], limit: int = 4) -> str:
    """
    ``a, b, c and 2 more`` — enough to go and look, not a wall of text.

    The window joins these sentences into one AdwBanner line, which is a
    single line: a list of every offending slide would push the sentences
    after it out of sight, which is worse than naming four and a count.
    """
    if len(names) > limit:
        return ", ".join(names[:limit]) + f" and {len(names) - limit} more"
    if len(names) > 1:
        return ", ".join(names[:-1]) + f" and {names[-1]}"
    return names[0] if names else ""


def overflow_warning(slide_info: list[dict]) -> str | None:
    """
    One sentence naming the slides with more text than fits, if any.

    A slide counts as over when it was fragmented onto a second page — the
    content the build then drops — or when the fold found a block reaching
    into the padding the theme reserves for the slide number.  Both are the
    slide running out of room, and neither implies the other, so the sentence
    is built from the union rather than from whichever was measured first.
    """
    over = [i for i, info in enumerate(slide_info)
            if info.get("fold_line") is not None or info.get("clipped")]
    if not over:
        return None
    noun = "Slide" if len(over) == 1 else "Slides"
    verb = "has" if len(over) == 1 else "have"
    where = _join([str(i + 1) for i in over])
    return f"{noun} {where} {verb} more text than fits — the rest is clipped."


def missing_image_warning(slide_info: list[dict]) -> str | None:
    """One sentence naming the pictures that could not be found, if any."""
    missing = [f"{src} (slide {i + 1})"
               for i, info in enumerate(slide_info)
               for src in info.get("missing_images") or ()]
    if not missing:
        return None
    if len(missing) == 1:
        return f"Picture not found: {missing[0]}."
    return f"{len(missing)} pictures not found: {_join(missing)}."


def _where(slide_info: list[dict], key: str) -> list[str]:
    """Every address under *key*, each followed by the slide it is on."""
    return [f"{src} (slide {i + 1})"
            for i, info in enumerate(slide_info)
            for src in info.get(key) or ()]


def blocked_image_warning(slide_info: list[dict]) -> str | None:
    """
    One sentence naming the pictures the deck was not allowed to load.

    A deck may read files from its own folder, and the web only when it
    asks to (see sources.py).  A picture outside those places is left off
    the slide, so the writer has to be told why, and what to do about it.
    """
    # Just the file's name: the slide number says where to look, and a full
    # path would push the rest of the banner out of sight.
    outside = [f"{PurePath(src).name} (slide {i + 1})"
               for i, info in enumerate(slide_info)
               for src in info.get("outside_images") or ()]
    remote  = _where(slide_info, "remote_images")
    parts = []
    if outside:
        parts.append(
            f"Not loaded because it is outside the presentation's folder: "
            f"{_join(outside)}. Move it into the folder to use it."
        )
    if remote:
        parts.append(
            f"Web picture not loaded: {_join(remote)}. Add "
            f"\"remote_images: true\" to the frontmatter to allow it."
        )
    return " ".join(parts) or None


def build_warnings(slide_info: list[dict],
                   theme_warning: str | None = None) -> list[str]:
    """
    Everything worth saying about a build that otherwise succeeded.

    Both engines fill *slide_info* the same way, so the CLI's stderr and the
    window's banner are built from one function over one set of facts.
    """
    return [w for w in (theme_warning,
                        overflow_warning(slide_info),
                        missing_image_warning(slide_info),
                        blocked_image_warning(slide_info)) if w]
