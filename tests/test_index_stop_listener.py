"""A stop listener that fails must put back the signal handlers of the caller (``cli._index``)."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize("entry", ["owned_index", "cmd_index"])
def test_a_failed_stop_listener_puts_back_the_handlers_of_the_caller(tmp_path: Path, monkeypatch, entry: str):
    """On Windows the stop listener raises OSError for a stop event that exists already.

    The call came after the handlers of the run were installed, and outside
    of the code that puts back the handlers of the caller.  A caller that
    goes on (an MCP tool, a test) then had the SIGTERM handler of a run that
    no longer exists.
    """
    import signal
    from types import SimpleNamespace

    from fw_context_mcp.cli import _index as index_mod
    from fw_context_mcp.processes import HANGUP_SIGNAL

    def refused() -> None:
        raise OSError(17, "the stop event exists already")

    def caller_handler(signum, frame) -> None:
        pass

    monkeypatch.setattr(index_mod, "install_stop_listener", refused)
    previous_term = signal.signal(signal.SIGTERM, caller_handler)
    previous_hup = signal.signal(HANGUP_SIGNAL, caller_handler)
    try:
        with pytest.raises(OSError, match="exists already"):
            if entry == "owned_index":
                with index_mod._owned_index(tmp_path, SimpleNamespace(background=False, takeover=False)):
                    pass
            else:
                index_mod.cmd_index(SimpleNamespace())
        assert signal.getsignal(signal.SIGTERM) is caller_handler
        assert signal.getsignal(HANGUP_SIGNAL) is caller_handler
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(HANGUP_SIGNAL, previous_hup)
