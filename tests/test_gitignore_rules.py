"""``.fw-context/config.toml`` must be committable, the rest must not.

``.fw-context/`` holds one file the team shares — ``config.toml``, the
build configuration that gives every developer the same index — and
several that must stay out: ``local.toml`` (paths and API keys of one
developer), ``build/`` and ``autobuild/`` (generated output).

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


# ── What git actually does ─────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_agrees(tmp_path):
    """Ask git, and not this test file, whether the rules are right."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603,S607
    fw = tmp_path / ".fw-context"
    (fw / "build").mkdir(parents=True)
    (fw / "autobuild" / "default").mkdir(parents=True)
    for path in (
        fw / "config.toml",
        fw / "local.toml",
        fw / "build" / "compile_commands.json",
        fw / "autobuild" / "default" / ".link_script.ld",
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
    assert ignored(".fw-context/build/compile_commands.json")
    assert ignored(".fw-context/autobuild/default/.link_script.ld")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_agrees_about_a_project_below_the_root(tmp_path):
    """A repository can hold a bootloader beside an application.

    Only the root usually has a ``.gitignore``.  A pair without the
    ``**/`` prefix holds a leading path element, which anchors it to the
    root, and the ``local.toml`` of the second project reaches git.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603,S607
    fw = tmp_path / "sub" / "bootloader" / ".fw-context"
    (fw / "autobuild" / "default").mkdir(parents=True)
    (fw / "config.toml").write_text("", encoding="utf-8")
    (fw / "local.toml").write_text("", encoding="utf-8")
    (fw / "autobuild" / "default" / "app.elf").write_text("", encoding="utf-8")

    _ensure_gitignore(tmp_path, fix=True)

    def ignored(relative: str) -> bool:
        result = subprocess.run(  # noqa: S603,S607
            ["git", "check-ignore", "-q", relative], cwd=tmp_path, check=False
        )
        return result.returncode == 0

    assert not ignored("sub/bootloader/.fw-context/config.toml")
    assert ignored("sub/bootloader/.fw-context/local.toml")
    assert ignored("sub/bootloader/.fw-context/autobuild/default/app.elf")
