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


def test_nothing_outside_the_usual_places_is_granted(finish_args):
    """
    The four user folders, and no more. Widening was considered and declined:
    a deck in ~/Projects still cannot have assets written beside it, which is
    the accepted cost of leaving dotfiles and everything else out of reach.
    """
    granted = set(_filesystems(finish_args))
    allowed = {"xdg-documents", "xdg-pictures", "xdg-download", "xdg-desktop",
               "xdg-data/presence", "xdg-data/fonts", "~/.local/share/fonts"}
    assert granted <= allowed, f"unexpected grants: {granted - allowed}"


@pytest.mark.parametrize("location", ["xdg-documents", "xdg-pictures",
                                     "xdg-download", "xdg-desktop"])
def test_a_deck_can_be_kept_in_any_of_the_usual_places(finish_args, location):
    """
    Read access is not enough, and assuming it was is what broke adding a
    picture at all.

    Presence copies the picture into assets/ *beside the document*, so it
    needs to write the document's own folder — and when it cannot, the file
    chooser portal does not even hand over the real path. It hands back a
    /run/flatpak/doc/ entry holding that one file, where mkdir("assets") is
    EPERM and always will be. A deck in Downloads therefore could not take a
    picture from anywhere, including from Downloads.
    """
    assert _filesystems(finish_args).get(location) == "rw", (
        f"{location} is read-only, so a deck kept there cannot have an "
        f"assets/ folder written beside it."
    )


def test_the_sandbox_does_not_reach_all_of_home(finish_args):
    """Widening to home is a decision to take deliberately, not to drift
    into while fixing a drag-and-drop bug."""
    assert "home" not in _filesystems(finish_args)


def test_no_api_key_secrets_portal_remains(finish_args):
    """The keyring was only ever used to store AI API keys."""
    assert "--talk-name=org.freedesktop.secrets" not in finish_args
