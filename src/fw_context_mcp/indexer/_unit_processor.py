"""Per-TU processing extracted from runner.py.

Handles translation unit staleness checks, libclang parsing, symbol
reassignment, and persistence — the core per-file indexing logic.
"""

from __future__ import annotations

import logging
import os
import hashlib
import sqlite3
import time
from contextlib import nullcontext
from pathlib import Path

from ..utils import MTIME_TOLERANCE_S, SAFE_EXCEPT, compute_source_hash, is_fatal
from .ops import _build_filtered_file_content, _normalize_file_path, store_symbols_for_unit
from .db import (
    get_file_hashes,
    open_db,
    transaction,
    upsert_file,
    write_lock,
)
from .compile_commands import _SOURCE_EXTS, validate_include_files
from .config_hash import compute_flags_hash, compute_tu_content_hash

log = logging.getLogger(__name__)


def _reassign_symbols_for_file(
    conn,
    new_config_hash: str,
    new_file_id: int,
    file_path: str,
) -> int:
    """Reassign data for *file_path* from an old config_hash to *new_config_hash* using UPDATE.

    Unlike the old ``_migrate_symbols_for_file`` which copied data via
    INSERT OR REPLACE (allocating new rows, rebuilding indexes), this
    function updates ``config_hash`` and ``file_id`` in-place on existing
    rows.  No new rows are allocated, indexes are updated only for the
    changed columns.

    Rows that would collide with already-indexed symbols (shared headers
    across TUs) are left under the old config_hash and cleaned up later
    by :func:`delete_build_data`.

    When no previous build exists (Scenario C — first index), returns 0
    and the caller falls through to normal libclang parsing.

    Runs inside the caller's transaction — atomic UPDATE across all tables.
    """
    # Find the most recent old config_hash that has symbols for this file
    old = conn.execute(
        "SELECT config_hash, id FROM files WHERE path=? AND config_hash!=? ORDER BY rowid DESC LIMIT 1",
        (file_path, new_config_hash),
    ).fetchone()
    if old is None:
        return 0  # Scenario C — first index, nothing to reassign

    old_ch, old_fid = old

    # ── symbols ──
    # UPDATE only those that don't collide with already-existing symbols
    # under the new config_hash.  NOT IN subselect: symbols shared across
    # TUs (headers) may already have been indexed under new_config_hash by
    # another TU.
    cur = conn.execute(
        """UPDATE symbols SET config_hash=?, file_id=?
           WHERE config_hash=? AND file_id=?
           AND usr NOT IN (SELECT usr FROM symbols WHERE config_hash=?)""",
        (new_config_hash, new_file_id, old_ch, old_fid, new_config_hash),
    )
    symbol_count = cur.rowcount
    # Reset pagerank — _build_pagerank() skips when pagerank > 0.
    # Values from the previous build are stale because the call graph
    # may have changed in other (updated) files.
    conn.execute(
        "UPDATE symbols SET pagerank = 0.0 WHERE config_hash = ? AND file_id = ?",
        (new_config_hash, new_file_id),
    )

    # ── macros ──
    # UNIQUE(config_hash, file_id, line) — on collision, keep the old version.
    conn.execute(
        """UPDATE macros SET config_hash=?, file_id=?
           WHERE config_hash=? AND file_id=?
           AND NOT EXISTS (
               SELECT 1 FROM macros m2
               WHERE m2.config_hash = ?
                 AND m2.file_id = ?
                 AND m2.line = macros.line
           )""",
        (new_config_hash, new_file_id, old_ch, old_fid, new_config_hash, new_file_id),
    )

    # ── refs ──
    # Delete anything already under new_config_hash for this file so
    # UPDATE won't create duplicates (refs has no UNIQUE constraint).
    conn.execute(
        "DELETE FROM refs WHERE config_hash=? AND from_file=?",
        (new_config_hash, file_path),
    )
    conn.execute(
        """UPDATE refs SET config_hash=?
           WHERE config_hash=? AND from_file=?""",
        (new_config_hash, old_ch, file_path),
    )

    # ── fp_assignments ──
    conn.execute(
        "DELETE FROM fp_assignments WHERE config_hash=? AND from_file=?",
        (new_config_hash, file_path),
    )
    conn.execute(
        """UPDATE fp_assignments SET config_hash=?
           WHERE config_hash=? AND from_file=?""",
        (new_config_hash, old_ch, file_path),
    )

    # ── indirect_call_sites ──
    conn.execute(
        "DELETE FROM indirect_call_sites WHERE config_hash=? AND from_file=?",
        (new_config_hash, file_path),
    )
    conn.execute(
        """UPDATE indirect_call_sites SET config_hash=?
           WHERE config_hash=? AND from_file=?""",
        (new_config_hash, old_ch, file_path),
    )

    # ── vec_symbols (sqlite-vec KNN) ──
    # vec0 virtual table has a config_hash column for filtered KNN queries.
    # After updating symbols we must also update config_hash in vec0.
    # DELETE before UPDATE — safer; vec0 virtual tables may not support
    # UPDATE with WHERE subselect.
    try:
        conn.execute(
            """DELETE FROM vec_symbols WHERE symbol_id IN (
                   SELECT id FROM symbols WHERE config_hash=? AND file_id=?
               )""",
            (new_config_hash, new_file_id),
        )
        conn.execute(
            """UPDATE vec_symbols SET config_hash=?
               WHERE symbol_id IN (
                   SELECT id FROM symbols WHERE config_hash=? AND file_id=?
               )""",
            (new_config_hash, new_config_hash, new_file_id),
        )
    except SAFE_EXCEPT as e:
        if is_fatal(e):
            raise
        pass  # libclang/SQLite fallback  # sqlite-vec may not be available

    # ── inheritance ──
    # UPDATE inheritance edges for classes defined in this file.
    # derived_usr must belong to symbols we just reassigned (they already
    # have the new config_hash).
    # NOT EXISTS: prevents UNIQUE constraint violation when another TU
    # (changed, Tier 3) already created the same (derived_usr, base_usr)
    # edge under the new config_hash.
    conn.execute(
        """UPDATE inheritance SET config_hash=?
           WHERE config_hash=? AND derived_usr IN (
               SELECT usr FROM symbols WHERE config_hash=? AND file_id=?
           )
           AND NOT EXISTS (
               SELECT 1 FROM inheritance i2
               WHERE i2.config_hash = ?
                 AND i2.derived_usr = inheritance.derived_usr
                 AND i2.base_usr = inheritance.base_usr
           )""",
        (new_config_hash, old_ch, new_config_hash, new_file_id, new_config_hash),
    )

    # ── overrides ──
    # NOT migrated — _build_overrides() rebuilds from scratch for the new
    # config_hash in post-processing.  No reassignment needed.

    return symbol_count


def _check_and_parse_unit(
    unit,
    config_hash,
    project_root,
    vendor_patterns,
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

    All translation units are indexed — no exclusion filtering.

    Args:
        vendor_patterns: LIKE patterns for vendor/SDK directories (used for
            manifest staleness checking — vendor headers are trusted).
        manifest: Optional ``{file_path: entry}`` lookup dict built from
            ``manifest.load()`` entries.  When provided, header staleness
            is checked via hash comparison against the manifest (fast —
            file reads + SHA-256 only).  When ``None``, falls back to
            source-hash-only comparison.

    Returns:
        * ``("unchanged", None, None, None)`` — no re-parse needed (Tier 1 mtime match).
        * ``("unchanged", None, None, hashes)`` — no re-parse needed (Tier 2 content-hash match).
        * ``("reuse", None, None, hashes)`` — TU is unchanged across config_hash
          change; caller must copy symbols from the old config_hash and create
          a file record for the new config_hash (Tier 2b manifest-based match).
        * ``("skipped", None, None, None)`` — parse failed.
        * ``("updated", parsed, (t_start, t_end), hashes)`` — parsed
          successfully, ready for ``_process_unit(pre_parsed=parsed)``.
          *hashes* is ``(source_hash, flags_hash, manifest_entry_hash)``.
    """
    resolved_tu = unit.file.resolve()

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
        vendor_patterns,
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

    # ── Tier 2b: manifest-based fallback for new config_hash ──
    # When file_path has no record in the current config_hash (e.g. after
    # --build with a new compile_commands.json), the manifest entry from
    # the PREVIOUS index provides source_hash, header hashes, and flags_hash.
    # If all three match current disk content, the TU is unchanged despite
    # the new config_hash — skip the expensive libclang parse.
    if not force_refs and file_path not in existing_files and manifest is not None:
        from .manifest import check_tu_staleness
        from .manifest import get_manifest_entry_hash as _entry_hash

        try:
            tu_rel = str(unit.file.resolve().relative_to(project_root))
        except ValueError:
            tu_rel = str(unit.file.resolve())
        entry = manifest.get(tu_rel)
        # Guard: only trust entries with real source_hash AND flags_hash.
        # Preliminary (degraded) manifests have empty source_hash; old
        # manifests from before this feature lack flags_hash.
        if entry is not None and entry.get("source_hash") and entry.get("flags_hash"):
            stale, _ = check_tu_staleness(entry, project_root, vendor_patterns)
            if not stale:
                current_flags = compute_flags_hash(unit.raw_entry) if unit.raw_entry else ""
                if entry["flags_hash"] == current_flags:
                    mh = _entry_hash(entry)
                    hashes = (source_hash, current_flags, mh)
                    # Return "reuse" — caller must copy symbols from old
                    # config_hash and create a file record for the new one.
                    return ("reuse", None, None, hashes)

    # ── Tier 3: libclang parse ──
    from .symbols import extract_all

    t_parse_start = time.monotonic()
    try:
        parsed = extract_all(
            unit,
            with_refs=index_refs,
            return_tu=True,
        )
    except sqlite3.Error:
        log.error("Fatal DB error parsing %s — stopping indexer", unit.file.name)
        raise
    except SAFE_EXCEPT as exc:
        if is_fatal(exc):
            raise
        msg = str(exc)
        if "unable to open database file" in msg:
            log.error("Fatal DB error parsing %s: %s — stopping indexer", unit.file.name, exc)
            raise
        log.warning("skip TU %s: %s", unit.file.name, exc)
        return ("skipped", None, None, None)
    t_parse_end = time.monotonic()
    return ("updated", parsed, (t_parse_start, t_parse_end), hashes)


    # NOTE: falls back to computing source SHA-256 when manifest is absent.
    # _check_and_parse_unit() also computes source hash — could be passed
    # as parameter.  Overhead is one extra file read per TU (rare path).
def _get_manifest_entry_hash_for_unit(
    unit,
    project_root: Path,
    vendor_patterns: list[str],
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
            stale, current_source_hash = check_tu_staleness(entry, project_root, vendor_patterns)
            if not stale:
                return _entry_hash(entry)
            # Stale — compute hash from CURRENT disk content (both source and headers)
            return compute_current_entry_hash(
                entry,
                project_root,
                vendor_patterns,
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
    vendor_patterns,
    project_patterns,
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

    All TUs are indexed — no exclusion filtering.

    Args:
        unit: The ``CompilationUnit`` to parse (file path + clang flags).
        config_hash: Content-addressable build fingerprint for scoping
            all DB operations to the current build configuration.
        project_root: Root directory used for path resolution.
        vendor_patterns: LIKE patterns for vendor/SDK directories.
        project_patterns: LIKE patterns for user-declared project directories.
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
        (failed during parsing), and
        *headers* is a list of ``{path, hash, generated}`` dicts for included
        header files (empty list for unchanged/skipped).
    """

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
                with_refs=index_refs,
                return_tu=True,
            )
        except SAFE_EXCEPT as exc:
            if is_fatal(exc):
                raise
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
                    vendor_patterns=vendor_patterns,
                    project_patterns=project_patterns,
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
    except SAFE_EXCEPT as exc:
        if is_fatal(exc):
            raise
        msg = str(exc)
        if "unable to open database file" in msg:
            log.error("Fatal DB error storing %s: %s — stopping indexer", unit.file.name, exc)
            raise
        log.warning("skip TU %s: %s", unit.file.name, exc)
        return ("skipped", 0, 0, (0.0, 0.0, 0.0), [])
    finally:
        if own_conn:
            conn.close()




def _handle_unchanged_or_reuse(
    unit,
    check_status: str,
    hashes: tuple | None,
    conn: sqlite3.Connection,
    config_hash: str,
    project_root: Path,
    build_dir_patterns: list[str] | None,
    db_path: Path,
    existing_files: dict,
    processed: int,
    total_units: int,
) -> dict:
    """Handle Phase 1 staleness outcomes — bookkeeping for unchanged/reuse TUs.

    Manages file-record updates, symbol reassignment from old config_hash
    (reuse migration), and ifdef-filtered content filling.  The caller is
    responsible for applying the returned counters to its own state.

    Args:
        unit: The compilation unit being processed.
        check_status: One of ``"unchanged"`` or ``"reuse"`` (from
            :func:`_check_and_parse_unit`).
        hashes: ``(source_hash, flags_hash, manifest_entry_hash)`` tuple
            from Tier 2 / Tier 2b staleness check, or ``None`` for Tier 1.
        conn: Open SQLite connection to the index database.
        config_hash: Active build config hash.
        project_root: Project root directory.
        build_dir_patterns: Build directory exclusion patterns.
        db_path: Path to the index database file.
        existing_files: Dict mapping file paths to ``FileHashRecord``.
        processed: 1-based index of this TU in the batch (for logging).
        total_units: Total TU count (for logging).

    Returns:
        dict with keys:
        - ``fallthrough`` (bool): True when the TU must be re-parsed in
          Phase 2 (reuse migration produced 0 symbols).
        - ``file_id`` (int | None): File record ID for use in Phase 2.
        - ``headers`` (dict | None): Collected headers for manifest update.
        - ``status`` (str): ``"reused (manifest)"``, ``"unchanged (content)"``,
          or ``"unchanged"``.
        - ``is_reuse`` (bool): True when the TU was migrated from old config.
        - ``total_syms`` (int): Symbols copied during reuse migration.
        - ``content_filled`` (int): 1 if ifdef content was filled, 0 otherwise.
    """
    is_reuse = check_status == "reuse"
    fname = unit.file.name
    file_path_str = _normalize_file_path(str(unit.file.resolve()), project_root)
    rec = existing_files.get(file_path_str)
    file_id = rec.file_id if rec else None
    try:
        current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
    except OSError:
        current_mtime = 0.0

    # When "reuse" migration produces 0 symbols (no old data for this
    # TU), we must fall through to Phase 2 for a real libclang parse
    # instead of skipping the TU permanently.
    fallthrough = False
    total_syms = 0
    content_filled = 0
    headers = None

    with write_lock(db_path.parent, timeout=120.0):
        with transaction(conn, checkpoint=False):
            if hashes is not None:
                # Tier 2 / Tier 2b: content-hash match — update or create file record
                source_hash, flags_hash, manifest_entry_hash = hashes
                content_hash_val = compute_tu_content_hash(source_hash, flags_hash, manifest_entry_hash)
                if file_id is not None:
                    conn.execute(
                        """UPDATE files SET mtime=?, content_hash=?, source_hash=?,
                           flags_hash=?
                           WHERE id=?""",
                        (current_mtime, content_hash_val, source_hash, flags_hash, file_id),
                    )
                else:
                    # New config_hash — create file record for this TU
                    file_id = upsert_file(
                        conn,
                        config_hash,
                        file_path_str,
                        unit.language,
                        mtime=current_mtime,
                        content_hash=content_hash_val,
                        source_hash=source_hash,
                        flags_hash=flags_hash,
                    )
            elif file_id is not None:
                # Tier 1: mtime match — just refresh stored mtime
                conn.execute(
                    "UPDATE files SET mtime=? WHERE id=?",
                    (current_mtime, file_id),
                )
            # For "reuse": copy symbols + refs from old config_hash
            if is_reuse and file_id is not None:
                syms_copied = _reassign_symbols_for_file(
                    conn, config_hash, file_id, file_path_str,
                )
                if syms_copied > 0:
                    total_syms = syms_copied
                else:
                    # No old data to migrate — clear the file record
                    # so orphan cleanup handles it, then fall through
                    # to Phase 2 for a real libclang parse.
                    log.info(
                        "[%d/%d] %s: reuse produced 0 symbols — re-parsing",
                        processed, total_units, fname,
                    )
                    conn.execute("UPDATE files SET content = '' WHERE id = ?", (file_id,))
                    fallthrough = True

            if not fallthrough:
                # Fill ifdef-filtered file content via tokenization
                fc, hdrs = _build_filtered_file_content(
                    conn, unit, config_hash, project_root, build_dir_patterns=build_dir_patterns
                )
                content_filled = fc
                if hdrs:
                    try:
                        tu_key = str(unit.file.resolve().relative_to(project_root))
                    except ValueError:
                        tu_key = str(unit.file.resolve())
                    headers = {tu_key: hdrs}

    return {
        "fallthrough": fallthrough,
        "file_id": file_id,
        "headers": headers,
        "status": "reused (manifest)" if is_reuse else ("unchanged (content)" if hashes is not None else "unchanged"),
        "is_reuse": is_reuse,
        "total_syms": total_syms,
        "content_filled": content_filled,
    }
