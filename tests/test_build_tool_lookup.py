"""A build looks for its tools in the environment of the project.

With ``[build] activate``, a tool is on PATH only after the activate script:
the activate script of NCS puts ``west`` and ``ninja`` there.  The lookup
ran ``shutil.which`` on the PATH of the fw-context process, thus a build
with a working activate script failed with "west is required", and a
``west`` from outside the environment could name a different workspace.

POSIX sources the script before each command (``bash -c "source ..."``).
Windows runs the script (``.bat``, ``.cmd`` or ``.ps1``), reads the
environment that it leaves, and runs the command with that environment,
without a shell.  The tests write the script and the tools in the form of
the host (``tests/_stubs.py``), thus the same tests run on each system.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from fw_context_mcp.indexer.build import BuildConfig
from fw_context_mcp.utils import build_tool_available, missing_tool_message, run_build_command
from tests._stubs import WINDOWS, activate_name, system_path, write_activate, write_tool

# A name that no system has, so that a west of the system cannot answer.
TOOL = "fwctx-test-west"

windows_only = pytest.mark.skipif(not WINDOWS, reason="the activate scripts of Windows")

PRINT_SOURCE = [sys.executable, "-c", "import os; print(os.environ['FWCTX_SOURCE'])"]


def _tool_dir(tmp_path: Path, name: str) -> Path:
    """A directory with the program *name* in it, which no PATH holds."""
    directory = tmp_path / "toolchain" / "bin"
    write_tool(directory, name)
    return directory


@pytest.fixture
def path_without_west(monkeypatch) -> None:
    """A PATH with the system tools and without the test tool."""
    monkeypatch.setenv("PATH", os.pathsep.join(system_path()))


def test_a_relative_activate_is_the_script_of_the_project(tmp_path: Path, monkeypatch) -> None:
    """`source env.sh` looks in PATH first: the env.sh of the project must win.

    A relative activate is relative to the directory of the build.  Bash
    reads a name without a slash from PATH before the current directory,
    thus a different env.sh on PATH set up a different environment.
    """
    other = tmp_path / "elsewhere"
    other.mkdir()
    write_activate(other / activate_name(), variables={"FWCTX_SOURCE": "path"})
    project = tmp_path / "project"
    project.mkdir()
    write_activate(project / activate_name(), variables={"FWCTX_SOURCE": "project"})
    monkeypatch.setenv("PATH", os.pathsep.join([str(other), *system_path()]))

    result = run_build_command(PRINT_SOURCE, cwd=project, build_cfg=BuildConfig(activate=activate_name()))

    assert result.stdout.strip() == "project"


@pytest.mark.usefixtures("path_without_west")
def test_a_tool_that_only_the_activate_script_gives_is_found(tmp_path: Path) -> None:
    tools = _tool_dir(tmp_path, TOOL)
    script = write_activate(tmp_path / activate_name(), prepend_path=[tools])

    assert build_tool_available(TOOL, tmp_path, BuildConfig(activate=str(script)))


@pytest.mark.usefixtures("path_without_west")
def test_without_activate_the_tool_of_that_script_is_not_found(tmp_path: Path) -> None:
    _tool_dir(tmp_path, TOOL)

    assert not build_tool_available(TOOL, tmp_path, BuildConfig())


@pytest.mark.usefixtures("path_without_west")
def test_extra_path_of_the_build_counts_without_activate(tmp_path: Path) -> None:
    tools = _tool_dir(tmp_path, TOOL)

    assert build_tool_available(TOOL, tmp_path, BuildConfig(extra_path=[str(tools)]))


@pytest.mark.usefixtures("path_without_west")
def test_a_failed_activate_script_finds_no_tool(tmp_path: Path) -> None:
    tools = _tool_dir(tmp_path, TOOL)
    script = write_activate(tmp_path / activate_name(), prepend_path=[tools], fail=True)

    assert not build_tool_available(TOOL, tmp_path, BuildConfig(activate=str(script)))


def test_the_tool_on_the_path_of_the_process_does_not_count_with_activate(tmp_path: Path, monkeypatch) -> None:
    """A west outside the environment of the project must not answer for it."""
    host = _tool_dir(tmp_path, TOOL)
    monkeypatch.setenv("PATH", os.pathsep.join([str(host), *system_path()]))
    script = write_activate(tmp_path / activate_name(), path_only=system_path())

    assert not build_tool_available(TOOL, tmp_path, BuildConfig(activate=str(script)))


def test_a_build_command_runs_the_tool_of_the_activate_script(tmp_path: Path, monkeypatch) -> None:
    """The build itself, not only the lookup: the tool is only on the PATH of the script."""
    tools = _tool_dir(tmp_path, TOOL)
    monkeypatch.setenv("PATH", os.pathsep.join(system_path()))
    script = write_activate(tmp_path / activate_name(), prepend_path=[tools])

    result = run_build_command([TOOL], cwd=tmp_path, build_cfg=BuildConfig(activate=str(script)))

    assert result.returncode == 0


def test_a_failed_activate_script_fails_the_build(tmp_path: Path) -> None:
    script = write_activate(tmp_path / activate_name(), variables={"FWCTX_SOURCE": "x"}, fail=True)

    with pytest.raises(RuntimeError):
        run_build_command(PRINT_SOURCE, cwd=tmp_path, build_cfg=BuildConfig(activate=str(script)))


@pytest.mark.usefixtures("path_without_west")
def test_a_zephyr_build_uses_the_west_of_the_activate_script(tmp_path: Path, monkeypatch) -> None:
    """THE regression: the build stopped with "west is required" before activate ran."""
    import fw_context_mcp.indexer.builders.zephyr as zephyr

    tools = _tool_dir(tmp_path, "west")
    script = write_activate(tmp_path / activate_name(), prepend_path=[tools])
    built: list[list[str]] = []

    def fake_build(cmd, cwd=None, description="", env=None, build_cfg=None, timeout=None):
        built.append([str(c) for c in cmd])
        out = Path(cmd[cmd.index("-d") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "compile_commands.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr(zephyr, "run_build_command", fake_build)

    zephyr.ZephyrBuildSystem().build(
        tmp_path, BuildConfig(board="nrf52840dk/nrf52840", activate=str(script)),
    )

    assert built and built[0][:2] == ["west", "build"]


@pytest.mark.skipif(WINDOWS, reason="the POSIX message names `source`")
def test_the_message_names_the_activate_script(tmp_path: Path) -> None:
    cfg = BuildConfig(activate="/opt/sdk/env.sh")

    message = missing_tool_message("west", cfg, "Install west.")

    assert "source /opt/sdk/env.sh" in message
    assert "Install west." not in message


@windows_only
def test_the_windows_message_names_the_activate_script() -> None:
    message = missing_tool_message("west", BuildConfig(activate=r"C:\ncs\env.bat"), "Install west.")

    assert r"activate script C:\ncs\env.bat" in message
    assert "Install west." not in message


def test_the_message_without_activate_gives_the_install_hint() -> None:
    assert missing_tool_message("west", BuildConfig(), "Install west.") == "west is required.  Install west."


# ── Windows: the forms of the activate script ───────────────────────────────


@windows_only
def test_a_powershell_activate_script_gives_its_environment(tmp_path: Path) -> None:
    """ESP-IDF has export.ps1 beside export.bat."""
    script = tmp_path / "export.ps1"
    script.write_text("Write-Host 'the script talks'\r\n$env:FWCTX_SOURCE = 'powershell'\r\n", encoding="utf-8")

    result = run_build_command(PRINT_SOURCE, cwd=tmp_path, build_cfg=BuildConfig(activate=str(script)))

    assert result.stdout.strip() == "powershell"


@windows_only
def test_a_value_that_is_not_ascii_comes_through(tmp_path: Path) -> None:
    """A path of a user (C:\\Users\\Přemysl) comes from the environment and goes through the script.

    The environment comes back as ASCII JSON of Python, thus no code page
    changes a value.  The probe compares the value itself and answers with
    its exit code: the decoding of the output of a build is a different
    question.  The script itself is ASCII, as the activate scripts of the
    SDKs are: cmd.exe reads a batch file in the code page of the console.
    """
    value = "C:\\Users\\Příliš žluťoučký kůň"
    script = tmp_path / "env.bat"
    script.write_text('@set "FWCTX_SOURCE=%FWCTX_FROM_PARENT%"\r\n', encoding="ascii")
    probe = [sys.executable, "-c", f"import os, sys; sys.exit(0 if os.environ['FWCTX_SOURCE'] == {value!r} else 1)"]

    result = run_build_command(probe, cwd=tmp_path, env={"FWCTX_FROM_PARENT": value},
                               build_cfg=BuildConfig(activate=str(script)))

    assert result.returncode == 0


@windows_only
def test_the_output_of_the_script_is_not_a_variable(tmp_path: Path) -> None:
    """The script writes text, and only the variables after it are the environment."""
    script = tmp_path / "env.bat"
    script.write_text('@echo NOT_A_VARIABLE=1\r\n@set "FWCTX_SOURCE=cmd"\r\n', encoding="utf-8")
    probe = [sys.executable, "-c", "import os; print(os.environ.get('NOT_A_VARIABLE'), os.environ['FWCTX_SOURCE'])"]

    result = run_build_command(probe, cwd=tmp_path, build_cfg=BuildConfig(activate=str(script)))

    assert result.stdout.strip() == "None cmd"


@windows_only
def test_a_posix_script_on_windows_is_an_error_that_names_the_forms(tmp_path: Path) -> None:
    script = tmp_path / "env.sh"
    script.write_text("export X=1\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"\.bat, \.cmd or \.ps1"):
        run_build_command(PRINT_SOURCE, cwd=tmp_path, build_cfg=BuildConfig(activate=str(script)))


@windows_only
def test_a_bat_path_with_a_percent_sign_is_refused(tmp_path: Path) -> None:
    """cmd.exe expands %NAME% also inside quotes."""
    directory = tmp_path / "100%done"
    directory.mkdir()
    script = write_activate(directory / "env.bat", variables={"FWCTX_SOURCE": "x"})

    with pytest.raises(RuntimeError, match="holds %"):
        run_build_command(PRINT_SOURCE, cwd=tmp_path, build_cfg=BuildConfig(activate=str(script)))


@windows_only
def test_a_posix_script_is_a_fault_of_the_config_and_not_a_missing_tool(tmp_path: Path) -> None:
    """A project from Linux names env.sh: the lookup must say why, not "west is not on PATH"."""
    script = tmp_path / "env.sh"
    script.write_text("export X=1\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"\.bat, \.cmd or \.ps1"):
        build_tool_available(TOOL, tmp_path, BuildConfig(activate=str(script)))


@windows_only
def test_a_bat_path_with_a_caret_is_refused(tmp_path: Path) -> None:
    """`call` doubles each caret, also inside quotes."""
    directory = tmp_path / "a^b"
    directory.mkdir()
    script = write_activate(directory / "env.bat", variables={"FWCTX_SOURCE": "x"})

    with pytest.raises(RuntimeError, match=r"\^"):
        run_build_command(PRINT_SOURCE, cwd=tmp_path, build_cfg=BuildConfig(activate=str(script)))


@windows_only
def test_the_error_of_a_failed_script_is_readable(tmp_path: Path) -> None:
    script = tmp_path / "env.bat"
    script.write_text("@echo ERROR: the toolchain is missing\r\n@exit /b 1\r\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="ERROR: the toolchain is missing"):
        run_build_command(PRINT_SOURCE, cwd=tmp_path, build_cfg=BuildConfig(activate=str(script)))


@windows_only
def test_the_script_and_the_command_share_one_timeout(tmp_path: Path) -> None:
    """On POSIX bash runs the two under one timeout; a slow script must not double it."""
    from fw_context_mcp.utils import BuildTimeoutError

    script = tmp_path / "env.bat"
    script.write_text("@ping -n 4 127.0.0.1 >nul\r\n", encoding="utf-8")

    with pytest.raises(BuildTimeoutError):
        run_build_command(PRINT_SOURCE, cwd=tmp_path, timeout=2, build_cfg=BuildConfig(activate=str(script)))
