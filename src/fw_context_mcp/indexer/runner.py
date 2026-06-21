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
from ..llm.ollama import call_ollama
from ..utils import MTIME_TOLERANCE_S
from .compile_commands import _SOURCE_EXTS
from .compile_commands import parse as parse_compile_commands
from .config_hash import compute as compute_config_hash
from .db import (
    CURRENT_SCHEMA_VERSION,
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
                      s.signature, s.is_definition, s.docstring, s.summary
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
        llm = (r["summary"] or "").strip()
        if llm:
            llm = llm[:200]
        parts = [path, file_, class_, name, sig]
        if doc:
            parts.append(doc)
        if llm:
            parts.append(llm)
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

# SDK path patterns for filtering (mbed-os, Zephyr, PlatformIO, build dirs)
_SDK_PATH_PATTERNS = ("mbed-os/", ".pio/", "zephyr/", "build/", "modules/")


def _read_body(abs_path: str, start_line: int, end_line: int) -> str:
    """Read a function body from a source file using line numbers.

    Returns the body text or an empty string on any error (missing file, bad range).
    """
    try:
        with open(abs_path) as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return ""
    if end_line > start_line and end_line <= len(lines):
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

        # Only read body for functions/methods with valid extents
        if kind in ("function", "method", "constructor", "destructor") and abs_path and end_line > start_line:
            body = _read_body(abs_path, start_line, end_line)

        # Fetch callees from the reference index
        if usr:
            callees = _fetch_callees(conn, usr, config_hash)

        d["body"] = body
        d["callees"] = callees
        enriched.append(d)
    return enriched


def _build_llm_analysis(conn, config_hash: str, llm_config) -> None:
    """Generate structured LLM analysis (summary, inputs, outputs) for all
    project-definition symbols using Ollama in batches.

    Follows the same pattern as _build_embeddings() but uses the chat endpoint
    instead of the embed endpoint, and stores structured text rather than vectors.
    Only project symbols (non-SDK) are analyzed.

    Since 2026-06 the prompt includes the full function body (read from disk via
    exact libclang extents) and callee names (from the reference index), which
    dramatically improves description quality — especially for large functions
    without docstrings.
    """
    import httpx

    from ..indexer.prompts import build_analysis_prompt, parse_analysis_response
    from .db import upsert_llm_analysis_batch

    # Check Ollama reachability
    try:
        resp = httpx.get(llm_config.ollama_url.rstrip("/") + "/api/tags", timeout=5.0)
        resp.raise_for_status()
    except Exception:
        log.warning("Ollama not reachable — skipping LLM analysis generation")
        return

    with transaction(conn):
        rows = conn.execute(
            """SELECT s.id, s.name, s.qualified_name, s.kind, s.file_path,
                      s.signature, s.is_definition, s.docstring,
                      s.end_line, s.line, s.usr,
                      f.path as abs_path
               FROM symbols s
               JOIN files f ON s.file_id = f.id
               WHERE s.config_hash = ?
                 AND s.is_definition = 1
                 AND s.kind IN ('function', 'method', 'constructor', 'destructor',
                                'class', 'struct')
                 AND s.file_path NOT LIKE 'mbed-os/%'
                 AND s.file_path NOT LIKE '.pio/%'
                 AND s.file_path NOT LIKE 'zephyr/%'
                 AND s.file_path NOT LIKE 'build/%'
                 AND s.file_path NOT LIKE 'modules/%'
                 AND s.id NOT IN (SELECT symbol_id FROM llm_analysis)
               ORDER BY s.kind, s.file_path, s.line""",
            (config_hash,),
        ).fetchall()
        if not rows:
            log.info("All project symbols already analyzed — nothing to do")
            return

    model = llm_config.model
    total = 0
    failed_ids: list[int] = []

    # ── Phase 1: batch analysis ──────────────────────────────────────────
    batch_size = 10
    batches = (len(rows) + batch_size - 1) // batch_size
    log.info("LLM analysis: %d symbols in %d batches (model=%s)", len(rows), batches, model)

    for batch_num in range(0, len(rows), batch_size):
        batch = rows[batch_num:batch_num + batch_size]
        batch_dicts = _enrich_batch(conn, batch, config_hash)
        prompt = build_analysis_prompt(batch_dicts)
        try:
            response = call_ollama(prompt, llm_config, temperature=0.1, num_predict=3000)
        except Exception as e:
            log.warning("LLM analysis batch %d failed: %s", batch_num // batch_size, e)
            failed_ids.extend(r["id"] for r in batch)
            continue

        parsed = parse_analysis_response(response, batch_dicts)
        if not parsed:
            log.warning(
                "LLM analysis batch %d: no valid entries parsed from response",
                batch_num // batch_size,
            )
            failed_ids.extend(r["id"] for r in batch)
            continue

        with transaction(conn):
            db_rows = [
                (r["symbol_id"], r["summary"], r["inputs"], r["outputs"], model)
                for r in parsed
            ]
            inserted = upsert_llm_analysis_batch(conn, db_rows)
            total += inserted

        if (batch_num // batch_size) % 5 == 0:
            log.info("  batch %d/%d: %d stored", batch_num // batch_size + 1, batches, total)

    # ── Phase 2: retry failed symbols individually ───────────────────────
    if failed_ids:
        log.info("Retrying %d failed symbols individually...", len(failed_ids))
        # Re-fetch only the failed symbols (they may have been modified)
        failed_rows = conn.execute(
            """SELECT s.id, s.name, s.qualified_name, s.kind, s.file_path,
                      s.signature, s.docstring, s.end_line, s.line, s.usr,
                      f.path as abs_path
               FROM symbols s
               JOIN files f ON s.file_id = f.id
               WHERE s.id IN ({})""".format(",".join("?" * len(failed_ids))),
            failed_ids,
        ).fetchall()
        for row in failed_rows:
            batch_dicts = _enrich_batch(conn, [row], config_hash)
            prompt = build_analysis_prompt(batch_dicts)
            try:
                response = call_ollama(prompt, llm_config, temperature=0.1, num_predict=4000)
                parsed = parse_analysis_response(response, batch_dicts)
                if parsed:
                    with transaction(conn):
                        inserted = upsert_llm_analysis_batch(
                            conn,
                            [(parsed[0]["symbol_id"], parsed[0]["summary"],
                              parsed[0]["inputs"], parsed[0]["outputs"], model)],
                        )
                        total += inserted
                else:
                    log.warning("Individual retry failed for %s", row["qualified_name"])
            except Exception as e:
                log.warning("Individual retry failed for %s: %s", row["qualified_name"], e)

    log.info("LLM analysis stored: %d symbols (model=%s)", total, model)


def _build_file_analysis(conn, config_hash: str, llm_config, extra_exclude_like: list[str] | None = None) -> None:
    """Generate file-level LLM analysis (2-3 sentence summaries) for project
    source files using Ollama in batches of 5.

    Uses the already-indexed symbols table to describe what each file is
    responsible for.  Only project files are analyzed (non-SDK).

    *extra_exclude_like* are additional LIKE patterns (relative to project
    root) to exclude, merged with the built-in SDK patterns.
    """
    import httpx

    from ..indexer.prompts import build_file_analysis_prompt, parse_file_analysis_response
    from .db import upsert_file_analysis_batch

    try:
        resp = httpx.get(llm_config.ollama_url.rstrip("/") + "/api/tags", timeout=5.0)
        resp.raise_for_status()
    except Exception:
        log.warning("Ollama not reachable — skipping file analysis generation")
        return

    # Gather files with their representative symbols (up to 30 per file).
    # Built-in SDK exclusions cover common embedded ecosystems; project-specific
    # exclude_paths (from .fw-context/config.toml) are appended.
    _SDK_EXCLUDES = ["mbed-os/%", ".pio/%", "zephyr/%", "build/%", "modules/%"]
    exclude_patterns = list(_SDK_EXCLUDES)
    if extra_exclude_like:
        exclude_patterns.extend(extra_exclude_like)
    where_clauses = " AND ".join(["f.path NOT LIKE ?"] * len(exclude_patterns))
    query = f"""SELECT f.id AS file_id, f.path,
                       COUNT(s.id) AS sym_count
                FROM files f
                JOIN symbols s ON s.file_id = f.id AND s.config_hash = ?
                WHERE f.config_hash = ?
                  AND {where_clauses}
                  AND f.id NOT IN (SELECT file_id FROM file_analysis)
                GROUP BY f.id
                ORDER BY sym_count DESC"""
    with transaction(conn):
        file_rows = conn.execute(
            query, (config_hash, config_hash, *exclude_patterns),
        ).fetchall()
        if not file_rows:
            log.info("All project files already analyzed — nothing to do")
            return

    # Fetch representative symbols for each file
    files_with_syms: list[dict] = []
    for fr in file_rows:
        syms = conn.execute(
            """SELECT name, qualified_name, kind, signature
               FROM symbols
               WHERE file_id = ? AND config_hash = ?
               ORDER BY is_definition DESC, kind, line
               LIMIT 30""",
            (fr["file_id"], config_hash),
        ).fetchall()
        files_with_syms.append({
            "file_id": fr["file_id"],
            "path": fr["path"],
            "symbols": [dict(s) for s in syms],
        })

    model = llm_config.model
    total = 0
    batch_size = 5
    batches = (len(files_with_syms) + batch_size - 1) // batch_size
    log.info("File analysis: %d files in %d batches (model=%s)", len(files_with_syms), batches, model)

    for batch_num in range(0, len(files_with_syms), batch_size):
        batch = files_with_syms[batch_num:batch_num + batch_size]
        prompt = build_file_analysis_prompt(batch)
        try:
            response = call_ollama(prompt, llm_config, temperature=0.1, num_predict=2000)
        except Exception as e:
            log.warning("File analysis batch %d failed: %s", batch_num // batch_size, e)
            continue

        parsed = parse_file_analysis_response(response, batch)
        if not parsed:
            log.warning(
                "File analysis batch %d: no valid entries parsed from response",
                batch_num // batch_size,
            )
            continue

        with transaction(conn):
            db_rows = [(r["file_id"], config_hash, r["summary"], model) for r in parsed]
            inserted = upsert_file_analysis_batch(conn, db_rows)
            total += inserted

        if (batch_num // batch_size) % 4 == 0:
            log.info("  file batch %d/%d: %d stored", batch_num // batch_size + 1, batches, total)

    log.info("File analysis stored: %d files (model=%s)", total, model)


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
    params_str = signature[paren_start + 1:paren_end].strip()
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
                ptr_prefix = last[:len(last) - len(stripped)]
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


def _build_overrides(conn, config_hash: str) -> None:
    """Build the method override graph by matching virtual methods to their
    base-class counterparts through the inheritance chain.

    Pure post-processing — walks the inheritance graph already stored in
    the ``inheritance`` table and matches methods by name.  Parameter-type
    comparison provides a basic guard against accidental name collisions
    (overloads, not overrides).
    """
    from .db import insert_overrides_batch

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
        with transaction(conn):
            insert_overrides_batch(conn, override_rows)
            total += len(override_rows)

    log.info(
        "Overrides stored: %d relationships (%d virtual, %d no-base, %d no-match)",
        total, len(virtual_rows), skipped_no_base, skipped_no_match,
    )


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
    analyze_symbols: bool = False,
    analyze_files: bool = False,
    analyze_overrides: bool = True,
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

    # LLM analysis generation (opt-in)
    if analyze_symbols and llm_config is not None and llm_config.enabled:
        log.info("Generating LLM analysis for project symbols...")
        _build_llm_analysis(conn, config_hash, llm_config)
        conn.commit()

    # File-level LLM analysis (opt-in, runs after symbol analysis)
    if analyze_files and llm_config is not None and llm_config.enabled:
        log.info("Generating file-level LLM analysis...")
        # Convert absolute exclude paths to LIKE patterns relative to project root
        extra_like: list[str] = []
        if exclude_paths:
            for ep in exclude_paths:
                try:
                    rel = ep.resolve().relative_to(project_root)
                    extra_like.append(str(rel) + "/%")
                except ValueError:
                    pass  # path not under project_root — skip
        _build_file_analysis(conn, config_hash, llm_config, extra_exclude_like=extra_like)
        conn.commit()

    # Method override tracking (post-processing, no LLM needed)
    if analyze_overrides:
        log.info("Building method override graph...")
        _build_overrides(conn, config_hash)
        conn.commit()

    elapsed = time.monotonic() - t0
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    # Stamp schema version — marks the index as current (get_active_build
    # compares PRAGMA user_version against CURRENT_SCHEMA_VERSION).
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    log.info(
        "Done: %d updated, %d unchanged, %d skipped — %d symbols, %d refs in %.1fs (config_hash=%s)",
        updated, unchanged, skipped, total_syms, total_refs, elapsed, config_hash[:12],
    )
    return config_hash
