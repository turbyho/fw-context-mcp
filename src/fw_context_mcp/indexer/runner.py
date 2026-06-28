"""Index runner: parse compile_commands.json, extract symbols, store to SQLite.

Uses ``indexer/ops.py`` for the shared "parse TU → store symbols" loop so
that runner, reindex_file, and auto-reindex all use the same code path.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path

from ..config.settings import derive_project_id
from ..llm.ollama import call_ollama
from ..utils import MTIME_TOLERANCE_S
from .compile_commands import _SOURCE_EXTS
from .compile_commands import parse as parse_compile_commands
from .config_hash import compute as compute_config_hash
from .db import (
    CURRENT_SCHEMA_VERSION,
    drop_fts_triggers,
    get_file_mtimes,
    open_db,
    rebuild_fts,
    transaction,
    upsert_build_config,
    upsert_project,
    write_lock,
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


def _build_embeddings(conn, config_hash: str, llm_config, db_dir: Path) -> None:
    """Generate and store vector embeddings for all definition symbols.

    Selects all function, method, constructor, destructor, class, and struct
    definitions from the current build, builds a human-readable description
    for each (combining file path, class, name, signature, docstring, and
    LLM summary), and produces embeddings via Ollama.

    Descriptions are processed in chunks of 100 to stay within model context
    limits.  Each batch is stored in two tables simultaneously:

    * ``upsert_embeddings`` — legacy BLOB table (backward compatibility).
    * ``upsert_embeddings_vec`` — ``sqlite-vec`` vec0 table for KNN search.

    When Ollama is unreachable or returns an error for a batch, a warning is
    logged and the batch is skipped (non-fatal — remaining batches continue).
    """
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


def _build_llm_analysis(conn, config_hash: str, llm_config, db_dir: Path, *, write_lock_held: bool = False) -> None:
    """Generate structured LLM analysis (summary, inputs, outputs) for each
    project-definition symbol using Ollama, one symbol per request.

    Processes symbols individually — one Ollama request per symbol — for
    reliable format adherence. Only project symbols (non-SDK) are analyzed.

    The prompt includes the full function body (read from disk via exact
    libclang extents) and callee names (from the reference index), which
    dramatically improves description quality.

    *db_dir* is the directory containing the index database — used for the
    write lock that serializes DB access across processes.
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
    total_symbols = len(rows)
    total = 0

    log.info("LLM analysis: %d symbols (model=%s)", total_symbols, model)

    for idx, row in enumerate(rows):
        qname = row["qualified_name"] or row["name"]
        try:
            batch_dicts = _enrich_batch(conn, [row], config_hash)
            prompt = build_analysis_prompt(batch_dicts)
            try:
                response = call_ollama(prompt, llm_config, temperature=0.1, num_predict=3000)
            except Exception as e:
                log.warning("[%d/%d] %s: Ollama call failed: %s", idx + 1, total_symbols, qname, e)
                continue

            parsed = parse_analysis_response(response, batch_dicts)
            if not parsed:
                log.warning("[%d/%d] %s: no valid entries parsed from response", idx + 1, total_symbols, qname)
                continue

            with (write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext()):
                with transaction(conn):
                    db_rows = [
                        (r["symbol_id"], r["summary"], r["inputs"], r["outputs"], model)
                        for r in parsed
                    ]
                    inserted = upsert_llm_analysis_batch(conn, db_rows)
                    total += inserted

            log.info("[%d/%d] %s: stored", idx + 1, total_symbols, qname)
        except Exception as e:
            log.warning("[%d/%d] %s: crashed: %s", idx + 1, total_symbols, qname, e)
            continue

    log.info("LLM analysis stored: %d/%d symbols (model=%s)", total, total_symbols, model)


def _build_file_analysis(conn, config_hash: str, llm_config, db_dir: Path, extra_exclude_like: list[str] | None = None, *, write_lock_held: bool = False) -> None:
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

        with (write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext()):
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


def _build_overrides(conn, config_hash: str, db_dir: Path, *, write_lock_held: bool = False) -> None:
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
        with (write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext()):
            with transaction(conn):
                insert_overrides_batch(conn, override_rows)
                total += len(override_rows)

    log.info(
        "Overrides stored: %d relationships (%d virtual, %d no-base, %d no-match)",
        total, len(virtual_rows), skipped_no_base, skipped_no_match,
    )


def _process_unit(unit, config_hash, project_root, source_roots, exclude_paths, index_refs, db_path, existing_files, lock=None, conn=None):
    """Process one translation unit: check staleness, parse, store.

    Opens its own DB connection when *conn* is ``None``, otherwise reuses
    the caller-supplied connection (persistent per-worker connection).

    Serializes DB writes via *lock* when supplied (``threading.Lock`` for
    intra-process synchronization).  When *lock* is ``None``, the caller
    is responsible for serialisation (sequential path with fcntl wrap).

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

    Returns:
        A tuple ``(status, symbols_added, refs_added)`` where ``status`` is
        ``"updated"`` (new or modified symbols stored), ``"unchanged"``
        (mtime matched — no work needed), or ``"skipped"`` (excluded by
        ``exclude_paths`` or failed during parsing).
    """
    resolved_tu = unit.file.resolve()
    if any(resolved_tu == ep or resolved_tu.is_relative_to(ep) for ep in exclude_paths):
        return ("unchanged", 0, 0, (0.0, 0.0, 0.0))

    file_path = str(unit.file)
    if file_path in existing_files:
        _, stored_mtime = existing_files[file_path]
        try:
            current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
        except OSError:
            current_mtime = 0.0
        if current_mtime <= stored_mtime + MTIME_TOLERANCE_S:
            return ("unchanged", 0, 0, (0.0, 0.0, 0.0))

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
        return ("skipped", 0, 0, (0.0, 0.0, 0.0))
    t_parse_end = time.monotonic()

    # Resolve connection: persistent (callable → lazy open, don't close),
    # explicit, or own (open now, close after).
    if callable(conn):
        conn = conn()          # lazy thread-local — caller manages lifecycle
        own_conn = False
    elif conn is None:
        conn = open_db(db_path)
        own_conn = True
    else:
        own_conn = False       # caller-supplied, don't close

    t_lock_start = time.monotonic()
    try:
        # threading.Lock (intra-process) or nullcontext (sequential path
        # where the caller holds fcntl write_lock across all TUs)
        lock_ctx: object = lock if lock is not None else nullcontext()
        with lock_ctx:
            t_write_start = time.monotonic()
            with transaction(conn, checkpoint=False):
                syms_added, refs_added = store_symbols_for_unit(
                    conn, unit, config_hash, project_root,
                    source_roots=source_roots,
                    exclude_paths=exclude_paths,
                    index_refs=index_refs,
                    pre_parsed=parsed,
                    existing_files=existing_files,
                )
            t_write_end = time.monotonic()
            t_parse = t_parse_end - t_parse_start
            t_lock = t_write_start - t_lock_start
            t_write = t_write_end - t_write_start
            log.debug(
                "  TU %s: parse=%.1fs lock_wait=%.2fs write=%.1fs syms=%d refs=%d",
                unit.file.name, t_parse, t_lock, t_write, syms_added, refs_added,
            )
        timing = (t_parse, t_lock, t_write)
        return ("updated", syms_added, refs_added, timing)
    except Exception as exc:
        log.warning("skip TU %s: %s", unit.file.name, exc)
        return ("skipped", 0, 0, (0.0, 0.0, 0.0))
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
    analyze_files: bool = False,
    analyze_overrides: bool = True,
    project_root: Path | None = None,
    project_id: str | None = None,
    llm_config=None,
    parallel: bool = True,
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
        analyze_files: When True, generate file-level summaries via Ollama
            (requires ``analyze_symbols`` to have run first for best quality).
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
        parallel: When True (default), use a ``ThreadPoolExecutor`` with
            up to 2 workers to parse multiple translation units concurrently.
            libclang releases the GIL during parsing, so threads provide real
            parallelism.  Set to False for debugging or single-core systems.

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
    config_hash = compute_config_hash(compile_commands)

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

    log.info("project=%s config_hash=%s", name, config_hash[:12])

    conn = open_db(db_path)
    with transaction(conn):
        upsert_project(conn, project_id, name, str(project_root))
        upsert_build_config(conn, config_hash, project_id, str(compile_commands))

    units = list(parse_compile_commands(compile_commands))
    units = [u for u in units if u.file.suffix.lower() in _SOURCE_EXTS]
    log.info("TUs to index: %d", len(units))

    existing_files = get_file_mtimes(conn, config_hash)

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
    t0 = time.monotonic()

    def _wait_if_paused() -> None:
        """If a manual operation requested pause, wait until it finishes.

        The MCP server writes ``<pid>`` to ``reindex.pause`` before a manual
        ``reindex_file`` or ``reset_index``.  This function blocks until the
        pause is lifted or the requesting process dies (stale marker cleanup).
        """
        pause_file = db_path.parent / "reindex.pause"
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

    _wait_if_paused()

    if parallel and len(units) > 1:
        # Single worker — libclang parsing through ctypes does not
        # meaningfully release the GIL in practice (tested).  Multiple
        # workers only increase memory pressure (multiple ASTs in RAM)
        # without measurable speedup.
        max_workers = 1

        # ── Intra-process lock ──
        # threading.Lock is user-space (no syscall per poll iteration).
        # fcntl write_lock is acquired ONCE around the entire parallel
        # block for cross-process protection (background reindex).
        db_lock = threading.Lock()

        # ── Per-thread persistent connections ──
        # Each worker thread opens one SQLite connection lazily (on first
        # use) and reuses it across TUs.  Without this, open_db() runs
        # PRAGMA integrity_check on the entire 6+ GB DB for every TU.
        _tlocal = threading.local()

        def _worker_conn_factory():
            """Lazy thread-local connection — called inside _process_unit."""
            if not hasattr(_tlocal, "conn"):
                # integrity_check already ran on the main connection;
                # skip it for workers — scanning 6+ GB per worker
                # saturates disk I/O and delays writes by minutes.
                _tlocal.conn = open_db(db_path, skip_integrity_check=True)
            return _tlocal.conn

        with write_lock(db_path.parent, timeout=60.0):
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        _process_unit, u, config_hash, project_root,
                        source_roots, exclude_paths, index_refs, db_path, existing_files,
                        lock=db_lock, conn=_worker_conn_factory,
                    ): i
                    for i, u in enumerate(units)
                }
                for future in as_completed(futures):
                    try:
                        status, syms, refs, timing = future.result()
                    except Exception as exc:
                        log.warning("Worker failed: %s", exc)
                        skipped += 1
                        continue
                    if status == "updated":
                        updated += 1
                        total_syms += syms
                        total_refs += refs
                        acc_parse += timing[0]
                        acc_lock += timing[1]
                        acc_write += timing[2]
                    elif status == "unchanged":
                        unchanged += 1
                    elif status == "skipped":
                        skipped += 1
                    if updated % 50 == 0 and updated > 0:
                        elapsed = time.monotonic() - t0
                        log.info(
                            "  %d/%d TUs, %d syms, %d refs, %.1fs | "
                            "parse=%.1fs lock=%.1fs write=%.1fs",
                            updated + unchanged + skipped, len(units),
                            total_syms, total_refs, elapsed,
                            acc_parse, acc_lock, acc_write,
                        )
    else:
        # Sequential path — uses per-TU transactions; wrap in fcntl lock
        # once so a background reindex cannot interleave writes.
        with write_lock(db_path.parent, timeout=60.0):
            for i, unit in enumerate(units):
                status, syms, refs, timing = _process_unit(
                    unit, config_hash, project_root,
                    source_roots, exclude_paths, index_refs, db_path, existing_files,
                )
                if status == "updated":
                    updated += 1
                    total_syms += syms
                    total_refs += refs
                    acc_parse += timing[0]
                    acc_lock += timing[1]
                    acc_write += timing[2]
                elif status == "unchanged":
                    unchanged += 1
                elif status == "skipped":
                    skipped += 1
                if updated % 50 == 0 and updated > 0:
                    elapsed = time.monotonic() - t0
                    log.info(
                        "  %d/%d TUs, %d syms, %d refs, %.1fs | "
                        "parse=%.1fs lock=%.1fs write=%.1fs",
                        i + 1, len(units), total_syms, total_refs, elapsed,
                        acc_parse, acc_lock, acc_write,
                    )

    # Rebuild FTS5 table from the now-complete symbols table — restores
    # full-text search after the triggers were dropped before indexing.
    log.info("Rebuilding FTS5 index...")
    t_fts_start = time.monotonic()
    rebuild_fts(conn)
    t_fts = time.monotonic() - t_fts_start

    elapsed = time.monotonic() - t0
    log.info(
        "Index summary: %d updated, %d unchanged, %d skipped, "
        "%d syms, %d refs, %.1fs total | "
        "parse=%.1fs lock_wait=%.1fs write=%.1fs fts_rebuild=%.1fs",
        updated, unchanged, skipped,
        total_syms, total_refs, elapsed,
        acc_parse, acc_lock, acc_write, t_fts,
    )

    # Embedding generation (opt-in)
    if index_embeddings and llm_config is not None and llm_config.enabled:
        log.info("Generating embeddings for %d symbols...", total_syms)
        _build_embeddings(conn, config_hash, llm_config, db_path.parent)
        conn.commit()

    # LLM analysis generation (opt-in)
    if analyze_symbols and llm_config is not None and llm_config.enabled:
        log.info("Generating LLM analysis for project symbols...")
        _build_llm_analysis(conn, config_hash, llm_config, db_path.parent)
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
        _build_file_analysis(conn, config_hash, llm_config, db_path.parent, extra_exclude_like=extra_like)
        conn.commit()

    # Method override tracking (post-processing, no LLM needed)
    if analyze_overrides:
        log.info("Building method override graph...")
        _build_overrides(conn, config_hash, db_path.parent)
        conn.commit()

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
    log.info(
        "Done: %d updated, %d unchanged, %d skipped — %d symbols, %d refs in %.1fs (config_hash=%s)",
        updated, unchanged, skipped, total_syms, total_refs, elapsed, config_hash[:12],
    )
    return config_hash
