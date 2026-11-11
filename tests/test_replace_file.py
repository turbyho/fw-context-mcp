"""The replace and the removal of a file that another process reads, on Windows.

Windows does not replace or remove a file that a process holds open, also
when that process opened it with FILE_SHARE_DELETE (measured on Windows 11:
1779 of 2000 replaces failed while three processes read the file in a
loop).  The MCP servers read ``daemon.port`` in each ping, thus a new
daemon could not write it, and it ended.  ``replace_file`` and
``remove_file`` try again for a short time on Windows.
"""

from __future__ import annotations

import ast
import multiprocessing
import sys
from pathlib import Path

import pytest

import fw_context_mcp
from fw_context_mcp import utils


class _Flaky:
    """An action that raises PermissionError *failures* times, then succeeds."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise PermissionError(13, "the file is open in another process")


def test_windows_tries_again_while_the_file_is_open(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(utils, "_SHARED_FILE_STEP_S", 0.0)
    action = _Flaky(failures=3)

    utils._retry_while_shared(action)

    assert action.calls == 4


def test_windows_gives_the_error_after_the_time_limit(monkeypatch):
    """A file that stays open, or a real lack of permission, is an error of the caller."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(utils, "_SHARED_FILE_RETRY_S", 0.05)
    monkeypatch.setattr(utils, "_SHARED_FILE_STEP_S", 0.0)

    with pytest.raises(PermissionError):
        utils._retry_while_shared(_Flaky(failures=10**9))


def test_posix_does_not_try_again(monkeypatch):
    """POSIX replaces and removes an open file: a PermissionError there is a real one."""
    monkeypatch.setattr(sys, "platform", "linux")
    action = _Flaky(failures=1)

    with pytest.raises(PermissionError):
        utils._retry_while_shared(action)
    assert action.calls == 1


def test_no_other_code_calls_os_replace():
    """Each replace of a file goes through replace_file, thus no writer misses the wait of Windows."""
    source = Path(fw_context_mcp.__file__).parent
    found = []
    for path in sorted(source.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "replace" and ast.unparse(node.func.value) == "os"
            ):
                found.append(f"{path.relative_to(source)}:{node.lineno}")
    # The one call is inside replace_file (a lambda in its body).
    assert found == [f"utils.py:{_line_of_replace_file_call()}"]


def _line_of_replace_file_call() -> int:
    source = Path(utils.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "replace_file":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and ast.unparse(inner.func) == "os.replace":
                    return inner.lineno
    raise AssertionError("replace_file calls no os.replace")


def _read_in_a_loop(target: str, started, stop, reads) -> None:
    """Read *target* until *stop*; *started* after the first read; *reads* counts the reads."""
    while not stop.is_set():
        try:
            Path(target).read_text(encoding="utf-8")
        except OSError:
            continue
        with reads.get_lock():
            reads.value += 1
        started.set()


@pytest.mark.skipif(sys.platform != "win32", reason="the sharing rules of Windows")
def test_write_text_atomic_while_other_processes_read(tmp_path: Path):
    """As MCP servers that read daemon.port in each ping (three readers, far more often than a ping)."""
    target = tmp_path / "daemon.port"
    utils.write_text_atomic(target, "1 a\n", mode=0o600)
    stop = multiprocessing.Event()
    started = [multiprocessing.Event() for _ in range(3)]
    reads = multiprocessing.Value("i", 0)
    readers = [
        multiprocessing.Process(target=_read_in_a_loop, args=(str(target), event, stop, reads)) for event in started
    ]
    for reader in readers:
        reader.start()
    try:
        # The writes start when each reader reads: the start of a process
        # on Windows takes longer than 200 writes.
        assert all(event.wait(60) for event in started), "a reader did not start"
        before = reads.value
        for number in range(200):
            utils.write_text_atomic(target, f"{number} {'a' * 32}\n", mode=0o600)
        during = reads.value - before
    finally:
        stop.set()
        for reader in readers:
            reader.join()

    assert target.read_text(encoding="utf-8").startswith("199 ")
    assert during > 0, "no read came during the writes: the test showed nothing"
