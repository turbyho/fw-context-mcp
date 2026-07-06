# Cache Server — Shared LLM Analysis Cache

Central HTTP cache for `llm_analysis` results. Multiple developers index the
same project independently; the cache server stores each unique symbol analysis
once and serves it to everyone. Built on FastAPI + PostgreSQL with token-based
auth, rate-limited nginx proxy, and optional Let's Encrypt TLS.

> **Non-server users** — `llm_analysis` lives in the local per-project index
> database. The cache server is strictly optional.

## How it works

```
fw-context index --analyze
        │
        ▼
    local per-project index
        │  (miss)
        ├──► ~/.fw-context/llm_cache.db     (local global cache)
        │         │  (miss)
        │         ├──► https://fw-cache.example.com/cache/batch
        │         │         │
        │         │         ▼
        │         │    PostgreSQL (shared)
        │         │
        │         └── return + store locally
        │
        └── return stored result
```

Two-tier lookup, each tier caches the answer for next time:

| Tier | Storage | Scope | Lookup cost |
|------|---------|-------|-------------|
| 1 | `~/.fw-context/llm_cache.db` | All projects, same machine | SQLite (fast) |
| 2 | Remote cache server (PostgreSQL) | All developers, all machines | HTTPS (network) |

Each content hash is computed from the function body + signature — identical
code produces identical hashes regardless of project. An analysis generated
for *birdie1* is automatically available to *zbox-ecb* and *HA_Boiler*.
Re-indexing any project with `[cache_server]` configured skips Ollama entirely
for symbols already cached.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | none | Public health check |
| `GET` | `/cache/stats` | `can_read` | Cache statistics (entries, models) |
| `POST` | `/cache/batch` | `can_read` | Batch lookup by content hash |
| `PUT` | `/cache/batch` | `can_write` | Batch write cache entries |
| `POST` | `/cache/clear` | `can_write` | Delete entries by content hash |

### GET /cache/stats

Returns cache-wide statistics: total entries, newest/oldest timestamps,
and per-model entry counts. Used by ``fw-context cache stats --remote``.

```json
→ {"total_entries": 13391, "newest_entry": "...", "oldest_entry": "...",
   "models": {"qwen2.5-coder:14b": 13383, ...}}
```

### POST /cache/clear

```json
{"hashes": ["abc123...", "def456..."]}
→ {"deleted": 2, "total": 2}
```

Deletes cache entries matching the given content hashes. Used by
`fw-context cache clear --remote` to purge a project's entries from
the shared server. Hashes are uploaded in chunks of `batch_size`.

## Quickstart

```bash
# Install the package with server dependencies
pip install fw-context-mcp[cache-server]

# Interactive wizard — installs PostgreSQL, creates DB, configures nginx + TLS
fw-cache-server setup

# Or step by step:
fw-cache-server init       # creates schema + admin token
fw-cache-server run        # starts server on :8000
fw-cache-admin project create my-project
fw-cache-admin token create my-project --write --description "dev-1"
```

## Prerequisites

| Component | Ubuntu / Debian | Arch |
|-----------|-----------------|------|
| Python 3.11+ | `apt install python3` | `pacman -S python` |
| PostgreSQL 15+ | `apt install postgresql` | `pacman -S postgresql` |
| nginx (optional) | `apt install nginx` | `pacman -S nginx` |
| certbot (optional) | `apt install certbot python3-certbot-nginx` | `pacman -S certbot certbot-nginx` |

The setup wizard detects and installs missing prerequisites automatically.
For manual installation, install PostgreSQL and ensure it is running:

```bash
# Ubuntu / Debian
sudo apt install postgresql
sudo systemctl enable --now postgresql

# Arch
sudo pacman -S postgresql
sudo -u postgres initdb -D /var/lib/postgres/data
sudo systemctl enable --now postgresql
```

## Installation

### Option 1: Setup wizard (recommended)

```bash
pip install fw-context-mcp[cache-server]
fw-cache-server setup
```

The wizard is **idempotent** — re-run it to add new projects, upgrade nginx
config, or recover from errors. Each step detects existing state and skips
already-configured parts.

What it does:

1. Creates `/opt/fw-cache-server/venv/` with FastAPI + asyncpg
2. Detects OS, installs PostgreSQL if missing
3. Creates `fw_cache` database user with random password
4. Creates `fw_cache_meta` + `fw_cache` databases
5. Initializes schema, prints admin token
6. Creates project(s) and tokens interactively
7. Installs systemd service (`fw-cache-server`)
8. Optionally configures nginx reverse proxy + Let's Encrypt TLS

### Option 2: Manual

```bash
pip install fw-context-mcp[cache-server]

# Create database user and databases
sudo -u postgres createuser fw_cache --pwprompt
sudo -u postgres createdb fw_cache_meta --owner fw_cache
sudo -u postgres createdb fw_cache --owner fw_cache

# Save credentials (used by fw-cache-server and fw-cache-admin)
cat > /var/lib/fw-cache-server/db.env << 'EOF'
FW_CACHE_DB_URL="postgresql://fw_cache:YOUR_PASSWORD@localhost:5432"
EOF
chmod 600 /var/lib/fw-cache-server/db.env

# Initialize schema + admin token
source /var/lib/fw-cache-server/db.env
fw-cache-server init
```

### Option 3: Wheel-based deployment

For deployment without `pip` or venv management:

```bash
# Build wheel from source
python3 -m build --wheel

# Copy wheel to server
scp dist/fw_context_mcp-*.whl server:/tmp/

# On server: install into isolated venv
sudo python3 -m venv /opt/fw-cache-server/venv
sudo /opt/fw-cache-server/venv/bin/pip install /tmp/fw_context_mcp-*.whl fastapi uvicorn[standard] asyncpg
sudo chown -R fw-cache:fw-cache /opt/fw-cache-server

# Then run fw-cache-server setup (it will detect the existing venv)
```

## Running the server

### systemd (Linux — recommended)

```bash
# Installed automatically by fw-cache-server setup. Manual:
fw-cache-server install-systemd --dry-run   # preview
fw-cache-server install-systemd             # write unit + enable + start

# Manage
sudo systemctl status fw-cache-server
sudo systemctl restart fw-cache-server
sudo journalctl -u fw-cache-server -f       # live logs
```

The systemd unit:
- Runs as `fw-cache` system user (created automatically)
- `ProtectSystem=strict`, `NoNewPrivileges=yes`, `PrivateTmp=yes`
- Restarts on failure

### launchd (macOS)

```bash
fw-cache-server install-launchd --dry-run   # preview plist
fw-cache-server install-launchd             # write + load

launchctl list com.fwcontext.cache-server
```

### Direct

```bash
fw-cache-server run --host 127.0.0.1 --port 8000
```

## nginx reverse proxy

The setup wizard configures this automatically. Manual setup:

```bash
# Generate and write config
fw-cache-server install-systemd  # creates /etc/nginx/sites-available/fw-cache

# Enable site
sudo ln -sf /etc/nginx/sites-available/fw-cache /etc/nginx/sites-enabled/

# Test and reload
sudo nginx -t
sudo systemctl reload nginx
```

Features of the generated config:
- HTTP → HTTPS redirect
- HTTP/2 on TLS
- TLS 1.2–1.3, modern cipher suite
- Rate limiting: 50 req/s, burst 20 (`X-RateLimit-*` headers)
- Keep-alive connections to upstream (max 32)
- `X-Forwarded-*` headers

### TLS with Let's Encrypt

```bash
sudo certbot --nginx -d fw-cache.example.com --non-interactive --agree-tos
```

The setup wizard runs this automatically after confirming DNS resolves.
Certificates auto-renew via `certbot.timer` (installed by the certbot package).

## Credentials

| File | Purpose | Permissions |
|------|---------|-------------|
| `/var/lib/fw-cache-server/db.env` | PostgreSQL connection URL | 644 (root:fw-cache) |
| `~/.fw-context/db.env` | Fallback for single-user setups | 600 (user) |

Format:
```bash
FW_CACHE_DB_URL="postgresql://fw_cache:PASSWORD@localhost:5432"
```

The server connects to `FW_CACHE_DB_URL/fw_cache_meta` and `FW_CACHE_DB_URL/fw_cache`
(prefix is auto-appended — the configured URL must be the **base**, not a specific database).

## Managing with fw-cache-admin

`fw-cache-admin` connects directly to PostgreSQL — no HTTP overhead.
Requires `FW_CACHE_DB_URL` and `FW_CACHE_ADMIN_TOKEN` in environment.

```bash
source /var/lib/fw-cache-server/db.env
FW_CACHE_ADMIN_TOKEN="<admin-token>" fw-cache-admin <command>
```

### Projects

```bash
fw-cache-admin project list
fw-cache-admin project create my-project --description "My firmware project"
fw-cache-admin project remove my-project --confirm
```

Removing a project deletes its tokens (cascade). Cache entries are **not**
deleted — cache is global across all projects.

### Tokens

```bash
fw-cache-admin token list my-project
fw-cache-admin token create my-project --write --description "dev-laptop"
fw-cache-admin token create my-project --readonly --description "CI-reader"
fw-cache-admin token revoke <token>

# Permission levels:
#   --readonly   can_read only
#   --write      can_read + can_write
#   --overwrite  can_read + can_write + can_overwrite (admin-level)
```

Token plaintext is printed once at creation. Store it securely — it is
SHA-256 hashed in the database and cannot be recovered.

### Cache maintenance

```bash
fw-cache-admin cache stats                         # overview
fw-cache-admin cache purge --older-than 90d        # remove entries older than 90 days
```

## Client configuration

Each developer configures their `<project>/.fw-context/local.toml`:

```toml
[cache_server]
url = "https://fw-cache.example.com"
token = "<your-read-write-token>"
# batch_size = 100        # hashes per request
# force = false           # set true to overwrite existing entries
```

The client uses `url` for `POST /cache/batch` (read), `PUT /cache/batch`
(write), and `POST /cache/clear` (delete). It chunks requests by `batch_size`.
Retries network errors 3× with exponential backoff; falls back gracefully
to local-only on failure.

### How the client uses the cache

On `fw-context index --analyze`:

1. **Local global cache** (Tier 1, `~/.fw-context/llm_cache.db`)
   — shared across all projects on the same machine. SQLite.
2. **Remote cache server** (Tier 2)
   — only if `[cache_server]` is configured.
   Cache misses from the server are stored back into the local cache.

### Cache management commands

```bash
# Show cache statistics
fw-context cache stats                   # both tiers
fw-context cache stats --remote          # Tier 2 only (queries server in real-time)

# Clear cache
fw-context cache clear                   # local cache only (Tier 1)
fw-context cache clear --remote          # project's entries from server (Tier 2)
fw-context cache clear --all             # both tiers
fw-context cache clear --remote -y       # skip confirmation

# Push local cache to remote server (with overwrite)
fw-context cache push                    # push all, batch size 100
fw-context cache push --batch 500        # larger batches for faster transfer

# Interactive remote cache setup
fw-context cache remote-init             # configure URL and token interactively
fw-context cache remote-init --project /path/to/project
```

### `fw-context cache remote-init`

Interactive wizard that configures the remote cache server connection.
Prompts for the server URL (defaults to existing value) and authentication
token, verifies the connection, and writes the `[cache_server]` section
to `.fw-context/local.toml`.

1. **URL** — server base URL (e.g. `https://fw-cache.example.com`)
2. **Token** — read or read+write token created with `fw-cache-admin token create`
3. **Verify** — calls `/health` and `/cache/stats` to confirm connectivity
4. **Write** — idempotently updates `local.toml` (replaces existing section
   or appends a new one)

Re-run to update the URL or rotate the token — the wizard detects existing
configuration and shows the current value as the default.

The `--remote` flag reads all content hashes from the project's per-project
`llm_analysis_cache` table and sends them to the server's `POST /cache/clear`
endpoint. Only the current project's entries are deleted — entries shared
with other projects remain on the server.

`fw-context cache push` uploads ALL entries from the local global cache
(``~/.fw-context/llm_cache.db``) to the remote server with overwrite enabled
(``X-Cache-Overwrite: true``). This is useful for seeding a newly-deployed
server or migrating cache between machines. Requires ``can_write`` and
``can_overwrite`` on the token.

## Hardening (production)

### Firewall

```bash
# Ubuntu / Debian — ufw
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable

# Arch — iptables or nftables
sudo iptables -A INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 443 -j ACCEPT
```

The server port (8000) should **not** be exposed — only nginx on 80/443.
Use `--host 127.0.0.1` in the systemd unit to bind locally.

### PostgreSQL hardening

```bash
# Ensure PostgreSQL listens on localhost only
sudo -u postgres psql -c "SHOW listen_addresses;"
# Expected: localhost

# Verify pg_hba.conf uses scram-sha-256
sudo grep -v '^#' /var/lib/postgresql/data/pg_hba.conf | grep -v '^$'
```

### Token rotation

```bash
# Revoke old token
fw-cache-admin token revoke <old-token>

# Create new token
fw-cache-admin token create my-project --write --description "rotation-2026-07"

# Update client configs
```

### Logs

```bash
# Application logs (systemd)
sudo journalctl -u fw-cache-server -n 100 --no-pager

# PostgreSQL logs
# Ubuntu / Debian: /var/log/postgresql/postgresql-*.log
# Arch: journalctl -u postgresql

# nginx access/error
tail -f /var/log/nginx/access.log
tail -f /var/log/nginx/error.log
```

## Troubleshooting

### `fw-cache-server: command not found`

The binary is at `/opt/fw-cache-server/venv/bin/fw-cache-server`.
Re-run `fw-cache-server setup` to repair the venv and symlink.

### Permission denied errors during setup

The wizard uses `sudo` for privileged operations. Ensure the current user
has passwordless sudo or enters the password when prompted.

### could not change directory to "/home/user"

Expected — the `fw-cache` system user has no home directory. The service
runs from `/tmp`. This message is harmless.

### Connection refused on port 8000

```bash
sudo systemctl status fw-cache-server
sudo systemctl restart fw-cache-server
```

Check the database URL:
```bash
sudo cat /var/lib/fw-cache-server/db.env
# Verify FW_CACHE_DB_URL points to a running PostgreSQL
sudo -u postgres pg_isready
```

### certbot rate-limited (HTTP 429)

Let's Encrypt enforces 5 certificates per domain per week.
Wait or use the staging environment:
```bash
sudo certbot --nginx -d fw-cache.example.com --staging
```

### certbot 503 from Let's Encrypt

Temporary LE API overload — retry in 30–60 minutes. The rate limit
is separate from this transient error.

### 401 Unauthorized

Token is missing, invalid, or revoked. Check:
- `Authorization: Bearer <token>` header is present
- Token matches the one from `fw-cache-admin token create`
- `fw-cache-admin token list <project>` shows the token as active (no revoked_at)

### 403 Forbidden on PUT

The token has `can_read` but not `can_write`. Create a write token:
```bash
fw-cache-admin token create my-project --write
```

### 403 Forbidden with X-Cache-Overwrite

Requires `can_overwrite`. Use an admin token or create an overwrite token:
```bash
fw-cache-admin token create my-project --overwrite
```

## Environment variables summary

| Variable | Required by | Description |
|----------|-------------|-------------|
| `FW_CACHE_DB_URL` | server, admin | PostgreSQL base URL (without database name) |
| `FW_CACHE_ADMIN_TOKEN` | admin | Admin token for `fw-cache-admin` commands |
| `FW_CACHE_HOST` | server | Bind address (default `0.0.0.0`) |
| `FW_CACHE_PORT` | server | Bind port (default `8000`) |
| `FW_CACHE_VENV` | setup | Path to server venv (set during setup) |

## File paths reference

| Path | Owner | Purpose |
|------|-------|---------|
| `/opt/fw-cache-server/venv/` | fw-cache:fw-cache | Server Python environment |
| `/var/lib/fw-cache-server/db.env` | root:fw-cache | Database credentials (644, readable by all) |
| `~/.fw-context/db.env` | user | Fallback credentials (single-user) |
| `/etc/systemd/system/fw-cache-server.service` | root | systemd unit |
| `~/Library/LaunchAgents/com.fwcontext.cache-server.plist` | user | macOS launchd plist |
| `/etc/nginx/sites-available/fw-cache` | root | nginx site config |
| `/etc/nginx/sites-enabled/fw-cache` | root | nginx site symlink |
| `/etc/letsencrypt/live/<domain>/` | root | TLS certificates |
| `~/.fw-context/llm_cache.db` | user | Local cache, shared across all projects (SQLite) |
| `<project>/.fw-context/config.toml` | user | Shared project config (commit to git) |
| `<project>/.fw-context/local.toml` | user | Local developer overrides (`[cache_server]`, `[llm]`) |

## Upgrading

```bash
# Pull latest release
pip install --upgrade fw-context-mcp[cache-server]

# Update the server venv
fw-cache-server setup   # detects existing install, upgrades package + deps
sudo systemctl restart fw-cache-server
```
