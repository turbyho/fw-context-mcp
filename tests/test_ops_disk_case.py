"""The maps of one parse are keyed by ``canonical_path``, thus each lookup uses it too.

``parsed_hashes`` and ``skipped_ranges`` key each file by
``path_key.canonical_path``, which gives the case of the disk on macOS.
``ops`` looked a file up with ``Path.resolve()``, which keeps the case of
the spelling there: a header that libclang reached through ``-I INC``
while the disk has ``inc`` found no hash and no skipped ranges.  These
tests give ``canonical_path`` the change of case of macOS on each system.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fw_context_mcp.indexer import ops
from fw_context_mcp.indexer.db import open_db, transaction, upsert_build_config, upsert_project


def _disk_case(monkeypatch, spelled: Path, on_disk: Path) -> None:
    """``canonical_path`` of *spelled* is *on_disk*, as macOS gives it."""
    real = ops.canonical_path
    monkeypatch.setattr(ops, "canonical_path", lambda path: on_disk if Path(path) == spelled else real(path))


def test_a_macro_header_spelled_in_another_case_gets_the_hash_of_its_parse(tmp_path: Path, monkeypatch):
    spelled = tmp_path / "INC" / "config.h"
    on_disk = tmp_path / "inc" / "config.h"
    _disk_case(monkeypatch, spelled, on_disk)
    macro = SimpleNamespace(
        file=spelled, name="LIMIT", value="16", params="", expanded_value="16", line=1, is_function_like=False,
    )
    conn = open_db(tmp_path / "index.db")
    try:
        with transaction(conn):
            upsert_project(conn, "p" * 32, "case", str(tmp_path))
            upsert_build_config(conn, "hash", "p" * 32, str(tmp_path / "compile_commands.json"))
        ops._store_macros_for_unit(
            conn, "hash", [macro], str(tmp_path / "main.c"), "main.c", 0.0, 1, {}, tmp_path,
            file_hashes={on_disk: "hash-of-the-parse"},
        )
        hashes = [row["source_hash"] for row in conn.execute("SELECT source_hash FROM files")]
    finally:
        conn.close()

    assert hashes == ["hash-of-the-parse"]
