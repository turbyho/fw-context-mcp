"""A run whose macros step failed leaves the marker of the run, and the next run repeats the step.

The step runs only on a signal (``_postprocess._needs_macros``): a parse,
a file that left the index, or the marker of a run (``_run_marker``).  The
runner removed the marker after the post-processing also when the step
failed, thus the next run, which parses nothing, did not repeat it, and the
macro rows kept no values.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import fw_context_mcp.config.settings as settings
from fw_context_mcp.indexer import _postprocess, _run_marker
from fw_context_mcp.indexer.db import open_db
from tests._toolchains import write_compile_commands, write_sources

pytestmark = pytest.mark.libclang

PID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def project(tmp_path: Path, monkeypatch, toolchain) -> tuple[Path, Path]:
    """One build of one file with one macro; return the root and the index database."""
    home = tmp_path / "home" / "config.toml"
    home.parent.mkdir()
    home.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", home)
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    write_sources(root, {"main.c": "#define LEVEL 3\nint level(void) { return LEVEL; }\n"})
    index_dir = tmp_path / "index"
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(f'[project]\nid = "{PID}"\n', encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text(f'[index]\ndb_dir = "{index_dir.as_posix()}"\n', encoding="utf-8")
    write_compile_commands(
        root / "compile_commands.json",
        [{"directory": str(root), "file": "main.c", "arguments": toolchain.arguments("main.c")}],
    )
    return root, index_dir / PID / "index.db"


def _run(root: Path, db_path: Path) -> str:
    from fw_context_mcp.indexer.runner import run

    return run(compile_commands=root / "compile_commands.json", db_path=db_path, project_root=root,
               project_id=PID, index_refs=False, index_embeddings=False, analyze_symbols=False)


def _values(db_path: Path) -> list[str]:
    conn = open_db(db_path)
    try:
        return [row[0] for row in conn.execute("SELECT expanded_values FROM macros WHERE name = 'LEVEL'")]
    finally:
        conn.close()


def test_the_next_run_repeats_a_macros_step_that_failed(project, monkeypatch):
    root, db_path = project

    def fails(conn, ctx):
        raise sqlite3.OperationalError("database is locked")

    steps = [(name, fails if name == "macros" else fn, guard) for name, fn, guard in _postprocess._STEPS]
    monkeypatch.setattr(_postprocess, "_STEPS", steps)
    config_hash = _run(root, db_path)

    assert _values(db_path) == [""]
    assert _run_marker.is_pending(db_path.parent, config_hash), "the marker went, thus no run repeats the step"

    monkeypatch.undo()
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", root.parent / "home" / "config.toml")
    assert _run(root, db_path) == config_hash

    [values] = _values(db_path)
    assert json.loads(values)[0]["value"] == "3"
    assert not _run_marker.is_pending(db_path.parent, config_hash)


def test_each_listing_of_a_file_has_the_headers_that_it_read_in_the_manifest(tmp_path: Path, monkeypatch, toolchain):
    """compile_commands.json lists main.c two times; only the listing with -DUSE_H reads h.h.

    The run keyed the headers by file, thus each entry of the manifest got
    the headers of the last listing: the listing with -DUSE_H missed h.h,
    and a change of h.h did not make it stale.
    """
    from fw_context_mcp.indexer.manifest import load as load_manifest

    home = tmp_path / "home" / "config.toml"
    home.parent.mkdir()
    home.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", home)
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    write_sources(root, {
        "h.h": "#define FROM_H 1\n", "main.c": "#ifdef USE_H\n#include \"h.h\"\n#endif\nint value(void) { return 0; }\n",
    })
    index_dir = tmp_path / "index"
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(f'[project]\nid = "{PID}"\n', encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text(f'[index]\ndb_dir = "{index_dir.as_posix()}"\n', encoding="utf-8")
    write_compile_commands(root / "compile_commands.json", [
        {"directory": str(root), "file": "main.c", "arguments": toolchain.arguments("main.c", "-DUSE_H", output="a.o")},
        {"directory": str(root), "file": "main.c", "arguments": toolchain.arguments("main.c", output="b.o")},
    ])
    db_path = index_dir / PID / "index.db"

    config_hash = _run(root, db_path)

    manifest = load_manifest(db_path.parent, config_hash)
    # The run orders the listings by its claim order, thus the entries are
    # told apart by their flags, not by their position.
    headers = {
        "-DUSE_H" in manifest["arg_sets"][entry["arg_set"]]: entry["headers"] for entry in manifest["entries"]
    }
    assert headers == {True: ["h.h"], False: []}


def test_the_state_of_the_index_names_rows_that_no_post_processing_saw(project):
    """reindex_file and a macros step with no answer leave the marker of the run.

    Nothing read the marker but the runner: get_project_status and each
    query answer said "current", and the daemon did not start the run that
    writes the macro values.
    """
    from fw_context_mcp.mcp.shared.stale import quick_structural_reasons

    root, db_path = project
    config_hash = _run(root, db_path)

    def reasons() -> list[str]:
        conn = open_db(db_path)
        try:
            row = conn.execute("SELECT * FROM build_configs WHERE config_hash = ?", (config_hash,)).fetchone()
            return quick_structural_reasons(conn, row)
        finally:
            conn.close()

    assert reasons() == []
    _run_marker.mark_pending(db_path.parent, config_hash)
    assert reasons() == ["the post-processing of an index run is not complete (macro values)"]


def test_get_project_status_names_rows_that_no_post_processing_saw(project):
    """The status tool builds its reasons on its own (``_build_health``), thus it needs the reason too."""
    from fw_context_mcp.mcp.handlers.maintenance import get_project_status

    root, db_path = project
    config_hash = _run(root, db_path)
    _run_marker.mark_pending(db_path.parent, config_hash)

    status = get_project_status(project_root=str(root))

    assert status["status"] == "reindex_needed", status
    assert any("post-processing of an index run is not complete" in reason for reason in status["reindex_reasons"])


def test_a_unit_that_the_preprocessor_cannot_read_is_named_and_no_reason_of_a_plain_run(tmp_path: Path, monkeypatch, toolchain):
    """b.c includes a header that the build did not make: clang -dM -E has no answer for it, in each run.

    Before, each macro row of the build lost its value, the marker stayed as
    "post-processing not complete", the status advised `fw-context index`
    (which repaired nothing), and the daemon started a run at each start.
    Now only the rows of the files of b.c say no_answer, the state of the
    index names b.c and its error with the action to repair the cause, and
    the structural check that the start of the daemon reads
    (quick_structural_reasons) has no reason.  A query answer advises to
    repair the cause, not to run the index.
    """

    from fw_context_mcp.mcp.handlers.maintenance import get_project_status
    from fw_context_mcp.mcp.shared.index_state import annotate_index_state
    from fw_context_mcp.mcp.shared.stale import quick_structural_reasons

    home = tmp_path / "home" / "config.toml"
    home.parent.mkdir()
    home.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", home)
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    write_sources(root, {
        "cfg.h": "#define LEVEL 3\n",
        "a.c": '#include "cfg.h"\nint a(void) { return LEVEL; }\n',
        "b.c": '#include "generated_missing.h"\n#define LOCAL 1\nint b(void) { return LOCAL; }\n',
    })
    index_dir = tmp_path / "index"
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(f'[project]\nid = "{PID}"\n', encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text(f'[index]\ndb_dir = "{index_dir.as_posix()}"\n', encoding="utf-8")
    # b.c includes a header that is not there: the compiler must refuse it.
    write_compile_commands(root / "compile_commands.json", [
        {"directory": str(root), "file": name, "arguments": toolchain.arguments(name)} for name in ("a.c", "b.c")
    ], broken={"b.c": "generated_missing.h"})
    db_path = index_dir / PID / "index.db"

    config_hash = _run(root, db_path)

    units = _run_marker.no_answer_units(db_path.parent, config_hash)
    assert [unit for unit, _ in units] == ["b.c"]
    assert "generated_missing.h" in units[0][1]
    conn = open_db(db_path)
    try:
        level = conn.execute("SELECT expanded_value FROM macros WHERE name = 'LEVEL'").fetchone()[0]
        local = conn.execute("SELECT expanded_values FROM macros WHERE name = 'LOCAL'").fetchone()[0]
        row = conn.execute("SELECT * FROM build_configs WHERE config_hash = ?", (config_hash,)).fetchone()
        daemon_reasons = quick_structural_reasons(conn, row)
    finally:
        conn.close()
    assert level == "3", "a unit that does not read cfg.h removed its value"
    assert json.loads(local) == [{"state": "no_answer", "units": 1}]
    assert daemon_reasons == [], "the daemon would start a run at each start, and it repairs nothing"

    status = get_project_status(project_root=str(root))
    assert status["status"] == "reindex_needed"
    [reason] = [text for text in status["reindex_reasons"] if "preprocessor gave no macro values" in text]
    assert "b.c" in reason and "Repair the cause, then run `fw-context index`" in reason

    answer = annotate_index_state({"name": "b"}, {"project_root": str(root)})
    assert answer["index_state"] == "not_scheduled"
    assert "Tell the user to repair the cause, then run `fw-context index`." in answer["index_warning"]
    assert "Tell the user to run `fw-context index`" not in answer["index_warning"]
