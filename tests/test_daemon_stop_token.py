"""Each index run of the daemon gets its own stop token (``processes.STOP_TOKEN_ENV``).

The daemon gave one token to each run for its whole life.  A run that the
daemon killed keeps its token event until Windows has torn the process
down, and the listener of a run refuses an event that exists already
(``processes._create_own_event``).  The next run with the same token then
did not start.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import fw_context_mcp  # noqa: F401 — the sqlite3 redirect runs first
from fw_context_mcp.mcp import daemon
from fw_context_mcp.processes import STOP_TOKEN_ENV


def _spawn_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[daemon._IndexRun, str]]:
    """Spawn two runs through ``_run_index_async`` and return each run with the token of its environment."""
    tokens: list[str] = []

    async def fake_exec(*args: object, env: dict[str, str], **kwargs: object) -> SimpleNamespace:
        tokens.append(env[STOP_TOKEN_ENV])
        return SimpleNamespace(pid=4242, returncode=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def drive() -> list[daemon._IndexRun]:
        return [
            await daemon._run_index_async(tmp_path, tmp_path),
            await daemon._run_index_async(tmp_path, tmp_path),
        ]

    runs = asyncio.run(drive())
    return list(zip(runs, tokens, strict=True))


def test_each_run_gets_a_new_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (first, first_token), (second, second_token) = _spawn_two(tmp_path, monkeypatch)

    assert first_token != second_token
    assert first.stop_token == first_token
    assert second.stop_token == second_token


@pytest.mark.skipif(sys.platform != "win32", reason="the stop request through a token is Windows")
def test_the_stop_request_uses_the_token_of_its_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (first, first_token), (second, second_token) = _spawn_two(tmp_path, monkeypatch)
    requests: list[str | None] = []
    monkeypatch.setattr(daemon, "request_stop", lambda pid, token=None: requests.append(token) or True)

    daemon._stop_run(second)
    daemon._stop_run(first)

    assert requests == [second_token, first_token]
