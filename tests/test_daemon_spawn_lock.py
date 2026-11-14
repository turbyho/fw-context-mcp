"""The spawn of the watcher daemon and the watcher lock (``background._ensure_daemon_running``).

The daemon takes ``watcher.lock`` with a try that does not wait, and it
exits when a different file descriptor holds the lock (``daemon_main``).
Thus the process that spawns the daemon must never hold the lock: it only
probes it (``file_lock.held_by_another``).  Before, the spawner held it until after ``Popen``: a daemon
that came to its lock call first exited with "Daemon already running", and
no daemon ran until the next ping cycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fw_context_mcp  # noqa: F401 — the sqlite3 redirect runs first
from fw_context_mcp.file_lock import ProbedLock, held_by_another
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
    daemon_lock = ProbedLock(db_dir / "watcher.lock")
    try:
        assert daemon_lock.try_acquire()
        background._ensure_daemon_running(db_dir)
        assert spawned == []
    finally:
        daemon_lock.close()


def test_the_check_does_not_make_the_lock_of_a_starting_daemon_fail(
    db_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon that starts during the second ping must get its lock.

    The function held ``watcher.lock`` during the second ping, and the try
    of a daemon that started in that time failed: the daemon exited with
    "Daemon already running", and no daemon ran.  The fake second ping is
    the moment where the daemon tries its lock.
    """
    starting = ProbedLock(db_dir / "watcher.lock")
    got_it: list[bool] = []

    def ping(root: Path) -> bool:
        if not got_it:
            got_it.append(False)
            return False
        got_it[0] = starting.try_acquire()
        return got_it[0]

    monkeypatch.setattr(daemon_mod, "ping_daemon", ping)
    spawned: list[Path] = []
    monkeypatch.setattr(background, "_spawn_daemon", spawned.append)
    try:
        background._ensure_daemon_running(db_dir)
    finally:
        starting.close()
    assert got_it == [True]
    assert spawned == []


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


@pytest.mark.parametrize(("own", "spawns"), [(True, 0), (False, 1)], ids=["own-user", "different-user"])
def test_a_daemon_of_v036_spawns_no_daemon(db_dir: Path, monkeypatch: pytest.MonkeyPatch, own: bool, spawns: int) -> None:
    """A daemon of v0.36.0 or earlier answers no ping and holds no status lock.

    Each ping cycle (15 s) spawned a daemon that exited at its lock call at
    once: one process start and one line of ``daemon.log`` each time.  The
    kernel gives the user of the listener on ``daemon.sock``: a listener of
    a different user is no daemon of this user.
    """
    monkeypatch.setattr(daemon_mod, "ping_daemon", lambda root: False)
    listener = daemon_mod.LegacyListener(pid=4242, uid=1000, own=own)
    monkeypatch.setattr(daemon_mod, "legacy_listener", lambda directory: listener)
    spawned: list[Path] = []
    monkeypatch.setattr(background, "_spawn_daemon", spawned.append)

    background._ensure_daemon_running(db_dir)

    assert len(spawned) == spawns
