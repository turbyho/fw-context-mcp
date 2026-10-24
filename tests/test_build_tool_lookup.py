"""A build looks for its tools in the environment of the project.

With ``[build] activate``, a tool is on PATH only after ``source
<activate>``: the activate script of NCS puts ``west`` and ``ninja`` there.
The lookup ran ``shutil.which`` on the PATH of the fw-context process, thus
a build with a working activate script failed with "west is required", and
a ``west`` from outside the environment could name a different workspace.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from fw_context_mcp.indexer.build import BuildConfig
from fw_context_mcp.utils import build_tool_available, missing_tool_message


def _tool_dir(tmp_path: Path, name: str) -> Path:
    """A directory with an executable *name* in it, which no PATH holds."""
    directory = tmp_path / "toolchain" / "bin"
    directory.mkdir(parents=True)
    tool = directory / name
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return directory


def _activate(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "env.sh"
    script.write_text(body, encoding="utf-8")
    return script


@pytest.fixture
def path_without_west(monkeypatch) -> None:
    """A PATH with the system tools and without west."""
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))


@pytest.mark.usefixtures("path_without_west")
def test_a_tool_that_only_the_activate_script_gives_is_found(tmp_path: Path) -> None:
    tools = _tool_dir(tmp_path, "west")
    script = _activate(tmp_path, f'export PATH="{tools}:$PATH"\n')

    assert build_tool_available("west", tmp_path, BuildConfig(activate=str(script)))


@pytest.mark.usefixtures("path_without_west")
def test_without_activate_the_tool_of_that_script_is_not_found(tmp_path: Path) -> None:
    _tool_dir(tmp_path, "west")

    assert not build_tool_available("west", tmp_path, BuildConfig())


@pytest.mark.usefixtures("path_without_west")
def test_extra_path_of_the_build_counts_without_activate(tmp_path: Path) -> None:
    tools = _tool_dir(tmp_path, "west")

    assert build_tool_available("west", tmp_path, BuildConfig(extra_path=[str(tools)]))


@pytest.mark.usefixtures("path_without_west")
def test_a_failed_activate_script_finds_no_tool(tmp_path: Path) -> None:
    tools = _tool_dir(tmp_path, "west")
    script = _activate(tmp_path, f'export PATH="{tools}:$PATH"\nreturn 1\n')

    assert not build_tool_available("west", tmp_path, BuildConfig(activate=str(script)))


def test_the_tool_on_the_path_of_the_process_does_not_count_with_activate(tmp_path: Path, monkeypatch) -> None:
    """A west outside the environment of the project must not answer for it."""
    host = _tool_dir(tmp_path, "west")
    monkeypatch.setenv("PATH", os.pathsep.join([str(host), "/usr/bin", "/bin"]))
    script = _activate(tmp_path, 'export PATH="/usr/bin:/bin"\n')

    assert not build_tool_available("west", tmp_path, BuildConfig(activate=str(script)))


@pytest.mark.usefixtures("path_without_west")
def test_a_zephyr_build_uses_the_west_of_the_activate_script(tmp_path: Path, monkeypatch) -> None:
    """THE regression: the build stopped with "west is required" before activate ran."""
    import fw_context_mcp.indexer.builders.zephyr as zephyr

    tools = _tool_dir(tmp_path, "west")
    script = _activate(tmp_path, f'export PATH="{tools}:$PATH"\n')
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


def test_the_message_names_the_activate_script(tmp_path: Path) -> None:
    cfg = BuildConfig(activate="/opt/sdk/env.sh")

    message = missing_tool_message("west", cfg, "Install west.")

    assert "source /opt/sdk/env.sh" in message
    assert "Install west." not in message


def test_the_message_without_activate_gives_the_install_hint() -> None:
    assert missing_tool_message("west", BuildConfig(), "Install west.") == "west is required.  Install west."
