"""
test_window_wiring.py — The window's calls into its child widgets resolve.

MainWindow drives the Editor, Inspector and Sidebar by name, and
GTK swallows an AttributeError raised inside a signal handler: the button
simply does nothing, with no traceback and no toast. Renaming
Editor.open_image_layout_popover() to choose_image_to_insert() left
window.py calling the old name, and every test passed while the toolbar's
image button was dead.

Nothing here builds a widget — GTK 4 has no offscreen backend, so
constructing one segfaults (see conftest.py). The check is the source of the
calls against the classes' own members, which is enough to catch a rename.
"""

import ast
import inspect
import pathlib

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from presence.editor import Editor          # noqa: E402
from presence.inspector import Inspector    # noqa: E402
from presence.sidebar import Sidebar        # noqa: E402
from presence.build_coordinator import BuildCoordinator      # noqa: E402
from presence.document_controller import DocumentController  # noqa: E402
from presence.export_controller import ExportController      # noqa: E402

# The attribute MainWindow holds each collaborator in, and the class behind it.
#
# The three controllers are here for a second reason.  They used to keep
# their state on the window and reach in for it, so "does this name exist"
# was a question about the window, not about them.  Now the window asks them
# (self.documents.file_path, self.builds.html_uri) and every one of those
# names has to resolve on the controller — which is what this file already
# knew how to check.
COLLABORATORS = {
    "editor":     Editor,
    "_inspector": Inspector,
    "sidebar":    Sidebar,
    "documents":  DocumentController,
    "builds":     BuildCoordinator,
    "exports":    ExportController,
}


def _self_assigned_names(cls) -> set:
    """Attribute names the class assigns to self anywhere in its body.

    Instance attributes never reach the class object, so hasattr() alone
    would report every one of them as missing.
    """
    try:
        tree = ast.parse(inspect.getsource(cls))
    except (OSError, TypeError):            # pragma: no cover - source always
        return set()                        # available for these classes
    names = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and isinstance(node.ctx, ast.Store)):
            names.add(node.attr)
    return names


def _calls_in_window() -> list:
    """Every ``self._<collaborator>.<name>`` the window module mentions."""
    path = pathlib.Path(inspect.getfile(Editor)).with_name("window.py")
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        owner = node.value
        if (isinstance(owner, ast.Attribute)
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "self"
                and owner.attr in COLLABORATORS):
            found.append((owner.attr, node.attr, node.lineno))
    return found


def test_the_window_reaches_at_least_one_collaborator():
    """Guards the parser itself: an empty result would pass every test below."""
    assert len(_calls_in_window()) > 10


@pytest.mark.parametrize("holder,attr,lineno", _calls_in_window(),
                         ids=lambda v: str(v))
def test_every_window_call_resolves(holder, attr, lineno):
    cls = COLLABORATORS[holder]
    ok = hasattr(cls, attr) or attr in _self_assigned_names(cls)
    assert ok, (
        f"window.py:{lineno} calls self.{holder}.{attr}(), but "
        f"{cls.__name__} has no such member. GTK would swallow this as a "
        f"control that silently does nothing."
    )


def test_the_image_button_reaches_a_real_method():
    """The rename that this file exists because of."""
    assert hasattr(Editor, "choose_image_to_insert")
    assert not hasattr(Editor, "open_image_layout_popover")
