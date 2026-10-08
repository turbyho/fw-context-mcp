"""Tell the caller about a newer release and about a server that does not run the installed code.

Two facts about the version of fw-context reach ``get_active_build``:

- **The server does not run the installed code.**  ``__version__`` is read
  once, when the process imports the package.  When the operator installs
  a different version while the MCP server runs (an upgrade or a
  downgrade), the installed version changes and the process keeps the
  code that it imported.  No command repairs this: the operator must restart the LLM
  client, because the MCP server is a child process of that client.  This
  check reads only local package metadata.
- **A newer release is on PyPI.**  One background thread per server process
  gets the JSON document of the package from PyPI and writes the newest
  version to a state file in the global fw-context directory.  After a
  request that succeeded, the thread sends no request for
  :data:`CHECK_INTERVAL`, thus a session start makes no request when
  another session made one recently.  A request that failed writes
  nothing, thus a machine without network access tries again at each
  server start.  The request runs in the background and costs no session
  time.
  ``get_active_build`` reads only the state file, never the network: a
  query must not wait for PyPI, and a machine without network access must
  get the same answers as a machine with it.

The network check is on by default.  ``[updates] check = false`` in the
global config, or a value in :data:`DISABLE_ENV`, stops it.  An editable
install (PEP 610 ``direct_url.json``) gets no network check, because its
package metadata does not follow the source tree: the version in the
metadata stays at the value of the last install, and a comparison with
PyPI would report a release that the source tree already holds.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from packaging.version import InvalidVersion, Version

from . import __version__
from .utils import fw_context_home, write_text_atomic

log = logging.getLogger(__name__)

DISTRIBUTION = "fw-context-mcp"
PYPI_URL = "https://pypi.org/pypi/fw-context-mcp/json"

# Any value other than "" and "0" stops the network check.  The variable
# lets a CI job or a test session stop it without a write to the global
# config of the operator.
DISABLE_ENV = "FW_CONTEXT_NO_UPDATE_CHECK"

# One request a day is sufficient for a release cadence of days to weeks,
# and it keeps the number of requests independent of the number of sessions.
CHECK_INTERVAL = timedelta(hours=24)

# The request runs in a background thread, thus no caller waits for it.  The
# short limits stop a thread that hangs on a network that drops packets.
_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)

_STATE_FILE_NAME = "update_check.json"

# The errors that a failed check can cause: no network, an HTTP status, a
# document that is not JSON (ValueError), a document of a different shape
# (KeyError, TypeError), a version that is not PEP 440 (InvalidVersion is a
# ValueError), and a global directory that fw-context cannot write (OSError).
_CHECK_ERRORS = (httpx.HTTPError, OSError, ValueError, KeyError, TypeError)

#: The distributions whose damaged ``direct_url.json`` got a warning in
#: this process (see :func:`is_editable_install`).
_direct_url_warned: set[str] = set()


@dataclass(frozen=True)
class UpdateState:
    """The result of the last PyPI request that succeeded.

    Attributes:
        checked_at: The UTC time of the request.
        latest: The newest version of the package on PyPI.
    """

    checked_at: datetime
    latest: str


def installed_version() -> str | None:
    """Return the version in the package metadata on disk now.

    ``importlib.metadata`` reads the metadata again at each call, thus the
    value follows an upgrade while the process runs.  ``None`` means that
    no metadata of the package exists: the package is not installed, and no
    version is available for a comparison.
    """
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None


def restart_reason(running: str = __version__) -> str | None:
    """Return the text that tells the caller to restart the LLM client.

    The text comes back when the installed version is not the version that
    this process loaded.  A downgrade gives the text too: the process then
    runs code that is not on disk any more.  When no package is installed,
    there is no version to compare, and the function gives ``None``.

    Args:
        running: The version that this process loaded at import.
    """
    installed = installed_version()
    if installed is None or installed == running:
        return None
    return (
        f"The installed fw-context is {installed}, and this server process runs "
        f"{running}, the version that was installed when the LLM client started "
        f"it. Tell the operator to restart the LLM client (Claude Code, opencode, "
        f"or the client in use). The MCP server is a child process of that client, "
        f"thus nobody can restart the server alone."
    )


def is_editable_install() -> bool | None:
    """Return True when the installed package is an editable install.

    The installer writes ``direct_url.json`` (PEP 610) into the metadata
    and sets ``dir_info.editable`` for an editable install.  The file is
    absent for an install from an index such as PyPI.

    ``None`` means that the file is there and is not JSON, thus the install
    type is not known.  The callers then skip the check, because a guess
    can report a release that the source tree already holds.  The answer
    of ``get_active_build`` must not fail for this file, thus the function
    does not raise.
    """
    try:
        text = importlib.metadata.distribution(DISTRIBUTION).read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return False
    if text is None:
        return False
    try:
        data = json.loads(text)
    except ValueError:
        # get_active_build calls this function at each call, and one
        # warning for each process is sufficient.
        if DISTRIBUTION not in _direct_url_warned:
            _direct_url_warned.add(DISTRIBUTION)
            log.warning("direct_url.json of %s is not JSON — the update check is skipped", DISTRIBUTION)
        return None
    dir_info = data.get("dir_info") if isinstance(data, dict) else None
    return isinstance(dir_info, dict) and dir_info.get("editable") is True


def _check_applies(config_check: bool) -> bool:
    """Return True when the operator lets the check run, and the install is not editable."""
    return update_check_enabled(config_check) and is_editable_install() is False


def update_check_enabled(config_check: bool) -> bool:
    """Return True when the operator lets fw-context ask PyPI.

    Args:
        config_check: The value of ``[updates] check`` in the global config.
    """
    if os.environ.get(DISABLE_ENV, "") not in ("", "0"):
        return False
    return config_check


def state_path() -> Path:
    """Return the path of the state file.

    The path is read at each call, not at import, because a test session
    and a second installation set ``FW_CONTEXT_HOME`` after the import.
    """
    return fw_context_home() / _STATE_FILE_NAME


def read_state() -> UpdateState | None:
    """Return the stored result of the last PyPI request.

    ``None`` means that no request succeeded yet.  A file that fw-context
    cannot parse gives ``None`` too: the next request writes it again, and
    a damaged state file must not stop the answer of ``get_active_build``.
    """
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        checked_at = datetime.fromisoformat(data["checked_at"])
        latest = str(data["latest"])
        Version(latest)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        log.debug("Update state file %s is not usable", state_path(), exc_info=True)
        return None
    if checked_at.tzinfo is None:
        return None
    return UpdateState(checked_at=checked_at, latest=latest)


def refresh_latest_version(now: datetime | None = None) -> None:
    """Ask PyPI for the newest version, and store it in the state file.

    The function makes no request when the stored result is younger than
    :data:`CHECK_INTERVAL`.  A failed request writes nothing, thus the next
    server start tries again.  The errors go to the caller.

    Args:
        now: The current time.  Tests give it to move the clock.
    """
    current = now if now is not None else datetime.now(UTC)
    state = read_state()
    # A time in the future (a clock that moved back, a home directory that
    # two machines share) gives a negative age.  Without the lower bound,
    # the check stops until the clock gets to the stored time.
    if state is not None and timedelta(0) <= current - state.checked_at < CHECK_INTERVAL:
        return
    response = httpx.get(PYPI_URL, timeout=_TIMEOUT)
    response.raise_for_status()
    latest = str(response.json()["info"]["version"])
    Version(latest)
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({"checked_at": current.isoformat(), "latest": latest}))


def _refresh_in_background() -> None:
    """Run :func:`refresh_latest_version` and log a failure.

    A failed check is a fact about the network of the machine, not about a
    query, thus it goes to the debug log and never to a tool answer.  The
    next server start tries again.
    """
    try:
        refresh_latest_version()
    except _CHECK_ERRORS:
        log.debug("Update check failed", exc_info=True)


def start_update_check(config_check: bool) -> threading.Thread | None:
    """Start the background PyPI request when the check applies.

    The check does not apply when the operator stopped it, and for an
    editable install (see the module docstring).

    Args:
        config_check: The value of ``[updates] check`` in the global config.

    Returns:
        The started thread, or ``None`` when the check does not apply.
    """
    if not _check_applies(config_check):
        return None
    # daemon=True: the thread must never keep the server process alive.  A
    # request that the process exit stops writes no state file, and the
    # next server start tries again.
    thread = threading.Thread(target=_refresh_in_background, daemon=True, name="fw-context-update-check")
    thread.start()
    return thread


def newer_release(config_check: bool) -> tuple[str, str] | None:
    """Return ``(latest, installed)`` when PyPI has a newer release.

    The data comes from the state file only.  It compares with the
    installed version and not with the running one: when the two are
    different, :func:`restart_reason` already tells the caller what to do.

    Args:
        config_check: The value of ``[updates] check`` in the global config.
            When the operator stopped the check, an old state file gives no
            result either.
    """
    if not _check_applies(config_check):
        return None
    installed = installed_version()
    state = read_state()
    if installed is None or state is None:
        return None
    try:
        if Version(state.latest) <= Version(installed):
            return None
    except InvalidVersion:
        log.debug("Installed version %r is not PEP 440", installed)
        return None
    return state.latest, installed


def update_notice(config_check: bool) -> str | None:
    """Return the text about a newer release for the LLM, or ``None``.

    The text tells the LLM to tell the operator, and not to upgrade the
    package itself: only the operator knows the tool that installed it
    (pip, pipx, uv, or ``make install`` from a clone).

    Args:
        config_check: The value of ``[updates] check`` in the global config.
    """
    found = newer_release(config_check)
    if found is None:
        return None
    latest, installed = found
    return (
        f"fw-context-mcp {latest} is available on PyPI, and this machine has "
        f"{installed}. Tell the operator one time in this session. Do not upgrade "
        f"fw-context yourself. The operator upgrades it with the tool that installed "
        f"it, and then restarts the LLM client."
    )
