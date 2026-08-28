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


class TestExcludedSources:
    """A file a build ran for and still did not cover is reported once.

    Without this, editing a .c that the build system deliberately leaves out
    holds get_active_build on "reindex_needed" for ever: the only thing that
    clears the report is a build that makes compile_commands.json newer than
    the file, and no build will ever take that file.
    """

    @staticmethod
    def _db_dir(conn) -> Path:
        return Path(conn.execute("PRAGMA database_list").fetchone()["file"]).parent

    def test_a_recorded_file_is_not_reported(self, project):
        from fw_context_mcp.indexer.autobuild import record_excluded
        from fw_context_mcp.utils import compute_source_hash

        conn, root, cc = project
        added = _add_file(root, "src/experiment.c", newer_than=cc)
        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == [
            "src/experiment.c"
        ]

        record_excluded(
            self._db_dir(conn), {"src/experiment.c": compute_source_hash(added)}
        )

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == []

    def test_an_edit_brings_it_back(self, project):
        """The user may have just added the file to the build."""
        from fw_context_mcp.indexer.autobuild import record_excluded
        from fw_context_mcp.utils import compute_source_hash

        conn, root, cc = project
        added = _add_file(root, "src/experiment.c", newer_than=cc)
        record_excluded(
            self._db_dir(conn), {"src/experiment.c": compute_source_hash(added)}
        )

        added.write_text("int experiment(void) { return 2; }\n")
        reference = os.path.getmtime(cc)
        os.utime(added, (reference + 200, reference + 200))

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == [
            "src/experiment.c"
        ]

    def test_the_writer_path_ignores_the_marker(self, project):
        """apply_exclusions=False, or the recorder filters out its own input."""
        from fw_context_mcp.indexer.autobuild import record_excluded
        from fw_context_mcp.utils import compute_source_hash

        conn, root, cc = project
        added = _add_file(root, "src/experiment.c", newer_than=cc)
        record_excluded(
            self._db_dir(conn), {"src/experiment.c": compute_source_hash(added)}
        )

        assert find_unindexed_sources(
            conn, _CONFIG_HASH, root, cc, apply_exclusions=False
        ) == ["src/experiment.c"]

    def test_a_damaged_marker_reads_as_empty(self, project):
        """Erring towards one extra report beats hiding an uncovered file."""
        conn, root, cc = project
        _add_file(root, "src/experiment.c", newer_than=cc)
        (self._db_dir(conn) / "excluded_sources.json").write_text(
            "{not json at all", encoding="utf-8"
        )

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == [
            "src/experiment.c"
        ]


class TestFwContextDirIsSkipped:
    """The directory fw-context writes into is never scanned for sources.

    The patterns of the backend do not cover it.  Measured against
    ``.fw-context/autobuild/default/``: mbed-os contributes ``BUILD/`` and
    platformio ``.pio/build/``, and neither matches; five other backends
    match only because "autobuild/" happens to hold the substring "build/".
    A generated .c left inside would be reported as missing from
    compile_commands.json, and that arms the automatic build again.
    """

    def test_a_source_under_autobuild_is_not_reported(self, project):
        conn, root, cc = project
        _add_file(root, ".fw-context/autobuild/default/src/generated.c", newer_than=cc)

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == []

    def test_a_source_under_the_generated_build_dir_is_not_reported(self, project):
        conn, root, cc = project
        _add_file(root, ".fw-context/build/whatever.c", newer_than=cc)

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == []

    def test_a_real_source_is_still_reported(self, project):
        """The exclusion must not narrow the scan any further than that."""
        conn, root, cc = project
        _add_file(root, "src/added.c", newer_than=cc)
        _add_file(root, ".fw-context/autobuild/default/src/generated.c", newer_than=cc)

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == ["src/added.c"]

    def test_fw_context_never_survives_as_a_scan_root(self, project):
        """Even when an indexed project file put it into the root set.

        A generated header under .fw-context can reach the files table with
        is_project=1, and _indexed_paths seeds the walk from the first path
        component of every project file.
        """
        from fw_context_mcp.indexer.db import transaction, upsert_file

        conn, root, cc = project
        generated = _add_file(root, ".fw-context/autobuild/default/cfg.c", newer_than=cc)
        with transaction(conn):
            upsert_file(
                conn, _CONFIG_HASH, ".fw-context/autobuild/default/cfg.c", "c",
                mtime=os.path.getmtime(generated),
            )
            conn.execute(
                "UPDATE files SET is_project=1 WHERE config_hash=?", (_CONFIG_HASH,)
            )
        _add_file(root, ".fw-context/autobuild/default/other.c", newer_than=cc)

        assert find_unindexed_sources(conn, _CONFIG_HASH, root, cc) == []


def _git_project(tmp_path: Path, *, gitignore: str = "") -> tuple[Path, Path]:
    """A git repository with one indexed source.  Returns (root, cc_path)."""
    import subprocess

    from fw_context_mcp.indexer.db import (
        open_db,
        transaction,
        upsert_build_config,
        upsert_file,
        upsert_project,
    )

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    if gitignore:
        (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    cc = root / "compile_commands.json"
    cc.write_text("[]", encoding="utf-8")

    for cmd in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True,
                       capture_output=True, timeout=30)

    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(root))
        upsert_build_config(conn, "ch", "pid", str(cc))
        upsert_file(conn, "ch", "src/main.c", "c", mtime=1.0)
        conn.execute("UPDATE files SET is_project=1 WHERE config_hash='ch'")
    # compile_commands.json must be older than anything reported as new
    os.utime(cc, (1000, 1000))
    return root, cc


class TestGitCandidates:
    """git ls-files answers what the directory walk could not.

    scan_roots comes from files the index already holds, so a whole new
    top-level directory named no root and the walk from "." does not
    descend.  An untracked file in a new directory is in the git listing.
    """

    def test_a_file_in_a_new_top_level_directory_is_reported(self, tmp_path: Path):
        from fw_context_mcp.indexer.db import open_db
        from fw_context_mcp.mcp.shared.stale import find_unindexed_sources

        root, cc = _git_project(tmp_path)
        (root / "tests").mkdir()
        (root / "tests" / "new.cpp").write_text("void t(){}\n", encoding="utf-8")

        conn = open_db(tmp_path / "index.db")
        try:
            found = find_unindexed_sources(conn, "ch", root, cc)
        finally:
            conn.close()

        assert "tests/new.cpp" in found, (
            "a new module arrives as a whole directory; the walk saw no root "
            "for it and reported nothing"
        )

    def test_a_gitignored_directory_is_not_reported(self, tmp_path: Path):
        from fw_context_mcp.indexer.db import open_db
        from fw_context_mcp.mcp.shared.stale import find_unindexed_sources

        root, cc = _git_project(tmp_path, gitignore="ncs/\n")
        (root / "ncs" / "deep").mkdir(parents=True)
        (root / "ncs" / "deep" / "vendor.c").write_text("int v(void);\n", encoding="utf-8")

        conn = open_db(tmp_path / "index.db")
        try:
            found = find_unindexed_sources(conn, "ch", root, cc)
        finally:
            conn.close()

        assert found == [], (
            "a gitignored vendor tree drops out of the listing by itself — "
            "on zbox-ecb-fw-v5 that is 109,687 candidates and 4 seconds"
        )

    def test_a_committed_vendor_tree_is_not_reported(self, tmp_path: Path):
        """git alone is not enough when the SDK is committed to the repo."""
        import subprocess

        from fw_context_mcp.indexer.db import open_db, transaction, upsert_file
        from fw_context_mcp.mcp.shared.stale import find_unindexed_sources

        root, cc = _git_project(tmp_path)
        (root / "mbed-os").mkdir()
        (root / "mbed-os" / "vendor.c").write_text("int v(void);\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                       capture_output=True, timeout=30)

        conn = open_db(tmp_path / "index.db")
        try:
            # The index knows mbed-os as vendor: a row with is_project=0.
            with transaction(conn):
                upsert_file(conn, "ch", "mbed-os/other.c", "c", mtime=1.0)
                conn.execute(
                    "UPDATE files SET is_project=0 WHERE config_hash='ch' "
                    "AND path='mbed-os/other.c'"
                )
            found = find_unindexed_sources(conn, "ch", root, cc)
        finally:
            conn.close()

        assert found == [], (
            "mbed-os is committed on zbox-ecb-fw — 6,215 source files in git "
            "— so the vendor roots have to come from the index"
        )

    def test_a_cxx_plus_plus_source_is_reported(self, tmp_path: Path):
        from fw_context_mcp.indexer.db import open_db
        from fw_context_mcp.mcp.shared.stale import find_unindexed_sources

        root, cc = _git_project(tmp_path)
        (root / "src" / "new.c++").write_text("void n(){}\n", encoding="utf-8")

        conn = open_db(tmp_path / "index.db")
        try:
            found = find_unindexed_sources(conn, "ch", root, cc)
        finally:
            conn.close()

        assert "src/new.c++" in found, "`.c++` was missing from this module's copy"

    def test_a_project_without_git_falls_back_to_the_walk(self, tmp_path: Path):
        """autoproj is a real project with no repository."""
        from fw_context_mcp.indexer.db import open_db
        from fw_context_mcp.mcp.shared.stale import find_unindexed_sources

        root, cc = _git_project(tmp_path)
        import shutil

        shutil.rmtree(root / ".git")
        (root / "src" / "new.c").write_text("void n(void){}\n", encoding="utf-8")

        conn = open_db(tmp_path / "index.db")
        try:
            found = find_unindexed_sources(conn, "ch", root, cc)
        finally:
            conn.close()

        assert "src/new.c" in found, (
            "with no repository the walk is the answer, and src is a scan root"
        )

    def test_a_missing_git_binary_falls_back(self, tmp_path: Path, monkeypatch):
        from fw_context_mcp.indexer.db import open_db
        from fw_context_mcp.mcp.shared.stale import find_unindexed_sources

        root, cc = _git_project(tmp_path)
        (root / "src" / "new.c").write_text("void n(void){}\n", encoding="utf-8")

        import subprocess as _sp

        def boom(*a, **kw):
            raise OSError("git not found")

        monkeypatch.setattr(_sp, "run", boom)

        conn = open_db(tmp_path / "index.db")
        try:
            found = find_unindexed_sources(conn, "ch", root, cc)
        finally:
            conn.close()

        assert "src/new.c" in found, "a missing git binary must not raise"

    def test_the_limit_can_be_lifted(self, tmp_path: Path):
        """record_excluded replaces the marker, thus it needs the whole set."""
        from fw_context_mcp.indexer.db import open_db
        from fw_context_mcp.mcp.shared.stale import find_unindexed_sources

        root, cc = _git_project(tmp_path)
        for i in range(25):
            (root / "src" / f"extra{i:02d}.c").write_text(f"void e{i}(void){{}}\n",
                                                          encoding="utf-8")

        conn = open_db(tmp_path / "index.db")
        try:
            capped = find_unindexed_sources(conn, "ch", root, cc)
            everything = find_unindexed_sources(conn, "ch", root, cc, limit=None)
        finally:
            conn.close()

        assert len(capped) == 20, "the default cap serves the caller that reports"
        assert len(everything) == 25
