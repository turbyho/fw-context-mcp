"""The pre-test of the session stops the run when a component is missing (tests/_environment.py).

A test that did not find its tool skipped itself, and the run looked
green: 190 tests on a Windows machine without clang in PATH, 710 more
without gcc.  These tests make sure that each missing component is a
problem with a repair, and that a problem stops the run.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import _environment
from tests._environment import Problem


def _item(*markers: str) -> SimpleNamespace:
    names = set(markers)
    return SimpleNamespace(get_closest_marker=lambda name: object() if name in names else None)


def test_a_marker_of_a_selected_test_makes_its_component_necessary():
    items = [_item("make"), _item("libclang"), _item("ollama", "make")]

    assert _environment.selected_components(items) == {"make", "ollama"}


def test_no_marker_needs_no_optional_component():
    assert _environment.selected_components([_item(), _item("libclang")]) == set()


def test_a_make_that_is_not_in_path_is_a_problem_with_a_repair(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(_environment.shutil, "which", lambda name, path=None: None)

    problems = _environment._make(tmp_path, {"system": "linux", "pkg_manager": "apt"})

    assert problems == [Problem("make", "make is not in PATH", "sudo apt install make")]


def test_the_make_probe_needs_no_compiler_of_the_host(tmp_path: Path):
    """The tests of the marker give make the compiler of a test SDK in CC."""
    assert _environment._make(tmp_path, {"system": "linux", "pkg_manager": "apt"}) == []
    assert (tmp_path / "probe-make").is_file()


def test_the_arm_toolchain_of_a_test_needs_the_test_sdks():
    """The assembly corpus takes the fixture arm_toolchain: the pre-test checks the test SDKs for it."""
    item = SimpleNamespace(fixturenames=["arm_toolchain"], get_closest_marker=lambda name: None)

    assert _environment.selected_components([item]) == {_environment.TEST_SDKS}


def test_a_missing_package_of_dev_is_a_problem(monkeypatch):
    real = _environment.importlib.util.find_spec
    monkeypatch.setattr(
        _environment.importlib.util, "find_spec", lambda name: None if name == "fastapi" else real(name),
    )

    [problem] = _environment._dev_packages()

    assert problem.message == "not installed: fastapi"


def test_a_check_of_the_installation_that_is_not_ok_is_a_problem(monkeypatch):
    from fw_context_mcp import deps

    rows = [
        deps.DepCheckResult(name="pysqlite3", status="ok", message="ok"),
        deps.DepCheckResult(name="clang", status="missing", message="the clang program does not run",
                            instructions="sudo apt install clang", critical=False),
    ]
    monkeypatch.setattr(deps, "run_full_check", lambda subset: rows)

    assert _environment._installation() == [
        Problem("clang", "missing: the clang program does not run", "sudo apt install clang"),
    ]


def test_the_report_names_each_problem_and_its_repair():
    text = _environment.report([Problem("make", "make is not in PATH", "sudo apt install make\nthen again")])

    assert "- make: make is not in PATH" in text
    assert "    sudo apt install make\n    then again" in text
    assert '-m "not <marker>"' in text


def test_a_problem_stops_the_run(monkeypatch):
    import tests.conftest as conftest

    monkeypatch.setattr(_environment, "check_environment",
                        lambda items, home: [Problem("make", "make is not in PATH", "sudo apt install make")])
    session = SimpleNamespace(config=SimpleNamespace(option=SimpleNamespace(collectonly=False)), items=[])

    with pytest.raises(pytest.exit.Exception) as stop:
        conftest.pytest_collection_finish(session)

    assert stop.value.returncode == pytest.ExitCode.USAGE_ERROR
    assert "- make: make is not in PATH" in stop.value.msg


def test_a_collection_only_run_checks_nothing(monkeypatch):
    import tests.conftest as conftest

    def fail(items, home):
        raise AssertionError("a run that runs no test needs no component")

    monkeypatch.setattr(_environment, "check_environment", fail)
    session = SimpleNamespace(config=SimpleNamespace(option=SimpleNamespace(collectonly=True)), items=[])

    conftest.pytest_collection_finish(session)
