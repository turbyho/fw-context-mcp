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
to ``127.0.0.1:8000``).

Why a CLI (not just ``python -m uvicorn``)?
-------------------------------------------
The CLI wraps multiple lifecycle operations (init, run, install,
setup) into a single entry point.  Operators don't need to remember
uvicorn flags, database URLs, or systemd unit paths — each command
handles its own dependencies and validation.  This is the same
pattern used by production tools like Gunicorn and Celery.
"""

from __future__ import annotations

import argparse
import os
import sys


def cmd_init(args: argparse.Namespace) -> int:
    """Create the meta + cache schema and print an admin token.

    Must be run BEFORE ``run``.  Creates both databases (``fw_cache_meta``,
    ``fw_cache``) and their tables, then generates a single admin token
    with ``project_id IS NULL`` (scoped to all projects).

    Why a separate init step?
    -------------------------
    Schema creation requires CREATE DATABASE privileges — the cache
    server's PostgreSQL user may not have these.  Separating init from
    run allows the DBA to run init with elevated privileges once,
    then the server runs with a lower-privilege user indefinitely.
    """
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
            token = await backend.create_admin_token()
            print("Admin token:", token)
            print()
            print("Use this token as FW_CACHE_ADMIN_TOKEN for fw-cache-admin commands.")
            print("Then create projects with: fw-cache-admin project create <id>")
            return 0
        finally:
            await backend.close()

    return asyncio.run(_init())


def cmd_run(args: argparse.Namespace) -> int:
    """Start the FastAPI cache server via uvicorn.

    Binds to ``FW_CACHE_HOST``:``FW_CACHE_PORT`` (default 127.0.0.1:8000).
    The server runs in the foreground — use systemd/launchd for daemonization.

    Why uvicorn (not hypercorn / Daphne)?
    ------------------------------------
    Uvicorn is the reference ASGI server for FastAPI.  It uses uvloop
    on Linux (faster than asyncio's default event loop) and has the
    most extensive production deployment documentation.  Hypercorn is
    a valid alternative for Windows; on Linux/macOS, uvicorn is the
    standard choice.
    """
    db_url = os.environ.get("FW_CACHE_DB_URL", "")
    if not db_url:
        print("Error: FW_CACHE_DB_URL environment variable is required", file=sys.stderr)
        return 1

    host = args.host or os.environ.get("FW_CACHE_HOST", "127.0.0.1")
    port = int(args.port or os.environ.get("FW_CACHE_PORT", "8000"))

    from .app import create_app

    os.environ["FW_CACHE_DB_URL"] = db_url
    app = create_app()

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def cmd_install_systemd(args: argparse.Namespace) -> int:
    """Generate and optionally install a systemd unit.

    With ``--dry-run``, prints the unit to stdout.  Otherwise, writes it
    to ``/etc/systemd/system/fw-cache-server.service`` via sudo.

    Why systemd (not SysV init / supervisord)?
    ------------------------------------------
    Systemd is the standard init system on all modern Linux distributions
    (Debian 8+, Ubuntu 16.04+, RHEL 7+, all derivatives).  It provides
    automatic restart on failure, log capture via journald, and dependency
    ordering (``After=postgresql.service``).  No extra supervisor process
    needed — systemd IS the process supervisor on modern Linux.
    """
    from .install import generate_systemd_unit, install_systemd_unit

    unit_text = generate_systemd_unit()
    if args.dry_run:
        print(unit_text)
        return 0
    install_systemd_unit(unit_text)
    return 0


def cmd_install_launchd(args: argparse.Namespace) -> int:
    """Generate and optionally install a launchd plist (macOS).

    With ``--dry-run``, prints the plist to stdout.  Otherwise, writes it
    to ``~/Library/LaunchAgents/com.fwcontext.cache-server.plist``.

    Why launchd (not homebrew services)?
    -----------------------------------
    Homebrew services are a wrapper around launchd.  Direct launchd
    plists work regardless of Homebrew installation and give full
    control over environment variables, log paths, and restart
    behavior.
    """
    from .install import generate_launchd_plist, install_launchd_plist

    plist_text = generate_launchd_plist()
    if args.dry_run:
        print(plist_text)
        return 0
    install_launchd_plist(plist_text)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Interactive installation wizard — delegates to cache_server.setup.

    The wizard walks through: OS detection, PostgreSQL installation,
    database creation, server init, project creation, service installation,
    and nginx HTTPS configuration.  Each step detects current state and
    only prompts when action is needed.

    Why a wizard?
    ------------
    Setting up a cache server involves ~8 distinct steps across
    multiple system components (PostgreSQL, systemd, nginx, certbot).
    A wizard reduces the error-prone manual process to a single
    command with guided prompts.
    """
    from .setup import setup_wizard

    return setup_wizard()


def main() -> None:
    """Entry point for ``fw-cache-server`` CLI.

    Registers five subcommands (init, run, install-systemd,
    install-launchd, setup) and dispatches to the appropriate handler.
    """
    parser = argparse.ArgumentParser(
        prog="fw-cache-server",
        description="Shared LLM analysis cache server for fw-context-mcp",
    )
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Initialise database schema and create admin project")
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="Start the cache server")
    p_run.add_argument("--host", default=None, help="Bind address (default: 127.0.0.1)")
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
