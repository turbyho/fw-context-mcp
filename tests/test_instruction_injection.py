"""``fw-context init`` must reach every instruction file, on every run.

The instructions live in ``config/tools.py`` and ``init`` writes them into
the instruction file of each AI tool.  Two write methods exist:

- ``marked_section`` — the text goes between markers in a file that also
  holds text of the user (``CLAUDE.md``, ``AGENTS.md``)
- ``separate_file`` — fw-context owns the whole file
  (``.codex/rules/fw-context.md``, ``.cursor/rules/fw-context.mdc``)

``check_target`` reads a file with three or more fw-context keywords and
no markers as text of the user, and ``init`` then skips that file.  A
separate file that fw-context wrote holds every one of those keywords,
thus each later run skipped it and its instructions stayed at the version
of the first run.  The markers in the separate file close that hole.
"""

from __future__ import annotations

import pytest

from fw_context_mcp.config.tools import (
    MARKER_END,
    MARKER_START,
    InstructionTarget,
    check_target,
)


def _target(method: str, name: str = "fw-context.md") -> InstructionTarget:
    return InstructionTarget(path="{project}/" + name, method=method, scope="project")


# ── What each method renders ───────────────────────────────────────────


def test_a_separate_file_carries_the_markers():
    text = _target("separate_file").render_instructions()
    assert text.startswith(MARKER_START)
    assert text.rstrip().endswith(MARKER_END)
    assert text.count(MARKER_START) == 1
    assert text.count(MARKER_END) == 1


def test_a_marked_section_carries_no_markers():
    """_update_marked_section adds them — a second pair would nest."""
    text = _target("marked_section").render_instructions()
    assert MARKER_START not in text
    assert MARKER_END not in text


@pytest.mark.parametrize("method", ["separate_file", "marked_section"])
def test_both_methods_carry_the_instructions(method):
    text = _target(method).render_instructions()
    assert "fw-context" in text
    assert "lookup_symbol" in text
    assert "A question about a DIFFERENT project" in text, (
        "the multi-project section does not reach this write method"
    )


# ── What init does with the result ─────────────────────────────────────


def test_init_updates_a_separate_file_that_it_wrote(tmp_path):
    """The regression: a second run must write, and not skip."""
    target = _target("separate_file")
    written = target.resolve(tmp_path)
    written.write_text(target.render_instructions(), encoding="utf-8")

    collision = check_target(target, tmp_path)
    assert collision.has_marked_section, "init would read its own file as text of the user"
    assert not collision.has_unmarked_content


def test_init_still_protects_a_file_of_the_user(tmp_path):
    """A hand-written file with fw-context text must stay untouched."""
    target = _target("separate_file", name="my-notes.md")
    notes = target.resolve(tmp_path)
    notes.write_text(
        "My own notes about fw-context.\n"
        "Use lookup_symbol for a name and search_code for a topic.\n"
        "get_active_build tells me whether the index is ready.\n",
        encoding="utf-8",
    )

    collision = check_target(target, tmp_path)
    assert collision.has_unmarked_content
    assert not collision.has_marked_section


def test_a_file_from_an_earlier_version_needs_force_once(tmp_path):
    """Documents the one migration step, so that nobody reads it as a bug."""
    target = _target("separate_file")
    legacy = target.resolve(tmp_path)
    body = target.render_instructions()
    legacy.write_text(body.replace(MARKER_START, "").replace(MARKER_END, ""), encoding="utf-8")

    collision = check_target(target, tmp_path)
    assert collision.has_unmarked_content, "an unmarked legacy file must still ask for --force"

    # --force writes the marked text, and every run after that is clean.
    legacy.write_text(body, encoding="utf-8")
    assert check_target(target, tmp_path).has_marked_section


def test_a_second_write_does_not_double_the_markers(tmp_path):
    target = _target("separate_file")
    written = target.resolve(tmp_path)
    for _ in range(2):
        written.write_text(target.render_instructions(), encoding="utf-8")
    content = written.read_text(encoding="utf-8")
    assert content.count(MARKER_START) == 1
    assert content.count(MARKER_END) == 1


def test_an_absent_file_is_no_collision(tmp_path):
    collision = check_target(_target("separate_file"), tmp_path)
    assert not collision.has_marked_section
    assert not collision.has_unmarked_content


# ── A marker is a line of its own ──────────────────────────────────────


SENTENCE = (
    "- fw-context writes the text between `<!-- fw-context -->` and\n"
    "  `<!-- /fw-context -->`. Do not edit that text.\n"
)


def _file(*parts: str) -> str:
    return "\n".join(parts)


def test_a_sentence_that_names_the_markers_is_no_block():
    """A CLAUDE.md of the user named the markers in its rules.

    A search for the substring took the sentence for a block, put the new
    block into the middle of the sentence, and kept the real block with its
    old text.
    """
    from fw_context_mcp.config.tools import marked_blocks

    assert marked_blocks("# Rules\n" + SENTENCE) == []


def test_the_real_block_is_updated_and_the_sentence_stays(tmp_path):
    from fw_context_mcp.cli._mcp import _update_marked_section

    path = tmp_path / "CLAUDE.md"
    path.write_text(_file("# Rules", SENTENCE, MARKER_START, "old rules", MARKER_END, "## After", ""),
                    encoding="utf-8")

    update = _update_marked_section(path, "new rules", marker="fw-context")

    text = path.read_text(encoding="utf-8")
    assert (update.blocks_removed, update.orphans_removed) == (0, 0)
    assert SENTENCE in text, "the sentence of the user changed"
    assert "new rules" in text and "old rules" not in text
    assert text.index("## After") > text.index("new rules"), "the text after the block moved"


def test_a_file_that_only_names_the_markers_gets_one_block(tmp_path):
    from fw_context_mcp.cli._mcp import _update_marked_section

    path = tmp_path / "CLAUDE.md"
    path.write_text("# Rules\n" + SENTENCE, encoding="utf-8")

    _update_marked_section(path, "new rules", marker="fw-context")

    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Rules\n" + SENTENCE)
    assert text.count(MARKER_START) == 2, "the sentence and one real block"


def test_a_second_block_goes(tmp_path):
    """A merge left two blocks, and an update of the first alone kept two versions of the rules."""
    from fw_context_mcp.cli._mcp import _update_marked_section

    path = tmp_path / "CLAUDE.md"
    path.write_text(_file("# Rules", MARKER_START, "old one", MARKER_END, "## Middle", "",
                          MARKER_START, "old two", MARKER_END, "## End", ""), encoding="utf-8")

    update = _update_marked_section(path, "new rules", marker="fw-context")

    text = path.read_text(encoding="utf-8")
    assert update.blocks_removed == 1
    assert text.count(MARKER_START) == 1 and "new rules" in text
    assert "old one" not in text and "old two" not in text
    assert text.index("new rules") < text.index("## Middle") < text.index("## End")


def test_check_target_does_not_take_the_sentence_for_a_block(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("# Rules\n" + SENTENCE, encoding="utf-8")
    target = InstructionTarget(path="{project}/CLAUDE.md", method="marked_section", scope="project")

    assert check_target(target, tmp_path).has_marked_section is False


def test_an_agent_file_keeps_its_sentence(tmp_path):
    """The sentence stands BEFORE the real block: a search for the substring found the sentence first."""
    from fw_context_mcp.cli._mcp import _inject_agent_section

    path = tmp_path / "agent.md"
    path.write_text(_file("---", "name: a", "---", "", SENTENCE, MARKER_START, "old", MARKER_END, ""),
                    encoding="utf-8")

    update = _inject_agent_section(path, "new", marker="fw-context")

    text = path.read_text(encoding="utf-8")
    assert update.replaced
    assert SENTENCE in text, "the sentence of the user changed"
    assert "new" in text and "old" not in text
    assert text.count(MARKER_START + "\n") == 1


def test_check_target_finds_the_block_beside_the_sentence(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text(_file("# Rules", SENTENCE, MARKER_START, "old", MARKER_END, ""), encoding="utf-8")
    target = InstructionTarget(path="{project}/CLAUDE.md", method="marked_section", scope="project")

    assert check_target(target, tmp_path).has_marked_section is True


# ── Markers without a pair, and markers in a code example ──────────────


def test_a_start_without_an_end_does_not_take_the_text_of_the_user(tmp_path):
    """A merge left a START alone above the text of the user, and the real block below it.

    The span from the first START took the text between the two starts for
    the block, and the update deleted it.
    """
    from fw_context_mcp.cli._mcp import _update_marked_section

    path = tmp_path / "CLAUDE.md"
    path.write_text(_file("# T", MARKER_START, "USER1", "## Mine", "USER2",
                          MARKER_START, "old", MARKER_END, ""), encoding="utf-8")

    update = _update_marked_section(path, "NEW", marker="fw-context")

    text = path.read_text(encoding="utf-8")
    assert "USER1" in text and "## Mine" in text and "USER2" in text, text
    assert (update.blocks_removed, update.orphans_removed) == (0, 1)
    assert text.count(MARKER_START) == 1 and "NEW" in text and "old" not in text


def test_an_end_without_a_start_goes(tmp_path):
    from fw_context_mcp.cli._mcp import _update_marked_section

    path = tmp_path / "CLAUDE.md"
    path.write_text(_file("# T", "text", MARKER_END, "more", MARKER_START, "old", MARKER_END, ""),
                    encoding="utf-8")

    update = _update_marked_section(path, "NEW", marker="fw-context")

    text = path.read_text(encoding="utf-8")
    assert update.orphans_removed == 1
    assert text.count(MARKER_END) == 1 and "text" in text and "more" in text


def test_a_lone_start_goes_and_the_block_is_added_once(tmp_path):
    """A file with a START alone gets one block, and a second run changes nothing."""
    from fw_context_mcp.cli._mcp import _update_marked_section

    path = tmp_path / "CLAUDE.md"
    path.write_text(_file("# T", MARKER_START, "USER", ""), encoding="utf-8")

    first = _update_marked_section(path, "NEW", marker="fw-context")
    after_first = path.read_text(encoding="utf-8")
    _update_marked_section(path, "NEW", marker="fw-context")

    assert first.orphans_removed == 1 and not first.replaced
    assert "USER" in after_first and after_first.count(MARKER_START) == 1
    assert path.read_text(encoding="utf-8") == after_first, "a second run changed the file"



def test_a_toml_agent_gets_the_same_rules(tmp_path):
    from fw_context_mcp.cli._mcp import _inject_agent_toml_section

    path = tmp_path / "agent.toml"
    path.write_text(_file("[instructions]", "Text names # fw-context in a line.", "# fw-context", "old one",
                          "# /fw-context", "keep", "# fw-context", "old two", "# /fw-context", ""),
                    encoding="utf-8")

    update = _inject_agent_toml_section(path, "NEW", "fw-context")

    text = path.read_text(encoding="utf-8")
    assert update.replaced and update.blocks_removed == 1
    assert "Text names # fw-context in a line." in text and "keep" in text
    assert "old one" not in text and "old two" not in text and text.count("NEW") == 1


def test_init_names_what_it_removed(capsys, tmp_path):
    from fw_context_mcp.cli._init import _report_marker_cleanup

    _report_marker_cleanup(tmp_path / "CLAUDE.md", 1, 2)
    _report_marker_cleanup(tmp_path / "AGENTS.md", 0, 0)
    _report_marker_cleanup(tmp_path / "x.md", 1, 0, dry_run=True)

    out = capsys.readouterr().out
    assert "removed 1 older fw-context section(s) and 2 fw-context marker line(s) without a pair" in out
    assert "AGENTS.md" not in out, "nothing removed, nothing said"
    assert "[dry-run]" in out and "would remove 1 older" in out


def test_an_indented_example_is_no_block(tmp_path):
    """A marker counts only from the first column: an indented line is an example of the user."""
    from fw_context_mcp.cli._mcp import _update_marked_section

    example = _file("    " + MARKER_START, "    example", "    " + MARKER_END)
    path = tmp_path / "CLAUDE.md"
    path.write_text(_file("# T", example, "", MARKER_START, "old", MARKER_END, ""), encoding="utf-8")

    update = _update_marked_section(path, "NEW", marker="fw-context")

    text = path.read_text(encoding="utf-8")
    assert example in text, "the example of the user changed"
    assert update.blocks_removed == 0 and "NEW" in text and "old" not in text


TRICKY = {
    "orphan end first": _file(MARKER_END, MARKER_START, "old", MARKER_END, "# after", ""),
    "orphan start first": _file(MARKER_START, MARKER_START, "old", MARKER_END, ""),
    "sentence, block": _file("# T", SENTENCE, MARKER_START, "old", MARKER_END, "# after", ""),
    "two blocks": _file("# T", MARKER_START, "a", MARKER_END, "x", MARKER_START, "b", MARKER_END, "y", ""),
    "block at the top": _file(MARKER_START, "old", MARKER_END, "# after", ""),
    "no block": _file("# T", "text", ""),
    "empty": "",
}


@pytest.mark.parametrize("name", sorted(TRICKY))
def test_a_second_run_changes_nothing(tmp_path, name):
    from fw_context_mcp.cli._mcp import _update_marked_section

    path = tmp_path / "CLAUDE.md"
    path.write_text(TRICKY[name], encoding="utf-8")

    _update_marked_section(path, "NEW", marker="fw-context")
    first = path.read_text(encoding="utf-8")
    _update_marked_section(path, "NEW", marker="fw-context")

    assert path.read_text(encoding="utf-8") == first
    assert first.count(MARKER_START + "\n") == 1 and "NEW" in first


@pytest.mark.parametrize("body", ["\nBody\n", "Body\n", "\n\n\nBody\n"])
def test_an_agent_file_is_stable_after_the_first_insert(tmp_path, body):
    """The first insert left a blank line that the second run removed (in HEAD too)."""
    from fw_context_mcp.cli._mcp import _inject_agent_section

    path = tmp_path / "agent.md"
    path.write_text("---\nname: a\n---\n" + body, encoding="utf-8")

    _inject_agent_section(path, "NEW", marker="fw-context")
    first = path.read_text(encoding="utf-8")
    _inject_agent_section(path, "NEW", marker="fw-context")

    assert path.read_text(encoding="utf-8") == first
    assert first.startswith("---\nname: a\n---\n\n" + MARKER_START) and "Body" in first


def test_a_toml_agent_is_stable_after_the_first_insert(tmp_path):
    from fw_context_mcp.cli._mcp import _inject_agent_toml_section

    path = tmp_path / "agent.toml"
    path.write_text(_file("# name: a", "", "[instructions]", "Text.", "", "[other]", "k = 1", ""),
                    encoding="utf-8")

    _inject_agent_toml_section(path, "NEW", "fw-context")
    first = path.read_text(encoding="utf-8")
    _inject_agent_toml_section(path, "NEW", "fw-context")

    assert path.read_text(encoding="utf-8") == first
    assert first.index("Text.") < first.index("NEW") < first.index("[other]")


def test_a_dry_run_on_a_file_it_cannot_read_gives_no_count(tmp_path, capsys):
    """A file that is not UTF-8 made the dry run stop with a traceback."""
    from fw_context_mcp.cli._init import _report_planned_cleanup

    path = tmp_path / "CLAUDE.md"
    path.write_bytes(b"# caf\xe9\n")

    _report_planned_cleanup(path)
    _report_planned_cleanup(tmp_path / "absent.md")

    assert capsys.readouterr().out == ""


# ── Offsets of a removed block, and a file of white space only ────────


@pytest.mark.parametrize("count", [2, 3])
def test_a_leading_blank_line_keeps_the_text_between_the_blocks(count):
    """A file that starts with a blank line, with two blocks or more.

    The removal of a later block took the blank line off the start of the
    file, and the edit of the first block then used its old offsets: the
    update cut characters of the user, ``mid`` became ``id``, and with three
    blocks the text between them went.  The first insert of an older
    version wrote that blank line in an agent file without frontmatter.
    """
    from fw_context_mcp.config.tools import replace_marked_blocks

    parts = [""]
    for number in range(count):
        parts += [MARKER_START, f"old{number}", MARKER_END, f"user{number}"]
    text = _file(*parts, "")

    update = replace_marked_blocks(text, _file(MARKER_START, "NEW", MARKER_END, ""))

    assert update.text == _file(MARKER_START, "NEW", MARKER_END, "", "\n\n".join(
        f"user{number}" for number in range(count)), "")
    assert update.blocks_removed == count - 1


def test_an_orphan_at_the_start_keeps_the_text_between_the_blocks():
    """The removal of an orphan start marker leaves the blank line at the start of the file."""
    from fw_context_mcp.config.tools import replace_marked_blocks

    text = _file(MARKER_START, "", MARKER_START, "a", MARKER_END, "mid", MARKER_START, "b", MARKER_END, "")

    update = replace_marked_blocks(text, _file(MARKER_START, "NEW", MARKER_END, ""))

    assert update.text == _file(MARKER_START, "NEW", MARKER_END, "", "mid", "")
    assert (update.blocks_removed, update.orphans_removed) == (1, 1)


def test_an_agent_file_with_a_leading_blank_line_keeps_its_text(tmp_path):
    from fw_context_mcp.cli._mcp import _inject_agent_section

    path = tmp_path / "agent.md"
    path.write_text(_file("", MARKER_START, "a", MARKER_END, "User paragraph one.",
                          MARKER_START, "b", MARKER_END, ""), encoding="utf-8")

    _inject_agent_section(path, "NEW", marker="fw-context")

    text = path.read_text(encoding="utf-8")
    assert "\nUser paragraph one.\n" in text, text
    assert text.count(MARKER_START + "\n") == 1 and text.startswith(MARKER_START)


def test_a_toml_agent_with_a_leading_blank_line_keeps_its_keys(tmp_path):
    from fw_context_mcp.cli._mcp import _inject_agent_toml_section

    start, end = "# fw-context", "# /fw-context"
    path = tmp_path / "agent.toml"
    path.write_text(_file("", start, "a", end, "key = 1", start, "b", end, ""), encoding="utf-8")

    _inject_agent_toml_section(path, "NEW", "fw-context")

    text = path.read_text(encoding="utf-8")
    assert "\nkey = 1\n" in text, text
    assert text.count(start + "\n") == 1 and "#\n" not in text


@pytest.mark.parametrize("content", [" ", "\t\n", "\n \n"])
def test_a_file_of_white_space_only_gets_a_whole_block(tmp_path, content):
    """The white space of the last line stood before the start marker.

    The start marker was then no marker, the update removed the end
    marker as an orphan, and each later run added one more block.
    """
    from fw_context_mcp.cli._mcp import _update_marked_section
    from fw_context_mcp.config.tools import marked_blocks

    path = tmp_path / "CLAUDE.md"
    path.write_text(content, encoding="utf-8")

    _update_marked_section(path, "NEW", marker="fw-context")
    first = path.read_text(encoding="utf-8")
    _update_marked_section(path, "NEW", marker="fw-context")

    assert first.startswith(MARKER_START + "\n") and len(marked_blocks(first)) == 1, repr(first)
    assert path.read_text(encoding="utf-8") == first
