"""``fw-context watch`` — background watcher daemon management."""

from __future__ import annotations

import argparse
import sqlite3
import sys


def cmd_watch(args: argparse.Namespace) -> int:
    """Dispatcher for ``fw-context watch`` sub-subcommands."""
    if getattr(args, "watch_command", None) is None:
        print("Usage: fw-context watch <status|restart>")
        return 1
    return args.func(args)


def cmd_watch_status(args: argparse.Namespace) -> int:
    """Print the watcher daemon status for a project."""

    from ..config import derive_project_id
    from ..config import load as load_config
    from ..mcp.background import _is_bg_reindex_running
    from ..mcp.daemon import DAEMON_SOCK_NAME, ping_daemon
    from ..utils import resolve_project_root

    root = resolve_project_root(args.project)
    cfg = load_config(project_root=root)
    project_id = derive_project_id(root)
    db_dir = cfg.index.db_dir / project_id

    pid_file = db_dir / "watcher.pid"
    sock_path = db_dir / DAEMON_SOCK_NAME
    log_file = db_dir / "reindex.log"

    print(f"Project:    {root.name}")
    print(f"Path:       {root}")
    print(f"DB dir:     {db_dir}")

    # Daemon status
    alive = ping_daemon(root) if sock_path.exists() else False
    if alive:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                import time as _time

                uptime_s = _time.time() - pid_file.stat().st_mtime
                print(f"Daemon:     running (pid {pid}, uptime {int(uptime_s)}s)")
            except (OSError, ValueError):
                print("Daemon:     running")
        else:
            print("Daemon:     running")
    else:
        if sock_path.exists():
            print("Daemon:     not responding (socket exists but no response)")
        elif pid_file.exists():
            print("Daemon:     dead (pid file exists but socket missing)")
        else:
            print("Daemon:     not running")

    # Socket
    print(f"Socket:     {sock_path}{' (active)' if alive else ''}")

    # Modified files count
    db_path = db_dir / "index.db"
    if db_path.exists():
        try:
            from ..indexer.db import get_active_config, open_db
            from ..mcp.shared.stale import _count_modified_files

            conn = open_db(db_path)
            try:
                cfg_data = get_active_config(conn, project_id)
                if cfg_data:
                    mod_count = _count_modified_files(conn, cfg_data["config_hash"], root, use_cache=False)
                    print(f"Modified:   {mod_count} file(s)")
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            pass

    # Index subprocess
    if _is_bg_reindex_running(root):
        print("Index:      running")
    else:
        print("Index:      idle")

    # Last index log line
    if log_file.exists():
        try:
            lines = log_file.read_text().splitlines()
            if lines:
                print(f"Last index: {lines[-1]}")
        except OSError:
            pass

    return 0


def cmd_watch_restart(args: argparse.Namespace) -> int:
    """Restart the watcher daemon for a project."""
    import os as _os
    import signal as _signal
    import time as _time

    from ..config import derive_project_id
    from ..config import load as load_config
    from ..mcp.background import _ensure_daemon_running
    from ..mcp.daemon import DAEMON_SOCK_NAME, ping_daemon
    from ..utils import resolve_project_root

    root = resolve_project_root(args.project)
    cfg = load_config(project_root=root)
    project_id = derive_project_id(root)
    db_dir = cfg.index.db_dir / project_id

    pid_file = db_dir / "watcher.pid"
    sock_path = db_dir / DAEMON_SOCK_NAME

    # Kill existing daemon
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            print(f"Stopping daemon (pid {pid})...")
            _os.kill(pid, _signal.SIGTERM)
            # Wait for shutdown
            for _ in range(30):  # 3 seconds max
                try:
                    _os.kill(pid, 0)
                except OSError:
                    break
                _time.sleep(0.1)
            else:
                print("Daemon did not stop, sending SIGKILL...")
                try:
                    _os.kill(pid, _signal.SIGKILL)
                except OSError:
                    pass
                _time.sleep(0.5)
            print("Old daemon stopped.")
        except (ValueError, OSError) as e:
            print(f"Could not read/stop old daemon: {e}")

    # Clean up leftover files
    pid_file.unlink(missing_ok=True)
    sock_path.unlink(missing_ok=True)
    (db_dir / "watcher.lock").unlink(missing_ok=True)

    # Spawn new daemon
    print("Starting new daemon...")
    _ensure_daemon_running(root)

    # Wait and verify
    _time.sleep(0.5)
    if ping_daemon(root):
        print("Daemon restarted successfully.")
    else:
        print("Daemon may still be starting — check 'fw-context watch status' in a moment.")

    return 0
