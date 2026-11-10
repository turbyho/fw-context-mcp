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
- ``remove_tree``: remove a directory tree that holds read-only files, as
  the objects of a git repository.
"""

from __future__ import annotations

import os
import shutil
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


def write_python_tool(directory: Path, name: str, code: str, *, batch: bool = False) -> Path:
    """Write the program *name* that runs the Python *code* with the interpreter of the tests; return its path.

    The code gets the arguments in ``sys.argv[1:]``.  POSIX: one file with a
    shebang.  Windows: ``<name>.exe``, as pip makes a console script (the
    launcher of distlib, a shebang and a zip with ``__main__.py``).  WHY an
    ``.exe``: a compiler, ``nrfutil`` or ``west`` is an ``.exe`` there, and
    the product treats a batch file in another way (``cmd.exe``).  *batch*
    gives ``<name>.cmd`` (and ``<name>.py``) on Windows, for a test of that
    other way.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if WINDOWS and batch:
        (directory / f"{name}.py").write_text(code, encoding="utf-8")
        tool = directory / f"{name}.cmd"
        tool.write_text(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n@exit /b %ERRORLEVEL%\r\n', encoding="utf-8")
        return tool
    if WINDOWS:
        tool = directory / f"{name}.exe"
        tool.write_bytes(_console_script(code))
        return tool
    tool = directory / name
    tool.write_text(f"#!{sys.executable}\n{code}", encoding="utf-8")
    return _executable(tool)


def _console_script(code: str) -> bytes:
    """The bytes of a Windows console script that runs *code*: the format of distlib, which pip uses.

    The launcher (``t64.exe``, or ``t64-arm.exe`` on ARM64) reads the
    shebang line and the zip archive after its own bytes, and runs
    ``__main__.py`` of the archive with the interpreter of the shebang.
    The launcher comes from the distlib in pip, thus the tests need pip,
    as each virtual environment has it.
    """
    import io
    import platform
    import zipfile

    import pytest

    try:
        from pip._vendor import distlib
    except ImportError as exc:
        pytest.skip(f"no launcher of distlib for a console script: {exc}")
    launcher = "t64-arm.exe" if platform.machine().lower() in ("arm64", "aarch64") else "t64.exe"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("__main__.py", code)
    shebang = f'#!"{sys.executable}"\n'.encode()
    return (Path(distlib.__file__).parent / launcher).read_bytes() + shebang + archive.getvalue()


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


def program_name(name: str) -> str:
    """The file name of the program *name* on this system: ``name.exe`` on Windows, *name* elsewhere."""
    return f"{name}.exe" if WINDOWS else name


def symlink_or_skip(link: Path, target: Path) -> None:
    """Make the symbolic link *link* to *target*, or skip the test where the system refuses it.

    Windows makes a symbolic link only with the privilege of it (an
    administrator, or the developer mode).  The test then cannot set up its
    case, which is not a failure of the code under test.
    """
    import pytest

    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"this system makes no symbolic link: {exc}")


def remove_tree(path: Path) -> None:
    """Remove the directory tree *path*, also its read-only files.

    WHY: git writes its objects read-only.  Windows does not delete a
    read-only file, and ``shutil.rmtree`` stops with WinError 5: the
    handler then makes the file writable and deletes it again.  POSIX
    deletes a read-only file in a writable directory, thus the handler
    gets no error from such a file there.  An error that is not a
    ``PermissionError`` stays an error.
    """

    def _make_writable_and_retry(function, name, error) -> None:
        # *error* is the exception (onexc of Python 3.12) or its exc_info
        # (onerror of Python 3.11).
        exc = error if isinstance(error, BaseException) else error[1]
        if not isinstance(exc, PermissionError):
            raise exc
        os.chmod(name, stat.S_IWRITE)
        function(name)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=_make_writable_and_retry)
