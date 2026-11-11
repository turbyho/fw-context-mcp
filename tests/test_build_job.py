"""Windows: a build runs in a job object, and each stop reaches each process of the build.

The POSIX form of these tests is ``test_build_process_group.py``.  A build
tool starts processes of its own, thus a stop that ends only the direct
child left compilers that wrote into the build directory of the next run.
``job_object.py`` gives the measurements.

These tests use real processes: a test double cannot show that a
grandchild stopped.  The child is Python and starts its grandchild on its
first line, thus a grandchild that escapes the job (a start before the
process went into the job) fails the tests.
"""

from __future__ import annotations

import _thread
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from fw_context_mcp.processes import kill_hard, pid_exists
from fw_context_mcp.utils import run_in_process_group

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the job object is Windows; POSIX: test_build_process_group.py")


def _wait_gone(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_exists(pid):
            return True
        time.sleep(0.05)
    return False


def _read_pid(pid_file: Path) -> int:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        text = pid_file.read_text(encoding="utf-8").strip() if pid_file.exists() else ""
        if text:
            return int(text)
        time.sleep(0.02)
    raise AssertionError("the grandchild did not start")


def _build_with_grandchild(pid_file: Path, then: str = "time.sleep(60)") -> list[str]:
    """A build that starts a grandchild on its first line, writes the PID of the grandchild, then does *then*."""
    script = textwrap.dedent(f"""
        import subprocess, sys, time
        grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(pid_file)!r}, "w").write(str(grandchild.pid))
        {then}
    """)
    return [sys.executable, "-c", script]


def test_a_timeout_stops_the_grandchild(tmp_path: Path):
    pid_file = tmp_path / "grandchild.pid"

    with pytest.raises(subprocess.TimeoutExpired):
        run_in_process_group(
            _build_with_grandchild(pid_file), cwd=tmp_path, env=dict(os.environ), timeout=3.0, capture_output=True,
        )

    assert _wait_gone(_read_pid(pid_file)), "the compiler of the build still runs"


def test_an_exception_while_the_build_runs_stops_the_grandchild(tmp_path: Path):
    """THE regression test: a stop request (IndexStopped) or Ctrl-C inside communicate().

    The stop must come at once.  ``communicate`` waits in
    ``WaitForSingleObject``, and the interrupt came only after the build
    ended: here after the 60 s of the build.
    """
    pid_file = tmp_path / "grandchild.pid"
    interrupted: list[float] = []

    def interrupt_when_the_grandchild_runs() -> None:
        _read_pid(pid_file)
        interrupted.append(time.monotonic())
        _thread.interrupt_main()

    timer = threading.Thread(target=interrupt_when_the_grandchild_runs, daemon=True)
    timer.start()
    with pytest.raises(KeyboardInterrupt):
        run_in_process_group(
            _build_with_grandchild(pid_file), cwd=tmp_path, env=dict(os.environ), timeout=120.0, capture_output=True,
        )
    stopped = time.monotonic()
    timer.join(timeout=30)

    assert stopped - interrupted[0] < 5.0, "the stop waited for the end of the build"
    assert _wait_gone(_read_pid(pid_file)), "the compiler of the build still runs"


def test_a_build_that_ends_gives_its_output(tmp_path: Path):
    script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"

    result = run_in_process_group(
        [sys.executable, "-c", script], cwd=tmp_path, env=dict(os.environ), timeout=60.0, capture_output=True,
    )

    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")


def test_a_process_that_the_build_left_ends_with_the_build(tmp_path: Path):
    """The build ends by itself and leaves its grandchild: the close of the job ends it.

    No captured output: the grandchild has the pipes of the build, and
    ``communicate`` waits for their end, on POSIX too.  A process that a
    build leaves on purpose (a compiler server) closes them.
    """
    pid_file = tmp_path / "grandchild.pid"

    result = run_in_process_group(
        _build_with_grandchild(pid_file, then="sys.exit(0)"),
        cwd=tmp_path, env=dict(os.environ), timeout=60.0, capture_output=False,
    )

    assert result.returncode == 0
    assert _wait_gone(_read_pid(pid_file)), "a process of the build lives on after the build"


def test_the_build_of_a_run_that_dies_ends(tmp_path: Path):
    """On POSIX a record lets the next run stop it; on Windows the job does it at once.

    The run gets ``TerminateProcess`` (``kill_hard``), as a hard stop of a
    takeover or the end of its terminal does it: no code of the run runs.
    """
    pid_file = tmp_path / "grandchild.pid"
    runner = textwrap.dedent(f"""
        import os
        from pathlib import Path
        from fw_context_mcp.utils import run_in_process_group
        run_in_process_group({_build_with_grandchild(pid_file)!r}, cwd=Path({str(tmp_path)!r}),
                             env=dict(os.environ), timeout=120.0, capture_output=True)
    """)
    run = subprocess.Popen([sys.executable, "-c", runner])  # noqa: S603 — fixed argv
    try:
        grandchild = _read_pid(pid_file)
        kill_hard(run.pid)
        run.wait(timeout=30)
        assert _wait_gone(grandchild), "the build of the dead run still runs"
    finally:
        if run.poll() is None:
            run.kill()
            run.wait(timeout=30)
