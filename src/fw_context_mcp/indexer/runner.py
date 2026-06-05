"""Index runner: parse compile_commands.json, extract symbols, store to SQLite.

Uses ``indexer/ops.py`` for the shared "parse TU → store symbols" loop so
that runner, reindex_file, and auto-reindex all use the same code path.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..config.settings import derive_project_id
from ..utils import MTIME_TOLERANCE_S
from .compile_commands import _SOURCE_EXTS
from .compile_commands import parse as parse_compile_commands
from .config_hash import compute as compute_config_hash
from .db import (
    get_file_mtimes,
    open_db,
    transaction,
    upsert_build_config,
    upsert_project,
)
from .ops import store_symbols_for_unit

log = logging.getLogger(__name__)

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
            try:
                rel = unit.file.resolve().relative_to(project_root)
                top = project_root / rel.parts[0]
                if top.is_dir() and top not in seen:
                    roots.append(top)
                    seen.add(top)
            except ValueError:
                pass
    except Exception:
        pass
    if not roots:
        roots = [project_root]
        log.info("No source directories detected, falling back to project root")
    log.info("Auto-detected source roots: %s", [str(r) for r in roots])
    return roots


def _build_embeddings(conn, config_hash: str, llm_config) -> None:
    """Generate and store embeddings for all definition symbols in a build."""
    import httpx

    from ..llm.ollama import call_ollama_embed
    from .db import _vec_to_blob, upsert_embeddings, upsert_embeddings_vec

    try:
        resp = httpx.get(llm_config.ollama_url.rstrip("/") + "/api/tags", timeout=5.0)
        resp.raise_for_status()
    except Exception:
        log.warning("Ollama not reachable — skipping embedding generation")
        return

    with transaction(conn):
        rows = conn.execute(
            """SELECT s.id, s.name, s.qualified_name, s.kind, s.file_path,
                      s.signature, s.is_definition, s.docstring
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.is_definition = 1
                 AND s.kind IN ('function', 'method', 'constructor', 'destructor',
                                'class', 'struct')
               ORDER BY CASE WHEN s.docstring IS NOT NULL AND LENGTH(s.docstring) > 30
                          THEN 0 ELSE 1 END""",
            (config_hash,),
        ).fetchall()
        if not rows:
            return

    # Build descriptions using the same format as embed phase
    descriptions = []
    for r in rows:
        fp = (r["file_path"] or "").replace("\\", "/")
        path = ""
        file_ = ""
        if "/" in fp:
            *dirs, file_ = fp.split("/")
            path = "/".join(dirs[-2:])
        elif fp:
            file_ = fp
        qname = r["qualified_name"] or ""
        name = r["name"] or ""
        class_ = "::".join(qname.split("::")[:-1]) if "::" in qname else ""
        sig = r["signature"] or ""
        is_os = "mbed-os" in fp.lower()
        doc = ""
        if not is_os:
            doc = (r["docstring"] or "").strip()
            if doc and len(doc) > 20:
                doc = doc[:150]
        parts = [path, file_, class_, name, sig]
        if doc:
            parts.append(doc)
        descriptions.append(" : ".join(p for p in parts if p))

    model = llm_config.embed_model
    total = 0
    chunk_size = 100
    for i in range(0, len(rows), chunk_size):
        chunk_rows = rows[i:i + chunk_size]
        chunk_descs = descriptions[i:i + chunk_size]
        try:
            embs = call_ollama_embed(chunk_descs, llm_config)
        except Exception as e:
            log.warning("Embedding batch %d failed: %s", i // chunk_size, e)
            continue

        # Store in legacy BLOB table (backward compatibility)
        blob_batch = [(r["id"], _vec_to_blob(emb), model) for r, emb in zip(chunk_rows, embs)]
        upsert_embeddings(conn, blob_batch)

        # Store in vec0 table (sqlite-vec KNN search)
        try:
            vec_batch = [(r["id"], config_hash, emb) for r, emb in zip(chunk_rows, embs)]
            upsert_embeddings_vec(conn, vec_batch)
        except Exception as e:
            log.debug("vec0 batch insert failed (sqlite-vec may not be loaded): %s", e)

        total += len(blob_batch)
    log.info("Embeddings stored: %d symbols (model=%s)", total, model)


def _process_unit(unit, config_hash, project_root, source_roots, exclude_paths, index_refs, db_path, existing_files):
    """Process one translation unit: check staleness, parse, store.

    Opens its own DB connection so it is safe to call from worker threads.
    Returns (status, symbols_added, refs_added) where status is
    'updated', 'unchanged', or 'skipped'.
    """
    resolved_tu = unit.file.resolve()
    if any(resolved_tu == ep or resolved_tu.is_relative_to(ep) for ep in exclude_paths):
        return ("unchanged", 0, 0)

    file_path = str(unit.file)
    if file_path in existing_files:
        _, stored_mtime = existing_files[file_path]
        try:
            current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
        except OSError:
            current_mtime = 0.0
        if abs(current_mtime - stored_mtime) < 0.001:
            return ("unchanged", 0, 0)

    conn = open_db(db_path)
    try:
        with transaction(conn, checkpoint=False):
            syms_added, refs_added = store_symbols_for_unit(
                conn, unit, config_hash, project_root,
                source_roots=source_roots,
                exclude_paths=exclude_paths,
                index_refs=index_refs,
            )
        return ("updated", syms_added, refs_added)
    except Exception as exc:
        log.warning("skip TU %s: %s", unit.file.name, exc)
        return ("skipped", 0, 0)
    finally:
        conn.close()


def run(
    compile_commands: Path,
    db_path: Path,
    source_roots: list[Path] | None = None,
    exclude_paths: list[Path] | None = None,
    project_name: str | None = None,
    index_refs: bool = False,
    index_embeddings: bool = False,
    project_root: Path | None = None,
    project_id: str | None = None,
    llm_config=None,
    parallel: bool = True,
) -> str:
    """Index a project. Returns config_hash of the indexed build.

    *parallel* (default True): use ThreadPoolExecutor to parse multiple TUs
    concurrently.  libclang releases the GIL during parsing, so threads provide
    real parallelism.  Set to False for debugging or single-core systems.
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
    config_hash = compute_config_hash(compile_commands)

    log.info("project=%s config_hash=%s", name, config_hash[:12])

    conn = open_db(db_path)
    with transaction(conn):
        upsert_project(conn, project_id, name, str(project_root))
        upsert_build_config(conn, config_hash, project_id, str(compile_commands))

    units = list(parse_compile_commands(compile_commands))
    units = [u for u in units if u.file.suffix.lower() in _SOURCE_EXTS]
    log.info("TUs to index: %d", len(units))

    existing_files = get_file_mtimes(conn, config_hash)

    total_syms = 0
    total_refs = 0
    skipped = 0
    unchanged = 0
    updated = 0
    t0 = time.monotonic()

    if parallel and len(units) > 1:
        max_workers = min(os.cpu_count() or 2, 2)  # libclang contention kills scaling above 2
        log.info("Parallel indexing with %d workers", max_workers)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _process_unit, u, config_hash, project_root,
                    source_roots, exclude_paths, index_refs, db_path, existing_files,
                ): i
                for i, u in enumerate(units)
            }
            for future in as_completed(futures):
                try:
                    status, syms, refs = future.result()
                except Exception as exc:
                    log.warning("Worker failed: %s", exc)
                    skipped += 1
                    continue
                if status == "updated":
                    updated += 1
                    total_syms += syms
                    total_refs += refs
                elif status == "unchanged":
                    unchanged += 1
                elif status == "skipped":
                    skipped += 1
                if updated % 50 == 0 and updated > 0:
                    elapsed = time.monotonic() - t0
                    log.info(
                        "  %d/%d TUs processed, %d symbols, %d refs, %.1fs elapsed",
                        updated + unchanged + skipped, len(units),
                        total_syms, total_refs, elapsed,
                    )
    else:
        # Sequential path — uses per-TU transactions
        for i, unit in enumerate(units):
            status, syms, refs = _process_unit(
                unit, config_hash, project_root,
                source_roots, exclude_paths, index_refs, db_path, existing_files,
            )
            if status == "updated":
                updated += 1
                total_syms += syms
                total_refs += refs
            elif status == "unchanged":
                unchanged += 1
            elif status == "skipped":
                skipped += 1
            if updated % 50 == 0 and updated > 0:
                elapsed = time.monotonic() - t0
                log.info(
                    "  %d/%d TUs processed, %d symbols, %d refs, %.1fs elapsed",
                    i + 1, len(units), total_syms, total_refs, elapsed,
                )

    # Embedding generation (opt-in)
    if index_embeddings and llm_config is not None and llm_config.enabled:
        log.info("Generating embeddings for %d symbols...", total_syms)
        _build_embeddings(conn, config_hash, llm_config)
        conn.commit()

    elapsed = time.monotonic() - t0
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    log.info(
        "Done: %d updated, %d unchanged, %d skipped — %d symbols, %d refs in %.1fs (config_hash=%s)",
        updated, unchanged, skipped, total_syms, total_refs, elapsed, config_hash[:12],
    )
    return config_hash
