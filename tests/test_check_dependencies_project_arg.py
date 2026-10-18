"""The ``fw-context doctor`` command of ``check_dependencies`` names the project of the call.

The caller runs the command in its own shell, whose directory is not always
the project of the call: ``doctor`` then checked and repaired a different
project, or none.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import fw_context_mcp.config.settings as settings


def test_the_doctor_command_names_the_project(tmp_path, monkeypatch):
    from fw_context_mcp.mcp.handlers.maintenance import check_dependencies

    home = tmp_path / "home" / "config.toml"
    home.parent.mkdir()
    home.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", home)
    root = tmp_path / "my proj"
    (root / ".git").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text("[index]\ndb_dir = 5\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rows = {row["name"]: row for row in check_dependencies(project_root=str(root))}

    assert rows["config"]["action"]["command"] == (
        f"fw-context doctor --fix --only config --project {shlex.quote(str(root))}"
    )


def test_other_commands_stay_as_they_are():
    from fw_context_mcp.mcp.handlers.maintenance import _with_project

    assert _with_project("pip install watchfiles", Path("/p")) == "pip install watchfiles"


def test_the_quotes_of_powershell(monkeypatch):
    import sys

    from fw_context_mcp.mcp.handlers.maintenance import _with_project

    monkeypatch.setattr(sys, "platform", "win32")

    assert _with_project("fw-context doctor --fix", Path("C:/a b/o'k")) == "fw-context doctor --fix --project 'C:/a b/o''k'"
