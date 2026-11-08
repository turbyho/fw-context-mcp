"""The spawn of the watcher daemon and the watcher lock (``background._ensure_daemon_running``).

The daemon takes ``watcher.lock`` with a try that does not wait, and it
exits when a different file descriptor holds the lock (``daemon_main``).
Thus the process that spawns the daemon must not hold the lock when the
daemon starts.  Before, the spawner held it until after ``Popen``: a daemon
that came to its lock call first exited with "Daemon already running", and
no daemon ran until the next ping cycle.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import fw_context_mcp  # noqa: F401 — the sqlite3 redirect runs first
from fw_context_mcp.file_lock import held_by_another, try_lock, unlock
from fw_context_mcp.mcp import background
from fw_context_mcp.mcp import daemon as daemon_mod


@pytest.fixture
def db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "index"
    directory.mkdir()
    (directory / "index.db").touch()
    monkeypatch.setattr(background, "_db_path", lambda root: directory / "index.db")
    return directory


def test_the_lock_is_free_when_the_daemon_starts(db_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_mod, "ping_daemon", lambda root: False)
    held_at_spawn: list[bool] = []
    monkeypatch.setattr(
        background, "_spawn_daemon",
        lambda root: held_at_spawn.append(held_by_another(db_dir / "watcher.lock")),
    )

    background._ensure_daemon_running(db_dir)

    assert held_at_spawn == [False]
    assert not held_by_another(db_dir / "watcher.lock")


def test_a_held_lock_spawns_no_daemon(db_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A different holder of the lock is a daemon that runs, or a daemon that starts."""
    monkeypatch.setattr(daemon_mod, "ping_daemon", lambda root: False)
    spawned: list[Path] = []
    monkeypatch.setattr(background, "_spawn_daemon", spawned.append)
    fd = os.open(db_dir / "watcher.lock", os.O_CREAT | os.O_RDWR)
    try:
        assert try_lock(fd)
        background._ensure_daemon_running(db_dir)
        assert spawned == []
        unlock(fd)
    finally:
        os.close(fd)


def test_an_answer_to_the_second_ping_spawns_no_daemon(db_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon that took the lock between the first ping and the try of the lock."""
    answers = iter([False, True])
    monkeypatch.setattr(daemon_mod, "ping_daemon", lambda root: next(answers))
    spawned: list[Path] = []
    monkeypatch.setattr(background, "_spawn_daemon", spawned.append)

    background._ensure_daemon_running(db_dir)

    assert spawned == []
    assert not held_by_another(db_dir / "watcher.lock")


def test_an_error_of_the_second_ping_releases_the_lock(db_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP server lives long: a lock that stays held stops each later spawn of the daemon."""
    calls = iter([False])

    def ping(root: Path) -> bool:
        try:
            return next(calls)
        except StopIteration:
            raise RuntimeError("ping failed") from None

    monkeypatch.setattr(daemon_mod, "ping_daemon", ping)
    monkeypatch.setattr(background, "_spawn_daemon", lambda root: None)

    with pytest.raises(RuntimeError, match="ping failed"):
        background._ensure_daemon_running(db_dir)

    assert not held_by_another(db_dir / "watcher.lock")
