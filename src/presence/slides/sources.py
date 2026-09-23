"""
sources.py — Which files and web addresses a deck is allowed to load.

A deck is a text file, and people send each other text files. When one is
opened, Presence builds it straight away. So a deck must not be able to
reach out and grab whatever it likes: a picture written as
``/home/you/private.png`` or ``https://tracker.example/pixel.png`` would be
loaded without anyone asking.

The rule is simple:

* A saved deck may load files from its own folder (and the folders inside
  it), and fonts from the installed theme folders.
* An unsaved draft has no folder yet, and every picture in it was put there
  by the person typing, so it may load local files from anywhere.
* Nothing loads from the web unless the deck says ``remote_images: true``
  in its frontmatter.
* ``data:`` addresses are always fine: the picture is written inside the
  address itself, so nothing is fetched.

:class:`SourcePolicy` answers the question for one deck.  Everything that
reads a picture checks with it first, and every WeasyPrint render gets
:meth:`SourcePolicy.fetcher` as a last line of defence, so a stylesheet or
an SVG cannot sneak a fetch past the checks either.
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

log = logging.getLogger(__name__)

__all__ = ["SourcePolicy", "wants_remote_images",
           "INLINE", "LOCAL", "REMOTE", "OUTSIDE", "REMOTE_OFF", "UNSAFE"]

# What check() can say about a picture's address.
INLINE     = "inline"       # data: — the picture is inside the address
LOCAL      = "local"        # a file we may read
REMOTE     = "remote"       # a web address the deck opted in to
OUTSIDE    = "outside"      # a file outside the places we may read
REMOTE_OFF = "remote-off"   # a web address, and the deck did not opt in
UNSAFE     = "unsafe"       # javascript:, vbscript: and anything else odd

_WEB = ("http", "https")


def wants_remote_images(meta: dict | None) -> bool:
    """True when the frontmatter says ``remote_images: true``."""
    value = (meta or {}).get("remote_images")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "on", "1")


class SourcePolicy:
    """
    The places one deck may load pictures, fonts and stylesheets from.

    *folder* is the deck's own folder, or None for an unsaved draft.
    *extra_roots* are more folders that are always fine — the theme folders,
    which is where a theme's own fonts live.  *allow_remote* lets web
    addresses through.

    A draft (no folder) may read local files from anywhere, because the
    person typing put every picture in it.  Pass ``local_anywhere=False`` to
    say no to that too, as :meth:`data_only` does.
    """

    def __init__(self, folder: "Path | str | None",
                 extra_roots=(), allow_remote: bool = False,
                 local_anywhere: bool | None = None) -> None:
        self.folder = Path(folder).resolve() if folder else None
        self.roots = [Path(r).resolve() for r in extra_roots if r]
        self.allow_remote = allow_remote
        self.local_anywhere = (self.folder is None if local_anywhere is None
                               else local_anywhere)

    @classmethod
    def data_only(cls) -> "SourcePolicy":
        """A policy that allows nothing but ``data:`` addresses."""
        return cls(None, local_anywhere=False)

    # ── One address at a time ────────────────────────────────────────────

    def check(self, src: str) -> "tuple[str, Path | None]":
        """
        Say whether *src* may be loaded, and where the file is if it is local.

        Relative paths are read against the deck's folder.  Returns one of
        the constants at the top of this module, plus the file's path when
        the answer is LOCAL or OUTSIDE.
        """
        if not src:
            return UNSAFE, None
        if src.startswith("data:"):
            return INLINE, None

        parsed = urlparse(src)
        scheme = parsed.scheme.lower()
        if scheme in _WEB:
            return (REMOTE if self.allow_remote else REMOTE_OFF), None
        if scheme == "file":
            path = Path(url2pathname(unquote(parsed.path)))
        elif scheme and len(scheme) > 1:
            # A one-letter "scheme" is a Windows drive, not a scheme.
            return UNSAFE, None
        else:
            path = Path(src)

        if not path.is_absolute():
            if self.folder is None:
                return (LOCAL if self.local_anywhere else OUTSIDE), path
            path = self.folder / path

        try:
            path = path.resolve()
        except (OSError, RuntimeError):
            return OUTSIDE, path
        return (LOCAL if self._may_read(path) else OUTSIDE), path

    def allows_url(self, url: str) -> bool:
        """The same question for an absolute URL WeasyPrint wants to fetch."""
        verdict, _path = self.check(url)
        return verdict in (INLINE, LOCAL, REMOTE)

    def _may_read(self, path: Path) -> bool:
        if self.local_anywhere:
            return True
        for root in ([self.folder] if self.folder else []) + self.roots:
            if path == root or path.is_relative_to(root):
                return True
        return False

    # ── For WeasyPrint ───────────────────────────────────────────────────

    def fetcher(self):
        """A WeasyPrint URL fetcher that refuses anything this policy refuses."""
        from weasyprint.urls import URLFetcher

        policy = self

        class _ConfinedFetcher(URLFetcher):
            def __init__(self) -> None:
                super().__init__()
                self.refused: list[str] = []

            def fetch(self, url, headers=None):
                if not policy.allows_url(url):
                    self.refused.append(url)
                    log.warning("Not loading %s — it is outside what this "
                                "presentation may read", url)
                    raise ValueError(f"Refused by Presence: {url}")
                return super().fetch(url, headers)

        return _ConfinedFetcher()
