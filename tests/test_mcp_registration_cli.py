"""The registration of fw-context with an AI tool through its CLI (``claude mcp add``).

Two faults on Windows:

- The command was ``template.replace("{bin}", path).split()``: a path with
  a space (``C:\\Users\\Jan Novák\\...``) became two arguments.
- ``subprocess.run(["claude", ...])`` does not start ``claude.cmd`` or
  ``codex.cmd``, the shims of npm: ``CreateProcess`` adds only ``.exe``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.cli import _mcp
from tests._stubs import write_python_tool


@pytest.mark.parametrize("batch", [False, pytest.param(True, marks=pytest.mark.skipif(
    sys.platform != "win32", reason="a shim of npm (.cmd) on Windows",
))], ids=["program", "batch shim"])
def test_the_path_of_the_server_is_one_argument(tmp_path: Path, monkeypatch, capsys, batch: bool):
    argv_file = tmp_path / "argv.json"
    tools = tmp_path / "tools"
    write_python_tool(
        tools, "fwctx-claude",
        f"import json, sys\nopen({str(argv_file)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        "print('Added stdio MCP server fw-context \\u2013 ok')\n",
        batch=batch,
    )
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ.get('PATH', '')}")
    server = tmp_path / "Jan Novák & R&D" / "fw-context-mcp"
    tool = SimpleNamespace(name="Claude Code", mcp_registration="fwctx-claude mcp add --scope user fw-context {bin}")

    _mcp._register_mcp_cli(tool, str(server))

    assert json.loads(argv_file.read_text(encoding="utf-8")) == [
        "mcp", "add", "--scope", "user", "fw-context", str(server),
    ]
    assert "[ok] Claude Code: fw-context registered" in capsys.readouterr().out


def test_a_tool_that_is_not_there_gives_the_command_to_run(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    tool = SimpleNamespace(name="Codex", mcp_registration="fwctx-codex mcp add fw-context -- {bin}")

    _mcp._register_mcp_cli(tool, "/opt/fw context/fw-context-mcp")

    out = capsys.readouterr().out
    assert "[skip] 'fwctx-codex' not found in PATH" in out
    assert "fw context" in out
