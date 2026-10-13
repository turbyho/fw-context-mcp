"""get_project_status reports the project and every build; get_build_info reports one build.

A project of several builds has no active build: the caller selects the
build.  Until 2026-10 one tool, get_active_build, reported the health of
the newest build and the SUM of the counts of every build, thus three
numbers of one project disagreed and none of them described a build.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from fw_context_mcp.mcp.handlers.maintenance import (
    check_dependencies,
    get_build_info,
    get_project_status,
    list_projects,
)

PROJECT_ID = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


def _project(
    tmp_path: Path, builds: list[tuple[str, str, str]], project_id: str | None = PROJECT_ID,
    manifest: str = "full",
) -> Path:
    """Make a project root and an index of *builds* ``(config_hash, variant, image)``.

    Each build carries ``CURRENT_ROW_FORMAT``, an existing database and the
    manifest verification *manifest*, because each stands in for a
    COMPLETED index run.
    """
    from fw_context_mcp.indexer.db import (
        CURRENT_ROW_FORMAT,
        open_db,
        transaction,
        upsert_build_config,
        upsert_project,
    )

    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    pid_line = f'id = "{project_id}"\n' if project_id else '# id = ""\n'
    (root / ".fw-context" / "config.toml").write_text(
        f"[project]\n{pid_line}\n[index]\ndb_dir = \"{index_dir.as_posix()}\"\n", encoding="utf-8",
    )
    (root / ".fw-context" / "local.toml").write_text("", encoding="utf-8")
    if project_id is None:
        return root
    conn = open_db(index_dir / project_id / "index.db")
    try:
        with transaction(conn):
            upsert_project(conn, project_id, "proj", str(root))
            for config_hash, variant, image in builds:
                cc = root / f"{config_hash}.json"
                cc.write_text("[]", encoding="utf-8")
                upsert_build_config(conn, config_hash, project_id, str(cc), variant=variant, image=image,
                                    board=f"board-{variant}" if variant else None, row_format=CURRENT_ROW_FORMAT)
            conn.execute("UPDATE build_configs SET manifest_verification = ?", (manifest,))
    finally:
        conn.close()
    return root


TWO_IMAGES = [("h-boot", "", "boot"), ("h-app", "", "app")]


@pytest.fixture(autouse=True)
def _reset_sentinel():
    from fw_context_mcp.mcp.shared import context as ctx

    ctx._server_init_error = None
    yield
    ctx._server_init_error = None


class TestProjectStatus:
    def test_one_build_gives_its_fields_and_a_list_of_one(self, tmp_path):
        root = _project(tmp_path, [("h-only", "", "")])

        result = get_project_status(project_root=str(root))

        assert result["status"] == "ready", result
        assert result["symbol_count"] == 0
        assert [(b["variant"], b["image"]) for b in result["builds"]] == [("", "")]
        assert "config_hash" not in result, "the detail of a build is in get_build_info"

    def test_several_builds_give_no_sum(self, tmp_path):
        """A file that two images compile is in the index of each, thus a sum counts it twice."""
        root = _project(tmp_path, TWO_IMAGES)

        result = get_project_status(project_root=str(root))

        assert result["status"] == "ready", result
        assert "symbol_count" not in result and "config_hash" not in result, result
        assert [b["image"] for b in result["builds"]] == ["app", "boot"], "sorted by build, not by age"
        assert all("symbol_count" in b and "reasons" in b for b in result["builds"])
        assert result["index_message"] == "Index is fully up to date (2 builds)."

    def test_a_reason_names_its_build(self, tmp_path):
        from fw_context_mcp.indexer.db import open_db, transaction

        root = _project(tmp_path, TWO_IMAGES)
        conn = open_db(tmp_path / "index" / PROJECT_ID / "index.db")
        try:
            with transaction(conn):
                conn.execute("UPDATE build_configs SET row_format = 'fw-context-rows/0' WHERE config_hash = 'h-boot'")
        finally:
            conn.close()

        result = get_project_status(project_root=str(root))

        assert result["status"] == "reindex_needed"
        assert [r[:30] for r in result["reindex_reasons"]] == ["build boot: row_format_mismatc"]
        boot = next(b for b in result["builds"] if b["image"] == "boot")
        assert boot["reindex_needed"] is True
        assert next(b for b in result["builds"] if b["image"] == "app")["reindex_needed"] is False

    def test_a_project_that_is_not_initialized(self, tmp_path):
        root = _project(tmp_path, [], project_id=None)

        assert get_project_status(project_root=str(root))["status"] == "not_initialized"


class TestBuildInfo:
    def test_one_build_needs_no_selector(self, tmp_path):
        root = _project(tmp_path, [("h-only", "", "")])

        result = get_build_info(project_root=str(root))

        assert result["config_hash"] == "h-only", result
        assert result["memory"] == [] and result["entry_point"] == ""
        assert result["status"] == "ready"
        assert "builds" not in result, "the list of builds is in get_project_status"

    def test_several_builds_need_the_pair(self, tmp_path):
        root = _project(tmp_path, TWO_IMAGES)

        result = get_build_info(project_root=str(root))

        assert set(result) == {"error"}, result
        assert "names no build" in result["error"] and 'image="boot"' in result["error"]

    def test_the_named_build_answers(self, tmp_path):
        root = _project(tmp_path, [("h-a", "a", "app"), ("h-b", "b", "app")])

        result = get_build_info(project_root=str(root), variant="b", image="app")

        assert (result["config_hash"], result["variant"], result["image"], result["board"]) == (
            "h-b", "b", "app", "board-b",
        )

    def test_a_project_that_is_not_initialized(self, tmp_path):
        root = _project(tmp_path, [], project_id=None)

        assert get_build_info(project_root=str(root))["status"] == "not_initialized"


class TestListProjects:
    def test_several_builds_give_a_count_and_no_sum(self, tmp_path):
        root = _project(tmp_path, TWO_IMAGES)

        entry = next(p for p in list_projects(project_root=str(root)) if p.get("project_id") == PROJECT_ID)

        assert entry["build_count"] == 2
        assert entry["symbol_count"] is None and entry["analysis"] is None

    def test_one_build_gives_its_counts(self, tmp_path):
        root = _project(tmp_path, [("h-only", "", "")])

        entry = next(p for p in list_projects(project_root=str(root)) if p.get("project_id") == PROJECT_ID)

        assert entry["build_count"] == 1
        assert entry["symbol_count"] == 0


class TestCheckDependencies:
    @staticmethod
    def _run(root: Path) -> tuple[list[dict], dict]:
        from fw_context_mcp.deps import DepCheckResult

        seen: dict = {}

        def fake_check(project_root=None, subset=None):
            seen["subset"] = subset
            return [DepCheckResult(name="libclang-so", status="missing", message="no .so",
                                   fix_cmd="apt install libclang-18-dev", instructions="", critical=True)]

        with mock.patch("fw_context_mcp.deps.run_full_check", fake_check):
            return check_dependencies(project_root=str(root)), seen

    def test_each_entry_gives_the_action(self, tmp_path):
        root = _project(tmp_path, [("h-only", "", "")])

        rows, seen = self._run(root)

        assert seen["subset"] is None, "an initialized project gets every check"
        assert rows == [{
            "name": "libclang-so", "status": "missing", "critical": True, "message": "no .so",
            "action": {"message": "no .so", "command": "apt install libclang-18-dev"},
        }]

    def test_a_project_that_is_not_initialized_gets_the_checks_without_config(self, tmp_path):
        root = _project(tmp_path, [], project_id=None)

        _rows, seen = self._run(root)

        assert seen["subset"] is not None and "libclang-so" in seen["subset"]
        assert "ollama" not in seen["subset"]


def test_the_stats_resource_gives_a_build_count_for_several_builds(monkeypatch):
    """list_projects gives no counts for several builds, and the resource must not print None."""
    from fw_context_mcp.mcp import server

    entry = {"name": "p", "project_id": "pid", "symbol_count": None, "file_count": None,
             "build_count": 7, "indexed_at": "2026-10-09 00:00:00", "reindex_needed": False}
    monkeypatch.setattr(server.maintenance, "list_projects", lambda: [entry])

    text = server.resource_stats()

    assert "7 builds" in text and "None" not in text, text


def _esp_idf_project(tmp_path: Path, *, application_built: bool) -> Path:
    """An ESP-IDF project of two images, the bootloader indexed last, thus the newest build.

    The database of the application is ``out/compile_commands.json``: the
    build system names it (``application_database``), thus it is the build
    that ``fw-context index`` checks.
    """
    from fw_context_mcp.indexer.build_layout import BuildLayout
    from fw_context_mcp.indexer.db import CURRENT_ROW_FORMAT, open_db, transaction, upsert_build_config, upsert_project

    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    (root / ".fw-context" / "config.toml").write_text(
        f"[project]\nid = \"{PROJECT_ID}\"\n\n[build]\nsystem = \"esp-idf\"\n\n"
        f"[index]\ndb_dir = \"{index_dir.as_posix()}\"\n", encoding="utf-8",
    )
    (root / ".fw-context" / "local.toml").write_text("", encoding="utf-8")
    out_dir = BuildLayout(root).out_dir("")
    app_cc, boot_cc = out_dir / "compile_commands.json", out_dir / "bootloader" / "compile_commands.json"
    boot_cc.parent.mkdir(parents=True)
    boot_cc.write_text("[]", encoding="utf-8")
    if application_built:
        app_cc.write_text("[]", encoding="utf-8")
    conn = open_db(index_dir / PROJECT_ID / "index.db")
    try:
        with transaction(conn):
            upsert_project(conn, PROJECT_ID, "proj", str(root))
            for config_hash, image, cc in (("h-app", "fwctx_app", app_cc), ("h-boot", "bootloader", boot_cc)):
                upsert_build_config(conn, config_hash, PROJECT_ID, str(cc), image=image, row_format=CURRENT_ROW_FORMAT)
            conn.execute("UPDATE build_configs SET manifest_verification = 'full'")
    finally:
        conn.close()
    return root


class TestProjectFacts:
    def test_another_build_does_not_clear_the_marker_of_the_application(self, tmp_path):
        """The bootloader is there, and the application is not: the marker of the run stays.

        Each build read and cleared the marker of the project, thus the
        bootloader removed it, and every query answer lost its build_warning
        while the application was still missing.
        """
        from fw_context_mcp.indexer.autobuild import read_problem, record_problem

        root = _esp_idf_project(tmp_path, application_built=False)
        db_dir = tmp_path / "index" / PROJECT_ID
        record_problem(db_dir, "The build is not there. Run `fw-context index --build`.", build_missing=True)

        result = get_project_status(project_root=str(root))

        assert read_problem(db_dir) is not None, "a check of the bootloader removed the marker"
        assert result["reindex_reasons"].count("The build is not there. Run `fw-context index --build`.") == 1, (
            result["reindex_reasons"]
        )

    def test_the_marker_goes_when_the_application_is_there_again(self, tmp_path):
        from fw_context_mcp.indexer.autobuild import read_problem, record_problem

        root = _esp_idf_project(tmp_path, application_built=True)
        db_dir = tmp_path / "index" / PROJECT_ID
        record_problem(db_dir, "The build is not there.", build_missing=True)

        get_project_status(project_root=str(root))

        assert read_problem(db_dir) is None, "the build of the check is there, thus the text is out of date"

    def test_new_sources_start_from_the_application(self, tmp_path, monkeypatch):
        """``find_unindexed_sources`` reads the TU extensions of the build that it starts from."""
        from fw_context_mcp.mcp.handlers import maintenance

        root = _esp_idf_project(tmp_path, application_built=True)
        started: list[str] = []

        def fake(conn, config_hash, *args, **kwargs):
            started.append(config_hash)
            return []

        monkeypatch.setattr(maintenance, "find_unindexed_sources", fake)

        get_project_status(project_root=str(root))

        assert started == ["h-app"], f"the scan started from {started}, and the bootloader sorts first"


class TestProjectMessage:
    def test_builds_without_full_header_tracking_are_not_up_to_date(self, tmp_path):
        """The message must not say "fully up to date" beside a warning that says "degraded"."""
        root = _project(tmp_path, TWO_IMAGES, manifest="none")

        result = get_project_status(project_root=str(root))

        assert "fully up to date" not in result["index_message"], result["index_message"]
        assert "manifest verification none" in result["index_message"]
        assert result["_warning"].startswith("Builds without full header tracking: app, boot."), result["_warning"]

    def test_a_run_in_progress_with_no_reason_asks_for_nothing(self, tmp_path, monkeypatch):
        from fw_context_mcp.mcp.handlers import maintenance

        root = _project(tmp_path, TWO_IMAGES)
        monkeypatch.setattr(maintenance, "read_reindex_state", lambda root: "reindexing")
        monkeypatch.setattr(maintenance, "read_reindex_progress", lambda db_dir: "→ 10/20")

        result = get_project_status(project_root=str(root))

        assert result["status"] == "reindexing"
        assert result["reindex_reasons"] == []
        assert "background reindex in progress" in result["index_message"], result["index_message"]
        assert "reindex_reasons" not in result["index_message"], "there is no reason to read"
        # The run does not change these builds, thus no build waits for it.
        assert "the index of each build is current" in result["index_message"], result["index_message"]
        assert [row["reindex_state"] for row in result["builds"]] == [None, None]

    def test_a_declared_variant_without_an_index_is_listed(self, tmp_path):
        root = _project(tmp_path, [("h-a", "a", "")])
        config = root / ".fw-context" / "config.toml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + '\n[[build.variants]]\nname = "a"\n\n[[build.variants]]\nname = "ghost"\n',
            encoding="utf-8",
        )

        result = get_project_status(project_root=str(root))

        assert result["declared_not_indexed"] == [
            {"variant": "ghost", "command": "fw-context index --build --variant ghost"},
        ]


def test_a_newer_row_format_of_one_build_asks_for_a_restart_one_time(tmp_path):
    """No command repairs it, thus the project gets the note one time, and no reason."""
    from fw_context_mcp.indexer.db import open_db, transaction

    root = _project(tmp_path, TWO_IMAGES)
    conn = open_db(tmp_path / "index" / PROJECT_ID / "index.db")
    try:
        with transaction(conn):
            conn.execute("UPDATE build_configs SET row_format = 'fw-context-rows/999' WHERE config_hash = 'h-boot'")
    finally:
        conn.close()

    result = get_project_status(project_root=str(root))

    assert result["client_restart_required"] is True
    assert result["index_message"].count("Restart the LLM client") == 1, result["index_message"]
    assert result["reindex_reasons"] == [] and result["status"] == "ready"


def test_a_build_that_a_reindex_removed_after_the_selection_is_an_error(tmp_path, monkeypatch):
    from fw_context_mcp.mcp.shared import variants

    root = _project(tmp_path, [("h-only", "", "")])
    monkeypatch.setattr(variants, "resolve_build", lambda *args, **kwargs: ("h-gone", None))

    result = get_build_info(project_root=str(root))

    assert set(result) == {"error"} and "No build config indexed" in result["error"], result


def test_reset_index_counts_the_whole_database(tmp_path):
    """The reset deletes every build, thus the dry run counts every build.

    It counted the symbols of the newest build alone: on a project of 7
    builds, the size of the one program that the last index run did.
    """
    from fw_context_mcp.indexer.db import open_db, transaction, upsert_file
    from fw_context_mcp.mcp.handlers.maintenance import reset_index

    root = _project(tmp_path, TWO_IMAGES)
    conn = open_db(tmp_path / "index" / PROJECT_ID / "index.db")
    try:
        with transaction(conn):
            for config_hash, count in (("h-boot", 2), ("h-app", 3)):
                file_id = upsert_file(conn, config_hash, "a.c", "c", mtime=0.0)
                for n in range(count):
                    conn.execute(
                        "INSERT INTO symbols (config_hash, file_id, usr, name, qualified_name, kind, line, col,"
                        " is_definition) VALUES (?, ?, ?, ?, ?, 'function', ?, 1, 1)",
                        (config_hash, file_id, f"{config_hash}-{n}", f"f{n}", f"f{n}", n + 1),
                    )
    finally:
        conn.close()

    result = reset_index(project_root=str(root))

    assert result["action"] == "dry_run"
    assert (result["build_count"], result["symbol_count"]) == (2, 5), result


def test_the_stale_recovery_needs_the_build_of_the_caller():
    """Without a build it read the newest build of the project, not the build that the caller named."""
    import inspect

    from fw_context_mcp.mcp.shared.stale import _with_stale_recovery

    parameter = inspect.signature(_with_stale_recovery).parameters["config_hash"]
    assert parameter.default is inspect.Parameter.empty and parameter.kind is inspect.Parameter.KEYWORD_ONLY
