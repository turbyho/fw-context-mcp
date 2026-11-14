"""System tests — fw-context init → index pipeline over real project fixtures.

Tests the full user workflow:
1. ``fw-context init`` — creates ``.fw-context/config.toml``
2. ``fw-context index`` — detects missing ``compile_commands.json``, builds, indexes

Uses the minimal project fixtures under ``tests/builds/``.

Run::

    # Detection only (always works, no tools needed)
    python3 -m pytest tests/test_system_indexing.py -m system -x -v -k "detection or registry"

    # Full init + index tests (requires build tools)
    python3 -m pytest tests/test_system_indexing.py -m system -x -v -k "InitAndIndex"

``-m system``: the default options of pytest (``pyproject.toml``) leave the
marker out.

A build tool that a selected class needs and that is missing (ESP-IDF,
arduino-cli, PlatformIO) fails the class: a skip looked like a test that
passed.  Leave a class out with ``-k``.

The Zephyr project and the PlatformIO ESP32-C3 project are in
``tests/test_sdk_projects.py``, and the bare, CMake and Makefile projects
in ``tests/test_builder_projects.py``: they build with the pinned test SDKs.  No
test builds Mbed OS (decision of the user, 2026-11-12); the detection
test of its fixture stays.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fw_context_mcp.indexer.build import detect_build_system
from fw_context_mcp.indexer.builders import registry as builder_registry
from fw_context_mcp.indexer.config_hash import DEFAULT_TRANSIENT_DEFINES
from fw_context_mcp.indexer.db import open_db
from tests._toml import toml_string
from tests._toolchains import write_compile_commands, write_sources

pytestmark = pytest.mark.system

# ── Helpers ────────────────────────────────────────────────────────────────

_BUILDS = Path(__file__).resolve().parent / "builds"


def _initialized_root(tmp_path: Path) -> Path:
    """Return the project root ``tmp_path/proj`` with a project ID, as ``fw-context init`` leaves it.

    WHY not a project of ``tests/builds``: git tracks no ``.fw-context``
    there, and the hash reads the project ID.  A test that used one passed
    only after a test that ran ``fw-context init`` in that project, thus it
    failed on a fresh clone, or on Windows, where that test did not run.
    """
    root = tmp_path / "proj"
    config = root / ".fw-context" / "config.toml"
    if not config.exists():
        config.parent.mkdir(parents=True)
        config.write_text(f'[project]\nid = "{"c" * 32}"\n', encoding="utf-8")
    return root


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _cc_path(proj: Path) -> Path:
    """Return the generated compile_commands.json location for a project.

    The build without variants writes into ``.fw-context/build/default/out``
    (see ``indexer/build_layout.py``), and the build system decides where
    in it: ``out/<env>/`` for PlatformIO, ``out/zephyr/`` for a Zephyr build
    with sysbuild.  The index run reads the file that
    ``build.default_compile_commands`` names, thus the test asks the same.
    """
    from fw_context_mcp.config import load as load_config
    from fw_context_mcp.indexer.build import default_compile_commands

    return default_compile_commands(proj, load_config(project_root=proj))


def _cli(
    args: list[str], cwd: Path | None = None, timeout: int = 180, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run fw-context CLI in a subprocess."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_project_root() / "src")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "fw_context_mcp.cli"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _db_path_for_project(project_root: Path) -> Path:
    from fw_context_mcp.config import derive_project_id
    from fw_context_mcp.config import load as load_config

    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    return cfg.index.db_dir / project_id / "index.db"


def _config_hash(conn, project_root):
    from fw_context_mcp.config import derive_project_id
    from fw_context_mcp.indexer.db import get_active_config

    project_id = derive_project_id(project_root)
    active = get_active_config(conn, project_id)
    return active["config_hash"] if active else None


def _cleanup_index_db(project_root: Path) -> None:
    """Delete the index database directory for a project."""
    db_path = _db_path_for_project(project_root)
    shutil.rmtree(db_path.parent, ignore_errors=True)


def _patch_config(config_path: Path, replacements: dict[str, str]) -> None:
    """Edit an existing TOML config in-place.

    *replacements* maps full key paths like ``"[build] system"`` to new values
    like ``"bare"``.  Uncomments existing lines; adds new keys to the correct
    section (before the next section header) when the key doesn't exist yet.
    """
    import re

    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    result: list[str] = []
    current_section = ""
    # Track keys we've consumed from replacements
    seen: set[str] = set()

    for line in lines:
        section_match = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if section_match:
            # Before moving to a new section, insert any remaining
            # replacements that belong to the current section
            for full_key, value in list(replacements.items()):
                if full_key.startswith(f"[{current_section}] "):
                    key = full_key.split("] ", 1)[1]
                    result.append(f"{key} = {value}")
                    seen.add(full_key)
                    del replacements[full_key]
            current_section = section_match.group(1)
            result.append(line)
            continue

        stripped = line.strip()
        uncommented = re.sub(r"^\s*#\s*", "", stripped)
        if "=" in uncommented:
            key = uncommented.split("=")[0].strip()
            full_key = f"[{current_section}] {key}"
            if full_key in replacements:
                value = replacements[full_key]
                indent = line[: len(line) - len(line.lstrip())]
                result.append(f"{indent}{key} = {value}")
                seen.add(full_key)
                del replacements[full_key]
                continue

        result.append(line)

    # Flush any remaining replacements for the last section
    for full_key, value in list(replacements.items()):
        if full_key.startswith(f"[{current_section}] "):
            key = full_key.split("] ", 1)[1]
            result.append(f"{key} = {value}")
            seen.add(full_key)
            del replacements[full_key]

    # Any leftovers that belong to sections not in the config at all
    for full_key, value in list(replacements.items()):
        section_name = full_key[1:].split("] ")[0]
        key = full_key.split("] ", 1)[1]
        result.append("")
        result.append(f"[{section_name}]")
        result.append(f"{key} = {value}")
        del replacements[full_key]

    config_path.write_text("\n".join(result) + "\n", encoding="utf-8")


def _init_and_index(
    proj: Path,
    *,
    replacements: dict[str, str] | None = None,
    timeout: int | None = None,
    extra_env: dict[str, str] | None = None,
    keep_build: bool = False,
    clean_db: bool = False,
) -> Path:
    """Run fw-context init, patch config, then fw-context index.

    Returns *proj* on success, fails the test on error.
    """
    if replacements is None:
        replacements = {}
    proj = proj.resolve()

    # Clean up previous state
    cc = proj / "compile_commands.json"
    if not keep_build and cc.exists():
        cc.unlink()
    if not keep_build:
        build_dir = proj / "build"
        if build_dir.exists():
            import shutil

            shutil.rmtree(build_dir)
        # PlatformIO uses .pio/ instead of build/
        pio_dir = proj / ".pio"
        if pio_dir.exists():
            import shutil

            shutil.rmtree(pio_dir)
        # Arduino uses a build/ dir inside its sketch folder,
        # but the builder cleans that automatically.
    fwctx_dir = proj / ".fw-context"
    if fwctx_dir.exists():
        import shutil

        shutil.rmtree(fwctx_dir)

    # Step 1: fw-context init — generates a unique UUID4 project ID
    # and writes it to .fw-context/config.toml.  Each test project
    # gets its own ID, independent of git remote or filesystem path.
    # Falls back to manual config creation when no AI tools are present
    # (e.g. CI environments).
    result = _cli(["init", "--project", str(proj)], cwd=proj, timeout=timeout, extra_env=extra_env)
    if result.returncode != 0:
        # init may fail when no AI tools are detected — create config manually
        fwctx = proj / ".fw-context"
        fwctx.mkdir(parents=True, exist_ok=True)
        config_toml = fwctx / "config.toml"
        if not config_toml.exists():
            import uuid

            config_toml.write_text(
                f'[project]\nid = "{uuid.uuid4().hex}"\n',
                encoding="utf-8",
            )

    # Clean the SQLite database when requested (SDK tests).
    # Stale mtimes from a previous run with the same config_hash
    # would cause the mtime fast-path to skip all TUs.
    if clean_db:
        from fw_context_mcp.config import derive_project_id
        from fw_context_mcp.config import load as load_config

        _cfg = load_config(project_root=proj)
        _pid = derive_project_id(proj)
        db_path = _cfg.index.db_dir / _pid / "index.db"
        for suffix in ("", "-wal", "-shm"):
            p = db_path.with_name(db_path.name + suffix)
            if p.exists():
                p.unlink()

    # Step 2: ensure config.toml exists and patch it
    config_path = proj / ".fw-context" / "config.toml"
    # config.toml is created by fw-context init (Step 1) — project-init removed in 0.22.0
    assert config_path.exists(), f"config.toml not created by init at {config_path}"

    if replacements:
        _patch_config(config_path, dict(replacements))

    # NOTE for anyone adding a build-environment override here: local.toml
    # OVERRIDES config.toml, and `fw-context init` writes its auto-detected
    # environment (python path, activate script) into local.toml.  A key set
    # in config.toml alone is silently ignored.

    # Step 3: fw-context index (auto-build + index)
    result = _cli(
        ["index", "--no-refs", "--no-analyze", "--no-embeddings", "--project", str(proj)],
        cwd=proj,
        timeout=timeout,
        extra_env=extra_env,
    )
    if result.returncode != 0:
        pytest.fail(f"index failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")

    generated_cc = _cc_path(proj)
    assert generated_cc.exists(), f"compile_commands.json not created at {generated_cc}"
    return proj


# ── Detection tests ────────────────────────────────────────────────────────


class TestBuildSystemDetection:
    """Verify build system detection for all supported project types."""

    def test_detect_bare_is_none(self):
        assert detect_build_system(_BUILDS / "bare") is None

    def test_detect_generic_cmake(self):
        assert detect_build_system(_BUILDS / "generic_cmake") == "cmake"

    def test_detect_makefile(self):
        assert detect_build_system(_BUILDS / "makefile") == "makefile"

    def test_detect_platformio(self):
        assert detect_build_system(_BUILDS / "platformio_esp32c3") == "platformio"

    def test_detect_mbed_os(self):
        assert detect_build_system(_BUILDS / "mbed_os") == "mbed-os"

    def test_detect_arduino(self):
        assert detect_build_system(_BUILDS / "arduino") == "arduino"

    def test_detect_zephyr(self):
        assert detect_build_system(_BUILDS / "zephyr") == "zephyr"

    def test_detect_esp_idf(self):
        assert detect_build_system(_BUILDS / "esp_idf") == "esp-idf"

    def test_detect_keil_mdk(self):
        assert detect_build_system(_BUILDS / "keil") == "keil-mdk"

    def test_detect_iar_ewarm(self):
        assert detect_build_system(_BUILDS / "iar") == "iar-ewarm"

    def test_detect_ti_ccs(self):
        assert detect_build_system(_BUILDS / "stubs") == "ti-ccs"

    def test_detect_none_for_empty_dir(self, tmp_path):
        assert detect_build_system(tmp_path) is None


# ── Registry completeness tests ────────────────────────────────────────────


class TestBuilderRegistry:
    """Verify all registered builders have required attributes."""

    _REQUIRED_ATTRS = ["name", "config_key", "markers"]

    @pytest.mark.parametrize(
        "config_key",
        [
            "bare",
            "cmake",
            "makefile",
            "platformio",
            "mbed-os",
            "arduino",
            "zephyr",
            "esp-idf",
            "keil-mdk",
            "iar-ewarm",
            "ti-ccs",
            "stm32cubeide",
        ],
    )
    def test_builder_has_required_attrs(self, config_key):
        builder_cls = builder_registry.get(config_key)
        assert builder_cls is not None, f"Builder '{config_key}' not registered"
        for attr in self._REQUIRED_ATTRS:
            assert hasattr(builder_cls, attr), f"Builder '{config_key}' missing attribute '{attr}'"

    @pytest.mark.parametrize(
        "config_key,expected_marker_count",
        [
            ("bare", 0),
            ("cmake", 1),
            ("makefile", 1),
            ("platformio", 1),
            ("mbed-os", 3),
            ("arduino", 1),
            ("zephyr", 2),
            ("esp-idf", 1),
            ("keil-mdk", 1),
            ("iar-ewarm", 2),
            ("ti-ccs", 1),
            ("stm32cubeide", 2),
        ],
    )
    def test_builder_marker_count(self, config_key, expected_marker_count):
        builder_cls = builder_registry.get(config_key)
        assert builder_cls is not None
        assert len(builder_cls.markers) == expected_marker_count, (
            f"Builder '{config_key}' has {len(builder_cls.markers)} markers, "
            f"expected {expected_marker_count}: {builder_cls.markers}"
        )


# ── Config hash tests ──────────────────────────────────────────────────────


@pytest.mark.system
class TestConfigHashStability:
    """Verify config_hash is stable across repeated indexing of the same project."""

    def test_same_project_same_config_hash(self, tmp_path, toolchain):
        """Two parses of one database give one hash.

        The database is written here, and not taken from a build that an
        earlier test left in ``tests/builds``: that made the result depend
        on the order of the tests and on the leftovers of an earlier run.
        """
        from fw_context_mcp.indexer.compile_commands import parse as parse_cc
        from fw_context_mcp.indexer.manifest import compute_structural_hash

        project_root = _initialized_root(tmp_path)
        write_sources(project_root, {"src/main.c": "int main(void) { return 0; }\n"})
        cc = write_compile_commands(tmp_path / "compile_commands.json", [{
            "directory": str(project_root),
            "file": str(project_root / "src" / "main.c"),
            "arguments": toolchain.arguments("src/main.c", "-DFOO=1", "-std=c11"),
        }])
        units1 = list(parse_cc(cc))
        units2 = list(parse_cc(cc))
        h1 = compute_structural_hash(cc, project_root, units1, transient_defines=DEFAULT_TRANSIENT_DEFINES)
        h2 = compute_structural_hash(cc, project_root, units2, transient_defines=DEFAULT_TRANSIENT_DEFINES)
        assert h1 == h2

    def _hash_for(self, tmp_path, name: str, entries: list[dict]) -> str:
        from fw_context_mcp.indexer.compile_commands import parse as parse_cc
        from fw_context_mcp.indexer.manifest import compute_structural_hash

        project_root = _initialized_root(tmp_path)
        write_sources(tmp_path, {entry["file"]: f"int {Path(entry['file']).stem}_value;\n" for entry in entries})
        cc = write_compile_commands(tmp_path / name, entries)
        return compute_structural_hash(cc, project_root, list(parse_cc(cc)), transient_defines=DEFAULT_TRANSIENT_DEFINES)

    def test_config_hash_changes_with_different_dialect(self, tmp_path, toolchain):
        """A different macro or standard is a different build."""
        base = self._hash_for(tmp_path, "base.json", [
            {"file": "a.c", "directory": str(tmp_path), "arguments": toolchain.arguments("a.c", "-std=c11")},
        ])
        define = self._hash_for(tmp_path, "define.json", [
            {"file": "a.c", "directory": str(tmp_path),
             "arguments": toolchain.arguments("a.c", "-std=c11", "-DFEATURE=1")},
        ])
        std = self._hash_for(tmp_path, "std.json", [
            {"file": "a.c", "directory": str(tmp_path), "arguments": toolchain.arguments("a.c", "-std=c99")},
        ])
        assert define != base
        assert std != base

    def test_config_hash_survives_a_different_file_set(self, tmp_path, toolchain):
        """A different set of translation units is the SAME build.

        config_hash identifies the compilation dialect.  Making it depend on
        the file list meant adding one .c file minted a new build identity for
        every unchanged TU, whose rows then had to be migrated to it — the
        "reuse" path, which lost rows owned by headers.  Which files exist is
        the manifest's job; whether one changed is files.source_hash's.
        """
        one = self._hash_for(tmp_path, "one.json", [
            {"file": "a.c", "directory": str(tmp_path), "arguments": toolchain.arguments("a.c", "-std=c11")},
        ])
        renamed = self._hash_for(tmp_path, "renamed.json", [
            {"file": "b.c", "directory": str(tmp_path), "arguments": toolchain.arguments("b.c", "-std=c11")},
        ])
        both = self._hash_for(tmp_path, "both.json", [
            {"file": "a.c", "directory": str(tmp_path), "arguments": toolchain.arguments("a.c", "-std=c11")},
            {"file": "b.c", "directory": str(tmp_path), "arguments": toolchain.arguments("b.c", "-std=c11")},
        ])
        assert renamed == one
        assert both == one


# ── Index statistics ─────────────────────────────────────────────────────────


def _collect_project_stats(project_root: Path) -> dict | None:
    """Collect indexing statistics for a single project, or None when it has no index.

    A project that no test initialized has no project ID, and thus no index.
    """
    from fw_context_mcp.config import ProjectNotInitializedError, derive_project_id
    from fw_context_mcp.config import load as load_config

    cfg = load_config(project_root=project_root)
    try:
        project_id = derive_project_id(project_root)
    except ProjectNotInitializedError:
        return None
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        return None

    conn = open_db(db_path)
    try:
        ch_row = conn.execute(
            "SELECT config_hash, manifest_verification FROM build_configs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if ch_row is None:
            return None
        config_hash = ch_row["config_hash"]
        manifest_verification = ch_row["manifest_verification"]

        file_count = conn.execute("SELECT COUNT(*) FROM files WHERE config_hash=?", (config_hash,)).fetchone()[0]

        sym_count = conn.execute("SELECT COUNT(*) FROM symbols WHERE config_hash=?", (config_hash,)).fetchone()[0]

        ref_count = conn.execute("SELECT COUNT(*) FROM refs WHERE config_hash=?", (config_hash,)).fetchone()[0]

        # File extensions (from files table)
        ext_rows = conn.execute(
            "SELECT path FROM files WHERE config_hash=?",
            (config_hash,),
        ).fetchall()
        exts: dict[str, int] = {}
        for (path,) in ext_rows:
            suffix = Path(path).suffix.lower()
            if suffix:
                exts[suffix] = exts.get(suffix, 0) + 1

        # Symbol kinds (definitions only)
        kind_rows = conn.execute(
            """SELECT kind, COUNT(*) FROM symbols
               WHERE config_hash=? AND is_definition=1
               GROUP BY kind ORDER BY COUNT(*) DESC""",
            (config_hash,),
        ).fetchall()
        kinds: dict[str, int] = dict(kind_rows)

        return {
            "name": project_root.name,
            "manifest_verification": manifest_verification,
            "files": file_count,
            "symbols": sym_count,
            "refs": ref_count,
            "exts": exts,
            "kinds": kinds,
        }
    finally:
        conn.close()


def _print_stats_table(all_stats: list[dict]) -> None:
    """Print a formatted ASCII table of indexing statistics."""
    if not all_stats:
        print("\nNo indexed projects found.")
        return

    # Collect all extension and kind keys for column headers
    ext_keys: list[str] = []
    kind_keys: list[str] = []
    for s in all_stats:
        for k in s["exts"]:
            if k not in ext_keys:
                ext_keys.append(k)
        for k in s["kinds"]:
            if k not in kind_keys:
                kind_keys.append(k)
    ext_keys.sort()
    kind_keys.sort()

    # Column definitions: (header, width, key_fn)
    columns = [
        ("Project", 16, lambda s: s["name"][:15]),
        ("Files", 6, lambda s: str(s["files"])),
        ("Syms", 6, lambda s: str(s["symbols"])),
        ("Refs", 6, lambda s: str(s["refs"])),
    ]
    for ek in ext_keys:
        columns.append((ek, len(ek) + 1, lambda s, ek=ek: str(s["exts"].get(ek, 0))))
    for kk in kind_keys:
        columns.append((kk, len(kk) + 1, lambda s, kk=kk: str(s["kinds"].get(kk, 0))))
    columns.append(("manifest_verification", 12, lambda s: s["manifest_verification"]))

    # Build separator and header
    parts = []
    header_parts = []
    for label, width, _fn in columns:
        parts.append("-" * width)
        header_parts.append(label.ljust(width))
    separator = "-+-".join(parts)
    header = " | ".join(header_parts)

    print("\n" + "=" * len(header))
    print("Index statistics")
    print("=" * len(header))
    print(header)
    print(separator)

    for s in all_stats:
        row_parts = []
        for _label, width, fn in columns:
            row_parts.append(fn(s).ljust(width))
        print(" | ".join(row_parts))

    print(separator)
    print()


@pytest.mark.system
class TestGhostRecordPurge:
    """End-to-end: delete file (same config_hash) → reindex purges ghosts.

    Simulates a git branch switch where a source file exists only on one
    branch but compile_commands.json stays the same (the common case —
    most teams do not regenerate cc.json on every checkout).
    """

    def test_ghost_purged_after_file_deletion(self, tmp_path: Path, toolchain):
        """Scenario: bare/ project indexed → one .c deleted from disk (cc.json
        unchanged) → reindex → ghost symbols purged, survivors intact."""
        import shutil as _shutil

        from fw_context_mcp.indexer.runner import run

        # ── Phase 1: copy bare/, add extra.c, init + index via proven path ──
        proj = tmp_path / "bare_ghost"
        _shutil.copytree(_BUILDS / "bare", proj)

        extra_c = proj / "src" / "extra.c"
        extra_c.write_text("""\
int extra_global = 0;
int extra_init(void) {
    extra_global = 1;
    return 42;
}
""", encoding="utf-8")

        # The bare builder compiles with [build] compiler and its flags.
        proj = _init_and_index(
            proj,
            replacements={
                "[build] system": '"bare"',
                "[build] source_dirs": '["src"]',
                "[build] include_dirs": '["src"]',
                "[build] compiler": toml_string(toolchain.driver("main.c")),
                "[build] extra_flags": json.dumps(list(toolchain.flags)),
            },
            clean_db=True,
        )

        db_path = _db_path_for_project(proj)
        assert db_path.exists()

        # ── Phase 2: verify initial state ──
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, proj)
            defs_before = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM symbols WHERE config_hash=? AND kind='function' AND is_definition=1",
                    (ch,),
                )
            }
            assert "extra_init" in defs_before, f"Missing extra_init in {sorted(defs_before)}"
            assert "uart_init" in defs_before
            assert "main" in defs_before
            print(f"  Before: {sorted(defs_before)}")
            config_hash_before = ch
        finally:
            conn.close()

        # ── Phase 3: delete extra.c from disk, cc.json untouched ──
        extra_c.unlink()

        # ── Phase 4: reindex — same config_hash, missing TU skipped ──
        cc_json = _cc_path(proj)
        config_hash_after = run(
            compile_commands=cc_json,
            db_path=db_path,
            project_root=proj,
            vendor_paths=[],
            project_paths=[],
            index_refs=False,
            index_embeddings=False,
            analyze_symbols=False,
            purge_max_missing_percent=100,
        )
        print(f"  Config hash: before={config_hash_before[:16]}… after={config_hash_after[:16]}…")
        assert config_hash_after == config_hash_before, (
            "config_hash changed — test must exercise the 'unchanged config_hash' path"
        )

        # ── Phase 5: verify ghosts purged, survivors intact ──
        conn = open_db(db_path)
        try:
            defs_after = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM symbols WHERE is_definition=1 AND kind='function'"
                ).fetchall()
            }
            print(f"  After: {sorted(defs_after)}")

            assert "extra_init" not in defs_after, (
                f"Ghost function extra_init survived purge: {sorted(defs_after)}"
            )
            assert "main" in defs_after, "main lost"
            assert "uart_init" in defs_after, "uart_init lost"
            assert "compute_checksum" in defs_after, "compute_checksum lost"

            extra_files = conn.execute(
                "SELECT path FROM files WHERE path LIKE '%extra%'"
            ).fetchall()
            assert len(extra_files) == 0, f"extra file rows survived: {extra_files}"
        finally:
            conn.close()

        _cleanup_index_db(proj)


class TestIndexStatistics:
    """Print indexing statistics for all test projects that have been indexed."""

    def test_print_statistics(self):
        """Collect and print statistics for all indexed test projects."""
        all_stats = []
        for proj_dir in sorted(_BUILDS.iterdir()):
            if not proj_dir.is_dir():
                continue
            # Skip non-project directories
            if proj_dir.name.startswith("."):
                continue
            if (proj_dir / ".git").exists() or (proj_dir / "src").exists():
                stats = _collect_project_stats(proj_dir)
                if stats is not None:
                    all_stats.append(stats)

        _print_stats_table(all_stats)
        # Always passes — informational only
        assert True
