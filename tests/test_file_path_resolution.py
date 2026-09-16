"""A bare filename must reach the file of that name, and prefer the project.

``read_file`` and ``get_file_map`` let a caller write ``main.cpp`` where
the index holds ``src/main.cpp``.  The resolution used to be

    SELECT path FROM files WHERE config_hash=? AND path LIKE '%main.cpp' LIMIT 3

and then the shortest of those three.  Three faults in one query:

* ``LIKE '%config.h'`` matches ``hw_config.h`` — the pattern reads the
  last characters of the path, not its last SEGMENT.
* ``LIMIT 3`` runs BEFORE the shortest-path choice and the query has no
  ORDER BY, thus the right file need not be among the rows that are
  ranked.  Measured on one project: six files matched ``config.h`` and
  the three rows SQLite returned were ``hw_config.h``, ``pinconfig.h``
  and ``c++config.h`` — the project's own ``src/config.h`` was not
  reachable through its own name at all.
* Nothing preferred application code, so a vendor file could win a name
  that the caller works on every day.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    insert_symbols_batch,
    open_db,
    split_tokens,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.mcp.handlers.source import get_file_map, read_file

PROJECT_ID = "a" * 32
CONFIG_HASH = "hash-paths"

# The project file, and the four vendor paths that end with its name or
# with a name that merely ends the same way.  The order here is the order
# they go into the table, so the test does not depend on rowid order.
VENDOR = [
    "/opt/sdk/framework/libraries/wrapper/inc/hw_config.h",
    "/opt/sdk/framework/libraries/wrapper/inc/pinconfig.h",
    "/opt/toolchain/include/c++/12/bits/c++config.h",
    "/opt/sdk/framework/system/config.h",
]
PROJECT_FILE = "src/config.h"


@pytest.fixture
def indexed(tmp_path: Path) -> Path:
    """A project whose own header shares its name with four vendor files."""
    from fw_context_mcp import config
    from fw_context_mcp.mcp.shared.readiness import _index_db_path

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "config.h").write_text(
        "#pragma once\n#define PROJECT_LIMIT 16\n", encoding="utf-8"
    )
    marker = root / ".fw-context"
    marker.mkdir()
    (marker / "config.toml").write_text(
        f'[project]\nid = "{PROJECT_ID}"\nname = "paths"\n', encoding="utf-8"
    )
    cc = root / "compile_commands.json"
    cc.write_text("[]", encoding="utf-8")

    db_path = _index_db_path(config.load(root))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, PROJECT_ID, "paths", str(root))
            upsert_build_config(conn, CONFIG_HASH, PROJECT_ID, str(cc))
            for path in VENDOR:
                file_id = upsert_file(conn, CONFIG_HASH, path, "c")
                conn.execute(
                    "UPDATE files SET is_project = 0, content = ? WHERE id = ?",
                    ("/* vendor */\n", file_id),
                )
            project_id_row = upsert_file(conn, CONFIG_HASH, PROJECT_FILE, "c")
            conn.execute(
                "UPDATE files SET is_project = 1, content = ? WHERE id = ?",
                ("#pragma once\n#define PROJECT_LIMIT 16\n", project_id_row),
            )
            insert_symbols_batch(conn, [
                (CONFIG_HASH, project_id_row, PROJECT_FILE,
                 split_tokens("PROJECT_LIMIT", "PROJECT_LIMIT"),
                 "usr-limit", "PROJECT_LIMIT", "PROJECT_LIMIT", "varglobal",
                 2, 1, 2, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0),
            ])
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return root


class TestABareNameReachesTheProjectFile:
    def test_read_file_resolves_to_the_project_header(self, indexed: Path):
        answer = read_file(file_path="config.h", project_root=str(indexed))

        assert "error" not in answer, answer
        assert answer["file"].endswith(PROJECT_FILE), (
            f"a bare name reached {answer['file']!r} instead of the project file"
        )

    def test_get_file_map_resolves_to_the_project_header(self, indexed: Path):
        answer = get_file_map(file_path="config.h", project_root=str(indexed))

        assert "error" not in answer, answer
        assert str(answer.get("file", "")).endswith(PROJECT_FILE), answer

    def test_the_full_relative_path_still_works(self, indexed: Path):
        answer = read_file(file_path=PROJECT_FILE, project_root=str(indexed))
        assert "error" not in answer, answer
        assert answer["file"].endswith(PROJECT_FILE)


class TestTheMatchIsOnAPathSegment:
    def test_a_longer_basename_is_not_a_match(self, indexed: Path):
        """``hw_config.h`` is not a file named ``config.h``."""
        from fw_context_mcp.mcp.handlers.source import _resolve_indexed_file

        conn = open_db(_db_of(indexed))
        try:
            resolved = _resolve_indexed_file(conn, CONFIG_HASH, "config.h")
        finally:
            conn.close()
        assert resolved == PROJECT_FILE

    def test_a_name_only_vendor_holds_still_resolves(self, indexed: Path):
        """A name with no project file resolves to the vendor file."""
        from fw_context_mcp.mcp.handlers.source import _resolve_indexed_file

        conn = open_db(_db_of(indexed))
        try:
            resolved = _resolve_indexed_file(conn, CONFIG_HASH, "pinconfig.h")
        finally:
            conn.close()
        assert resolved == "/opt/sdk/framework/libraries/wrapper/inc/pinconfig.h"

    def test_a_name_nothing_holds_resolves_to_nothing(self, indexed: Path):
        from fw_context_mcp.mcp.handlers.source import _resolve_indexed_file

        conn = open_db(_db_of(indexed))
        try:
            assert _resolve_indexed_file(conn, CONFIG_HASH, "no_such_file.h") is None
        finally:
            conn.close()

    def test_a_like_wildcard_in_the_name_is_a_character(self, indexed: Path):
        """``%`` and ``_`` come from a filename, thus they must not widen."""
        from fw_context_mcp.mcp.handlers.source import _resolve_indexed_file

        conn = open_db(_db_of(indexed))
        try:
            assert _resolve_indexed_file(conn, CONFIG_HASH, "%config.h") is None
            assert _resolve_indexed_file(conn, CONFIG_HASH, "_onfig.h") is None
        finally:
            conn.close()


def _db_of(root: Path) -> Path:
    from fw_context_mcp import config
    from fw_context_mcp.mcp.shared.readiness import _index_db_path

    return _index_db_path(config.load(root))
