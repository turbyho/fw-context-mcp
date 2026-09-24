"""A build runs in its own process group, and a stop reaches the whole group.

``subprocess.run`` stopped only the direct child.  A build tool starts
compilers of its own (``make -j``, ``bear``, scons under ``pio``), thus a run
that another run took over left its compilers to write object files into the
directory that the new build cleaned and filled.

These tests use real processes: a process group is a property of the kernel,
and a test double cannot show that a grandchild stopped.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fw_context_mcp.utils import process_start_time, record_build_groups, run_in_process_group


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _grandchild_script(pid_file: Path) -> list[str]:
    """A shell that starts a grandchild, writes its PID, and waits for it."""
    return ["sh", "-c", f"sleep 60 & echo $! > {pid_file}; wait"]


def _read_pid(pid_file: Path) -> int:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        text = pid_file.read_text(encoding="utf-8").strip() if pid_file.exists() else ""
        if text:
            return int(text)
        time.sleep(0.02)
    raise AssertionError("the grandchild did not start")


class _Stop(BaseException):
    """What IndexStopped does from a signal handler: an exception in communicate()."""


def test_a_timeout_stops_the_grandchild(tmp_path: Path):
    pid_file = tmp_path / "grandchild.pid"

    with pytest.raises(subprocess.TimeoutExpired):
        run_in_process_group(
            _grandchild_script(pid_file), cwd=tmp_path, env=dict(os.environ),
            timeout=1.0, capture_output=True,
        )

    assert _wait_gone(_read_pid(pid_file)), "the compiler of the build still runs"


def test_an_exception_while_the_build_runs_stops_the_grandchild(tmp_path: Path):
    """THE regression test: SIGTERM → IndexSuperseded inside communicate()."""
    pid_file = tmp_path / "grandchild.pid"

    def _raise(signum, frame):
        raise _Stop

    previous = signal.signal(signal.SIGALRM, _raise)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(_Stop):
            run_in_process_group(
                _grandchild_script(pid_file), cwd=tmp_path, env=dict(os.environ),
                timeout=30.0, capture_output=False,
            )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    assert _wait_gone(_read_pid(pid_file)), "the compiler of the build still runs"


def test_a_build_that_ends_gives_its_output(tmp_path: Path):
    result = run_in_process_group(
        ["sh", "-c", "echo out; echo err >&2; exit 3"], cwd=tmp_path, env=dict(os.environ),
        timeout=30.0, capture_output=True,
    )

    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")


def test_the_build_runs_in_a_new_session(tmp_path: Path):
    """Ctrl-C then reaches this process only, and it stops the build itself."""
    result = run_in_process_group(
        [sys.executable, "-c", "import os; print(os.getsid(0))"], cwd=tmp_path, env=dict(os.environ),
        timeout=30.0, capture_output=True,
    )

    assert int(result.stdout.strip()) != os.getsid(0)


def test_the_group_is_recorded_while_the_build_runs(tmp_path: Path):
    """After a SIGKILL the next run reads the record and stops the group."""
    record = tmp_path / "reindex.build"
    copy = tmp_path / "copy.json"

    with record_build_groups(record):
        result = run_in_process_group(
            ["sh", "-c", f"cat {record} > {copy}; echo $$"], cwd=tmp_path,
            env=dict(os.environ), timeout=30.0, capture_output=True,
        )

    seen = json.loads(copy.read_text(encoding="utf-8"))
    started = seen.pop("started")
    assert seen == {"index_pid": os.getpid(), "pgid": int(result.stdout.strip()), "leader": "sh"}
    assert isinstance(started, str), "the start time tells a reused PID apart"
    assert not record.exists(), "the record must go when the build ends"


def test_no_record_outside_the_block(tmp_path: Path):
    record = tmp_path / "reindex.build"
    with record_build_groups(record):
        pass

    run_in_process_group(["true"], cwd=tmp_path, env=dict(os.environ), timeout=30.0, capture_output=True)

    assert not record.exists()


# ── The next owner stops the build that a SIGKILL left ──────────────────────


def _orphan_group(program: str = "sleep") -> subprocess.Popen:
    """A process group that no index run owns any more."""
    return subprocess.Popen([program, "60"], start_new_session=True)  # noqa: S603 — fixed argv


def _index_run_stand_in() -> subprocess.Popen:
    """A live process with the argv of an index run (see _index_run_kind)."""
    proc = subprocess.Popen(  # noqa: S603 — fixed argv
        [sys.executable, "-c", "import time; print('READY', flush=True); time.sleep(60)",
         "-m", "fw_context_mcp.cli", "index"],
        stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "READY"
    return proc


def _record(db_dir: Path, **fields) -> Path:
    record = db_dir / "reindex.build"
    record.write_text(json.dumps(fields), encoding="utf-8")
    return record


def test_the_build_of_a_dead_run_is_stopped(tmp_path: Path):
    """THE regression test: SIGKILL stops the index run, and its build runs on."""
    from fw_context_mcp.cli._index import _reap_orphan_build_group

    group = _orphan_group()
    try:
        record = _record(
            tmp_path, index_pid=2147483646, pgid=group.pid, leader="sleep",
            started=process_start_time(group.pid),
        )

        _reap_orphan_build_group(tmp_path)

        assert group.wait(timeout=10) is not None
        assert not record.exists()
    finally:
        if group.poll() is None:
            group.kill()
        group.wait(timeout=10)


def test_a_group_whose_leader_is_another_program_stays(tmp_path: Path):
    """The PGID came back for an unrelated process: never signal it."""
    from fw_context_mcp.cli._index import _reap_orphan_build_group

    group = _orphan_group()
    try:
        record = _record(tmp_path, index_pid=2147483646, pgid=group.pid, leader="bear")

        _reap_orphan_build_group(tmp_path)

        assert group.poll() is None
        assert not record.exists(), "a record that names nothing of ours is garbage"
    finally:
        group.kill()
        group.wait(timeout=10)


def test_the_build_of_a_live_run_stays(tmp_path: Path):
    """That run still owns its build and stops it itself."""
    from fw_context_mcp.cli._index import _reap_orphan_build_group

    group = _orphan_group()
    other = _index_run_stand_in()
    try:
        record = _record(
            tmp_path, index_pid=other.pid, pgid=group.pid, leader="sleep",
            started=process_start_time(group.pid),
        )

        _reap_orphan_build_group(tmp_path)

        assert group.poll() is None
        assert record.exists()
    finally:
        for proc in (group, other):
            proc.kill()
            proc.wait(timeout=10)


def test_a_bad_record_goes(tmp_path: Path):
    from fw_context_mcp.cli._index import _reap_orphan_build_group

    record = tmp_path / "reindex.build"
    record.write_text("{not json", encoding="utf-8")

    _reap_orphan_build_group(tmp_path)

    assert not record.exists()


def test_the_owned_index_records_its_build(tmp_path: Path):
    """The wiring: a build inside _owned_index writes the record there."""
    from types import SimpleNamespace

    from fw_context_mcp.cli._index import _owned_index

    copy = tmp_path / "copy.json"
    with _owned_index(tmp_path, SimpleNamespace(background=False, takeover=False)):
        run_in_process_group(
            ["sh", "-c", f"cat {tmp_path / 'reindex.build'} > {copy}"], cwd=tmp_path,
            env=dict(os.environ), timeout=30.0, capture_output=True,
        )

    assert json.loads(copy.read_text(encoding="utf-8"))["index_pid"] == os.getpid()


# ── A stop that is interrupted, and a build that ignores SIGTERM ───────────


def _group_ignoring_sigterm(pid_file: Path) -> subprocess.Popen:
    """A group whose members ignore SIGTERM: SIG_IGN goes through exec."""
    return subprocess.Popen(  # noqa: S603 — fixed argv
        ["sh", "-c", f"trap '' TERM; sleep 60 & echo $! > {pid_file}; wait"],
        start_new_session=True,
    )


def test_a_group_that_ignores_sigterm_gets_sigkill(tmp_path: Path):
    from fw_context_mcp.utils import stop_process_group

    pid_file = tmp_path / "member.pid"
    group = _group_ignoring_sigterm(pid_file)
    try:
        member = _read_pid(pid_file)
        stop_process_group(group.pid, grace=0.5, leader=group)
        assert _wait_gone(member)
    finally:
        if group.poll() is None:
            group.kill()
        group.wait(timeout=10)


def test_a_second_interrupt_during_the_grace_still_kills_the_group(tmp_path: Path):
    """A second Ctrl-C left out the SIGKILL, and the build ran on."""
    from fw_context_mcp.utils import stop_process_group

    pid_file = tmp_path / "member.pid"
    group = _group_ignoring_sigterm(pid_file)

    def _raise(signum, frame):
        raise _Stop

    previous = signal.signal(signal.SIGALRM, _raise)
    try:
        member = _read_pid(pid_file)
        signal.setitimer(signal.ITIMER_REAL, 0.5)
        with pytest.raises(_Stop):
            stop_process_group(group.pid, grace=30.0, leader=group)
        assert _wait_gone(member)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if group.poll() is None:
            group.kill()
        group.wait(timeout=10)


def test_a_leader_with_another_start_time_stays(tmp_path: Path):
    """The shell of a new terminal got the PID, and its name is the recorded one."""
    from fw_context_mcp.cli._index import _reap_orphan_build_group

    group = _orphan_group()
    try:
        record = _record(
            tmp_path, index_pid=2147483646, pgid=group.pid, leader="sleep", started="1"
        )

        _reap_orphan_build_group(tmp_path)

        assert group.poll() is None
        assert not record.exists()
    finally:
        group.kill()
        group.wait(timeout=10)


def test_the_recorded_start_time_matches_the_live_leader(tmp_path: Path):
    """The leader writes its own start time while it runs, and the record must agree."""
    record = tmp_path / "reindex.build"
    copy = tmp_path / "copy.json"
    own = tmp_path / "own.txt"
    probe = (
        "import sys; from fw_context_mcp.utils import process_start_time; "
        "import os; print(process_start_time(os.getpid()))"
    )
    with record_build_groups(record):
        run_in_process_group(
            ["sh", "-c", f"cat {record} > {copy}; exec {sys.executable} -c '{probe}' > {own}"],
            cwd=tmp_path, env=dict(os.environ), timeout=30.0, capture_output=True,
        )

    started = json.loads(copy.read_text(encoding="utf-8"))["started"]
    # exec keeps the PID and the start time of the shell, thus the leader
    # itself reads the value that the record must hold.
    assert started is not None
    assert own.read_text(encoding="utf-8").strip() == started


def test_a_pre_build_timeout_is_a_runtime_error(tmp_path: Path):
    """Each caller of generate_compile_commands catches RuntimeError only."""
    from fw_context_mcp.indexer.build import BuildConfig, _run_pre_build

    with pytest.raises(RuntimeError, match="timed out"):
        _run_pre_build(BuildConfig(pre_build="sleep 30", timeout=1), tmp_path)


def test_a_live_pid_that_is_no_index_run_does_not_protect_the_orphan(tmp_path: Path):
    """The system gave the PID of the dead run to an unrelated process."""
    from fw_context_mcp.cli._index import _reap_orphan_build_group

    group = _orphan_group()
    unrelated = subprocess.Popen(["sleep", "60"])  # noqa: S603 — fixed argv
    try:
        _record(
            tmp_path, index_pid=unrelated.pid, pgid=group.pid, leader="sleep",
            started=process_start_time(group.pid),
        )

        _reap_orphan_build_group(tmp_path)

        assert group.wait(timeout=10) is not None
        assert unrelated.poll() is None
    finally:
        for proc in (group, unrelated):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


def test_a_live_leader_without_a_start_time_is_not_signalled(tmp_path: Path):
    """Fail-closed: the name alone does not identify a leader."""
    from fw_context_mcp.cli._index import _reap_orphan_build_group

    group = _orphan_group()
    try:
        record = _record(tmp_path, index_pid=2147483646, pgid=group.pid, leader="sleep", started=None)

        _reap_orphan_build_group(tmp_path)

        assert group.poll() is None
        assert not record.exists()
    finally:
        group.kill()
        group.wait(timeout=10)


def test_nohup_keeps_its_sighup_ignored(tmp_path: Path):
    """``nohup fw-context index`` must live on after the terminal closes."""
    from types import SimpleNamespace

    from fw_context_mcp.cli._index import _owned_index

    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with _owned_index(tmp_path, SimpleNamespace(background=False, takeover=False)):
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
            os.kill(os.getpid(), signal.SIGHUP)
            time.sleep(0.1)
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)
