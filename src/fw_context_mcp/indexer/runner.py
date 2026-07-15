"""Index runner: parse compile_commands.json, extract symbols, store to SQLite.

Uses ``indexer/ops.py`` for the shared "parse TU → store symbols" loop so
that runner, reindex_file, and auto-reindex all use the same code path.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from ..config.settings import derive_project_id
from ..llm.ollama import call_ollama
from ..utils import MTIME_TOLERANCE_S, compute_source_hash, read_file_lines
from .compile_commands import _SOURCE_EXTS, validate_include_files
from .compile_commands import parse as parse_compile_commands
from .config_hash import compute_flags_hash, compute_tu_content_hash
from .db import (
    CURRENT_SCHEMA_VERSION,
    drop_fts_triggers,
    get_file_hashes,
    open_db,
    rebuild_files_fts,
    rebuild_fts,
    transaction,
    upsert_build_config,
    upsert_project,
    write_lock,
)
from .ops import _build_filtered_file_content, _normalize_file_path, store_symbols_for_unit

log = logging.getLogger(__name__)


def _fmt_dur(seconds: float) -> str:
    """Human-readable duration: ``87ms``, ``1.2s``, ``12s``."""
    if seconds < 0.1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


_COMMON_SOURCE_DIRS = ["src", "lib", "app", "include", "drivers", "modules"]
_COMMON_OS_DIRS = ["zephyr", "mbed-os"]


def _detect_source_roots(project_root: Path, compile_commands: Path) -> list[Path]:
    """Auto-detect source directories from project structure and compile_commands.json."""
    roots: list[Path] = []
    seen: set[Path] = set()

    for name in _COMMON_SOURCE_DIRS:
        p = project_root / name
        if p.is_dir() and p not in seen:
            roots.append(p)
            seen.add(p)
    for name in _COMMON_OS_DIRS:
        p = project_root / name
        if p.is_dir() and p not in seen:
            roots.append(p)
            seen.add(p)
    try:
        units = list(parse_compile_commands(compile_commands))
        for unit in units:
            resolved = unit.file.resolve()
            try:
                rel = resolved.relative_to(project_root)
                top = project_root / rel.parts[0]
                if top.is_dir() and top not in seen:
                    roots.append(top)
                    seen.add(top)
            except ValueError:
                # TU is outside the project root (e.g. PlatformIO packages,
                # ESP-IDF framework, Zephyr modules). Walk up to find a
                # sensible framework root directory.
                _add_external_root(resolved, roots, seen)
    except Exception:
        pass
    # If there are source files directly in the project root (e.g. main.cpp
    # for Mbed OS), the cc.json loop above won't add project_root because
    # rel.parts[0] is a file, not a directory.  Scan for such files and add
    # the root so they are not silently excluded.
    if project_root not in seen:
        try:
            for entry in project_root.iterdir():
                if entry.is_file() and entry.suffix in _SOURCE_EXTS:
                    if project_root not in seen:
                        roots.append(project_root)
                        seen.add(project_root)
                    break
        except OSError:
            pass

    if not roots:
        roots = [project_root]
        log.info("No source directories detected, falling back to project root")
    log.info("Auto-detected source roots: %s", [str(r) for r in roots])
    return roots


def _add_external_root(tu_path: Path, roots: list[Path], seen: set[Path]) -> None:
    """Add a framework root directory for a TU outside the project root.

    Walks up looking for known markers (library.json, library.properties,
    CMakeLists.txt with project()/idf_build, Kconfig), then falls back to
    the grandparent or great-great-grandparent directory — whichever is the
    smallest directory that still groups multiple TUs.
    """
    # Walk up looking for known framework/library markers
    for ancestor in tu_path.parents:
        if ancestor in seen:
            return
        # PlatformIO library markers
        if (ancestor / "library.json").exists() or (ancestor / "library.properties").exists():
            if ancestor not in seen:
                roots.append(ancestor)
                seen.add(ancestor)
            return
        # ESP-IDF component / Zephyr module marker
        cmake = ancestor / "CMakeLists.txt"
        if cmake.exists():
            try:
                text = cmake.read_text()
                if "idf_build" in text or "idf_component" in text:
                    # This is an ESP-IDF component dir — go one level up for
                    # the IDF root (e.g. ~/esp/esp-idf/components/foo → ~/esp/esp-idf)
                    parent = ancestor.parent
                    if parent not in seen:
                        roots.append(parent)
                        seen.add(parent)
                    return
                if "zephyr_library" in text or "zephyr_module" in text:
                    parent = ancestor.parent
                    if parent not in seen:
                        roots.append(parent)
                        seen.add(parent)
                    return
            except (OSError, UnicodeDecodeError):
                pass

    # Fallback: walk up 2–4 levels and add the first directory not yet covered.
    # For PlatformIO: ~/.platformio/packages/framework-arduinoespressif32/cores/esp32/foo.c
    #   up 2 → framework-arduinoespressif32/cores/ (too deep)
    #   up 3 → framework-arduinoespressif32/ (correct)
    # For ESP-IDF:  ~/esp/esp-idf/components/esp_system/esp_err.c
    #   up 2 → esp-idf/components/ (too deep)
    #   up 3 → esp-idf/ (correct)
    parents = list(tu_path.parents)
    for level in (3, 2, 4):  # try level 3 first (best heuristic), then 2, then 4
        if level < len(parents):
            candidate = parents[level]
            if candidate not in seen and not str(candidate).startswith("/usr"):
                roots.append(candidate)
                seen.add(candidate)
                return


def _detect_sdk_exclude_like(project_root: Path, extra_exclude: list[str] | None = None) -> list[str]:
    """Return LIKE patterns for SDK/vendor directories present in *project_root*.

    Auto-detects known ecosystem markers and returns patterns with a ``%``
    prefix so they match both relative (``mbed-os/...``) and absolute
    (``/home/.../mbed-os/...``) paths in the files table.

    Merges with *extra_exclude* from the project config (``exclude_paths``).
    """
    patterns: list[str] = []

    _MARKERS: dict[str, str] = {
        "mbed-os": "mbed-os",
        "zephyr": "zephyr",
        ".pio": ".pio",
        "modules": "modules",
    }

    for marker_dir, pattern_base in _MARKERS.items():
        if (project_root / marker_dir).is_dir():
            patterns.append(f"%{pattern_base}/%")

    # Build output directories (build/BUILD) — always added as they are
    # common across all ecosystems and the config default includes them.
    for build_dir in ("build", "BUILD"):
        patterns.append(f"%{build_dir}/%")

    if extra_exclude:
        for p in extra_exclude:
            p = p.strip("/")
            if p and f"%{p}/%" not in patterns:
                patterns.append(f"%{p}/%")

    return patterns


def _build_embeddings(conn, config_hash: str, llm_config, db_dir: Path) -> None:
    """Generate and store vector embeddings for all definition symbols.

    Selects all function, method, constructor, destructor, class, struct,
    and union definitions from the current build, builds a human-readable description
    for each (combining file path, class, name, signature, docstring, and
    LLM summary), and produces embeddings via Ollama.

    Descriptions are processed in chunks of 100 to stay within model context
    limits.  Each batch is stored in two tables simultaneously:

    * ``upsert_embeddings`` — legacy BLOB table (backward compatibility).
    * ``upsert_embeddings_vec`` — ``sqlite-vec`` vec0 table for KNN search.

    When Ollama is unreachable or returns an error for a batch, a warning is
    logged and the batch is skipped (non-fatal — remaining batches continue).
    """
    import time

    import httpx

    from ..llm.ollama import call_ollama_embed
    from .db import _vec_to_blob, upsert_embeddings, upsert_embeddings_vec

    # Suppress httpx INFO logs (one per batch — noisy during embedding)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        resp = httpx.get(llm_config.ollama_url.rstrip("/") + "/api/tags", timeout=5.0)
        resp.raise_for_status()
    except Exception:
        log.warning("Ollama not reachable — skipping embedding generation")
        return

    model = llm_config.embed_model
    with transaction(conn):
        rows = conn.execute(
            """SELECT s.id, s.name, s.qualified_name, s.kind, s.file_path,
                      s.signature, s.is_definition, s.docstring, s.summary
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.is_definition = 1
                  AND s.kind IN ('function', 'method', 'constructor', 'destructor',
                                'class', 'struct', 'union', 'typedef', 'enum')
                  AND s.id NOT IN (SELECT symbol_id FROM embeddings WHERE model = ?)
               ORDER BY CASE WHEN s.docstring IS NOT NULL AND LENGTH(s.docstring) > 30
                          THEN 0 ELSE 1 END""",
            (config_hash, model),
        ).fetchall()
        if not rows:
            log.info("All definition symbols already embedded (model=%s) — nothing to do", model)
            return

    # Build descriptions using the same format as embed phase
    descriptions = []
    for r in rows:
        fp = (r["file_path"] or "").replace("\\", "/")
        path = ""
        if "/" in fp:
            *dirs, _ = fp.split("/")
            path = "/".join(dirs[-2:])
        qname = r["qualified_name"] or ""
        name = r["name"] or ""
        kind = r["kind"] or ""
        class_ = "::".join(qname.split("::")[:-1]) if "::" in qname else ""
        sig = r["signature"] or ""
        is_os = "mbed-os" in fp.lower()
        doc = ""
        if not is_os:
            doc = (r["docstring"] or "").strip()
            if doc and len(doc) > 20:
                doc = doc[:150]
        llm = (r["summary"] or "").strip()
        if llm:
            llm = llm[:200]

        # Structured description: kind prefix helps embeddings distinguish
        # e.g. "function", "class", "typedef" from each other
        parts = [f"{kind} {name}"]
        if class_:
            parts.append(f"in {class_}")
        if path:
            parts.append(f"in {path}")
        if sig:
            parts.append(sig)
        if doc:
            parts.append(doc)
        if llm:
            parts.append(llm)
        descriptions.append(" : ".join(parts))

    total = 0
    chunk_size = 100
    total_batches = (len(rows) + chunk_size - 1) // chunk_size
    embedding_dim: int | None = None
    for i in range(0, len(rows), chunk_size):
        batch_num = i // chunk_size
        chunk_rows = rows[i : i + chunk_size]
        chunk_descs = descriptions[i : i + chunk_size]
        t0 = time.monotonic()
        try:
            embs = call_ollama_embed(chunk_descs, llm_config, query=False)
        except Exception as e:
            elapsed = time.monotonic() - t0
            log.warning("[%d/%d] embedding batch failed %s: %s", batch_num + 1, total_batches, _fmt_dur(elapsed), e)
            continue

        if embedding_dim is None and embs:
            embedding_dim = len(embs[0])
            log.info("Embedding dimension detected: %d (model=%s)", embedding_dim, model)
            from .db import init_vec_table

            try:
                init_vec_table(conn, embedding_dim)
            except Exception as e:
                log.warning("vec0 table recreation failed (non-fatal): %s", e)

        # Store in legacy BLOB table (backward compatibility)
        with write_lock(db_dir, timeout=5.0):
            blob_batch = [(r["id"], _vec_to_blob(emb), model) for r, emb in zip(chunk_rows, embs, strict=True)]
            upsert_embeddings(conn, blob_batch)

            # Store in vec0 table (sqlite-vec KNN search)
            try:
                vec_batch = [(r["id"], config_hash, emb) for r, emb in zip(chunk_rows, embs, strict=True)]
                upsert_embeddings_vec(conn, vec_batch)
            except Exception as e:
                log.warning("vec0 batch insert failed (sqlite-vec may not be loaded): %s", e)

        total += len(blob_batch)
        elapsed = time.monotonic() - t0
        log.info("[%d/%d] %d symbols embedded %s", batch_num + 1, total_batches, len(chunk_rows), _fmt_dur(elapsed))
    log.info("Embeddings stored: %d symbols (model=%s)", total, model)
    if embedding_dim is not None:
        conn.execute(
            "UPDATE build_configs SET embedding_dim = ? WHERE config_hash = ?",
            (embedding_dim, config_hash),
        )


# SDK path patterns for filtering (mbed-os, Zephyr, PlatformIO, build dirs)
_SDK_PATH_PATTERNS = ("mbed-os/", ".pio/", "zephyr/", "build/", "modules/")


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

        # Fetch callees from the reference index
        if usr:
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
    exclude_like: list[str] | None = None,
    write_lock_held: bool = False,
    cache_client=None,
    retry_unparseable: bool = False,
) -> None:
    """Generate structured LLM analysis (summary, inputs, outputs) for each
    project-definition symbol using Ollama, one symbol per request.

    Processes symbols individually — one Ollama request per symbol — for
    reliable format adherence. Only project symbols (non-SDK) are analyzed.

    The prompt includes the full function body (read from disk via exact
    libclang extents) and callee names (from the reference index), which
    dramatically improves description quality.

    *db_dir* is the directory containing the index database — used for the
    write lock that serializes DB access across processes.
    *exclude_like* are LIKE patterns for SDK/vendor paths to skip
    (auto-detected from project structure when omitted).
    *retry_unparseable* when True clears all ``skip:unparseable`` sentinels
    so previously-failed symbols are re-attempted. Set True for manual
    indexing, False for background reindex (safe: retries only on model change).
    """
    import httpx

    from ..indexer.prompts import build_analysis_prompt, parse_analysis_response
    from ..utils import compute_content_hash
    from .db import upsert_llm_analysis_batch

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
    except Exception:
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

    if exclude_like is None:
        exclude_like = []

    exclude_clauses = " AND ".join(["s.file_path NOT LIKE ?"] * len(exclude_like))
    query = """SELECT s.id, s.name, s.qualified_name, s.kind, s.file_path,
                      s.signature, s.is_definition, s.docstring,
                      s.end_line, s.line, s.usr,
                      f.path as abs_path
               FROM symbols s
               JOIN files f ON s.file_id = f.id
               WHERE s.config_hash = ?
                 AND s.is_definition = 1
                  AND s.kind IN ('function', 'method', 'constructor', 'destructor',
                                 'class', 'struct', 'union', 'typedef', 'enum')
                  AND s.name NOT LIKE '%(anonymous%'
                 AND s.name NOT LIKE '%(unnamed%'"""
    if exclude_clauses:
        query += f" AND {exclude_clauses}"
    query += """ AND s.id NOT IN (SELECT symbol_id FROM llm_analysis)
               ORDER BY s.kind, s.file_path, s.line"""

    with transaction(conn):
        rows = conn.execute(query, (config_hash, *exclude_like)).fetchall()
        if not rows:
            log.info("All project symbols already analyzed — nothing to do")
            return

    model = llm_config.model
    total_symbols = len(rows)
    total = 0

    log.info("LLM analysis: %d symbols (model=%s)", total_symbols, model)

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
                from ..cache_client import get_local_cache_db, local_cache_lookup

                local_db = get_local_cache_db(readonly=True)
                local_hits = local_cache_lookup(local_db, [h])
                local_db.close()
                cached = local_hits.get(h)
            except Exception as e:
                log.debug("Local global cache lookup failed: %s", e)

            # Tier 2: remote cache server
            if not cached and cache_client is not None:
                try:
                    remote_hits = cache_client.batch_get([h])
                    cached = remote_hits.get(h)
                    if cached:
                        # Store in local global cache for next time
                        try:
                            local_db = get_local_cache_db()
                            from ..cache_client import local_cache_upsert

                            local_cache_upsert(local_db, [{"hash": h, **cached}])
                            local_db.close()
                        except Exception as e:
                            log.debug("Local global cache write failed: %s", e)
                    else:
                        log.debug("Remote cache miss for %s (hash=%s…)", qname, h[:12])
                except Exception as e:
                    log.debug("Remote cache lookup failed for %s: %s", qname, e)

            if cached:
                # Cache hit — re-use existing analysis
                with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
                    with transaction(conn):
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
                _est_prompt_tokens = len(prompt) / 3.5
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
            _est_prompt_tokens = len(prompt) / 3.5
            _safety_margin = 300
            _ctx_size = _model_ctx_size
            num_predict = max(500, int(_ctx_size - _est_prompt_tokens - _safety_margin))

            # If the prompt STILL doesn't fit after truncation, skip.
            # The model needs at least 300 tokens of response space.
            if _est_prompt_tokens + 300 + _safety_margin > _ctx_size:
                with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
                    with transaction(conn):
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
            except Exception as e:
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
                    with transaction(conn):
                        sentinel = f"skip:unparseable:{model}"
                        upsert_llm_analysis_batch(conn, [(d["id"], "", "", "", sentinel, h)])
                elapsed = time.monotonic() - t0
                log.warning(
                    "[%d/%d] %s: err %s: unparseable response", idx + 1, total_symbols, qname, _fmt_dur(elapsed)
                )
                continue

            r = parsed[0]
            with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
                with transaction(conn):
                    db_rows = [(r["symbol_id"], r["summary"], r["inputs"], r["outputs"], model, h)]
                    inserted = upsert_llm_analysis_batch(conn, db_rows)
                    # Store in local global cache
                    try:
                        from ..cache_client import get_local_cache_db, local_cache_upsert

                        local_db = get_local_cache_db()
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
                        local_db.close()
                    except Exception as e:
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
                        except Exception as e:
                            log.debug("Remote cache write failed: %s", e)
                    total += inserted

            elapsed = time.monotonic() - t0
            log.info("[%d/%d] %s: ok %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed))
            log.debug("  summary: %s", r["summary"])
            log.debug("  inputs : %s", r["inputs"])
            log.debug("  outputs: %s", r["outputs"])
        except Exception as e:
            elapsed = time.monotonic() - t0
            log.warning("[%d/%d] %s: err %s: %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed), e)
            continue

    log.info("LLM analysis stored: %d/%d symbols (model=%s)", total, total_symbols, model)


def _extract_param_types(signature: str) -> str:
    """Extract the parameter type list from a C/C++ function signature.

    Strips parameter names, default values, and whitespace to produce a
    normalized string suitable for override comparison.

    Examples:
        "int read(char *buf, size_t len)" → "char *,size_t"
        "void write(const uint8_t *data, size_t len)" → "const uint8_t *,size_t"
        "void reset()" → ""
        "void set(int)" → "int"
    """
    # Find the outermost parentheses
    paren_start = signature.find("(")
    if paren_start == -1:
        return ""
    paren_depth = 0
    paren_end = paren_start
    for i in range(paren_start, len(signature)):
        if signature[i] == "(":
            paren_depth += 1
        elif signature[i] == ")":
            paren_depth -= 1
            if paren_depth == 0:
                paren_end = i
                break
    params_str = signature[paren_start + 1 : paren_end].strip()
    if not params_str:
        return ""
    # In C++, foo(void) and foo() are semantically identical — normalize both to empty
    if params_str == "void":
        return ""

    # Split by top-level commas, strip parameter names (keep only types)
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in params_str:
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            if ch in "<({[":
                depth += 1
            elif ch in ">)}]":
                depth -= 1
            current.append(ch)
    if current:
        parts.append("".join(current).strip())

    # For each parameter, extract the type by removing the parameter name
    # and any trailing default value
    normalized: list[str] = []
    for param in parts:
        param = param.strip()
        if not param:
            continue
        # Remove default value (everything after '=')
        eq_idx = param.find("=")
        if eq_idx != -1:
            param = param[:eq_idx].strip()
        # Remove parameter name from the end.
        # The parameter name is the last identifier — it may be preceded by
        # pointer/reference markers (*, &, &&) that belong to the type.
        tokens = param.split()
        if len(tokens) >= 2:
            last = tokens[-1]
            # Strip leading pointer/reference markers from the last token
            stripped = last.lstrip("*&")
            # If what remains is a pure identifier (alphanumeric + underscores),
            # it's the parameter name — remove it, keeping any pointer/ref prefix
            # on the type.  But C++ type qualifiers (const, volatile, etc.) are
            # NOT parameter names — keep them.
            _CPP_TYPE_QUALIFIERS = frozenset({"const", "volatile", "constexpr", "noexcept"})
            if stripped and stripped.replace("_", "").isalnum() and stripped not in _CPP_TYPE_QUALIFIERS:
                ptr_prefix = last[: len(last) - len(stripped)]
                if ptr_prefix:
                    # Pointer/ref on the name token (e.g. "*buf") — move markers
                    # to the type by keeping them as a separate token
                    tokens[-1] = ptr_prefix
                else:
                    # Pure name — drop the last token
                    tokens = tokens[:-1]
        param = " ".join(tokens).strip()
        normalized.append(param)

    return ",".join(normalized)


def _build_overrides(
    conn, config_hash: str, db_dir: Path, *, write_lock_held: bool = False, force: bool = False
) -> None:
    """Build the method override graph by matching virtual methods to their
    base-class counterparts through the inheritance chain.

    Pure post-processing — walks the inheritance graph already stored in
    the ``inheritance`` table and matches methods by name.  Parameter-type
    comparison provides a basic guard against accidental name collisions
    (overloads, not overrides).

    Set *force* to True to recompute even when overrides already exist
    (e.g. after incremental reindex).
    """
    from .db import insert_overrides_batch

    # Idempotency: if overrides were already built for this config, skip
    if not force:
        row = conn.execute("SELECT COUNT(*) FROM overrides WHERE config_hash = ?", (config_hash,)).fetchone()
        if row and row[0] > 0:
            log.info("Override graph already built (%d relationships) — nothing to do", row[0])
            return
    else:
        # Start from a clean slate — old overrides may reference removed
        # virtual methods or changed inheritance chains.
        conn.execute("DELETE FROM overrides WHERE config_hash = ?", (config_hash,))

    total = 0

    # Phase 1: collect all virtual/pure-virtual project methods with parent class info
    with transaction(conn):
        virtual_rows = conn.execute(
            """SELECT s.usr, s.name, s.qualified_name, s.signature,
                      s.parent_usr, s.kind
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.is_definition = 1
                 AND (s.is_virtual = 1 OR s.is_pure_virtual = 1)
                 AND s.kind IN ('method', 'destructor')
                 AND s.parent_usr != ''
               ORDER BY s.parent_usr, s.name""",
            (config_hash,),
        ).fetchall()

    if not virtual_rows:
        log.info("No virtual methods found — skipping override analysis")
        return

    # Phase 2: for each virtual method, walk the inheritance chain up and
    # find base-class methods with the same name.
    # Build parent→bases lookup cache for efficiency.
    parent_to_bases: dict[str, list[str]] = {}

    def _get_bases_recursive(parent_usr: str, visited: set | None = None) -> list[str]:
        """BFS up the inheritance chain — return all ancestor USRs."""
        if visited is None:
            visited = set()
        if parent_usr in parent_to_bases:
            return parent_to_bases[parent_usr]
        bases: list[str] = []
        queue = [parent_usr]
        while queue:
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            rows = conn.execute(
                """SELECT base_usr FROM inheritance
                   WHERE config_hash = ? AND derived_usr = ?""",
                (config_hash, cur),
            ).fetchall()
            for r in rows:
                if r["base_usr"] not in visited:
                    bases.append(r["base_usr"])
                    queue.append(r["base_usr"])
        parent_to_bases[parent_usr] = bases
        return bases

    # Phase 3: resolve overrides
    override_rows: list[tuple[str, str, str]] = []
    skipped_no_base = 0
    skipped_no_match = 0

    for vrow in virtual_rows:
        base_usrs = _get_bases_recursive(vrow["parent_usr"])
        if not base_usrs:
            skipped_no_base += 1
            continue

        # Find virtual methods with the same name in any base class.
        # Only virtual/pure-virtual base methods can be overridden — non-virtual
        # methods with the same signature are *hidden*, not overridden.
        placeholders = ",".join("?" * len(base_usrs))
        base_methods = conn.execute(
            f"""SELECT usr, signature, parent_usr, qualified_name
                FROM symbols
                WHERE config_hash = ?
                  AND name = ?
                  AND kind IN ('method', 'destructor')
                  AND (is_virtual OR is_pure_virtual)
                  AND parent_usr IN ({placeholders})
                ORDER BY qualified_name""",
            (config_hash, vrow["name"], *base_usrs),
        ).fetchall()

        if not base_methods:
            skipped_no_match += 1
            continue

        # Compare parameter types to filter out accidental name collisions
        derived_params = _extract_param_types(vrow["signature"] or "")
        for bm in base_methods:
            base_params = _extract_param_types(bm["signature"] or "")
            if derived_params == base_params:
                override_rows.append((config_hash, vrow["usr"], bm["usr"]))

    if override_rows:
        with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
            with transaction(conn):
                insert_overrides_batch(conn, override_rows)
                total += len(override_rows)

    log.info(
        "Overrides stored: %d relationships (%d virtual, %d no-base, %d no-match)",
        total,
        len(virtual_rows),
        skipped_no_base,
        skipped_no_match,
    )


def _build_pagerank(conn, config_hash: str, *, write_lock_held: bool = False, force: bool = False) -> None:
    """Compute PageRank scores for function/method symbols from the call graph.

    Iterates until convergence (max 50 iterations, damping factor 0.85).
    Scores are normalized to 0.0–1.0 and stored in ``symbols.pagerank``.

    Idempotent — skips when pagerank already exists for this config.
    Set *force* to True to recompute even when pagerank data already exists
    (e.g. after incremental reindex).
    Requires the reference index (``fw-context index`` — refs on by default).
    """
    # Check already computed
    if not force:
        row = conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE config_hash = ? AND pagerank > 0",
            (config_hash,),
        ).fetchone()
        if row and row[0] > 0:
            log.info("PageRank already computed — nothing to do")
            return

    edges = conn.execute(
        """SELECT DISTINCT r.from_usr, r.to_usr
           FROM refs r
           JOIN symbols fs ON fs.usr = r.from_usr AND fs.config_hash = r.config_hash
           JOIN symbols ts ON ts.usr = r.to_usr AND ts.config_hash = r.config_hash
           WHERE r.config_hash = ?
             AND r.ref_kind = 'call'
             AND fs.kind IN ('function', 'method', 'constructor', 'destructor')
             AND ts.kind IN ('function', 'method', 'constructor', 'destructor')
             AND r.from_usr != ''
             AND r.to_usr != ''
        """,
        (config_hash,),
    ).fetchall()

    outgoing: dict[str, list[str]] = {}
    incoming: dict[str, list[str]] = {}
    all_nodes: set[str] = set()

    for e in edges:
        frm, to = e["from_usr"], e["to_usr"]
        outgoing.setdefault(frm, []).append(to)
        incoming.setdefault(to, []).append(frm)
        all_nodes.add(frm)
        all_nodes.add(to)

    n = len(all_nodes)
    if n == 0:
        log.info("No call graph edges — skipping PageRank")
        return

    damping = 0.85
    scores: dict[str, float] = {node: 1.0 / n for node in all_nodes}

    for iteration in range(50):
        new_scores: dict[str, float] = {}
        for node in all_nodes:
            rank = (1 - damping) / n
            for caller in incoming.get(node, []):
                out_count = len(outgoing.get(caller, [1]))
                rank += damping * scores[caller] / out_count
            new_scores[node] = rank
        diff = sum(abs(new_scores[node] - scores[node]) for node in all_nodes)
        scores = new_scores
        if diff < 1e-6:
            log.info("PageRank converged after %d iterations", iteration + 1)
            break

    # Normalize to 0.0–1.0
    max_score = max(scores.values()) if scores else 1.0
    if max_score > 0:
        for node in scores:
            scores[node] /= max_score

    with transaction(conn):
        conn.executemany(
            "UPDATE symbols SET pagerank = ? WHERE config_hash = ? AND usr = ?",
            [(scores[usr], config_hash, usr) for usr in scores],
        )

    log.info("PageRank stored: %d nodes", n)


def _build_hotspot_cache(conn, config_hash: str, *, force: bool = False) -> None:
    """Pre-compute hotspot caller counts for instant ``find_hotspots`` queries.

    Idempotent — skips when cache already exists for this config.
    Set *force* to True to recompute even when cache data already exists
    (e.g. after incremental reindex).
    Requires the reference index.
    """
    if not force:
        row = conn.execute("SELECT COUNT(*) FROM hotspot_cache WHERE config_hash = ?", (config_hash,)).fetchone()
        if row and row[0] > 0:
            log.info("Hotspot cache already built — nothing to do")
            return

    with transaction(conn):
        if force:
            conn.execute("DELETE FROM hotspot_cache WHERE config_hash = ?", (config_hash,))
        conn.execute(
            """INSERT INTO hotspot_cache (config_hash, symbol_id, caller_count)
               SELECT r.config_hash, s.id, COUNT(r.rowid)
               FROM refs r
               JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
               WHERE r.config_hash = ?
                 AND s.is_definition = 1
                 AND r.ref_kind IN ('call', 'indirect')
               GROUP BY s.usr
            """,
            (config_hash,),
        )

    cnt = conn.execute("SELECT COUNT(*) FROM hotspot_cache WHERE config_hash = ?", (config_hash,)).fetchone()[0]
    log.info("Hotspot cache stored: %d entries", cnt)


# ── Content-hash helpers ────────────────────────────────────────────


def _refresh_header_mtimes_from_manifest(
    conn,
    config_hash: str,
    project_root: Path,
    manifest: dict | None,
) -> int:
    """Refresh stored mtimes for headers touched by VCS operations.

    After ``git checkout`` / ``git merge``, header files get new mtimes
    even when their content hasn't changed.  The stored ``files.mtime``
    values fall behind, causing ``_count_modified_files`` to report phantom
    modifications and spawn unnecessary background reindexes.

    This function scans the manifest's header entries and updates the
    stored mtime whenever the on-disk mtime is newer but the stored mtime
    is stale — fixing the drift without a full Tier-3 reparse.

    Called once after the main TU loop, before the manifest update phase.
    Returns the number of refreshed header records.
    """
    if manifest is None:
        return 0
    refreshed = 0
    for entry in manifest.get("entries", []):
        for h in entry.get("headers", []):
            # Resolve absolute path for stat() — h["path"] may be relative
            p = Path(h["path"])
            if not p.is_absolute():
                p_resolved = (project_root / p).resolve()
            else:
                p_resolved = p.resolve()
            try:
                cur_mtime = p_resolved.stat().st_mtime
            except OSError:
                continue
            # Use the manifest path directly — it already matches files.path format
            cur_obj = conn.execute(
                "UPDATE files SET mtime=? WHERE config_hash=? AND path=? AND mtime < ?",
                (cur_mtime, config_hash, h["path"], cur_mtime),
            )
            if cur_obj.rowcount:
                refreshed += 1
    if refreshed:
        log.info("header mtimes refreshed from manifest: %d", refreshed)
    return refreshed


def _is_excluded(file_path: Path, exclude_paths: list[Path], source_roots: list[Path]) -> bool:
    """Return True when *file_path* should be excluded from indexing.

    Source roots take priority over exclude paths — a file inside a
    source root is never excluded, even when it also falls under an
    exclude path.  This handles build systems that place preprocessed
    source files inside the build directory (Arduino, PlatformIO).
    """
    resolved = file_path.resolve()
    in_source = any(resolved == sr or resolved.is_relative_to(sr) for sr in source_roots)
    if in_source:
        return False
    return any(resolved == ep or resolved.is_relative_to(ep) for ep in exclude_paths)


def _update_manifest_after_index(
    *,
    manifest: dict | None,
    units: list,
    project_root: Path,
    db_dir: Path,
    compile_commands: Path,
    updated_count: int,
    tu_headers: dict[str, list[dict]] | None = None,
    build_dir_patterns: list[str] | None = None,
) -> dict | None:
    """Update ``manifest.json`` after an indexing run.

    Strategy:
    - No existing manifest → build from scratch (tokenize all TUs).
    - Manifest exists, nothing changed → skip (manifest is still valid).
    - Manifest exists, TUs changed, *tu_headers* provided → incremental:
      reuse stored entries for unchanged TUs, update only changed ones.
    - Manifest exists, TUs changed, no *tu_headers* → fallback to full
      rebuild (re-tokenize all TUs).

    Returns the updated manifest dict, or ``None`` when no update needed.
    """
    from .manifest import MANIFEST_FORMAT, _collect_headers_from_tokens, save

    # Nothing changed — keep existing manifest as-is, but only when all
    # of these hold:
    #   - TU list hasn't changed (same number of entries)
    #   - No stale header hashes were collected (tu_headers empty)
    #   - Manifest entries have real source_hash data (not a preliminary
    #     manifest written by build_preliminary with empty hashes)
    # A different TU count means files were added/removed from
    # compile_commands.json.  A preliminary manifest means the on-disk
    # file was overwritten by build_preliminary and needs regeneration.
    if manifest is not None and updated_count == 0 and not tu_headers:
        # Check for degraded (preliminary) manifest — entries with empty
        # source_hash mean build_preliminary overwrote the real manifest.
        entries = manifest.get("entries", [])
        if entries and not entries[0].get("source_hash"):
            log.info("Manifest has preliminary entries — regenerating with full hashes")
            # Fall through to full regeneration below (don't return early)
        else:
            old_count = len(entries)
            if old_count == len(units):
                return manifest
            log.info("Rebuilding manifest.json (TU count changed: %d → %d)", old_count, len(units))

    # ── Build/update manifest entries ──
    # Priority: 1) tu_headers (pre-collected during main loop — no extra I/O),
    # 2) old manifest entries (unchanged), 3) libclang tokenization (slow fallback).
    from .manifest import generate as generate_manifest
    from .manifest import load as reload_manifest

    if tu_headers is not None:
        # Use pre-collected header hashes from _build_filtered_file_content.
        # Avoids a second libclang parse — tu_headers was populated during
        # the main TU loop for every unchanged/updated TU.
        log.info("Building manifest.json from %d pre-collected TU headers...", len(tu_headers))
        old_entries: dict[str, dict] = {}
        if manifest is not None:
            old_entries = {e.get("file", ""): e for e in manifest.get("entries", [])}
        entries = []
        reused = 0
        updated = 0

        for unit in units:
            try:
                tu_rel = str(unit.file.resolve().relative_to(project_root))
            except ValueError:
                tu_rel = str(unit.file.resolve())

            if tu_rel in tu_headers:
                source_hash = compute_source_hash(unit.file.resolve())
                entries.append(
                    {
                        "file": tu_rel,
                        "directory": str(unit.directory) if unit.directory else str(project_root),
                        "arguments": unit.clang_args,
                        "source_hash": source_hash,
                        "headers": tu_headers[tu_rel],
                    }
                )
                updated += 1
            elif tu_rel in old_entries:
                entries.append(old_entries[tu_rel])
                reused += 1
            else:
                headers = _collect_headers_from_tokens(unit, project_root, build_dir_patterns)
                source_hash = compute_source_hash(unit.file.resolve())
                entries.append(
                    {
                        "file": tu_rel,
                        "directory": str(unit.directory) if unit.directory else str(project_root),
                        "arguments": unit.clang_args,
                        "source_hash": source_hash,
                        "headers": headers,
                    }
                )
                updated += 1

        log.info("manifest.json: %d updated (from tu_headers), %d reused", updated, reused)
    elif manifest is None:
        # No manifest and no tu_headers — full rebuild via libclang (slow)
        log.info("Generating manifest.json from %d TUs...", len(units))
        generate_manifest(compile_commands, db_dir, project_root, units, build_dir_patterns=build_dir_patterns)
        return reload_manifest(db_dir)
    else:
        # ── Incremental update (tu_headers=None, manifest exists) ──
        old_entries = {e.get("file", ""): e for e in manifest.get("entries", [])}
        entries = []
        reused = 0
        updated = 0

        for unit in units:
            try:
                tu_rel = str(unit.file.resolve().relative_to(project_root))
            except ValueError:
                tu_rel = str(unit.file.resolve())

            if tu_rel in old_entries:
                entries.append(old_entries[tu_rel])
                reused += 1
            else:
                headers = _collect_headers_from_tokens(unit, project_root, build_dir_patterns)
                source_hash = compute_source_hash(unit.file.resolve())
                entries.append(
                    {
                        "file": tu_rel,
                        "directory": str(unit.directory) if unit.directory else str(project_root),
                        "arguments": unit.clang_args,
                        "source_hash": source_hash,
                        "headers": headers,
                    }
                )
                updated += 1

        log.info("manifest.json incremental: %d updated, %d reused", updated, reused)

    log.info("manifest.json incremental: %d updated, %d reused", updated, reused)

    manifest_data = {
        "_format": MANIFEST_FORMAT,
        "compile_commands_path": str(compile_commands),
        "project_root": str(project_root),
        "entries": entries,
    }
    # Preserve build_dir_patterns across incremental updates
    if build_dir_patterns:
        manifest_data["build_dir_patterns"] = build_dir_patterns
    elif manifest and manifest.get("build_dir_patterns"):
        manifest_data["build_dir_patterns"] = manifest["build_dir_patterns"]
    # Preserve macros from old manifest
    if manifest and manifest.get("macros"):
        manifest_data["macros"] = manifest["macros"]
    config_hash = save(manifest_data, db_dir)
    header_count = sum(len(e.get("headers", [])) for e in entries)
    log.info("manifest.json saved: %d TUs, %d headers, config_hash=%s", len(entries), header_count, config_hash[:12])
    return manifest_data


def _check_and_parse_unit(
    unit,
    config_hash,
    project_root,
    source_roots,
    exclude_paths,
    index_refs,
    existing_files,
    force=False,
    manifest=None,
):
    """Check whether *unit* needs re-parsing and parse it if so.

    Uses a three-tier staleness check:
    1. **mtime fast-path** — unchanged mtime → skip (no I/O).
    2. **content-hash check** — mtime differs but hashes match → skip
       (the source, flags, and header dependencies have not changed).
       Uses ``manifest.json`` for header hashes when available — no libclang needed.
    3. **libclang parse** — content hashes differ → parse.

    Does NOT write to the database — the caller is responsible for
    acquiring ``write_lock`` and calling ``_process_unit(pre_parsed=...)``
    to persist the result.

    Args:
        manifest: Optional ``{file_path: entry}`` lookup dict built from
            ``manifest.load()`` entries.  When provided, header staleness
            is checked via hash comparison against the manifest (fast —
            file reads + SHA-256 only).  When ``None``, falls back to
            source-hash-only comparison.

    Returns:
        * ``("unchanged", None, None, None)`` — no re-parse needed.
        * ``("skipped", None, None, None)`` — excluded path or parse failed.
        * ``("updated", parsed, (t_start, t_end), hashes)`` — parsed
          successfully, ready for ``_process_unit(pre_parsed=parsed)``.
          *hashes* is ``(source_hash, flags_hash, manifest_entry_hash)``.
    """
    resolved_tu = unit.file.resolve()
    if _is_excluded(resolved_tu, exclude_paths, source_roots):
        return ("unchanged", None, None, None)

    file_path = _normalize_file_path(str(resolved_tu), project_root)
    force_refs = force or os.environ.get("FW_CONTEXT_FORCE_REFINDEX") == "1"

    # ── Tier 1: mtime fast-path ──
    if not force_refs and file_path in existing_files:
        rec = existing_files[file_path]
        stored_mtime = rec.mtime
        try:
            current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
        except OSError:
            current_mtime = 0.0
        if current_mtime <= stored_mtime + MTIME_TOLERANCE_S:
            return ("unchanged", None, None, None)

    # ── Tier 2: content-hash check (mtime differs) ──
    # Compute source hash first — cheap, no libclang.
    try:
        source_hash = compute_source_hash(unit.file)
    except OSError:
        source_hash = ""

    if unit.raw_entry is not None:
        flags_hash = compute_flags_hash(unit.raw_entry)
    else:
        flags_hash = ""

    # Determine manifest entry hash for Tier 2 comparison.
    # When manifest.json exists, use check_tu_staleness() — fast hash comparison
    # against stored values.  When not, fall back to source-only hash.
    manifest_entry_hash = _get_manifest_entry_hash_for_unit(
        unit,
        project_root,
        source_roots,
        manifest,
    )

    content_hash = compute_tu_content_hash(source_hash, flags_hash, manifest_entry_hash)
    hashes = (source_hash, flags_hash, manifest_entry_hash)

    if not force_refs and file_path in existing_files:
        rec = existing_files[file_path]
        # rec is a FileHashRecord from get_file_hashes() — attribute access
        if rec.content_hash and rec.content_hash == content_hash:
            # Content unchanged — just the mtime was bumped by a rebuild.
            return ("unchanged", None, None, hashes)

    # ── Tier 3: libclang parse ──
    from .symbols import extract_all

    t_parse_start = time.monotonic()
    try:
        parsed = extract_all(
            unit,
            source_roots=source_roots,
            exclude_paths=exclude_paths,
            with_refs=index_refs,
        )
    except sqlite3.Error:
        log.error("Fatal DB error parsing %s — stopping indexer", unit.file.name)
        raise
    except Exception as exc:
        msg = str(exc)
        if "unable to open database file" in msg:
            log.error("Fatal DB error parsing %s: %s — stopping indexer", unit.file.name, exc)
            raise
        log.warning("skip TU %s: %s", unit.file.name, exc)
        return ("skipped", None, None, None)
    t_parse_end = time.monotonic()
    return ("updated", parsed, (t_parse_start, t_parse_end), hashes)


def _get_manifest_entry_hash_for_unit(
    unit,
    project_root: Path,
    source_roots: list[Path],
    manifest_lookup: dict[str, dict] | None,
) -> str:
    """Return the manifest entry hash for a TU for Tier 2 staleness comparison.

    When *manifest_lookup* is available (``{file_path: entry}`` dict from
    ``manifest.json``), checks header staleness via ``check_tu_staleness()``
    — fast file reads + SHA-256 only, no libclang.

    When *manifest_lookup* is ``None``, falls back to a source-only hash
    (no header tracking possible).
    """
    from .manifest import check_tu_staleness, compute_current_entry_hash
    from .manifest import get_manifest_entry_hash as _entry_hash

    # ── Manifest path (fast — no libclang, O(1) lookup) ──
    if manifest_lookup is not None:
        try:
            tu_rel = str(unit.file.resolve().relative_to(project_root))
        except ValueError:
            tu_rel = str(unit.file.resolve())

        entry = manifest_lookup.get(tu_rel)
        if entry is not None:
            stale, current_source_hash = check_tu_staleness(entry, project_root, source_roots)
            if not stale:
                return _entry_hash(entry)
            # Stale — compute hash from CURRENT disk content (both source and headers)
            return compute_current_entry_hash(
                entry,
                project_root,
                source_roots,
                new_source_hash=current_source_hash,
            )

    # ── Fallback: source-only hash (no manifest, no header tracking) ──
    try:
        source_hash = hashlib.sha256(unit.file.resolve().read_bytes()).hexdigest()
    except OSError:
        source_hash = ""
    return source_hash


def _process_unit(
    unit,
    config_hash,
    project_root,
    source_roots,
    exclude_paths,
    index_refs,
    db_path,
    existing_files,
    lock=None,
    conn=None,
    force=False,
    pre_parsed=None,
    parse_timing=(0.0, 0.0),
    hashes=None,
    build_dir_patterns=None,
):
    """Process one translation unit: check staleness, parse, store.

    Opens its own DB connection when *conn* is ``None``, otherwise reuses
    the caller-supplied connection (persistent per-worker connection).

    Serializes DB writes via *lock* when supplied (``threading.Lock`` for
    intra-process synchronization).  When *lock* is ``None``, the caller
    is responsible for serialisation (sequential path with fcntl wrap).

    When *pre_parsed* is not ``None``, the staleness check and libclang
    parsing are skipped — the caller already performed them and the lock
    is only held for the DB write.  *parse_timing* provides the
    ``(t_start, t_end)`` values for the summary statistics.

    Args:
        unit: The ``CompilationUnit`` to parse (file path + clang flags).
        config_hash: Content-addressable build fingerprint for scoping
            all DB operations to the current build configuration.
        project_root: Root directory used for path resolution.
        source_roots: Directories whose symbols are considered project code.
        exclude_paths: Directories to skip during extraction.
        index_refs: When True, extract call-graph references.
        db_path: Path to the SQLite database — used to open a connection
            when *conn* is ``None``.
        existing_files: Dictionary mapping file paths to ``(file_id, mtime)``
            tuples, used to skip unchanged translation units.
        lock: Optional ``threading.Lock`` used as a context manager to
            serialise DB writes between workers (intra-process).
        conn: Optional persistent SQLite connection — when provided, the
            caller manages its lifecycle (open once per worker thread,
            close after all TUs).  When ``None``, a connection is opened
            and closed for this call.
        pre_parsed: When not ``None``, the result of ``extract_all()``
            from a prior parse.  Staleness check, parse, and exception
            handling on the parsing step are skipped — the caller already
            decided the TU needs storing.
        parse_timing: ``(t_start, t_end)`` tuple from the caller's
            ``time.monotonic()`` measurements around the parse step.
            Ignored when *pre_parsed* is ``None``.

    Returns:
        A tuple ``(status, symbols_added, refs_added, timing, headers)`` where
        *status* is ``"updated"`` (new or modified symbols stored),
        ``"unchanged"`` (mtime matched — no work needed), or ``"skipped"``
        (excluded by ``exclude_paths`` or failed during parsing), and
        *headers* is a list of ``{path, hash, generated}`` dicts for included
        header files (empty list for unchanged/skipped).
    """
    resolved_tu = unit.file.resolve()
    if _is_excluded(resolved_tu, exclude_paths, source_roots):
        return ("unchanged", 0, 0, (0.0, 0.0, 0.0), [])

    if pre_parsed is not None:
        parsed = pre_parsed
        t_parse_start = parse_timing[0]
        t_parse_end = parse_timing[1]
    else:
        file_path = _normalize_file_path(str(unit.file.resolve()), project_root)
        force_refs = force or os.environ.get("FW_CONTEXT_FORCE_REFINDEX") == "1"
        if not force_refs and file_path in existing_files:
            rec = existing_files[file_path]
            # get_file_hashes returns FileHashRecord (attribute access),
            # get_file_mtimes returns tuple[int, float] (positional).
            stored_mtime = rec.mtime if hasattr(rec, "mtime") else rec[1]
            try:
                current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
            except OSError:
                current_mtime = 0.0
            if current_mtime <= stored_mtime + MTIME_TOLERANCE_S:
                return ("unchanged", 0, 0, (0.0, 0.0, 0.0), [])

        # Parse with libclang outside any lock — this is the expensive
        # CPU-bound step.  Only serialise DB writes, not parsing.
        from .symbols import extract_all

        t_parse_start = time.monotonic()
        try:
            parsed = extract_all(
                unit,
                source_roots=source_roots,
                exclude_paths=exclude_paths,
                with_refs=index_refs,
            )
        except Exception as exc:
            log.warning("skip TU %s: %s", unit.file.name, exc)
            return ("skipped", 0, 0, (0.0, 0.0, 0.0), [])
        t_parse_end = time.monotonic()

    # Resolve connection: persistent (callable → lazy open, don't close),
    # explicit, or own (open now, close after).
    # Must detect already-opened connections first — sqlite3/pysqlite3
    # Connection objects became callable in Python 3.14 (conn("SQL") shortcut).
    if hasattr(conn, "execute"):
        own_conn = False  # caller-supplied, don't close
    elif callable(conn):
        conn = conn()  # lazy thread-local — caller manages lifecycle
        own_conn = False
    elif conn is None:
        conn = open_db(db_path)
        own_conn = True
    else:
        own_conn = False  # caller-supplied, don't close

    t_lock_start = time.monotonic()
    try:
        # threading.Lock (intra-process) or nullcontext (sequential path
        # where the caller holds fcntl write_lock across all TUs)
        lock_ctx: object = lock if lock is not None else nullcontext()
        with lock_ctx:
            t_write_start = time.monotonic()
            with transaction(conn, checkpoint=False):
                syms_added, refs_added, headers = store_symbols_for_unit(
                    conn,
                    unit,
                    config_hash,
                    project_root,
                    source_roots=source_roots,
                    exclude_paths=exclude_paths,
                    index_refs=index_refs,
                    pre_parsed=parsed,
                    existing_files=existing_files,
                    hashes=hashes,
                    build_dir_patterns=build_dir_patterns,
                )
            t_write_end = time.monotonic()
            t_parse = t_parse_end - t_parse_start
            t_lock = t_write_start - t_lock_start
            t_write = t_write_end - t_write_start
            log.debug(
                "  TU %s: parse=%.1fs lock_wait=%.2fs write=%.1fs syms=%d refs=%d",
                unit.file.name,
                t_parse,
                t_lock,
                t_write,
                syms_added,
                refs_added,
            )
        timing = (t_parse, t_lock, t_write)
        return ("updated", syms_added, refs_added, timing, headers)
    except sqlite3.Error:
        log.error("Fatal DB error storing %s — stopping indexer", unit.file.name)
        raise
    except Exception as exc:
        msg = str(exc)
        if "unable to open database file" in msg:
            log.error("Fatal DB error storing %s: %s — stopping indexer", unit.file.name, exc)
            raise
        log.warning("skip TU %s: %s", unit.file.name, exc)
        return ("skipped", 0, 0, (0.0, 0.0, 0.0), [])
    finally:
        if own_conn:
            conn.close()


def run(
    compile_commands: Path,
    db_path: Path,
    source_roots: list[Path] | None = None,
    exclude_paths: list[Path] | None = None,
    project_name: str | None = None,
    index_refs: bool = False,
    index_embeddings: bool = False,
    analyze_symbols: bool = False,
    analyze_overrides: bool = True,
    project_root: Path | None = None,
    project_id: str | None = None,
    llm_config=None,
    cache_server_config=None,
    parallel: bool = True,
    force: bool = False,
    index_macros_expanded: bool = True,
    config_header: str = "",
    build_dir_patterns: list[str] | None = None,
) -> str:
    """Index a project: parse translation units, extract symbols, and store to SQLite.

    This is the main entry point for indexing a firmware project.  It reads
    ``compile_commands.json``, filters translation units by their source root,
    parses each with libclang, deduplicates symbols across files, and persists
    the result to the SQLite database at ``db_path``.  Optionally builds
    embeddings, LLM-based symbol analysis, file-level summaries, and the
    method override graph.

    Args:
        compile_commands: Path to ``compile_commands.json`` (generated by
            ``bear``, ``compiledb``, or ``CMAKE_EXPORT_COMPILE_COMMANDS``).
        db_path: Path to the SQLite database file that will store the index.
        source_roots: Directories whose files are considered project code.
            Symbols outside these roots are not indexed.  Auto-detected from
            common directory names and the compile_commands entries when not
            provided.
        exclude_paths: Directories to exclude from indexing (applied after
            ``source_roots`` filtering).
        project_name: Human-readable name for the project (defaults to the
            directory name of ``project_root``).
        index_refs: When True, extract call-graph references (call, ref,
            member, and indirect edges) during AST traversal.
        index_embeddings: When True, generate vector embeddings for all
            definition symbols using Ollama after indexing.
        analyze_symbols: When True, generate structured LLM analysis
            (summary, inputs, outputs) for project symbols via Ollama.
        analyze_overrides: When True (default), build the method override
            graph by matching virtual methods across the inheritance chain.
            Runs entirely on the local index — no LLM needed.
        project_root: Root directory of the project.  Used to resolve relative
            paths and derive defaults.  Defaults to the parent of
            ``compile_commands``.
        project_id: Unique project identifier (auto-derived from
            ``project_root`` when not provided).
        llm_config: Configuration dataclass for Ollama connection (URL,
            model names, enabled flag).  Required when any ``index_*`` or
            ``analyze_*`` option is enabled.
        parallel: Deprecated — kept for backward compatibility.  All
            indexing is now sequential with per-TU write locks so manual
            operations (reindex_file) can interleave via the pause marker.

    Returns:
        The ``config_hash`` string — a content-addressable fingerprint of the
        ``compile_commands.json`` used for staleness detection.
    """
    if project_root is None:
        project_root = compile_commands.parent.resolve()
    else:
        project_root = project_root.resolve()
    if not source_roots:
        source_roots = _detect_source_roots(project_root, compile_commands)
    source_roots = [r.resolve() for r in source_roots if r.exists()]
    if exclude_paths is None:
        exclude_paths = []
    exclude_paths = [p.resolve() for p in exclude_paths]

    if project_id is None:
        project_id = derive_project_id(project_root)
    name = project_name or project_root.name
    # Parse compile_commands.json to discover translation units.  Must
    # happen before config_hash computation so the manifest can be built
    # from the actual TU list.
    units = list(parse_compile_commands(compile_commands))
    units = [u for u in units if u.file.suffix.lower() in _SOURCE_EXTS]
    log.info("TUs to index: %d", len(units))

    # Determine config_hash from manifest.json.  The manifest captures the
    # full structural build identity (files, directories, compiler flags) —
    # more comprehensive than hashing compile_commands.json alone.
    #
    # When a manifest exists, compute the expected structural hash from the
    # current units and compare.  If they match, reuse the stored hash so
    # _update_manifest_after_index() can do an incremental header update.
    # If they differ (compile_commands.json changed), rebuild the preliminary
    # manifest.  When no manifest exists yet (first index), build one.
    from .manifest import build_preliminary, compute_structural_hash
    from .manifest import load as load_manifest

    manifest = load_manifest(db_path.parent)
    expected_hash = compute_structural_hash(
        compile_commands,
        project_root,
        units,
        build_dir_patterns,
    )
    if manifest is not None and manifest.get("config_hash") == expected_hash:
        config_hash = manifest["config_hash"]
    else:
        config_hash = build_preliminary(
            compile_commands,
            db_path.parent,
            project_root,
            units,
            build_dir_patterns,
        )
        # Reload manifest from disk — build_preliminary may have overwritten
        # manifest.json.  The in-memory manifest must reflect what _update_manifest_after_index
        # will find on disk (degraded or fresh), so the early-return guard can detect
        # preliminary (empty source_hash) entries and fall through to regeneration.
        manifest = load_manifest(db_path.parent)

    # Heartbeat for background reindex watchdog.  When the subprocess is
    # stuck (deadlock / hung syscall), this daemon thread stops writing
    # and the watchdog kills the process.  Only active when the heartbeat
    # log path is passed via env var (background reindex).
    _hb_log = os.environ.get("FW_CONTEXT_HEARTBEAT_LOG")
    if _hb_log:
        _hb_stop = threading.Event()

        def _heartbeat() -> None:
            while not _hb_stop.wait(30.0):
                try:
                    with open(_hb_log, "a") as f:
                        f.write(f"{time.strftime('%H:%M:%S')} heartbeat\n")
                except Exception:
                    pass

        _hb_thread = threading.Thread(target=_heartbeat, daemon=True)
        _hb_thread.start()

    log.info("", extra={"phase": f"Indexing {name} ({config_hash[:12]})"})
    log.info("project=%s project_id=%s config_hash=%s", name, project_id, config_hash[:12])

    conn = open_db(db_path)
    # Determine initial manifest verification for this config.
    # For a new config_hash (first index), default to "none".
    # For an existing config, preserve the previous value.
    old_row = conn.execute(
        "SELECT manifest_verification FROM build_configs WHERE config_hash=?",
        (config_hash,),
    ).fetchone()
    initial_manifest_verification = old_row["manifest_verification"] if old_row else "none"
    # Serialize with any concurrent writer (background reindex, daemon)
    with write_lock(db_path.parent, timeout=120.0):
        with transaction(conn):
            upsert_project(conn, project_id, name, str(project_root))
            upsert_build_config(
                conn,
                config_hash,
                project_id,
                str(compile_commands),
                manifest_verification=initial_manifest_verification,
            )

    # Inject user-configured config header when the build system doesn't emit
    # -include flags (custom builds, legacy Makefiles, etc.).
    if config_header:
        ch = project_root / config_header
        if not ch.exists():
            raise RuntimeError(
                f"Configured config_header not found: {ch}\nCheck [index] config_header in .fw-context/config.toml"
            )
        ch_abs = ch.resolve()
        for unit in units:
            unit.clang_args.extend(("-include", str(ch_abs)))

    # Validate that all -include/-imacros referenced files exist BEFORE
    # starting any libclang parsing.  A missing build-generated config
    # header (e.g. BUILD/.../mbed_config.h) would otherwise cause libclang
    # to fail for every TU that includes the SDK, producing a partial index
    # and wasting ~seconds per TU on doomed parse attempts.
    for unit in units:
        validate_include_files(unit.clang_args)

    existing_files = get_file_hashes(conn, config_hash)

    # Pre-build lookup dict for O(1) manifest entry access during Tier 2 checks.
    # *manifest* was loaded above (before config_hash computation) — reuse it.
    manifest_lookup: dict[str, dict] = {}
    if manifest is not None:
        for e in manifest.get("entries", []):
            manifest_lookup[e.get("file", "")] = e

    # Drop FTS5 content-sync triggers before bulk indexing — each symbol
    # INSERT/DELETE/UPDATE would otherwise pay per-row FTS index overhead
    # (~2× write I/O).  The FTS table is rebuilt from scratch in one pass
    # after all TUs are stored.
    drop_fts_triggers(conn)

    total_syms = 0
    total_refs = 0
    skipped = 0
    unchanged = 0
    updated = 0
    acc_parse = 0.0
    acc_lock = 0.0
    acc_write = 0.0
    content_filled = 0
    # Collect headers during tokenization for incremental manifest update.
    # Maps file_path → list of {path, hash, generated} header dicts.
    tu_headers: dict[str, list[dict]] = {}
    t0 = time.monotonic()

    log.info("", extra={"phase": f"Parsing ({len(units)} TUs)"})

    def _wait_if_paused() -> None:
        """If a manual operation requested pause, wait until it finishes.

        The MCP server writes ``<pid>`` to ``reindex.pause`` before a manual
        ``reindex_file`` or ``reset_index``.  This function blocks until the
        pause is lifted or the requesting process dies (stale marker cleanup).

        When the current process wrote the marker itself (e.g. ``fw-context
        index --force`` was invoked from the CLI while a background reindex
        is running), the marker is skipped so the foreground process does not
        pause itself.
        """
        pause_file = db_path.parent / "reindex.pause"
        our_pid = os.getpid()
        while True:
            if not pause_file.exists():
                return
            try:
                content = pause_file.read_text(encoding="utf-8").strip()
                requester_pid = int(content)
            except (OSError, ValueError):
                try:
                    pause_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return
            # Never pause on our own marker — this process created it
            # to signal the background reindex, not to block itself.
            if requester_pid == our_pid:
                return
            try:
                os.kill(requester_pid, 0)
            except OSError:
                # Process dead — clean up stale marker
                try:
                    pause_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return
            time.sleep(1.0)  # Wait, then check again

    # Single sequential loop with per-TU write lock for responsiveness.
    # The lock is acquired and released for each translation unit so that
    # manual operations (reindex_file, reset_index) can interleave via
    # the pause marker mechanism instead of blocking for 60+ seconds.
    #
    # Libclang parsing runs OUTSIDE the write lock — a single TU can take
    # seconds to minutes on large codebases (mbed-os, Zephyr).  Holding the
    # lock during parsing starves other indexers (bg reindex, concurrent
    # ``fw-context index --force``) and causes WriteLockTimeout errors.
    for i, unit in enumerate(units):
        _wait_if_paused()  # Check pause marker before each TU
        fname = unit.file.name
        processed = i + 1

        # ── Phase 1: staleness check + libclang parse (no lock) ──
        check_status, parsed_data, parse_timing, hashes = _check_and_parse_unit(
            unit,
            config_hash,
            project_root,
            source_roots,
            exclude_paths,
            index_refs,
            existing_files,
            force=force,
            manifest=manifest_lookup,
        )

        if check_status == "unchanged":
            unchanged += 1
            file_path_str = _normalize_file_path(str(unit.file.resolve()), project_root)
            rec = existing_files.get(file_path_str)
            file_id = rec.file_id if rec else None
            try:
                current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
            except OSError:
                current_mtime = 0.0

            with write_lock(db_path.parent, timeout=120.0):
                if hashes is not None:
                    # Tier 2: content-hash match — update all hashes
                    source_hash, flags_hash, manifest_entry_hash = hashes
                    content_hash_val = compute_tu_content_hash(source_hash, flags_hash, manifest_entry_hash)
                    if file_id is not None:
                        conn.execute(
                            """UPDATE files SET mtime=?, content_hash=?, source_hash=?,
                               flags_hash=?
                               WHERE id=?""",
                            (current_mtime, content_hash_val, source_hash, flags_hash, file_id),
                        )
                elif file_id is not None:
                    # Tier 1: mtime match — just refresh stored mtime
                    conn.execute(
                        "UPDATE files SET mtime=? WHERE id=?",
                        (current_mtime, file_id),
                    )
                # Fill ifdef-filtered file content via tokenization
                fc, headers = _build_filtered_file_content(
                    conn, unit, config_hash, project_root, build_dir_patterns=build_dir_patterns
                )
                content_filled += fc
                if headers:
                    try:
                        tu_key = str(unit.file.resolve().relative_to(project_root))
                    except ValueError:
                        tu_key = str(unit.file.resolve())
                    tu_headers[tu_key] = headers
            terse = "unchanged (content)" if hashes is not None else "unchanged"
            log.info("[%d/%d] %s: %s", processed, len(units), fname, terse)
            continue

        if check_status == "skipped":
            skipped += 1
            log.info("[%d/%d] %s: skipped", processed, len(units), fname)
            continue

        # ── Phase 2: DB store (inside lock) ──
        with write_lock(db_path.parent, timeout=120.0):
            status, syms, refs, timing, tu_headers_list = _process_unit(
                unit,
                config_hash,
                project_root,
                source_roots,
                exclude_paths,
                index_refs,
                db_path,
                existing_files,
                conn=conn,
                force=force,
                pre_parsed=parsed_data,
                parse_timing=parse_timing,
                hashes=hashes,
                build_dir_patterns=build_dir_patterns,
            )
            if status == "updated":
                updated += 1
                total_syms += syms
                total_refs += refs
                acc_parse += timing[0]
                acc_lock += timing[1]
                acc_write += timing[2]
                if tu_headers_list:
                    try:
                        tu_key = str(unit.file.resolve().relative_to(project_root))
                    except ValueError:
                        tu_key = str(unit.file.resolve())
                    tu_headers[tu_key] = tu_headers_list
                log.info(
                    "[%d/%d] %s: %d syms, %d refs, %.1fs",
                    processed,
                    len(units),
                    fname,
                    syms,
                    refs,
                    sum(timing),
                )
            else:
                skipped += 1
                log.info("[%d/%d] %s: skipped", processed, len(units), fname)

    # Rebuild FTS5 table from the now-complete symbols table — restores
    # full-text search after the triggers were dropped before indexing.
    log.info("", extra={"phase": "FTS5 rebuild"})
    t_fts_start = time.monotonic()
    rebuild_fts(conn)
    rebuild_files_fts(conn)
    t_fts = time.monotonic() - t_fts_start
    log.info("fts5 + files_fts rebuilt  %s", _fmt_dur(t_fts))

    # Clean up file records that no longer have symbols or macros.
    from .db import delete_orphan_files

    orphans = delete_orphan_files(conn, config_hash)
    if orphans:
        log.info("Orphan files cleaned up: %d", orphans)

    elapsed_parse = time.monotonic() - t0
    log.info(
        "Parsing done: %d updated, %d unchanged, %d skipped — parse=%.1fs  lock=%.1fs  write=%.1fs  %s",
        updated,
        unchanged,
        skipped,
        acc_parse,
        acc_lock,
        acc_write,
        _fmt_dur(elapsed_parse),
    )
    if content_filled:
        log.info("ifdef-filtered content: %d files filled", content_filled)

    # ── Refresh header mtimes from manifest ──
    # VCS operations (git checkout/merge) update header file mtimes without
    # changing content.  Refresh stored mtimes so _count_modified_files
    # doesn't report phantom modifications and trigger unnecessary reindexes.
    _refresh_header_mtimes_from_manifest(conn, config_hash, project_root, manifest)

    # ── Update manifest.json ──
    # Rebuild/update the manifest with fresh header hashes after indexing.
    # For incremental runs (no --build), only updated TUs get new entries;
    # unchanged TUs keep their stored entries from the previous manifest.
    _update_manifest_after_index(
        manifest=manifest,
        units=units,
        project_root=project_root,
        db_dir=db_path.parent,
        compile_commands=compile_commands,
        updated_count=updated,
        tu_headers=tu_headers if tu_headers else None,
        build_dir_patterns=build_dir_patterns,
    )

    # Resolve expanded macro values via clang -dM -E (opt-in, best-effort).
    if index_macros_expanded and units:
        log.info("", extra={"phase": "Macro expansion"})
        t_macro = time.monotonic()
        macro_updated = 0
        try:
            from .macros import resolve_and_update

            seen_flags: set[tuple] = set()
            for unit in units:
                flag_key = tuple(sorted(unit.clang_args))
                if flag_key in seen_flags:
                    continue
                seen_flags.add(flag_key)
                try:
                    macro_updated += resolve_and_update(
                        conn,
                        config_hash,
                        unit.clang_args,
                        unit.file.resolve(),
                    )
                except Exception:
                    pass  # best-effort per TU
        except Exception:
            pass  # best-effort
        elapsed_macro = time.monotonic() - t_macro
        if macro_updated:
            log.info("%d values resolved  %s", macro_updated, _fmt_dur(elapsed_macro))
        else:
            log.info("nothing to resolve  %s", _fmt_dur(elapsed_macro))

    # Post-processing — each function handles its own idempotency
    # (returns immediately when data already exists).

    # Compute SDK exclude patterns once (auto-detected from project structure
    # + config exclude_paths).  When analyze_vendor is True, skip exclusion
    # so vendor/SDK code is also analyzed.
    if llm_config is not None and llm_config.analyze_vendor:
        exclude_like: list[str] = []
    else:
        config_exclude_strs = [
            str(p.relative_to(project_root)) for p in exclude_paths if p.is_relative_to(project_root)
        ]
        exclude_like = _detect_sdk_exclude_like(project_root, config_exclude_strs)

    # Embedding generation (opt-in)
    if index_embeddings and llm_config is not None and llm_config.enabled:
        if force:
            conn.execute(
                "DELETE FROM embeddings WHERE symbol_id IN (SELECT id FROM symbols WHERE config_hash = ?)",
                (config_hash,),
            )
            try:
                conn.execute(
                    "DELETE FROM embeddings_vec WHERE symbol_id IN (SELECT id FROM symbols WHERE config_hash = ?)",
                    (config_hash,),
                )
            except Exception:
                pass  # sqlite-vec table may not exist for legacy indexes
            conn.commit()
        log.info("", extra={"phase": "Embeddings"})
        t_emb = time.monotonic()
        _build_embeddings(conn, config_hash, llm_config, db_path.parent)
        conn.commit()
        log.info("done  %s", _fmt_dur(time.monotonic() - t_emb))

    # LLM analysis generation (opt-in)
    if analyze_symbols and llm_config is not None and llm_config.enabled:
        # Create CacheClient from cache_server_config if available
        cc = None
        if cache_server_config is not None and cache_server_config.url:
            try:
                from ..cache_client import CacheClient

                cc = CacheClient(
                    url=cache_server_config.url,
                    token=cache_server_config.token,
                    force=cache_server_config.force,
                    batch_size=cache_server_config.batch_size,
                )
            except Exception as e:
                log.warning("Failed to create CacheClient: %s", e)
        else:
            log.info(
                "Remote LLM cache server not configured — all symbols will be analyzed locally. "
                "Run 'fw-context cache-remote-init' to configure."
            )

        if force:
            conn.execute(
                "DELETE FROM llm_analysis WHERE symbol_id IN (SELECT id FROM symbols WHERE config_hash = ?)",
                (config_hash,),
            )
            conn.commit()
        log.info("", extra={"phase": f"LLM Analysis ({llm_config.model})"})
        t_llm = time.monotonic()
        _build_llm_analysis(
            conn,
            config_hash,
            llm_config,
            db_path.parent,
            exclude_like=exclude_like,
            cache_client=cc,
            retry_unparseable=True,
        )
        if cc:
            cc.close()
        conn.commit()
        log.info("done  %s", _fmt_dur(time.monotonic() - t_llm))

    # Method override tracking (post-processing, no LLM needed)
    if analyze_overrides:
        if force:
            conn.execute("DELETE FROM overrides WHERE config_hash = ?", (config_hash,))
            conn.commit()
        log.info("", extra={"phase": "Override graph"})
        t_ov = time.monotonic()
        _build_overrides(conn, config_hash, db_path.parent)
        conn.commit()
        log.info("done  %s", _fmt_dur(time.monotonic() - t_ov))

    # PageRank computation (post-processing, requires reference index)
    if index_refs:
        if force:
            conn.execute("UPDATE symbols SET pagerank = 0.0 WHERE config_hash = ?", (config_hash,))
            conn.execute("DELETE FROM hotspot_cache WHERE config_hash = ?", (config_hash,))
            conn.commit()
        log.info("", extra={"phase": "PageRank"})
        t_pr = time.monotonic()
        _build_pagerank(conn, config_hash)
        conn.commit()
        log.info("done  %s", _fmt_dur(time.monotonic() - t_pr))

        log.info("", extra={"phase": "Hotspot cache"})
        t_hs = time.monotonic()
        _build_hotspot_cache(conn, config_hash)
        conn.commit()
        log.info("done  %s", _fmt_dur(time.monotonic() - t_hs))

    # Manifest verification — "full" when we have a manifest.json, "none" otherwise.
    # The manifest is generated during `fw-context index --build` and provides
    # header dependency hashes without needing .d files.
    manifest_path = db_path.parent / "manifest.json"
    manifest_verification: str = "full" if manifest_path.exists() else "none"
    with transaction(conn):
        upsert_build_config(
            conn, config_hash, project_id, str(compile_commands), manifest_verification=manifest_verification
        )

    elapsed = time.monotonic() - t0
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    # Stamp schema version — marks the index as current (get_active_build
    # compares PRAGMA user_version against CURRENT_SCHEMA_VERSION).
    # PRAGMA does not support bound parameters; CURRENT_SCHEMA_VERSION is
    # an integer constant — no injection risk.
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    log.info("", extra={"phase": f"Done — {total_syms} symbols, {total_refs} refs, {_fmt_dur(elapsed)}"})
    log.info("%d updated, %d unchanged, %d skipped  config_hash=%s", updated, unchanged, skipped, config_hash[:12])
    return config_hash
