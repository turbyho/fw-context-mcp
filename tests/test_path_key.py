"""Tests of fw_context_mcp.path_key: one key for each file, on each system.

The tests run on Linux, macOS and Windows.  The test of the case runs only
on a file system that ignores case (Windows, macOS by default): on Linux a
second spelling is a different file.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

import fw_context_mcp  # noqa: F401 — the sqlite3 redirect runs first
from fw_context_mcp.indexer.sdk_detect import classify_key
from fw_context_mcp.path_key import ABSOLUTE_KEY_SQL, canonical_key, canonical_path, index_key, is_absolute_key


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "Proj"
    (root / "Src" / "Drv").mkdir(parents=True)
    (root / "Src" / "Drv" / "Uart.c").write_text("int x;\n", encoding="utf-8")
    return root


def test_a_file_in_the_root_gets_a_relative_key_with_slashes(project: Path) -> None:
    assert index_key(project / "Src" / "Drv" / "Uart.c", project) == "Src/Drv/Uart.c"


def test_a_file_outside_the_root_gets_an_absolute_key_with_slashes(project: Path, tmp_path: Path) -> None:
    sdk = tmp_path / "sdk" / "inc"
    sdk.mkdir(parents=True)
    (sdk / "hal.h").write_text("", encoding="utf-8")

    key = index_key(sdk / "hal.h", project)

    assert key == canonical_path(sdk / "hal.h").as_posix()
    assert "\\" not in key
    assert is_absolute_key(key)


def test_the_key_of_a_canonical_path_is_the_index_key(project: Path, tmp_path: Path) -> None:
    """canonical_key is the second half of index_key, for a caller that has the canonical path already."""
    outside = tmp_path / "sdk" / "hal.h"
    for path in (project / "Src" / "Drv" / ".." / "Drv" / "Uart.c", outside):
        assert canonical_key(canonical_path(path), project) == index_key(path, project)


def test_dot_dot_and_a_relative_spelling_give_the_same_key(project: Path) -> None:
    spelled = project / "Src" / "Drv" / ".." / "Drv" / "Uart.c"
    assert index_key(spelled, project) == index_key(project / "Src" / "Drv" / "Uart.c", project)


def test_a_file_that_does_not_exist_gets_a_key(project: Path) -> None:
    """A deleted file must keep its key: the index removes its rows by it."""
    assert index_key(project / "Src" / "Gone.c", project) == "Src/Gone.c"


def test_a_root_through_a_link_gives_the_keys_of_the_real_root(project: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    try:
        link.symlink_to(project, target_is_directory=True)
    except OSError as exc:  # Windows without the privilege of a symlink
        pytest.skip(f"no symlink on this system: {exc}")
    assert index_key(project / "Src" / "Drv" / "Uart.c", link) == "Src/Drv/Uart.c"
    assert index_key(link / "Src" / "Drv" / "Uart.c", project) == "Src/Drv/Uart.c"


def test_a_spelling_with_a_different_case_gets_the_case_of_the_disk(project: Path) -> None:
    spelled = Path(str(project).lower()) / "src" / "DRV" / "uart.C"
    if not spelled.exists():
        pytest.skip("the file system keeps the case: a different spelling is a different file")
    assert index_key(spelled, project) == "Src/Drv/Uart.c"
    assert index_key(project / "Src" / "Drv" / "Uart.c", Path(str(project).upper())) == "Src/Drv/Uart.c"


def test_the_part_that_exists_gets_the_case_of_the_disk(project: Path) -> None:
    spelled = Path(str(project).lower()) / "src" / "drv" / "New.c"
    if not (project / "src").exists():
        pytest.skip("the file system keeps the case: a different spelling is a different file")
    assert index_key(spelled, project) == "Src/Drv/New.c"


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root opens a file of mode 000")
def test_a_file_that_cannot_be_opened_gets_the_case_of_the_disk(project: Path) -> None:
    """macOS: a file of mode 000 does not open, and its whole path kept the spelling of the caller.

    The root then had the case of the disk and the file the case of the
    caller: the file got an absolute key.  Its own name in the case of the
    caller gave one file two keys.  Now the directories get the case of
    the disk from the nearest one that opens, and the name from the entry
    of its directory.  The test applies where a different case names the
    same file (macOS, Windows).
    """
    locked = project / "Src" / "Drv" / "Locked.h"
    locked.write_text("", encoding="utf-8")
    spelled = Path(str(project).lower()) / "src" / "drv" / "locked.h"
    if not spelled.exists():
        pytest.skip("the file system keeps the case: a different spelling is a different file")
    locked.chmod(0)
    try:
        assert index_key(spelled, project) == "Src/Drv/Locked.h"
    finally:
        locked.chmod(0o644)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or sys.platform == "win32", reason="a Unix socket")
def test_a_socket_in_the_project_gets_the_case_of_the_disk() -> None:
    """macOS: ``open`` of a Unix socket gives EOPNOTSUPP, not EACCES, and the path kept the spelling of the caller.

    A short directory in /tmp: the path of a Unix socket has at most 104
    bytes on macOS.
    """
    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="fwk", dir="/tmp"))
    try:
        root = base / "Proj"
        (root / "Run").mkdir(parents=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(root / "Run" / "Ctl.sock"))
            spelled = Path(str(root).lower()) / "run" / "ctl.sock"
            if not spelled.exists():
                pytest.skip("the file system keeps the case: a different spelling is a different file")
            assert index_key(spelled, root) == "Run/Ctl.sock"
        finally:
            listener.close()
    finally:
        shutil.rmtree(base)


def test_a_relative_root_is_the_directory_of_each_call(tmp_path: Path, monkeypatch) -> None:
    """A root of ``.``: the cache of the canonical root kept the directory of the first call.

    A long-lived process that changes its directory then gave the keys of
    the first project to the files of the second one.
    """
    first, second = tmp_path / "first", tmp_path / "second"
    for root in (first, second):
        (root / "src").mkdir(parents=True)
        (root / "src" / "a.c").write_text("", encoding="utf-8")

    monkeypatch.chdir(first)
    assert index_key(first / "src" / "a.c", Path(".")) == "src/a.c"
    monkeypatch.chdir(second)
    assert index_key(second / "src" / "a.c", Path(".")) == "src/a.c"


def test_a_root_with_dot_dot_after_a_link_is_the_directory_of_its_files(tmp_path: Path, monkeypatch) -> None:
    """``build/..`` with ``build`` a link: the link first, then ``..``, as the files of the root do.

    ``os.path.abspath`` removed the ``..`` from the text first, and the root
    named the directory of the link, not the directory of its files: each
    file got an absolute key.
    """
    target = tmp_path / "elsewhere" / "out"
    target.mkdir(parents=True)
    (tmp_path / "elsewhere" / "src").mkdir()
    (tmp_path / "elsewhere" / "src" / "a.c").write_text("", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    try:
        (work / "build").symlink_to(target, target_is_directory=True)
    except OSError as exc:  # Windows without the privilege of a symlink
        pytest.skip(f"no symlink on this system: {exc}")
    monkeypatch.chdir(work)

    assert index_key(Path("build/../src/a.c"), Path("build/..")) == "src/a.c"


@pytest.mark.parametrize(("key", "absolute"), [
    ("src/a.c", False),
    ("a.c", False),
    ("/opt/sdk/a.c", True),
    ("C:/ncs/zephyr/a.c", True),
    ("c:/ncs/a.c", True),
    ("//server/share/a.c", True),
])
def test_is_absolute_key(key: str, absolute: bool) -> None:
    assert is_absolute_key(key) is absolute


def test_the_sql_condition_agrees_with_is_absolute_key() -> None:
    import sqlite3  # after fw_context_mcp: the pysqlite3 redirect

    keys = ["src/a.c", "a.c", "/opt/sdk/a.c", "C:/ncs/a.c", "c:/ncs/a.c", "//server/share/a.c", "src/C:/x"]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE files (path TEXT)")
    conn.executemany("INSERT INTO files VALUES (?)", [(k,) for k in keys])
    absolute = {row[0] for row in conn.execute(f"SELECT path FROM files WHERE {ABSOLUTE_KEY_SQL}")}
    assert absolute == {k for k in keys if is_absolute_key(k)}


class TestClassifyKey:
    """The rule of ``is_project`` for C, assembly and linker scripts."""

    def test_a_project_file_is_project_code(self) -> None:
        assert classify_key("src/main.c", ["mbed-os/%"], []) == 1

    def test_a_vendor_pattern_makes_vendor_code(self) -> None:
        assert classify_key("mbed-os/drivers/a.c", ["mbed-os/%"], []) == 0

    def test_a_project_pattern_wins_over_a_vendor_pattern(self) -> None:
        assert classify_key("mbed-os/ours/a.c", ["mbed-os/%"], ["mbed-os/ours/%"]) == 1

    def test_a_file_outside_the_root_is_vendor_code(self) -> None:
        assert classify_key("/opt/sdk/a.c", [], []) == 0
        assert classify_key("C:/ncs/a.c", [], []) == 0

    def test_a_relative_project_pattern_does_not_claim_a_file_outside_the_root(self) -> None:
        """``src/%`` is relative to the root: an SDK ``.../src/foo.h`` stays vendor code."""
        assert classify_key("/home/u/.platformio/packages/framework-x/src/foo.h", [], ["src/%"]) == 0
        assert classify_key("C:/pio/packages/framework-x/src/foo.h", [], ["src/%"]) == 0
        assert classify_key("src/foo.h", ["%framework-x/%"], ["src/%"]) == 1

    def test_a_pattern_with_a_leading_wildcard_claims_a_file_outside_the_root(self) -> None:
        assert classify_key("/opt/sdk/ours/a.c", [], ["%/ours/%"]) == 1

    def test_a_project_pattern_claims_a_file_outside_the_root(self) -> None:
        assert classify_key("/home/u/esp/components/fork/a.c", [], ["/home/u/esp/components/fork/%"]) == 1
        assert classify_key("C:/esp/components/fork/a.c", [], ["C:/esp/components/fork/%"]) == 1


def test_the_key_has_no_separator_of_the_system(project: Path) -> None:
    """The key is the same string on each system: never ``os.sep`` other than ``/``."""
    key = index_key(project / "Src" / "Drv" / "Uart.c", project)
    if os.sep != "/":
        assert os.sep not in key
    assert key.split("/") == ["Src", "Drv", "Uart.c"]
