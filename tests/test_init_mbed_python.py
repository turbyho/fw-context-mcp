"""``fw-context init`` on an Mbed OS project when it finds no Python with mbed-cli.

The Mbed OS build stops without ``[build] python``, thus init asks for the
Python, and it tells what to set when the user gives none.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.cli import _init, _init_interactive
from fw_context_mcp.indexer.builders.mbed_os import MbedOSBuildSystem


@pytest.fixture
def written(monkeypatch) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    monkeypatch.setattr(_init, "_write_build_key", lambda root, key, value: keys.append((key, value)))
    monkeypatch.setattr(
        MbedOSBuildSystem, "detect_environment", classmethod(lambda cls, root: {"python": None, "activate": None})
    )
    return keys


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(build=SimpleNamespace(python=None, activate=None, idf_path=None))


def test_without_an_answer_init_names_the_setting(tmp_path: Path, written, capsys):
    _init_interactive.resolve_build_env(tmp_path, _cfg(), "mbed-os", non_interactive=True, dry_run=False)

    out = capsys.readouterr().out
    assert "The Mbed OS build needs [build] python" in out
    assert "install mbed-cli" in out
    assert written == []


@pytest.fixture
def python_with_mbed_cli(tmp_path: Path, monkeypatch) -> str:
    """The Python of the tests, with a fake mbed-cli on PYTHONPATH."""
    site = tmp_path / "site" / "mbed"
    site.mkdir(parents=True)
    (site / "__init__.py").write_text("", encoding="utf-8")
    (site / "mbed.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "site"))
    return sys.executable


def test_the_answer_goes_into_local_toml(tmp_path: Path, written, monkeypatch, capsys, python_with_mbed_cli):
    monkeypatch.setattr(_init_interactive, "_input", lambda prompt: python_with_mbed_cli)

    _init_interactive.resolve_build_env(tmp_path, _cfg(), "mbed-os", non_interactive=False, dry_run=False)

    assert written == [("python", python_with_mbed_cli)]
    assert f"[ok] Build python: {python_with_mbed_cli}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("answer", "problem"),
    [
        pytest.param("missing", "the file does not exist", id="no-file"),
        pytest.param(sys.executable, "it cannot import mbed-cli", id="no-mbed-cli"),
    ],
)
def test_a_wrong_answer_is_not_written(tmp_path: Path, written, monkeypatch, capsys, answer, problem):
    """A typing error shows at init, and not at the first index run."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    path = str(tmp_path / answer) if answer == "missing" else answer
    monkeypatch.setattr(_init_interactive, "_input", lambda prompt: path)

    _init_interactive.resolve_build_env(tmp_path, _cfg(), "mbed-os", non_interactive=False, dry_run=False)

    out = capsys.readouterr().out
    assert f"{path}: {problem}" in out
    assert "The Mbed OS build needs [build] python" in out
    assert written == []


def test_activate_without_python_still_asks(tmp_path: Path, written, monkeypatch, python_with_mbed_cli):
    """An Mbed OS project with only activate configured: the build still needs python."""
    cfg = SimpleNamespace(build=SimpleNamespace(python=None, activate="/opt/env.sh", idf_path=None))
    monkeypatch.setattr(_init_interactive, "_input", lambda prompt: python_with_mbed_cli)

    _init_interactive.resolve_build_env(tmp_path, cfg, "mbed-os", non_interactive=False, dry_run=False)

    assert written == [("python", python_with_mbed_cli)]


def test_a_detected_python_needs_no_question(tmp_path: Path, written, monkeypatch, capsys):
    monkeypatch.setattr(
        MbedOSBuildSystem, "detect_environment",
        classmethod(lambda cls, root: {"python": "/found/bin/python", "activate": None}),
    )
    monkeypatch.setattr(_init_interactive, "_input", lambda prompt: pytest.fail(f"asked: {prompt}"))

    _init_interactive.resolve_build_env(tmp_path, _cfg(), "mbed-os", non_interactive=False, dry_run=False)

    assert written == [("python", "/found/bin/python")]
    assert "No Python with mbed-cli" not in capsys.readouterr().out
