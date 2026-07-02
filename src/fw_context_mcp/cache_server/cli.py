"""``fw-cache-server`` — run the shared LLM analysis cache server.

Commands:
    init                Initialise the database schema + admin token
    run                 Start the server (foreground)
    install-systemd     Generate and install a systemd unit
    install-launchd     Generate a launchd plist (macOS)
    setup               Interactive installation wizard (delegates to
                        ``cache_server.setup``)

Configuration is read from the ``FW_CACHE_DB_URL`` environment variable
(required) and ``FW_CACHE_HOST`` / ``FW_CACHE_PORT`` (optional, defaults
to ``0.0.0.0:8000``).
"""

from __future__ import annotations

import argparse
import os
import sys


def cmd_init(args: argparse.Namespace) -> int:
    """Create the meta + cache schema and print an admin token."""
    db_url = os.environ.get("FW_CACHE_DB_URL", "")
    if not db_url:
        print("Error: FW_CACHE_DB_URL environment variable is required", file=sys.stderr)
        print("Usage: FW_CACHE_DB_URL=postgresql://fw_cache:pass@localhost:5432 fw-cache-server init", file=sys.stderr)
        return 1

    import asyncio

    async def _init() -> int:
        from .backend import CacheBackend

        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            await backend.init_schema()
            token = await backend.create_project("default")
            if token is None:
                token = "project 'default' already exists — use fw-cache-admin to manage"
            print("Admin token:", token)
            return 0
        finally:
            await backend.close()

    return asyncio.run(_init())


def cmd_run(args: argparse.Namespace) -> int:
    """Start the FastAPI cache server."""
    db_url = os.environ.get("FW_CACHE_DB_URL", "")
    if not db_url:
        print("Error: FW_CACHE_DB_URL environment variable is required", file=sys.stderr)
        return 1

    host = args.host or os.environ.get("FW_CACHE_HOST", "0.0.0.0")
    port = int(args.port or os.environ.get("FW_CACHE_PORT", "8000"))

    from .app import create_app

    app = create_app(db_url)

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def cmd_install_systemd(args: argparse.Namespace) -> int:
    """Generate and optionally install a systemd unit."""
    from .install import generate_systemd_unit, install_systemd_unit

    unit_text = generate_systemd_unit()
    if args.dry_run:
        print(unit_text)
        return 0
    install_systemd_unit(unit_text)
    return 0


def cmd_install_launchd(args: argparse.Namespace) -> int:
    """Generate and optionally install a launchd plist (macOS)."""
    from .install import generate_launchd_plist, install_launchd_plist

    plist_text = generate_launchd_plist()
    if args.dry_run:
        print(plist_text)
        return 0
    install_launchd_plist(plist_text)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Interactive installation wizard."""
    from .setup import setup_wizard

    return setup_wizard()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fw-cache-server",
        description="Shared LLM analysis cache server for fw-context-mcp",
    )
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Initialise database schema and create admin project")
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="Start the cache server")
    p_run.add_argument("--host", default=None, help="Bind address (default: 0.0.0.0)")
    p_run.add_argument("--port", default=None, help="Bind port (default: 8000)")
    p_run.set_defaults(func=cmd_run)

    p_install_systemd = sub.add_parser("install-systemd", help="Generate and install a systemd unit")
    p_install_systemd.add_argument("--dry-run", action="store_true", help="Print the unit to stdout instead of installing")
    p_install_systemd.set_defaults(func=cmd_install_systemd)

    p_install_launchd = sub.add_parser("install-launchd", help="Generate a launchd plist (macOS)")
    p_install_launchd.add_argument("--dry-run", action="store_true", help="Print the plist to stdout instead of installing")
    p_install_launchd.set_defaults(func=cmd_install_launchd)

    p_setup = sub.add_parser("setup", help="Interactive installation wizard")
    p_setup.set_defaults(func=cmd_setup)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(args.func(args))
