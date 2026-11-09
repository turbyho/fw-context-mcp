"""``fw-context watch`` — background watcher daemon management.

Manages the file-watching daemon that keeps the symbol index current by
reindexing modified source files automatically.  The daemon monitors the
project directory for changes and starts ``fw-context index --background``,
which re-parses the changed units of each build.

WHY a daemon: during development, the index quickly falls out of sync with
source files if the user must remember to reindex manually.  The daemon
uses inotify/FSEvents to detect changes and reindexes within seconds.

WHY debouncing: editors save frequently (auto-save, rapid edits).
Reindexing on every event would waste CPU.  A cooldown period collects
multiple rapid saves into a single reindex.
"""

from __future__ import annotations

import argparse
import sqlite3


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
    from ..mcp.background import read_reindex_progress, read_reindex_state
    from ..mcp.daemon import DAEMON_PORT_NAME, _read_port_file, ping_daemon
    from ..utils import resolve_project_root

    root = resolve_project_root(args.project)
    cfg = load_config(project_root=root)
    project_id = derive_project_id(root)
    db_dir = cfg.index.db_dir / project_id

    pid_file = db_dir / "watcher.pid"
    port_file = db_dir / DAEMON_PORT_NAME

    print(f"Project:    {root.name}")
    print(f"Path:       {root}")
    print(f"DB dir:     {db_dir}")

    # Daemon status
    alive = ping_daemon(root)
    if alive:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                import time as _time

                uptime_s = _time.time() - pid_file.stat().st_mtime
                print(f"Daemon:     running (pid {pid}, uptime {int(uptime_s)}s)")
            except (OSError, ValueError):
                print("Daemon:     running")
        else:
            print("Daemon:     running")
    else:
        if port_file.exists():
            print("Daemon:     not responding (port file exists but no answer)")
        elif pid_file.exists():
            print("Daemon:     dead (pid file exists but port file missing)")
        else:
            print("Daemon:     not running")

    # Port file of the ping (TCP on 127.0.0.1): the port, for netstat or a
    # firewall tool when the daemon does not answer.
    known = _read_port_file(port_file)
    port_text = f" (port {known[0]})" if known else ""
    print(f"Port file:  {port_file}{port_text}{' (active)' if alive else ''}")

    # Modified files count
    db_path = db_dir / "index.db"
    if db_path.exists():
        try:
            _print_modified_counts(db_path, project_id, root)
        except (sqlite3.Error, OSError):
            pass

    # Index subprocess.  "scheduled" is work of the daemon that waits for
    # the debounce, for a manual operation, or for the start of its run.
    state = read_reindex_state(root)
    if state == "reindexing":
        print("Index:      running")
    elif state == "pending":
        print("Index:      scheduled")
    else:
        print("Index:      idle")

    # Last line the index run itself wrote.  Not the last line of the
    # file: reindex.log also holds the output of the build tools, and the
    # helper says why taking that would be wrong.
    last = read_reindex_progress(db_dir)
    if last is not None:
        print(f"Last index: {last}")

    return 0


def cmd_watch_restart(args: argparse.Namespace) -> int:
    """Restart the watcher daemon for a project."""
    import time as _time

    from ..config import derive_project_id
    from ..config import load as load_config
    from ..mcp.background import _ensure_daemon_running
    from ..mcp.daemon import DAEMON_PORT_NAME, ping_daemon
    from ..processes import kill_hard, pid_exists, request_stop
    from ..utils import resolve_project_root

    root = resolve_project_root(args.project)
    cfg = load_config(project_root=root)
    project_id = derive_project_id(root)
    db_dir = cfg.index.db_dir / project_id

    pid_file = db_dir / "watcher.pid"
    port_file = db_dir / DAEMON_PORT_NAME

    # Kill existing daemon
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            print(f"Stopping daemon (pid {pid})...")
            # The request runs the SIGTERM handler of the daemon, also on
            # Windows (processes.request_stop), where os.kill(pid, SIGTERM)
            # is TerminateProcess and os.kill(pid, 0) sends Ctrl+C.
            request_stop(pid)
            # Wait for shutdown
            for _ in range(30):  # 3 seconds max
                if not pid_exists(pid):
                    break
                _time.sleep(0.1)
            else:
                print("Daemon did not stop, ending it...")
                kill_hard(pid)
                _time.sleep(0.5)
            print("Old daemon stopped.")
        except (ValueError, OSError) as e:
            print(f"Could not read/stop old daemon: {e}")

    # Clean up leftover files.  Not watcher.lock: the daemons exclude each
    # other with a flock on that file, and a removed file gives the new
    # daemon a new inode, thus a lock of its own beside an old daemon that
    # survived its SIGKILL: two daemons.  A lock of a dead process is free.
    pid_file.unlink(missing_ok=True)
    port_file.unlink(missing_ok=True)

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


def _print_modified_counts(db_path, project_id: str, root) -> None:
    """Print the count of the modified files of each build, read-only.

    The status migrates no schema beside an index run.  An index that it
    cannot read (an older schema after an upgrade) gives one line, and the
    status goes on: the daemon migrates the index, and the lines after the
    count show that run.  Before, the status stopped here with exit 1.
    """
    from ..indexer.db import open_db_readonly
    from ..mcp.shared.stale import _count_modified_files, build_label, latest_builds
    from ._builds import reading_problem

    conn = open_db_readonly(db_path)
    try:
        problem = reading_problem(conn)
        if problem is not None:
            print(f"Modified:   not known: {problem[1]}")
            return
        # Each build: the builds of one project are not always indexed
        # together, thus one build can be behind while the newest is
        # current.  The daemon checks each build too.  One hash of each
        # file for all builds: a file that two builds compile has one text
        # on disk.
        hash_cache: dict[str, str] = {}
        for build in sorted(latest_builds(conn, project_id), key=build_label):
            mod_count = _count_modified_files(
                conn, build["config_hash"], root, use_cache=False, hash_cache=hash_cache,
            )
            print(f"Modified:   {mod_count} file(s) in build {build_label(build) or '(no name)'}")
    finally:
        conn.close()
