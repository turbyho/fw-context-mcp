"""Nginx configuration helper for the cache server.

Detects existing nginx state, generates reverse-proxy configuration,
and manages Let's Encrypt certificates via certbot.

Why nginx (not Caddy / Traefik)?
--------------------------------
Nginx is installed on >90% of production Linux servers.  It's the
default reverse proxy in Debian/Ubuntu, has the most battle-tested
TLS configuration, and integrates with certbot via a supported plugin
(``python3-certbot-nginx``).  Caddy and Traefik are excellent
alternatives, but nginx is the lowest-common-denominator choice —
any Linux admin can maintain it.

Security design
---------------
- Domain validation rejects shell metacharacters (no injection into
  config files or sudo commands).
- ``server_name`` is the ONLY untrusted input in the generated config.
- Rate limiting at 50 req/s with burst=20 is generous for batch
  operations but prevents abuse.
- SSL ciphers restrict to HIGH:!aNULL:!MD5 — no weak ciphers.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

NGINX_SITES_AVAILABLE = Path("/etc/nginx/sites-available")
NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")
NGINX_CONFIG_NAME = "fw-cache"

# Domain name validation — rejects shell metacharacters and path components.
# ``| ; & $ `` and ``/`` are all rejected — these cannot appear in valid DNS
# names but could appear in injection attempts.
_DOMAIN_RE = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)*$')


def _validate_domain(domain: str) -> None:
    """Raise ValueError if *domain* is not a valid DNS name.

    Rejects strings containing shell metacharacters, newlines, semicolons,
    path separators, or other characters that could be used for injection
    into nginx configuration or filesystem paths.

    Why validate here (not at the CLI layer)?
    -----------------------------------------
    This function is called from multiple entry points — the setup wizard,
    the CLI, and potentially programmatic callers.  Validating at the
    lowest level ensures no caller can accidentally pass untrusted input
    to nginx config generation or to sudo commands that use the domain
    as a filesystem path component.
    """
    if not domain or not _DOMAIN_RE.match(domain):
        raise ValueError(
            f"Invalid domain: {domain!r}. "
            "Domain must be a valid DNS name (e.g. fw-cache.example.com)."
        )


def detect_nginx() -> dict[str, Any]:
    """Detect the current nginx state on the system.

    Returns a dict with keys:
        installed (bool), running (bool), has_https (bool), domains (list[str])

    Why detect instead of assuming?
    -------------------------------
    The setup wizard uses this to decide what steps are needed:
    - nginx not installed → prompt to install
    - nginx installed but not running → prompt to start
    - nginx running with HTTPS → skip nginx configuration entirely
    - domains list → show operator what's already configured
    """
    result: dict[str, Any] = {"installed": False, "running": False, "has_https": False, "domains": []}

    # Check if nginx binary exists
    nginx_bin = shutil.which("nginx")
    if not nginx_bin:
        return result
    result["installed"] = True

    # Check if nginx is running — systemctl is-interactive for the active state
    try:
        subprocess.run(["systemctl", "is-active", "--quiet", "nginx"], check=True, timeout=5)
        result["running"] = True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        log.debug("Failed to check nginx running state", exc_info=True)

    # Detect existing HTTPS configs by scanning sites-enabled for
    # ``listen 443 ssl`` directives.  This is a heuristic — a full
    # nginx config parser would be overkill for a detection step.
    if NGINX_SITES_ENABLED.exists():
        for conf in NGINX_SITES_ENABLED.iterdir():
            if conf.is_symlink() or conf.is_file():
                try:
                    content = conf.read_text()
                    if "listen 443 ssl" in content or "listen [::]:443 ssl" in content:
                        result["has_https"] = True
                        # Extract server_name for display
                        for line in content.splitlines():
                            line = line.strip()
                            if not line.lstrip().startswith("#") and line.startswith("server_name ") and ";" in line:
                                names = line.replace("server_name ", "").replace(";", "").split()
                                result["domains"].extend(names)
                except OSError:
                    log.debug("Failed to read nginx config %s", conf, exc_info=True)

    return result


def detect_certbot() -> dict[str, Any]:
    """Check certbot availability.

    Returns a dict with keys:
        installed (bool), has_certificates (bool)

    Why handle PermissionError as "has certificates"?
    -------------------------------------------------
    ``/etc/letsencrypt/live/`` is mode 0700 root — unprivileged users
    cannot list it.  If we get PermissionError, certificates almost
    certainly exist (the directory exists, just not readable).  This
    is more accurate than reporting "no certificates" when they do
    exist but are not readable by the current user.
    """
    result: dict[str, Any] = {"installed": False, "has_certificates": False}

    if shutil.which("certbot"):
        result["installed"] = True

    cert_dir = Path("/etc/letsencrypt/live")
    try:
        if cert_dir.exists() and any(cert_dir.iterdir()):
            result["has_certificates"] = True
    except PermissionError:
        # Can't read without sudo — assume certs may exist
        result["has_certificates"] = True

    return result


def generate_nginx_config(domain: str, proxy_port: int = 8000) -> str:
    """Generate an nginx site configuration for reverse-proxying the cache server.

    The configuration enables HTTPS on port 443 with HTTP/1.1 keep-alive,
    sets appropriate proxy headers, and includes rate limiting at 50 req/s
    with a burst of 20.

    ``limit_req_zone`` is written separately to
    ``/etc/nginx/conf.d/fw-cache-rate-limit.conf`` by
    :func:`write_nginx_config` — it must reside in the ``http`` block,
    which is not available inside ``sites-available/`` configs on
    non-Debian systems.

    Why HTTP → HTTPS redirect (301)?
    --------------------------------
    The cache server carries bearer tokens in the ``Authorization``
    header.  HTTP (unencrypted) would expose these tokens to any
    network observer between the client and server.  The 301 redirect
    ensures clients that accidentally connect via HTTP are immediately
    redirected to HTTPS — no plain-text token transmission.

    Why upstream keepalive 32?
    --------------------------
    Each connection to the upstream (uvicorn) involves TCP + TLS
    handshake overhead.  HTTP/1.1 keep-alive pools 32 idle connections
    to the upstream, eliminating handshake overhead for subsequent
    requests from the same nginx worker.  32 is sufficient for the
    expected concurrency (single-digit concurrent developers).

    Why X-Forwarded-For and X-Forwarded-Proto?
    ------------------------------------------
    The cache server's rate limiter needs the real client IP — not
    nginx's 127.0.0.1.  Setting ``X-Forwarded-For`` with
    ``$proxy_add_x_forwarded_for`` appends the real client IP to the
    chain.  ``X-Forwarded-Proto`` tells the backend whether the
    original request was HTTPS — needed if the backend ever generates
    absolute URLs.
    """
    _validate_domain(domain)
    return f"""# fw-context Cache Server — nginx reverse proxy
# Generated by fw-cache-server setup

upstream fw_cache_backend {{
    server 127.0.0.1:{proxy_port};
    keepalive 32;
}}

server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl http2;
    server_name {domain};

    ssl_certificate     /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    limit_req zone=fw_context_cache burst=20 nodelay;

    location / {{
        proxy_pass http://fw_cache_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""


def write_nginx_config(domain: str, proxy_port: int = 8000) -> Path:
    """Write the nginx config file via sudo and return its path.

    Also writes ``limit_req_zone`` to
    ``/etc/nginx/conf.d/fw-cache-rate-limit.conf`` so the directive
    always resides in the ``http`` block regardless of distribution.

    Why write rate-limit zone to conf.d/?
    -------------------------------------
    ``limit_req_zone`` must be in the ``http`` context (not ``server``).
    On Debian/Ubuntu, ``sites-available/`` configs are included inside
    a ``server`` block — the ``limit_req_zone`` directive there would
    be ignored or cause an error.  Writing to ``conf.d/`` places it
    in the global ``http`` block where nginx's main config includes
    ``conf.d/*.conf``.

    Why ``$binary_remote_addr`` (not ``$remote_addr``)?
    --------------------------------------------------
    ``$binary_remote_addr`` is a 4-byte (IPv4) or 16-byte (IPv6)
    binary representation — much smaller than the string form of
    ``$remote_addr`` (up to 45 chars including brackets).  A 10 MB
    shared memory zone with binary addresses can track millions of
    IPs; with string addresses it would overflow at ~100k IPs.
    """
    import subprocess
    config_text = generate_nginx_config(domain, proxy_port)
    config_path = NGINX_SITES_AVAILABLE / NGINX_CONFIG_NAME
    try:
        subprocess.run(
            ["sudo", "tee", str(config_path)],
            input=config_text, text=True, capture_output=True, timeout=10, check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Failed to write {config_path} — write it manually: {e}", file=sys.stderr)
        print(config_text)

    # Write rate-limit zone to conf.d/ — must be in http block
    rate_limit_conf = "limit_req_zone $binary_remote_addr zone=fw_context_cache:10m rate=50r/s;\n"
    rate_limit_path = Path("/etc/nginx/conf.d/fw-cache-rate-limit.conf")
    try:
        subprocess.run(
            ["sudo", "tee", str(rate_limit_path)],
            input=rate_limit_conf, text=True, capture_output=True, timeout=10, check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Failed to write {rate_limit_path} — write it manually: {e}", file=sys.stderr)

    return config_path


def enable_nginx_site() -> None:
    """Create a symlink from sites-available to sites-enabled via sudo.

    Why symlink (not copy)?
    -----------------------
    The Debian nginx convention (followed by Ubuntu and derivatives)
    uses ``sites-available/`` for config files and ``sites-enabled/``
    for symlinks.  Enabling a site is a symlink — disabling is
    removing the symlink.  This preserves the original config file
    and makes enable/disable atomic and reversible.
    """
    import subprocess
    src = str(NGINX_SITES_AVAILABLE / NGINX_CONFIG_NAME)
    dst = str(NGINX_SITES_ENABLED / NGINX_CONFIG_NAME)
    try:
        subprocess.run(["sudo", "ln", "-sf", src, dst], check=True, timeout=10)
    except subprocess.CalledProcessError as e:
        print(f"Failed to enable site — run: sudo ln -s {src} {dst} ({e})", file=sys.stderr)


def test_nginx_config() -> bool:
    """Run ``nginx -t`` to validate configuration.  Returns True on success.

    Why test before reload?
    -----------------------
    ``nginx -s reload`` with invalid config leaves the old config
    running — it does NOT validate before applying.  Running ``nginx -t``
    first catches syntax errors in the generated config before they
    could cause a failed reload (which would leave the server running
    but with the old, possibly stale config).
    """
    try:
        subprocess.run(["sudo", "nginx", "-t"], check=True, capture_output=True, text=True, timeout=10)
        return True
    except subprocess.CalledProcessError as e:
        print("nginx config test failed:", e.stderr, file=sys.stderr)
        return False
    except FileNotFoundError:
        print("nginx not found — cannot test config", file=sys.stderr)
        return False


def reload_nginx() -> bool:
    """Reload nginx.  Returns True on success.

    Why try systemctl first, then nginx -s?
    ---------------------------------------
    ``systemctl reload nginx`` is the standard systemd way — it sends
    SIGHUP to the master process.  But on non-systemd systems or
    container environments without systemd, ``nginx -s reload`` is the
    direct equivalent.  Trying both covers all deployment scenarios.
    """
    try:
        subprocess.run(["systemctl", "reload", "nginx"], check=True, timeout=10)
        return True
    except subprocess.CalledProcessError:
        try:
            subprocess.run(["nginx", "-s", "reload"], check=True, timeout=10)
            return True
        except subprocess.CalledProcessError:
            return False


def cert_exists(domain: str) -> bool:
    """Check if a Let's Encrypt certificate exists for *domain* (via sudo).

    Why sudo?
    ---------
    ``/etc/letsencrypt/live/`` is mode 0700 root — only root can read
    the certificate files.  The setup wizard typically runs with sudo
    access, so ``sudo test -f`` works in the expected deployment context.
    """
    _validate_domain(domain)
    import subprocess
    cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
    try:
        result = subprocess.run(
            ["sudo", "test", "-f", cert_path],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def obtain_certificate(domain: str, email: str = "", expand: bool = False) -> bool:
    """Obtain or expand a Let's Encrypt certificate via certbot.

    *email* — required by Let's Encrypt in non-interactive mode for
    expiration notifications and account recovery.
    *expand* — if True, adds *domain* to the existing certificate
    (``certbot --nginx -d <domain> --expand``).  Useful when adding
    the cache server domain to an existing certificate that already
    covers other subdomains.

    Why ``--non-interactive``?
    --------------------------
    The setup wizard runs as a semi-automated process — the operator
    should not need to interact with certbot's prompts after the
    wizard confirms the domain.  Non-interactive mode handles the
    ACME challenge automatically (nginx plugin handles HTTP-01
    challenges by modifying the nginx config temporarily).

    Why 300-second timeout?
    -----------------------
    DNS-01 challenges require DNS propagation which can take minutes.
    HTTP-01 challenges are faster (<30s), but certbot's default
    timeout is generous to handle slow ACME servers or rate limiting.

    Why ``--register-unsafely-without-email``?
    ------------------------------------------
    This is only used when no email is provided.  Let's Encrypt
    strongly recommends an email for expiration notices — if the
    operator skips it, we still proceed but with a warning in the
    certbot output.
    """
    _validate_domain(domain)
    cmd = ["sudo", "certbot", "--nginx", "-d", domain, "--non-interactive", "--agree-tos"]
    if email:
        cmd.extend(["-m", email])
    else:
        cmd.append("--register-unsafely-without-email")
    if expand:
        cmd.append("--expand")
    try:
        subprocess.run(cmd, check=True, timeout=300)  # 5 min for DNS-01 challenges
        return True
    except subprocess.CalledProcessError as e:
        print(f"certbot failed: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("certbot not found", file=sys.stderr)
        return False
