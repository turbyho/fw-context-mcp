"""semantic_search MCP tool."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated

from pydantic import Field

from fw_context_mcp.config import derive_project_id, load as load_config
from fw_context_mcp.indexer.db import _cosine_sim, get_active_config
from fw_context_mcp.llm.ollama import check_setup
from fw_context_mcp.utils import abs_path, resolve_project_root
from fw_context_mcp.mcp.shared.context import _db_path, _open_db_safe
from fw_context_mcp.mcp.shared.fallback import _fallback_to_search_code, _fallback_to_search_code_inner
from fw_context_mcp.mcp.shared.stale import _with_stale_recovery
from ._search_fallbacks import _fmt_symbol_rows

async def semantic_search(
    query: Annotated[str, Field(description="Natural language description, 5-15 words. E.g. 'parcel locker state machine' or 'how does the modem connect?'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    threshold: Annotated[float, Field(description="Minimum cosine similarity (0.0-1.0). Default 0.60. Use 0.55 for exploratory, 0.50 for broad search.")] = 0.60,
    limit: Annotated[int, Field(description="Maximum results (default 20, max 100).")] = 20,
) -> list[dict]:
    """Semantic search using pre-computed libclang symbol embeddings. Finds
    symbols by meaning, not by text — matches concepts even when query
    words don't appear literally in the code. Uses cosine similarity over
    variable-dimension embeddings generated during ``fw-context index``.
    Dimensions vary by model: mxbai-embed-large → 1024,
    qwen3-embedding → 4096.

    **When to prefer over search_code:** When you're describing a *concept*
    rather than searching for a known keyword.  Examples:
    - ``"parcel locker state"`` finds door-state and shipment methods even
      though "parcel" and "locker" don't appear in their names.
    - ``"cell modem"`` finds ``_socket_t`` and ``ModemMsg*`` classes.
    - ``"delivery box"`` finds ``set_shipment`` and ``get_zrtdata``.
    - ``"power consumption"`` finds ``get_load_power`` and INA260 class.

    **When to prefer search_code instead:** When you know the exact keyword
    or symbol name (``"fram_write"``, ``"cbor encode"``).  FTS5 is faster
    and more precise for lexical matches.

    **Threshold guidance (mxbai-embed-large model):**
    - ``0.50`` — exploratory: more results, lower precision
    - ``0.55`` — balanced (~1000 results)
    - ``0.60`` — precise: ~175 avg, high precision (default)
    - ``0.65`` — strict: few results, may miss relevant symbols

    **Source-aware ranking:** Project code boosted 1.2×, library code
    1.1×, vendored SDK code 0.85×.

    **Requires an LLM** with an embedding model.
    Falls back to ``search_code`` with a warning if the LLM is unavailable.

    Read-only. No side effects.

    Args:
        query: Natural language description of what you're looking for.
               Be specific — 5–15 words works best.
        project_root: Project root. Auto-detected if omitted.
        threshold: Minimum cosine similarity (0.0-1.0). Default 0.60.
        limit: Maximum number of results (default 20, max 100).

    Returns:
        list of dicts, each with: name, qualified_name, kind, file, line,
        is_definition, signature, docstring, plus ``_similarity`` (cosine
        similarity score) and ``_method`` (``"embedding"`` or
        ``"search_code_fallback"``).
    """
    import asyncio

    try:
        root = resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

        limit = min(limit, 100)
        threshold = max(0.0, min(1.0, threshold))

        # Check Ollama availability
        cfg = load_config(project_root=root)
        if not cfg.llm.enabled:
            return _fallback_to_search_code(
                root, db_path, query, limit,
                warning="LLM is disabled in config. "
                        "Enable it with `[llm] enabled = true` to use semantic search.",
            )

        try:
            setup = check_setup(cfg.llm)
        except Exception:
            setup = {"ollama_running": False}

        if not setup.get("ollama_running"):
            return _fallback_to_search_code(
                root, db_path, query, limit,
                warning="LLM is not running. Start it to use semantic search.",
            )

        # Generate query embedding
        try:
            from fw_context_mcp.llm.embedder_factory import get_embedder

            embedder = get_embedder(cfg.llm)
            query_embs = await asyncio.to_thread(
                embedder.embed_queries, [query]
            )
            query_vec = query_embs[0]
        except Exception as e:
            log.warning("semantic_search: Ollama embed failed: %s", e)
            return _fallback_to_search_code(
                root, db_path, query, limit,
                warning=f"LLM embedding failed: {e}. "
                        "Showing lexical search results instead.",
            )

        # KNN search via sqlite-vec vec0 table (O(log n) instead of O(n))
        def _do_semantic(c: sqlite3.Connection, config_hash: str) -> list[dict]:
            from fw_context_mcp.indexer.db._embeddings import search_similar_vec

            # Check if vec0 table has embeddings for this config
            vec_total = c.execute(
                "SELECT COUNT(*) FROM vec_symbols WHERE config_hash = ?",
                (config_hash,),
            ).fetchone()[0]

            if vec_total == 0:
                return _fallback_to_search_code_inner(
                    c, root, query, config_hash, limit,
                    warning="No vector embeddings found. "
                            "Run `fw-context index --embeddings` to generate them.",
                )

            # KNN search via vec0
            vec_rows = search_similar_vec(c, query_vec, config_hash, threshold=threshold, limit=limit * 10)

            if not vec_rows:
                return [{
                    "warning": f"No symbols matched with similarity > {threshold}. "
                                "Try lowering the threshold or rephrasing the query.",
                    "hint": "Use search_code for lexical/keyword search.",
                }]

            # Post-process: filter is_definition, get is_project, apply source boost
            sym_ids_all = [r["symbol_id"] for r in vec_rows]
            placeholders_all = ",".join("?" * len(sym_ids_all))
            sym_rows = c.execute(
                f"SELECT id, is_project FROM symbols WHERE id IN ({placeholders_all}) AND is_definition = 1",
                sym_ids_all,
            ).fetchall()
            sym_proj: dict[int, int] = {r["id"]: r["is_project"] for r in sym_rows}

            scored: list[tuple[float, int, float]] = []
            for r in vec_rows:
                sid = r["symbol_id"]
                if sid not in sym_proj:
                    continue
                raw_sim = 1.0 - r["distance"]  # convert cosine distance → similarity
                boost = 1.2 if sym_proj[sid] == 1 else 0.85
                score = raw_sim * boost
                scored.append((score, sid, raw_sim))

            if not scored:
                return [{
                    "warning": f"No definition symbols matched. "
                                "Try lowering the threshold or rephrasing the query.",
                    "hint": "Use search_code for lexical/keyword search.",
                }]

            scored.sort(key=lambda x: -x[0])
            scored_top = scored[:limit]

            top = [(sid, (score, sim)) for score, sid, sim in scored_top]
            sym_ids = [sid for sid, _ in top]
            placeholders = ",".join("?" * len(sym_ids))
            sym_rows = c.execute(
                f"""SELECT * FROM symbols
                    WHERE config_hash = ? AND id IN ({placeholders})
                    ORDER BY CASE id {' '.join(f'WHEN {i} THEN {j}' for j, i in enumerate(sym_ids))} END""",
                (config_hash, *sym_ids),
            ).fetchall()

            sym_map = {r["id"]: r for r in sym_rows}
            sim_map = {sid: info[1] for sid, info in top}  # symbol_id → raw_sim

            results: list[dict] = []
            for sid in sym_ids:
                sr = sym_map.get(sid)
                if sr is None:
                    continue
                d = _symbol_row_to_dict(
                    sr, root,
                    _similarity=round(sim_map[sid], 4),
                    _method="embedding",
                )
                results.append(d)

            # Apply reranker when configured — improves precision at
            # the cost of a small latency increase (cross-encoder pass).
            if cfg.llm.reranker_model and results:
                try:
                    from fw_context_mcp.search.reranker import get_reranker
                    reranker = get_reranker(cfg.llm.reranker_model)
                    if reranker is not None:
                        results = reranker.rank(
                            query, results,
                            min(cfg.index.rerank_top_k, len(results)),
                        )
                except Exception as e:
                    log.warning("Reranker failed, returning unranked results: %s", e)

            return results

        return await asyncio.to_thread(_with_stale_recovery, root, db_path, _do_semantic)

    except Exception as e:
        log.exception("semantic_search failed: %s", e)
        return [{"error": f"semantic_search failed: {e}"}]

