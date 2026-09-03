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
