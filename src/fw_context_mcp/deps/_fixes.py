"""Auto-install functions for fixable dependencies.

Each fix function accepts a ``DepCheckResult`` and an optional project
root, then attempts to install or repair the dependency.  Returns
``(ok, message)``.

WHY: ``fw-context doctor --fix`` must be able to repair common issues
without the user typing multiple shell commands.  Each fix is a
separate function so the registry can map check names to fix actions
and so individual fixes can be tested in isolation.

Design:
* ``_pip_install`` wraps pip/uv with timeout and error handling.
* ``_ollama_pull`` uses the CLI binary when available (better UX
  with progress bar), falls back to HTTP API.
* Fix functions are thin wrappers — the real logic lives in the
  shared helpers to avoid duplication.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from shutil import which
from typing import Any

# Seconds without progress before a model pull is killed.  Pulling a
# multi-GB model can legitimately run far longer than any fixed timeout,
# so the timeout is stall-based: only an absence of progress (no new
# bytes) for this long aborts the pull.
STALL_TIMEOUT = 300


def _pip_install(package: str) -> tuple[bool, str]:
    """Install a Python package. Returns (ok, output).

    WHY (uv preference): ``uv pip install`` is 10-100× faster than
    ``pip install`` for cached packages.  However, uv only works inside
    a virtual environment (it requires VIRTUAL_ENV or a venv).  We
    check for both uv binary AND VIRTUAL_ENV before using it, then
    fall back to ``sys.executable -m pip`` which knows its own venv
    implicitly.
    """
    # Prefer uv only when inside a venv (VIRTUAL_ENV set) — otherwise
    # uv pip install fails with "No virtual environment found".
    # Always fall back to sys.executable which knows where its own venv is.
    if which("uv") and os.environ.get("VIRTUAL_ENV"):
        cmd = ["uv", "pip", "install", "--python", sys.executable, package]
    else:
        cmd = [sys.executable, "-m", "pip", "install", package]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode == 0:
            return True, result.stdout.strip() or "installed"
        return False, result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    except FileNotFoundError:
        return False, f"installer not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, "timed out after 180 s"


def _ollama_pull(model: str, url: str | None = None) -> tuple[bool, str]:
    """Pull an Ollama model. Returns (ok, output).

    WHY (CLI-first): The ``ollama pull`` CLI shows a progress bar and
    is already authenticated for self-hosted registries.  The HTTP API
    fallback exists for headless servers where the CLI is not installed
    but Ollama is running as a daemon.

    WHY stall-based timeout: a multi-GB pull may legitimately run for
    tens of minutes on a slow link.  A fixed timeout would kill a
    healthy download.  Instead the pull is aborted only when progress
    STOPS — no new bytes for ``STALL_TIMEOUT`` seconds means it is hung.
    """
    if which("ollama"):
        try:
            return _pull_via_cli(model)
        except FileNotFoundError:
            pass

    if url is None:
        url = "http://localhost:11434"

    return _pull_via_api(model, url)


def _kill(proc: subprocess.Popen) -> None:
    """Terminate a subprocess, escalating to SIGKILL when it ignores SIGTERM."""
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


def _extract_cli_error(tail: bytes) -> str:
    """Return the last meaningful error line from a merged ``ollama pull`` stream.

    Ollama prints carriage-return progress updates; on failure it emits an
    ``Error: ...`` line.  Splitting on both ``\\r`` and ``\\n`` and preferring
    a line that mentions "error" avoids returning garbled progress-bar bytes
    as the failure message.
    """
    text = tail.decode(errors="replace")
    parts = [p.strip() for p in text.replace("\r", "\n").split("\n") if p.strip()]
    for p in reversed(parts):
        if "error" in p.lower():
            return p
    return parts[-1] if parts else ""


def _pull_via_cli(model: str) -> tuple[bool, str]:
    """Pull *model* via the ``ollama`` CLI with a stall-based timeout.

    Reads the merged stdout+stderr stream from a background thread and
    echoes it to stderr, so the CLI progress bar stays visible during the
    pull.  Any new byte resets the stall clock; ``STALL_TIMEOUT`` seconds
    without bytes kills the process.

    WHY a reader thread instead of ``select``: ``select.select`` waits only
    on sockets on Windows, not on subprocess pipes, so the previous raw-fd
    approach would crash there.  A thread performing a blocking ``read`` is
    portable and keeps the same incremental-progress semantics.
    """
    import threading
    import time

    proc = subprocess.Popen(
        ["ollama", "pull", model],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Progress state is shared between the reader thread and this loop, so it
    # is guarded by a lock — a plain closure variable would be a data race.
    # ollama (a Go binary) writes progress to stderr unbuffered, so bytes
    # arrive promptly even when stdout is not a TTY (no block-buffer stalls).
    lock = threading.Lock()
    state: dict = {"last_byte": time.monotonic(), "tail": b""}

    def _read_stream() -> None:
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                sys.stderr.write(chunk.decode(errors="replace"))
                sys.stderr.flush()
                with lock:
                    state["last_byte"] = time.monotonic()
                    state["tail"] = (state["tail"] + chunk)[-4096:]
        except OSError:
            pass

    reader = threading.Thread(target=_read_stream, daemon=True)
    reader.start()
    try:
        while proc.poll() is None:
            with lock:
                last = state["last_byte"]
            if time.monotonic() - last > STALL_TIMEOUT:
                _kill(proc)
                reader.join(timeout=1.0)
                return False, (
                    f"pull stalled — no progress for {STALL_TIMEOUT}s; "
                    f"run `ollama pull {model}` manually"
                )
            time.sleep(1.0)
        reader.join(timeout=5.0)
        # End the progress bar on a clean line — a trailing ``\r`` update
        # would otherwise leave the caller's result message mid-line.
        sys.stderr.write("\n")
        sys.stderr.flush()
        with lock:
            tail = state["tail"]
        if proc.returncode == 0:
            return True, "pulled"
        return False, _extract_cli_error(tail) or f"exit {proc.returncode}"
    finally:
        if proc.poll() is None:
            _kill(proc)


def _pull_via_api(model: str, url: str) -> tuple[bool, str]:
    """Pull *model* via the Ollama HTTP API with a stall-based timeout.

    Streams ``/api/pull`` and folds ``status``/``error`` from the
    NDJSON stream.  A stall is enforced by the httpx per-chunk ``read``
    timeout (``STALL_TIMEOUT``) — if the server stops sending bytes for
    that long, ``httpx.ReadTimeout`` fires and is reported as a stall.
    A long "verifying sha256 digest" phase (which keeps emitting lines)
    does not trigger a false abort, because each received line resets the
    read deadline.
    """
    import json

    import httpx

    try:
        with httpx.stream(
            "POST",
            f"{url.rstrip('/')}/api/pull",
            json={"name": model, "stream": True},
            timeout=httpx.Timeout(connect=10.0, read=STALL_TIMEOUT, write=10.0, pool=10.0),
        ) as resp:
            resp.raise_for_status()
            status = "unknown"
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                status = data.get("status", status)
                if data.get("error"):
                    return False, data["error"]
                if status == "success":
                    return True, "pulled via API"
            if status == "success":
                return True, "pulled via API"
            return False, status or "unknown API error"
    except httpx.ConnectError:
        return False, f"Ollama not reachable at {url}"
    except httpx.ReadTimeout:
        return False, (
            f"pull stalled — no progress for {STALL_TIMEOUT}s; "
            f"run `ollama pull {model}` manually"
        )
    except httpx.HTTPError as e:
        return False, str(e)
    except OSError as e:
        return False, str(e)


# ── Fix functions (one per fixable dependency) ──────────────────────────


def fix_pysqlite3(_result, _project_root=None) -> tuple[bool, str]:
    """Install pysqlite3."""
    return _pip_install("pysqlite3")


def fix_sqlite_vec(_result, _project_root=None) -> tuple[bool, str]:
    """Install sqlite-vec."""
    return _pip_install("sqlite-vec")


def fix_libclang_python(_result, _project_root=None) -> tuple[bool, str]:
    """Install libclang."""
    return _pip_install("libclang")


def fix_watchfiles(_result, _project_root=None) -> tuple[bool, str]:
    """Install watchfiles."""
    return _pip_install("watchfiles")


def fix_tomli_w(_result, _project_root=None) -> tuple[bool, str]:
    """Install tomli-w (required for config writes during init)."""
    return _pip_install("tomli-w")


def fix_chat_model(result, _project_root=None) -> tuple[bool, str]:
    """Pull the configured chat model."""
    # Extract model name from the result message or fix_cmd
    model = _extract_model_name(result, "ollama")
    return _ollama_pull(model)


def fix_embed_model(result, _project_root=None) -> tuple[bool, str]:
    """Pull the configured embedding model."""
    model = _extract_model_name(result, "ollama")
    return _ollama_pull(model)




def fix_sqlite_ext(_result, _project_root=None) -> tuple[bool, str]:
    """Cannot auto-fix — Python must be reinstalled with --enable-loadable-sqlite-extensions."""
    return False, (
        "sqlite3 loadable extensions are not enabled. "
        "Reinstall Python with --enable-loadable-sqlite-extensions "
        "(e.g. PYTHON_CONFIGURE_OPTS=\"--enable-loadable-sqlite-extensions\" pyenv install <version>), "
        "or install pysqlite3 as a workaround."
    )

def _extract_model_name(result, default_url: str) -> str:
    """Extract model name from fix_cmd like 'ollama pull <model>'."""
    if result.fix_cmd and result.fix_cmd.startswith("ollama pull "):
        return result.fix_cmd[len("ollama pull "):].strip()
    # Fallback: parse from message
    return result.message.split("'")[1] if "'" in result.message else "unknown"


# ── Registry ────────────────────────────────────────────────────────────


FIXABLE: dict[str, Any] = {
    "pysqlite3": fix_pysqlite3,
    "sqlite-vec": fix_sqlite_vec,
    "libclang-python": fix_libclang_python,
    "watchfiles": fix_watchfiles,
    "tomli-w": fix_tomli_w,
    "chat-model": fix_chat_model,
    "embed-model": fix_embed_model,
    "sqlite-ext": fix_sqlite_ext,
}


def run_fix(name: str, result, project_root: str | Path | None = None) -> tuple[bool, str]:
    """Run the fix for *name*. Returns (ok, message).

    WHY (registry dispatch): A dictionary lookup is safer and more
    testable than if/elif chains.  New fixes can be added by
    registering a function in FIXABLE without modifying dispatch logic.
    """
    fn = FIXABLE.get(name)
    if fn is None:
        return False, f"no fix available for '{name}'"
    try:
        return fn(result, project_root)
    except Exception as exc:
        return False, str(exc)
