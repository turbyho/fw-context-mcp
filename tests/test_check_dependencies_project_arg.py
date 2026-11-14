"""The ``fw-context doctor`` command of ``check_dependencies`` names the project of the call.

The caller runs the command in its own shell, whose directory is not always
the project of the call: ``doctor`` then checked and repaired a different
project, or none.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

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


def test_a_project_before_init_gets_each_check_of_the_installation(tmp_path, monkeypatch):
    """Before `fw-context init`, the tool ran 6 of the 8 checks of the installation.

    `clang-resource` was not one of them, thus the missing ARM headers of
    libclang were not reported before the first init.
    """
    from fw_context_mcp.mcp.handlers.maintenance import check_dependencies

    home = tmp_path / "home" / "config.toml"
    home.parent.mkdir()
    home.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", home)
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)

    names = {row["name"] for row in check_dependencies(project_root=str(root))}

    assert names == {
        "pysqlite3", "sqlite-ext", "sqlite-vec", "libclang-python", "libclang-so", "clang-resource", "clang", "watchfiles",
        "tomli-w",
    }


def test_no_check_of_the_installation_reads_the_config():
    """The checks before `config` run before init: none of them is a check that `run_full_check` loads the config for.

    The set of the 8 names is pinned by the test above.
    """
    from fw_context_mcp.deps import _NEEDS_CONFIG, installation_checks

    assert installation_checks() and not installation_checks() & _NEEDS_CONFIG
    assert "config" not in installation_checks()


def test_other_commands_stay_as_they_are():
    from fw_context_mcp.mcp.handlers.maintenance import _with_project

    assert _with_project("pip install watchfiles", Path("/p")) == "pip install watchfiles"


def test_the_quotes_of_powershell(monkeypatch):
    import sys

    from fw_context_mcp.mcp.handlers.maintenance import _with_project

    monkeypatch.setattr(sys, "platform", "win32")
    # The path keeps the separator of the host: a backslash on Windows.
    project = r"'C:\a b\o''k'" if os.name == "nt" else "'C:/a b/o''k'"

    assert _with_project("fw-context doctor --fix", Path("C:/a b/o'k")) == f"fw-context doctor --fix --project {project}"


@pytest.fixture(autouse=True)
def _no_cloud_model_list(monkeypatch):
    """No request to the Ollama of the operator: the rule of the Ollama profiles asks for the cloud models."""
    from fw_context_mcp.llm import _diag

    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {})


def test_a_clang_program_that_does_not_run_is_named_with_its_repair(monkeypatch):
    """Without the clang program each macro row says no_answer and no assembly unit is read; no check named it."""
    from fw_context_mcp.deps import _checks
    from fw_context_mcp.indexer import _clang_cli

    monkeypatch.setattr(_clang_cli, "clang_binary", lambda: "fw-context-no-such-clang")

    [result] = [row for row in _checks.CHECK_ORDER if row[0] == "clang"]
    outcome = result[1]()

    assert outcome.status == "missing"
    assert "no macro values and no assembly units" in outcome.message
    assert "clang" in outcome.instructions


def test_a_windows_path_of_the_loaded_libclang_is_ok(monkeypatch):
    """A test for "/" in the path read each Windows path as "not found", also when the library loaded."""
    from types import SimpleNamespace

    from fw_context_mcp.deps import _checks
    from fw_context_mcp.indexer._libclang import cindex

    path = r"C:\Users\u\venv\Lib\site-packages\clang\native\libclang.dll"
    class LoadedConfig:
        """``Config().lib`` of a binding that loaded the DLL of a Windows venv."""

        lib = SimpleNamespace(_name=path)

    monkeypatch.setattr(cindex, "Config", LoadedConfig)
    monkeypatch.setattr(cindex.Index, "create", staticmethod(lambda: object()))

    result = _checks.check_libclang_so()

    assert (result.status, result.message) == ("ok", path)


def test_a_libclang_that_creates_no_index_is_degraded(monkeypatch):
    from fw_context_mcp.deps import _checks
    from fw_context_mcp.indexer._libclang import cindex

    def fail():
        raise cindex.LibclangError("no symbol clang_createIndex")

    monkeypatch.setattr(cindex.Index, "create", staticmethod(fail))

    result = _checks.check_libclang_so()

    assert result.status == "degraded"
    assert "clang_createIndex" in result.message
