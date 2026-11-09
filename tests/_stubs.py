"""Tools and activate scripts for tests, in the form of the host.

A test that needs a program on PATH wrote a ``#!/bin/sh`` file and set its
execute bit.  Windows runs no such file: it finds a program through the
extensions of ``PATHEXT`` (``.exe``, ``.cmd``, ``.bat``), and an activate
script there is a ``.bat`` file.  These helpers write the form of the host,
thus one test runs on each system.

- ``write_tool``: a program that exits with a code.
- ``write_python_tool``: a program whose behaviour is Python code, for a
  stub that must read its arguments (a compiler driver, ``nrfutil``).
- ``write_activate``: an activate script that puts directories at the
  start of PATH, sets variables, or fails.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

WINDOWS = sys.platform == "win32"


def _executable(path: Path) -> Path:
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def write_tool(directory: Path, name: str, *, exit_code: int = 0) -> Path:
    """Write the program *name* that exits with *exit_code*; return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        tool = directory / f"{name}.cmd"
        tool.write_text(f"@exit /b {exit_code}\r\n", encoding="utf-8")
        return tool
    tool = directory / name
    tool.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    return _executable(tool)


def write_python_tool(directory: Path, name: str, code: str) -> Path:
    """Write the program *name* that runs the Python *code* with the interpreter of the tests; return its path.

    The code gets the arguments in ``sys.argv[1:]``.  POSIX: one file with a
    shebang.  Windows: ``<name>.py`` and the launcher ``<name>.cmd``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        (directory / f"{name}.py").write_text(code, encoding="utf-8")
        tool = directory / f"{name}.cmd"
        tool.write_text(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n@exit /b %ERRORLEVEL%\r\n', encoding="utf-8")
        return tool
    tool = directory / name
    tool.write_text(f"#!{sys.executable}\n{code}", encoding="utf-8")
    return _executable(tool)


def activate_name() -> str:
    """The file name of an activate script of the host."""
    return "env.bat" if WINDOWS else "env.sh"


def write_activate(
    path: Path,
    *,
    prepend_path: Sequence[Path] = (),
    path_only: list[str] | None = None,
    variables: dict[str, str] | None = None,
    fail: bool = False,
) -> Path:
    """Write the activate script *path* (``.sh`` or ``.bat``, see ``activate_name``).

    *prepend_path* goes before PATH.  *path_only* replaces PATH.  *fail*
    makes the script end with a failure after its other lines.
    """
    lines: list[str] = []
    if WINDOWS:
        lines.append("@echo off")
        if path_only is not None:
            lines.append(f'set "PATH={os.pathsep.join(path_only)}"')
        for directory in prepend_path:
            lines.append(f'set "PATH={directory}{os.pathsep}%PATH%"')
        for name, value in (variables or {}).items():
            lines.append(f'set "{name}={value}"')
        if fail:
            lines.append("exit /b 1")
        path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return path
    if path_only is not None:
        lines.append(f'export PATH="{os.pathsep.join(path_only)}"')
    for directory in prepend_path:
        lines.append(f'export PATH="{directory}{os.pathsep}$PATH"')
    for name, value in (variables or {}).items():
        lines.append(f'export {name}="{value}"')
    if fail:
        lines.append("return 1")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def system_path() -> list[str]:
    """The directories of PATH that hold the programs of the system, and no tool of a test."""
    if WINDOWS:
        root = os.environ["SystemRoot"]
        return [str(Path(root, "System32")), root]
    return ["/usr/bin", "/bin"]
