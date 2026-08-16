"""Embedding generation extracted from runner.py.

WHY separate module: embedding generation is the most memory-intensive phase
of indexing — it loads sentence-transformer models that consume gigabytes of
VRAM/RAM.  Isolating it from the main runner allows the model to be garbage-
collected after the embedding phase completes, freeing memory for subsequent
analysis phases.

Handles embedding model key generation, body chunking/truncation,
orphaned compile_commands artifact cleanup, and the main embedding
build phase.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from pathlib import Path

import httpx

from ..config.settings import DESCRIPTION_VERSION
from ..llm.embedder_factory import get_embedder
from ..utils import SAFE_EXCEPT, is_fatal
from .db import open_db
from .db._embeddings import _vec_to_blob

log = logging.getLogger(__name__)


def _embed_model_key(embed_model: str, embed_bodies: bool) -> str:
    """Return the cache-disambiguating model key including the description version."""
    version = DESCRIPTION_VERSION if embed_bodies else "desc-v1"
    return f"{embed_model}:{version}"


def _embed_content_hash(r) -> str:
    """Content-addressable hash of the fields that feed an embedding description.

    Mirrors the description assembly in :func:`_build_embeddings`: any change
    to name, qualified name, kind, file path, signature, docstring, LLM
    summary, or body must invalidate the embedding so the symbol is
    re-embedded on the next index run.
    """

    raw = "|".join([
        r["name"] or "",
        r["qualified_name"] or "",
        r["kind"] or "",
        r["file_path"] or "",
        r["signature"] or "",
        r["docstring"] or "",
        r["summary"] or "",
        r["source"] or "",
    ])
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _chunk_body(source: str, max_chars: int) -> list[str]:
    """Split a C/C++ function body into semantic chunks at statement boundaries.

    WHY chunking: embedding models have a fixed maximum context window
    (typically 512-1024 tokens).  Large function bodies (e.g. a 2000-line
    state machine) must be split into overlapping or contiguous chunks.
    Semantic splitting at brace/semicolon boundaries preserves statement
    integrity — each chunk is a self-contained semantic unit rather than
    an arbitrary character-range window.


    Split points (highest to lowest priority):
    1. Closing brace ``}`` at end of line (end of block)
    2. Semicolon ``;`` at end of line (end of statement)
    3. Blank line (paragraph boundary)
    4. Line boundary (fallback — any ``\\n``)

    Never splits inside a brace block — the closing brace is always the
    last line of a chunk.

    **Limitation:** does not parse string literals or comments — a semicolon
    inside ``printf("hello; world")`` or ``// comment;`` may trigger an
    early split.  This is acceptable because chunks are used only for
    embedding generation (not code transformation), so suboptimal chunk
    boundaries degrade embedding quality locally without causing functional
    errors.

    Returns list of body chunks (1 chunk when *source* <= *max_chars*).
    """
    if len(source) <= max_chars:
        return [source]

    lines = source.split("\n")
    chunks: list[str] = []

    # Find indices of preferred split points
    split_prio: dict[int, int] = {}  # line_idx → priority (1=best, 4=worst)
    for idx, raw in enumerate(lines):
        stripped = raw.rstrip()
        if stripped.endswith("}"):
            split_prio[idx] = 1  # closing brace
        elif stripped.endswith(";"):
            split_prio[idx] = 2  # semicolon
        elif stripped == "":
            split_prio[idx] = 3  # blank line
        else:
            split_prio[idx] = 4  # arbitrary line

    chunk_start = 0
    while chunk_start < len(lines):
        chunk_end = chunk_start
        current_len = 0

        # Find the furthest line that fits within max_chars
        # (trading off: we'd rather go a bit past max_chars than split mid-statement)
        last_split_idx = chunk_start
        last_split_prio = 4  # lower = better

        for i in range(chunk_start, len(lines)):
            line_len = len(lines[i]) + 1  # +1 for newline
            if i > chunk_start and current_len + line_len > max_chars:
                # Would overflow — split at the last good split point
                if last_split_idx > chunk_start:
                    chunk_end = last_split_idx
                else:
                    chunk_end = i  # no split point found, hard cut
                break
            current_len += line_len
            if split_prio[i] <= last_split_prio and i + 1 < len(lines):
                last_split_idx = i + 1  # split AFTER this line (good boundary)
                last_split_prio = split_prio[i]
            chunk_end = i + 1  # tentatively include this line
        else:
            chunk_end = len(lines)  # all remaining lines fit

        chunk = "\n".join(lines[chunk_start:chunk_end])
        # Safety: head+tail truncation for chunks that still exceed max_chars
        # (happens when a single long line has no split boundary)
        if len(chunk) > max_chars:
            chunk = _truncate_body(chunk, max_chars)
        chunks.append(chunk)
        chunk_start = chunk_end

    return chunks if chunks else [source]


def _truncate_body(body: str, max_chars: int, *, head_frac: float = 0.6) -> str:
    """Truncate a body string with head+tail split when it exceeds *max_chars*."""
    if len(body) <= max_chars:
        return body
    head = int(max_chars * head_frac)
    tail = max_chars - head - 8  # 8 for the separator
    if tail < 0:
        return body[:max_chars]
    return body[:head] + "\n// ...\n" + body[-tail:]


def _fmt_dur(seconds: float) -> str:
    """Human-readable duration: ``87ms``, ``1.2s``, ``12s``."""
    if seconds < 0.1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


# REMOVED: _COMMON_SOURCE_DIRS, _COMMON_OS_DIRS — replaced by is_project + vendor_patterns
# REMOVED: _detect_source_roots — no longer needed; all files in compile_commands.json are indexed
# REMOVED: _add_external_root — only called by _detect_source_roots, removed together


def _cleanup_orphaned_cc_artifacts(db_path: Path, project_id: str) -> int:
    """Delete ``compile_commands.<hash>.json`` files that don't match any active build_config.

    These files are written by :func:`compute_config_hash` as debug artifacts
    and can become orphaned when ``fw-context index`` is interrupted before
    the end-of-run cleanup.  Call this at the START of a new index run so
    orphans from a previous crashed run are cleaned up immediately.

    Returns the number of deleted files.
    """
    cc_dir = Path.home() / ".fw-context" / "index" / project_id
    if not cc_dir.is_dir():
        return 0

    # Collect active config_hashes from the database — DB may not exist yet
    # on first index or after reset_index.
    active_hashes: set[str] = set()
    if db_path.exists():
        try:
            conn = open_db(db_path, skip_integrity_check=True)
            rows = conn.execute("SELECT config_hash FROM build_configs WHERE project_id = ?", (project_id,)).fetchall()
            conn.close()
            active_hashes = {r["config_hash"] for r in rows}
        except SAFE_EXCEPT as e:
            if is_fatal(e):
                raise
            pass  # non-fatal — continue  # DB may be corrupt or schema not yet initialized

    deleted = 0
    for f in cc_dir.iterdir():
        if not f.is_file():
            continue
        name = f.name
        if not name.startswith("compile_commands.") or not name.endswith(".json"):
            continue
        # Extract hash from filename: compile_commands.<64-char-hex>.json
        hash_part = name[len("compile_commands."): -len(".json")]
        if len(hash_part) != 64 or not all(c in "0123456789abcdef" for c in hash_part):
            continue
        if hash_part not in active_hashes:
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass

    if deleted:
        log.info("Cleaned up %d orphaned compile_commands artifacts in %s", deleted, cc_dir)
    return deleted


def _build_embeddings(
    conn: sqlite3.Connection, config_hash: str, llm_config, db_dir: Path,
    *,
    symbol_ids: list[int] | None = None,
) -> None:
    """Generate and store vector embeddings for definition symbols.

    Selects all function, method, constructor, destructor, class, struct,
    and union definitions from the current build, builds a human-readable description
    for each (combining file path, class, name, signature, docstring, and
    LLM summary), and produces embeddings via Ollama.

    When *symbol_ids* is provided, only those symbols are cleaned and re-embedded
    (used by ``reindex_file`` for fast per-file updates).  When None, all
    definition symbols are processed (full index rebuild).

    Descriptions are processed in chunks of 100 to stay within model context
    limits.  Each batch is stored in two tables simultaneously:

    * ``upsert_embeddings`` — legacy BLOB table (backward compatibility).
    * ``upsert_embeddings_vec`` — ``sqlite-vec`` vec0 table for KNN search.

    When Ollama is unreachable or returns an error for a batch, a warning is
    logged and the batch is skipped (non-fatal — remaining batches continue).
    """



    # Suppress httpx INFO logs (one per batch — noisy during embedding)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    embedder = get_embedder(llm_config)

    from ..llm.auto_model import _model_installed
    from ..llm.ollama import OllamaEmbedder
    if isinstance(embedder, OllamaEmbedder):
        try:
            resp = httpx.get(llm_config.ollama_url.rstrip("/") + "/api/tags", timeout=5.0)
            resp.raise_for_status()
        except SAFE_EXCEPT as e:
            if is_fatal(e):
                raise
            log.warning("Ollama not reachable — skipping embedding generation")
            return
        # Model-not-installed guard for reindex_file: warn and skip.
        # The embedder's HTTP 404 detection + auto-pull handles full indexes —
        # no separate pull logic needed here.
        if symbol_ids is not None and not _model_installed(llm_config.ollama_url, llm_config.embed_model):
            log.warning("Embedding model not installed. Run manual reindex to pull it.")
            return
    model = _embed_model_key(embedder.name, True)

    # ── Incremental re-embedding (Phase 0) ──
    # Skip symbols whose embedding description content did not change since
    # the last successful embedding pass.  Without this, every `fw-context
    # index` run re-embeds ALL definition symbols even when nothing changed
    # (the embeddings step has no idempotency check otherwise).
    #
    # Safety gate: only skip when a previous embedding pass stored a known
    # embedding_dim for this build config.  On the very first run (no stored
    # dim) every symbol is embedded so the vec0 table is (re)built fully and
    # the dimension is recorded.  When the dimension later changes (model
    # switched) the skip is disabled, vec0 is recreated, and everything is
    # re-embedded.
    existing_hashes: dict[int, str] = {}
    stored_dim: int | None = None
    try:
        _dim_row = conn.execute(
            "SELECT embedding_dim FROM build_configs WHERE config_hash = ?",
            (config_hash,),
        ).fetchone()
        if _dim_row is not None:
            stored_dim = _dim_row["embedding_dim"]
    except sqlite3.OperationalError:
        pass
    incremental_ok = stored_dim is not None
    if incremental_ok:
        try:
            _hash_rows = conn.execute(
                """SELECT e.symbol_id, e.content_hash
                   FROM embeddings e
                   JOIN symbols s ON s.id = e.symbol_id
                   WHERE e.model = ? AND s.config_hash = ? AND e.chunk_index = 0""",
                (model, config_hash),
            ).fetchall()
            existing_hashes = {r["symbol_id"]: r["content_hash"] for r in _hash_rows}
        except sqlite3.OperationalError:
            pass  # content_hash column missing (pre-migration) — fall back to full embed

    # ── Phase 1: SELECT symbols to embed (read-only) ──
    if symbol_ids:
        id_placeholders = ",".join("?" * len(symbol_ids))
        rows = conn.execute(
            f"""SELECT s.id, s.name, s.qualified_name, s.kind, s.file_path,
                       s.signature, s.is_definition, s.docstring, s.summary,
                       s.source, s.is_project
                FROM symbols s
                WHERE s.config_hash = ?
                  AND s.is_definition = 1
                  AND s.id IN ({id_placeholders})
                  AND s.kind IN ('function', 'method', 'constructor', 'destructor',
                                'class', 'struct', 'union', 'typedef', 'enum', 'varglobal')
                ORDER BY CASE WHEN s.docstring IS NOT NULL AND LENGTH(s.docstring) > 30
                          THEN 0 ELSE 1 END""",
            (config_hash, *symbol_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.id, s.name, s.qualified_name, s.kind, s.file_path,
                      s.signature, s.is_definition, s.docstring, s.summary,
                      s.source, s.is_project
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.is_definition = 1
                 AND s.kind IN ('function', 'method', 'constructor', 'destructor',
                               'class', 'struct', 'union', 'typedef', 'enum', 'varglobal')
               ORDER BY CASE WHEN s.docstring IS NOT NULL AND LENGTH(s.docstring) > 30
                         THEN 0 ELSE 1 END""",
            (config_hash,),
        ).fetchall()
    if not rows:
        log.info("No definition symbols to embed (model=%s)", model)
        return

    # Save full row set before incremental filter — needed for dim-change restart.
    _all_rows = rows
    # Keep only symbols whose content changed or which lack an embedding for
    # this model key.  When incremental_ok is False (no stored dim yet) the
    # filter is a no-op — everything is embedded.
    if incremental_ok:
        to_embed = [
            r for r in rows if existing_hashes.get(r["id"]) != _embed_content_hash(r)
        ]
        skipped = len(rows) - len(to_embed)
        if skipped:
            log.info(
                "Embeddings: %d/%d symbols unchanged — skipped (model=%s)",
                skipped, len(rows), model,
            )
        rows = to_embed
        if not rows:
            log.info("All symbols already embedded (model=%s) — nothing to do", model)
            return

    target_ids = sorted({r["id"] for r in rows})
    log.info(
        "Embeddings: %d symbols to embed (model=%s, %s)",
        len(target_ids), model,
        "full rebuild" if not incremental_ok else "incremental",
    )

    # Build contextual descriptions — flowing sentences instead of token lists.
    # Anthropic Contextual Retrieval: chunk-specific context as sentences improves
    # embedding recall by -35% failures vs token-boundary lists.
    body_kinds = frozenset({"function", "method", "constructor", "destructor"})
    max_body_chars = embedder.max_tokens * 3
    MAX_EMBED_BATCH_SYMBOLS = 10

    desc_rows: list[tuple[dict, int, str]] = []

    def _rebuild_desc_rows() -> None:
        """(Re)build description rows from the current ``rows`` list.

        Clears and refills ``desc_rows`` — called once before Phase 2 and
        again when a dim change forces a full rebuild.
        """
        desc_rows.clear()
        for r in rows:
            fp = (r["file_path"] or "").replace("\\", "/")
            path = ""
            module = ""
            if "/" in fp:
                *dirs, fname = fp.split("/")
                path = "/".join(dirs[-2:])
                module = dirs[-1] if dirs else ""
            qname = r["qualified_name"] or ""
            name = r["name"] or ""
            kind = r["kind"] or ""
            class_ = "::".join(qname.split("::")[:-1]) if "::" in qname else ""
            sig = r["signature"] or ""
            is_vendor = r["is_project"] == 0
            doc = ""
            if not is_vendor:
                doc = (r["docstring"] or "").strip()
                if doc and len(doc) > 20:
                    doc = doc[:150]
            llm = (r["summary"] or "").strip()
            if llm:
                llm = llm[:200]

            clauses = [f"{kind} {name}"]
            if class_:
                clauses.append(f"in class {class_}")
            if module:
                clauses.append(f"in module {module}")
            if path:
                clauses.append(f"at {path}")
            if fp:
                clauses.append(f"file: {fp}")
            context_prefix = ", ".join(clauses) + "."

            sig_line = f" Signature: {sig}." if sig else ""

            extra = []
            if doc:
                extra.append(doc)
            if llm:
                extra.append(llm)
            extra_text = " ".join(extra) if extra else ""

            body_chunks: list[str] = []
            if kind in body_kinds:
                source = (r["source"] or "").strip()
                if source:
                    body_chunks = _chunk_body(source, max_body_chars)

            if not body_chunks:
                body_chunks = [""]

            n_chunks = len(body_chunks)
            for ci, body in enumerate(body_chunks):
                body_text = ""
                if body:
                    if n_chunks > 1:
                        body_text = f"\n\nBody [part {ci + 1}/{n_chunks}]:\n{body}"
                    else:
                        body_text = f"\n\nBody:\n{body}"
                desc_rows.append((
                    r, ci,
                    f"{context_prefix}{sig_line} {extra_text}{body_text}".rstrip() + "\n",
                ))

    _rebuild_desc_rows()

    # ── Phase 2: Generate and store embeddings incrementally ──
    # Write in transactions of up to CHUNK_SYMBOLS (5000) symbols to avoid
    # buffering all embeddings in RAM (OOM risk on large projects).
    # Each chunk is atomic (DELETE old + INSERT new for those symbols).
    from .db import upsert_embeddings, upsert_embeddings_vec, write_lock
    from .db._connection import transaction

    CHUNK_SYMBOLS = 5000
    total = 0
    embedding_dim: int | None = None
    chunk_blob: list[tuple] = []
    chunk_vec: list[tuple] = []
    first_chunk = True

    def _flush_chunk() -> None:
        """Write accumulated batch to DB in a single transaction."""
        nonlocal first_chunk, total
        if not chunk_blob:
            return
        with write_lock(db_dir, timeout=30.0):
            with transaction(conn):
                if first_chunk:
                    # DELETE old embeddings only for the symbols being (re)embedded.
                    # Unchanged symbols keep their existing rows — this is what
                    # makes repeated `fw-context index` runs incremental instead of
                    # re-embedding every definition symbol.
                    #
                    # Batch deletes in chunks of 500 to stay under SQLite's
                    # SQLITE_MAX_VARIABLE_NUMBER limit (default 999).  A single
                    # DELETE with 5000+ parameters would fail silently.
                    _BATCH = 500
                    for _batch_start in range(0, len(target_ids), _BATCH):
                        _batch_ids = target_ids[_batch_start : _batch_start + _BATCH]
                        _phs = ",".join("?" * len(_batch_ids))
                        conn.execute(
                            f"DELETE FROM embeddings WHERE model = ? AND symbol_id IN ({_phs})",
                            (model, *_batch_ids),
                        )
                        try:
                            conn.execute(
                                f"DELETE FROM vec_symbols WHERE config_hash = ? AND symbol_id IN ({_phs})",
                                (config_hash, *_batch_ids),
                            )
                        except sqlite3.OperationalError:
                            pass
                    first_chunk = False
                upsert_embeddings(conn, chunk_blob)
                try:
                    upsert_embeddings_vec(conn, chunk_vec)
                except SAFE_EXCEPT as e:
                    if is_fatal(e):
                        raise
                    log.warning("vec0 batch insert failed (sqlite-vec may not be loaded): %s", e)
                total += len(chunk_blob)

    _phase2_idx = 0
    batch_num = 0
    while _phase2_idx < len(desc_rows):
        batch: list[tuple] = []
        seen: set[int] = set()
        while _phase2_idx < len(desc_rows):
            r = desc_rows[_phase2_idx][0]
            if r["id"] not in seen:
                if len(seen) >= MAX_EMBED_BATCH_SYMBOLS:
                    break
                seen.add(r["id"])
            batch.append(desc_rows[_phase2_idx])
            _phase2_idx += 1
        batch_num += 1
        chunk_descs = [d for _, _, d in batch]
        _batch_chars = sum(len(d) for d in chunk_descs)
        t0 = time.monotonic()
        embs = None
        _last_exc = None
        for _attempt in range(3):
            try:
                embs = embedder.embed_documents(chunk_descs)
                break
            except SAFE_EXCEPT as _e:
                _last_exc = _e
                if is_fatal(_e):
                    raise
                if _attempt < 2:
                    time.sleep(2)
        if embs is None:
            elapsed = time.monotonic() - t0
            log.warning("[%d] embedding batch failed %s: %s", batch_num, _fmt_dur(elapsed), _last_exc)
            continue

        if embedding_dim is None and embs:
            embedding_dim = len(embs[0])
            log.info("Embedding dimension detected: %d (model=%s)", embedding_dim, model)
            from .db import init_vec_table

            dim_changed = stored_dim is not None and stored_dim != embedding_dim
            if dim_changed:
                log.info("Embedding dimension changed %d → %d — forcing full rebuild", stored_dim, embedding_dim)
                try:
                    init_vec_table(conn, embedding_dim, recreate=True)
                except SAFE_EXCEPT as e:
                    if is_fatal(e):
                        raise
                    log.warning("vec0 table recreation failed (non-fatal): %s", e)
                rows = _all_rows
                target_ids.clear()
                target_ids.extend(sorted({r["id"] for r in rows}))
                chunk_blob.clear()
                chunk_vec.clear()
                first_chunk = True
                total = 0
                _rebuild_desc_rows()
                batch_num = 0
                _phase2_idx = 0
                log.info("Restarting embeddings with new dimension %d...", embedding_dim)
                continue
            else:
                try:
                    init_vec_table(conn)
                except SAFE_EXCEPT as e:
                    if is_fatal(e):
                        raise
                    log.warning("vec0 table ensure failed (non-fatal): %s", e)

        blob_rows = [
            (r["id"], ci, _vec_to_blob(emb), model, _embed_content_hash(r))
            for (r, ci, _), emb in zip(batch, embs, strict=True)
        ]
        vec_rows = [
            (r["id"], ci, config_hash, emb)
            for (r, ci, _), emb in zip(batch, embs, strict=True)
        ]
        chunk_blob.extend(blob_rows)
        chunk_vec.extend(vec_rows)

        elapsed = time.monotonic() - t0
        log.info("[%d] %d symbols (%d chars) embedded %s", batch_num, len(batch), _batch_chars, _fmt_dur(elapsed))

        if len(chunk_blob) >= CHUNK_SYMBOLS:
            _flush_chunk()
            chunk_blob.clear()
            chunk_vec.clear()

    # Flush remaining
    _flush_chunk()

    if embedding_dim is not None:
        with write_lock(db_dir, timeout=30.0):
            with transaction(conn):
                conn.execute(
                    "UPDATE build_configs SET embedding_dim = ? WHERE config_hash = ?",
                    (embedding_dim, config_hash),
                )

    # Prune embeddings with non-versioned model keys (leftover from
    # older fw-context versions that used bare model names without
    # the :desc-v<N> version suffix).  Must run after new embeddings
    # are stored so the DELETE only targets stale rows.
    try:
        with write_lock(db_dir, timeout=30.0):
            with transaction(conn):
                conn.execute(
                    "DELETE FROM embeddings WHERE model NOT LIKE '%:desc-v%'"
                    " AND symbol_id IN (SELECT id FROM symbols WHERE config_hash = ?)",
                    (config_hash,),
                )
    except sqlite3.OperationalError:
        pass

    log.info("Embeddings stored: %d embedding rows (model=%s)", total, model)


# ═══════════════════════════════════════════════════════════════
# SECTION: LLM analysis (→ llm_analysis.py)
# ═══════════════════════════════════════════════════════════════

