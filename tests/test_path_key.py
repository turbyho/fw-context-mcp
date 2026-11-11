"""Tests of fw_context_mcp.path_key: one key for each file, on each system.

The tests run on Linux, macOS and Windows.  The test of the case runs only
on a file system that ignores case (Windows, macOS by default): on Linux a
second spelling is a different file.
"""

from __future__ import annotations

import os
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
