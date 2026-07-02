# Shared LLM Analysis Cache — Centrální cache pro analýzu symbolů

**Stav:** Schváleno — Fáze 1 (server) jako první

## Problém

### Primární — opakovaná LLM analýza velkých symbolů (VYŘEŠENO)

Velké structy (100+ fields) generují prompt který se nevejde do kontextu
modelu. LLM vrací `unparseable response`, ale protože se neuloží žádný
záznam do `llm_analysis`, symbol se při každém `fw-context index --analyze`
znovu a znovu posílá do Ollama → nekonečná smyčka.

**Vyřešeno v `runner.py` (`_build_llm_analysis`):** prompt-fit check +
sentinel row `model="skip:toolarge:{ctx_size}"`.

### Sekundární — sdílení LLM analýzy mezi vývojáři (TENTO PLÁN)

**Situace:**
- Projekt zbox: `fw-context index --analyze` trvá 8–12 h na CPU (jen LLM
  analýza symbolů — samotné parsování a indexování je rychlé, minuty)
- Někteří devové nemají GPU → analýzu nespouští, nebo trvá desítky hodin
- GPU stanice se vypíná, server běží 24/7

**Klíčový vhled:** Jediné co je pomalé a potřebuje sdílení je **LLM analýza
symbolů**. Parsování, indexování, reference, embeddingy — to všechno jsou
minuty. Jen Ollama volání (tisíce symbolů, každý 10-30s) trvá hodiny.

`llm_analysis_cache` už existuje — je to content-addressable cache kde
klíčem je `SHA256(f"{body}|{qualified_name}|{signature}|{docstring}")`.
SHA256 je kryptografický hash — pravděpodobnost kolize je zanedbatelná
(2⁻²⁵⁶).  Zahrnutí `qualified_name` je záměrné: i identické tělo funkce
v různých namespacech dostane jiný hash, což je správné chování
(analýza pro `mbed::sleep` ≠ `zephyr::sleep`).  Identický symbol
v různých projektech nebo po re-indexu dostane stejný hash → cache hit.

## Řešení

**Centralizovat jen `llm_analysis_cache`, zbytek zůstává lokální SQLite.**

Jedna centrální cache pro **všechny projekty**. Dnes je cache per-projekt
(v lokálním SQLite) — každý projekt analyzuje Zephyr/Mbed symboly znovu.
S centrálním serverem první projekt naplní cache a všechny ostatní ji čtou:

- Projekt A (zbox, Zephyr) → 5000 symbolů, 8h na GPU → zaplní cache
- Projekt B (birdie, Zephyr) → 80 % symbolů cache hit → jen ~1000 Ollama volání
- Projekt C (další Zephyr) → 85 % cache hit → jen ~750 volání

### Architektura — jedna instance, multi-tenant

```
                        internet (HTTPS)
                              │
┌─────────────────────────────┼──────────────────────────────────┐
│ Server (24/7)               │                                  │
│                              │                                  │
│  ┌───────────────────────────┴──────────────────────────────┐  │
│  │ nginx :443 (HTTPS)                                      │  │
│  │  ├─ reverse proxy → fw-cache-server :8000               │  │
│  │  ├─ SSL cert (Let's Encrypt)                            │  │
│  │  └─ rate limiting (limit_req, burst 50)                 │  │
│  └───────────────────────────┬──────────────────────────────┘  │
│                              │                                  │
│  ┌───────────────────────────┴──────────────────────────────┐  │
│  │ fw-cache-server :8000                                   │  │
│  │   jeden proces, jeden port                              │  │
│  │                                                         │  │
│  │   Token auth middleware (Bearer token → oprávnění)       │  │
│  │   POST /cache/batch  — dávkový lookup (pole hashů)     │  │
│  │   PUT  /cache/batch  — dávkový zápis                    │  │
│  │   GET  /health       — health check                     │  │
│  └───────────────────────────┬──────────────────────────────┘  │
│                              │                                  │
│  ┌───────────────────────────┴──────────────────────────────┐  │
│  │ PostgreSQL :5432                                        │  │
│  │   ├─ fw_cache_meta  — tokeny, projekty, revocations     │  │
│  │   └─ fw_cache       — llm_analysis_cache (globální)     │  │
│  └─────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
         ▲                        ▲
         │ HTTPS                  │ HTTPS
    ┌────┴──────────┐       ┌────┴──────────┐
    │ Dev A (GPU)   │       │ Dev B (CPU)    │
    │               │       │               │
    │ Lokální       │       │ Lokální        │
    │ index.db      │       │ index.db       │
    │               │       │               │
    │ Token: read+write    │ Token: read-only│
    │               │       │               │
    │ Cache miss →  │       │ Cache miss →   │
    │ Ollama (GPU)  │       │ GET /cache/    │
    │ → PUT /cache  │       │   → hit!       │
    │               │       │   (uloží se    │
    │               │       │    lokálně)    │
    └───────────────┘       └───────────────┘
```

**Multi-tenant model:**

- Jeden `fw-cache-server` proces, jeden port
- **Jedna globální cache** — všechny projekty sdílí `fw_cache` databázi
  (content hash je univerzální: `body|qualified_name|signature|docstring`
  v SHA256 vstupu zajišťuje že různé verze téže knihovny i různé
  namespacey (`mbed::sleep` vs `zephyr::sleep`) dostanou jiný hash).
  **Namespace projektu (`firma/zbox`) je čistě organizační — slouží jen
  pro token management.** Na sdílení cache nemá vliv. 5 projektů nad
  Zephyr = první zaplní cache, zbylé 4 čtou.
- Token určuje tři oprávnění: `can_read` + `can_write` + `can_overwrite`
  (běžný write token **nepřepisuje** existující záznamy — `ON CONFLICT DO
  NOTHING`; `can_overwrite` povolí `X-Cache-Overwrite` hlavičku a přepíše
  záznam novou analýzou, např. po upgradu modelu)
- Projekty slouží jako organizační jednotka pro tokeny (token patří k projektu)
- Přidání projektu = `fw-cache-admin project create firma/zbox` (žádný nový proces)
- Tokeny se generují serverem, hashnuté před uložením (SHA256 → `/etc/shadow` model)

**Flow:**
1. GPU dev spustí `fw-context index --analyze`
   - Pro každý symbol: `compute_content_hash()` → POST `/cache/batch` na server
   - Cache hit → okamžitě uloženo do lokální `llm_analysis` (bez Ollama)
   - Cache miss → Ollama (GPU) → výsledek se uloží lokálně **a zároveň
     PUT `/cache/batch` na server**
2. CPU dev spustí `fw-context index --analyze`
   - Pro každý symbol: `compute_content_hash()` → POST `/cache/batch` na server
   - Cache hit → okamžitě uloženo lokálně (naprostá většina)
   - Cache miss → Ollama (CPU) — ale jen pro nové/změněné symboly
3. GPU dev s write tokenem zapisuje do cache jen **nové záznamy**
   (`INSERT ON CONFLICT DO NOTHING` — existující záznam zůstane).
   Pro přepsání po upgradu modelu se použije `--force` flag, který
   posílá `X-Cache-Overwrite: true` — server ho povolí jen tokenům
   s `can_overwrite=true` (typicky jeden admin/writer).
   CPU dev s read-only tokenem nemůže zapisovat (HTTP 403).

**Efekt:**
- CPU dev analyzuje jen symboly které ještě nikdo neanalyzoval
- Jakmile GPU dev jednou projet celý projekt, všichni ostatní mají cache hit
  pro všechny nezměněné symboly → `fw-context index --analyze` trvá minuty
  místo hodin
- Cache je content-addressable → přežije re-index, změnu configu, přechod
  na jiný projekt se stejným SDK kódem

## Instalace a deployment serveru

### Předpoklady

- Linux nebo macOS server s veřejnou IP / doménou
- Python 3.11+
- sudo přístup

### 1. Interaktivní setup (doporučená cesta)

```bash
pip install fw-context-mcp
fw-cache-server setup
```

Jeden příkaz, který interaktivně provede vše — detekuje OS, nainstaluje
PostgreSQL/nginx/certbot (apt/brew), vytvoří `fw_cache` uživatele a databáze
(`fw_cache_meta` + `fw_cache`), inicializuje cache server, vytvoří první
projekt a tokeny, nainstaluje systemd/launchd službu, nakonfiguruje nginx
HTTPS + Let's Encrypt, otevře firewall, nastaví logrotate.

Detailní průběh:

```
$ fw-cache-server setup

 ▸ fw-context Cache Server Setup
 ────────────────────────────────────────────

 [✓] OS — Linux (Ubuntu 24.04) / macOS 15
 [!] PostgreSQL — nenalezen
 ▶ Nainstalovat PostgreSQL? [Y/n] y
     sudo apt install postgresql postgresql-client
     sudo systemctl enable --now postgresql
 [✓] PostgreSQL — běží (verze 16.2)

 [!] Databázový uživatel 'fw_cache' — neexistuje
 ▶ Vytvořit? [Y/n] y
     sudo -u postgres psql -c "CREATE USER fw_cache WITH PASSWORD '<generovano>' CREATEDB"
 [✓] Uživatel vytvořen
     Přihlašovací údaje uloženy do /etc/fw-cache-server/db.env

 [!] Meta databáze 'fw_cache_meta' — neexistuje
 ▶ Vytvořit? [Y/n] y
     sudo -u postgres psql -c "CREATE DATABASE fw_cache_meta OWNER fw_cache"
 [✓] Databáze vytvořena

 [!] Cache databáze 'fw_cache' — neexistuje
 ▶ Vytvořit? [Y/n] y
     sudo -u postgres psql -c "CREATE DATABASE fw_cache OWNER fw_cache"
 [✓] Databáze vytvořena
 [ ] Cache server — neinicializován

 ▶ Spustit init? [Y/n] y
     Vytvářím tabulky v fw_cache_meta...
     Admin token: a3f7b2c1d4e5f6a7b8c9d0e1f2a3b4c5
     ⚠ Uložte si tento token — slouží pro správu!

 [✓] Cache server — inicializován

 ────────────────────────────────────────────

 ▶ Název projektu (např. firma/zbox): firma/zbox
     [✓] Projekt vytvořen
     Write token:  a3f7b2c1d4e5f6a7b8c9d0e1f2a3b4c5
     Read token:   f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1

 ────────────────────────────────────────────

 [ ] systemd služba — nenainstalována
 ▶ Nainstalovat systemd službu? [Y/n] y
     [✓] /etc/systemd/system/fw-cache-server.service
     ▶ Spustit a povolit? [Y/n] y
     [✓] fw-cache-server — running

 ────────────────────────────────────────────

 [!] nginx — nenalezen
     (nginx HTTPS reverse proxy doporučujeme pro ostrý provoz)

 ▶ Chcete nainstalovat a nakonfigurovat nginx? [Y/n] y

     ▶ Instalovat nginx? [Y/n] y
         sudo apt install nginx ...
     [✓] nginx nainstalován

     ▶ Doména pro HTTPS (např. fw-cache.example.com): fw-cache.mojefirma.cz
     [✓] DNS záznam ověřen — fw-cache.mojefirma.cz → <server IP>

     [!] Certifikát nenalezen
     ▶ Vygenerovat certifikát přes Let's Encrypt? [Y/n] y
         sudo certbot --nginx -d fw-cache.mojefirma.cz
     [✓] Certifikát vydán — expiruje 2026-09-30
     [✓] Auto-renewal nakonfigurován (certbot.timer)

     [✓] nginx site vytvořen — /etc/nginx/sites-available/fw-cache
     [✓] REVERSE PROXY → https://fw-cache.mojefirma.cz → :8000

 ────────────────────────────────────────────

 [✓] Instalace dokončena!

   Server běží na:  https://fw-cache.mojefirma.cz
   Admin token:     a3f7b2c1d4e5f6a7b8c9d0e1f2a3b4c5
    Projekt 'firma/zbox':
      Write token:   a3f7b2c1d4e5f6a7b8c9d0e1f2a3b4c5
      Read token:    f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1

   Pro devy — do .fw-context/local.toml přidejte:
     [cache_server]
     url = "https://fw-cache.mojefirma.cz"
     token = "${FW_CACHE_TOKEN}"
```

**Detekce existujícího nginx s HTTPS:**

Když na serveru už nginx s HTTPS běží:

```
 [!] nginx — detekován, HTTPS aktivní (na <existující domény>)

 ▶ Použít existující nginx instalaci? [Y/n] y

     ▶ Doména pro cache server: fw-cache.mojefirma.cz
         (must point to this server's IP)
     [✓] DNS záznam ověřen

     [!] Port 443 už používá jiný server block
     ▶ Přidat nový server block na port 443 (SNI)? [Y/n] y
     [!] Pro novou doménu je potřeba certifikát
     ▶ Chcete rozšířit existující Let's Encrypt certifikát
        nebo vytvořit nový? [expand/nový] expand
         sudo certbot --nginx -d fw-cache.mojefirma.cz --expand
     [✓] Certifikát rozšířen

     [✓] /etc/nginx/sites-available/fw-cache
     [✓] nginx reload proveden
```

**Detekce stavů — co setup zvládá:**

| Stav | Akce |
|------|------|
| PostgreSQL neinstalován | `sudo apt install postgresql` / `brew install postgresql@16` |
| PostgreSQL běží, `fw_cache` user chybí | `CREATE USER fw_cache WITH PASSWORD '<gen>' CREATEDB` |
| `fw_cache_meta` DB chybí | `CREATE DATABASE fw_cache_meta OWNER fw_cache` |
| nginx neinstalován | `sudo apt install nginx` / `brew install nginx` |
| nginx běží bez HTTPS | Nabídne certbot, získá certifikát |
| nginx běží s HTTPS (jiná doména) | Přidá nový server block (SNI), rozšíří certifikát |
| nginx běží, certifikát existuje | Přeskočí certbot, jen přidá config |

**Další kroky které setup automaticky řeší:**

- **Firewall** — detekuje ufw/firewalld, nabídne otevření portů 80+443
  ```bash
  sudo ufw allow 80/tcp
  sudo ufw allow 443/tcp
  ```
- **Certbot instalace** — pokud není, nainstaluje (`apt install certbot python3-certbot-nginx`)
- **nginx config syntax check** — `nginx -t` před reloadem
- **Závislosti** — ověří že `nginx` (a `certbot`) uživatel má právo číst `/etc/nginx/sites-available/`
- **Konflikt portů** — detekuje že port 8000 není obsazený; pokud je, nabídne alternativní port
- **Unix user** — doporučí vytvořit dedikovaného `fw-cache` uživatele pro systemd službu (sudo useradd)
- **Logrotate** — vygeneruje `/etc/logrotate.d/fw-cache-server` pro rotaci logů
- **Systemd hardening** — unit template obsahuje `ProtectSystem=strict`, `NoNewPrivileges=yes`, `PrivateTmp=yes`

### 2. Ruční cesta (když setup nelze použít)

```bash
# 1. Nainstalovat PostgreSQL
sudo apt install postgresql postgresql-client   # Debian/Ubuntu
brew install postgresql@16                      # macOS

# 2. Vytvořit uživatele a databáze
sudo -u postgres psql <<SQL
CREATE USER fw_cache WITH PASSWORD 'bezpecne_heslo' CREATEDB;
CREATE DATABASE fw_cache_meta OWNER fw_cache;
CREATE DATABASE fw_cache OWNER fw_cache;
SQL

# 3. Nainstalovat fw-context-mcp
pip install fw-context-mcp

# 4. Inicializovat
export FW_CACHE_DB_URL="postgresql://fw_cache:bezpecne_heslo@localhost:5432"
fw-cache-server init
# → Admin token: xxxxxxxxxxxx

# 5. Spustit
fw-cache-server install-systemd   # nebo install-launchd na macOS
sudo systemctl enable --now fw-cache-server

# 6. nginx (volitelné — viz sekce 5)
```

### 3. Správa projektů a tokenů (přes admin CLI)

Administrátorské operace (`fw-cache-admin`) běží na serveru a komunikují **přímo
s PostgreSQL** (přes `FW_CACHE_DB_URL` + admin token) — ne přes HTTP API.
Nevyžaduje to žádné admin endpointy, což je bezpečnější (žádný admin přístup
zvenčí). Admin operace jsou vzácné (párkrát za život projektu), shell access
na server je dostačující.

```bash
# Všechny admin příkazy používají FW_CACHE_DB_URL + admin token
export FW_CACHE_ADMIN_TOKEN="xxxxxxxxxxxx"

# Vytvořit projekt (projekt_id = namespace/název, např. firma/zbox)
fw-cache-admin project create firma/zbox --description "Z-BOX firmware"
# → write token: a3f7b2c1d4e5f6a7b8c9d0e1f2a3b4c5  (předat GPU devovi)
# → read token:  f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1  (předat CPU devům)

# Vytvořit write token (s možností overwrite pro upgrade modelu)
fw-cache-admin token create firma/zbox --write --description "GPU server"
# → write token: a3f7b2c1d4e5f6a7b8c9d0e1f2a3b4c5
# Vytvořit write token s právem přepisu (pro admina s nejlepším modelem)
fw-cache-admin token create firma/zbox --write --overwrite --description "Admin GPU"
fw-cache-admin token create firma/zbox --readonly --description "Honza - CPU"

# Další projekt
fw-cache-admin project create startupera/birdie --description "Birdie firmware"
fw-cache-admin token create startupera/birdie --readonly --description "Petr"

# Smazat projekt (maže jen projekt + tokeny z fw_cache_meta,
# centrální cache ve fw_cache zůstává — je sdílená)
fw-cache-admin project remove firma/zbox --confirm

# Cache management (cache je globální, nesouvisí s projekty)
fw-cache-admin cache stats        # počet záznamů, velikost, stáří
fw-cache-admin cache purge --older-than 90d   # smazat starší 90 dní
# Lokálně (u deva):
fw-context cache clear             # smazat ~/.fw-context/llm_cache.db

# Výpis
fw-cache-admin project list
fw-cache-admin token list firma/zbox
```

### 4. Spuštění cache serveru

```bash
# Na popředí (testování)
fw-cache-server run --host 0.0.0.0 --port 8000

# Jako systemd služba (Linux)
fw-cache-server install-systemd
# → Zapíše unit do /etc/systemd/system/fw-cache-server.service
sudo systemctl enable --now fw-cache-server
sudo systemctl status fw-cache-server

# Jako launchd služba (macOS)
fw-cache-server install-launchd
# → Zapíše plist do ~/Library/LaunchAgents/com.fwcontext.cache-server.plist
launchctl load ~/Library/LaunchAgents/com.fwcontext.cache-server.plist
```

Obsah systemd unit (generovaný automaticky):

```ini
[Unit]
Description=fw-context LLM Analysis Cache Server
After=network.target postgresql.service

[Service]
Type=simple
User=fw-cache
Environment=FW_CACHE_DB_URL=postgresql://fw_cache:bezpecne_heslo@localhost:5432
Environment=FW_CACHE_PORT=8000
ExecStart=/path/to/fw-cache-server run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 5. nginx HTTPS reverse proxy (ruční, fallback)

```nginx
# /etc/nginx/sites-available/fw-cache
upstream fw_cache_backend {
    server 127.0.0.1:8000;
    keepalive 32;
}
server {
    listen 443 ssl;
    server_name fw-cache.example.com;

    ssl_certificate     /etc/letsencrypt/live/fw-cache.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/fw-cache.example.com/privkey.pem;

    # Rate limiting — 50 req/s burst 20
    limit_req_zone $binary_remote_addr zone=fw_cache:10m rate=50r/s;
    limit_req zone=fw_cache burst=20 nodelay;

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

### 6. Deploy checklist

**Doporučená cesta — `fw-cache-server setup` (interaktivní):**

```bash
pip install fw-context-mcp
fw-cache-server setup   # projde vše: PostgreSQL, init, projekty, systemd, nginx, HTTPS
```

**Ruční cesta (když setup nelze použít — např. air-gapped):**

- [ ] PostgreSQL nainstalováno a běží
- [ ] Vytvořen `fw_cache` uživatel s `CREATEDB`
- [ ] Vytvořeny databáze `fw_cache_meta` a `fw_cache`
- [ ] `pip install fw-context-mcp`
- [ ] `fw-cache-server init` → uschovat admin token
- [ ] Vytvořeny projekty a tokeny pro devy (`fw-cache-admin`)
- [ ] `fw-cache-server install-systemd` / `install-launchd`
- [ ] nginx HTTPS + Let's Encrypt (je-li potřeba)

## Co se musí změnit

### 1. Nové moduly

```
src/fw_context_mcp/
  cache_server/               # Nový modul — jen serverová část
    __init__.py
    app.py                    # FastAPI app, endpointy, middleware
    auth.py                   # Token validation, permission check
    backend.py                # PostgreSQL backend (asyncpg)
    admin.py                  # CLI: project/token management
    cli.py                    # Entry point: fw-cache-server
    install.py                # systemd/launchd unit generátor
    setup.py                  # Interaktivní průvodce instalací
    nginx_config.py           # Detekce nginx, certbot, generování configu
```

Nové entry points v `pyproject.toml`:

```toml
[project.scripts]
fw-cache-server = "fw_context_mcp.cache_server.cli:main"
fw-cache-admin  = "fw_context_mcp.cache_server.admin:main"
```

### 2. API endpointy

```python
# Autentizace: Authorization: Bearer <token>
# Token middleware extrahuje (project_id, can_read, can_write) z fw_cache_meta

# POST — dávkový lookup
# Vstup: {"hashes": ["sha256_a", "sha256_b", ...]}
# Výstup: {"results": {"sha256_a": {cache_entry}|null, ...}}
POST /cache/batch

# PUT — dávkový zápis (vyžaduje can_write=true)
# Vstup: {"entries": [{"hash": "sha", "summary": "...", "inputs": "...",
#           "outputs": "...", "model": "..."}, ...]}
# Implicitní chování: INSERT ON CONFLICT DO NOTHING
#   (první analýza vyhrává — horší model nepřepíše lepší)
# S hlavičkou X-Cache-Overwrite: true (vyžaduje can_overwrite=true)
#   → INSERT ON CONFLICT DO UPDATE (přepis, např. po upgradu modelu)
PUT /cache/batch

# Health check (bez autentizace)
GET /health

HTTP status codes:
  200 OK — úspěšný batch_get/batch_put
  401 Unauthorized — chybějící/neplatný token
  403 Forbidden — token nemá potřebné oprávnění (can_read/can_write/can_overwrite)
  422 Unprocessable Entity — nevalidní JSON body
  429 Too Many Requests — rate limit překročen (nginx limit_req)
  503 Service Unavailable — PostgreSQL nedostupné
```

### 3. Databázové schéma

**Meta databáze `fw_cache_meta`:**

```sql
CREATE TABLE projects (
    id          TEXT PRIMARY KEY,      -- "firma/zbox"
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE tokens (
    id            SERIAL PRIMARY KEY,
    token_hash    BYTEA NOT NULL UNIQUE, -- SHA256(full_token)
    project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    can_read      BOOLEAN NOT NULL DEFAULT true,
    can_write     BOOLEAN NOT NULL DEFAULT false,
    can_overwrite BOOLEAN NOT NULL DEFAULT false, -- povoluje X-Cache-Overwrite
    description   TEXT,                  -- "Honza - CPU"
    created_at    TIMESTAMPTZ DEFAULT now(),
    revoked_at    TIMESTAMPTZ            -- NULL = aktivní
);

CREATE INDEX idx_tokens_project ON tokens(project_id);
```

**Cache databáze `fw_cache` (globální, sdílená všemi projekty):**

```sql
CREATE TABLE llm_analysis_cache (
    content_hash TEXT PRIMARY KEY,   -- SHA256
    summary      TEXT NOT NULL,
    inputs       TEXT NOT NULL,
    outputs      TEXT NOT NULL,
    model        TEXT NOT NULL,
    analyzed_at  TIMESTAMPTZ DEFAULT now()  -- pro cache purge --older-than
);
```

### 4. Autentizační flow

```
Request: POST /cache/batch
Header:  Authorization: Bearer a3f7b2c1d4e5f6a7b8c9d0e1f2a3b4c5

1. SHA256(token) → lookup v fw_cache_meta.tokens
2. WHERE token_hash = SHA256(...) AND revoked_at IS NULL
3. → (project_id="firma/zbox", can_read, can_write, can_overwrite)
4. Query spustit proti fw_cache (jedna globální cache)
5. POST /cache/batch — vyžaduje can_read → 403 pokud false
6. PUT /cache/batch:
   - vyžaduje can_write → 403 pokud false
   - bez X-Cache-Overwrite: INSERT ON CONFLICT DO NOTHING (první analýza vyhrává)
   - s X-Cache-Overwrite: vyžaduje can_overwrite → 403 pokud false
     → INSERT ON CONFLICT DO UPDATE (přepíše existující záznam)
7. Díky tomu horší model nikdy nepřepíše lepší — bez číselníku modelů
```

Tokeny se v DB ukládají jako SHA256 hash — plaintext nikde neleží.
Hash index na `token_hash` zajišťuje rychlý lookup (O(log n), pro stovky
tokenů okamžité).

### 5. Klient v `runner.py`

`CacheClient` — HTTP klient pro centrální server:

```python
class CacheClient:
    def __init__(self, url: str, token: str, timeout: float = 30.0, force: bool = False):
        ...
    def batch_get(self, hashes: list[str]) -> dict[str, dict | None]:
        """POST /cache/batch — lookup více hashů najednou.
        Connection error → warning log + prázdný dict (offline fallback)."""
        ...
    def batch_put(self, entries: list[dict]) -> None:
        """PUT /cache/batch — zápis více záznamů najednou.
        With force=True: X-Cache-Overwrite header → INSERT ON CONFLICT DO UPDATE.
        Without force: INSERT ON CONFLICT DO NOTHING (první analýza vyhrává).
        Connection error → warning log + pokračuje (non-fatal)."""
        ...
```

Lokální cache — `~/.fw-context/llm_cache.db` (globální SQLite pro všechny projekty):

```python
def get_local_cache_db() -> sqlite3.Connection:
    path = Path.home() / ".fw-context" / "llm_cache.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE IF NOT EXISTS llm_analysis_cache (
        content_hash TEXT PRIMARY KEY, summary TEXT, inputs TEXT,
        outputs TEXT, model TEXT, analyzed_at TEXT)""")
    return conn
```

V `runner.py:_build_llm_analysis`:

Signatura se rozšiřuje o `cache_client: CacheClient | None` — `run()`
ho vytvoří z `Config.cache_server` a předá:

```python
def _build_llm_analysis(
    conn, config_hash, llm_config, db_dir, *,
    exclude_like=None, write_lock_held=False,
    cache_client: CacheClient | None = None,  # ← nový parametr
) -> None:

```python
local_cache = get_local_cache_db()

# Fáze 1: Hromadný lokální lookup
hashes = [(h, sym) for h, sym in symbols]
local_hits = _bulk_local_cache_lookup(local_cache, hashes)

# Fáze 2: Jeden hromadný remote lookup pro lokální missy
misses = [h for h, _ in hashes if h not in local_hits]
if cache_client and misses:
    remote_hits = cache_client.batch_get(misses)  # jediný POST za dávku
    for h, entry in remote_hits.items():
        if entry:
            upsert_local_cache(local_cache, h, entry)
            local_hits[h] = entry

# Fáze 3: Ollama pro zbylé missy, batch_put po dávkách
pending_put = []
for h, sym in hashes:
    if h in local_hits:
        upsert_project_db(conn, h, local_hits[h])
        continue
    result = ollama_analyze(sym, ...)
    upsert_local_cache(local_cache, h, result)
    upsert_project_db(conn, h, result)
    if can_write:
        pending_put.append({"hash": h, "summary": ..., "inputs": ..., "outputs": ..., "model": ...})
        if len(pending_put) >= BATCH_SIZE:
            cache_client.batch_put(pending_put)  # force=False → ON CONFLICT DO NOTHING
            pending_put.clear()
if pending_put:
    cache_client.batch_put(pending_put)
```

### 6. Konfigurace

```toml
# Dev: .fw-context/local.toml

[cache_server]
url = "https://fw-cache.example.com"
token = "${FW_CACHE_TOKEN}"          # read+write nebo read-only token
# batch_size = 100                   # počet hashů v jednom requestu (výchozí)
# force = false                      # true → X-Cache-Overwrite header (vyžaduje can_overwrite)
```

```toml
# Server: /etc/fw-cache-server/config.toml

[cache_server]
host = "0.0.0.0"
port = 8000
# Jeden connection string — server z něj odvodí obě DB připojení
# (fw_cache_meta a fw_cache) přidáním /databaze
db_url = "postgresql://fw_cache:bezpecne_heslo@localhost:5432"

[logging]
level = "info"
```

### 7. CLI rozhraní

```bash
# Server (na serveru)
fw-cache-server init                  # Inicializace — meta DB + admin token
fw-cache-server run                   # Spuštění serveru (foreground)
fw-cache-server install-systemd       # Vygenerovat systemd unit
fw-cache-server install-launchd       # Vygenerovat launchd plist

# Admin (na serveru, s admin tokenem)
fw-cache-admin project create <id>    # Vytvořit projekt + tokeny (cache je globální)
fw-cache-admin project remove <id>    # Smazat projekt + tokeny (cache zůstává)
fw-cache-admin project list
fw-cache-admin token create <project> [--readonly] [--write] [--overwrite] [--description ...]
fw-cache-admin token revoke <token>
fw-cache-admin token list <project>
fw-cache-admin cache stats           # Statistiky centrální cache
fw-cache-admin cache purge --older-than 90d  # Vyčistit staré záznamy (podle analyzed_at)

# Dev (lokálně)
fw-context index --analyze           # Použije [cache_server] z configu
fw-context index --analyze --force   # Přepíše existující analýzy (vyžaduje can_overwrite)
fw-context cache stats               # Statistiky lokální cache
fw-context cache clear               # Smazat ~/.fw-context/llm_cache.db
```

## Co se nemění

- **SQLite zůstává** — každý dev má svůj lokální `index.db` pro parsování, reference, FTS5
- **Parsování, reference, embeddingy** — vše lokální, rychlé (minuty)
- **FTS5, vec0** — lokální, žádná změna
- **MCP server** — lokální, stdio, žádná změna
- **Offline režim** — bez cache serveru to funguje jako dřív (pomalejší, ale funkční)

## Co se mění v lokální cache

- **Lokální `llm_analysis_cache` se přesouvá z per-project `index.db` do
  `~/.fw-context/llm_cache.db`** — globální SQLite pro všechny lokální projekty
- Obsahový hash je univerzální: analyzovaný `mbed::sleep` v projektu A se
  automaticky použije i pro projekt B (na stejném stroji, bez centrálního serveru)
- `CacheClient` synchronizuje mezi lokální cache ↔ centrální cache
  (obousměrně: co se načte ze serveru, uloží se lokálně; co se vygeneruje
  lokálně, pošle se na server)

Flow lookupu:

```
compute_content_hash(symbol)
  → lokální ~/.fw-context/llm_cache.db  (SQLite, lokální, rychlé)
  → centrální fw_cache                  (POST /cache/batch, volitelné)
  → Ollama (jen když nikde není hit)
```

Tím pádem:
- Dev bez GPU = cache hit z lokálního SQLite pro symboly které už dřív
  analyzoval (i v jiném projektu)
- Dev s GPU = cache hit z lokálního SQLite, zbytek Ollama → uložení do
  lokálního SQLite + PUT na server
- Bez centrálního serveru = pořád benefit z lokální cross-project cache

### Migrace existujících cache dat

Při prvním spuštění nové verze s aktivovaným `[cache_server]` proběhne
jednorázová transparentní migrace:

1. `_build_llm_analysis` (nebo `CacheClient.init`) projde všechny existující
   `index.db` soubory v `db_dir`
2. Z každého zkopíruje `llm_analysis_cache` řádky do `~/.fw-context/llm_cache.db`
   (deduplikace podle `content_hash` — `INSERT OR IGNORE`)
3. Po migraci se stará `llm_analysis_cache` tabulka v `index.db` ignoruje
   (při příštím bumpu schema verze se dropne)
4. Dev nemusí dělat nic — migrace je plně automatická

**Centrální cache bootstrap:**
- Admin (první GPU dev s `can_overwrite`) spustí `fw-context index --analyze`
  → poprvé naplní centrální cache
- Ostatní s write tokenem doplňují nové symboly průběžně (bez přepisu)
- Po upgradu modelu: `fw-context index --analyze --force` → pošle
  `X-Cache-Overwrite` → přepíše staré analýzy novým modelem

## Konfigurace — rozšíření

### `[cache_server]` v `Config` dataclassu

```python
@dataclass
class CacheServerConfig:
    """Optional centralized LLM analysis cache server."""
    url: str = ""                       # "https://fw-cache.example.com"
    token: str = ""                     # Bearer token (z env)
    batch_size: int = 100               # počet hashů v jednom requestu
    force: bool = False                 # true → X-Cache-Overwrite (vyžaduje can_overwrite)
```

Přidáno do `Config.cache_server: CacheServerConfig | None`. `_from_dict()`
parsuje `[cache_server]` sekci z TOML. Když `url` není prázdné, `CacheClient`
se vytvoří v `run()` a předá do `_build_llm_analysis`.

### Závislosti v `pyproject.toml`

```toml
[project.optional-dependencies]
cache-server = ["fastapi>=0.110", "uvicorn[standard]>=0.27", "asyncpg>=0.29"]
```

`fw-cache-server setup` kontroluje že jsou nainstalované.

## Plán implementace

### Fáze 1: Server — instalace, deployment, provoz (~4 dny)

1. **Nový modul `cache_server/`** — adresářová struktura
2. **Databázové schéma** — `fw_cache_meta` (projekty, tokeny vč. `can_overwrite`) + `fw_cache` (globální cache vč. `analyzed_at`)
3. **`Backend`** — asyncpg connection pool (dva pooly: meta + cache), batch get/put, `INSERT ON CONFLICT DO NOTHING` / `DO UPDATE` podle `can_overwrite`
4. **`Auth` middleware** — token validation (SHA256 hash lookup), permission check (`can_read`, `can_write`, `can_overwrite`), `X-Cache-Overwrite` header
5. **FastAPI app** — endpointy `/cache/batch` (POST/GET, PUT), `/health`, HTTP status codes
6. **`fw-cache-server` CLI** — `init`, `run`, `install-systemd`, `install-launchd`
7. **`fw-cache-admin` CLI** — project a token management (`--write`, `--overwrite` flagy)
8. **`fw-cache-server setup`** — interaktivní průvodce:
   - Detekce OS, PostgreSQL instalace+konfigurace
   - `init` (vytvoření tabulek, admin token)
   - Vytvoření projektu a tokenů
   - systemd/launchd unit
   - nginx HTTPS reverse proxy + Let's Encrypt certbot
   - Firewall, logrotate, systemd hardening
9. **`nginx_config.py`** — detekce stavu nginx, generování configu, správa certbot certifikátů (nový / rozšíření / přeskočení)
10. **Závislosti** — `[project.optional-dependencies] cache-server` v `pyproject.toml`
11. **Testy** — unit testy na auth, backend; integrační test na API (TestClient, PostgreSQL v Dockeru)

### Fáze 2: Klient — integrace do fw-context-mcp (~2–3 dny)

12. **`CacheServerConfig`** dataclass + `[cache_server]` v TOML configu
13. **`CacheClient`** — HTTP klient, batch get/put s `--force` flagem, retry logika (3 pokusy s backoff), graceful offline fallback
14. **Lokální globální cache** — přesun `llm_analysis_cache` z per-project `index.db` do `~/.fw-context/llm_cache.db` + auto-migrace existujících dat
15. **Integrace do `runner.py`** — `run()` vytvoří `CacheClient` z `Config.cache_server` a předá do `_build_llm_analysis`; lookup flow: lokální cache → centrální cache → Ollama → zápis do obou
16. **CLI** — `fw-context cache stats`, `fw-context cache clear`, `fw-context index --analyze --force`
17. **Testy** — unit testy na `CacheClient`, integrační test s mock serverem

**Celkový odhad: 6–8 dní**

## Otevřené otázky

1. **Je to dostatečné?** Cache server řeší jen LLM analýzu — ne sdílení
   embeddingů nebo referencí. Embeddingy se počítají rychle (batch po 100,
   jeden Ollama call). Reference se počítají lokálně při indexování.

2. **Cache eviction?** `llm_analysis_cache` roste s každým unikátním
   symbolem. Po roce to může být pár set MB. Limitovat LRU? Nebo neřešit
   (disk je levný)?

3. **Cache warming přes CI?** GPU dev by měl po každé změně kódu spustit
   `fw-context index --analyze` aby naplnil cache pro ostatní. Automatizovat
   přes CI/CD pipeline?

4. **Race condition a multi-writer?** Dva writeři zapíšou stejný hash
   současně → `INSERT ON CONFLICT DO NOTHING` — první vyhrává, druhý se
   tiše ignoruje. Více writerů je bezpečné díky content-addressable
   cache. **Horší model nemůže přepsat lepší** — přepis je možný jen
   s `X-Cache-Overwrite` hlavičkou a `can_overwrite=true`, což drží
   typicky jeden admin s nejlepším modelem. Žádný číselník modelů není
   potřeba — stačí tenhle jednoduchý conflict-resolution mechanismus.

5. **Latence batch requestů?** Při 5000 symbolech a 100/batch je to 50 HTTP
   requestů. Při 50ms RTT ~2.5s overhead. Keep-alive + HTTP/2 to ještě
   sníží. Přijatelné.

6. **Závislost na `asyncpg`?** Přidává native závislost. Alternativa:
   `psycopg` (čistý Python, pomalejší). Pro jednoduché SELECT/INSERT stačí.

---

_Vytvořeno: 2025-07-02_
