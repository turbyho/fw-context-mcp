"""``db-integrity`` tells damage of the index from a lock, a permission or a fault of the disk.

The check reported each failed read as a critical ``error``.  The exit code
2 of ``fw-context doctor`` then said "damaged" for a good index that an
index run held, or that the user could not read.  ``reset_index`` already
told the two apart; the two now use one function.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

import fw_context_mcp.config.settings as settings

PID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> Path:
    """The path of the index of an initialized project; the test writes the file."""
    home = tmp_path / "home" / "config.toml"
    home.parent.mkdir()
    home.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", home)
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(f'[project]\nid = "{PID}"\n', encoding="utf-8")
    index_dir = tmp_path / "index"
    (root / ".fw-context" / "local.toml").write_text(f'[index]\ndb_dir = "{index_dir.as_posix()}"\n', encoding="utf-8")
    monkeypatch.chdir(root)
    path = index_dir / PID / "index.db"
    path.parent.mkdir(parents=True)
    return path


def test_a_damaged_file_is_a_critical_error(db_path):
    from fw_context_mcp.deps._checks import check_db_integrity

    db_path.write_bytes(b"this is not a database" * 100)

    result = check_db_integrity()

    assert (result.status, result.critical) == ("error", True), result
    assert "reset_index" in result.instructions


def test_a_file_without_read_permission_is_not_damage(db_path):
    from fw_context_mcp.deps._checks import check_db_integrity

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    db_path.chmod(0)
    try:
        if os.access(db_path, os.R_OK):
            pytest.skip("root reads a file without read permission")

        result = check_db_integrity()
    finally:
        db_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert (result.status, result.critical) == ("degraded", False), result
    assert "do not delete the database" in result.message


@pytest.mark.parametrize(("code", "damage"), [(5, False), (6, False), (14, False), (10 | (1 << 8), False), (11, True), (26, True)])
def test_the_result_code_tells_damage(code, damage):
    """BUSY, LOCKED, CANTOPEN, IOERR_READ are not damage; CORRUPT and NOTADB are."""
    from fw_context_mcp.indexer.db import error_shows_damage

    error = sqlite3.OperationalError("x")
    error.sqlite_errorcode = code

    assert error_shows_damage(error) is damage


def test_an_error_without_a_code_is_not_damage():
    from fw_context_mcp.indexer.db import error_shows_damage

    assert error_shows_damage(sqlite3.ProgrammingError("x")) is False


def test_damage_of_quick_check_stays_damage_when_the_second_read_fails(db_path, monkeypatch):
    """A lock in `integrity_check` after a `quick_check` that showed damage read as "not damage"."""
    from fw_context_mcp.deps import _checks

    db_path.write_bytes(b"x")

    class _Connection:
        def execute(self, sql):
            if "quick_check" in sql:
                return _Cursor(("*** in database main ***\nPage 3: btreeInitPage() returns error code 11",))
            error = _checks.sqlite3.OperationalError("database is locked")  # the sqlite3 of fw_context_mcp: pysqlite3
            error.sqlite_errorcode = 5
            raise error

        def close(self):
            pass

    class _Cursor:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    monkeypatch.setattr(_checks.sqlite3, "connect", lambda *args, **kwargs: _Connection())

    result = _checks.check_db_integrity()

    assert (result.status, result.critical) == ("error", True), result
    assert "btreeInitPage" in result.message
