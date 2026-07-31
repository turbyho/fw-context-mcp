"""LLM analysis batch processing extracted from runner.py.

Handles reading symbol bodies, fetching callees/referencers,
enriching batches, and the main LLM analysis build phase.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import nullcontext
from pathlib import Path


from ..cache_client import get_local_cache_db, local_cache_lookup, local_cache_upsert
from ..llm.ollama import call_ollama
from ..utils import read_file_lines
from ..config.settings import DESCRIPTION_VERSION
from ._embedding import _chunk_body, _fmt_dur
from .db import open_db, transaction, upsert_llm_analysis_batch

log = logging.getLogger(__name__)


def _read_body(abs_path: str, start_line: int, end_line: int) -> str:
    """Read a function body from a source file using line numbers.

    Returns the body text or an empty string on any error (missing file, bad range).
    """
    lines = read_file_lines(abs_path)
    if lines is None:
        return ""
    if 0 < start_line <= end_line <= len(lines):
        return "".join(lines[start_line - 1 : end_line])
    return ""


def _fetch_callees(conn, symbol_usr: str, config_hash: str) -> list[str]:
    """Return the qualified names of functions called by *symbol_usr*."""
    rows = conn.execute(
        """SELECT DISTINCT s.qualified_name
           FROM refs r
           JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = ?
           WHERE r.from_usr = ?
             AND r.ref_kind = 'call'
             AND r.config_hash = ?
             AND s.qualified_name != ''
           ORDER BY s.qualified_name
           LIMIT 35""",
        (config_hash, symbol_usr, config_hash),
    ).fetchall()
    return [r[0] for r in rows]


def _fetch_referencers(conn, symbol_usr: str, config_hash: str) -> list[str]:
    """Return qualified names of functions that reference (read/write) *symbol_usr*."""
    rows = conn.execute(
        """SELECT DISTINCT s.qualified_name
           FROM refs r
           JOIN symbols s ON s.usr = r.from_usr AND s.config_hash = ?
           WHERE r.to_usr = ?
             AND r.config_hash = ?
             AND s.qualified_name != ''
           ORDER BY s.qualified_name
           LIMIT 35""",
        (config_hash, symbol_usr, config_hash),
    ).fetchall()
    return [r[0] for r in rows]


def _enrich_batch(conn, batch_rows, config_hash: str) -> list[dict]:
    """Augment symbol rows with ``body`` and ``callees`` keys.

    Reads function/method bodies from disk and fetches callee names
    from the reference index.  Failures are non-fatal — missing body
    or callees are left as empty strings/lists.
    """
    enriched: list[dict] = []
    for r in batch_rows:
        d = dict(r)
        body = ""
        callees: list[str] = []

        kind = d.get("kind", "")
        abs_path = d.get("abs_path", "")
        start_line = d.get("line", 0)
        end_line = d.get("end_line", 0)
        usr = d.get("usr", "")

        # Read body for symbols with meaningful extents.  Enums and typedefs
        # benefit from body text during LLM analysis (enum constants, type alias).
        if (
            kind in ("function", "method", "constructor", "destructor", "class", "struct", "union", "enum", "typedef")
            and abs_path
            and end_line > start_line
        ):
            body = _read_body(abs_path, start_line, end_line)

        # Fetch callees / referencers from the reference index
        if usr:
            if kind == "varglobal":
                callees = _fetch_referencers(conn, usr, config_hash)
            elif kind in ("varlocal", "variable", "field"):
                callees = []
            else:
                callees = _fetch_callees(conn, usr, config_hash)

        d["body"] = body
        d["callees"] = callees
        enriched.append(d)
    return enriched


def _build_llm_analysis(
    conn,
    config_hash: str,
    llm_config,
    db_dir: Path,
    *,
    project_only: bool = True,
    write_lock_held: bool = False,
    cache_client=None,
    retry_unparseable: bool = False,
) -> None:
    """Generate structured LLM analysis (summary, inputs, outputs) for each
    project-definition symbol using Ollama, one symbol per request.

    Processes symbols individually — one Ollama request per symbol — for
    maximum isolation and retry-ability.  NOTE(turbyho, 2026-07-31): Ollama batching API would
    reduce ~10K symbols from ~10K HTTP round trips to O(batches).
    reliable format adherence. Only project symbols (non-SDK) are analyzed.

    The prompt includes the full function body (read from disk via exact
    libclang extents) and callee names (from the reference index), which
    dramatically improves description quality.

    *db_dir* is the directory containing the index database — used for the
    write lock that serializes DB access across processes.
    *project_only* when True (default) filters to symbols where
    ``s.is_project = 1`` (project code).  When False, all symbols
    including vendor/SDK are analyzed.  Uses the ``is_project`` column
    which is computed during indexing from vendor/project path patterns.
    *retry_unparseable* when True clears all ``skip:unparseable`` sentinels
    so previously-failed symbols are re-attempted. Set True for manual
    indexing, False for background reindex (safe: retries only on model change).
    """
    import httpx

    from ..indexer.prompts import build_analysis_prompt, parse_analysis_response
    from ..utils import compute_content_hash
    from .db import open_db, transaction, upsert_llm_analysis_batch

    # Suppress httpx INFO logs (one per symbol — noisy during analysis)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Check Ollama reachability and resolve model context size.
    # /api/tags response includes details.context_length per model —
    # authoritative for both local and cloud-proxied models.
    _model_ctx_size = llm_config.num_ctx
    try:
        resp = httpx.get(llm_config.ollama_url.rstrip("/") + "/api/tags", timeout=5.0)
        resp.raise_for_status()
        for m in resp.json().get("models", []):
            if m.get("name") == llm_config.model:
                ctx = m.get("details", {}).get("context_length", 0)
                if ctx > 0:
                    _model_ctx_size = ctx
                    log.debug("Resolved model context from Ollama: %d tokens", ctx)
                break
    except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error):
        log.warning("Ollama not reachable — skipping LLM analysis generation")
        return

    model = llm_config.model

    # Un-skip symbols that were previously skipped because of a smaller
    # context window.  When the user switches to a model with a larger
    # context, those symbols may now fit and should be re-attempted.
    conn.execute(
        """DELETE FROM llm_analysis
           WHERE model LIKE 'skip:toolarge:%'
             AND CAST(SUBSTR(model, 15) AS INTEGER) < ?""",
        (_model_ctx_size,),
    )

    # Un-skip symbols that were skipped due to unparseable output.
    # The sentinel is "skip:unparseable:<model>".
    # Background reindex keeps sentinels (retry only on model change);
    # manual fw-context index / reindex_file clears all sentinels so
    # the fixed parser/LLM gets another chance.
    if retry_unparseable:
        conn.execute("DELETE FROM llm_analysis WHERE model LIKE 'skip:unparseable:%'")
    else:
        conn.execute(
            """DELETE FROM llm_analysis
               WHERE model LIKE 'skip:unparseable:%'
                 AND SUBSTR(model, 18) != ?""",
            (model,),
        )

    if not project_only:
        is_project_clause = ""
    else:
        is_project_clause = "AND s.is_project = 1"

    query = """SELECT s.id, s.name, s.qualified_name, s.kind, s.file_path,
                      s.signature, s.is_definition, s.docstring,
                      s.end_line, s.line, s.usr,
                      f.path as abs_path
               FROM symbols s
               JOIN files f ON s.file_id = f.id
               WHERE s.config_hash = ?
                 AND s.is_definition = 1
                  AND s.kind IN ('function', 'method', 'constructor', 'destructor',
                                  'class', 'struct', 'union', 'typedef', 'enum', 'varglobal')
                 AND s.name NOT LIKE '%(anonymous%'
                 AND s.name NOT LIKE '%(unnamed%'
                 """
    if is_project_clause:
        query += f" {is_project_clause}"
    query += """ AND s.id NOT IN (SELECT symbol_id FROM llm_analysis)
               ORDER BY s.kind, s.file_path, s.line"""

    with transaction(conn, checkpoint=False):
        rows = conn.execute(query, (config_hash,)).fetchall()
        if not rows:
            log.info("All project symbols already analyzed — nothing to do")
            return

    model = llm_config.model
    total_symbols = len(rows)
    total = 0

    log.info("LLM analysis: %d symbols (model=%s)", total_symbols, model)

    from ..cache_client import get_local_cache_db, local_cache_lookup, local_cache_upsert

    local_db = get_local_cache_db()  # single connection for all symbols

    for idx, row in enumerate(rows):
        t0 = time.monotonic()
        qname = row["qualified_name"] or row["name"]
        try:
            batch_dicts = _enrich_batch(conn, [row], config_hash)
            d = batch_dicts[0]

            # ── Cache check — 2 tiers: local global → remote ──
            h = compute_content_hash(d["body"], d["qualified_name"], d["signature"], d["docstring"])

            # Tier 1: local global cache (~/.fw-context/llm_cache.db)
            cached = None
            try:
                local_hits = local_cache_lookup(local_db, [h])
                cached = local_hits.get(h)
            except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error) as e:
                log.debug("Local global cache lookup failed: %s", e)

            # Tier 2: remote cache server
            if not cached and cache_client is not None:
                try:
                    remote_hits = cache_client.batch_get([h])
                    cached = remote_hits.get(h)
                    if cached:
                        # Store in local global cache for next time
                        try:
                            local_cache_upsert(local_db, [{"hash": h, **cached}])
                        except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error) as e:
                            log.debug("Local global cache write failed: %s", e)
                    else:
                        log.debug("Remote cache miss for %s (hash=%s…)", qname, h[:12])
                except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error) as e:
                    log.debug("Remote cache lookup failed for %s: %s", qname, e)

            if cached:
                # Cache hit — re-use existing analysis
                with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
                    with transaction(conn, checkpoint=False):
                        upsert_llm_analysis_batch(
                            conn,
                            [
                                (
                                    d["id"],
                                    cached["summary"],
                                    cached["inputs"],
                                    cached["outputs"],
                                    cached["model"],
                                    h,
                                )
                            ],
                        )
                total += 1
                elapsed = time.monotonic() - t0
                log.info("[%d/%d] %s: ok %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed))
                continue

            # Cache miss — build prompt, check context fit, then call Ollama
            prompt = build_analysis_prompt(batch_dicts)

            # If the symbol body is very large (>5000 chars), truncate it
            # regardless of context budget.  Models struggle to produce
            # structured JSON when the prompt contains hundreds of lines
            # of C++ declarations — they default to reproducing field lists
            # instead of summarising.  Keeping ~60 lines gives enough
            # structure for a useful analysis without overwhelming the model.
            body = d.get("body", "")
            if body and len(body) > 5000:
                body_lines = body.split("\n")
                truncated = "\n".join(body_lines[:60])
                if len(truncated) > 9000:  # safety: raw cutoff if 60 lines is still huge
                    truncated = body[:9000]
                truncated += f"\n// ... ({len(body)} total chars, {len(body_lines)} lines — truncated for analysis)\n"
                d["body"] = truncated
                prompt = build_analysis_prompt(batch_dicts)
                _est_prompt_tokens = len(prompt) / 2.5  # conservative for C++ code
                log.debug(
                    "[%d/%d] %s: body truncated %d → %d chars, prompt %d chars",
                    idx + 1,
                    total_symbols,
                    qname,
                    len(body),
                    len(truncated),
                    len(prompt),
                )

            # Compute response budget — how many tokens remain after the prompt
            # inside the model context window.
            _est_prompt_tokens = len(prompt) / 2.5  # conservative for C++ code
            _safety_margin = 300
            _ctx_size = _model_ctx_size
            num_predict = max(500, int(_ctx_size - _est_prompt_tokens - _safety_margin))

            # If the prompt STILL doesn't fit after truncation, skip.
            # The model needs at least 300 tokens of response space.
            if _est_prompt_tokens + 300 + _safety_margin > _ctx_size:
                with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
                    with transaction(conn, checkpoint=False):
                        upsert_llm_analysis_batch(conn, [(d["id"], "", "", "", f"skip:toolarge:{_model_ctx_size}", h)])
                total += 1
                elapsed = time.monotonic() - t0
                log.warning(
                    "[%d/%d] %s: skip %s (body too large even after trunc, prompt=%d chars, ctx=%d tokens)",
                    idx + 1,
                    total_symbols,
                    qname,
                    _fmt_dur(elapsed),
                    len(prompt),
                    _ctx_size,
                )
                continue

            try:
                response = call_ollama(prompt, llm_config, temperature=0.1, num_predict=num_predict)
            except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error) as e:
                elapsed = time.monotonic() - t0
                log.warning("[%d/%d] %s: err %s: %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed), e)
                continue

            parsed = parse_analysis_response(response, batch_dicts)
            if not parsed:
                # Store sentinel in the per-build analysis table only —
                # NOT in the content-addressable cache.  If we store it in
                # the cache, the next run with a DIFFERENT prompt (e.g.
                # after an Ollama model upgrade or prompt fix) will hit
                # the cache and silently reuse the sentinel, skipping the
                # re-analysis.  The sentinel protects the CURRENT build
                # from infinite retries; the cache protects across builds
                # and should only hold successful analyses.
                total += 1
                with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
                    with transaction(conn, checkpoint=False):
                        sentinel = f"skip:unparseable:{model}"
                        upsert_llm_analysis_batch(conn, [(d["id"], "", "", "", sentinel, h)])
                elapsed = time.monotonic() - t0
                log.warning(
                    "[%d/%d] %s: err %s: unparseable response", idx + 1, total_symbols, qname, _fmt_dur(elapsed)
                )
                continue

            r = parsed[0]
            with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
                with transaction(conn, checkpoint=False):
                    db_rows = [(r["symbol_id"], r["summary"], r["inputs"], r["outputs"], model, h)]
                    inserted = upsert_llm_analysis_batch(conn, db_rows)
                    # Store in local global cache
                    try:
                        local_cache_upsert(
                            local_db,
                            [
                                {
                                    "hash": h,
                                    "summary": r["summary"],
                                    "inputs": r["inputs"],
                                    "outputs": r["outputs"],
                                    "model": model,
                                }
                            ],
                        )
                    except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error) as e:
                        log.debug("Local global cache write failed: %s", e)
                    # Store on remote cache server (fire-and-forget)
                    if cache_client is not None:
                        try:
                            cache_client.batch_put(
                                [
                                    {
                                        "hash": h,
                                        "summary": r["summary"],
                                        "inputs": r["inputs"],
                                        "outputs": r["outputs"],
                                        "model": model,
                                    }
                                ]
                            )
                        except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error) as e:
                            log.debug("Remote cache write failed: %s", e)
                    total += inserted

            elapsed = time.monotonic() - t0
            log.info("[%d/%d] %s: ok %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed))
            log.debug("  summary: %s", r["summary"])
            log.debug("  inputs : %s", r["inputs"])
            log.debug("  outputs: %s", r["outputs"])
        except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error) as e:
            elapsed = time.monotonic() - t0
            log.warning("[%d/%d] %s: err %s: %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed), e)
            continue

    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error):
        pass  # libclang/SQLite fallback

    log.info("LLM analysis stored: %d/%d symbols (model=%s)", total, total_symbols, model)
    local_db.close()

