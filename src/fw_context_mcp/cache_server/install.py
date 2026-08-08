"""Systemd and launchd unit generators for the cache server.

Generates unit files and provides functions to install them.

Why separate generators from the install functions?
---------------------------------------------------
Separation of generation and installation enables:
1. ``--dry-run`` — print the unit file for inspection before writing.
2. Testing — unit tests can assert the generated content without sudo.
3. Manual installation — operators who manage configs via Ansible or
   Puppet can consume the generated text directly.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _server_executable() -> str:
    """Return the path to the fw-cache-server executable.

    Checks /opt (production), PATH, and current venv.  Falls back to
    ``python -m fw_context_mcp.cache_server.cli`` — requires the
    ``[cache-server]`` extras to be installed in the current environment.

    Why check multiple locations?
    -----------------------------
    The setup wizard installs to ``/opt/fw-cache-server/venv``.  But
    the operator might have installed via pip into a user venv or
    system Python.  Checking PATH and the current venv covers these
    cases without requiring the operator to configure a path manually.
    The ``python -m`` fallback works in any environment with the
    package installed — useful for pipx and uv tool installs.
    """
    for candidate in (
        "/opt/fw-cache-server/venv/bin/fw-cache-server",
        shutil.which("fw-cache-server"),
        Path(sys.executable).parent / "fw-cache-server",
    ):
        if candidate and Path(str(candidate)).exists():
            return str(candidate)
    return f"{sys.executable} -m fw_context_mcp.cache_server.cli"


SYSTEMD_UNIT_TEMPLATE = """[Unit]
Description=fw-context LLM Analysis Cache Server
After=network.target postgresql.service

[Service]
Type=simple
User={user}
EnvironmentFile=/var/lib/fw-cache-server/db.env
Environment=FW_CACHE_PORT={port}
ExecStart={executable} run
Restart=always
RestartSec=5
ProtectSystem=strict
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
"""


def generate_systemd_unit(
    user: str | None = None,
    port: int = 8000,
) -> str:
    """Generate a systemd unit file content.

    Database credentials are read from ``/var/lib/fw-cache-server/db.env``
    via ``EnvironmentFile=`` — they are NOT embedded in the unit file.

    Why EnvironmentFile (not Environment=)?
    ---------------------------------------
    Embedding the database URL in the unit file would leak the password
    to anyone who can run ``systemctl cat fw-cache-server``.  Using
    ``EnvironmentFile=`` keeps the credentials in a separate file with
    ``chmod 640 root:fw-cache`` — readable only by root and the fw-cache
    group.  The unit file itself can be broadly readable.

    Why ``ProtectSystem=strict``?
    -----------------------------
    The cache server only needs network access and PostgreSQL connectivity.
    It should NOT be able to modify system files, load kernel modules, or
    access other users' home directories.  Strict protection limits
    blast radius if the server process is compromised.
    """
    env_port = int(os.environ.get("FW_CACHE_PORT", str(port)))
    return SYSTEMD_UNIT_TEMPLATE.format(
        user=user or "fw-cache",
        port=env_port,
        executable=_server_executable(),
    )


def install_systemd_unit(unit_text: str) -> None:
    """Write the unit file to /etc/systemd/system/fw-cache-server.service (via sudo).

    Why check passwordless sudo first?
    ----------------------------------
    ``sudo tee`` will prompt for a password interactively — but this
    function runs inside a setup wizard or CLI command where stdin may
    not be a TTY (e.g. piped from Ansible, run via SSH without ``-t``).
    Checking ``sudo -n true`` (non-interactive) first detects this case
    and falls back to printing the unit for manual installation instead
    of hanging indefinitely on a password prompt.
    """
    import subprocess
    unit_path = "/etc/systemd/system/fw-cache-server.service"

    # Check passwordless sudo before attempting tee
    try:
        subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        print("Passwordless sudo not available — printing unit for manual install:", file=sys.stderr)
        print(unit_text)
        print(f"\nSave the above to {unit_path}, then run:")
        print("  sudo systemctl daemon-reload")
        print("  sudo systemctl enable --now fw-cache-server")
        return

    try:
        result = subprocess.run(
            ["sudo", "tee", unit_path],
            input=unit_text, text=True, capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            print(f"Unit written to {unit_path}")
            print("To enable and start:")
            print("  sudo systemctl daemon-reload")
            print("  sudo systemctl enable --now fw-cache-server")
            return
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"Failed to install unit file: {e}", file=sys.stderr)
    # Fallback: print for manual install
    print(unit_text)
    print(f"\nSave the above to {unit_path}, then run:")
    print("  sudo systemctl daemon-reload")
    print("  sudo systemctl enable --now fw-cache-server")


LAUNCHD_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.fwcontext.cache-server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>. /var/lib/fw-cache-server/db.env 2>/dev/null \
         || . ~/.fw-context/db.env 2>/dev/null; \
         exec {executable} run --host {host} --port {port}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/fw-cache-server.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/fw-cache-server-err.log</string>
</dict>
</plist>
"""


def generate_launchd_plist(
    host: str = "127.0.0.1",
    port: int = 8000,
) -> str:
    """Generate a launchd plist file content.

    Database credentials are read from ``/var/lib/fw-cache-server/db.env``
    (or ``~/.fw-context/db.env`` as fallback) at launch time — they are
    NOT embedded in the plist file.

    Why shell sourcing instead of EnvironmentVariables?
    ---------------------------------------------------
    launchd's ``EnvironmentVariables`` dictionary is the standard way
    to pass env vars, but it can't source shell files with export
    statements.  Using ``/bin/sh -c '. file; exec ...'`` lets us reuse
    the same ``db.env`` file that systemd reads via ``EnvironmentFile=``
    — single source of truth for credentials across both init systems.

    Why ``KeepAlive`` true?
    ----------------------
    Without KeepAlive, launchd monitors the first process only — if
    uvicorn forks workers, launchd won't restart them on crash.
    KeepAlive ensures launchd watches the process tree and restarts
    when any component dies.
    """
    env_port = int(os.environ.get("FW_CACHE_PORT", str(port)))
    return LAUNCHD_PLIST_TEMPLATE.format(
        executable=_server_executable(),
        host=host,
        port=env_port,
        log_dir="/var/log",
    )


def install_launchd_plist(plist_text: str) -> None:
    """Write the plist to ~/Library/LaunchAgents/com.fwcontext.cache-server.plist.

    Why user LaunchAgents (not /Library/LaunchDaemons)?
    ---------------------------------------------------
    System LaunchDaemons require root and run as root — inappropriate
    for a user-level service.  User LaunchAgents run under the
    installing user's account and are loaded by that user's launchd
    instance.  This follows the macOS convention: user services go
    in ~/Library/LaunchAgents, system services in /Library/LaunchDaemons.
    """
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.fwcontext.cache-server.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist_text)
    print(f"Plist written to {plist_path}")
    print("To load:")
    print(f"  launchctl load {plist_path}")
