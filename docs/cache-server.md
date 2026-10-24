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

When you set the environment variable `FW_CONTEXT_HOME`, the tier 1 file is
`$FW_CONTEXT_HOME/llm_cache.db`.

fw-context computes each content hash from the body, the qualified name, the
signature and the docstring of the symbol. Identical code produces identical
hashes, regardless of the project. So an analysis that fw-context generates for *the second Mbed project* is also
available automatically to *the Mbed project* and *the ESP32 project*. When you configure
`[cache_server]` and re-index any project, fw-context does not send a symbol
to the LLM when the symbol is already in the cache.

The hash does not include the model or the prompt. Thus, after you change
the model or the prompt, an index run gives the old entries of the cache. To
make a new analysis of each symbol, use `--force-llm` (see
[A new analysis with --force-llm](#a-new-analysis-with---force-llm)).

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | none | Public health check |
| `GET` | `/cache/stats` | `can_read` | Cache statistics (entries, models) and the permissions of the token |
| `POST` | `/cache/batch` | `can_read` | Batch lookup by content hash |
| `PUT` | `/cache/batch` | `can_write` | Batch write cache entries |
| `POST` | `/cache/clear` | `can_write` | Delete entries by content hash |

`POST /cache/batch` and `PUT /cache/batch` use the first 1000 hashes or
entries of a request, and ignore the others. The answer then has
`"truncated": true`.

### GET /cache/stats

This endpoint returns cache-wide statistics: the total entries, the newest
and oldest timestamps, and the entry counts for each model. It also returns
the permissions of the token in `can_read`, `can_write` and `can_overwrite`.
`fw-context cache stats --remote` uses this endpoint. The client also uses
this endpoint to find the permissions of its token before its first write.

```json
→ {"total_entries": 13391, "newest_entry": "...", "oldest_entry": "...",
   "models": {"qwen2.5-coder:14b": 13383, ...},
   "can_read": true, "can_write": true, "can_overwrite": false}
```

### POST /cache/clear

```json
{"hashes": ["abc123...", "def456..."]}
→ {"deleted": 2, "total": 2}
```

This endpoint deletes the cache entries that match the given content hashes.
`fw-context cache clear --remote` uses this endpoint. The server takes 1 to
10000 hashes in one request, and answers 422 to a request with more. The
client always sends the hashes in chunks of 100. It does not use
`batch_size` for this endpoint.

## Quickstart

```bash
# Install the package with server dependencies
pip install fw-context-mcp[cache-server]

# Interactive wizard — installs PostgreSQL, creates DB, configures nginx + TLS
fw-cache-server setup

# Or step by step:
fw-cache-server init       # creates schema + admin token
fw-cache-server run        # starts server on :8000
fw-cache-admin project create <project-id>
fw-cache-admin token create <project-id> --write --description "dev-1"
```

`<project-id>` is the project ID: 32 lowercase hex characters, the value of
`id` in the `[project]` section of `<project>/.fw-context/config.toml`.
`fw-context init` writes this value. `fw-cache-admin project create` refuses
a different format.

## Prerequisites

| Component | Ubuntu / Debian | Arch |
|-----------|-----------------|------|
| Python 3.11+ | `apt install python3` | `pacman -S python` |
| PostgreSQL 15+ | `apt install postgresql` | `pacman -S postgresql` |
| nginx (optional) | `apt install nginx` | `pacman -S nginx` |
| certbot (optional) | `apt install certbot python3-certbot-nginx` | `pacman -S certbot certbot-nginx` |

The setup wizard detects missing prerequisites, and asks before it installs
them. It installs them only with `apt` on Linux (PostgreSQL, nginx, certbot)
and with `brew` on macOS (PostgreSQL). It does not use `pacman`. Thus, on
Arch and on other distributions, install the prerequisites before you run
the wizard. For a manual installation, install PostgreSQL, and make sure
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

The wizard is **idempotent**. Re-run the wizard to add new projects, or to
recover from errors. Each step detects the existing state, and skips the
parts that are already configured.

The wizard does not upgrade an existing installation:

- When the server venv exists, the wizard uses it as it is. It does not
  install a newer package into it (see [Upgrading](#upgrading)).
- When an enabled nginx site has `listen 443 ssl`, the wizard does not
  change the nginx configuration.

When the server venv does not exist, the wizard installs fw-context-mcp
into it from a local wheel, not from PyPI. The wheel must have the version
of the fw-context-mcp that runs the wizard:
`fw_context_mcp-<version>-py3-none-any.whl`, in your home directory or in
the temporary directory (usually `/tmp`). If the wizard finds no such wheel,
it stops.

What the wizard does:

1. Creates `/opt/fw-cache-server/venv/` with FastAPI + asyncpg, and the
   `fw-cache` system user
2. Detects the OS, and installs PostgreSQL if missing
3. Creates the `fw_cache` database user, with a random password
4. Creates the `fw_cache_meta` and `fw_cache` databases
5. Initializes the schema, and prints the admin token
6. Creates the project, interactively, and prints two tokens of the
   project: a write token (`can_read`, `can_write`, `can_overwrite`) and a
   read token (`can_read` only)
7. Installs the systemd service (`fw-cache-server`), and creates the
   `fw-cache` system user if it does not exist
8. Optionally configures the nginx reverse proxy and Let's Encrypt TLS

### Option 2: Manual

```bash
pip install fw-context-mcp[cache-server]

# Create database user and databases
sudo -u postgres createuser fw_cache --pwprompt
sudo -u postgres createdb fw_cache_meta --owner fw_cache
sudo -u postgres createdb fw_cache --owner fw_cache

# Create the fw-cache system user (the systemd unit runs as this user)
sudo useradd -r -s /usr/sbin/nologin -d /nonexistent fw-cache

# Save credentials (used by fw-cache-server and fw-cache-admin),
# with the same owner and permissions that fw-cache-server setup uses
sudo mkdir -p /var/lib/fw-cache-server
sudo chmod 0750 /var/lib/fw-cache-server
sudo tee /var/lib/fw-cache-server/db.env > /dev/null << 'EOF'
FW_CACHE_DB_URL="postgresql://fw_cache:YOUR_PASSWORD@localhost:5432"
EOF
sudo chown root:fw-cache /var/lib/fw-cache-server/db.env
sudo chmod 640 /var/lib/fw-cache-server/db.env

# Initialize schema + admin token (only root can read the file)
sudo sh -c 'set -a; . /var/lib/fw-cache-server/db.env; exec "$0" init' "$(command -v fw-cache-server)"
```

The directory `/var/lib/fw-cache-server` is owned by root and has the
permissions 0750. Thus only root can read `db.env` through it. systemd reads
the file as root, through `EnvironmentFile=`.

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
fw-cache-server install-systemd             # write the unit only
sudo systemctl daemon-reload
sudo systemctl enable --now fw-cache-server

# Manage
sudo systemctl status fw-cache-server
sudo systemctl restart fw-cache-server
sudo journalctl -u fw-cache-server -f       # live logs
```

`fw-cache-server install-systemd` writes
`/etc/systemd/system/fw-cache-server.service`. It does not enable or start
the service, and it does not create the `fw-cache` user. Only
`fw-cache-server setup` creates this user.

The systemd unit:
- Runs as the `fw-cache` system user
- Sets `ProtectSystem=strict`, `NoNewPrivileges=yes`, `PrivateTmp=yes`
- Restarts on failure

### launchd (macOS)

```bash
fw-cache-server install-launchd --dry-run   # preview plist
fw-cache-server install-launchd             # write the plist only
launchctl load ~/Library/LaunchAgents/com.fwcontext.cache-server.plist

launchctl list com.fwcontext.cache-server
```

`fw-cache-server install-launchd` writes the plist. It does not load it.

### Direct

```bash
fw-cache-server run --host 127.0.0.1 --port 8000
```

## nginx reverse proxy

The setup wizard configures this automatically. Only `fw-cache-server setup`
writes the nginx configuration. No other command generates it. The wizard
writes two files:

| File | Content |
|------|---------|
| `/etc/nginx/sites-available/fw-cache` | The site configuration |
| `/etc/nginx/conf.d/fw-cache-rate-limit.conf` | The `limit_req_zone` directive, which must be in the `http` block |

For a manual setup, write the two files, then enable the site:

```bash
# /etc/nginx/conf.d/fw-cache-rate-limit.conf holds one line:
#   limit_req_zone $binary_remote_addr zone=fw_context_cache:10m rate=50r/s;

# Enable site
sudo ln -sf /etc/nginx/sites-available/fw-cache /etc/nginx/sites-enabled/

# Test and reload
sudo nginx -t
sudo systemctl reload nginx
```

The site configuration that the wizard writes, for the domain
`fw-cache.example.com`:

```nginx
upstream fw_cache_backend {
    server 127.0.0.1:8000;
    keepalive 32;
}

server {
    listen 80;
    server_name fw-cache.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name fw-cache.example.com;

    ssl_certificate     /etc/letsencrypt/live/fw-cache.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/fw-cache.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    limit_req zone=fw_context_cache burst=20 nodelay;
    client_max_body_size 10485760;

    location / {
        proxy_pass http://fw_cache_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Features of the generated config:
- HTTP → HTTPS redirect
- HTTP/2 on TLS
- TLS 1.2–1.3, modern cipher suite
- Rate limiting: 50 req/s, burst 20
- Request body limit: 10 MB (`client_max_body_size 10485760`), the same
  limit as the server
- Keep-alive connections to upstream (max 32)
- `X-Forwarded-*` headers

A configuration from an older setup has no `client_max_body_size` line.
nginx then uses its default limit of 1 MB, and answers 413 to a larger
request before the request gets to the server. The wizard does not write
an existing configuration again. Thus, add `client_max_body_size 10485760;`
to the `server` block on port 443 yourself, then test and reload nginx.

### TLS with Let's Encrypt

```bash
sudo certbot --nginx -d fw-cache.example.com --non-interactive --agree-tos
```

The setup wizard runs this command automatically when no certificate for the
domain exists. The wizard gives no email address, thus it adds
`--register-unsafely-without-email`. Before this step, the wizard does a
check of the DNS name. If the name does not resolve, the wizard shows a
warning and asks if you want to continue. The default answer is yes, and
certbot then runs. Certificates renew automatically, through
`certbot.timer`, which the certbot package installs.

## Credentials

| File | Purpose | Permissions |
|------|---------|-------------|
| `/var/lib/fw-cache-server/db.env` | PostgreSQL connection URL | 640 (root:fw-cache), in a directory with 0750 |
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
export FW_CACHE_DB_URL="$(sudo sh -c '. /var/lib/fw-cache-server/db.env; printf %s "$FW_CACHE_DB_URL"')"
FW_CACHE_ADMIN_TOKEN="<admin-token>" fw-cache-admin <command>
```

Only root can read `/var/lib/fw-cache-server/db.env` (see
[Credentials](#credentials)). Thus the first command reads the file as
root, through `sudo`, as systemd does. `fw-cache-admin` then runs as your
user. For the fallback file `~/.fw-context/db.env`, use
`source ~/.fw-context/db.env` and `export FW_CACHE_DB_URL`.

### Projects

```bash
fw-cache-admin project list
fw-cache-admin project create <project-id> --description "My firmware project"
fw-cache-admin project remove <project-id> --confirm
```

`<project-id>` is 32 lowercase hex characters (see [Quickstart](#quickstart)).

`fw-cache-admin project create` also creates the first token of the project,
and prints it as `Admin token`. This token has `can_read`, `can_write` and
`can_overwrite`. It is not an admin token for `fw-cache-admin`: only a token
with `is_admin` can run `fw-cache-admin` commands.

When you remove a project, `fw-cache-admin` also deletes the project's
tokens, in a cascade. `fw-cache-admin` does **not** delete the cache entries.
The cache is global, across all projects.

### Tokens

```bash
fw-cache-admin token list <project-id>
fw-cache-admin token create <project-id> --write --description "dev-laptop"
fw-cache-admin token create <project-id> --description "CI-reader"
fw-cache-admin token revoke <token>

# Permission levels:
#   (no flag)    can_read only
#   --write      can_read + can_write
#   --overwrite  can_read + can_write + can_overwrite (not is_admin)
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
# batch_size = 100        # hashes or entries per request, 1 to 1000
# force = false           # set true to overwrite existing entries
```

The client uses `url` for four endpoints: `POST /cache/batch` to read,
`PUT /cache/batch` to write, `POST /cache/clear` to delete, and
`GET /cache/stats` to find the permissions of the token before the first
write or delete. The client chunks the reads and the writes by `batch_size`.
The server uses at most 1000 hashes or entries of one batch request, thus
the client uses 1000 for a larger value. The client sends the deletes in
chunks of 100. The
client also splits a write chunk into more requests, to keep each request
body below 8 MB (80 % of the 10 MB server limit). A `batch_size` below 1
makes `fw-context cache push` exit with status 1. On a network error, a 429
or a 5xx, the client tries the request 3 times in total, with exponential
backoff. If all the attempts fail, the client falls back to the local cache
only.

### How the client uses the cache

On `fw-context index --analyze`, on `fw-context analyze`, and on the MCP
tool `reindex_file`:

1. **Local global cache** (Tier 1, `~/.fw-context/llm_cache.db`): shared
   across all projects on the same machine, with SQLite.
2. **Remote cache server** (Tier 2): only when you configure `[cache_server]`.
   The client stores a cache hit from the server in the local cache.

### A new analysis with --force-llm

`fw-context index --force-llm` and `fw-context analyze --force-llm` make a
new analysis of each symbol. With `--force-llm`, the client:

1. Does not read the remote cache server.
2. Ignores a local entry that is older than the start of the forced run, or
   that is from a different model.
3. Writes the new analysis over the local entry.
4. Sends the new analysis to the server with `X-Cache-Overwrite: true`.

The overwrite on the server requires `can_overwrite` on the token. If the
token does not have it, the server answers 403. The client then sets the
token as read-only, and sends no more writes in that run.

### Cache management commands

```bash
# Show cache statistics
fw-context cache stats                   # Tier 1 only
fw-context cache stats --remote          # Tier 2 only, and how many entries of this project the server has

# Clear cache
fw-context cache clear                   # local cache only (Tier 1)
fw-context cache clear --remote          # the hashes of this project, from the server (Tier 2)
fw-context cache clear --all             # both tiers
fw-context cache clear --remote -y       # skip confirmation

# Upload the local entries that the remote server does not have
fw-context cache push                    # batch size 100
fw-context cache push --batch 500        # larger batches for faster transfer (1 or more; above 1000 sends 1000)
fw-context cache push --overwrite        # also replace the server's entries

# Interactive remote cache setup
fw-context cache remote-init             # configure URL and token interactively
```

### `fw-context cache remote-init`

This is an interactive wizard that configures the remote cache server
connection. The wizard prompts you for the server URL, which defaults to
the existing value, and for the authentication token. The wizard verifies
the connection, and then writes two files:

- The URL goes into the `[cache_server]` section of the global config,
  `~/.fw-context/config.toml`.
- The token goes into `~/.fw-context/.cache_token`, with the permissions
  0600. The token does not go into a config file.

The wizard writes no project file. It ignores `--project`. A
`[cache_server]` value in the `local.toml` of a project has priority over
the global value. fw-context reads `~/.fw-context/.cache_token` only when
no config file sets `token`.

1. **URL**: the server's base URL, for example `https://fw-cache.example.com`
2. **Token**: a read token, or a read-and-write token, created with
   `fw-cache-admin token create`
3. **Verify**: the wizard calls `/health` and `/cache/stats`, to confirm
   connectivity
4. **Write**: the wizard sets `url` in the global config, and writes the
   token file again

Re-run the wizard to update the URL or to rotate the token. The wizard
detects the existing configuration, and shows the current URL as the
default.

`fw-context cache clear --remote` reads all the content hashes from the
`llm_analysis` table of the project index. It sends these hashes to the
server's `POST /cache/clear` endpoint. The server keeps one entry for each
hash, for all projects. Thus the command also deletes the entries that other
projects share with this project. The other projects then lose these entries
too.

`fw-context cache push` sends the entries of the local global cache
(`~/.fw-context/llm_cache.db`) to the remote server. The server keeps each
entry it already has (first write wins), so only the missing entries are
stored. The command reports how many entries it inserted and how many the
server already had. This command is useful for seeding a newly-deployed
server, or for migrating the cache between machines. This command requires
`can_write` on the token.

The same content hash does not guarantee the same analysis text: the output
of the model varies from run to run, and the hash does not cover the model
or the prompt. To replace the server's entries with the local ones, add
`--overwrite`. This sends `X-Cache-Overwrite: true`, and requires
`can_overwrite` on the token. To make a new analysis before the push, use
`--force-llm` (see
[A new analysis with --force-llm](#a-new-analysis-with---force-llm)).

The command checks the token before it sends anything. It exits with status
1 in these cases:

- The server rejects the token (401 or 403).
- The server answers 429. The server gives 429 only after too many failed
  token attempts. Check the token, then wait and try again.
- The server answers with a body that is not a JSON object. A proxy can be
  in front of the server.
- The server cannot be reached.
- The token cannot write.
- `--overwrite` is given without `can_overwrite`.
- The server refuses a write part of the way through.

For the first four cases, the message tells which cause it is.

The server refuses a whole request when one entry in it breaks one of these
limits:

| Field | Limit |
|---|---|
| `hash` | 64 lowercase hex characters (SHA-256) |
| `summary` | 5000 characters |
| `inputs`, `outputs` | 100000 characters each |
| `model` | 100 characters |
| Request body | 10 MB (the server, and the nginx config that setup writes) |

The command sends no such entry. It keeps the entry in the local cache,
shows the number of these entries and the first five with the reason, and
sends the others. These entries do not change the exit status. The client
also makes each request smaller than the body limit: it splits a batch of
large entries into more requests.

After the first write request that fails, the command sends no more
requests. It shows how many entries the server took before the failure, and
exits with status 1. The entries of the requests before the failure stay on
the server.

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
The server binds to `127.0.0.1` by default, and the generated systemd unit
keeps this default.

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
fw-cache-admin token create <project-id> --write --description "rotation-2026-07"

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
`fw-cache-server setup` puts the symlinks `fw-cache-server` and
`fw-cache-admin` in `/usr/local/bin`. It does not repair the venv. When the
binary exists, setup uses the venv as it is, and only adds a symlink that
does not exist. It keeps a symlink that points to a missing file.

To repair:

1. If the binary exists, make the symlinks:
   `sudo ln -sf /opt/fw-cache-server/venv/bin/fw-cache-server /usr/local/bin/fw-cache-server`
   and the same for `fw-cache-admin`.
2. If the binary does not exist, re-run `fw-cache-server setup`. Setup then
   makes the venv again from the local wheel (see
   [Option 1](#option-1-setup-wizard-recommended)).

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

### 429 Too Many Requests, or a body that is not JSON

The server gives 429 only to a request with a token that failed. After 20
failed token attempts from one IP address in 60 seconds, the server answers
429 to the next failed attempts from that address. Check the token, then
wait and try again.

When the server answers 200 with a body that is not a JSON object, a proxy
or a captive portal is usually in front of the server. The log names the
content type of the body. This tells you which device answered.

During `fw-context index`, the client continues for both cases: a read gives
no cached entries, and a write counts as failed. The client logs the
non-JSON body as a warning once for each client, and at debug level after
that.

### 403 Forbidden on PUT

The token has `can_read` but not `can_write`. Create a write token:
```bash
fw-cache-admin token create <project-id> --write
```

### 403 Forbidden with X-Cache-Overwrite

This action requires `can_overwrite`. Use an admin token, or create an
overwrite token:
```bash
fw-cache-admin token create <project-id> --overwrite
```

## Environment variables summary

| Variable | Required by | Description |
|----------|-------------|-------------|
| `FW_CACHE_DB_URL` | server, admin | PostgreSQL base URL (without database name) |
| `FW_CACHE_ADMIN_TOKEN` | admin | Admin token for `fw-cache-admin` commands |
| `FW_CACHE_HOST` | server | Bind address (default `127.0.0.1`) |
| `FW_CACHE_PORT` | server, `install-systemd`, `install-launchd` | Bind port (default `8000`). The two install commands write this port into the unit or the plist |
| `FW_CACHE_TRUSTED_PROXIES` | server | Comma-separated CIDRs of the proxies whose `X-Forwarded-For` the server uses for the client IP address. Default: loopback and the private ranges (`127.0.0.0/8`, `::1/128`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`). The server ignores an incorrect CIDR |

## File paths reference

| Path | Owner | Purpose |
|------|-------|---------|
| `/opt/fw-cache-server/venv/` | fw-cache:fw-cache | Server Python environment |
| `/var/lib/fw-cache-server/db.env` | root:fw-cache | Database credentials (640, readable by root and the `fw-cache` group) |
| `~/.fw-context/db.env` | user | Fallback credentials (single-user) |
| `/etc/systemd/system/fw-cache-server.service` | root | systemd unit |
| `~/Library/LaunchAgents/com.fwcontext.cache-server.plist` | user | macOS launchd plist |
| `/etc/nginx/sites-available/fw-cache` | root | nginx site config |
| `/etc/nginx/sites-enabled/fw-cache` | root | nginx site symlink |
| `/etc/nginx/conf.d/fw-cache-rate-limit.conf` | root | nginx rate-limit zone (`limit_req_zone`) |
| `/etc/letsencrypt/live/<domain>/` | root | TLS certificates |
| `~/.fw-context/llm_cache.db` | user | Local cache, shared across all projects (SQLite) |
| `<project>/.fw-context/config.toml` | user | Shared project config (commit to git) |
| `<project>/.fw-context/local.toml` | user | Local developer overrides (`[cache_server]`, `[llm]`) |

## Upgrading

`fw-cache-server setup` does not upgrade an existing server venv. It finds
the venv, and uses it as it is. Thus, install the new release into the
server venv yourself:

```bash
# Install the new release into the server venv
sudo /opt/fw-cache-server/venv/bin/pip install --upgrade fw-context-mcp[cache-server]
sudo chown -R fw-cache:fw-cache /opt/fw-cache-server/venv
sudo systemctl restart fw-cache-server
```
