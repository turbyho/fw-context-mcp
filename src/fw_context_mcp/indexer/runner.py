"""Index runner: parse compile_commands.json, extract symbols, store to SQLite."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..config.settings import derive_project_id
from .compile_commands import _SOURCE_EXTS
from .compile_commands import parse as parse_compile_commands
from .config_hash import compute as compute_config_hash
from .db import (
    delete_refs_for_file,
    delete_symbols_for_file,
    get_file_mtimes,
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    split_tokens,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from .symbols import extract_all

log = logging.getLogger(__name__)

# Directories to look for when auto-detecting source roots
_COMMON_SOURCE_DIRS = ["src", "lib", "app", "include", "drivers", "modules"]
_COMMON_OS_DIRS = ["zephyr", "mbed-os"]


def _detect_source_roots(project_root: Path, compile_commands: Path) -> list[Path]:
    """Auto-detect source directories from project structure and compile_commands.json.

    Scans project root for common source/OS directories, then supplements with
    top-level directories discovered from compile_commands.json entries.
    Falls back to the project root itself if nothing is found.
    """
    roots: list[Path] = []
    seen: set[Path] = set()

    # 1. Scan for common source directories
    for name in _COMMON_SOURCE_DIRS:
        p = project_root / name
        if p.is_dir() and p not in seen:
            roots.append(p)
            seen.add(p)

    # 2. Scan for common OS/framework directories
    for name in _COMMON_OS_DIRS:
        p = project_root / name
        if p.is_dir() and p not in seen:
            roots.append(p)
            seen.add(p)

    # 3. Discover additional top-level dirs from compile_commands.json
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
                pass  # outside project root, skip
    except Exception:
        pass

    # 4. Fallback: index everything under project root
    if not roots:
        roots = [project_root]
        log.info("No source directories detected, falling back to project root")

    log.info("Auto-detected source roots: %s", [str(r) for r in roots])
    return roots


def _build_embeddings(conn, config_hash: str, llm_config) -> None:
    """Generate and store embeddings for all definition symbols in a build."""
    import httpx

    from ..llm.ollama import call_ollama_embed, OllamaError
    from .db import _vec_to_blob, upsert_embeddings

    # Quick check — skip entire phase if Ollama is unreachable.
    try:
        resp = httpx.get(llm_config.ollama_url.rstrip("/") + "/api/tags", timeout=5.0)
        resp.raise_for_status()
    except Exception:
        log.warning("Ollama not reachable — skipping embedding generation")
        return

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

    # Release the read lock before the (potentially slow) Ollama calls.
    conn.commit()

    # Build descriptions using the same format as _build_symbol_description
    # in server.py — keep them in sync.
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

        batch = []
        for r, emb in zip(chunk_rows, embs):
            blob = _vec_to_blob(emb)
            batch.append((r["id"], blob, model))
        upsert_embeddings(conn, batch)
        total += len(batch)

    log.info("Embeddings stored: %d symbols (model=%s)", total, model)


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
    llm_config = None,
) -> str:
    """Index a project. Returns config_hash of the indexed build.

    *project_root* and *project_id* should be passed by the caller (the CLI) so
    they match the identity used to compute ``db_path``. When omitted they are
    derived from ``compile_commands.parent`` — which is only correct when the
    file lives at the project root and the repo has a git remote. Passing them
    explicitly avoids a project_id mismatch for out-of-tree builds (e.g. Zephyr
    ``build/compile_commands.json``) in repos without a remote.
    """
    if project_root is None:
        project_root = compile_commands.parent.resolve()
    else:
        project_root = project_root.resolve()
    if not source_roots:
        source_roots = _detect_source_roots(project_root, compile_commands)
    # Only keep roots that actually exist
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

    # Load existing file records for incremental update
    existing_files = get_file_mtimes(conn, config_hash)

    total_syms = 0
    total_refs = 0
    skipped = 0
    unchanged = 0
    updated = 0
    t0 = time.monotonic()

    for i, unit in enumerate(units):
        file_path = str(unit.file)

        # Skip TUs that live under an excluded path entirely
        resolved_tu = unit.file.resolve()
        if any(resolved_tu == ep or resolved_tu.is_relative_to(ep) for ep in exclude_paths):
            unchanged += 1
            continue

        try:
            current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
        except OSError:
            current_mtime = 0.0

        # Skip if file hasn't changed since last index
        if file_path in existing_files:
            _, stored_mtime = existing_files[file_path]
            if abs(current_mtime - stored_mtime) < 0.001:
                unchanged += 1
                continue

        try:
            syms, refs = extract_all(
                unit, source_roots=source_roots, exclude_paths=exclude_paths,
                with_refs=index_refs,
            )
        except Exception as exc:
            log.warning("skip TU %s: %s", unit.file.name, exc)
            skipped += 1
            continue

        with transaction(conn, checkpoint=False):
            if file_path in existing_files:
                file_id, _ = existing_files[file_path]
                delete_symbols_for_file(conn, file_id)
            # Register the TU file (for mtime tracking)
            upsert_file(conn, config_hash, file_path, unit.language, mtime=current_mtime)

            if syms:
                # Each symbol may come from a different file (e.g. included header).
                # Build a per-file cache so file_id reflects the symbol's actual location.
                file_id_cache: dict[str, int] = {}
                rows = []
                for s in syms:
                    sym_file = s.file
                    if sym_file not in file_id_cache:
                        lang = "cpp" if Path(sym_file).suffix.lower() in {".cpp", ".cc", ".cxx", ".c++"} else "c"
                        try:
                            sym_mtime = Path(sym_file).stat().st_mtime
                        except OSError:
                            sym_mtime = 0.0
                        file_id_cache[sym_file] = upsert_file(conn, config_hash, sym_file, lang, mtime=sym_mtime)
                    try:
                        rel_path = str(Path(sym_file).resolve().relative_to(project_root))
                    except ValueError:
                        rel_path = sym_file
                    rows.append((
                        config_hash,
                        file_id_cache[sym_file],
                        rel_path,
                        split_tokens(s.name, s.qualified_name),
                        s.usr,
                        s.name,
                        s.qualified_name,
                        s.kind,
                        s.line,
                        s.column,
                        s.end_line,
                        int(s.is_definition),
                        s.signature,
                        s.docstring,
                    ))
                total_syms += insert_symbols_batch(conn, rows)

            if index_refs and refs:
                # Store from_file relative to project root (consistent with
                # symbols.file_path). On incremental reindex, clear this TU's
                # old refs first (keyed by the TU's relative path).
                def _rel(p: str) -> str:
                    try:
                        return str(Path(p).resolve().relative_to(project_root))
                    except ValueError:
                        return p

                tu_rel = _rel(file_path)
                if file_path in existing_files:
                    delete_refs_for_file(conn, config_hash, tu_rel)
                ref_rows = [
                    (config_hash, r.to_usr, _rel(r.from_file), r.from_line, r.from_usr, r.ref_kind)
                    for r in refs
                ]
                total_refs += insert_refs_batch(conn, ref_rows)

        updated += 1
        if updated % 50 == 0:
            elapsed = time.monotonic() - t0
            log.info(
                "  %d/%d TUs processed, %d symbols, %d refs, %.1fs elapsed",
                i + 1, len(units), total_syms, total_refs, elapsed,
            )

    # --- Embedding generation (opt-in) ---
    if index_embeddings and llm_config is not None and llm_config.enabled:
        log.info("Generating embeddings for %d symbols...", total_syms)
        _build_embeddings(conn, config_hash, llm_config)
        conn.commit()

    elapsed = time.monotonic() - t0
    # Single WAL checkpoint after all per-TU commits.
    # Use PASSIVE — TRUNCATE requires an exclusive lock which may conflict
    # with embedding writes that just completed.
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    log.info(
        "Done: %d updated, %d unchanged, %d skipped — %d symbols, %d refs in %.1fs (config_hash=%s)",
        updated, unchanged, skipped, total_syms, total_refs, elapsed, config_hash[:12],
    )
    return config_hash
