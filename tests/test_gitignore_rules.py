"""``.fw-context/config.toml`` must be committable, the rest must not.

``.fw-context/`` holds one file the team shares — ``config.toml``, the
build configuration that gives every developer the same index — and
several that must stay out: ``local.toml`` (paths and API keys of one
developer) and ``build/`` (generated output).

The rule turns on one character.  ``.fw-context/`` excludes the
DIRECTORY, and git does not descend into an excluded directory, thus no
later negation can bring ``config.toml`` back.  ``.fw-context/*``
excludes the CONTENTS, and the negation works.  The last test in this
file asks git itself, and not this reasoning.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from fw_context_mcp.cli._init import (
    FW_CONTEXT_IGNORE_PAIR,
    FW_CONTEXT_SUPERSEDED_IGNORES,
    _ensure_gitignore,
    plan_gitignore,
)

EXCLUDE, NEGATION = FW_CONTEXT_IGNORE_PAIR


# ── The plan ───────────────────────────────────────────────────────────


def test_a_new_project_gets_the_pair_in_order():
    _, removed, append = plan_gitignore([])
    assert removed == []
    assert append.index(EXCLUDE) < append.index(NEGATION), "a negation before the exclude has no result"
    assert "compile_commands.json" in append


def test_mbed_gets_its_generated_header():
    _, _, append = plan_gitignore([], build_system="mbed-os")
    assert "mbed_config.h" in append


def test_a_correct_file_needs_no_change():
    raw = ["compile_commands.json", EXCLUDE, NEGATION]
    kept, removed, append = plan_gitignore(raw)
    assert removed == []
    assert append == []
    assert kept == raw


@pytest.mark.parametrize("superseded", sorted(FW_CONTEXT_SUPERSEDED_IGNORES))
def test_a_superseded_line_is_removed(superseded):
    """It hides config.toml, or it misses every project below the root."""
    kept, removed, append = plan_gitignore(["*.pyc", superseded, "build/"])
    assert removed == [superseded]
    assert superseded not in kept
    assert append[-2:] == [EXCLUDE, NEGATION]


def test_a_reversed_pair_is_put_back_in_order():
    kept, removed, append = plan_gitignore([NEGATION, EXCLUDE])
    assert sorted(removed) == sorted([EXCLUDE, NEGATION])
    assert EXCLUDE not in kept and NEGATION not in kept
    assert append[-2:] == [EXCLUDE, NEGATION]


def test_a_half_pair_is_completed():
    _, removed, append = plan_gitignore([NEGATION])
    assert removed == [NEGATION]
    assert append[-2:] == [EXCLUDE, NEGATION]


def test_no_other_line_is_touched():
    raw = ["# my comment", "*.pyc", ".fw-context/", "node_modules/", "", "docs/build/"]
    kept, _, _ = plan_gitignore(raw)
    assert kept == ["# my comment", "*.pyc", "node_modules/", "", "docs/build/"]


def test_the_legacy_entries_are_left_alone():
    """`.fw-context/*` covers them — removing them would be noise."""
    raw = [EXCLUDE, NEGATION, ".fw-context/build/", ".fw-context/local.toml"]
    kept, removed, append = plan_gitignore(raw)
    assert removed == []
    assert append == ["compile_commands.json"]
    assert ".fw-context/build/" in kept


# ── The file ───────────────────────────────────────────────────────────


def test_ensure_is_idempotent(tmp_path, capsys):
    for _ in range(3):
        _ensure_gitignore(tmp_path, fix=True)
    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert content.count(EXCLUDE) == 1
    assert content.count(NEGATION) == 1
    assert "[ok]" in capsys.readouterr().out


def test_ensure_repairs_a_blanket_line(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.pyc\n.fw-context/\n", encoding="utf-8")
    _ensure_gitignore(tmp_path, fix=True)
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    assert ".fw-context/" not in lines
    assert "*.pyc" in lines
    assert lines.index(EXCLUDE) < lines.index(NEGATION)


def test_a_dry_run_writes_nothing(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".fw-context/\n", encoding="utf-8")
    _ensure_gitignore(tmp_path, fix=False)
    assert gitignore.read_text(encoding="utf-8") == ".fw-context/\n"


# ── One fw-context block ───────────────────────────────────────────────

#: A .gitignore that an earlier fw-context wrote, with the block of another
#: tool after it.  The blanket line hides config.toml.
EARLIER = [
    ".pio",
    ".fw-context/",
    "",
    "# fw-context",
    "compile_commands.json",
    ".fw-context/local.toml",
    "",
    "# lean-ctx writes its rules into this file. It stays on disk, out of git.",
    "LEAN-CTX.md",
]


def _block(lines: list[str]) -> list[str]:
    """Return the lines of the first fw-context block, its header included."""
    start = lines.index("# fw-context")
    end = start + 1
    while end < len(lines) and lines[end].strip() and not lines[end].startswith("#"):
        end += 1
    return lines[start:end]


def test_new_entries_go_into_the_existing_block(tmp_path):
    """init appended a second ``# fw-context`` block after the block of another tool."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("\n".join(EARLIER) + "\n", encoding="utf-8")
    _ensure_gitignore(tmp_path, fix=True)
    lines = gitignore.read_text(encoding="utf-8").splitlines()

    assert lines.count("# fw-context") == 1
    assert _block(lines) == [
        "# fw-context", "compile_commands.json", ".fw-context/local.toml", EXCLUDE, NEGATION,
    ]
    # The new negation would go above LEAN-CTX.md, a pattern, at the end of
    # the first block: the block moves to the end of the file instead.
    lean = lines.index(EARLIER[-2])
    assert lines[lean : lean + 2] == EARLIER[-2:], "the block of the other tool stays as it is"
    assert lines[-1] == NEGATION


def test_two_blocks_become_one(tmp_path):
    """The file that the defect wrote: the next init joins the two blocks."""
    gitignore = tmp_path / ".gitignore"
    split = [line for line in EARLIER if line != ".fw-context/"] + ["", "# fw-context", EXCLUDE, NEGATION]
    gitignore.write_text("\n".join(split) + "\n", encoding="utf-8")
    _ensure_gitignore(tmp_path, fix=True)
    lines = gitignore.read_text(encoding="utf-8").splitlines()

    assert lines.count("# fw-context") == 1
    assert _block(lines) == [
        "# fw-context", "compile_commands.json", ".fw-context/local.toml", EXCLUDE, NEGATION,
    ]
    # The negation would move up past LEAN-CTX.md, a pattern: the join goes
    # into the last block instead, and the first one moves down.
    lean = lines.index(EARLIER[-2])
    assert lines[lean + 1] == "LEAN-CTX.md"
    assert lines[-1] == NEGATION
    assert "" not in lines[lean + 2 : lines.index("# fw-context")][1:], "one blank line before the block"
    assert lines[: lines.index(EARLIER[-2])].count("") == 1, "no blank line stays where the first block was"

    before = gitignore.read_text(encoding="utf-8")
    _ensure_gitignore(tmp_path, fix=True)
    assert gitignore.read_text(encoding="utf-8") == before


def test_a_file_without_a_block_gets_one_at_the_end(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.pyc\n\n", encoding="utf-8")
    _ensure_gitignore(tmp_path, fix=True)
    assert gitignore.read_text(encoding="utf-8") == (
        f"*.pyc\n\n# fw-context\ncompile_commands.json\n{EXCLUDE}\n{NEGATION}\n"
    ), "one blank line before the block, not two"


def _git_ignores(root, relative: str) -> bool:
    result = subprocess.run(  # noqa: S603,S607
        ["git", "check-ignore", "-q", relative], cwd=root, check=False
    )
    return result.returncode == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_join_keeps_the_pair_in_order(tmp_path):
    """The negation of a later block moved before an exclude outside the blocks.

    Measured before the fix: git then ignored config.toml until a second run.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603,S607
    (tmp_path / ".fw-context").mkdir()
    (tmp_path / ".fw-context" / "config.toml").write_text("", encoding="utf-8")
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(
        "\n".join(["# fw-context", "compile_commands.json", "", "# tools", EXCLUDE, "# fw-context", NEGATION]) + "\n",
        encoding="utf-8",
    )
    _ensure_gitignore(tmp_path, fix=True)
    lines = gitignore.read_text(encoding="utf-8").splitlines()

    assert not _git_ignores(tmp_path, ".fw-context/config.toml")
    assert lines.count("# fw-context") == 1
    assert lines.index(EXCLUDE) < lines.index(NEGATION)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_join_that_changes_what_git_ignores_does_not_happen(tmp_path, capsys):
    """A user negation between the blocks: a move across it changes the answer of git."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603,S607
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "compile_commands.json").write_text("", encoding="utf-8")
    (tmp_path / "keep.o").write_text("", encoding="utf-8")
    raw = [
        "# fw-context", EXCLUDE, NEGATION, "", "!tools/compile_commands.json", "*.o",
        "", "# fw-context", "compile_commands.json", "", "!keep.o",
    ]
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("\n".join(raw) + "\n", encoding="utf-8")
    before = {path: _git_ignores(tmp_path, path) for path in ("tools/compile_commands.json", "keep.o")}

    _ensure_gitignore(tmp_path, fix=True)

    assert {path: _git_ignores(tmp_path, path) for path in before} == before
    assert "[warn]" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_new_entry_does_not_go_before_a_later_line_of_the_other_polarity(tmp_path):
    """The pair went to the end of the block, before a later ``*.toml`` of another tool.

    The earlier code wrote new entries at the end of the file, after it, and
    git saw config.toml.  At the end of the block, git ignored it.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603,S607
    (tmp_path / ".fw-context").mkdir()
    (tmp_path / ".fw-context" / "config.toml").write_text("", encoding="utf-8")
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(
        "\n".join(["# fw-context", ".fw-context/", "compile_commands.json", "", "# other tool", "*.toml"]) + "\n",
        encoding="utf-8",
    )
    _ensure_gitignore(tmp_path, fix=True)
    lines = gitignore.read_text(encoding="utf-8").splitlines()

    assert not _git_ignores(tmp_path, ".fw-context/config.toml")
    assert lines.count("# fw-context") == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_repeated_entry_keeps_its_last_place(tmp_path):
    """The first copy of X, !X, X stayed, and git then stopped to ignore X."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603,S607
    (tmp_path / "LEAN-CTX.md").write_text("", encoding="utf-8")
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(
        "\n".join(["# fw-context", "# fw-context", "LEAN-CTX.md", "!LEAN-CTX.md", "LEAN-CTX.md", EXCLUDE, NEGATION])
        + "\n",
        encoding="utf-8",
    )
    assert _git_ignores(tmp_path, "LEAN-CTX.md")
    _ensure_gitignore(tmp_path, fix=True)
    assert _git_ignores(tmp_path, "LEAN-CTX.md")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_lines_that_git_reads_as_different_stay_different(tmp_path):
    """``keep.o`` and ``keep.o<TAB>`` are two patterns for git; strip() made them one."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603,S607
    (tmp_path / "keep.o").write_text("", encoding="utf-8")
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(
        "\n".join(["# fw-context", "keep.o", EXCLUDE, NEGATION, "", "# fw-context", "keep.o\t"]) + "\n",
        encoding="utf-8",
    )
    assert _git_ignores(tmp_path, "keep.o")
    _ensure_gitignore(tmp_path, fix=True)
    assert _git_ignores(tmp_path, "keep.o")


def test_a_line_with_a_leading_space_is_not_a_superseded_line():
    """Git keeps a leading space: `` .fw-context/`` names another directory."""
    kept, removed, _ = plan_gitignore([" .fw-context/", EXCLUDE, NEGATION])
    assert removed == []
    assert " .fw-context/" in kept


def test_a_file_with_a_byte_order_mark_keeps_one_block(tmp_path):
    """Git skips a UTF-8 BOM at the start of the file; the header on line 1 is a header."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_bytes(
        ("\ufeff# fw-context\ncompile_commands.json\n" + f"{EXCLUDE}\n{NEGATION}\n").encode("utf-8")
    )
    _ensure_gitignore(tmp_path, fix=True, build_system="mbed-os")
    data = gitignore.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf# fw-context\n"), "the BOM stays, the header stays first"
    assert data.count(b"# fw-context") == 1
    assert b"mbed_config.h" in data


def test_a_line_is_split_only_at_a_newline(tmp_path):
    """git splits a .gitignore at "\\n" only; a form feed is part of a pattern."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_bytes(b"pat\x0cx\n")
    _ensure_gitignore(tmp_path, fix=True)
    assert gitignore.read_bytes().startswith(b"pat\x0cx\n")


def test_mixed_line_ends_stay_as_they_are(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_bytes(b"a\nb\r\nc\n")
    _ensure_gitignore(tmp_path, fix=True)
    data = gitignore.read_bytes()
    assert data.startswith(b"a\nb\r\nc\n")
    assert b"\r" not in data[len(b"a\nb\r\nc\n"):], "the new lines take the line end of most lines"


def test_a_crlf_file_stays_crlf(tmp_path):
    """The earlier code appended to the file, thus the other lines kept their CRLF."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_bytes(b"*.pyc\r\n.fw-context/\r\n")
    _ensure_gitignore(tmp_path, fix=True)
    data = gitignore.read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n")
    assert b"*.pyc\r\n" in data


# ── What git actually does ─────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_agrees(tmp_path):
    """Ask git, and not this test file, whether the rules are right."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603,S607
    fw = tmp_path / ".fw-context"
    (fw / "build" / "default" / "out").mkdir(parents=True)
    for path in (
        fw / "config.toml",
        fw / "local.toml",
        fw / "build" / "default" / "out" / "compile_commands.json",
        fw / "build" / "default" / "out" / ".link_script.ld",
    ):
        path.write_text("", encoding="utf-8")

    # A project that carries the blanket line from an earlier version.
    (tmp_path / ".gitignore").write_text(".fw-context/\n", encoding="utf-8")
    _ensure_gitignore(tmp_path, fix=True)

    def ignored(relative: str) -> bool:
        result = subprocess.run(  # noqa: S603,S607
            ["git", "check-ignore", "-q", relative], cwd=tmp_path, check=False
        )
        return result.returncode == 0

    assert not ignored(".fw-context/config.toml"), "the shared config must be committable"
    assert ignored(".fw-context/local.toml")
    assert ignored(".fw-context/build/default/out/compile_commands.json")
    assert ignored(".fw-context/build/default/out/.link_script.ld")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_agrees_about_a_project_below_the_root(tmp_path):
    """A repository can hold a bootloader beside an application.

    Only the root usually has a ``.gitignore``.  A pair without the
    ``**/`` prefix holds a leading path element, which anchors it to the
    root, and the ``local.toml`` of the second project reaches git.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603,S607
    fw = tmp_path / "sub" / "bootloader" / ".fw-context"
    (fw / "build" / "default" / "out").mkdir(parents=True)
    (fw / "config.toml").write_text("", encoding="utf-8")
    (fw / "local.toml").write_text("", encoding="utf-8")
    (fw / "build" / "default" / "out" / "app.elf").write_text("", encoding="utf-8")

    _ensure_gitignore(tmp_path, fix=True)

    def ignored(relative: str) -> bool:
        result = subprocess.run(  # noqa: S603,S607
            ["git", "check-ignore", "-q", relative], cwd=tmp_path, check=False
        )
        return result.returncode == 0

    assert not ignored("sub/bootloader/.fw-context/config.toml")
    assert ignored("sub/bootloader/.fw-context/local.toml")
    assert ignored("sub/bootloader/.fw-context/build/default/out/app.elf")
