"""Systemd and launchd unit generators for the cache server.

Generates unit files and provides functions to install them.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _server_executable() -> str:
    """Return the path to the fw-cache-server executable."""
    # Check /opt (production install) first, then PATH, then current venv
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
Environment=FW_CACHE_DB_URL={db_url}
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
    db_url: str | None = None,
    port: int = 8000,
) -> str:
    """Generate a systemd unit file content."""
    return SYSTEMD_UNIT_TEMPLATE.format(
        user=user or "fw-cache",
        db_url=db_url or os.environ.get("FW_CACHE_DB_URL", "postgresql://fw_cache:changeme@localhost:5432"),
        port=port or int(os.environ.get("FW_CACHE_PORT", "8000")),
        executable=_server_executable(),
    )


def install_systemd_unit(unit_text: str) -> None:
    """Write the unit file to /etc/systemd/system/fw-cache-server.service (via sudo)."""
    import subprocess
    unit_path = "/etc/systemd/system/fw-cache-server.service"
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
    except Exception:
        pass
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
        <string>{executable}</string>
        <string>run</string>
        <string>--host</string>
        <string>{host}</string>
        <string>--port</string>
        <string>{port}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>FW_CACHE_DB_URL</key>
        <string>{db_url}</string>
    </dict>
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
    db_url: str | None = None,
) -> str:
    """Generate a launchd plist file content."""
    return LAUNCHD_PLIST_TEMPLATE.format(
        executable=_server_executable(),
        host=host,
        port=port,
        db_url=db_url or os.environ.get("FW_CACHE_DB_URL", "postgresql://fw_cache:changeme@localhost:5432"),
        log_dir=os.path.expanduser("~"),
    )


def install_launchd_plist(plist_text: str) -> None:
    """Write the plist to ~/Library/LaunchAgents/com.fwcontext.cache-server.plist."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.fwcontext.cache-server.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist_text)
    print(f"Plist written to {plist_path}")
    print("To load:")
    print(f"  launchctl load {plist_path}")
