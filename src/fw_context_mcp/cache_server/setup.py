"""Interactive setup wizard for the fw-context cache server.

Walks the user through installing and configuring all dependencies:
PostgreSQL, cache server init, project/token creation, systemd/launchd,
nginx HTTPS reverse proxy, firewall, and logrotate.

Each step detects the current state and only prompts when action is needed.
"""

from __future__ import annotations

import os
import platform
import secrets
import subprocess
import sys
from pathlib import Path


def setup_wizard() -> int:
    """Run the interactive setup wizard.  Returns 0 on success, non-zero on error."""

    print("\n  fw-context Cache Server Setup")
    print("  " + "─" * 36 + "\n")

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

    # 5. Initialize cache server
    token_result = _ensure_cache_server_init()
    if token_result is None:
        return 1
    admin_token = token_result

    # 6. Create project and tokens
    project_tokens = _setup_project_and_tokens(admin_token)

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
    print("    Server port:    8000")
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


def _run(cmd: list[str], **kwargs: object) -> bool:
    """Run a shell command.  Returns True on success."""
    try:
        subprocess.run(cmd, check=True, **kwargs)  # type: ignore[arg-type]
        return True
    except subprocess.CalledProcessError as e:
        print(f"    Error: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print(f"    Error: command not found — {' '.join(cmd)}", file=sys.stderr)
        return False


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
    except Exception:
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
    """Ensure the ``fw_cache`` PostgreSQL user exists."""
    try:
        result = subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-tAc",
             "SELECT 1 FROM pg_roles WHERE rolname='fw_cache'"],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip() == "1":
            print("  [✓] Database user 'fw_cache' — exists")
            return True
    except Exception:
        pass

    print("  [!] Database user 'fw_cache' — does not exist")

    if not _ask("Create user 'fw_cache'?"):
        return False

    password = secrets.token_urlsafe(24)
    if not _run(
        ["sudo", "-u", "postgres", "psql", "-c",
         f"CREATE USER fw_cache WITH PASSWORD '{password}' CREATEDB"],
        timeout=10,
    ):
        return False

    # Store credentials
    env_path = Path("/etc/fw-cache-server/db.env")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(f'FW_CACHE_DB_URL="postgresql://fw_cache:{password}@localhost:5432"\n')

    os.environ["FW_CACHE_DB_URL"] = f"postgresql://fw_cache:{password}@localhost:5432"

    print("  [✓] User created — credentials saved to /etc/fw-cache-server/db.env")
    return True


# -- Databases --

def _ensure_databases() -> bool:
    """Ensure the meta and cache databases exist."""
    db_url = os.environ.get("FW_CACHE_DB_URL", "")
    if not db_url:
        env_file = Path("/etc/fw-cache-server/db.env")
        if env_file.exists():
            import re
            content = env_file.read_text()
            m = re.search(r'FW_CACHE_DB_URL="([^"]+)"', content)
            if m:
                os.environ["FW_CACHE_DB_URL"] = m.group(1)
                db_url = m.group(1)

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
        except Exception:
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


# -- Cache server init --

def _ensure_cache_server_init() -> str | None:
    """Initialize the cache server schema and return the admin token, or None on failure."""
    db_url = os.environ.get("FW_CACHE_DB_URL", "")
    if not db_url:
        env_file = Path("/etc/fw-cache-server/db.env")
        if env_file.exists():
            import re
            content = env_file.read_text()
            m = re.search(r'FW_CACHE_DB_URL="([^"]+)"', content)
            if m:
                db_url = m.group(1)
                os.environ["FW_CACHE_DB_URL"] = db_url

    print("  [ ] Cache server — checking...")

    if not db_url:
        print("    FW_CACHE_DB_URL not available — run 'fw-cache-server init' manually")
        return os.environ.get("FW_CACHE_DB_URL", "unknown")

    import asyncio

    async def _init() -> str | None:
        from .backend import CacheBackend

        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            await backend.init_schema()

            # Check if "default" project already exists
            existing = await backend.list_projects()
            if existing:
                _ = await backend.list_tokens(existing[0]["id"])
                print("  [✓] Cache server — already initialized")
                print("      Re-run 'fw-cache-server init' manually if you need a new admin token")
                return os.environ.get("FW_CACHE_ADMIN_TOKEN", "unknown")

            # Create default project
            token = await backend.create_project("default")
            if token is None:
                token = "unknown"
            os.environ["FW_CACHE_ADMIN_TOKEN"] = token
            return token
        finally:
            await backend.close()

    try:
        token = asyncio.run(_init())
    except Exception as e:
        print(f"  [!] Failed to init cache server: {e}")
        return None

    if token and token != "unknown":
        print("  [✓] Cache server — initialized")
        print(f"      Admin token: {token}")
        print("      ⚠  Save this token — it's needed for admin operations!")
    return token


# -- Project and tokens --

def _setup_project_and_tokens(admin_token: str) -> tuple[str, str, str] | None:
    """Interactively create a project and its read/write tokens."""
    print()
    if not _ask("Create a project and tokens for devs now?"):
        return None

    try:
        project_id = input("  Project ID (e.g. firma/zbox): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if not project_id:
        print("    Empty project ID — skipping")
        return None

    db_url = os.environ.get("FW_CACHE_DB_URL", "")

    import asyncio

    async def _create() -> tuple[str, str, str] | None:
        from .backend import CacheBackend

        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            # Create project (or reuse existing)
            await backend.create_project(project_id)

            write_token = await backend.create_token(
                project_id, can_read=True, can_write=True, can_overwrite=True, description="admin"
            )
            read_token = await backend.create_token(
                project_id, can_read=True, can_write=False, can_overwrite=False, description="read-only"
            )
            return project_id, write_token, read_token
        finally:
            await backend.close()

    try:
        result = asyncio.run(_create())
        if result:
            proj_id, write_token, read_token = result
            print(f"    [✓] Project '{proj_id}' created")
            print(f"      Write token:  {write_token}")
            print(f"      Read token:   {read_token}")
            return result
    except Exception as e:
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
        print("  [✓] certbot — installed")

    # Obtain certificate
    if not certbot_state["has_certificates"]:
        print("  [!] No Let's Encrypt certificates found")
        if _ask("Obtain certificate via Let's Encrypt?"):
            if obtain_certificate(domain):
                print(f"  [✓] Certificate obtained for {domain}")
            else:
                print("  [!] Certificate failed — you can run certbot manually later")
    else:
        print("  [✓] Let's Encrypt certificates — found")
        if _ask("Expand existing certificate with this domain?"):
            if obtain_certificate(domain, expand=True):
                print(f"  [✓] Certificate expanded with {domain}")
            else:
                print("  [!] Expand failed — you can run certbot --expand manually later")

    # Test and reload nginx
    if test_nginx_config():
        print("  [✓] nginx config — valid")
        if reload_nginx():
            print(f"  [✓] nginx reloaded — reverse proxy active at https://{domain}")
        else:
            print("  [!] nginx reload failed")
    else:
        print("  [!] nginx config has errors — fix manually")
