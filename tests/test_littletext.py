from linkedin_publisher import littletext


def test_markdown_is_normalized():
    text, counts = littletext.normalize_markdown(
        "## Title\n**bold** and *italic* and [a link](https://x.io)\n* bullet")
    assert text == "Title\nbold and italic and a link https://x.io\n- bullet"
    assert counts == {"headers": 1, "links": 1, "bullets": 1, "bold": 1, "italic": 1}


def test_intraword_underscores_are_not_italic():
    text, counts = littletext.normalize_markdown("Set SOME_ENV_VAR_NAME first.")
    assert text == "Set SOME_ENV_VAR_NAME first."
    assert counts["italic"] == 0


def test_reserved_characters_are_escaped():
    assert littletext.escape("a (b) @c") == r"a \(b\) \@c"


def test_backslash_is_escaped_once():
    assert littletext.escape("a\\b") == "a\\\\b"


def test_hashtags_are_left_alone():
    assert littletext.prepare("Done. #AIgovernance #MKB") == "Done. #AIgovernance #MKB"


def test_prepared_text_always_passes_preflight():
    nasty = "Costs (per year): <100> | {x} [y] ~z~ @me a_b *c* \\ end"
    assert littletext.find_unescaped(littletext.prepare(nasty)) == []


def test_preflight_catches_raw_text():
    assert littletext.find_unescaped("a (b)") == ["(", ")"]


def test_dutch_text_without_markup_passes_unchanged():
    text = "Ik heb vanochtend een Hyves-profiel aangemaakt. Drie minuten."
    assert littletext.prepare(text) == text
