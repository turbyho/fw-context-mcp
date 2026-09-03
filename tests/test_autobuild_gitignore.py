"""Keeping the autobuild output out of the user's git status.

fw-context builds into ``.fw-context/autobuild/`` when it starts a build of
its own.  That output is a tool's build artifact, and it was showing up as
untracked in the user's repository — measured on the Mbed project, which had been
initialised before that directory existed and so listed only
``.fw-context/build/``.

Two things cover it, and both are tested here: the rule written inside the
directory, which needs no action from the user and works for a project that
will never run ``init`` again, and the entry ``init`` adds for new projects.
"""
import subprocess

import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.utils import AUTOBUILD_REL, ignore_autobuild_dir


def test_the_rule_is_written_inside_the_directory(tmp_path):
    """The project's own .gitignore is not touched.

    That file belongs to the user, and a tool appending to it on every build
    would be its own kind of noise.
    """
    project_gitignore = tmp_path / ".gitignore"
    project_gitignore.write_text("build/\n", encoding="utf-8")

    ignore_autobuild_dir(tmp_path)

    written = tmp_path / AUTOBUILD_REL / ".gitignore"
    assert written.read_text(encoding="utf-8").endswith("*\n")
    assert project_gitignore.read_text(encoding="utf-8") == "build/\n"


def test_it_creates_the_directory_when_missing(tmp_path):
    """The rule has to be in place BEFORE a builder writes anything there."""
    assert not (tmp_path / AUTOBUILD_REL).exists()

    ignore_autobuild_dir(tmp_path)

    assert (tmp_path / AUTOBUILD_REL / ".gitignore").is_file()


def test_it_does_not_overwrite_an_existing_rule(tmp_path):
    """A user who edited the file keeps their version.

    It runs before every build, so overwriting would undo an edit silently
    and repeatedly.
    """
    marker = tmp_path / AUTOBUILD_REL / ".gitignore"
    marker.parent.mkdir(parents=True)
    marker.write_text("*\n!keep-this\n", encoding="utf-8")

    ignore_autobuild_dir(tmp_path)

    assert marker.read_text(encoding="utf-8") == "*\n!keep-this\n"


def test_an_unwritable_project_does_not_stop_a_build(tmp_path):
    """Untidy git status beats a refused build."""
    (tmp_path / ".fw-context").write_text("not a directory", encoding="utf-8")

    ignore_autobuild_dir(tmp_path)  # must not raise


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True, check=False).returncode != 0,
    reason="git is not available",
)
def test_git_really_stops_reporting_the_directory(tmp_path):
    """The point of the whole thing, checked against git itself.

    A rule that looks right but that git does not honour would be no fix, so
    this asserts on `git status` and not on the file we wrote.
    """
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            capture_output=True, text=True, check=True,
        ).stdout

    git("init", "-q")
    # Hermetic: the machine's identity and signing settings must not decide
    # whether this passes.
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    git("config", "commit.gpgsign", "false")

    # The shape of a real project: config.toml is committed, so git knows
    # about .fw-context/ and reports paths inside it individually.  Without
    # a tracked file in there, `git status` collapses the whole directory to
    # one `?? .fw-context/` line and the test would prove nothing.
    config = tmp_path / ".fw-context" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[build]\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".fw-context/build/\n", encoding="utf-8")
    git("add", ".fw-context/config.toml", ".gitignore")
    git("commit", "-q", "-m", "initial")

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")

    # An autobuild output as a builder would leave it.
    output = tmp_path / AUTOBUILD_REL / "default"
    output.mkdir(parents=True)
    (output / "app.elf").write_bytes(b"\x7fELF")

    before = git("status", "--porcelain")
    assert ".fw-context/autobuild/" in before, before

    ignore_autobuild_dir(tmp_path)

    after = git("status", "--porcelain")
    assert ".fw-context" not in after, after
    assert "main.c" in after, "an unrelated file must stay visible"
    assert git("ls-files", ".fw-context/").strip() == ".fw-context/config.toml", (
        "the committed config must stay tracked"
    )


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True, check=False).returncode != 0,
    reason="git is not available",
)
def test_init_covers_the_directory_for_a_new_project(tmp_path):
    """A new project ignores the autobuild output through its own .gitignore.

    The rule inside the directory covers every project; the entry that
    ``init`` writes is what covers a project whose autobuild directory does
    not exist yet.

    ``init`` writes ``.fw-context/*`` and not one line for each directory,
    thus this asks git what the entries do and does not read their text.
    The shared ``config.toml`` must stay visible — ``test_gitignore_rules``
    holds that whole rule.
    """
    from fw_context_mcp.cli._init import _ensure_gitignore

    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    _ensure_gitignore(tmp_path, fix=True)

    def ignored(relative: str) -> bool:
        return subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", "-q", relative], check=False
        ).returncode == 0

    assert ignored(f"{AUTOBUILD_REL}/default/app.elf")
    assert ignored(".fw-context/build/compile_commands.json")
    assert not ignored(".fw-context/config.toml"), "the shared config must stay committable"
