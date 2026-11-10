"""The detection of a venv Python on Windows and on POSIX (utils.venv_program).

The ``venv`` module puts the programs into ``Scripts`` with ``.exe`` on
Windows, and into ``bin`` on POSIX.  The detection looked only for
``bin/python``, thus ``fw-context init`` found no PlatformIO and no
mbed-cli on Windows.
"""

from __future__ import annotations

import shutil
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

from fw_context_mcp.indexer.builders._environment_help import build_setting
from fw_context_mcp.indexer.builders.mbed_os import MbedOSBuildSystem
from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem
from fw_context_mcp.utils import venv_program

_LAYOUTS = [
    pytest.param("win32", ("Scripts", "python.exe"), ("Scripts", "mbed.exe"), id="windows"),
    pytest.param("linux", ("bin", "python"), ("bin", "mbed"), id="posix"),
]


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name, path=None: None)
    return tmp_path


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


@pytest.mark.parametrize(("platform", "python", "mbed"), _LAYOUTS)
def test_venv_program_follows_the_layout_of_the_host(monkeypatch, platform, python, mbed):
    monkeypatch.setattr(sys, "platform", platform)

    assert venv_program(Path("v"), "python") == Path("v", *python)
    assert venv_program(Path("v"), "mbed") == Path("v", *mbed)


@pytest.mark.parametrize(("platform", "python", "mbed"), _LAYOUTS)
def test_platformio_finds_its_venv(home: Path, monkeypatch, platform, python, mbed):
    monkeypatch.setattr(sys, "platform", platform)
    expected = _touch(home / ".platformio" / "penv" / Path(*python))

    assert PlatformIOBuildSystem.detect_environment(home) == {"python": str(expected), "activate": None}


@pytest.mark.parametrize(("platform", "python", "mbed"), _LAYOUTS)
def test_mbed_finds_a_venv_with_mbed_cli(home: Path, monkeypatch, platform, python, mbed):
    monkeypatch.setattr(sys, "platform", platform)
    expected = _touch(home / "mbed_venv" / Path(*python))
    _touch(home / "mbed_venv" / Path(*mbed))

    assert MbedOSBuildSystem.detect_environment(home) == {"python": str(expected), "activate": None}


@pytest.mark.parametrize(("platform", "python", "mbed"), _LAYOUTS)
def test_mbed_ignores_a_venv_without_mbed_cli(home: Path, monkeypatch, platform, python, mbed):
    """A venv without the mbed command would give the build a Python without mbed-cli."""
    monkeypatch.setattr(sys, "platform", platform)
    _touch(home / "mbed_venv" / Path(*python))

    assert MbedOSBuildSystem.detect_environment(home) == {"python": None, "activate": None}


def test_the_help_shows_the_venv_of_windows(home: Path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    text = MbedOSBuildSystem.environment_help()

    assert "py -3.11 -m venv" in text
    assert f"{home / 'mbed_venv' / 'Scripts' / 'pip.exe'} install mbed-cli" in text
    assert _setting_of(text) == {"build": {"python": str(home / "mbed_venv" / "Scripts" / "python.exe")}}
    assert _setting_of(PlatformIOBuildSystem.environment_help()) == {
        "build": {"python": str(Path("/path/to/pio/venv") / "Scripts" / "python.exe")}
    }


def _setting_of(help_text: str) -> dict:
    """Return the TOML that the help gives after "local.toml:", as the user copies it."""
    return tomllib.loads(textwrap.dedent(help_text.split("local.toml:\n", 1)[1]))


def test_the_setting_in_the_help_is_valid_toml_for_a_windows_path():
    """A Windows path in a basic TOML string needs escapes: the user copies these lines."""
    path = r"C:\Users\user\mbed_venv\Scripts\python.exe"

    assert tomllib.loads(textwrap.dedent(build_setting("python", path))) == {"build": {"python": path}}
