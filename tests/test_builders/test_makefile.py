"""Tests for MakefileBuildSystem — compile_commands.json via compiledb."""

import pytest

from fw_context_mcp.indexer.build import BuildConfig
from fw_context_mcp.indexer.builders.makefile import MakefileBuildSystem

pytest.importorskip("compiledb", reason="compiledb not installed")


class TestMakefileBuildSystem:
    def test_detected(self, tmp_path):
        (tmp_path / "Makefile").write_text("all:\n\t@echo ok\n")
        assert MakefileBuildSystem.detect(tmp_path) is True

    def test_not_detected(self, tmp_path):
        assert MakefileBuildSystem.detect(tmp_path) is False

    def test_build_with_compiledb(self, tmp_path):
        """Integration test: runs compiledb on a trivial Makefile project."""
        import json

        # Create a trivial C project with a Makefile
        (tmp_path / "hello.c").write_text("int main(void) { return 0; }\n")
        (tmp_path / "Makefile").write_text(
            "all: hello\n\n"
            "hello: hello.c\n"
            "\t$(CC) -c hello.c -o hello.o\n"
            "\t$(CC) hello.o -o hello\n"
            "clean:\n\trm -f hello hello.o\n"
        )

        cfg = BuildConfig(
            system="makefile",
            make_target="all",
        )

        builder = MakefileBuildSystem()
        cc_path = builder.generate(tmp_path, cfg)

        assert cc_path.exists()
        data = json.loads(cc_path.read_text(encoding="utf-8"))
        assert len(data) >= 1
        # Should contain entry for hello.c
        files = [e.get("file", "") for e in data]
        assert any("hello.c" in f for f in files)

    def test_build_passes_make_vars(self, tmp_path):
        """Verify that make_vars are forwarded to compiledb."""
        import json

        (tmp_path / "hello.c").write_text("int main(void) { return 0; }\n")
        (tmp_path / "Makefile").write_text(
            "all: hello\n\n"
            "hello: hello.c\n"
            "\t$(CC) $(CFLAGS) -c hello.c -o hello.o\n"
            "\t$(CC) hello.o -o hello\n"
        )

        cfg = BuildConfig(
            system="makefile",
            make_target="all",
            make_vars={"CFLAGS": "-DFOO=1"},
        )

        builder = MakefileBuildSystem()
        cc_path = builder.generate(tmp_path, cfg)

        data = json.loads(cc_path.read_text(encoding="utf-8"))
        assert len(data) >= 1

    def test_make_dry_run_compiledb_flag(self, tmp_path):
        """make_dry_run=True should pass -n to compiledb."""
        import json

        (tmp_path / "hello.c").write_text("int main(void) { return 0; }\n")
        (tmp_path / "Makefile").write_text(
            "all: hello\n\n"
            "hello: hello.c\n"
            "\t$(CC) -c hello.c -o hello.o\n"
            "\t$(CC) hello.o -o hello\n"
        )

        cfg = BuildConfig(
            system="makefile",
            make_target="all",
            make_dry_run=True,
        )

        builder = MakefileBuildSystem()
        cc_path = builder.generate(tmp_path, cfg)

        assert cc_path.exists()
        data = json.loads(cc_path.read_text(encoding="utf-8"))
        assert len(data) >= 1

    def test_required_tools(self):
        tools = MakefileBuildSystem().required_tools()
        assert "compiledb" in tools
        assert "make" in tools

    def test_detect_environment_returns_defaults(self, tmp_path):
        """detect_environment returns safe defaults."""
        result = MakefileBuildSystem.detect_environment(tmp_path)
        assert result == {"python": None, "activate": None}


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
        "\tmkdir -p $(BUILD_DIR)\n"
        "\t$(CC) -c hello.c -o $(BUILD_DIR)/hello.o\n"
    )

    def test_a_real_make_writes_into_out(self, tmp_path):
        import json

        from fw_context_mcp.indexer.build_layout import BuildLayout

        (tmp_path / "hello.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        (tmp_path / "Makefile").write_text(self._MAKEFILE, encoding="utf-8")
        cfg = BuildConfig(system="makefile", make_dry_run=False, out_dir_var="BUILD_DIR")

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

    def test_a_makefile_that_ignores_the_variable_is_reported(self, tmp_path):
        """`override` keeps the build in the directory of the user; an empty out/ shows it."""
        (tmp_path / "hello.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        (tmp_path / "Makefile").write_text(
            "override BUILD_DIR := build\n" + self._MAKEFILE.replace("BUILD_DIR ?= build\n", ""),
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="override"):
            MakefileBuildSystem().generate(tmp_path, BuildConfig(make_dry_run=False, out_dir_var="BUILD_DIR"))

    @pytest.mark.parametrize("name", ["BUILD DIR", "A=B", "X:Y", "#X"])
    def test_a_name_that_is_no_make_variable_is_refused(self, tmp_path, name):
        with pytest.raises(RuntimeError, match="not the name of a make variable"):
            MakefileBuildSystem().generate(tmp_path, BuildConfig(out_dir_var=name))

    def test_a_dry_run_passes_the_variable_too(self, tmp_path):
        """The -o paths of the database then name out/, as a real build would write them."""
        import json

        from fw_context_mcp.indexer.build_layout import BuildLayout

        (tmp_path / "hello.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        (tmp_path / "Makefile").write_text(self._MAKEFILE, encoding="utf-8")

        cc_path = MakefileBuildSystem().generate(tmp_path, BuildConfig(out_dir_var="BUILD_DIR", variant_name="dev"))

        out = BuildLayout(tmp_path.resolve()).out_dir("dev")
        entries = json.loads(cc_path.read_text(encoding="utf-8"))
        assert any(str(out) in " ".join(e.get("arguments", [])) or str(out) in e.get("command", "") for e in entries)
        assert not (out / "hello.o").exists(), "a dry run compiles nothing"
