"""The search for the fw-context-mcp program that init registers as the MCP server.

On Windows each program of a venv, of pip, pipx and ``uv tool`` has the
extension ``.exe``, and the programs of a Python are in ``Scripts``.  The
search looked only for ``bin/fw-context-mcp`` and for the program beside
``sys.executable``, thus init registered no server when ``Scripts`` was
not on PATH.
"""

from __future__ import annotations

import shutil
import sys
import sysconfig
from pathlib import Path

import pytest

from fw_context_mcp.cli import _mcp

_HOSTS = [
    pytest.param("win32", "fw-context-mcp.exe", ("Scripts", "fw-context-mcp.exe"), id="windows"),
    pytest.param("linux", "fw-context-mcp", ("bin", "fw-context-mcp"), id="posix"),
]


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """An empty home, no program on PATH, and the scripts directories of a Python that has none."""
    places = {
        "home": tmp_path / "home",
        "scripts": tmp_path / "python" / "Scripts",
        "user": tmp_path / "user" / "Scripts",
    }
    for place in places.values():
        place.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: places["home"]))
    monkeypatch.setattr(shutil, "which", lambda name, path=None: None)

    def get_path(name, scheme=None):
        assert name == "scripts"
        return str(places["scripts"] if scheme is None else places["user"])

    monkeypatch.setattr(sysconfig, "get_path", get_path)
    return places


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


@pytest.mark.parametrize(("platform", "name", "venv_path"), _HOSTS)
def test_the_program_of_pipx_or_uv_tool(dirs, monkeypatch, platform, name, venv_path):
    monkeypatch.setattr(sys, "platform", platform)
    program = _touch(dirs["home"] / ".local" / "bin" / name)

    assert _mcp._resolve_mcp_bin() == str(program)


@pytest.mark.parametrize(("platform", "name", "venv_path"), _HOSTS)
def test_the_program_of_the_managed_venv(dirs, monkeypatch, platform, name, venv_path):
    monkeypatch.setattr(sys, "platform", platform)
    program = _touch(dirs["home"] / ".fw-context" / ".venv" / Path(*venv_path))

    assert _mcp._resolve_mcp_bin() == str(program)


@pytest.mark.parametrize(("platform", "name", "venv_path"), _HOSTS)
def test_the_program_of_the_running_python(dirs, monkeypatch, platform, name, venv_path):
    """A venv, an editable install, or a system Python such as C:\\Python314 with its Scripts."""
    monkeypatch.setattr(sys, "platform", platform)
    program = _touch(dirs["scripts"] / name)

    assert _mcp._resolve_mcp_bin() == str(program)


@pytest.mark.parametrize(("platform", "name", "venv_path"), _HOSTS)
def test_the_program_of_pip_install_user(dirs, monkeypatch, platform, name, venv_path):
    monkeypatch.setattr(sys, "platform", platform)
    program = _touch(dirs["user"] / name)

    assert _mcp._resolve_mcp_bin() == str(program)


def test_no_program_gives_none(dirs, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    assert _mcp._resolve_mcp_bin() is None
