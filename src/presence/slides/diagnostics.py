"""
diagnostics.py — What a finished build has to tell the writer.

Three things can go wrong in a way the rendered slide will not show: text
that runs past the bottom of the box, a picture whose path does not resolve,
and a theme named in the frontmatter that is not installed.  Each of them
produces a slide that looks deliberate — a short slide, a low heading, the
wrong palette — so each has to be said out loud.

The sentences are built here rather than in either frontend so the CLI's
stderr and the window's banner say the same thing about the same deck.
"""
from __future__ import annotations


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


def build_warnings(slide_info: list[dict],
                   theme_warning: str | None = None) -> list[str]:
    """
    Everything worth saying about a build that otherwise succeeded.

    Both engines fill *slide_info* the same way, so the CLI's stderr and the
    window's banner are built from one function over one set of facts.
    """
    return [w for w in (theme_warning,
                        overflow_warning(slide_info),
                        missing_image_warning(slide_info)) if w]
