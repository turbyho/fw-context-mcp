"""A unit whose parse failed keeps its old rows, thus the index is not current until a parse succeeds.

The manifest entry of such a unit kept the hashes of its last good parse,
and the status read it as current: an index without the result of each
unit of the build read as valid.  The run and ``reindex_file`` now give
the entry ``needs_reparse``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("clang.cindex", reason="libclang not available")
pytestmark = pytest.mark.libclang

PROJECT_ID = "fa11ed0afa11ed0afa11ed0afa11ed00"


@pytest.fixture
def project(tmp_path: Path):
    """A project of two C units, indexed once with success; returns ``(root, db_path)``."""
    from fw_context_mcp.indexer.runner import run

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src/a.c").write_text("int a_value(void) { return 1; }\n", encoding="utf-8")
    (root / "src/b.c").write_text("int b_value(void) { return 2; }\n", encoding="utf-8")
    index_dir = tmp_path / "index"
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(f'[project]\nid = "{PROJECT_ID}"\n', encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text(f'[index]\ndb_dir = "{index_dir.as_posix()}"\n', encoding="utf-8")
    db_path = index_dir / PROJECT_ID / "index.db"
    cc = root / "compile_commands.json"
    cc.write_text(json.dumps([
        {"directory": str(root), "file": name, "arguments": ["cc", "-c", name, "-o", f"{name}.o"]}
        for name in ("src/a.c", "src/b.c")
    ]), encoding="utf-8")
    run(
        compile_commands=cc, db_path=db_path, project_root=root, project_id=PROJECT_ID,
        index_refs=False, index_embeddings=False, analyze_symbols=False, variant="a",
    )
    return root, db_path


def _fail_parse_of(monkeypatch, name: str) -> None:
    """Make the parse of the unit *name* raise, as a macro text that is no type does."""
    from fw_context_mcp.indexer import symbols

    extract_all = symbols.extract_all

    def _extract_all(unit, *args, **kwargs):
        if unit.file.name == name:
            raise ValueError(f"the unit {unit.file} gives a macro text that is not a type")
        return extract_all(unit, *args, **kwargs)

    monkeypatch.setattr(symbols, "extract_all", _extract_all)


def _stale_files(root: Path, db_path: Path) -> set[str]:
    """The files of the entries that the status reads as stale (``header_affected_tus``)."""
    import fw_context_mcp  # noqa: F401  (pysqlite3 redirect first)
    from fw_context_mcp.mcp.shared.stale import _check_header_staleness

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        [config_hash] = [row[0] for row in conn.execute("SELECT config_hash FROM build_configs")]
        _, files = _check_header_staleness(conn, config_hash, root, use_cache=False)
    finally:
        conn.close()
    return {Path(file).name for file in files}


def test_an_index_with_no_failure_is_current(project):
    root, db_path = project
    assert _stale_files(root, db_path) == set()


def test_a_unit_whose_parse_failed_in_the_run_is_stale(project, monkeypatch):
    """A forced run parses each unit; `b.c` fails and keeps the rows of its last parse."""
    from fw_context_mcp.indexer.runner import run

    root, db_path = project
    _fail_parse_of(monkeypatch, "b.c")

    run(
        compile_commands=root / "compile_commands.json", db_path=db_path, project_root=root,
        project_id=PROJECT_ID, index_refs=False, index_embeddings=False, analyze_symbols=False,
        variant="a", force=True,
    )

    assert _stale_files(root, db_path) == {"b.c"}


def test_a_unit_whose_parse_failed_in_reindex_file_is_stale(project, monkeypatch):
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file

    root, db_path = project
    _fail_parse_of(monkeypatch, "b.c")

    result = reindex_file("src/b.c", str(root), with_analysis=False)

    assert result["skipped_tus"] == ["b.c"], result
    assert _stale_files(root, db_path) == {"b.c"}


def test_a_good_reindex_file_after_a_failed_one_makes_the_unit_current(project, monkeypatch):
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file

    root, db_path = project
    with monkeypatch.context() as patch:
        _fail_parse_of(patch, "b.c")
        reindex_file("src/b.c", str(root), with_analysis=False)

    result = reindex_file("src/b.c", str(root), with_analysis=False)

    assert "skipped_tus" not in result, result
    assert _stale_files(root, db_path) == set()


def test_the_next_good_parse_makes_the_unit_current(project, monkeypatch):
    from fw_context_mcp.indexer.runner import run

    root, db_path = project
    with monkeypatch.context() as patch:
        _fail_parse_of(patch, "b.c")
        run(
            compile_commands=root / "compile_commands.json", db_path=db_path, project_root=root,
            project_id=PROJECT_ID, index_refs=False, index_embeddings=False, analyze_symbols=False,
            variant="a", force=True,
        )

    run(
        compile_commands=root / "compile_commands.json", db_path=db_path, project_root=root,
        project_id=PROJECT_ID, index_refs=False, index_embeddings=False, analyze_symbols=False,
        variant="a",
    )

    assert _stale_files(root, db_path) == set()
