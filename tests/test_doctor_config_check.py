"""``doctor`` and ``check_dependencies`` name a config file that cannot be used.

After ``load`` started to raise ``ConfigError``, each check that reads the
config reported "check crashed" with the same message, each with
``critical=True``, and ``db-integrity`` among them read as a damaged
database.  The MCP tool
``check_dependencies`` itself failed on the error.  Now one check
``config`` names the file, and the checks that read the config are
``skipped``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

PID = "0123456789abcdef0123456789abcdef"
NEEDS_CONFIG = {"ollama", "chat-model", "embed-model", "db-integrity", "disk-space"}


@pytest.fixture
def broken(tmp_path) -> Path:
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(f'[project]\nid = "{PID}"\n[index\n', encoding="utf-8")
    return root


def test_one_check_names_the_file_and_the_others_are_skipped(broken):
    from fw_context_mcp.deps import run_full_check

    results = {r.name: r for r in run_full_check(project_root=broken)}

    config = results["config"]
    assert (config.status, config.critical) == ("error", True)
    assert config.message.startswith(f"{broken / '.fw-context' / 'config.toml'}: not valid TOML")
    for name in NEEDS_CONFIG:
        assert results[name].status == "skipped", results[name]
        assert "(check config)" in results[name].message
    assert not any("check crashed" in r.message for r in results.values())


def test_the_obsolete_files_check_runs_on_a_broken_config(broken):
    """It parses each config file itself: a skip hid its result."""
    from fw_context_mcp.deps import run_full_check

    [result] = run_full_check(project_root=broken, subset={"obsolete-files"})

    assert result.status != "skipped", result
    assert "check crashed" not in result.message


def test_a_subset_gives_the_config_check_when_the_config_does_not_load(broken):
    """`doctor --only db-integrity` gave "All checks passed." and exit code 0."""
    from fw_context_mcp.cli._doctor import cmd_doctor
    from fw_context_mcp.deps import run_full_check

    names = [r.name for r in run_full_check(project_root=broken, subset={"db-integrity"})]
    code = cmd_doctor(argparse.Namespace(only="db-integrity", project=str(broken), fix=False, json=False))

    assert names == ["config", "db-integrity"]
    assert code == 2


def test_a_subset_gives_no_config_check_when_the_config_loads(tmp_path):
    from fw_context_mcp.deps import run_full_check

    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)

    names = [r.name for r in run_full_check(project_root=root, subset={"disk-space"})]

    assert names == ["disk-space"]


def test_the_config_check_is_ok_for_a_valid_config(tmp_path):
    from fw_context_mcp.deps import run_full_check

    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)

    [config] = run_full_check(project_root=root, subset={"config"})

    assert config.status == "ok"


def test_the_mcp_tool_runs_the_audit_on_a_config_error(broken):
    from fw_context_mcp.mcp.handlers.maintenance import check_dependencies

    rows = {row["name"]: row for row in check_dependencies(project_root=str(broken))}

    assert rows["config"]["status"] == "error"
    assert rows["db-integrity"]["status"] == "skipped"
    assert rows["pysqlite3"]["status"] in ("ok", "missing", "degraded")


def test_a_subset_reports_a_load_error_that_is_not_a_config_error(tmp_path, monkeypatch):
    """The load of the row "config" ran outside the try: the error left `run_full_check` without a table."""
    from fw_context_mcp import deps

    def _two_projects(project_root):
        raise ValueError("The project name matches 2 projects")

    monkeypatch.setattr(deps, "load_project_config", _two_projects)

    results = {r.name: r for r in deps.run_full_check(project_root=tmp_path, subset={"db-integrity"})}

    assert list(results) == ["config", "db-integrity"], results
    assert results["config"].message == "check crashed: The project name matches 2 projects"
    assert results["db-integrity"].status == "error", results
    assert "The project name matches 2 projects" in results["db-integrity"].message
    assert deps.exit_code(list(results.values())) == 2
