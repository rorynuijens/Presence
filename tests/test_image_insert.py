"""
test_image_insert.py — Inserting a picture writes a path a slide can use.

A slide's image source ends at whitespace or a closing paren
(`splitter._IMAGE_RE`), so a picture called "My Holiday Photo.png" produces a
tag that parses as no image at all, and "shot(1).png" produces one truncated
to "shot(1".  Either way the writer chose a file and got nothing on the slide.

Presence copies the picture into `assets/` next to the document, so it owns
that copy's name and can give it one the document is able to refer to.  These
tests are about that naming, and about what happens when there is nowhere to
copy to.

The naming logic is a static method and the copy step only touches the
filesystem, so neither needs a widget — which is just as well, since GTK 4
widgets segfault under this suite's conftest.
"""

import pytest

from pathlib import Path

from presence.editor import Editor, _same_file
from presence.slides.splitter import _IMAGE_RE


def parsed_src(tag: str):
    """What the splitter would actually read out of *tag*, or None."""
    match = _IMAGE_RE.search(tag)
    return match.group(2) if match else None


# ── Naming the copy ───────────────────────────────────────────────────────────

def test_a_plain_name_is_left_alone():
    assert Editor._asset_name(Path("/pictures/barn.jpg")) == "barn.jpg"


def test_spaces_become_hyphens():
    assert Editor._asset_name(Path("/p/My Holiday Photo.png")) == "My-Holiday-Photo.png"


def test_brackets_and_quotes_go():
    assert Editor._asset_name(Path("/p/shot(1).png")) == "shot-1.png"
    assert Editor._asset_name(Path("/p/it's here.png")) == "it-s-here.png"


def test_a_run_of_unsafe_characters_collapses_to_one_hyphen():
    assert Editor._asset_name(Path("/p/a   b.png")) == "a-b.png"


def test_leading_and_trailing_hyphens_are_trimmed():
    assert Editor._asset_name(Path("/p/ spaced .png")) == "spaced.png"


def test_a_name_of_nothing_but_spaces_still_gets_a_name():
    assert Editor._asset_name(Path("/p/   .png")) == "image.png"


def test_non_ascii_names_are_kept():
    """The syntax can carry them, so there is no reason to mangle them."""
    assert Editor._asset_name(Path("/p/café.png")) == "café.png"


@pytest.mark.parametrize("name", [
    "My Holiday Photo.png", "shot(1).png", "it's here.png", " spaced .png",
])
def test_every_renamed_copy_can_be_read_back_out_of_a_slide(name):
    """The point of the renaming: the tag has to parse to the file it names."""
    safe = Editor._asset_name(Path("/pictures") / name)

    assert parsed_src(f"![alt](assets/{safe})") == f"assets/{safe}"


def test_the_original_names_are_what_break():
    """Pins the bug itself, so a future change cannot quietly reintroduce it."""
    assert parsed_src("![alt](assets/My Holiday Photo.png)") is None
    assert parsed_src("![alt](assets/shot(1).png)") == "assets/shot(1"


# ── Copying next to the document ──────────────────────────────────────────────

class FakeEditor:
    """Editor's copy step, without a widget."""

    _UNSAFE_IN_SRC = Editor._UNSAFE_IN_SRC
    _asset_name = staticmethod(Editor._asset_name)
    _copy_into_assets = Editor._copy_into_assets

    def __init__(self, base_path=None):
        self._base_path = base_path


def test_a_picture_is_copied_next_to_the_document(tmp_path):
    doc = tmp_path / "talk.md"
    doc.write_text("")
    src = tmp_path / "pictures" / "My Holiday Photo.png"
    src.parent.mkdir()
    src.write_bytes(b"PNGDATA")

    rel = FakeEditor(doc)._copy_into_assets(src)

    assert rel == "assets/My-Holiday-Photo.png"
    assert (tmp_path / "assets" / "My-Holiday-Photo.png").read_bytes() == b"PNGDATA"


def test_the_same_picture_twice_is_copied_once(tmp_path):
    doc = tmp_path / "talk.md"
    doc.write_text("")
    src = tmp_path / "barn.png"
    src.write_bytes(b"PNGDATA")
    editor = FakeEditor(doc)

    first = editor._copy_into_assets(src)
    second = editor._copy_into_assets(src)

    assert first == second == "assets/barn.png"
    assert len(list((tmp_path / "assets").iterdir())) == 1


def test_a_different_picture_of_the_same_name_does_not_overwrite(tmp_path):
    """
    Two folders each holding a "shot.png" must stay two pictures.

    Reusing the name would silently change what the first tag in the document
    points at, which is the kind of thing a writer finds out on stage.
    """
    doc = tmp_path / "talk.md"
    doc.write_text("")
    one = tmp_path / "a" / "shot.png"
    two = tmp_path / "b" / "shot.png"
    for path, data in ((one, b"FIRST"), (two, b"SECOND")):
        path.parent.mkdir()
        path.write_bytes(data)
    editor = FakeEditor(doc)

    first = editor._copy_into_assets(one)
    second = editor._copy_into_assets(two)

    assert first == "assets/shot.png"
    assert second == "assets/shot-2.png"
    assert (tmp_path / "assets" / "shot.png").read_bytes() == b"FIRST"
    assert (tmp_path / "assets" / "shot-2.png").read_bytes() == b"SECOND"


def test_no_document_means_nowhere_to_copy_to(tmp_path):
    src = tmp_path / "barn.png"
    src.write_bytes(b"PNGDATA")

    assert FakeEditor(None)._copy_into_assets(src) is None


def test_a_copy_that_cannot_be_made_reports_rather_than_raises(tmp_path):
    doc = tmp_path / "nowhere" / "talk.md"          # its folder does not exist
    src = tmp_path / "barn.png"
    src.write_bytes(b"PNGDATA")

    assert FakeEditor(doc)._copy_into_assets(src) is None


# ── Telling one file from another ─────────────────────────────────────────────

def test_same_file_sees_through_two_paths_to_one_file(tmp_path):
    a = tmp_path / "one.png"
    a.write_bytes(b"DATA")

    assert _same_file(a, a) is True


def test_same_file_compares_contents_not_names(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"DATA")
    b.write_bytes(b"DATA")

    assert _same_file(a, b) is True


def test_same_file_is_false_for_different_pictures(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"FIRST")
    b.write_bytes(b"SECOND!")

    assert _same_file(a, b) is False


def test_same_file_compares_bytes_not_just_size(tmp_path):
    """Two different pictures can easily be the same number of bytes."""
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"AAAAAAAA")
    b.write_bytes(b"BBBBBBBB")

    assert _same_file(a, b) is False


def test_same_file_is_false_when_one_is_missing(tmp_path):
    a = tmp_path / "a.png"
    a.write_bytes(b"DATA")

    assert _same_file(a, tmp_path / "gone.png") is False


# ── Nothing to copy to ────────────────────────────────────────────────────────

def test_an_unsaved_presentation_says_why_it_cannot_take_the_picture(tmp_path):
    """
    With no document on disk there is no assets/ to copy into, and the path as
    it stands would produce a tag that renders nothing.  Say so rather than
    inserting one.
    """
    src = tmp_path / "My Holiday Photo.png"
    src.write_bytes(b"PNGDATA")

    inserted, notices = _insert_without_a_widget(None, src)

    assert inserted == []
    assert notices and "Save the presentation first" in notices[0]


def test_an_unsaved_presentation_still_takes_a_plain_name(tmp_path):
    """A path the syntax can carry works fine without a document to sit beside."""
    src = tmp_path / "barn.png"
    src.write_bytes(b"PNGDATA")

    inserted, notices = _insert_without_a_widget(None, src)

    assert inserted == [f"![barn]({src})"]
    assert notices == []


def _insert_without_a_widget(base_path, src: Path):
    """
    Run Editor._on_layout_insert's logic with the widget parts stubbed.

    The method's decision — copy, refuse, or insert as-is — is what matters
    here; _replace_selection and grab_focus are the widget's business.
    """
    inserted, notices = [], []

    class Stub(FakeEditor):
        _on_layout_insert = Editor._on_layout_insert

        def _replace_selection(self, text):
            inserted.append(text)

        def emit(self, _signal, message):
            notices.append(message)

        @property
        def _view(self):
            class _V:
                grab_focus = staticmethod(lambda: None)
            return _V()

    Stub(base_path)._on_layout_insert(src.stem, str(src))
    return inserted, notices
