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
    """
    # Prefer the CLI binary — streams progress and is already authenticated
    if which("ollama"):
        try:
            result = subprocess.run(
                ["ollama", "pull", model],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                return True, result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "pulled"
            return False, result.stderr.strip() or f"exit {result.returncode}"
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired:
            return False, f"timed out after 600 s — try running `ollama pull {model}` manually"

    # Fallback: HTTP pull via the Ollama API
    if url is None:
        url = "http://localhost:11434"

    import httpx

    try:
        resp = httpx.post(
            f"{url.rstrip('/')}/api/pull",
            json={"name": model, "stream": False},
            timeout=600,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return True, "pulled via API"
        return False, data.get("error", "unknown API error")
    except httpx.ConnectError:
        return False, f"Ollama not reachable at {url}"
    except httpx.HTTPError as e:
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
