"""
test_sandbox_escape.py — The Flatpak cannot run programs outside itself.

Talking to org.freedesktop.Flatpak lets an app start any command on the
host, which undoes the sandbox.  Presence only ever needed it to open a PDF,
and a file launcher does that through a portal.  These tests keep the
permission, and the code that would need it, from coming back.
"""

import json
import pathlib

import pytest

ROOT     = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "io.gitlab.gtk4_apps1.Presence.json"
SOURCE   = ROOT / "src" / "presence"


def test_the_manifest_does_not_grant_host_commands():
    if not MANIFEST.is_file():
        pytest.skip("Flatpak manifest not present in this checkout")
    args = json.loads(MANIFEST.read_text())["finish-args"]
    assert "--talk-name=org.freedesktop.Flatpak" not in args
    assert not any(a.startswith("--talk-name=org.freedesktop.Flatpak")
                   for a in args)


@pytest.mark.parametrize("marker", ["flatpak-spawn", "import subprocess",
                                    "from subprocess", "os.system("])
def test_no_code_starts_a_program_on_the_host(marker):
    offenders = [str(p.relative_to(ROOT))
                 for p in SOURCE.rglob("*.py")
                 if marker in p.read_text(encoding="utf-8")]
    assert offenders == []
