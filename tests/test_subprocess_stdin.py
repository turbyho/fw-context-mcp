"""Each program that fw-context starts gets its own stdin, not the stdin of fw-context.

The stdin of an MCP server is the JSON-RPC pipe of the client, and a thread
of the server waits in a read of it.  On Windows a program that inherits
that pipe stops at its start until the read ends (measured on Windows 11:
``git --version`` did not end in 10 s; with ``stdin=DEVNULL`` it ended in
0.03 s).  The read ends only with the next message of the client, and the
client waits for the answer of the tool: the MCP server stopped in
``get_project_status`` at ``git ls-files``.  On each system a program that
reads its stdin also took the messages of the client.

The setup wizard of the cache server is not checked: it runs on Linux and
macOS only, from a terminal, and some of its commands (sudo, certbot) talk
to the operator.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import fw_context_mcp

_SOURCE = Path(fw_context_mcp.__file__).parent
_SUBPROCESS_CALLS = {"run", "Popen", "check_output", "check_call", "call"}
#: These take no stdin argument, thus they always give the child the stdin of fw-context.
_INHERITING_CALLS = {
    ("subprocess", "getoutput"), ("subprocess", "getstatusoutput"),
    ("os", "system"), ("os", "popen"), ("os", "posix_spawn"), ("os", "posix_spawnp"),
}
_ASYNCIO_CALLS = {"create_subprocess_exec", "create_subprocess_shell"}
#: The values of stdin that are not the stdin of fw-context.
_OWN_STDIN = {"DEVNULL", "PIPE"}


def _source_trees():
    for path in sorted(_SOURCE.rglob("*.py")):
        if "cache_server" in path.relative_to(_SOURCE).parts:
            continue
        yield path.relative_to(_SOURCE), ast.parse(path.read_text(encoding="utf-8"))


def _gives_own_stdin(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "input":
            return True
        if keyword.arg == "stdin":
            value = keyword.value
            return isinstance(value, ast.Attribute) and value.attr in _OWN_STDIN
    return False


def _faults() -> list[str]:
    found = []
    for name, tree in _source_trees():
        for node in ast.walk(tree):
            # Only `subprocess.x(...)` is checked, thus no other name may hold it.
            if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                found.append(f"{name}:{node.lineno}: from subprocess import")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "subprocess" and alias.asname:
                        found.append(f"{name}:{node.lineno}: import subprocess as {alias.asname}")
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = ast.unparse(node.func.value)
            attribute = node.func.attr
            if (owner, attribute) in _INHERITING_CALLS or (owner == "os" and attribute.startswith("spawn")):
                found.append(f"{name}:{node.lineno}: {owner}.{attribute} gives the stdin of fw-context")
            elif (owner == "subprocess" and attribute in _SUBPROCESS_CALLS) or attribute in _ASYNCIO_CALLS:
                if not _gives_own_stdin(node):
                    found.append(f"{name}:{node.lineno}: {owner}.{attribute} without stdin=DEVNULL")
    return found


def test_each_program_gets_its_own_stdin():
    assert _faults() == []


_CHILD = """
import sys, threading, time
from pathlib import Path
from fw_context_mcp.mcp.shared.stale import _git_tu_candidates

# As the MCP server: a thread waits in a read of the stdin pipe.
threading.Thread(target=sys.stdin.buffer.readline, daemon=True).start()
time.sleep(0.5)
print(_git_tu_candidates(Path(sys.argv[1]), frozenset({".c"})), flush=True)
"""

#: Less than the timeout of the git listing (``stale._GIT_LS_TIMEOUT_S``),
#: thus a listing that only ends through its timeout fails the test.
_ANSWER_TIMEOUT_S = 20


@pytest.mark.skipif(shutil.which("git") is None, reason="the listing runs git")
def test_git_answers_while_a_thread_reads_the_stdin_pipe(tmp_path: Path):
    """The stdin stays an open pipe, as the pipe of an MCP client.

    Measured on Windows 11 with the old code: no answer (the test waited
    60 s).  The untracked file is in the listing, thus the answer tells a
    listing from a timeout or an error, which give None.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], stdin=subprocess.DEVNULL, check=True)
    (tmp_path / "a.c").write_text("int a;\n", encoding="utf-8")

    with subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    ) as proc:
        lines: list[bytes] = []
        reader = threading.Thread(target=lambda: lines.append(proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout=_ANSWER_TIMEOUT_S)
        proc.kill()

    assert [line.strip() for line in lines] == [b"['a.c']"]
