"""The status tools give ``"config_error"`` for a config file that ``load`` cannot use.

Before commit e003906, ``load`` ignored such a file: the project read as
not initialized, and the answer told the caller to run ``fw-context
init``, which wrote a new project ID over the file.  After it, the two
tools raised the ``ConfigError``.  An answer with ``error`` alone means
"the index holds no build" in the LLM instructions.  Thus the state has
its own status, which names the file and tells the caller not to run
init.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

PID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def broken(tmp_path) -> Path:
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(f'[project]\nid = "{PID}"\n[index\n', encoding="utf-8")
    return root


@pytest.mark.parametrize("tool", ["get_project_status", "get_build_info"])
def test_the_status_tools_name_the_file(broken, tool):
    from fw_context_mcp.mcp.handlers import maintenance

    state = getattr(maintenance, tool)(project_root=str(broken))

    path = broken / ".fw-context" / "config.toml"
    assert state["status"] == "config_error", state
    assert state["config_file"] == str(path)
    assert state["index_message"].startswith(f"{path}: not valid TOML")
    assert "Do not run `fw-context init`" in state["index_message"]
    assert "not initialized" not in state["index_message"]


def test_fw_context_status_prints_the_problem(broken, capsys):
    from fw_context_mcp.cli._search import cmd_status

    assert cmd_status(argparse.Namespace(project=str(broken))) == 1
    assert "not valid TOML" in capsys.readouterr().out


def test_the_two_copies_of_the_llm_instructions_name_the_status():
    """The instructions live in two copies: a client reads one or the other."""
    from fw_context_mcp.config.tools import BASE_INSTRUCTIONS
    from fw_context_mcp.mcp import server

    for text in (BASE_INSTRUCTIONS, server.mcp.instructions):
        assert "config_error" in text
        assert "Do NOT run `fw-context init` or `fw-context index`" in " ".join(text.split())
