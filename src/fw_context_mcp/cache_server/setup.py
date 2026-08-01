"""Interactive setup wizard for the fw-context cache server.

Walks the user through installing and configuring all dependencies:
PostgreSQL, cache server init, project/token creation, systemd/launchd,
nginx HTTPS reverse proxy, firewall, and logrotate.

Each step detects the current state and only prompts when action is needed.
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
from pathlib import Path
from typing import Any


def setup_wizard() -> int:
    """Run the interactive setup wizard.  Returns 0 on success, non-zero on error."""

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
    """Prompt the user yes/no.  Returns True for yes."""
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
    """Run a shell command.  Returns True on success."""
    # Suppress "could not change directory" noise from sudo commands
    scwd = kwargs.pop("cwd", "/tmp") if cmd[0] == "sudo" and "cwd" not in kwargs else kwargs.pop("cwd", None)
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
    """Ensure a dedicated venv exists (default /opt/fw-cache-server/venv)."""
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
        Path("/tmp") / f"fw_context_mcp-{_current_version}-py3-none-any.whl",
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

    # Ensure fw-cache system user exists before chown
    try:
        pwd.getpwnam("fw-cache")
    except KeyError:
        _run(["sudo", "useradd", "-r", "-s", shutil.which("nologin") or "/usr/sbin/nologin", "-d", "/nonexistent", "fw-cache"], timeout=10)
    # Ensure fw-cache user can read the venv
    _run(["sudo", "chown", "-R", "fw-cache:fw-cache", str(venv_dir)], timeout=10)

    os.environ["FW_CACHE_VENV"] = str(venv_dir)
    _install_cli_symlinks(venv_dir)
    print(f"  [✓] Server venv — {venv_dir}")
    return True


def _install_cli_symlinks(venv_dir: Path) -> None:
    """Create symlinks in /usr/local/bin for fw-cache-server and fw-cache-admin."""
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
    """Check if PostgreSQL is installed and running."""
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
    """Ensure PostgreSQL is installed and running, prompting to install if needed."""
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
    """Ensure the ``fw_cache`` PostgreSQL user exists and credentials are saved."""
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

    # User exists but no saved password — regenerate
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
    """Save the database URL to a credentials file."""
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
    """Find the PostgreSQL connection URL from env or credentials files."""
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
    """Ensure the meta and cache databases exist."""
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
    """Initialize schema + admin token using an already-connected *backend*."""
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
    """Create a project + tokens using an already-connected *backend*."""
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

    async def _standalone():
        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            return await _async_init_cache_server(backend)
        finally:
            await backend.close()

    try:
        token = asyncio.run(_standalone())
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
    """Read project ID from .fw-context/config.toml in the current directory."""
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
    """
    import asyncio

    from .backend import CacheBackend
    db_url = _find_db_url()

    existing: list[dict] = []

    async def _standalone():
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
        return asyncio.run(_standalone())
    except (RuntimeError, OSError) as e:
        print(f"    [!] Failed to create project: {e}")
    return None


# -- systemd --

def _ensure_systemd_service() -> None:
    """Prompt to install the systemd service."""
    if Path("/etc/systemd/system/fw-cache-server.service").exists():
        print("  [✓] systemd service — already installed")
        return

    print("  [ ] systemd service — not installed")
    if not _ask("Install systemd service?"):
        return

    # Ensure fw-cache system user exists
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
    """Prompt to install and configure nginx with HTTPS."""
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

    # Check DNS
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
        # First, create temporary HTTP-only config so certbot can verify
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

    # Write full HTTPS nginx config (cert now exists)
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
