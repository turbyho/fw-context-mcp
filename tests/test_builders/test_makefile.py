"""Tests for MakefileBuildSystem — compile_commands.json from the log of ``make -Bnkwp``.

A real make gets the compiler of a toolchain of the test SDKs in ``CC``,
as the Makefile of a firmware sets it.  The Makefiles compile and do not
link: a firmware links with the linker script of its board, and the
database holds only the compile commands.
"""

from pathlib import Path

import pytest

from fw_context_mcp.indexer.build import BuildConfig
from fw_context_mcp.indexer.builders.makefile import MakefileBuildSystem
from tests._toolchains import Toolchain, write_sources

_HELLO = {"hello.c": "int main(void) { return 0; }\n"}


def _cc(toolchain: Toolchain) -> str:
    """The value of the make variable CC: the C driver of *toolchain* and the flags of its board."""
    return " ".join([str(toolchain.driver("hello.c")), *toolchain.flags])


class TestMakefileBuildSystem:
    def test_detected(self, tmp_path):
        (tmp_path / "Makefile").write_text("all:\n\t@echo ok\n")
        assert MakefileBuildSystem.detect(tmp_path) is True

    def test_not_detected(self, tmp_path):
        assert MakefileBuildSystem.detect(tmp_path) is False

    @pytest.mark.make
    def test_the_log_of_make_gives_the_database(self, tmp_path, toolchain):
        """Integration test: the log of ``make -Bnkwp`` of a trivial Makefile project."""
        import json

        # Create a trivial C project with a Makefile
        write_sources(tmp_path, {**_HELLO, "Makefile": (
            "all: hello.o\n\n"
            "hello.o: hello.c\n"
            "\t$(CC) -c hello.c -o hello.o\n"
            "clean:\n\trm -f hello.o\n"
        )})

        cfg = BuildConfig(
            system="makefile",
            make_target="all",
            make_vars={"CC": _cc(toolchain)},
        )

        builder = MakefileBuildSystem()
        cc_path = builder.generate(tmp_path, cfg)

        assert cc_path.exists()
        data = json.loads(cc_path.read_text(encoding="utf-8"))
        assert len(data) >= 1
        # Should contain entry for hello.c
        files = [e.get("file", "") for e in data]
        assert any("hello.c" in f for f in files)
        # The compiler of the database is the one of CC, not the cc of the host.
        commands = [" ".join(e.get("arguments", [])) or e.get("command", "") for e in data]
        assert all(f"{toolchain.prefix}gcc" in command for command in commands), commands

    @pytest.mark.make
    def test_build_passes_make_vars(self, tmp_path, toolchain):
        """Verify that make_vars reach make."""
        import json

        write_sources(tmp_path, {**_HELLO, "Makefile": (
            "all: hello.o\n\n"
            "hello.o: hello.c\n"
            "\t$(CC) $(CFLAGS) -c hello.c -o hello.o\n"
        )})

        cfg = BuildConfig(
            system="makefile",
            make_target="all",
            make_vars={"CC": _cc(toolchain), "CFLAGS": "-DFOO=1"},
        )

        builder = MakefileBuildSystem()
        cc_path = builder.generate(tmp_path, cfg)

        data = json.loads(cc_path.read_text(encoding="utf-8"))
        assert [entry["file"] for entry in data] == ["hello.c"]
        assert "-DFOO=1" in data[0]["arguments"]

    @pytest.mark.make
    def test_a_dry_run_compiles_nothing(self, tmp_path, toolchain):
        """make_dry_run=True: only the log of make, no object file."""
        import json

        write_sources(tmp_path, {**_HELLO, "Makefile": (
            "all: hello.o\n\n"
            "hello.o: hello.c\n"
            "\t$(CC) -c hello.c -o hello.o\n"
        )})

        cfg = BuildConfig(
            system="makefile",
            make_target="all",
            make_dry_run=True,
            make_vars={"CC": _cc(toolchain)},
        )

        builder = MakefileBuildSystem()
        cc_path = builder.generate(tmp_path, cfg)

        assert cc_path.exists()
        assert not (tmp_path / "hello.o").exists(), "a dry run compiles nothing"
        data = json.loads(cc_path.read_text(encoding="utf-8"))
        assert len(data) >= 1

    @pytest.mark.make
    def test_the_shell_of_make_reads_each_command(self, tmp_path, toolchain):
        """The shell of make on the host: sh on POSIX, cmd.exe on Windows without sh.exe.

        Each recipe line here means the same in the two shells: a text of
        $(info) with a quote, a redirection, a cd, and a sub-make.
        """
        import json

        write_sources(tmp_path, {
            **_HELLO,
            "sub/sub.c": "int sub(void) { return 1; }\n",
            "lib/lib.c": "int lib(void) { return 2; }\n",
            "lib/Makefile": "all:\n\t$(CC) -DLIB -c lib.c -o lib.o\n",
            "Makefile": (
                "all:\n"
                "\t$(info Don't panic: this text is no command)\n"
                "\t$(CC) -DROOT -c hello.c -o hello.o 2>&1\n"
                "\tcd sub && $(CC) -DSUB -c sub.c -o sub.o\n"
                "\t$(MAKE) -C lib\n"
            ),
        })

        cc_path = MakefileBuildSystem().generate(tmp_path, BuildConfig(make_vars={"CC": _cc(toolchain)}))

        data = json.loads(cc_path.read_text(encoding="utf-8"))
        found = {(Path(entry["directory"]).resolve(), entry["file"]): entry["arguments"] for entry in data}
        root = tmp_path.resolve()
        assert set(found) == {(root, "hello.c"), (root / "sub", "sub.c"), (root / "lib", "lib.c")}
        assert "2>&1" not in found[(root, "hello.c")], "a redirection is no argument of the compiler"
        assert "-DSUB" in found[(root / "sub", "sub.c")]
        assert "-DLIB" in found[(root / "lib", "lib.c")]

    @pytest.mark.make
    def test_a_make_that_restarts_after_it_remakes_an_included_makefile(self, tmp_path, toolchain):
        """``include`` of dependency files with a rule for them (the manual of GNU make, "Generating Prerequisites").

        make remakes hello.d, reads its makefiles again and prints a second
        data base: the compiles of the next pass are in the database.
        """
        import json

        write_sources(tmp_path, {
            **_HELLO,
            "Makefile": (
                "SRCS := hello.c\n"
                "all: $(SRCS:.c=.o)\n"
                "%.o: %.c\n"
                "\t$(CC) -DGOAL -c $< -o $@\n"
                "%.d: %.c\n"
                "\t$(CC) -MM $< > $@\n"
                "include $(SRCS:.c=.d)\n"
            ),
        })

        cc_path = MakefileBuildSystem().generate(tmp_path, BuildConfig(make_vars={"CC": _cc(toolchain)}))

        data = json.loads(cc_path.read_text(encoding="utf-8"))
        assert [entry["file"] for entry in data] == ["hello.c"]
        assert "-DGOAL" in data[0]["arguments"], "the entry is the compile of the goal, not the -MM of hello.d"

    @pytest.mark.make
    def test_a_variable_that_is_not_utf8(self, tmp_path, toolchain):
        """A Makefile in Latin-2: the data base prints the value as it is, and the compiles do not use it."""
        import json

        write_sources(tmp_path, _HELLO)
        (tmp_path / "Makefile").write_bytes(b"NAME := Kol\xe8\nall:\n\t$(CC) -c hello.c -o hello.o\n")

        cc_path = MakefileBuildSystem().generate(tmp_path, BuildConfig(make_vars={"CC": _cc(toolchain)}))

        assert [entry["file"] for entry in json.loads(cc_path.read_text(encoding="utf-8"))] == ["hello.c"]

    @pytest.mark.make
    def test_a_make_that_fails_gives_no_database(self, tmp_path):
        """make -k goes on after an error and exits with 2: the log then holds a part of the build."""
        write_sources(tmp_path, {**_HELLO, "Makefile": "all: missing.c hello.o\nhello.o: hello.c\n\tgcc -c hello.c\n"})

        with pytest.raises(RuntimeError) as error:
            MakefileBuildSystem().generate(tmp_path, BuildConfig())

        message = str(error.value)
        assert "missing.c" in message, "the message gives the error of make"
        assert "writes no compilation database" in message
        assert "Make data base" not in message, "the data base holds the environment, and it stays out"
        assert not (tmp_path / ".fw-context").exists() or not list((tmp_path / ".fw-context").rglob("compile_commands.json"))

    def test_required_tools(self):
        assert MakefileBuildSystem().required_tools() == ["make"]

    def test_detect_environment_returns_defaults(self, tmp_path):
        """detect_environment returns safe defaults."""
        result = MakefileBuildSystem.detect_environment(tmp_path)
        assert result == {"python": None, "activate": None}


@pytest.mark.make
class TestTheOutputDirectoryOfARealMake:
    """A real make builds into the output directory of fw-context, through out_dir_var.

    A Makefile names its output directory in a variable of its own, thus the
    config names that variable.  Without it a real make would write into the
    tree of the build of the user.
    """

    _MAKEFILE = (
        "BUILD_DIR ?= build\n"
        "all: $(BUILD_DIR)/hello.o\n\n"
        "$(BUILD_DIR)/hello.o: hello.c\n"
        "\t$(CC) -c hello.c -o $(BUILD_DIR)/hello.o\n"
    )

    def test_a_real_make_writes_into_out(self, tmp_path, toolchain):
        import json

        from fw_context_mcp.indexer.build_layout import BuildLayout

        write_sources(tmp_path, {**_HELLO, "Makefile": self._MAKEFILE})
        cfg = BuildConfig(
            system="makefile", make_dry_run=False, out_dir_var="BUILD_DIR", make_vars={"CC": _cc(toolchain)},
        )

        cc_path = MakefileBuildSystem().generate(tmp_path, cfg)

        out = BuildLayout(tmp_path.resolve()).out_dir("")
        assert (out / "hello.o").is_file()
        assert not (tmp_path / "build").exists(), "the directory of the build of the user stays untouched"
        data = json.loads(cc_path.read_text(encoding="utf-8"))
        assert any(str(out) in " ".join(entry.get("arguments", [])) or str(out) in entry.get("command", "")
                   for entry in data)

    def test_a_real_make_without_the_variable_is_refused(self, tmp_path):

        (tmp_path / "Makefile").write_text(self._MAKEFILE, encoding="utf-8")

        with pytest.raises(RuntimeError, match="out_dir_var"):
            MakefileBuildSystem().generate(tmp_path, BuildConfig(system="makefile", make_dry_run=False))

    def test_the_variable_in_make_vars_is_refused(self, tmp_path):

        cfg = BuildConfig(system="makefile", out_dir_var="BUILD_DIR", make_vars={"BUILD_DIR": "x"})

        with pytest.raises(RuntimeError, match="make_vars"):
            MakefileBuildSystem().generate(tmp_path, cfg)

    def test_a_real_make_with_the_variable_may_build_in_the_background(self):
        from fw_context_mcp.indexer.builders import background_build_safe

        cfg = BuildConfig(make_dry_run=False, out_dir_var="BUILD_DIR")

        assert background_build_safe(MakefileBuildSystem(), cfg) is True

    def test_a_path_with_a_space_is_refused_before_make_runs(self, tmp_path):
        """make splits at whitespace; measured: the build made directories outside the project."""
        root = tmp_path / "my proj"
        root.mkdir()
        (root / "Makefile").write_text(self._MAKEFILE, encoding="utf-8")

        with pytest.raises(RuntimeError, match="space"):
            MakefileBuildSystem().generate(root, BuildConfig(make_dry_run=False, out_dir_var="BUILD_DIR"))
        assert not (tmp_path / "my").exists()

    def test_a_makefile_that_ignores_the_variable_is_reported(self, tmp_path, toolchain):
        """`override` keeps the build in the directory of the user; the -o of the log shows it."""
        write_sources(tmp_path, {
            **_HELLO, "Makefile": "override BUILD_DIR := build\n" + self._MAKEFILE.replace("BUILD_DIR ?= build\n", ""),
        })
        # The build directory of the user, as a project has it: the recipe
        # makes no directory (no POSIX shell on Windows).
        (tmp_path / "build").mkdir()
        cfg = BuildConfig(make_dry_run=False, out_dir_var="BUILD_DIR", make_vars={"CC": _cc(toolchain)})

        with pytest.raises(RuntimeError, match="override"):
            MakefileBuildSystem().generate(tmp_path, cfg)
        assert list((tmp_path / "build").iterdir()) == [], "the check runs before the build"

    @pytest.mark.parametrize("name", ["BUILD DIR", "A=B", "X:Y", "#X"])
    def test_a_name_that_is_no_make_variable_is_refused(self, tmp_path, name):
        with pytest.raises(RuntimeError, match="not the name of a make variable"):
            MakefileBuildSystem().generate(tmp_path, BuildConfig(out_dir_var=name))

    def test_a_dry_run_passes_the_variable_too(self, tmp_path, toolchain):
        """The -o paths of the database then name out/, as a real build would write them."""
        import json

        from fw_context_mcp.indexer.build_layout import BuildLayout

        write_sources(tmp_path, {**_HELLO, "Makefile": self._MAKEFILE})
        cfg = BuildConfig(out_dir_var="BUILD_DIR", variant_name="dev", make_vars={"CC": _cc(toolchain)})

        cc_path = MakefileBuildSystem().generate(tmp_path, cfg)

        out = BuildLayout(tmp_path.resolve()).out_dir("dev")
        entries = json.loads(cc_path.read_text(encoding="utf-8"))
        assert any(str(out) in " ".join(e.get("arguments", [])) or str(out) in e.get("command", "") for e in entries)
        assert not (out / "hello.o").exists(), "a dry run compiles nothing"
