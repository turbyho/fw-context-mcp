"""The build tree of fw-context: where each build writes, and git does not see it.

Every build that fw-context runs writes into ``.fw-context/build/<variant>/out``
(see ``indexer/build_layout.py``).  The variant name is a directory name, thus
it has the rules of a directory name on Linux, macOS and Windows, and two
names of one directory are an error.

The output is a tool's build artifact, and it must not show up as untracked
in the user's repository.  Two things cover that, and both are tested here:
the rule written inside the build tree, which needs no action from the user
and works for a project that will never run ``init`` again, and the entry
``init`` adds for new projects.
"""
import subprocess
from pathlib import Path

import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer.build_layout import (
    BUILD_ROOT_REL,
    DEFAULT_VARIANT,
    BuildLayout,
    InvalidVariantName,
    check_variant_names,
    validate_variant_name,
)


class TestPaths:
    def test_the_build_without_variants_has_a_name(self, tmp_path: Path):
        layout = BuildLayout(tmp_path)

        assert layout.variant_dir("") == tmp_path / BUILD_ROOT_REL / DEFAULT_VARIANT
        assert layout.out_dir("") == tmp_path / ".fw-context" / "build" / "default" / "out"

    def test_each_variant_gets_its_own_output_directory(self, tmp_path: Path):
        """One shared directory would make the variants overwrite each other."""
        layout = BuildLayout(tmp_path)

        assert layout.out_dir("nrf52840-dev") != layout.out_dir("nrf54lm20a-dev")
        assert layout.out_dir("nrf52840-dev") == tmp_path / BUILD_ROOT_REL / "nrf52840-dev" / "out"

    def test_a_path_creates_nothing(self, tmp_path: Path):
        """A reader asks for the paths too, and must not make a directory that says "a build was here"."""
        BuildLayout(tmp_path).out_dir("v")

        assert not (tmp_path / ".fw-context").exists()

    def test_a_bad_name_is_refused_before_it_becomes_a_path(self, tmp_path: Path):
        with pytest.raises(InvalidVariantName):
            BuildLayout(tmp_path).out_dir("../escape")


class TestVariantNames:
    @pytest.mark.parametrize("name", ["default", "nrf52840-dev", "esp32dev", "v1.2_rc", "Release", "console", "com10"])
    def test_a_directory_name_is_accepted(self, name: str):
        validate_variant_name(name)

    @pytest.mark.parametrize(
        "name",
        ["", ".", "..", ".hidden", "a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b", "a\tb",
         "dev.", "dev ", "nul", "CON", "com1", "lpt9.dev"],
    )
    def test_a_name_that_cannot_be_a_directory_is_refused(self, name: str):
        with pytest.raises(InvalidVariantName):
            validate_variant_name(name)

    def test_two_names_of_one_directory_are_refused(self):
        """macOS and Windows file systems do not tell case apart, thus the two share one directory there."""
        with pytest.raises(InvalidVariantName, match="differ only in case"):
            check_variant_names(["Dev", "dev"])

    def test_a_repeated_name_is_not_a_case_collision(self):
        check_variant_names(["dev", "dev", "prod"])


class TestIgnoreRule:
    def test_the_rule_is_written_inside_the_build_tree(self, tmp_path: Path):
        """The project's own .gitignore is not touched.

        That file belongs to the user, and a tool appending to it on every
        build would be its own kind of noise.
        """
        project_gitignore = tmp_path / ".gitignore"
        project_gitignore.write_text("build/\n", encoding="utf-8")

        BuildLayout(tmp_path).ensure_ignored()

        written = tmp_path / BUILD_ROOT_REL / ".gitignore"
        assert written.read_text(encoding="utf-8").endswith("*\n")
        assert project_gitignore.read_text(encoding="utf-8") == "build/\n"

    def test_it_creates_the_directory_when_missing(self, tmp_path: Path):
        """The rule has to be in place BEFORE a builder writes anything there."""
        assert not (tmp_path / BUILD_ROOT_REL).exists()

        BuildLayout(tmp_path).ensure_ignored()

        assert (tmp_path / BUILD_ROOT_REL / ".gitignore").is_file()

    def test_it_does_not_overwrite_an_existing_rule(self, tmp_path: Path):
        """A user who edited the file keeps their version.

        It runs before every build, so overwriting would undo an edit
        silently and repeatedly.
        """
        marker = tmp_path / BUILD_ROOT_REL / ".gitignore"
        marker.parent.mkdir(parents=True)
        marker.write_text("*\n!keep-this\n", encoding="utf-8")

        BuildLayout(tmp_path).ensure_ignored()

        assert marker.read_text(encoding="utf-8") == "*\n!keep-this\n"

    def test_an_unwritable_project_does_not_stop_a_build(self, tmp_path: Path, caplog):
        """Untidy git status beats a refused build, and the log says so."""
        (tmp_path / ".fw-context").write_text("not a directory", encoding="utf-8")

        BuildLayout(tmp_path).ensure_ignored()  # must not raise

        assert "untracked" in caplog.text


_GIT_MISSING = subprocess.run(["git", "--version"], capture_output=True, check=False).returncode != 0


@pytest.mark.skipif(_GIT_MISSING, reason="git is not available")
def test_git_really_stops_reporting_the_build_tree(tmp_path: Path):
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
    # one `?? .fw-context/` line and the test would prove nothing.  The
    # project .gitignore names no fw-context path, as in a project that was
    # initialised by hand.
    config = tmp_path / ".fw-context" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[build]\n", encoding="utf-8")
    git("add", ".fw-context/config.toml")
    git("commit", "-q", "-m", "initial")

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")

    # A build output as a builder would leave it.
    output = BuildLayout(tmp_path).out_dir("")
    output.mkdir(parents=True)
    (output / "app.elf").write_bytes(b"\x7fELF")

    before = git("status", "--porcelain")
    assert ".fw-context/build/" in before, before

    BuildLayout(tmp_path).ensure_ignored()

    after = git("status", "--porcelain")
    assert ".fw-context" not in after, after
    assert "main.c" in after, "an unrelated file must stay visible"
    assert git("ls-files", ".fw-context/").strip() == ".fw-context/config.toml", (
        "the committed config must stay tracked"
    )


@pytest.mark.skipif(_GIT_MISSING, reason="git is not available")
def test_init_covers_the_build_tree_for_a_new_project(tmp_path: Path):
    """A new project ignores the build output through its own .gitignore.

    The rule inside the build tree covers every project; the entry that
    ``init`` writes is what covers a project whose build tree does not exist
    yet.

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

    assert ignored(f"{BUILD_ROOT_REL}/default/out/app.elf")
    assert ignored(f"{BUILD_ROOT_REL}/default/out/compile_commands.json")
    assert not ignored(".fw-context/config.toml"), "the shared config must stay committable"
