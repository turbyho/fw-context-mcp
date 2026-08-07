"""Tests for the three-state bootstrap flow: not_initialized → no_index → ready.

get_active_build() is the diagnostic tool that guides the agent through
project setup.  It must gracefully report the project state without
raising, while other tools fail-fast with ProjectNotInitializedError.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.config.settings import generate_project_id
from fw_context_mcp.mcp.handlers.maintenance import get_active_build

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_project_root(tmp_path: Path, project_id: str | None = None) -> Path:
    """Create a project root with .fw-context/config.toml.

    When *project_id* is None, config.toml has no [project].id (uninitialized).
    db_dir is set to a temp subdirectory to avoid polluting the user's home.
    """
    root = tmp_path / "proj"
    root.mkdir()
    cfg_dir = root / ".fw-context"
    cfg_dir.mkdir()
    index_dir = tmp_path / "index"
    index_dir.mkdir()

    pid_line = f'id = "{project_id}"\n' if project_id else '# id = ""\n'
    config_toml = (
        "[project]\n"
        f"{pid_line}\n"
        "[index]\n"
        f'db_dir = "{str(index_dir).replace(chr(92), "/")}"\n'
        'compile_commands = "compile_commands.json"\n'
    )
    (cfg_dir / "config.toml").write_text(config_toml, encoding="utf-8")
    (cfg_dir / "local.toml").write_text("", encoding="utf-8")
    return root


def _create_index_db(db_path: Path, project_id: str, root: Path) -> None:
    """Create a minimal index DB with one project and one build config."""
    from fw_context_mcp.indexer.db import open_db, transaction, upsert_build_config, upsert_project

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Create a dummy compile_commands.json so _is_stale returns (False, None)
    cc_path = root / "compile_commands.json"
    cc_path.write_text("[]", encoding="utf-8")
    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, project_id, "test-proj", str(root))
            upsert_build_config(conn, "hash-test", project_id, str(cc_path))
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _reset_sentinel():
    """Ensure no leftover sentinel from other tests."""
    from fw_context_mcp.mcp.shared import context as ctx

    ctx._server_init_error = None
    yield
    ctx._server_init_error = None


# ── not_initialized ──────────────────────────────────────────────────────────


class TestNotInitialized:
    """get_active_build returns not_initialized when project ID is missing."""

    def test_returns_not_initialized_status(self, tmp_path: Path):
        root = _make_project_root(tmp_path, project_id=None)
        result = get_active_build(project_root=str(root))

        assert result["status"] == "not_initialized"
        assert result["project_root"] == str(root)
        assert "index_message" in result
        assert "fw-context init" in result["index_message"]

    def test_does_not_raise(self, tmp_path: Path):
        """get_active_build must not raise even when project is uninitialized."""
        root = _make_project_root(tmp_path, project_id=None)
        # This should not raise ProjectNotInitializedError
        result = get_active_build(project_root=str(root))
        assert result["status"] == "not_initialized"

    def test_no_project_id_in_result(self, tmp_path: Path):
        """not_initialized result should not contain a project_id."""
        root = _make_project_root(tmp_path, project_id=None)
        result = get_active_build(project_root=str(root))
        assert "project_id" not in result


# ── no_index ─────────────────────────────────────────────────────────────────


class TestNoIndex:
    """get_active_build returns no_index when project is initialized but no DB."""

    def test_returns_no_index_status(self, tmp_path: Path):
        pid = generate_project_id()
        root = _make_project_root(tmp_path, project_id=pid)
        result = get_active_build(project_root=str(root))

        assert result["status"] == "no_index"
        assert result["project_root"] == str(root)
        assert result["project_id"] == pid
        assert "index_message" in result
        assert "fw-context index" in result["index_message"]

    def test_db_path_does_not_exist(self, tmp_path: Path):
        """After init but before index, the DB file should not exist."""
        pid = generate_project_id()
        root = _make_project_root(tmp_path, project_id=pid)
        result = get_active_build(project_root=str(root))
        assert result["status"] == "no_index"


# ── ready ────────────────────────────────────────────────────────────────────


class TestReady:
    """get_active_build returns ready when a valid index DB exists."""

    def test_returns_ready_status(self, tmp_path: Path):
        pid = generate_project_id()
        root = _make_project_root(tmp_path, project_id=pid)
        cfg = _load_cfg(root)
        db_path = cfg.index.db_dir / pid / "index.db"
        _create_index_db(db_path, pid, root)

        result = get_active_build(project_root=str(root))

        assert result["status"] == "ready"
        assert result["project_id"] == pid
        assert result["symbol_count"] == 0
        assert result["file_count"] == 0

    def test_returns_config_hash(self, tmp_path: Path):
        pid = generate_project_id()
        root = _make_project_root(tmp_path, project_id=pid)
        cfg = _load_cfg(root)
        db_path = cfg.index.db_dir / pid / "index.db"
        _create_index_db(db_path, pid, root)

        result = get_active_build(project_root=str(root))
        assert result["config_hash"] == "hash-test"


def _load_cfg(root: Path):
    from fw_context_mcp.config import load as load_config

    return load_config(project_root=root)


# ── Other tools fail-fast ────────────────────────────────────────────────────


class TestOtherToolsFailFast:
    """Operational tools (not get_active_build) must raise PIE when uninitialized."""

    def test_reset_index_raises_pie(self, tmp_path: Path):
        from fw_context_mcp.config.settings import ProjectNotInitializedError

        root = _make_project_root(tmp_path, project_id=None)
        from fw_context_mcp.mcp.handlers.maintenance import reset_index

        with pytest.raises(ProjectNotInitializedError):
            reset_index(project_root=str(root))

    def test_reindex_file_raises_pie(self, tmp_path: Path):
        from fw_context_mcp.config.settings import ProjectNotInitializedError

        root = _make_project_root(tmp_path, project_id=None)
        with pytest.raises(ProjectNotInitializedError):
            from fw_context_mcp.mcp.handlers.maintenance import reindex_file

            reindex_file("test.c", project_root=str(root))


# ── Sentinel bypass ──────────────────────────────────────────────────────────


class TestSentinelBypass:
    """get_active_build must work even when a sentinel is set (fatal error case)."""

    def test_works_with_sentinel_set(self, tmp_path: Path):
        """Even if _server_init_error is set, get_active_build should not be blocked."""
        from fw_context_mcp.mcp.shared import context as ctx

        ctx._server_init_error = "Some fatal error from main()"
        root = _make_project_root(tmp_path, project_id=None)
        result = get_active_build(project_root=str(root))
        # get_active_build bypasses _check_server_ready, so it still works
        assert result["status"] == "not_initialized"

    def test_other_tools_blocked_by_sentinel(self, tmp_path: Path):
        """When sentinel is set, other tools should raise RuntimeError."""
        from fw_context_mcp.mcp.shared import context as ctx

        ctx._server_init_error = "Fatal error"
        pid = generate_project_id()
        root = _make_project_root(tmp_path, project_id=pid)
        with pytest.raises(RuntimeError, match="Fatal error"):
            from fw_context_mcp.mcp.handlers.maintenance import reset_index

            reset_index(project_root=str(root))
