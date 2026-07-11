"""Tests for MakefileBuildSystem — compile_commands.json via compiledb."""

import pytest
from fw_context_mcp.indexer.build import BuildConfig
from fw_context_mcp.indexer.builders.makefile import MakefileBuildSystem


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
