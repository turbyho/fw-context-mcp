"""`fw-context init` reports each dependency that does not work, with its repair, and fails on a required one.

Three gaps of the report of init:

* A check without a fix command (the clang program, a system package)
  printed ``[✗]`` and no repair.
* The list of the remaining steps named only a missing ``libclang-so``.
* init printed "Setup complete" and ended with exit code 0 when a
  critical check failed, and no index run can work then.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from fw_context_mcp.cli import _init_deps
from fw_context_mcp.cli._init import cmd_init
from fw_context_mcp.deps import DepCheckResult

_OK = [DepCheckResult(name=name, status="ok", message="ok") for name in ("pysqlite3", "sqlite-ext", "sqlite-vec")]
_NO_CLANG = DepCheckResult(
    name="clang", status="missing", message="the clang program does not run",
    instructions="Install the clang program:\n  sudo apt install clang", critical=False,
)
_NO_LIBCLANG = DepCheckResult(
    name="libclang-so", status="missing", message="libclang not found",
    fix_cmd="pip install --force-reinstall libclang-ng", critical=True,
)
_SKIPPED = DepCheckResult(name="libclang-so", status="skipped", message="depends on libclang-python", critical=True)


def test_a_check_without_a_fix_command_prints_its_instructions(capsys):
    _init_deps._print_deps_summary([*_OK, _NO_CLANG])

    out = capsys.readouterr().out
    assert "[✗]  clang: the clang program does not run" in out
    assert "        sudo apt install clang" in out


def test_the_checklist_names_each_check_of_the_installation_that_failed(capsys):
    _init_deps._print_checklist(
        [*_OK, _NO_CLANG, _NO_LIBCLANG], {}, build_ok=True, cc_path=None, build_system=None,
        tools_registered=[], index_exists=True,
    )

    out = capsys.readouterr().out
    assert "[system]   1. clang: the clang program does not run" in out
    assert "sudo apt install clang" in out
    assert "[system]   2. libclang-so: libclang not found" in out
    assert "pip install --force-reinstall libclang-ng" in out


def test_only_a_failed_critical_check_is_missing_and_required():
    """A skipped check depends on a check that failed, and that one is in the list."""
    assert _init_deps.missing_required([*_OK, _NO_CLANG, _NO_LIBCLANG]) == [_NO_LIBCLANG]
    assert _init_deps.missing_required([*_OK, _NO_CLANG, _SKIPPED]) == []


def _init_args(root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        tool=None, dry_run=False, force=False, no_prune=False, instructions_only=False, scope="project",
        project=str(root), list_tools=False, quick=True, skip_doctor=False, skip_build=True,
        non_interactive=True, name=None,
    )


def test_init_fails_when_a_required_dependency_does_not_work(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    results = [*_OK, _NO_LIBCLANG]
    monkeypatch.setattr(_init_deps, "_run_deps_auto_fix", lambda project_root, dry_run=False: (results, results))

    code = cmd_init(_init_args(root))

    assert code == 1
    assert "Setup incomplete: a required dependency does not work (libclang-so)" in capsys.readouterr().err


def test_init_completes_when_only_an_optional_dependency_is_missing(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    results = [*_OK, _NO_CLANG]
    monkeypatch.setattr(_init_deps, "_run_deps_auto_fix", lambda project_root, dry_run=False: (results, results))

    code = cmd_init(_init_args(root))

    assert code == 0
    assert "Setup incomplete" not in capsys.readouterr().err
