"""The version is written once, and everything that shows it agrees.

The About dialog said 1.0.0 through two releases because the number was
typed into application.py and cli.py and nobody bumped them.
"""

import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

from presence.slides.version import app_version

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_running_version_is_pyproject():
    assert app_version() == _pyproject_version()


def test_meson_agrees():
    meson = (ROOT / "meson.build").read_text()
    assert re.search(r"version:\s*'([^']+)'", meson).group(1) == _pyproject_version()


def test_newest_metainfo_release_agrees():
    tree = ET.parse(ROOT / "data" / "io.gitlab.gtk4_apps1.Presence.metainfo.xml")
    newest = tree.find("releases/release")
    assert newest.get("version") == _pyproject_version()


def test_no_source_file_writes_its_own_version():
    pattern = re.compile(r"""version\s*=\s*(f?["'][^"']*\d+\.\d+\.\d+)""")
    offenders = [
        f"{path.relative_to(ROOT)}: {m.group(0)}"
        for path in (ROOT / "src" / "presence").rglob("*.py")
        for m in pattern.finditer(path.read_text())
    ]
    assert offenders == []
