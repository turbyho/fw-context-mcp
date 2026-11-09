"""The command lines of the config and the program of a build, by the rules of the host.

``[build] pre_build`` and ``[build] command`` are command lines.  POSIX
splits them as its shell does (``shlex``); Windows as ``CommandLineToArgvW``
does.  ``shlex`` on Windows removed each backslash: ``C:\\tools\\pre.bat``
became ``C:toolspre.bat``.

A build on Windows gets its program as a full path from the ``PATH`` of the
build: ``CreateProcess`` looks in the ``PATH`` of the parent and adds only
``.exe``, thus a tool of ``extra_path`` or a ``.cmd`` tool did not start.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from fw_context_mcp import utils

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the rules of Windows")


@pytest.mark.parametrize(("text", "program", "rest"), [
    (r"C:\tools\pre.bat --x", r"C:\tools\pre.bat", " --x"),
    (r'"C:\Program Files\x\y.exe" -DA=1', r"C:\Program Files\x\y.exe", " -DA=1"),
    # A backslash is literal in the program name, also before the quote.
    ('"C:\\tools\\" a', "C:\\tools\\", " a"),
    ("  west build", "west", " build"),
    ("west", "west", ""),
    ('"no end', "no end", ""),
])
def test_the_program_name_of_a_windows_command_line(text: str, program: str, rest: str):
    assert utils._windows_program_word(text) == (program, rest)


@pytest.mark.skipif(sys.platform == "win32", reason="the rules of POSIX")
def test_a_posix_command_line_is_split_as_the_shell_does():
    assert utils.command_words("sh -c 'echo a b' \"c d\"") == ["sh", "-c", "echo a b", "c d"]


@windows_only
@pytest.mark.parametrize(("text", "words"), [
    (r'C:\tools\pre.bat --out "C:\My Build\x" -DA="1 2"', [r"C:\tools\pre.bat", "--out", r"C:\My Build\x", "-DA=1 2"]),
    (r'"C:\Program Files\cmake\bin\cmake.exe" --build .', [r"C:\Program Files\cmake\bin\cmake.exe", "--build", "."]),
    ("", []),
])
def test_a_windows_command_line_is_split_as_command_line_to_argv(text: str, words: list[str]):
    assert utils.command_words(text) == words


def _cmd_tool(directory: Path, name: str, output: str) -> Path:
    """A ``.cmd`` tool that prints *output*, as pip makes ``west.cmd`` or ``idf.py`` shims."""
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / f"{name}.cmd"
    tool.write_text(f"@echo {output}\r\n", encoding="utf-8")
    return tool


@windows_only
def test_a_cmd_tool_on_the_path_of_the_build_runs(tmp_path: Path):
    """The tool is only on the PATH of the build (extra_path), not of this process."""
    tools = tmp_path / "tools"
    _cmd_tool(tools, "fwtool", "from-the-build-path")
    env = {**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ.get('PATH', '')}"}

    result = utils.run_in_process_group(["fwtool"], cwd=tmp_path, env=env, timeout=60.0, capture_output=True)

    assert (result.returncode, result.stdout.strip()) == (0, "from-the-build-path")


@windows_only
def test_a_tool_that_the_path_of_the_build_does_not_hold_is_not_found(tmp_path: Path):
    env = {**os.environ, "PATH": str(tmp_path / "empty")}

    with pytest.raises(FileNotFoundError, match="fw-context-no-such-tool"):
        utils.run_in_process_group(
            ["fw-context-no-such-tool"], cwd=tmp_path, env=env, timeout=60.0, capture_output=True,
        )


ARGUMENTS_WITH_CMD_CHARACTERS = [r"C:\R&D\fw", "100%", "%PATH%", 'a "quoted" b', "x|y<z>w", "^caret!", "trail\\"]


@windows_only
def test_a_batch_program_gets_each_argument_as_it_is(tmp_path: Path):
    """BatBadBut: cmd.exe ran the text after `&` of an argument; list2cmdline does not quote for cmd.exe."""
    from tests._stubs import write_python_tool

    out = tmp_path / "argv.json"
    tool = write_python_tool(
        tmp_path / "tools", "fwargs", f"import json, sys\nopen({str(out)!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
    )
    assert tool.suffix == ".cmd"

    result = utils.run_in_process_group(
        [str(tool), *ARGUMENTS_WITH_CMD_CHARACTERS], cwd=tmp_path, env=dict(os.environ),
        timeout=60.0, capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == ARGUMENTS_WITH_CMD_CHARACTERS


@windows_only
def test_a_line_break_cannot_go_to_a_batch_program(tmp_path: Path):
    tool = tmp_path / "x.cmd"
    tool.write_text("@exit /b 0\r\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line break"):
        utils.run_in_process_group([str(tool), "a\nb"], cwd=tmp_path, env=dict(os.environ), timeout=60.0,
                                   capture_output=True)


@windows_only
def test_the_current_directory_of_this_process_is_not_the_path_of_the_build(tmp_path: Path, monkeypatch):
    """shutil.which of Python 3.11 looks in the current directory first; the build must not."""
    _cmd_tool(tmp_path, "fwcwdtool", "from-the-cwd")
    monkeypatch.chdir(tmp_path)
    env = {**os.environ, "PATH": str(tmp_path / "empty")}

    with pytest.raises(FileNotFoundError):
        utils.run_in_process_group(["fwcwdtool"], cwd=tmp_path, env=env, timeout=60.0, capture_output=True)


def test_a_build_gets_no_stdin(tmp_path: Path):
    """In an MCP server the stdin is the JSON-RPC pipe of the client: a build must not read it."""
    probe = [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"]

    result = utils.run_in_process_group(probe, cwd=tmp_path, env=dict(os.environ), timeout=60.0, capture_output=True)

    assert result.stdout.strip() == "''"
