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


# 'a\\"b': the batch file gives its arguments on to a program (%*), and the
# C runtime of that program reads \\" as a quote; the backslash was lost.
ARGUMENTS_WITH_CMD_CHARACTERS = [
    r"C:\R&D\fw", "100%", "%PATH%", 'a "quoted" b', "x|y<z>w", "^caret!", "trail\\", 'a\\"b', 'c\\\\"d',
]


@windows_only
def test_a_batch_program_gets_each_argument_as_it_is(tmp_path: Path):
    """BatBadBut: cmd.exe ran the text after `&` of an argument; list2cmdline does not quote for cmd.exe."""
    from tests._stubs import write_python_tool

    out = tmp_path / "argv.json"
    tool = write_python_tool(
        tmp_path / "tools", "fwargs", f"import json, sys\nopen({str(out)!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
        batch=True,
    )
    assert tool.suffix == ".cmd"

    result = utils.run_in_process_group(
        [str(tool), *ARGUMENTS_WITH_CMD_CHARACTERS], cwd=tmp_path, env=dict(os.environ),
        timeout=60.0, capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == ARGUMENTS_WITH_CMD_CHARACTERS


@pytest.mark.parametrize(
    ("batch", "spelling"),
    [
        (False, "relative"),
        pytest.param(True, "relative", marks=windows_only),
        pytest.param(False, "no-extension", marks=windows_only),
        pytest.param(True, "no-extension", marks=windows_only),
        pytest.param(False, "root-no-drive", marks=windows_only),
    ],
    ids=["exe", "batch", "exe-no-extension", "batch-no-extension", "root-no-drive"],
)
def test_a_program_with_a_directory_is_found_from_the_directory_of_the_build(
    tmp_path: Path, monkeypatch, batch: bool, spelling: str,
):
    """``tools/gen`` of a build command: the directory of the build, not the directory of fw-context.

    On Windows ``CreateProcess`` read it from the current directory of this
    process.  The MCP server runs outside of the project, and the program
    was not found.  ``Popen`` on POSIX reads it from the directory of the
    build, and a batch file through ``cmd.exe`` did too.  ``tools\\gen``
    with no extension found no ``gen.cmd``: ``CreateProcess`` adds only
    ``.exe``.  ``\\tools\\gen.exe`` (a root, no drive) took the drive from
    the version of Python.  The batch cases apply only to Windows: on POSIX
    ``write_python_tool`` makes the same script for the two.
    """
    from tests._stubs import write_python_tool

    build_dir = tmp_path / "project"
    out = tmp_path / "ran.txt"
    tool = write_python_tool(
        build_dir / "tools", "gen", f"open({str(out)!r}, 'w').write('ran')\n", batch=batch,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    if spelling == "relative":
        program = str(tool.relative_to(build_dir))
    elif spelling == "no-extension":
        program = str(tool.relative_to(build_dir).with_suffix(""))
    else:
        program = str(tool)[len(tool.drive):]  # \Users\...\gen.exe: a root, no drive
    result = utils.run_in_process_group(
        [program], cwd=build_dir, env=dict(os.environ), timeout=60.0, capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert out.read_text(encoding="utf-8") == "ran"


@windows_only
def test_a_path_with_a_root_and_no_drive_gets_the_drive_of_the_build():
    """``\\tools\\gen.exe`` with the build on D: is ``D:\\tools\\gen.exe``, on each version of Python."""
    assert utils._with_drive_of("\\tools\\gen.exe", Path("D:/proj")) == "D:\\tools\\gen.exe"
    assert utils._with_drive_of("tools\\gen.exe", Path("D:/proj")) == "D:\\proj\\tools\\gen.exe"
    assert utils._with_drive_of("C:\\sdk\\gen.exe", Path("D:/proj")) == "C:\\sdk\\gen.exe"


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


@pytest.mark.skipif(sys.platform == "win32", reason="the rules of POSIX")
def test_a_space_is_a_part_of_a_posix_path_entry():
    """execvp reads '/opt/my tools ' as a directory with that name."""
    assert utils.path_directories({"PATH": "/opt/my tools :/usr/bin"}) == [Path("/opt/my tools "), Path("/usr/bin")]


@windows_only
def test_a_quoted_windows_path_entry_is_a_directory():
    assert utils.path_directories({"Path": ' "C:\\Program Files\\x" ;C:\\y'}) == [Path(r"C:\Program Files\x"), Path(r"C:\y")]


# ── The command field of compile_commands.json ────────────────────────────

WINDOWS_COMMAND = r'"C:\Program Files\tc\bin\arm-none-eabi-gcc.exe" -IC:\proj\inc -DX=1 -c C:\proj\src\main.c'


def test_the_compiler_of_a_windows_command_field(monkeypatch):
    """The build started the compiler with the rules of Windows; shlex removed each backslash of a bare name."""
    from fw_context_mcp.indexer._driver_query import compiler_token

    monkeypatch.setattr(sys, "platform", "win32")

    assert compiler_token({"command": WINDOWS_COMMAND}) == r"C:\Program Files\tc\bin\arm-none-eabi-gcc.exe"
    assert compiler_token({"command": r"C:\tc\gcc.exe -c a.c"}) == r"C:\tc\gcc.exe"


def test_the_flags_of_a_windows_command_field_keep_their_backslashes(monkeypatch):
    """The identity of a build (config_hash) read -IC:projinc before."""
    from fw_context_mcp.indexer.config_hash import DEFAULT_TRANSIENT_DEFINES, _normalize_entry

    monkeypatch.setattr(sys, "platform", "win32")

    entry = {"directory": r"C:\proj", "file": r"C:\proj\src\main.c", "command": WINDOWS_COMMAND}
    args = _normalize_entry(entry, DEFAULT_TRANSIENT_DEFINES)["args"]

    assert r"-IC:\proj\inc" in args


@windows_only
def test_a_windows_command_field_gives_the_include_directory(tmp_path: Path):
    """The parse of compile_commands.json: the unit gets the directory as the build wrote it."""
    from fw_context_mcp.indexer.compile_commands import parse

    include = tmp_path / "inc"
    include.mkdir()
    source = tmp_path / "main.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    database = tmp_path / "compile_commands.json"
    database.write_text(json.dumps([{
        "directory": str(tmp_path), "file": str(source),
        "command": f'"{sys.executable}" "-I{include}" -c "{source}"',
    }]), encoding="utf-8")

    unit = next(parse(database))

    assert f"-I{include}" in unit.clang_args


def test_the_defines_of_a_command_field_lose_their_quotes():
    """str.split kept the quotes: -DVERSION=\\"1.2\\" gave \\"1.2\\", and "a b" gave two words."""
    from fw_context_mcp.indexer.compile_commands import _defines_of_entry

    entry = {"command": 'cc -DVERSION=\\"1.2\\" "-DNAME=a b" -c a.c'}

    assert _defines_of_entry(entry) == {"VERSION": '"1.2"', "NAME": "a b"}


def test_an_output_path_with_a_space_is_one_word():
    from types import SimpleNamespace

    from fw_context_mcp.indexer.builders._linker import _output_paths

    unit = SimpleNamespace(raw_entry={"command": 'cc -c a.c -o "out dir/a.o"'})

    assert _output_paths(unit) == ["out dir/a.o"]


@windows_only
@pytest.mark.parametrize("spelling", ["bare-name", "relative"])
def test_a_python_file_of_pathext_is_no_program_of_a_build(tmp_path: Path, monkeypatch, spelling: str):
    """``PATHEXT`` holds ``.PY`` after an install of Python, and ``gen`` found ``gen.py``.

    ``CreateProcess`` cannot start a ``.py`` file (WinError 193, "not a valid
    Win32 application"), and that error did not tell that no program was
    found.  Now the lookup takes only an ``.exe``, ``.com``, ``.bat`` or
    ``.cmd`` file, and the build gets FileNotFoundError.
    """
    build_dir = tmp_path / "project"
    (build_dir / "tools").mkdir(parents=True)
    (build_dir / "tools" / "gen.py").write_text("print('no')\n", encoding="utf-8")
    env = dict(os.environ)
    env["PATHEXT"] = ".COM;.EXE;.BAT;.CMD;.PY"
    if spelling == "bare-name":
        env["PATH"] = str(build_dir / "tools") + os.pathsep + env.get("PATH", "")
        program = "gen"
    else:
        program = "tools\\gen"

    with pytest.raises(FileNotFoundError):
        utils.run_in_process_group([program], cwd=build_dir, env=env, timeout=60.0, capture_output=True)


@windows_only
def test_idf_py_runs_idf_py_exe_when_pathext_holds_py(tmp_path: Path):
    """ESP-IDF gives ``idf.py`` and ``idf.py.exe``, and ``PATHEXT`` holds ``.PY`` after an install of Python.

    The name had an extension of ``PATHEXT`` and stayed as it was; the
    filter of the files that Windows can start then left no name, and each
    ESP-IDF build failed with "idf.py is not on the PATH".  The ``.py`` file
    is no program of a build; ``idf.py.exe`` is.
    """
    from tests._stubs import write_python_tool

    tools = tmp_path / "tools"
    out = tmp_path / "ran.txt"
    exe = write_python_tool(tools, "idf.py", f"open({str(out)!r}, 'w').write('exe')\n")
    assert exe.name == "idf.py.exe"
    (tools / "idf.py").write_text("raise SystemExit('the .py file ran')\n", encoding="utf-8")
    env = dict(os.environ)
    env["PATHEXT"] = ".COM;.EXE;.BAT;.CMD;.PY"
    env["PATH"] = str(tools) + os.pathsep + env.get("PATH", "")

    result = utils.run_in_process_group(["idf.py"], cwd=tmp_path, env=env, timeout=60.0, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert out.read_text(encoding="utf-8") == "exe"


@pytest.mark.parametrize(
    ("name", "pathext", "expected"),
    [
        ("gen", ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.PY", ["gen.com", "gen.exe", "gen.bat", "gen.cmd"]),
        ("idf.py", ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.PY", ["idf.py.com", "idf.py.exe", "idf.py.bat", "idf.py.cmd"]),
        ("gcc.exe", ".COM;.EXE;.BAT;.CMD;.PY", ["gcc.exe"]),
        ("west.CMD", ".COM;.EXE;.BAT;.CMD", ["west.CMD"]),
        ("gcc", None, ["gcc.com", "gcc.exe", "gcc.bat", "gcc.cmd"]),
        ("gcc", ".PY;.JS", ["gcc.com", "gcc.exe", "gcc.bat", "gcc.cmd"]),
        ("gcc", ".EXE;.exe;.CMD;.EXE", ["gcc.exe", "gcc.cmd"]),
    ],
    ids=["bare", "py-name", "exe-name", "cmd-name", "no-pathext", "pathext-of-scripts", "repeated"],
)
def test_the_file_names_of_a_program_are_files_that_windows_can_start(monkeypatch, name, pathext, expected):
    """``program_file_names`` on Windows: only files that Windows can start, in the order of ``PATHEXT``, one time each.

    A pure function of its arguments, thus the test runs on each system with
    the platform of Windows.  A ``PATHEXT`` of scripts only (``.PY``) gave no
    name, and no program was found.
    """
    monkeypatch.setattr(utils.sys, "platform", "win32")
    env = {} if pathext is None else {"PATHEXT": pathext}
    assert utils.program_file_names(name, env) == expected
