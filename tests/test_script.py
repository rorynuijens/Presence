"""
test_script.py — The spoken script: what survives parsing, and the timing.

The presenter view paints these blocks into a GtkTextView, so a span that
is off by a character underlines the wrong word.  The timing arithmetic is
what the pace readout accuses the speaker of, so it is worth pinning too.
"""

from presence.slides.script import (
    BOLD, BULLET, CODE, HEADING, ITALIC, MONO, PARA, QUOTE,
    Span, deck_schedule, parse_inline, parse_script, script_words,
    slide_seconds, speaking_seconds,
)


def kinds(md):
    return [b.kind for b in parse_script(md)]


def texts(md):
    return [b.text for b in parse_script(md)]


# ── Inline ────────────────────────────────────────────────────────────────

def test_emphasis_becomes_a_span_over_the_spoken_words():
    text, spans = parse_inline("Say **this** clearly")
    assert text == "Say this clearly"
    assert spans == [Span(4, 8, BOLD)]


def test_nested_emphasis_keeps_both_marks():
    text, spans = parse_inline("**all of *this* matters**")
    assert text == "all of this matters"
    assert Span(0, 19, BOLD) in spans
    assert Span(7, 11, ITALIC) in spans


def test_a_link_is_read_as_its_text():
    text, spans = parse_inline("see [the paper](https://example.com/x) first")
    assert text == "see the paper first"
    assert spans == []


def test_an_image_is_read_as_its_description():
    assert parse_inline("![a bar chart](chart.png)")[0] == "a bar chart"


def test_code_is_literal_inside_its_backticks():
    text, spans = parse_inline("run `git *log*` now")
    assert text == "run git *log* now"
    assert spans == [Span(4, 13, MONO)]


def test_underscores_inside_a_word_are_not_emphasis():
    # snake_case_names would otherwise lose their underscores mid-sentence.
    assert parse_inline("call read_editor_prefs now")[0] == \
        "call read_editor_prefs now"


# ── Blocks ────────────────────────────────────────────────────────────────

def test_soft_wrapped_lines_join_into_one_paragraph():
    md = "This sentence was typed\nacross two source lines.\n"
    assert texts(md) == ["This sentence was typed across two source lines."]


def test_the_block_kinds_a_script_can_hold():
    md = (
        "## Opening\n\n"
        "A paragraph.\n\n"
        "- a point\n"
        "- another\n\n"
        "> a quotation\n\n"
        "```\ncode(1)\n```\n"
    )
    assert kinds(md) == [HEADING, PARA, BULLET, BULLET, QUOTE, CODE]


def test_a_list_item_carries_its_own_marker_and_shifts_its_spans():
    blocks = parse_script("1. the **first** thing\n")
    assert blocks[0].kind == BULLET
    assert blocks[0].text == "1. the first thing"
    # "first" sits at 7 in "1. the first thing", not at 4 as in the source.
    assert blocks[0].spans == [Span(7, 12, BOLD)]

    dash = parse_script("- the **first** thing\n")
    assert dash[0].text == "• the first thing"
    assert dash[0].spans == [Span(6, 11, BOLD)]


def test_a_fenced_block_is_not_reparsed_as_markdown():
    blocks = parse_script("```\n- not a bullet\n**not bold**\n```\n")
    assert len(blocks) == 1
    assert blocks[0].kind == CODE
    assert blocks[0].text == "- not a bullet\n**not bold**"


def test_a_list_item_typed_across_lines_stays_one_item():
    """
    Lazy continuation, which is what makes a wrapped script readable.

    Without it every continuation line became its own paragraph, so a
    three-line numbered item read as an item followed by two orphans
    hanging at the left margin.
    """
    md = (
        "1. The script is a single document. You wrote it as prose,\n"
        "   and you should be able to read it back that way.\n"
        "2. The slides come out of it.\n"
    )
    blocks = parse_script(md)
    assert [b.kind for b in blocks] == [BULLET, BULLET]
    assert blocks[0].text == (
        "1. The script is a single document. You wrote it as prose, "
        "and you should be able to read it back that way."
    )


def test_emphasis_survives_a_line_join():
    md = "- a point that is **very\n  important** indeed\n"
    block = parse_script(md)[0]
    assert block.text == "• a point that is very important indeed"
    assert Span(18, 32, BOLD) in block.spans


def test_consecutive_quote_lines_are_one_quotation():
    md = "> The gap was never features.\n> It was a premise.\n"
    blocks = parse_script(md)
    assert len(blocks) == 1
    assert blocks[0].kind == QUOTE
    assert blocks[0].text == "The gap was never features. It was a premise."


def test_an_empty_script_has_no_blocks():
    assert parse_script("") == []
    assert parse_script("   \n\n  \n") == []


# ── Timing ────────────────────────────────────────────────────────────────

def test_words_are_counted_after_the_markup_is_gone():
    # "Read the paper now" — the asterisks and the URL are not said out loud.
    assert script_words("Read **the** [paper](https://x.example) now") == 4


def test_code_is_not_counted_as_something_said():
    assert script_words("Two words\n\n```\nlots of code tokens here\n```\n") == 2


def test_speaking_seconds_rounds_to_the_nearest_second():
    assert speaking_seconds(110, 110) == 60
    assert speaking_seconds(55, 110) == 30
    assert speaking_seconds(0) == 0


def test_a_slide_is_timed_by_its_script_not_by_its_bullets():
    notes = " ".join(["word"] * 110)
    body  = "Three short bullets"
    assert slide_seconds(notes, body, 110) == 60


def test_a_slide_without_a_script_falls_back_to_its_own_text():
    body = " ".join(["word"] * 55)
    assert slide_seconds("", body, 110) == 30


def test_the_schedule_is_cumulative():
    info = [
        {"notes": " ".join(["w"] * 110), "body": ""},
        {"notes": " ".join(["w"] * 55),  "body": ""},
    ]
    assert deck_schedule(info, wpm=110) == [60, 90]


def test_a_target_duration_scales_the_estimates_to_fit_it():
    info = [
        {"notes": " ".join(["w"] * 110), "body": ""},   # twice as long…
        {"notes": " ".join(["w"] * 55),  "body": ""},   # …as this one
    ]
    # 10 minutes split 2:1 by what the script actually says.
    assert deck_schedule(info, wpm=110, target_secs=600) == [400, 600]


def test_a_deck_with_no_script_at_all_splits_the_target_evenly():
    info = [{"notes": "", "body": ""} for _ in range(4)]
    assert deck_schedule(info, wpm=110, target_secs=600) == [150, 300, 450, 600]
