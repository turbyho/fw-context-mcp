# Cache Server — Shared LLM Analysis Cache

This is a central HTTP cache for `llm_analysis` results. Multiple developers
can index the same project independently. The cache server stores each
unique symbol analysis once, and serves this analysis to everyone. The cache
server is built on FastAPI and PostgreSQL, with token-based authentication, a
rate-limited nginx proxy, and optional Let's Encrypt TLS.

> **Non-server users:** `llm_analysis` stays in the local per-project index
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

fw-context uses a two-tier lookup. Each tier caches the answer, for next time:

| Tier | Storage | Scope | Lookup cost |
|------|---------|-------|-------------|
| 1 | `~/.fw-context/llm_cache.db` | All projects, same machine | SQLite (fast) |
| 2 | Remote cache server (PostgreSQL) | All developers, all machines | HTTPS (network) |

fw-context computes each content hash from the function body and the
signature. Identical code produces identical hashes, regardless of the
project. So an analysis that fw-context generates for *birdie1* is also
available automatically to *zbox-ecb* and *HA_Boiler*. When you configure
`[cache_server]` and re-index any project, fw-context skips Ollama entirely
for symbols that are already in the cache.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | none | Public health check |
| `GET` | `/cache/stats` | `can_read` | Cache statistics (entries, models) |
| `POST` | `/cache/batch` | `can_read` | Batch lookup by content hash |
| `PUT` | `/cache/batch` | `can_write` | Batch write cache entries |
| `POST` | `/cache/clear` | `can_write` | Delete entries by content hash |

### GET /cache/stats

This endpoint returns cache-wide statistics: the total entries, the newest
and oldest timestamps, and the entry counts for each model.
`fw-context cache stats --remote` uses this endpoint.

```json
→ {"total_entries": 13391, "newest_entry": "...", "oldest_entry": "...",
   "models": {"qwen2.5-coder:14b": 13383, ...}}
```

### POST /cache/clear

```json
{"hashes": ["abc123...", "def456..."]}
→ {"deleted": 2, "total": 2}
```

This endpoint deletes the cache entries that match the given content hashes.
`fw-context cache clear --remote` uses this endpoint, to purge a project's
entries from the shared server. The client uploads the hashes in chunks of
`batch_size`.

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

The setup wizard detects missing prerequisites, and installs them
automatically. For a manual installation, install PostgreSQL, and make sure
PostgreSQL is running:

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

The wizard is **idempotent**. Re-run the wizard to add new projects, to
upgrade the nginx configuration, or to recover from errors. Each step
detects the existing state, and skips the parts that are already configured.

What the wizard does:

1. Creates `/opt/fw-cache-server/venv/` with FastAPI + asyncpg
2. Detects the OS, and installs PostgreSQL if missing
3. Creates the `fw_cache` database user, with a random password
4. Creates the `fw_cache_meta` and `fw_cache` databases
5. Initializes the schema, and prints the admin token
6. Creates the project or projects, and the tokens, interactively
7. Installs the systemd service (`fw-cache-server`)
8. Optionally configures the nginx reverse proxy and Let's Encrypt TLS

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
- Runs as the `fw-cache` system user, created automatically
- Sets `ProtectSystem=strict`, `NoNewPrivileges=yes`, `PrivateTmp=yes`
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

The setup wizard runs this command automatically, after the wizard confirms
that DNS resolves correctly. Certificates renew automatically, through
`certbot.timer`, which the certbot package installs.

## Credentials

| File | Purpose | Permissions |
|------|---------|-------------|
| `/var/lib/fw-cache-server/db.env` | PostgreSQL connection URL | 644 (root:fw-cache) |
| `~/.fw-context/db.env` | Fallback for single-user setups | 600 (user) |

Format:
```bash
FW_CACHE_DB_URL="postgresql://fw_cache:PASSWORD@localhost:5432"
```

The server connects to `FW_CACHE_DB_URL/fw_cache_meta` and to
`FW_CACHE_DB_URL/fw_cache`. The server appends this prefix automatically. So
the configured URL must be the **base** URL, not a specific database.

## Managing with fw-cache-admin

`fw-cache-admin` connects directly to PostgreSQL, with no HTTP overhead.
`fw-cache-admin` requires `FW_CACHE_DB_URL` and `FW_CACHE_ADMIN_TOKEN` in the
environment.

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

When you remove a project, `fw-cache-admin` also deletes the project's
tokens, in a cascade. `fw-cache-admin` does **not** delete the cache entries.
The cache is global, across all projects.

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

`fw-cache-admin` prints the plaintext token once, at creation time. Store
the token securely. The database stores only a SHA-256 hash of the token,
so you cannot recover the plaintext token later.

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

The client uses `url` for three endpoints: `POST /cache/batch` to read,
`PUT /cache/batch` to write, and `POST /cache/clear` to delete. The client
chunks the requests by `batch_size`. On a network error, the client retries
3 times, with exponential backoff. If all retries fail, the client falls
back to the local cache only.

### How the client uses the cache

On `fw-context index --analyze`:

1. **Local global cache** (Tier 1, `~/.fw-context/llm_cache.db`): shared
   across all projects on the same machine, with SQLite.
2. **Remote cache server** (Tier 2): only when you configure `[cache_server]`.
   The client stores a cache miss from the server back into the local cache.

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

This is an interactive wizard that configures the remote cache server
connection. The wizard prompts you for the server URL, which defaults to
the existing value, and for the authentication token. The wizard verifies
the connection, and writes the `[cache_server]` section to
`.fw-context/local.toml`.

1. **URL**: the server's base URL, for example `https://fw-cache.example.com`
2. **Token**: a read token, or a read-and-write token, created with
   `fw-cache-admin token create`
3. **Verify**: the wizard calls `/health` and `/cache/stats`, to confirm
   connectivity
4. **Write**: the wizard updates `local.toml` idempotently. The wizard
   replaces the existing `[cache_server]` section, or appends a new one

Re-run the wizard to update the URL or to rotate the token. The wizard
detects the existing configuration, and shows the current value as the
default.

The `--remote` flag reads all the content hashes from the project's
`llm_analysis_cache` table. The `--remote` flag sends these hashes to the
server's `POST /cache/clear` endpoint. This deletes only the current
project's entries. Entries that other projects share remain on the server.

`fw-context cache push` uploads all entries from the local global cache
(`~/.fw-context/llm_cache.db`) to the remote server, with overwrite enabled
(`X-Cache-Overwrite: true`). This command is useful for seeding a
newly-deployed server, or for migrating the cache between machines. This
command requires `can_write` and `can_overwrite` on the token.

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

The wizard uses `sudo` for privileged operations. Make sure the current
user has passwordless sudo, or can enter the password when the system
prompts for it.

### could not change directory to "/home/user"

This message is expected. The `fw-cache` system user has no home directory,
so the service runs from `/tmp`. This message is harmless.

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

This error is a temporary overload of the Let's Encrypt API. Retry in 30 to
60 minutes. The rate limit is a separate issue from this transient error.

### 401 Unauthorized

The token is missing, invalid, or revoked. Check:
- The `Authorization: Bearer <token>` header is present
- The token matches the token from `fw-cache-admin token create`
- `fw-cache-admin token list <project>` shows the token as active, with no
  `revoked_at` value

### 403 Forbidden on PUT

The token has `can_read` but not `can_write`. Create a write token:
```bash
fw-cache-admin token create my-project --write
```

### 403 Forbidden with X-Cache-Overwrite

This action requires `can_overwrite`. Use an admin token, or create an
overwrite token:
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
