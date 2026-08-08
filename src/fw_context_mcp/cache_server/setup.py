"""Interactive setup wizard for the fw-context cache server.

Walks the user through installing and configuring all dependencies:
PostgreSQL, cache server init, project/token creation, systemd/launchd,
nginx HTTPS reverse proxy, firewall, and logrotate.

Each step detects the current state and only prompts when action is needed.

Why a wizard instead of 8 separate commands?
--------------------------------------------
Without the wizard, the operator would need to:
1. Install PostgreSQL manually
2. Create database user with password
3. Create two databases
4. Save credentials to db.env
5. Run ``fw-cache-server init``
6. Run ``fw-cache-admin project create <id>``
7. Run ``fw-cache-server install-systemd``
8. Configure nginx + certbot manually

Each step has its own environment variables, file paths, and error
modes.  The wizard chains them together with state detection at each
step — it only prompts when action is actually needed, making both
fresh installs and re-runs (after partial failures) fast.

Design principles
-----------------
- **Idempotent** — every step detects current state and skips completed work.
- **Minimal interaction** — yes/no prompts, not configuration file editing.
- **Credentials safety** — PostgreSQL passwords use secrets.token_urlsafe,
  are piped via stdin (not command-line args visible in ps), and are
  saved to db.env with chmod 640.
"""

from __future__ import annotations

import os
import platform
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def setup_wizard() -> int:
    """Run the interactive setup wizard.  Returns 0 on success, non-zero on error.

    The wizard proceeds through ~9 phases in order:
    0. Ensure dedicated venv in /opt
    1. Detect OS
    2. PostgreSQL installation
    3. Database user creation
    4. Database creation
    5. Cache server init + admin token
    6. Project + token creation
    7. systemd/launchd service installation
    8. nginx HTTPS reverse proxy

    Phases 5-6 use a SINGLE ``asyncio.run()`` call to avoid the overhead
    of opening/closing database pools for each step.  All user input is
    collected synchronously BEFORE the async block — no async I/O in
    interactive prompts.

    Why single asyncio.run() for phases 5-6?
    -----------------------------------------
    Each ``asyncio.run()`` creates a new event loop, which means new
    asyncpg connection pools.  Two separate ``asyncio.run()`` calls
    would open/close pools twice — wasting connection overhead and
    PostgreSQL resources.  One call keeps the pool alive across both
    init and project creation.
    """

    print("\n  fw-context Cache Server Setup")
    print("  " + "─" * 36 + "\n")

    # 0. Ensure dedicated venv in /opt (accessible by fw-cache user)
    if not _ensure_server_venv():
        return 1

    # 1. Detect OS
    system = platform.system()
    print(f"  [✓] OS — {system} ({platform.release()})")

    is_linux = system == "Linux"
    is_macos = system == "Darwin"

    if not is_linux and not is_macos:
        print("  [!] Unsupported OS — setup supports Linux and macOS only")
        return 1

    # 2. PostgreSQL
    if not _ensure_postgresql():
        return 1

    # 3. Create fw_cache database user
    if not _ensure_db_user():
        return 1

    # 4. Create databases
    if not _ensure_databases():
        return 1

    # 5-6. DB operations — SINGLE asyncio.run(), all user input collected first
    # Collecting user input BEFORE the async block avoids mixing sync
    # input() calls with async database operations — input() blocks
    # the event loop, which would cause connection timeouts.
    db_url = _find_db_url()
    if not db_url:
        print("    FW_CACHE_DB_URL not available — run 'fw-cache-server init' manually")
        admin_token = os.environ.get("FW_CACHE_ADMIN_TOKEN", "unknown")
        project_tokens = None
    else:
        import asyncio

        from .backend import CacheBackend

        # Collect ALL user input BEFORE the async block
        detected_id = _detect_project_id_from_cwd()
        create_project = True
        project_id = ""

        if detected_id:
            print()
            print(f"  Detected project ID: {detected_id}")
            if _ask("Use this ID?"):
                project_id = detected_id
            else:
                try:
                    project_id = input("  Project ID (UUID4 hex): ").strip()
                except (EOFError, KeyboardInterrupt):
                    project_id = ""
        else:
            print()
            print("  No .fw-context/config.toml detected in current directory.")
            try:
                project_id = input("  Enter project ID (UUID4 hex, or leave empty to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                project_id = ""

        if not project_id:
            print("    No project ID — skipping project creation")
            create_project = False

        # Single asyncio.run() for ALL DB operations
        async def _all_db_ops():
            backend = CacheBackend(db_url)
            try:
                await backend.connect()

                # Init cache server
                token = await _async_init_cache_server(backend)
                if token is None:
                    return None, None
                if token != "unknown":
                    print("  [✓] Cache server — initialized")
                    print(f"      Admin token: {token}")
                    print("      ⚠  Save this token — it's needed for admin operations!")

                # List existing projects
                existing = await _async_list_projects(backend)
                if existing:
                    existing_ids = ", ".join(p["id"] for p in existing)
                    print(f"  Projects: {existing_ids}")

                # Create project if user requested one
                tokens = None
                if create_project and project_id:
                    result = await _async_create_project(backend, project_id)
                    if result:
                        proj_id, write_token, read_token = result
                        print(f"    [✓] Project '{proj_id}' created")
                        print(f"      Write token:  {write_token}")
                        print(f"      Read token:   {read_token}")
                        tokens = result
                    else:
                        print(f"    [!] Failed to create project '{project_id}'")

                return token, tokens
            finally:
                await backend.close()

        try:
            admin_token, project_tokens = asyncio.run(_all_db_ops())
            if admin_token is None:
                return 1
        except (RuntimeError, OSError) as e:
            print(f"  [!] DB setup failed: {e}")
            return 1

    print()

    # 7. Install systemd/launchd service
    if is_linux:
        _ensure_systemd_service()
    elif is_macos:
        _ensure_launchd_service()

    print()

    # 8. nginx HTTPS reverse proxy
    if is_linux:
        _ensure_nginx()

    # 9. Finish
    print("  [✓] Installation complete!\n")
    print(f"    Server port:    {os.environ.get('FW_CACHE_PORT', '8000')}")
    print(f"    Admin token:    {admin_token}")
    if project_tokens:
        proj_id, write_token, read_token = project_tokens
        print(f"    Project '{proj_id}':")
        print(f"      Write token:  {write_token}")
        print(f"      Read token:   {read_token}")
    print()
    if project_tokens:
        print("    For devs — add to .fw-context/local.toml:")
        print('      [cache_server]')
        print('      url = "https://<your-domain>"')
        print(f'      token = "{read_token}"')
        print()

    return 0


def _ask(msg: str, default: str = "y") -> bool:
    """Prompt the user yes/no.  Returns True for yes.

    Handles EOF (Ctrl+D) and KeyboardInterrupt (Ctrl+C) gracefully —
    both exit the wizard.  Empty input uses the *default*.
    """
    if default == "y":
        prompt = f"  > {msg} [Y/n] "
    else:
        prompt = f"  > {msg} [y/N] "
    while True:
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if not answer:
            answer = default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def _run(cmd: list[str], **kwargs: Any) -> bool:
    """Run a shell command.  Returns True on success.

    Handles the common pattern: ``sudo`` commands often fail when CWD
    is inaccessible to root (e.g. a user home directory with mode 0700).
    When ``cwd`` is not explicitly passed and the command starts with
    ``sudo``, this function changes CWD to ``/tmp`` to avoid permission
    errors.
    """
    scwd = kwargs.pop("cwd", tempfile.gettempdir()) if cmd[0] == "sudo" and "cwd" not in kwargs else kwargs.pop("cwd", None)
    try:
        subprocess.run(cmd, check=True, cwd=scwd, **kwargs)
        return True
    except subprocess.CalledProcessError as e:
        print(f"    Error: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print(f"    Error: command not found — {' '.join(cmd)}", file=sys.stderr)
        return False


def _ensure_server_venv() -> bool:
    """Ensure a dedicated venv exists (default /opt/fw-cache-server/venv).

    Why a dedicated venv in /opt?
    -----------------------------
    The cache server runs as its own system user (``fw-cache``) via
    systemd.  The venv must be readable by that user.  Installing into
    a system-wide Python or a developer's home directory would either
    require running as a developer user (bad for isolation) or would
    be inaccessible to the fw-cache user.  ``/opt/fw-cache-server/``
    is the standard location for third-party application installations
    per the FHS (Filesystem Hierarchy Standard).

    Why install from a local wheel (not PyPI)?
    ------------------------------------------
    The setup wizard runs from an already-installed fw-context-mcp.
    Installing the same version from a local wheel ensures the cache
    server runs the exact same code as the developer's environment —
    no version mismatch between the server and the clients that push
    analyses to it.
    """
    _DEFAULT = "/opt/fw-cache-server"

    # Check for existing installation
    for candidate in ("/opt/fw-cache-server/venv/bin/fw-cache-server", "/var/lib/fw-cache-server/venv/bin/fw-cache-server"):
        if Path(candidate).exists():
            venv_dir = Path(candidate).parent.parent
            print(f"  [✓] Server venv — {venv_dir}")
            os.environ["FW_CACHE_VENV"] = str(venv_dir)
            _install_cli_symlinks(venv_dir)
            return True

    print("  [!] Server venv — not found")
    try:
        path = input(f"  Install path (default {_DEFAULT}): ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    if not path:
        path = _DEFAULT

    venv_dir = Path(path) / "venv"

    # Create dir via sudo
    _run(["sudo", "mkdir", "-p", str(venv_dir.parent)], timeout=10)
    _run(["sudo", "chown", f"{os.getuid()}:{os.getgid()}", str(venv_dir.parent)], timeout=10)

    import subprocess
    # Create venv
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, timeout=60)

    pip = str(venv_dir / "bin" / "pip")

    # Install fw-context-mcp from local wheel (same version as running)
    from fw_context_mcp import __version__ as _current_version
    wheel_candidates = [
        Path.home() / f"fw_context_mcp-{_current_version}-py3-none-any.whl",
        Path(tempfile.gettempdir()) / f"fw_context_mcp-{_current_version}-py3-none-any.whl",
    ]
    wheel_path = None
    for w in wheel_candidates:
        if w.exists():
            wheel_path = str(w)
            break

    if wheel_path:
        subprocess.run([pip, "install", wheel_path], check=True, timeout=120)
    else:
        print("    No wheel found — install manually: pip install fw-context-mcp", file=sys.stderr)
        return False
    subprocess.run([pip, "install", "fastapi", "uvicorn[standard]", "asyncpg"], check=True, timeout=120)

    # Ensure fw-cache system user exists before chown.
    # The user must exist so the systemd service can run as fw-cache.
    try:
        pwd.getpwnam("fw-cache")
    except KeyError:
        _run(["sudo", "useradd", "-r", "-s", shutil.which("nologin") or "/usr/sbin/nologin", "-d", "/nonexistent", "fw-cache"], timeout=10)
    # Ensure fw-cache user can read the venv.
    # Without chown, the venv is owned by the installing user (root or sudoer)
    # and the fw-cache service user cannot import fw-context-mcp modules.
    _run(["sudo", "chown", "-R", "fw-cache:fw-cache", str(venv_dir)], timeout=10)

    os.environ["FW_CACHE_VENV"] = str(venv_dir)
    _install_cli_symlinks(venv_dir)
    print(f"  [✓] Server venv — {venv_dir}")
    return True


def _install_cli_symlinks(venv_dir: Path) -> None:
    """Create symlinks in /usr/local/bin for fw-cache-server and fw-cache-admin.

    Why symlinks (not shell wrappers)?
    ---------------------------------
    Shell wrappers add an extra process layer and can lose environment
    variable propagation.  Symlinks to the venv executables preserve
    the venv's activate behavior — the shebang line in the installed
    console script already points to the venv's Python interpreter.
    """
    bindir = Path("/usr/local/bin")
    for cmd in ("fw-cache-server", "fw-cache-admin"):
        target = venv_dir / "bin" / cmd
        link = bindir / cmd
        if link.exists() or link.is_symlink():
            continue
        if _run(["sudo", "ln", "-s", str(target), str(link)], timeout=10):
            print(f"  [✓] Symlink — {link} -> {target}")


# -- PostgreSQL --

def _detect_postgresql() -> bool:
    """Check if PostgreSQL is installed and running.

    Tries ``pg_isready`` first (fastest, direct connection check), then
    falls back to ``systemctl is-active``.  ``pg_isready`` is more
    reliable because it actually connects — systemctl only checks the
    process state, which can report "active" for a PostgreSQL that is
    still starting up and not accepting connections.
    """
    pg_isready = subprocess.run(["pg_isready", "-q"], capture_output=True, timeout=5)
    if pg_isready.returncode == 0:
        return True
    # Try systemctl
    try:
        result = subprocess.run(["systemctl", "is-active", "--quiet", "postgresql"], capture_output=True, timeout=5)
        return result.returncode == 0
    except subprocess.CalledProcessError:
        return False


def _ensure_postgresql() -> bool:
    """Ensure PostgreSQL is installed and running, prompting to install if needed.

    Why install from apt/brew (not from source)?
    -------------------------------------------
    Package-manager PostgreSQL is pre-configured with sensible defaults
    (auto-start on boot, correct file permissions, system user creation).
    Building from source would require manual configuration of all these
    — the wizard is about convenience, not maximal control.

    Why PostgreSQL 16 on macOS?
    ---------------------------
    Homebrew's ``postgresql`` formula tracks the latest stable release.
    Pinning to ``@16`` ensures the formula name doesn't change when 17
    becomes the default — no breakage on Homebrew upgrades.
    """
    if _detect_postgresql():
        print("  [✓] PostgreSQL — running")
        return True

    print("  [!] PostgreSQL — not found")

    if not _ask("Install PostgreSQL?"):
        print("    PostgreSQL is required. Aborting.")
        return False

    system = platform.system()
    if system == "Linux":
        _run(["sudo", "apt", "update"], timeout=120)
        if not _run(["sudo", "apt", "install", "-y", "postgresql", "postgresql-client"], timeout=300):
            return False
        _run(["sudo", "systemctl", "enable", "--now", "postgresql"], timeout=30)
    elif system == "Darwin":
        if not _run(["brew", "install", "postgresql@16"], timeout=300):
            return False
        _run(["brew", "services", "start", "postgresql@16"], timeout=30)

    if _detect_postgresql():
        print("  [✓] PostgreSQL — running")
        return True
    print("    Failed to start PostgreSQL", file=sys.stderr)
    return False


# -- Database user --

def _ensure_db_user() -> bool:
    """Ensure the ``fw_cache`` PostgreSQL user exists and credentials are saved.

    Why ``secrets.token_urlsafe(24)`` for passwords?
    ------------------------------------------------
    24 bytes of urlsafe base64 = 192 bits of entropy, encoded as ~32
    printable characters.  This is far beyond what PostgreSQL's password
    authentication can withstand (the wire protocol uses SCRAM-SHA-256
    which salts and hashes, but a strong password prevents offline
    dictionary attacks if pg_hba.conf or the password hash file is
    compromised).

    Why pipe SQL via stdin (not command-line args)?
    -----------------------------------------------
    PostgreSQL's ``-c`` flag puts the SQL in the process command line,
    visible via ``ps aux`` and in shell history.  Piping via stdin
    keeps the password out of process listings — only root can see
    the subprocess stdin buffer.
    """
    user_exists = False
    try:
        result = subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-tAc",
             "SELECT 1 FROM pg_roles WHERE rolname='fw_cache'"],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip() == "1":
            user_exists = True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    if not user_exists:
        print("  [!] Database user 'fw_cache' — does not exist")
        if not _ask("Create user 'fw_cache'?"):
            return False
        password = secrets.token_urlsafe(24)
        # Pipe SQL via stdin to avoid password in command-line arguments
        # where it would be visible in ps output and shell history.
        sql = f"CREATE USER fw_cache WITH PASSWORD E'{password}' CREATEDB"
        result = subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-q"],
            input=sql + ";\n", text=True, capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            print(f"    Error: {result.stderr.strip()}", file=sys.stderr)
            return False
        _save_credentials(password)
        return True

    # User exists — check if we have saved credentials
    if _find_db_url():
        print("  [✓] Database user 'fw_cache' — exists")
        return True

    # User exists but no saved password — regenerate.
    # This case occurs when the database was created manually or the
    # credentials file was deleted/lost.  ALTER USER resets the password.
    print("  [!] Database user 'fw_cache' exists but no saved credentials")
    password = secrets.token_urlsafe(24)
    sql = f"ALTER USER fw_cache WITH PASSWORD E'{password}'"
    result = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-q"],
        input=sql + ";\n", text=True, capture_output=True, timeout=10,
    )
    if result.returncode != 0:
        print(f"    Error: {result.stderr.strip()}", file=sys.stderr)
        return False
    _save_credentials(password)
    return True

    print("    Set FW_CACHE_DB_URL manually to continue")
    return False


def _save_credentials(password: str) -> None:
    """Save the database URL to a credentials file.

    Writes to two locations in order of preference:
    1. ``/var/lib/fw-cache-server/db.env`` (production, systemd
       EnvironmentFile=, chmod 640 root:fw-cache)
    2. ``~/.fw-context/db.env`` (fallback, chmod 600)

    Why chmod 640 (not 600)?
    ------------------------
    640 allows the ``fw-cache`` group to read the file — necessary
    because systemd reads EnvironmentFile as root but the service runs
    as the ``fw-cache`` user.  600 (owner-only) would prevent the
    service user from reading the credentials at runtime.

    Why two locations?
    ------------------
    ``/var/lib/fw-cache-server/`` is the production path (system-wide,
    readable by the fw-cache group).  ``~/.fw-context/`` is the fallback
    for single-user setups where the operator doesn't have sudo access
    or is running in a development environment.
    """
    db_url = f"postgresql://fw_cache:{password}@localhost:5432"
    db_url_line = f'FW_CACHE_DB_URL="{db_url}"\n'

    # Production primary: /var/lib/fw-cache-server/ — app data, readable by fw-cache group
    lib_path = Path("/var/lib/fw-cache-server")
    try:
        _run(["sudo", "mkdir", "-p", str(lib_path)], timeout=10)
        _run(["sudo", "chmod", "0750", str(lib_path)], timeout=10)
        import subprocess
        subprocess.run(
            ["sudo", "tee", str(lib_path / "db.env")],
            input=db_url_line, text=True, capture_output=True, timeout=10, check=True,
        )
        _run(["sudo", "chown", "root:fw-cache", str(lib_path / "db.env")], timeout=10)
        _run(["sudo", "chmod", "640", str(lib_path / "db.env")], timeout=10)
        print(f"  [✓] Credentials saved to {lib_path / 'db.env'}")
        os.environ["FW_CACHE_DB_URL"] = db_url
        return
    except (subprocess.CalledProcessError, OSError):
        pass

    # Fallback: user home directory (single-user non-sudo setups)
    home_parent = Path.home() / ".fw-context"
    home_parent.mkdir(parents=True, exist_ok=True)
    home_path = home_parent / "db.env"
    home_path.write_text(db_url_line)
    home_path.chmod(0o600)
    print(f"  [✓] Credentials saved to {home_path}")
    os.environ["FW_CACHE_DB_URL"] = db_url


def _find_db_url() -> str:
    """Find the PostgreSQL connection URL from env or credentials files.

    Checks in order: environment variable, /var/lib/fw-cache-server/db.env,
    ~/.fw-context/db.env.  Returns the first found URL or empty string.

    Why set os.environ?
    -------------------
    Once a db_url is found from a file, setting ``FW_CACHE_DB_URL`` in
    the environment ensures subsequent functions in the wizard don't
    re-read the file — they get the cached value from os.environ.
    """
    db_url = os.environ.get("FW_CACHE_DB_URL", "")
    if db_url:
        return db_url
    for env_path in (Path("/var/lib/fw-cache-server/db.env"), Path.home() / ".fw-context" / "db.env"):
        if env_path.exists():
            content = env_path.read_text()
            m = re.search(r'FW_CACHE_DB_URL="([^"]+)"', content)
            if m:
                os.environ["FW_CACHE_DB_URL"] = m.group(1)
                return m.group(1)
    return ""


# -- Databases --

def _ensure_databases() -> bool:
    """Ensure the meta and cache databases exist.

    Checks both ``fw_cache_meta`` and ``fw_cache`` via PostgreSQL's
    ``pg_database`` catalog.  Creates any missing database with
    ``OWNER fw_cache`` — this is critical: without the correct owner,
    the fw_cache user cannot create tables in the database during
    ``fw-cache-server init``.

    Why separate databases (meta + cache)?
    --------------------------------------
    The meta database (projects, tokens) has different security
    requirements than the cache database (shared analyses).  Separate
    databases allow different PostgreSQL user permissions, backup
    schedules, and connection pool sizes.  This also isolates the
    impact of a cache database corruption — meta operations
    (token validation) continue working.
    """
    db_url = _find_db_url()

    if not db_url:
        print("  [!] No FW_CACHE_DB_URL set — cannot verify databases")
        print("      Create manually:")
        print("      sudo -u postgres psql -c \"CREATE DATABASE fw_cache_meta OWNER fw_cache\"")
        print("      sudo -u postgres psql -c \"CREATE DATABASE fw_cache OWNER fw_cache\"")
        return True  # continue anyway

    missing = []
    for db_name in ["fw_cache_meta", "fw_cache"]:
        try:
            result = subprocess.run(
                ["sudo", "-u", "postgres", "psql", "-tAc",
                 f"SELECT 1 FROM pg_database WHERE datname='{db_name}'"],
                capture_output=True, text=True, timeout=10,
            )
            if result.stdout.strip() != "1":
                missing.append(db_name)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            missing.append(db_name)

    if not missing:
        print("  [✓] Databases fw_cache_meta + fw_cache — exist")
        return True

    for db_name in missing:
        print(f"  [!] Database '{db_name}' — not found")
        if _ask(f"Create database '{db_name}'?"):
            _run(
                ["sudo", "-u", "postgres", "psql", "-c",
                 f"CREATE DATABASE {db_name} OWNER fw_cache"],
                timeout=10,
            )

    return True


# -- Cache server init + project/token creation (single async context) --

async def _async_init_cache_server(backend) -> str | None:
    """Initialize schema + admin token using an already-connected *backend*.

    When projects already exist (re-run scenario), returns the existing
    admin token from the environment without creating a duplicate.

    Why check for existing projects before creating admin token?
    ------------------------------------------------------------
    ``create_admin_token()`` creates a new token each time it's called.
    Running the wizard twice would create duplicate admin tokens —
    confusing and wasteful.  If projects exist, the server has already
    been initialized — return the existing token (or "unknown" if not
    in environment).
    """
    await backend.init_schema()

    existing = await backend.list_projects()
    if existing:
        _ = await backend.list_tokens(existing[0]["id"])
        return os.environ.get("FW_CACHE_ADMIN_TOKEN", "unknown")

    token = await backend.create_admin_token()
    os.environ["FW_CACHE_ADMIN_TOKEN"] = token
    return token


async def _async_list_projects(backend) -> list[dict]:
    """List existing projects using an already-connected *backend*."""
    return await backend.list_projects()


async def _async_create_project(
    backend, project_id: str
) -> tuple[str, str, str] | None:
    """Create a project + tokens using an already-connected *backend*.

    Returns ``(project_id, write_token, read_token)`` on success, ``None``
    on failure.  Also generates two additional tokens (write+overwrite and
    read-only) even when the project already exists — the existing
    project's admin token may have been lost.

    Why create both write and read tokens?
    --------------------------------------
    - Write+overwrite token — for the project maintainer/CI to push analyses
    - Read-only token — for developers to consume cached analyses
    This gives the operator both tokens immediately; they distribute the
    read-only token to developers and keep the write token for CI.
    """
    result = await backend.create_project(project_id)
    if result is None:
        print(f"    Project '{project_id}' already exists — creating additional tokens")

    write_token = await backend.create_token(
        project_id, can_read=True, can_write=True, can_overwrite=True, description="admin"
    )
    read_token = await backend.create_token(
        project_id, can_read=True, can_write=False, can_overwrite=False, description="read-only"
    )
    return project_id, write_token, read_token


def _ensure_cache_server_init() -> str | None:
    """Standalone wrapper — used by CLI. Delegates to :func:`_async_init_cache_server`."""
    db_url = _find_db_url()
    print("  [ ] Cache server — checking...")
    if not db_url:
        print("    FW_CACHE_DB_URL not available — run 'fw-cache-server init' manually")
        return os.environ.get("FW_CACHE_ADMIN_TOKEN", "unknown")

    import asyncio

    from .backend import CacheBackend

    async def _init_server():
        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            return await _async_init_cache_server(backend)
        finally:
            await backend.close()

    try:
        token = asyncio.run(_init_server())
    except (RuntimeError, OSError) as e:
        print(f"  [!] Failed to init cache server: {e}")
        return None

    if token and token != "unknown":
        print("  [✓] Cache server — initialized")
        print(f"      Admin token: {token}")
        print("      ⚠  Save this token — it's needed for admin operations!")
    return token


# -- Project and tokens (interactive UI, no asyncio.run inside) --

def _detect_project_id_from_cwd() -> str | None:
    """Read project ID from .fw-context/config.toml in the current directory.

    Why read from config.toml?
    --------------------------
    The operator typically runs the setup wizard from their project
    directory (where they ran ``fw-context init``).  Reading the
    project ID from config.toml saves them from having to copy-paste
    a 32-character hex string — the wizard detects it automatically
    and offers to use it.
    """
    config_path = Path.cwd() / ".fw-context" / "config.toml"
    if not config_path.exists():
        return None
    try:
        import tomllib
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        pid = data.get("project", {}).get("id")
        if pid:
            return pid
    except (OSError, ValueError):
        pass
    return None


def _setup_project_and_tokens(
    admin_token: str,
) -> tuple[str, str, str] | None:
    """Standalone project+token creation — used by CLI, not the wizard.

    Opens its own database connection.  The wizard uses the inline
    ``_all_db_ops()`` path instead (single ``asyncio.run()`` call).

    Why a separate entry point for CLI?
    -----------------------------------
    The CLI command ``fw-cache-server setup`` may need to create
    projects interactively after the server is already running.
    This function handles the interactive project creation flow
    independently of the full wizard — useful for adding projects
    to an existing cache server.
    """
    import asyncio

    from .backend import CacheBackend
    db_url = _find_db_url()


    async def _create_project():
        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            projects = await _async_list_projects(backend)

            # Show existing projects
            if projects:
                print()
                existing_ids = ", ".join(p["id"] for p in projects)
                print(f"  Projects: {existing_ids}")
                if not _ask("Create another project?"):
                    return None

            # Get project ID
            detected_id = _detect_project_id_from_cwd()
            print()
            if detected_id:
                print(f"  Detected project ID: {detected_id}")
                if _ask("Use this ID?"):
                    pid = detected_id
                else:
                    try:
                        pid = input("  Project ID (UUID4 hex): ").strip()
                    except (EOFError, KeyboardInterrupt):
                        return None
            else:
                print("  No .fw-context/config.toml detected in current directory.")
                try:
                    pid = input("  Project ID (UUID4 hex): ").strip()
                except (EOFError, KeyboardInterrupt):
                    return None

            if not pid:
                print("    Empty project ID — skipping")
                return None

            # Create project + tokens
            result = await _async_create_project(backend, pid)
            if result:
                proj_id, write_token, read_token = result
                print(f"    [✓] Project '{proj_id}' created")
                print(f"      Write token:  {write_token}")
                print(f"      Read token:   {read_token}")
            return result
        finally:
            await backend.close()

    try:
        return asyncio.run(_create_project())
    except (RuntimeError, OSError) as e:
        print(f"    [!] Failed to create project: {e}")
    return None


# -- systemd --

def _ensure_systemd_service() -> None:
    """Prompt to install the systemd service.

    Why check for existing service first?
    ------------------------------------
    The setup wizard should be idempotent — running it twice should
    not overwrite a manually-tuned systemd unit or create a duplicate.
    """
    if Path("/etc/systemd/system/fw-cache-server.service").exists():
        print("  [✓] systemd service — already installed")
        return

    print("  [ ] systemd service — not installed")
    if not _ask("Install systemd service?"):
        return

    # Ensure fw-cache system user exists.
    # The systemd unit runs as User=fw-cache — the user must exist
    # before the service can start.  ``-r`` creates a system user
    # (no home directory, no login shell), ``-s nologin`` prevents
    # interactive login, ``-d /nonexistent`` provides a non-existent
    # home directory (the service never needs disk access).
    import pwd
    try:
        pwd.getpwnam("fw-cache")
    except KeyError:
        _run(["sudo", "useradd", "-r", "-s", shutil.which("nologin") or "/usr/sbin/nologin", "-d", "/nonexistent", "fw-cache"], timeout=10)

    from .install import generate_systemd_unit, install_systemd_unit

    unit_text = generate_systemd_unit()
    install_systemd_unit(unit_text)

    if _ask("Enable and start the service?"):
        _run(["sudo", "systemctl", "daemon-reload"], timeout=10)
        _run(["sudo", "systemctl", "enable", "--now", "fw-cache-server"], timeout=10)
        print("  [✓] fw-cache-server — running")


# -- launchd --

def _ensure_launchd_service() -> None:
    """Prompt to install the launchd service (macOS)."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.fwcontext.cache-server.plist"
    if plist_path.exists():
        print("  [✓] launchd service — already installed")
        return

    print("  [ ] launchd service — not installed")
    if not _ask("Install launchd service?"):
        return

    from .install import generate_launchd_plist, install_launchd_plist

    plist_text = generate_launchd_plist()
    install_launchd_plist(plist_text)

    if _ask("Load and start the service?"):
        _run(["launchctl", "load", str(plist_path)], timeout=10)
        print("  [✓] fw-cache-server — running")


# -- nginx --

def _ensure_nginx() -> None:
    """Prompt to install and configure nginx with HTTPS.

    This is the most complex step in the wizard.  It handles:
    1. nginx installation (if missing)
    2. DNS validation
    3. Config generation with HTTP-only temporary config
    4. certbot certificate acquisition
    5. Full HTTPS config generation
    6. Config test + reload

    Why a temporary HTTP-only config before certbot?
    -----------------------------------------------
    certbot's nginx plugin needs to serve the ACME HTTP-01 challenge
    on port 80.  The full HTTPS config only listens on 443 — if we
    generate the full config first, nginx won't serve the challenge
    and certbot fails.  The temporary config serves port 80 only
    (static root), certbot passes the challenge, then we overwrite
    with the full HTTPS config.
    """
    from .nginx_config import (
        detect_certbot,
        detect_nginx,
        enable_nginx_site,
        obtain_certificate,
        reload_nginx,
        test_nginx_config,
        write_nginx_config,
    )

    nginx_state = detect_nginx()

    if nginx_state["installed"] and nginx_state["running"] and nginx_state["has_https"]:
        domains = nginx_state["domains"]
        print(f"  [✓] nginx — running, HTTPS active ({', '.join(domains[:2])}{'...' if len(domains) > 2 else ''})")
        return  # Nothing to do — already fully configured

    elif nginx_state["installed"] and nginx_state["running"]:
        print("  [✓] nginx — running (no HTTPS detected)")

    elif nginx_state["installed"]:
        print("  [!] nginx — installed but not running")

    else:
        print("  [!] nginx — not installed")
        if not _ask("Install nginx?"):
            return
        if not _run(["sudo", "apt", "install", "-y", "nginx"], timeout=120):
            return
        _run(["sudo", "systemctl", "enable", "--now", "nginx"], timeout=10)
        print("  [✓] nginx — installed")
        nginx_state["installed"] = True
        nginx_state["running"] = True

    # Get domain
    try:
        domain = input("  Domain (e.g. fw-cache.example.com): ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not domain:
        print("    No domain — skipping nginx/HTTPS configuration")
        return

    # Check DNS — certbot needs the domain to resolve to this server
    # for the HTTP-01 challenge.  Warn but don't abort — DNS might
    # be propagating or configured after the wizard runs.
    import socket
    try:
        socket.getaddrinfo(domain, None)
        print(f"  [✓] DNS — {domain} resolves")
    except socket.gaierror:
        print(f"  [!] DNS — {domain} does not resolve. Verify DNS before proceeding.")
        if not _ask("Continue anyway?"):
            return

    # Write nginx config
    config_path = write_nginx_config(domain)
    enable_nginx_site()
    print(f"  [✓] nginx config — {config_path}")

    # Certbot
    certbot_state = detect_certbot()
    if not certbot_state["installed"]:
        print("  [!] certbot — not installed")
        if _ask("Install certbot?"):
            _run(["sudo", "apt", "install", "-y", "certbot", "python3-certbot-nginx"], timeout=120)
        else:
            print("    Skipping HTTPS — certbot required for Let's Encrypt")
            return
    else:
        print("  [✓] certbot — installed")

    # Obtain certificate if needed
    from .nginx_config import cert_exists
    if not cert_exists(domain):
        print(f"  [!] No certificate for {domain} — obtaining via certbot...")
        # First, create temporary HTTP-only config so certbot can verify.
        # certbot --nginx modifies nginx configs but ONLY those that have
        # a server_name matching the requested domain.  The temporary
        # config ensures port 80 is served.
        http_config = f"""server {{
    listen 80;
    server_name {domain};
    root /var/www/html;
}}
"""
        import subprocess

        from .nginx_config import NGINX_CONFIG_NAME, NGINX_SITES_AVAILABLE
        tmp_path = str(NGINX_SITES_AVAILABLE / NGINX_CONFIG_NAME)
        subprocess.run(["sudo", "tee", tmp_path], input=http_config, text=True,
                       capture_output=True, timeout=10)
        enable_nginx_site()
        if not test_nginx_config():
            print("  [!] nginx config test failed — skipping reload", file=sys.stderr)
            return
        subprocess.run(["sudo", "nginx", "-s", "reload"], capture_output=True, timeout=10)

        # Run certbot (tries standalone first, then nginx)
        cert_ok = obtain_certificate(domain)
        if not cert_ok:
            print(f"  [!] Certificate failed — you can run: sudo certbot --nginx -d {domain}")
            print("      Continuing with HTTP-only config")
            return

        print(f"  [✓] Certificate obtained for {domain}")

    # Write full HTTPS nginx config (cert now exists).
    # This overwrites the temporary HTTP-only config with the full
    # HTTPS config that includes SSL certificates and the reverse
    # proxy to the cache server backend.
    config_path = write_nginx_config(domain)
    enable_nginx_site()
    print(f"  [✓] nginx config — {config_path}")

    # Test and reload nginx
    if test_nginx_config():
        print("  [✓] nginx config — valid")
        if reload_nginx():
            print(f"  [✓] nginx reloaded — reverse proxy active at https://{domain}")
        else:
            print("  [!] nginx reload failed")
    else:
        print("  [!] nginx config has errors — fix manually")
