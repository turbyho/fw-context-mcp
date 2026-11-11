"""The setup wizard of the cache server on a system that it does not support.

The wizard runs sudo, useradd, systemd or launchd and nginx, and it reads
the users of the system through ``pwd``.  On Windows the import of the
wizard stopped with ``ModuleNotFoundError: No module named 'pwd'``, a
traceback and not a message.
"""

from __future__ import annotations

import argparse
import sys

from fw_context_mcp.cache_server import cli


def test_the_wizard_refuses_windows_with_a_message(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delitem(sys.modules, "fw_context_mcp.cache_server.setup", raising=False)

    assert cli.cmd_setup(argparse.Namespace()) == 1
    assert "Linux and macOS only" in capsys.readouterr().err
    assert "fw_context_mcp.cache_server.setup" not in sys.modules, "the wizard ran no step, thus it is not imported"
