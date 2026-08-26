"""
test_flatpak_permissions.py — The sandbox can reach the files people drop.

Dragging an image out of Nautilus hands the app a real host path. Under
Flatpak the app can only open it if the manifest says so, and when it cannot
the drop handler's shutil.copy2() raises OSError — which used to be caught
and turned into an absolute path the renderer could not read either. The
result was an image tag that silently rendered nothing.

The manifest is the fix, so the manifest is what is pinned here.
"""

import json
import pathlib

import pytest

MANIFEST = (pathlib.Path(__file__).resolve().parent.parent
            / "io.gitlab.gtk4_apps1.Presence.json")


@pytest.fixture(scope="module")
def finish_args() -> list:
    if not MANIFEST.is_file():
        pytest.skip("Flatpak manifest not present in this checkout")
    return json.loads(MANIFEST.read_text())["finish-args"]


def _filesystems(finish_args) -> dict:
    """Map of granted path → access mode ("rw" unless suffixed)."""
    out = {}
    for arg in finish_args:
        if not arg.startswith("--filesystem="):
            continue
        value = arg.split("=", 1)[1]
        path, _, mode = value.partition(":")
        out[path] = mode or "rw"
    return out


@pytest.mark.parametrize("location", ["xdg-pictures", "xdg-download",
                                      "xdg-desktop"])
def test_images_can_be_dropped_from_the_usual_places(finish_args, location):
    """Where people actually keep the picture they are dragging in."""
    assert location in _filesystems(finish_args), (
        f"{location} is not reachable, so an image dropped from there "
        f"inserts a tag that renders nothing."
    )


def test_documents_stays_writable(finish_args):
    """A dropped image is copied into assets/ beside the .md, so the
    document's own directory has to be writable."""
    assert _filesystems(finish_args).get("xdg-documents") == "rw"


def test_the_read_only_grants_really_are_read_only(finish_args):
    """Reading is all that is needed to copy a picture out of them."""
    granted = _filesystems(finish_args)
    for location in ("xdg-pictures", "xdg-download", "xdg-desktop"):
        assert granted.get(location) == "ro"


def test_the_sandbox_does_not_reach_all_of_home(finish_args):
    """Widening to home is a decision to take deliberately, not to drift
    into while fixing a drag-and-drop bug."""
    assert "home" not in _filesystems(finish_args)


def test_no_api_key_secrets_portal_remains(finish_args):
    """The keyring was only ever used to store AI API keys."""
    assert "--talk-name=org.freedesktop.secrets" not in finish_args
