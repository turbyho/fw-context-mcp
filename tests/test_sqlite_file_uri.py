"""The SQLite URI of a database file, also of a network path of Windows.

``Path.as_uri()`` gives a UNC path (``\\\\server\\share\\x.db``) the
authority of the server, and SQLite refuses it: "invalid uri authority"
(measured on Windows 11 with SQLite 3.54).  ``resolve()`` turns a mapped
network drive into such a path, thus an index on a network drive could not
be opened read-only.
"""

from __future__ import annotations

import sys
from pathlib import Path, PureWindowsPath

import pytest

from fw_context_mcp.utils import _unc_file_uri, sqlite_file_uri


@pytest.mark.parametrize(("path", "uri"), [
    (PureWindowsPath(r"\\srv\share\i2 probe\x#1.db"), "file:////srv/share/i2%20probe/x%231.db"),
    (PureWindowsPath(r"\\127.0.0.1\C$\Users\Jiří\x.db"), "file:////127.0.0.1/C%24/Users/Ji%C5%99%C3%AD/x.db"),
], ids=["unc", "unc with a drive share"])
def test_the_form_of_a_unc_uri(path: PureWindowsPath, uri: str):
    """An empty authority and the server in the path: the form that SQLite takes."""
    assert _unc_file_uri(path) == uri


class _AbsolutePath:
    """A path whose ``absolute()`` is *pure*: the UNC branch of ``sqlite_file_uri`` on each system."""

    def __init__(self, pure: PureWindowsPath) -> None:
        self.pure = pure

    def absolute(self) -> PureWindowsPath:
        return self.pure


@pytest.mark.parametrize("text", [r"\\srv\share\x.db", r"\\?\C:\x.db", r"\\?\UNC\srv\share\x.db", r"\\.\C:\x.db"])
def test_each_path_whose_drive_starts_with_two_backslashes_takes_the_unc_form(text: str):
    """Measured on Windows 11: each of these opens in this form, and as_uri of Python 3.11 failed."""
    path = PureWindowsPath(text)

    assert sqlite_file_uri(_AbsolutePath(path), "ro") == f"{_unc_file_uri(path)}?mode=ro"  # type: ignore[arg-type]  # a path with absolute() only


def test_each_other_path_is_as_uri(tmp_path: Path):
    database = tmp_path / "a b#%.db"

    assert sqlite_file_uri(database, "rwc") == f"{database.absolute().as_uri()}?mode=rwc"


def test_the_mode_is_a_query(tmp_path: Path):
    import sqlite3  # here: after the redirect of fw_context_mcp to pysqlite3

    database = tmp_path / "x.db"
    sqlite3.connect(database).close()

    uri = sqlite_file_uri(database, "ro")

    assert uri.endswith("?mode=ro")
    readonly = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("CREATE TABLE t (a)")
    finally:
        readonly.close()


@pytest.mark.skipif(sys.platform != "win32", reason="a network path of Windows")
def test_a_database_on_a_unc_path_opens_read_only(tmp_path: Path):
    """The administrative share of this machine (\\\\127.0.0.1\\C$) is a UNC path to a local file."""
    import sqlite3  # here: after the redirect of fw_context_mcp to pysqlite3

    database = tmp_path / "i2 probe#.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE t (a)")
    connection.commit()
    connection.close()
    drive = database.drive.rstrip(":")
    unc = Path(f"\\\\127.0.0.1\\{drive}$") / database.relative_to(database.anchor)
    try:
        available = unc.exists()
    except OSError:  # Python 3.11 and 3.12 raise for a share that refuses the access
        available = False
    if not available:
        pytest.skip("the administrative share of this machine is not available")

    from fw_context_mcp.indexer.db import open_db_readonly

    readonly = sqlite3.connect(sqlite_file_uri(unc, "ro"), uri=True)
    try:
        assert readonly.execute("SELECT count(*) FROM t").fetchone() == (0,)
    finally:
        readonly.close()
    # The read-only open of an index, which resolves the path first.
    index = open_db_readonly(unc)
    try:
        assert index.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    finally:
        index.close()
