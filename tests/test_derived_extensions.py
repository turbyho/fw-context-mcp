"""Which suffixes a project uses is derived, not declared.

The compiler rules in utils say which suffix means C or C++.  This is the
other question: which suffixes THIS build actually touches.  No hand-written
list can answer it — measured across the test projects, five of seven
compile `.S`, and their headers include `.tcc` and extension-less libstdc++
ones that no set in the code had.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fw_context_mcp.indexer.manifest import derive_extension_sets


def _cc(tmp_path: Path, files: list[str]) -> Path:
    cc = tmp_path / "compile_commands.json"
    cc.write_text(
        json.dumps([{"directory": str(tmp_path), "file": f, "arguments": ["cc", "-c", f]}
                    for f in files]),
        encoding="utf-8",
    )
    return cc


class TestDerivedFromCompileCommands:
    def test_the_suffixes_come_from_the_units_the_build_declares(self, tmp_path: Path):
        cc = _cc(tmp_path, ["src/a.c", "src/b.cpp", "src/startup.S"])

        tu, _ = derive_extension_sets(cc, None)

        assert tu == [".S", ".c", ".cpp"]

    def test_assembly_is_not_lost(self, tmp_path: Path):
        """The reason this reads compile_commands.json and not the entries.

        By the time manifest entries exist, the runner has dropped every
        unit libclang cannot read.  Deriving from them would report that a
        project never compiles assembly, when the truth is that its
        assembly is skipped — and the new-file scan would then stay blind
        to exactly the files worth reporting.
        """
        cc = _cc(tmp_path, ["src/a.c", "src/startup.S"])

        tu, _ = derive_extension_sets(cc, None)

        assert ".S" in tu

    def test_a_project_that_compiles_only_c_says_so(self, tmp_path: Path):
        cc = _cc(tmp_path, ["src/a.c", "src/b.c"])

        tu, _ = derive_extension_sets(cc, None)

        assert tu == [".c"], "no .cpp anywhere, thus none should be looked for"

    def test_header_suffixes_come_from_the_header_table(self, tmp_path: Path):
        cc = _cc(tmp_path, ["src/a.cpp"])
        headers = {"inc/a.h": {}, "inc/b.tcc": {}, "/usr/include/c++/16/string": {}}

        _, hdr = derive_extension_sets(cc, headers)

        assert hdr == ["", ".h", ".tcc"], (
            "the extension-less libstdc++ headers are real and the watcher "
            "was blind to them; dropping the empty suffix would keep it so"
        )

    def test_a_missing_compile_commands_gives_an_empty_set(self, tmp_path: Path):
        tu, hdr = derive_extension_sets(tmp_path / "absent.json", None)

        assert (tu, hdr) == ([], [])

    def test_a_damaged_compile_commands_gives_an_empty_set(self, tmp_path: Path):
        cc = tmp_path / "compile_commands.json"
        cc.write_text("{not json", encoding="utf-8")

        assert derive_extension_sets(cc, None) == ([], [])


class TestLoadingFromTheManifest:
    @staticmethod
    def _manifest(tmp_path: Path, **extra) -> tuple[Path, str]:
        from fw_context_mcp.indexer.manifest import _manifest_path

        db_dir = tmp_path / "db"
        db_dir.mkdir()
        ch = "ch"
        data = {
            "_format": "fw-context-manifest/2",
            "config_hash": ch,
            "project_root": str(tmp_path),
            "entries": [],
            "headers": {},
            **extra,
        }
        _manifest_path(db_dir, ch).write_text(json.dumps(data), encoding="utf-8")
        return db_dir, ch

    def test_the_stored_set_is_returned(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import load_tu_extensions

        db_dir, ch = self._manifest(tmp_path, tu_extensions=[".c", ".S"])

        assert load_tu_extensions(db_dir, ch) == frozenset({".c", ".S"})

    def test_a_manifest_without_the_key_says_it_does_not_know(self, tmp_path: Path):
        """None, not an empty set — the caller must fall back, not scan for nothing."""
        from fw_context_mcp.indexer.manifest import load_tu_extensions

        db_dir, ch = self._manifest(tmp_path)

        assert load_tu_extensions(db_dir, ch) is None

    def test_a_missing_manifest_says_it_does_not_know(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import load_tu_extensions

        assert load_tu_extensions(tmp_path / "nowhere", "ch") is None

    def test_the_header_set_loads_too(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import load_header_extensions

        db_dir, ch = self._manifest(tmp_path, header_extensions=["", ".h", ".tcc"])

        assert load_header_extensions(db_dir, ch) == frozenset({"", ".h", ".tcc"})

    def test_one_parse_serves_every_cheap_key(self, tmp_path: Path, monkeypatch):
        """The manifest is 52 MB on a real project; two caches meant two parses."""
        import fw_context_mcp.indexer.manifest as mod

        db_dir, ch = self._manifest(
            tmp_path, tu_extensions=[".c"], header_extensions=[".h"],
            build_dir_patterns=["build/"],
        )
        mod._BUILD_PATTERNS_CACHE.clear()

        parses = {"n": 0}
        original = mod.load

        def counting(*a, **kw):
            parses["n"] += 1
            return original(*a, **kw)

        monkeypatch.setattr(mod, "load", counting)

        mod.load_tu_extensions(db_dir, ch)
        mod.load_header_extensions(db_dir, ch)
        mod.load_build_dir_patterns(db_dir, ch)

        assert parses["n"] == 1


class TestTheScanUsesTheDerivedSet:
    """A build that compiles an unusual suffix gets it looked for."""

    @staticmethod
    def _project(tmp_path: Path, cc_files: list[str]):
        import subprocess

        from fw_context_mcp.indexer.db import (
            open_db,
            transaction,
            upsert_build_config,
            upsert_file,
            upsert_project,
        )

        root = tmp_path / "proj"
        (root / "src").mkdir(parents=True)
        (root / "src" / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
        cc = root / "compile_commands.json"
        cc.write_text(
            json.dumps([{"directory": str(root), "file": str(root / f),
                         "arguments": ["cc", "-c", f]} for f in cc_files]),
            encoding="utf-8",
        )
        for cmd in (["init", "-q"], ["add", "-A"]):
            subprocess.run(["git", "-C", str(root), *cmd], check=True,
                           capture_output=True, timeout=30)

        db_path = tmp_path / "index.db"
        conn = open_db(db_path)
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(root))
            upsert_build_config(conn, "ch", "pid", str(cc))
            upsert_file(conn, "ch", "src/main.c", "c", mtime=1.0)
            conn.execute("UPDATE files SET is_project=1 WHERE config_hash='ch'")
        import os

        os.utime(cc, (1000, 1000))
        return conn, root, cc

    def test_an_assembly_file_is_reported_when_the_build_compiles_assembly(
        self, tmp_path: Path, monkeypatch
    ):
        import fw_context_mcp.mcp.shared.stale as st

        conn, root, cc = self._project(tmp_path, ["src/main.c", "src/startup.S"])
        (root / "src" / "extra.S").write_text("  .syntax unified\n", encoding="utf-8")
        monkeypatch.setattr(st, "load_tu_extensions", lambda d, c: frozenset({".c", ".S"}))
        try:
            found = st.find_unindexed_sources(conn, "ch", root, cc)
        finally:
            conn.close()

        assert "src/extra.S" in found, (
            "the build compiles assembly, thus an assembly file it does not "
            "cover is worth reporting"
        )

    def test_assembly_is_not_reported_when_the_build_never_compiles_it(
        self, tmp_path: Path, monkeypatch
    ):
        import fw_context_mcp.mcp.shared.stale as st

        conn, root, cc = self._project(tmp_path, ["src/main.c"])
        (root / "src" / "extra.S").write_text("  .syntax unified\n", encoding="utf-8")
        monkeypatch.setattr(st, "load_tu_extensions", lambda d, c: frozenset({".c"}))
        try:
            found = st.find_unindexed_sources(conn, "ch", root, cc)
        finally:
            conn.close()

        assert found == [], "no assembly in this build, thus none to look for"

    def test_a_project_without_the_key_falls_back_to_the_compiler_rules(
        self, tmp_path: Path, monkeypatch
    ):
        import fw_context_mcp.mcp.shared.stale as st

        conn, root, cc = self._project(tmp_path, ["src/main.c"])
        (root / "src" / "new.cpp").write_text("void n(){}\n", encoding="utf-8")
        monkeypatch.setattr(st, "load_tu_extensions", lambda d, c: None)
        try:
            found = st.find_unindexed_sources(conn, "ch", root, cc)
        finally:
            conn.close()

        assert "src/new.cpp" in found, (
            "an index written before the key existed must still work"
        )
