"""Which version of Presence is running.

The number is written in one place, ``pyproject.toml``.  Everything that
shows it — the About dialog, ``presence-cli --version`` — asks here.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version as _installed
from pathlib import Path

# src/presence/slides/version.py → the checkout root, when run from source.
_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


def app_version() -> str:
    """Return the running version, e.g. ``"1.1.1"``.

    A source checkout reads its own ``pyproject.toml`` first, so it never
    reports the version of some other copy that happens to be installed.
    An installed copy (the Flatpak, a pip install) reads its package
    metadata.  ``"unknown"`` if neither is there.
    """
    try:
        data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
        if data.get("project", {}).get("name") == "presence":
            return str(data["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return _installed("presence")
    except PackageNotFoundError:
        return "unknown"
