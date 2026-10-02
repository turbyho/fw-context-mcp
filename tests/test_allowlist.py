"""`fw-context init`, `doctor` and each index run add the toolchains of a project to the allowlist.

The GCC driver query runs only a compiler that matches ``[index]
query_driver``.  A toolchain in a directory of its own was outside the
default list, and its units kept the guessed headers without any visible
sign: the user did not know that the list exists.  The missing compilers
now go into ``.fw-context/toolchains.toml`` with no flag, under
``query_driver_extra``, which the load adds to ``query_driver``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import sys
from pathlib import Path

import pytest

from fw_context_mcp.indexer._allowlist import allow_project_toolchains, missing_toolchain_globs

posix_only = pytest.mark.skipif(os.name == "nt", reason="the owner and mode check is POSIX only")


def _tool(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 1\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """An initialized project, a fake home and an empty global config."""
    import fw_context_mcp.config.settings as settings

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text("")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_cfg)
    root = tmp_path / "proj"
    (root / ".fw-context" / "build").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text("[index]\n")
    return root


def _toolchains(project: Path) -> Path:
    return project / ".fw-context" / "toolchains.toml"


def _cc(project: Path, compilers: list[str]) -> Path:
    cc = project / ".fw-context" / "build" / "compile_commands.json"
    cc.write_text(json.dumps([
        {"directory": str(project), "file": f"src/{i}.c", "arguments": [compiler, "-c", f"src/{i}.c"]}
        for i, compiler in enumerate(compilers)
    ]))
    return cc


class TestMissingGlobs:
    def test_a_toolchain_outside_the_list_is_found(self, tmp_path, project) -> None:
        gcc = _tool(tmp_path / "tools" / "gcc-arm-9" / "bin" / "arm-none-eabi-gcc")
        cc = _cc(project, [str(gcc)])
        assert missing_toolchain_globs([cc], ["/opt/**"], project) == [gcc.as_posix()]

    def test_an_allowed_toolchain_is_not_added(self, tmp_path, project) -> None:
        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        cc = _cc(project, [str(gcc)])
        assert missing_toolchain_globs([cc], [f"{tmp_path.as_posix()}/tools/**"], project) == []

    def test_a_compiler_inside_the_project_is_never_added(self, project) -> None:
        """The query never runs it, and the files of a project come from its repository."""
        gcc = _tool(project / "tools" / "bin" / "arm-none-eabi-gcc")
        cc = _cc(project, [str(gcc)])
        assert missing_toolchain_globs([cc], [], project) == []

    def test_a_wrapper_and_a_missing_compiler_are_not_added(self, tmp_path, project, monkeypatch) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        ccache = _tool(tmp_path / "wrap" / "ccache")
        cc = _cc(project, [str(ccache), "/nowhere/bin/arm-none-eabi-gcc", "no-such-elf-gcc"])
        assert missing_toolchain_globs([cc], [], project) == []

    def test_a_toolchain_under_home_is_written_with_a_tilde(self, project) -> None:
        """The file belongs to one developer; ~ keeps it readable."""
        gcc = _tool(Path.home() / "dev_tools" / "gcc-arm" / "bin" / "arm-none-eabi-g++")
        cc = _cc(project, [str(gcc)])
        assert missing_toolchain_globs([cc], [], project) == ["~/dev_tools/gcc-arm/bin/arm-none-eabi-g++"]

    def test_a_command_field_gives_the_compiler(self, tmp_path, project) -> None:
        gcc = _tool(tmp_path / "my tools" / "bin" / "arm-none-eabi-gcc")
        cc = project / ".fw-context" / "build" / "compile_commands.json"
        cc.write_text(json.dumps([
            {"directory": str(project), "file": "a.c", "command": f"{shlex.quote(str(gcc))} -c a.c -DX=1"},
        ]))
        assert missing_toolchain_globs([cc], [], project) == [gcc.as_posix()]

    def test_a_glob_character_in_the_directory_is_refused(self, tmp_path, project, caplog) -> None:
        """The glob would read ``*`` as a wildcard and allow more than the one directory."""
        gcc = _tool(tmp_path / "tools*" / "bin" / "arm-none-eabi-gcc")
        cc = _cc(project, [str(gcc)])
        assert missing_toolchain_globs([cc], [], project) == []
        assert any("glob character" in r.message for r in caplog.records)

    @posix_only
    def test_a_directory_that_all_users_can_write_is_refused(self, tmp_path, project, caplog) -> None:
        gcc = _tool(tmp_path / "shared" / "bin" / "arm-none-eabi-gcc")
        gcc.parent.chmod(0o777)
        cc = _cc(project, [str(gcc)])
        assert missing_toolchain_globs([cc], [], project) == []
        assert any("all users can write" in r.message for r in caplog.records)

    @posix_only
    def test_a_sticky_bin_directory_is_refused_too(self, tmp_path, project) -> None:
        """In the bin directory itself, any user can add a new compiler that the glob allows."""
        gcc = _tool(tmp_path / "shared" / "bin" / "arm-none-eabi-gcc")
        gcc.parent.chmod(0o1777)
        cc = _cc(project, [str(gcc)])
        assert missing_toolchain_globs([cc], [], project) == []

    @posix_only
    def test_a_sticky_directory_above_is_accepted(self, tmp_path, project) -> None:
        """As /tmp: only the owner can replace an entry of a sticky directory."""
        sticky = tmp_path / "sticky"
        gcc = _tool(sticky / "mine" / "bin" / "arm-none-eabi-gcc")
        sticky.chmod(0o1777)
        cc = _cc(project, [str(gcc)])
        assert missing_toolchain_globs([cc], [], project) == [gcc.as_posix()]

    @posix_only
    def test_a_compiler_that_all_users_can_write_is_refused(self, tmp_path, project) -> None:
        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        gcc.chmod(0o777)
        cc = _cc(project, [str(gcc)])
        assert missing_toolchain_globs([cc], [], project) == []

    @posix_only
    def test_a_compiler_of_another_user_is_refused(self, tmp_path, project, monkeypatch, caplog) -> None:
        """The files of the test belong to the real user; the code sees another uid."""
        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        cc = _cc(project, [str(gcc)])
        monkeypatch.setattr(os, "getuid", lambda: os.stat(gcc).st_uid + 1)
        assert missing_toolchain_globs([cc], [], project) == []
        assert any("belongs to another user" in r.message for r in caplog.records)

    @posix_only
    def test_a_directory_that_the_group_can_write_is_refused(self, tmp_path, project, caplog) -> None:
        """Even the primary group of the user: on macOS it is ``staff``, the group of all users."""
        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        gcc.parent.chmod(0o775)
        cc = _cc(project, [str(gcc)])
        assert missing_toolchain_globs([cc], [], project) == []
        assert any("can write to" in r.message for r in caplog.records)

    def test_only_the_named_compiler_is_allowed_not_its_directory(self, tmp_path, project) -> None:
        """A sibling had no check: a glob for the directory would allow it, and a file added later."""
        from fw_context_mcp.config import load
        from fw_context_mcp.indexer._driver_query import driver_allowed

        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        sibling = _tool(gcc.parent / "arm-none-eabi-g++")
        _cc(project, [str(gcc)])
        allow_project_toolchains(project)
        patterns = load(project).index.query_driver
        assert driver_allowed(gcc, patterns, project)
        assert not driver_allowed(sibling, patterns, project)

    @posix_only
    def test_a_link_to_a_compiler_that_all_users_can_write_is_refused(self, tmp_path, project) -> None:
        real = _tool(tmp_path / "shared" / "bin" / "arm-none-eabi-gcc")
        real.parent.chmod(0o777)
        link = tmp_path / "tools" / "bin" / "arm-none-eabi-gcc"
        link.parent.mkdir(parents=True)
        link.symlink_to(real)
        cc = _cc(project, [str(link)])
        assert missing_toolchain_globs([cc], [], project) == []

    @posix_only
    def test_a_middle_link_in_a_directory_that_all_users_can_write_is_refused(self, tmp_path, project) -> None:
        """In ``a -> b -> c`` the location of ``b`` is in neither the path nor the real path."""
        real = _tool(tmp_path / "safe" / "bin" / "arm-none-eabi-gcc")
        middle = tmp_path / "open" / "gcc-link"
        middle.parent.mkdir()
        middle.symlink_to(real)
        middle.parent.chmod(0o777)
        first = tmp_path / "tools" / "bin" / "arm-none-eabi-gcc"
        first.parent.mkdir(parents=True)
        first.symlink_to(middle)
        cc = _cc(project, [str(first)])
        assert missing_toolchain_globs([cc], [], project) == []
        middle.parent.chmod(0o755)
        assert missing_toolchain_globs([cc], [], project) == [first.as_posix()]

    def test_a_compiler_inside_the_git_repository_of_the_project_is_refused(self, tmp_path, monkeypatch, caplog) -> None:
        """A nested project: the repository can commit a script beside the project directory."""
        import fw_context_mcp.config.settings as settings

        global_cfg = tmp_path / "global.toml"
        global_cfg.write_text("")
        monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_cfg)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        nested = repo / "fw"
        (nested / ".fw-context" / "build").mkdir(parents=True)
        gcc = _tool(repo / "tools" / "bin" / "arm-none-eabi-gcc")
        cc = _cc(nested, [str(gcc)])
        assert missing_toolchain_globs([cc], [], nested) == []
        assert any("inside the git repository" in r.message for r in caplog.records)

    def test_a_compiler_in_the_superproject_of_a_submodule_is_refused(self, tmp_path, monkeypatch) -> None:
        """The submodule has a ``.git`` of its own; the superproject above it counts too."""
        import fw_context_mcp.config.settings as settings

        global_cfg = tmp_path / "global.toml"
        global_cfg.write_text("")
        monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_cfg)
        superproject = tmp_path / "super"
        (superproject / ".git").mkdir(parents=True)
        module = superproject / "modules" / "fw"
        (module / ".fw-context" / "build").mkdir(parents=True)
        (module / ".git").write_text("gitdir: ../../.git/modules/fw\n")
        gcc = _tool(superproject / "tools" / "bin" / "arm-none-eabi-gcc")
        cc = _cc(module, [str(gcc)])
        assert missing_toolchain_globs([cc], [], module) == []

    def test_a_compiler_in_the_main_work_tree_of_a_worktree_is_refused(self, tmp_path, monkeypatch) -> None:
        import fw_context_mcp.config.settings as settings

        global_cfg = tmp_path / "global.toml"
        global_cfg.write_text("")
        monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_cfg)
        main = tmp_path / "main"
        gitdir = main / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n")
        worktree = tmp_path / "wt"
        (worktree / ".fw-context" / "build").mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
        gcc = _tool(main / "tools" / "bin" / "arm-none-eabi-gcc")
        cc = _cc(worktree, [str(gcc)])
        assert missing_toolchain_globs([cc], [], worktree) == []

    def test_a_compiler_in_another_worktree_of_the_same_repository_is_refused(self, tmp_path) -> None:
        from fw_context_mcp.indexer._allowlist import repository_roots

        main = tmp_path / "main"
        (main / ".git").mkdir(parents=True)
        trees = {}
        for name in ("a", "b"):
            gitdir = main / ".git" / "worktrees" / name
            gitdir.mkdir(parents=True)
            (gitdir / "commondir").write_text("../..\n")
            tree = tmp_path / name
            tree.mkdir()
            (tree / ".git").write_text(f"gitdir: {gitdir}\n")
            (gitdir / "gitdir").write_text(f"{tree / '.git'}\n")
            trees[name] = tree
        assert set(repository_roots(trees["a"])) == {trees["a"], main, trees["b"]}

    def test_a_relative_worktree_pointer_is_relative_to_its_entry(self, tmp_path) -> None:
        """``git worktree add --relative-paths`` (git 2.48): not relative to the current directory."""
        from fw_context_mcp.indexer._allowlist import repository_roots

        main = tmp_path / "main"
        for name in ("a", "b"):
            gitdir = main / ".git" / "worktrees" / name
            gitdir.mkdir(parents=True)
            (gitdir / "commondir").write_text("../..\n")
            (gitdir / "gitdir").write_text(f"../../../../{name}/.git\n")
            (tmp_path / name).mkdir()
            (tmp_path / name / ".git").write_text(f"gitdir: ../main/.git/worktrees/{name}\n")
        assert set(repository_roots(tmp_path / "a")) == {tmp_path / "a", main, tmp_path / "b"}

    def test_one_unreadable_worktree_does_not_hide_the_others(self, tmp_path) -> None:
        from fw_context_mcp.indexer._allowlist import repository_roots

        main = tmp_path / "main"
        (main / ".git" / "worktrees" / "bad" / "gitdir").mkdir(parents=True)  # a directory: the read fails
        good = main / ".git" / "worktrees" / "good"
        good.mkdir()
        (good / "gitdir").write_text(f"{tmp_path / 'good' / '.git'}\n")
        assert set(repository_roots(main)) == {main, tmp_path / "good"}

    def test_the_work_tree_of_a_separate_git_dir_is_found(self, tmp_path) -> None:
        """A submodule worktree or ``--separate-git-dir``: only core.worktree names the main tree."""
        from fw_context_mcp.indexer._allowlist import repository_roots

        common = tmp_path / "store" / "fw.git"
        common.mkdir(parents=True)
        (common / "config").write_text('[core]\n\tworktree = ../../src/fw\n[remote "origin"]\n\turl = x\n')
        gitdir = common / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
        (gitdir / "gitdir").write_text(f"{worktree / '.git'}\n")
        assert set(repository_roots(worktree)) == {worktree, tmp_path / "src" / "fw"}

    def test_a_real_git_worktree_is_read(self, tmp_path) -> None:
        """The layout that git itself writes, not one that the test invents."""
        import shutil
        import subprocess

        git = shutil.which("git")
        if git is None:
            pytest.skip("git is not installed")
        main, other = tmp_path / "main", tmp_path / "other"
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        run = {"check": True, "capture_output": True, "env": env}
        subprocess.run([git, "init", "-q", str(main)], **run)
        subprocess.run([git, "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "x"], **run)
        subprocess.run([git, "-C", str(main), "worktree", "add", "-q", str(other)], **run)
        from fw_context_mcp.indexer._allowlist import repository_roots

        resolved = {Path(os.path.realpath(p)) for p in repository_roots(other)}
        assert {Path(os.path.realpath(main)), Path(os.path.realpath(other))} <= resolved

    def test_one_unreadable_entry_does_not_hide_the_others(self, tmp_path, project) -> None:
        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        cc = project / ".fw-context" / "build" / "compile_commands.json"
        cc.write_text(json.dumps([
            {"directory": str(project), "file": "a.c", "command": "'broken -c a.c"},
            {"directory": str(project), "file": "b.c", "arguments": [str(gcc), "-c", "b.c"]},
        ]))
        assert missing_toolchain_globs([cc], [], project) == [gcc.as_posix()]


class TestWindowsRule:
    """fw-context reads no ACL; only the home directory and Program Files are trusted."""

    def test_the_home_directory_and_program_files_are_accepted(self, tmp_path, project, monkeypatch) -> None:
        from fw_context_mcp.indexer._allowlist import _windows_refusal

        program_files = tmp_path / "Program Files"
        monkeypatch.setenv("ProgramFiles", str(program_files))
        assert _windows_refusal(Path.home() / "tools" / "gcc.exe") is None
        assert _windows_refusal(program_files / "Arm" / "bin" / "gcc.exe") is None

    @pytest.mark.skipif(sys.platform == "win32", reason="a link needs a privilege on Windows")
    def test_a_link_from_outside_into_program_files_is_refused(self, tmp_path, project, monkeypatch) -> None:
        """The build runs the link; other users can replace it where it is."""
        from fw_context_mcp.indexer._allowlist import _windows_refusal

        program_files = tmp_path / "Program Files"
        real = _tool(program_files / "Arm" / "bin" / "gcc.exe")
        monkeypatch.setenv("ProgramFiles", str(program_files))
        link = tmp_path / "tools" / "gcc.exe"
        link.parent.mkdir()
        link.symlink_to(real)
        assert _windows_refusal(link) is not None

    def test_another_directory_is_refused(self, tmp_path, project, monkeypatch) -> None:
        from fw_context_mcp.indexer._allowlist import _windows_refusal

        for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            monkeypatch.delenv(name, raising=False)
        assert "does not read ACLs" in (_windows_refusal(tmp_path / "Public" / "gcc.exe") or "")


class TestCompilerToken:
    """The regex path gives the word that ``shlex`` gives, without the split of the whole line."""

    @pytest.mark.parametrize("command", [
        "gcc -c a.c",
        "  /opt/x/bin/arm-none-eabi-gcc -DX=1 -c a.c",
        "'/opt/my tools/gcc' -c a.c",
        '"/opt/my tools/gcc" -c a.c',
        '"" -c',
        "gcc",
        "gcc\t-c",
        '"/opt/a"b -c',  # joined quote: shlex
        r"/opt/my\ tools/gcc -c",  # escape: shlex
        "a'b c' -c",
    ])
    def test_the_same_word_as_shlex(self, command) -> None:
        from fw_context_mcp.indexer._driver_query import compiler_token

        assert compiler_token({"command": command}) == shlex.split(command)[0]

    def test_an_unclosed_quote_raises_value_error(self) -> None:
        from fw_context_mcp.indexer._driver_query import compiler_token

        with pytest.raises(ValueError):
            compiler_token({"command": "'gcc -c a.c"})


class TestWrite:
    def test_the_glob_lands_in_toolchains_toml_and_the_default_stays(self, tmp_path, project) -> None:
        from fw_context_mcp.config import load
        from fw_context_mcp.config.settings import DEFAULT_QUERY_DRIVER

        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        _cc(project, [str(gcc)])

        added = allow_project_toolchains(project)

        glob = gcc.as_posix()
        assert added == [glob]
        assert load(project).index.query_driver == [*DEFAULT_QUERY_DRIVER, glob]
        text = _toolchains(project).read_text()
        assert text.startswith("# Written by fw-context")
        # Only the added glob: a copy of the default would freeze it.
        assert DEFAULT_QUERY_DRIVER[0] not in text

    def test_local_toml_is_not_touched(self, tmp_path, project) -> None:
        """A developer's comments, and even a syntax error, stay as they are."""
        local = project / ".fw-context" / "local.toml"
        local.write_text("# my notes\n[index\nbroken = \n")
        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        _cc(project, [str(gcc)])

        assert allow_project_toolchains(project) != []
        assert local.read_text() == "# my notes\n[index\nbroken = \n"

    def test_a_query_driver_of_local_toml_still_replaces_the_default(self, tmp_path, project) -> None:
        from fw_context_mcp.config import load

        (project / ".fw-context" / "local.toml").write_text('[index]\nquery_driver = ["/only/*"]\n')
        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        _cc(project, [str(gcc)])
        allow_project_toolchains(project)
        assert load(project).index.query_driver == ["/only/*", gcc.as_posix()]

    def test_a_second_run_changes_nothing(self, tmp_path, project) -> None:
        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        _cc(project, [str(gcc)])
        allow_project_toolchains(project)
        before = _toolchains(project).stat().st_mtime_ns
        assert allow_project_toolchains(project) == []
        assert _toolchains(project).stat().st_mtime_ns == before

    def test_the_globs_already_in_the_file_stay(self, tmp_path, project) -> None:
        from fw_context_mcp.config import load

        _toolchains(project).write_text('[index]\nquery_driver_extra = ["/old/bin/*"]\n')
        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        _cc(project, [str(gcc)])
        allow_project_toolchains(project)
        assert load(project).index.query_driver[-2:] == ["/old/bin/*", gcc.as_posix()]

    def test_the_added_compiler_is_then_queried(self, tmp_path, project) -> None:
        """End to end: after the write, the driver query may run the compiler."""
        from fw_context_mcp.config import load
        from fw_context_mcp.indexer._driver_query import driver_allowed

        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        _cc(project, [str(gcc)])
        assert not driver_allowed(gcc, load(project).index.query_driver, project)
        allow_project_toolchains(project)
        assert driver_allowed(gcc, load(project).index.query_driver, project)

    @pytest.mark.skipif(sys.platform == "win32", reason="a link needs a privilege on Windows")
    def test_a_linked_toolchains_toml_is_not_written_or_read(self, tmp_path, project) -> None:
        """A repository can commit the file as a link to a file of its choice."""
        from fw_context_mcp.config import load

        target = tmp_path / "elsewhere.toml"
        target.write_text('[index]\nquery_driver_extra = ["/evil/*"]\n')
        _toolchains(project).symlink_to(target)
        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        _cc(project, [str(gcc)])

        assert "/evil/*" not in load(project).index.query_driver
        with pytest.raises(OSError, match="not a regular file"):
            allow_project_toolchains(project)
        assert target.read_text() == '[index]\nquery_driver_extra = ["/evil/*"]\n'

    @pytest.mark.skipif(sys.platform == "win32", reason="a link needs a privilege on Windows")
    def test_a_linked_config_directory_is_not_written(self, tmp_path, project) -> None:
        import shutil

        outside = tmp_path / "outside"
        shutil.move(project / ".fw-context", outside)
        (project / ".fw-context").symlink_to(outside)
        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        _cc(project, [str(gcc)])

        with pytest.raises(OSError, match="not a directory of the project"):
            allow_project_toolchains(project)
        assert not (outside / "toolchains.toml").exists()


class TestLoad:
    def test_a_committed_query_driver_extra_is_ignored(self, project) -> None:
        from fw_context_mcp.config import load

        (project / ".fw-context" / "config.toml").write_text('[index]\nquery_driver_extra = ["/repo/*"]\n')
        assert "/repo/*" not in load(project).index.query_driver

    def test_a_committed_query_driver_auto_cannot_cancel_a_global_false(self, project) -> None:
        import fw_context_mcp.config.settings as settings
        from fw_context_mcp.config import load

        settings._GLOBAL_CONFIG_PATH.write_text("[index]\nquery_driver_auto = false\n")
        (project / ".fw-context" / "config.toml").write_text("[index]\nquery_driver_auto = true\n")
        _toolchains(project).write_text('[index]\nquery_driver_extra = ["/evil/gcc"]\n')
        cfg = load(project).index
        assert cfg.query_driver_auto is False
        assert "/evil/gcc" not in cfg.query_driver

    def test_query_driver_extra_of_local_toml_is_added(self, project) -> None:
        from fw_context_mcp.config import load
        from fw_context_mcp.config.settings import DEFAULT_QUERY_DRIVER

        (project / ".fw-context" / "local.toml").write_text('[index]\nquery_driver_extra = ["/mine/*"]\n')
        assert load(project).index.query_driver == [*DEFAULT_QUERY_DRIVER, "/mine/*"]

    def test_the_cache_sees_the_file_appear_and_go(self, project) -> None:
        from fw_context_mcp.config import load

        assert "/new/*" not in load(project).index.query_driver
        _toolchains(project).write_text('[index]\nquery_driver_extra = ["/new/*"]\n')
        assert "/new/*" in load(project).index.query_driver
        _toolchains(project).unlink()
        assert "/new/*" not in load(project).index.query_driver

    def test_the_global_and_the_local_extra_lists_add(self, project) -> None:
        import fw_context_mcp.config.settings as settings
        from fw_context_mcp.config import load
        from fw_context_mcp.config.settings import DEFAULT_QUERY_DRIVER

        settings._GLOBAL_CONFIG_PATH.write_text('[index]\nquery_driver_extra = ["/global/*"]\n')
        (project / ".fw-context" / "local.toml").write_text('[index]\nquery_driver_extra = ["/mine/*"]\n')
        _toolchains(project).write_text('[index]\nquery_driver_extra = ["/auto/gcc"]\n')
        cfg = load(project).index
        assert cfg.query_driver == [*DEFAULT_QUERY_DRIVER, "/global/*", "/mine/*", "/auto/gcc"]
        assert cfg.query_driver_extra == ["/global/*", "/mine/*"]

    def test_query_driver_auto_false_stops_the_file_and_the_write(self, tmp_path, project) -> None:
        """``query_driver = []`` must stay empty when the developer says so."""
        from fw_context_mcp.config import load

        (project / ".fw-context" / "local.toml").write_text(
            "[index]\nquery_driver = []\nquery_driver_auto = false\n",
        )
        _toolchains(project).write_text('[index]\nquery_driver_extra = ["/auto/gcc"]\n')
        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        _cc(project, [str(gcc)])
        assert allow_project_toolchains(project) == []
        assert load(project).index.query_driver == []
        assert "/auto/gcc" in _toolchains(project).read_text()

    def test_a_broken_file_gives_no_glob(self, project, caplog) -> None:
        from fw_context_mcp.config import load
        from fw_context_mcp.config.settings import DEFAULT_QUERY_DRIVER

        _toolchains(project).write_text("[index\n")
        assert load(project).index.query_driver == list(DEFAULT_QUERY_DRIVER)
        assert any("Cannot read" in r.message for r in caplog.records)


class TestIndexRun:
    """Each index run adds a toolchain that its build just named, before it parses."""

    def test_the_compilation_database_of_the_run_is_read(self, tmp_path, project) -> None:
        from fw_context_mcp.config import load
        from fw_context_mcp.indexer._allowlist import ensure_project_toolchains

        gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
        custom = tmp_path / "elsewhere" / "cc.json"
        custom.parent.mkdir()
        custom.write_text(json.dumps([{"directory": str(project), "file": "a.c", "arguments": [str(gcc), "-c", "a.c"]}]))

        ensure_project_toolchains(project, custom)

        assert gcc.as_posix() in load(project).index.query_driver

    def test_an_uninitialized_directory_is_left_alone(self, tmp_path) -> None:
        from fw_context_mcp.indexer._allowlist import ensure_project_toolchains

        bare = tmp_path / "bare"
        bare.mkdir()
        ensure_project_toolchains(bare)
        assert not (bare / ".fw-context").exists()

    def test_a_write_error_does_not_stop_the_run(self, project, monkeypatch, caplog) -> None:
        from fw_context_mcp.indexer import _allowlist

        def read_only(*args, **kwargs):
            raise PermissionError("read-only")

        monkeypatch.setattr(_allowlist, "allow_project_toolchains", read_only)
        _allowlist.ensure_project_toolchains(project)
        assert any("Cannot add the toolchains" in r.message for r in caplog.records)

    def test_an_unclosed_quote_does_not_stop_the_run(self, project, caplog) -> None:
        from fw_context_mcp.indexer._allowlist import ensure_project_toolchains

        cc = project / ".fw-context" / "build" / "compile_commands.json"
        cc.write_text(json.dumps([{"directory": str(project), "file": "a.c", "command": "'gcc -c a.c"}]))
        ensure_project_toolchains(project)  # must not raise
        assert not any("Cannot add the toolchains" in r.message for r in caplog.records)
        assert not _toolchains(project).exists()


def test_doctor_writes_the_allowlist_without_a_flag(tmp_path, project, capsys) -> None:
    from fw_context_mcp.cli._doctor import cmd_doctor

    gcc = _tool(tmp_path / "tools" / "bin" / "arm-none-eabi-gcc")
    _cc(project, [str(gcc)])

    cmd_doctor(argparse.Namespace(project=str(project), fix=False, json=False, only="clang-resource"))

    assert "query-driver: added" in capsys.readouterr().out
    assert "query_driver_extra" in _toolchains(project).read_text()


def test_doctor_outside_a_project_writes_nothing(tmp_path, monkeypatch) -> None:
    from fw_context_mcp.cli._doctor import cmd_doctor

    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / ".git").mkdir()
    cmd_doctor(argparse.Namespace(project=str(bare), fix=False, json=False, only="clang-resource"))
    assert not (bare / ".fw-context").exists()
