"""Shared LLM analysis cache server — FastAPI app, auth, PostgreSQL backend.

The cache server enables *multiple* ``fw-context-mcp`` installations to
share LLM-generated symbol analyses.  Without it, each developer who
runs ``fw-context index --analyze`` independently generates the same
LLM analysis for the same symbols — wasted compute and latency.

Architecture
------------
* **Cache key** — ``SHA‑256(symbol_body || qualified_name || signature || docstring)``.
  The key is content-addressed (same symbol → same hash across all projects),
  so analyses are *global* — two projects indexing the same SDK symbol share
  the same cached result.
* **Meta database** (``fw_cache_meta``) — projects, bearer tokens, permissions.
* **Cache database** (``fw_cache``) — ``llm_analysis_cache`` table (global,
  no project scoping).
* **Auth model** — bearer tokens with ``can_read``, ``can_write``,
  ``can_overwrite`` permissions.  Read-only tokens are the default for
  developers — they can consume cached analyses but cannot write.
* **Write semantics** — ``INSERT ON CONFLICT DO NOTHING`` by default
  (first write wins).  ``X-Cache-Overwrite: true`` header (with
  ``can_overwrite`` permission) enables overwrite for re-analysis.

Why not a local SQLite-only cache?
----------------------------------
A local cache is also present (see ``cache_client.py`` — local SQLite
for offline first-tier lookups), but a **shared server** solves the
cold-start problem: the first developer who indexes a new SDK version
populates the shared cache; all others get instant hits.  Without shared
caching, every CI run and every developer workstation independently
calls the LLM for the same 10,000+ symbols.

Packages
--------
* ``cache_server.admin`` — CLI admin tool (``fw-cache-admin``)
* ``cache_server.app`` — FastAPI application
* ``cache_server.auth`` — bearer-token middleware with rate limiting
* ``cache_server.backend`` — PostgreSQL storage (``asyncpg``)
* ``cache_server.cli`` — server CLI (``fw-cache-server``)
* ``cache_server.install`` — systemd / launchd unit generators
* ``cache_server.nginx_config`` — nginx reverse-proxy config helper
* ``cache_server.setup`` — interactive installation wizard
"""
