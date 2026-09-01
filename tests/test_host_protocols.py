"""
test_host_protocols.py — The seam between the window and its controllers.

`BuildCoordinator`, `DocumentController` and `ExportController` each declare a
Protocol saying exactly what they need from the window they belong to —
`BuildHost`, `DocumentHost`, `ExportHost`.  This file is what makes those
declarations true, in both directions:

*  Everything a Host promises, `MainWindow` provides.  A rename on the window
   that a controller still calls is a control that silently does nothing,
   because GTK swallows an AttributeError raised inside a signal handler.
*  Everything a controller reaches for, its Host declares.  This is the half
   that stops the seam growing back: the three controllers once reached for
   forty-seven private attributes of the window, one at a time, each addition
   perfectly reasonable on its own.

**Why a test and not a type checker.**  `MainWindow` inherits from
`Adw.ApplicationWindow`, and PyGObject ships no type stubs, so a checker sees
the base class as `Any` — which makes `MainWindow` satisfy *every* Protocol
vacuously.  Adding a bogus member to `BuildHost` and running mypy over
`window.py` produces no error at all.  So the Protocols are the statement of
the contract and this file is its enforcement.

Nothing here builds a widget: the check is the source of `window.py` against
the Protocols' own members, which is enough to catch a rename.
"""

import ast
import inspect
import pathlib

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from presence.build_coordinator import BuildCoordinator, BuildHost      # noqa: E402
from presence.document_controller import (DocumentController,           # noqa: E402
                                          DocumentHost)
from presence.export_controller import ExportController, ExportHost     # noqa: E402

# Each controller, and the Protocol it takes its window as.
SEAMS = {
    "BuildCoordinator":   (BuildCoordinator,   BuildHost),
    "DocumentController": (DocumentController, DocumentHost),
    "ExportController":   (ExportController,   ExportHost),
}


def _protocol_members(protocol) -> set:
    """Every name a Host promises: its annotated attributes and its methods."""
    names = set(getattr(protocol, "__annotations__", {}))
    names |= {name for name, value in vars(protocol).items()
              if callable(value) and not name.startswith("_")}
    return names


def _self_assigned_names(cls) -> set:
    """Attribute names the class assigns to self anywhere in its body.

    Instance attributes never reach the class object, so hasattr() alone
    would report every one of them as missing.
    """
    tree = ast.parse(inspect.getsource(cls))
    names = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and isinstance(node.ctx, ast.Store)):
            names.add(node.attr)
    return names


def _window_class():
    from presence.window import MainWindow
    return MainWindow


def _window_members() -> set:
    cls = _window_class()
    return set(dir(cls)) | _self_assigned_names(cls)


def _window_reaches(cls) -> list:
    """Every ``win.<name>`` / ``self._win.<name>`` the controller mentions."""
    tree = ast.parse(inspect.getsource(cls))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        owner = node.value
        is_local = isinstance(owner, ast.Name) and owner.id == "win"
        is_field = (isinstance(owner, ast.Attribute)
                    and isinstance(owner.value, ast.Name)
                    and owner.value.id == "self"
                    and owner.attr == "_win")
        if is_local or is_field:
            found.append((node.attr, node.lineno))
    return found


# ── The window keeps every promise a Host makes ───────────────────────────────

def test_every_seam_declares_something():
    """Guards the parsers: empty sets would make every test below pass."""
    for name, (_cls, protocol) in SEAMS.items():
        assert len(_protocol_members(protocol)) >= 5, name
    assert len(_window_members()) > 50


@pytest.mark.parametrize(
    "seam,member",
    [(seam, member)
     for seam, (_cls, protocol) in SEAMS.items()
     for member in sorted(_protocol_members(protocol))],
)
def test_the_window_provides_what_a_host_promises(seam, member):
    assert member in _window_members(), (
        f"{seam}'s Host protocol promises MainWindow.{member}, and the window "
        f"has no such member.  GTK would swallow this as a control that "
        f"silently does nothing."
    )


# ── A controller reaches for nothing its Host has not declared ────────────────

@pytest.mark.parametrize(
    "seam,attr,lineno",
    [(seam, attr, lineno)
     for seam, (cls, _protocol) in SEAMS.items()
     for attr, lineno in _window_reaches(cls)],
    ids=lambda v: str(v),
)
def test_every_reach_into_the_window_is_declared(seam, attr, lineno):
    cls, protocol = SEAMS[seam]
    module = pathlib.Path(inspect.getfile(cls)).name
    assert attr in _protocol_members(protocol), (
        f"{module}:{lineno} uses window.{attr}, which {protocol.__name__} does "
        f"not declare.  Either add it to the Protocol — deliberately, because "
        f"the seam is now one name wider — or find what already owns it."
    )


def test_no_controller_reaches_for_a_private_attribute():
    """The forty-seven that started this.  Zero, and it stays zero."""
    private = {
        f"{seam}.{attr} (line {lineno})"
        for seam, (cls, _protocol) in SEAMS.items()
        for attr, lineno in _window_reaches(cls)
        if attr.startswith("_")
    }
    assert not private, sorted(private)
