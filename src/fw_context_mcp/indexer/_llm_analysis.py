"""LLM analysis batch processing extracted from runner.py.

WHY separate module: the LLM analysis loop is complex (hash-based caching,
two-tier cache lookup, body truncation, context-window budgeting, sentinel
management).  Extracting it from runner.py keeps the runner focused on the
parse→store pipeline and makes the analysis logic testable in isolation.

Handles reading symbol bodies, fetching callees/referencers,
enriching batches, and the main LLM analysis build phase.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import nullcontext
from pathlib import Path

from ..cache_client import get_local_cache_db, local_cache_lookup, local_cache_upsert
from ..llm.ollama import call_ollama
from ..utils import SAFE_EXCEPT, is_fatal, read_file_lines
from ._embedding import _fmt_dur
from .db import transaction, upsert_llm_analysis_batch, write_lock

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


def _enrich_batch(conn, batch_rows, config_hash: str, *, project_root: Path | None = None) -> list[dict]:
    """Augment symbol rows with ``body`` and ``callees`` keys.

    Reads function/method bodies from disk and fetches callee names
    from the reference index.  Failures are non-fatal — missing body
    or callees are left as empty strings/lists.
    """
    from ..utils import abs_path as resolve_abs_path

    log = logging.getLogger(__name__)
    enriched: list[dict] = []
    for r in batch_rows:
        d = dict(r)
        body = ""
        callees: list[str] = []

        kind = d.get("kind", "")
        file_path = d.get("file_path", "")
        start_line = d.get("line", 0)
        end_line = d.get("end_line", 0)
        usr = d.get("usr", "")

        abs_file_path = resolve_abs_path(project_root, file_path) if project_root else file_path

        # Read body for symbols with meaningful extents.  Enums and typedefs
        # benefit from body text during LLM analysis (enum constants, type alias).
        if (
            kind in ("function", "method", "constructor", "destructor", "class", "struct", "union", "enum", "typedef")
            and abs_file_path
            and end_line > start_line
        ):
            if not os.path.exists(abs_file_path):
                log.warning(
                    "[%s] body not available for %s — file missing: %s",
                    kind, d.get("qualified_name", "?"), abs_file_path,
                )
            else:
                body = _read_body(abs_file_path, start_line, end_line)
                if not body and start_line > 0:
                    log.error(
                        "[%s] empty body for %s at %s:%d-%d — "
                        "path resolution or extent mismatch",
                        kind, d.get("qualified_name", "?"), abs_file_path, start_line, end_line,
                    )

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

def _resolve_model_context_size(llm_config) -> int:
    """Resolve the model context window size from the Ollama API.

    Falls back to ``llm_config.num_ctx`` when the API is unreachable.
    """
    import httpx

    try:
        resp = httpx.get(llm_config.ollama_url.rstrip("/") + "/api/tags", timeout=5.0)
        resp.raise_for_status()
        for m in resp.json().get("models", []):
            if m.get("name") == llm_config.model:
                ctx = m.get("details", {}).get("context_length", 0)
                if ctx > 0:
                    log.debug("Resolved model context from Ollama: %d tokens", ctx)
                    return ctx
                break
    except SAFE_EXCEPT as e:
        if is_fatal(e):
            raise
        log.warning("Ollama not reachable — skipping LLM analysis generation")
        return 0
    return llm_config.num_ctx


def _clear_skip_sentinels(
    conn,
    model: str,
    model_ctx_size: int,
    retry_unparseable: bool,
) -> None:
    """Remove skip sentinels that are no longer applicable.

    - ``skip:toolarge:`` sentinels whose recorded context size is smaller
      than the current model's context window are cleared (the symbol may
      now fit).
    - ``skip:unparseable:`` sentinels are cleared when *retry_unparseable*
      is True, or when the stored model name doesn't match *model*.
    """
    conn.execute(
        """DELETE FROM llm_analysis
           WHERE model LIKE 'skip:toolarge:%'
             AND CAST(SUBSTR(model, 15) AS INTEGER) < ?""",
        (model_ctx_size,),
    )
    if retry_unparseable:
        conn.execute("DELETE FROM llm_analysis WHERE model LIKE 'skip:unparseable:%'")
    else:
        conn.execute(
            """DELETE FROM llm_analysis
               WHERE model LIKE 'skip:unparseable:%'
                 AND SUBSTR(model, 18) != ?""",
            (model,),
        )


def _select_unanalyzed_symbols(
    conn,
    config_hash: str,
    project_only: bool,
) -> list:
    """Return definition symbols that may need LLM analysis.

    LEFT JOINs ``llm_analysis`` so the caller can compare the
    content-addressable hash: if ``existing_hash`` matches the
    freshly-computed hash, the analysis is still current and the
    symbol can be skipped.  Symbols without any analysis have
    ``existing_hash = NULL`` and always proceed to cache lookup.
    """
    is_project_clause = "AND s.is_project = 1" if project_only else ""
    query = f"""SELECT s.id, s.name, s.qualified_name, s.kind, s.file_path,
                       s.signature, s.is_definition, s.docstring,
                       s.end_line, s.line, s.usr, s.source,
                       f.path as file_path,
                       a.content_hash as existing_hash
                FROM symbols s
                JOIN files f ON s.file_id = f.id
                LEFT JOIN llm_analysis a ON a.symbol_id = s.id
                WHERE s.config_hash = ?
                  AND s.is_definition = 1
                  AND s.kind IN ('function', 'method', 'constructor', 'destructor',
                                 'class', 'struct', 'union', 'typedef', 'enum', 'varglobal')
                  AND s.name NOT LIKE '%(anonymous%'
                  AND s.name NOT LIKE '%(unnamed%'
                  {is_project_clause}
                ORDER BY s.kind, s.file_path, s.line"""
    with transaction(conn, checkpoint=False):
        return conn.execute(query, (config_hash,)).fetchall()


def _build_llm_analysis(
    conn,
    config_hash: str,
    llm_config,
    db_dir: Path,
    *,
    project_only: bool = True,
    project_root: Path | None = None,
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
    from ..indexer.prompts import build_analysis_prompt, parse_analysis_response
    from ..utils import compute_content_hash

    # Suppress httpx INFO logs (one per symbol — noisy during analysis)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Check Ollama reachability and resolve model context size.
    _model_ctx_size = _resolve_model_context_size(llm_config)
    if _model_ctx_size == 0:
        return

    model = llm_config.model

    # Clear skip sentinels that are no longer applicable
    _clear_skip_sentinels(conn, model, _model_ctx_size, retry_unparseable)

    # Select symbols needing analysis
    rows = _select_unanalyzed_symbols(conn, config_hash, project_only)
    if not rows:
        log.info("All project symbols already analyzed — nothing to do")
        return

    model = llm_config.model
    total_symbols = len(rows)
    total = 0

    log.info("LLM analysis: %d symbols (model=%s)", total_symbols, model)


    local_db = get_local_cache_db()  # single connection for all symbols

    for idx, row in enumerate(rows):
        t0 = time.monotonic()
        qname = row["qualified_name"] or row["name"]
        try:
            # ── Step 0: project DB hash check — skip unchanged symbols
            #    without disk I/O or callee fetch.  Uses the ``source``
            #    column stored during indexing (same body text as
            #    _enrich_batch would read from disk).
            existing_hash = row.get("existing_hash")
            if existing_hash:
                source_body = row["source"] or ""
                if not source_body:
                    # source column is empty (legacy symbol from before
                    # the 'source' column was added).  Fall back to
                    # reading the body from disk for the hash comparison.
                    from ..utils import abs_path as _resolve_abs_path

                    file_path = row.get("file_path", "")
                    abs_file_path = _resolve_abs_path(project_root, file_path) if project_root else file_path
                    if abs_file_path:
                        source_body = _read_body(abs_file_path, row["line"], row["end_line"])
                h = compute_content_hash(
                    source_body,
                    row["qualified_name"] or "",
                    row["signature"] or "",
                    row["docstring"] or "",
                )
                if existing_hash == h:
                    total += 1
                    elapsed = time.monotonic() - t0
                    log.debug("[%d/%d] %s: hash-matched %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed))
                    continue

            batch_dicts = _enrich_batch(conn, [row], config_hash, project_root=project_root)
            d = batch_dicts[0]

            # ── Cache check — 2 tiers: local global → remote ──
            h = compute_content_hash(d["body"], d["qualified_name"], d["signature"], d["docstring"])

            # Catch edge cases where source-H ≠ body-H (e.g. varglobal
            # with multi-line initializer — source column captured the
            # body but _enrich_batch left it empty).
            if existing_hash and existing_hash == h:
                total += 1
                elapsed = time.monotonic() - t0
                log.debug("[%d/%d] %s: hash-matched %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed))
                continue

            # Tier 1: local global cache (~/.fw-context/llm_cache.db)
            cached = None
            try:
                local_hits = local_cache_lookup(local_db, [h])
                cached = local_hits.get(h)
            except SAFE_EXCEPT as e:
                if is_fatal(e):
                    raise
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
                        except SAFE_EXCEPT as e:
                            if is_fatal(e):
                                raise
                            log.debug("Local global cache write failed: %s", e)
                    else:
                        log.debug("Remote cache miss for %s (hash=%s…)", qname, h[:12])
                except SAFE_EXCEPT as e:
                    if is_fatal(e):
                        raise
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
            except SAFE_EXCEPT as e:
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
                    except SAFE_EXCEPT as e:
                        if is_fatal(e):
                            raise
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
                        except SAFE_EXCEPT as e:
                            if is_fatal(e):
                                raise
                            log.debug("Remote cache write failed: %s", e)
                    total += inserted

            elapsed = time.monotonic() - t0
            log.info("[%d/%d] %s: ok %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed))
            log.debug("  summary: %s", r["summary"])
            log.debug("  inputs : %s", r["inputs"])
            log.debug("  outputs: %s", r["outputs"])
        except SAFE_EXCEPT as e:
            elapsed = time.monotonic() - t0
            log.warning("[%d/%d] %s: err %s: %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed), e)
            continue

    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        log.info("LLM analysis stored: %d/%d symbols (model=%s)", total, total_symbols, model)
    except SAFE_EXCEPT as e:
        if is_fatal(e):
            raise
        pass  # non-fatal — continue
    finally:
        local_db.close()

