from linkedin_publisher import frontmatter


def parse(text: str) -> dict:
    lines, _ = frontmatter.split(text)
    return frontmatter.parse(lines)


def test_inline_comment_after_quoted_value_is_dropped():
    # The bug that silently removed a post from the schedule.
    meta = parse('---\nslot_time: "10:00"  # moved from 23-09\n---\nbody')
    assert meta["slot_time"] == "10:00"


def test_inline_comment_after_unquoted_value_is_dropped():
    assert parse("---\nslot_time: 10:00  # moved\n---\n")["slot_time"] == "10:00"


def test_hash_inside_quotes_is_kept():
    assert parse('---\nalt_text: "A card with #AIgovernance on it"\n---\n')["alt_text"] == \
        "A card with #AIgovernance on it"


def test_hash_without_space_is_not_a_comment():
    assert parse("---\ntag: C#\n---\n")["tag"] == "C#"


def test_literal_block_keeps_line_breaks():
    meta = parse("---\nalt_text: |\n  First line.\n  Second line.\nstatus: queued\n---\n")
    assert meta["alt_text"] == "First line.\nSecond line."
    assert meta["status"] == "queued"


def test_folded_block_joins_lines():
    assert parse("---\nnote: >\n  one\n  two\n---\n")["note"] == "one two"


def test_indented_line_inside_a_block_never_overrides_a_real_key():
    meta = parse("---\nslot: 2026-09-24\nsource: |\n  slot: 2099-01-01\n  status: queued\n---\n")
    assert meta["slot"] == "2026-09-24"
    assert "status" not in meta


def test_stray_indented_line_is_ignored():
    assert parse("---\nslot: 2026-09-24\n  slot: 2099-01-01\n---\n")["slot"] == "2026-09-24"


def test_no_frontmatter_returns_everything_as_body():
    lines, body = frontmatter.split("Just text.")
    assert lines == [] and body == "Just text."


def test_unclosed_frontmatter_is_treated_as_body():
    lines, body = frontmatter.split("---\nstatus: queued\nno closing line")
    assert lines == []


def test_body_starts_after_closing_line():
    _, body = frontmatter.split("---\nstatus: queued\n---\n\nHello.\n\n---\n\nnotes")
    assert body.startswith("Hello.")
    assert "notes" in body
