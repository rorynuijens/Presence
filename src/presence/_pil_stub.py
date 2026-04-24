"""
_pil_stub.py — Stub out C extensions that are absent in the Flatpak sandbox.

Import this module before importing weasyprint anywhere.

Why PIL is NOT stubbed here
---------------------------
WeasyPrint imports PIL defensively inside a try/except ImportError block:

    try:
        from PIL import Image as PILImage
    except ImportError:
        PILImage = None

When PIL is genuinely absent, WeasyPrint sets PILImage = None and falls
back to its built-in image decoder — which handles PNG, JPEG, GIF, SVG,
and WebP without Pillow.

If we install a MagicMock stub for PIL, WeasyPrint thinks PIL is present
(the import succeeds) and attempts to use it.  PIL methods such as
Image.open() then return MagicMock objects instead of real image data.
When WeasyPrint later computes intrinsic_ratio = image.width / image.height,
it gets MagicMock / MagicMock = MagicMock, and the subsequent comparison
    constraint_width > constraint_height * MagicMock
raises TypeError: '>' not supported between instances of 'float' and
'MagicMock'.  This is exactly the crash that was observed in production.

The correct fix is to let the ImportError propagate naturally so WeasyPrint
activates its built-in fallback — which works correctly in the Flatpak
sandbox.

Why brotli and zopfli ARE stubbed here
---------------------------------------
Some WeasyPrint versions import brotli at module level without a try/except,
so the import fails immediately when the package is absent, preventing
WeasyPrint from loading at all.  Zopfli has the same risk in certain
releases.  Both are optional compression backends; stubbing them as inert
no-op modules is safe because WeasyPrint checks their attributes before use
and degrades gracefully when the compression is unavailable.
"""

import sys
from importlib.abc import MetaPathFinder, Loader
from importlib.machinery import ModuleSpec
from unittest.mock import MagicMock


class _InertLoader(Loader):
    """Creates an inert MagicMock module for a named stub."""

    def create_module(self, spec: ModuleSpec):
        mod = MagicMock(spec_set=None)
        mod.__name__    = spec.name
        mod.__package__ = spec.name
        mod.__spec__    = spec
        mod.__path__    = []
        mod.__loader__  = self
        return mod

    def exec_module(self, module):
        pass  # MagicMock is ready as-is; nothing to execute.


class _CompressionFinder(MetaPathFinder):
    """
    Intercepts imports of brotli and zopfli — C extensions that are absent
    in the Flatpak sandbox — and returns inert MagicMock stubs so that
    WeasyPrint can load without raising ImportError at module level.

    PIL/Pillow is intentionally NOT intercepted here; see module docstring.
    """
    _STUBBED = frozenset(("brotli", "zopfli"))
    _loader  = _InertLoader()

    def find_spec(self, fullname: str, path, target=None):
        if fullname in self._STUBBED and fullname not in sys.modules:
            return ModuleSpec(fullname, self._loader)
        return None


sys.meta_path.insert(0, _CompressionFinder())
