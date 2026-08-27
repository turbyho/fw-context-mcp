"""Tests for the detection of source files that the build system never saw.

A new .c file has no translation unit: it is absent from
compile_commands.json, and only a build puts it there.  A plain reindex
therefore skips it without a word, and every tool then reports an index that
is "fully up to date" over code it knows nothing about.

Two conditions define the case, and the tests hold both in place:

1. The index holds no row for the file.
2. Its mtime is newer than compile_commands.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fw_context_mcp.mcp.shared.stale import (
    EMPTY_RESULT_NEW_SOURCE_MESSAGE,
    annotate_stale,
    find_unindexed_sources,
)

_CONFIG_HASH = "hash-deadbeef"


@pytest.fixture
def project(populated_db, tmp_path: Path):
    """A project with one indexed source file and a compile_commands.json.

    Gives ``(conn, root, cc_path)``.  The compile_commands.json is written
    last and therefore is the newest file, which matches a project at rest.
    """
    from fw_context_mcp.indexer.db import transaction, upsert_file
    from fw_context_mcp.indexer.ops import _normalize_file_path

    src = tmp_path / "src"
    src.mkdir()
    (src / "modem.c").write_text("int modem_init(void) { return 0; }\n")

    with transaction(populated_db):
        upsert_file(
            populated_db,
            _CONFIG_HASH,
            _normalize_file_path(str(src / "modem.c"), tmp_path),
            "c",
            mtime=os.path.getmtime(src / "modem.c"),
        )
        populated_db.execute(
            "UPDATE files SET is_project=1 WHERE config_hash=?", (_CONFIG_HASH,)
        )

    cc_path = tmp_path / "compile_commands.json"
    cc_path.write_text(json.dumps([{"file": str(src / "modem.c")}]))
    return populated_db, tmp_path, cc_path


def _add_file(root: Path, relative: str, *, newer_than: Path | None = None) -> Path:
    """Write a file and, when asked, make it newer than *newer_than*."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("int f(void) { return 1; }\n")
    if newer_than is not None:
        reference = os.path.getmtime(newer_than)
        os.utime(path, (reference + 100, reference + 100))
    return path


class TestFindUnindexedSources:
    def test_a_project_at_rest_reports_nothing(self, project):
        conn, root, cc = project
        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == []

    def test_a_new_source_file_is_reported(self, project):
        """The core case: a file the build system has never seen."""
        conn, root, cc = project
        _add_file(root, "src/sensor.c", newer_than=cc)

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == ["src/sensor.c"]

    def test_an_old_unindexed_file_is_not_reported(self, project):
        """A project keeps files out of the build on purpose.

        Library sources that nothing compiles, tests outside the build, and
        templates are all absent from the index and all correct.  On
        birdie1-v2-fw-v3 there are 726 of them.  Only the mtime tells them
        apart from a file that the user just added.
        """
        conn, root, cc = project
        old = _add_file(root, "src/vendored_example.c")
        reference = os.path.getmtime(cc)
        os.utime(old, (reference - 100, reference - 100))

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == []

    def test_a_new_header_is_not_reported(self, project):
        """A header has no translation unit of its own.

        One that nothing includes stays out of the index even after a build,
        thus reporting it would give a warning with no cure.  One that some
        unit includes reaches the index without help.
        """
        conn, root, cc = project
        _add_file(root, "src/sensor.h", newer_than=cc)

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == []

    def test_a_file_in_a_new_subdirectory_is_reported(self, project):
        conn, root, cc = project
        _add_file(root, "src/drivers/uart.c", newer_than=cc)

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == [
            "src/drivers/uart.c"
        ]

    def test_a_file_outside_the_project_roots_is_not_reported(self, project):
        """The walk starts at the roots that the index names.

        A directory with no indexed code is not part of the build, thus a
        file there is not a missing translation unit.
        """
        conn, root, cc = project
        _add_file(root, "scripts/helper.c", newer_than=cc)

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == []

    def test_a_missing_compile_commands_reports_nothing(self, project):
        """Without a reference point every file looks new.

        A warning on every query is worse than no warning.
        """
        conn, root, cc = project
        _add_file(root, "src/sensor.c", newer_than=cc)
        cc.unlink()

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == []

    def test_the_result_is_capped(self, project):
        conn, root, cc = project
        for i in range(5):
            _add_file(root, f"src/gen_{i}.c", newer_than=cc)

        found = find_unindexed_sources(conn, _CONFIG_HASH, root, cc, limit=3)

        assert len(found) == 3
        assert found == sorted(found), "a capped list must still be deterministic"

    def test_a_rebuilt_compile_commands_silences_the_report(self, project):
        """The loop stops here: a build refreshes the reference point.

        A file that the build system never accepts is reported once, not on
        every query for ever.
        """
        conn, root, cc = project
        new_file = _add_file(root, "src/sensor.c", newer_than=cc)
        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == ["src/sensor.c"]

        reference = os.path.getmtime(new_file)
        os.utime(cc, (reference + 100, reference + 100))

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == []


class TestAnnotateNewSources:
    def test_the_message_names_the_file_and_the_command(self):
        annotated = annotate_stale([], [], empty_new_sources=["src/sensor.c"])

        warning = annotated[0]["warning"]
        assert "src/sensor.c" in warning
        assert "--build" in warning, "a plain reindex cannot repair this"

    def test_a_new_source_wins_over_a_changed_file_count(self):
        """Only the new-source case needs a build, thus it is named first."""
        annotated = annotate_stale(
            [], [], empty_dirty_count=4, empty_new_sources=["src/sensor.c"]
        )

        assert annotated[0]["warning"] == EMPTY_RESULT_NEW_SOURCE_MESSAGE.format(
            count=1, first="src/sensor.c"
        )

    def test_a_named_file_still_wins_over_both(self):
        annotated = annotate_stale(
            [{"file": "src/a.c"}],
            ["/abs/src/a.c"],
            empty_dirty_count=4,
            empty_new_sources=["src/sensor.c"],
        )

        assert "changed" in annotated[0]["warning"]
        assert "--build" not in annotated[0]["warning"]


class TestScanRootFilter:
    """A build directory reaches the scan roots and must not be walked.

    Mbed writes a generated ``mbed_config.h`` into ``BUILD/``, and the
    indexer marks it as project code.  ``BUILD`` therefore lands among the
    roots of zbox-ecb-fw and birdie1-v2-fw-v3, and walking it would cost a
    lot and could report generated sources as new.
    """

    def test_a_build_root_is_dropped(self):
        from fw_context_mcp.mcp.shared.stale import _project_scan_roots

        assert _project_scan_roots({"src", "lib", "BUILD"}, ["BUILD/"]) == {"src", "lib"}

    def test_the_pattern_separator_is_handled(self):
        """The patterns carry a trailing separator, the roots do not.

        ``_path_matches_patterns`` does a substring test, thus comparing the
        bare ``"BUILD"`` against ``"BUILD/"`` matched nothing: the filter had
        no effect and the walk descended into the build output.
        """
        from fw_context_mcp.mcp.shared.stale import _project_scan_roots

        assert _project_scan_roots({"BUILD"}, ["BUILD/"]) == set()
        assert _project_scan_roots({".pio"}, [".pio/"]) == set()

    def test_the_project_root_survives_every_pattern(self):
        from fw_context_mcp.mcp.shared.stale import _project_scan_roots

        assert _project_scan_roots({"."}, ["BUILD/", "build/"]) == {"."}

    def test_an_unrelated_root_survives(self):
        from fw_context_mcp.mcp.shared.stale import _project_scan_roots

        assert _project_scan_roots({"targets_custom"}, ["BUILD/"]) == {"targets_custom"}
