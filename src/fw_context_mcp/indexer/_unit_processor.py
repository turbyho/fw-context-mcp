"""Unit-level processing for the incremental indexing pipeline.

Position in the pipeline
------------------------
1. The **runner** (:mod:`runner`) iterates all translation units (TUs)
   and delegates each to this module for staleness checking, parsing,
   and persistence.  This module is the hot path — every TU passes
   through it.
2. **Phase 1** (parallel): :func:`_check_and_parse_unit` runs in a
   thread pool — no database writes.  It determines whether each TU
   is unchanged (skip) or changed (re-parse with libclang).
3. **Phase 2** (serialised): :func:`_handle_unchanged` and
   :func:`_process_unit` serialise via ``write_lock`` and persist
   the results.

Design principles
-----------------
* **Avoid libclang when possible** — three-tier staleness check
  (mtime → content-hash → libclang parse).  mtime is ~100× cheaper
  than content-hashing; content-hashing is ~50× cheaper than
  libclang parsing.
* **No cross-build import** — a TU is either up to date under the
  current ``config_hash`` or it is re-parsed.  Rows are never copied
  from another build: ``config_hash`` identifies the compilation
  dialect, so another build's rows were produced by different macros
  and say nothing about this one.  Adding or removing a source file no
  longer changes ``config_hash``, so the case this used to optimise
  does not arise.
* **Parallel parse, serialise writes** — libclang parsing is
  CPU-bound and runs in a thread pool without any lock.  Only
  database writes are serialised, maximising throughput on
  multi-core machines.
* **Thread-safe connection management** — callers can supply a
  persistent per-worker connection (avoiding per-TU open/close
  overhead) or let this module manage its own.

Key decisions
-------------
* Shared headers (included by multiple TUs) are claimed by exactly one
  TU per run via ``skip_files`` — the first TU to walk a header owns
  its rows, and later TUs skip its subtree entirely.
* Overrides are rebuilt from scratch in post-processing by
  ``_build_overrides`` because the override graph depends on the full
  set of indexed classes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

try:
    from clang.cindex import TranslationUnitLoadError
except ImportError:
    TranslationUnitLoadError = RuntimeError  # clang not available — use fallback

from ..utils import MTIME_TOLERANCE_S, SAFE_EXCEPT, compute_source_hash, is_fatal
from .config_hash import compute_flags_hash, compute_tu_content_hash
from .db import (
    open_db,
    transaction,
    upsert_file,
    write_lock,
)
from .ops import _build_filtered_file_content, _normalize_file_path, store_symbols_for_unit

log = logging.getLogger(__name__)


def _check_and_parse_unit(
    unit,
    config_hash,
    project_root,
    index_refs,
    existing_files,
    force=False,
    manifest=None,
    skip_files: set[str] | None = None,
    header_stale_tus: frozenset[str] = frozenset(),
    hash_cache: dict[str, str] | None = None,
    header_table: dict[str, dict] | None = None,
):
    """Check whether *unit* needs re-parsing and parse it if so.

    Uses a three-tier staleness check:
    1. **mtime fast-path** — unchanged mtime → skip (no I/O).
    2. **content-hash check** — mtime differs but hashes match → skip
       (the source, flags, and header dependencies have not changed).
       Uses ``manifest.json`` for header hashes when available — no libclang needed.
    3. **libclang parse** — content hashes differ → parse.

    Tier 1 does not look at the flags.  A build whose flags changed but
    whose sources did not moves no mtime, so the check stops here.

    Does NOT write to the database — the caller is responsible for
    acquiring ``write_lock`` and calling ``_process_unit(pre_parsed=...)``
    to persist the result.

    All translation units are indexed — no exclusion filtering.

    Args:
        manifest: Optional ``{file_path: entry}`` lookup dict built from
            ``manifest.load()`` entries.  When provided, header staleness
            is checked via hash comparison against the manifest (fast —
            file reads + SHA-256 only).  When ``None``, falls back to
            source-hash-only comparison.
        header_stale_tus: Normalized paths of TUs that include a header
            whose content changed (from ``manifest.collect_stale_headers``).
            For those TUs both cheap tiers are skipped: a header change
            never moves the TU's own mtime, so Tier 1 would report
            "unchanged" and the header's symbols would never be refreshed.
        hash_cache: Shared ``{resolved path: sha256}`` memo for project
            header hashes, so a header included by many TUs is read once
            per index run.

    Returns:
        * ``("unchanged", None, None, None)`` — no re-parse needed (Tier 1 mtime match).
        * ``("unchanged", None, None, hashes)`` — no re-parse needed (Tier 2 content-hash match).
        * ``("skipped", None, None, None)`` — parse failed.
        * ``("updated", parsed, (t_start, t_end), hashes)`` — parsed
          successfully, ready for ``_process_unit(pre_parsed=parsed)``.
          *hashes* is ``(source_hash, flags_hash, manifest_entry_hash)``.
    """
    resolved_tu = unit.file.resolve()

    # ── Three-tier staleness strategy ──
    # Each tier is 1-2 orders of magnitude cheaper than the next:
    #   Tier 1 — mtime comparison (stat syscall, no hash computation)
    #   Tier 2 — content-hash comparison (SHA-256 of source + flags +
    #            headers)
    #   Tier 3 — libclang parse + full AST walk (seconds per TU)
    # Tiers 1 and 2 are lock-free — no DB write contention.  Only
    # Tier 3 results require serialised persistence in Phase 2.

    file_path = _normalize_file_path(str(resolved_tu), project_root)
    force_refs = force or os.environ.get("FW_CONTEXT_FORCE_REFINDEX") == "1"

    # A changed header leaves the TU's own mtime and source hash untouched,
    # so both cheap tiers would wave this TU through.  Skip them and let
    # Tier 3 re-parse — that is the only way the header's symbols and its
    # ifdef-filtered content get refreshed.
    tu_header_stale = file_path in header_stale_tus

    # ── Tier 1: mtime fast-path ──
    if not force_refs and not tu_header_stale and file_path in existing_files:
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
        manifest,
        hash_cache=hash_cache,
        header_table=header_table,
    )

    content_hash = compute_tu_content_hash(source_hash, flags_hash, manifest_entry_hash)
    hashes = (source_hash, flags_hash, manifest_entry_hash)

    if not force_refs and not tu_header_stale and file_path in existing_files:
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
            with_refs=index_refs,
            return_tu=True,
            skip_files=skip_files,
        )
    except sqlite3.Error:
        log.error("Fatal DB error parsing %s — stopping indexer", unit.file.name)
        raise
    # TranslationUnitLoadError is named next to SAFE_EXCEPT and not inside
    # it: the class derives straight from Exception, so the tuple
    # `(ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error,
    # OSError)` does not hold it.  Without the name here the exception left
    # the run, and `ops.py` — which handles the same failure by skipping —
    # never saw the unit at all.
    #
    # Measured on zbox-ecb-fw: a branch switch removed two generated zcbor
    # sources that compile_commands.json still listed, and those two files
    # out of 881 ended the run at translation unit 39.  The 842 units behind
    # them were never read.
    except (*SAFE_EXCEPT, TranslationUnitLoadError) as exc:
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


def _get_manifest_entry_hash_for_unit(
    unit,
    project_root: Path,
    manifest_lookup: dict[str, dict] | None,
    *,
    hash_cache: dict[str, str] | None = None,
    header_table: dict[str, dict] | None = None,
) -> str:
    """Return the manifest entry hash for a TU for Tier 2 staleness comparison.

    When *manifest_lookup* is available (``{file_path: entry}`` dict from
    ``manifest.json``), checks header staleness via ``check_tu_staleness()``
    — fast file reads + SHA-256 only, no libclang.

    When *manifest_lookup* is ``None``, falls back to a source-only hash
    (no header tracking possible).

    *header_table* is the manifest's shared ``headers`` map.  The entries hold
    header paths only; without the table they resolve to empty hashes and
    every TU reads as stale.
    """
    from .manifest import check_tu_staleness, compute_current_entry_hash, resolve_headers
    from .manifest import get_manifest_entry_hash as _entry_hash

    # ── Manifest path (fast — no libclang, O(1) lookup) ──
    if manifest_lookup is not None:
        try:
            tu_rel = str(unit.file.resolve().relative_to(project_root))
        except ValueError:
            tu_rel = str(unit.file.resolve())

        entry = manifest_lookup.get(tu_rel)
        if entry is not None:
            headers = resolve_headers(entry, header_table)
            stale, current_source_hash = check_tu_staleness(
                entry, project_root,
                hash_cache=hash_cache, headers=headers,
            )
            if not stale:
                return _entry_hash(entry, headers)
            # Stale — compute hash from CURRENT disk content (both source and headers)
            return compute_current_entry_hash(
                entry,
                project_root,
                new_source_hash=current_source_hash,
                hash_cache=hash_cache,
                headers=headers,
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
    skip_files: frozenset[str] | None = None,
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
                skip_files=skip_files,
            )
        # Same reason as in `_check_and_parse_unit`: the parse error is not
        # inside SAFE_EXCEPT, thus it has to be named.
        except (*SAFE_EXCEPT, TranslationUnitLoadError) as exc:
            if is_fatal(exc):
                raise
            log.warning("skip TU %s: %s", unit.file.name, exc)
            return ("skipped", 0, 0, (0.0, 0.0, 0.0), [])
        t_parse_end = time.monotonic()

    # Resolve connection: caller-supplied or own.
    # Persistent per-worker connections avoid open()+close() per TU
    # (~0.5 ms each).  When the caller manages the lifecycle (open
    # once per thread, close after all TUs), total indexing time
    # drops by 5-10 % on large projects with many small TUs.
    if conn is not None:
        own_conn = False  # caller-supplied, don't close
    else:
        conn = open_db(db_path)
        own_conn = True

    t_lock_start = time.monotonic()
    try:
        # threading.Lock (intra-process) or nullcontext (sequential path
        # where the caller holds fcntl write_lock across all TUs)
        lock_ctx: AbstractContextManager = lock if lock is not None else nullcontext()
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
                    skip_files=skip_files,
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




def _handle_unchanged(
    unit,
    check_status: str,
    hashes: tuple | None,
    conn: sqlite3.Connection,
    config_hash: str,
    project_root: Path,
    build_dir_patterns: list[str] | None,
    db_path: Path,
    existing_files: dict,
    skip_files: frozenset[str] | None = None,
    manifest_lookup: dict[str, dict] | None = None,
    content_backfill_needed: bool = True,
) -> dict:
    """Handle a TU that needs no re-parse — file-record and content bookkeeping.

    Updates the TU's file record and fills ifdef-filtered content.  The caller
    is responsible for applying the returned counters to its own state.

    Args:
        unit: The compilation unit being processed.
        check_status: Always ``"unchanged"`` (from
            :func:`_check_and_parse_unit`).  Kept as a parameter so the
            caller's tier dispatch stays explicit.
        hashes: ``(source_hash, flags_hash, manifest_entry_hash)`` tuple
            from the Tier 2 content-hash check, or ``None`` for Tier 1.
        conn: Open SQLite connection to the index database.
        config_hash: Active build config hash.
        project_root: Project root directory.
        build_dir_patterns: Build directory exclusion patterns.
        db_path: Path to the index database file.
        existing_files: Dict mapping file paths to ``FileHashRecord``.
        skip_files: Headers already processed by earlier TUs in this run.
        manifest_lookup: ``{tu path: manifest entry}`` from ``manifest.json``.
            A usable entry (real ``source_hash`` and ``flags_hash``) lets an
            unchanged TU skip the header/content pass entirely — see the
            comment below.
        content_backfill_needed: True when some file in the index still has
            an empty ``content`` column.  Computed once per run by the
            caller; when True the header/content pass runs even for
            unchanged TUs so the backfill can complete.

    Returns:
        dict with keys:
        - ``file_id`` (int | None): File record ID for use in Phase 2.
        - ``headers`` (dict | None): Collected headers for manifest update.
        - ``status`` (str): ``"unchanged (content)"`` or ``"unchanged"``.
        - ``content_filled`` (int): 1 if ifdef content was filled, 0 otherwise.
    """
    file_path_str = _normalize_file_path(str(unit.file.resolve()), project_root)
    try:
        tu_key = str(unit.file.resolve().relative_to(project_root))
    except ValueError:
        tu_key = str(unit.file.resolve())

    # An unchanged TU has nothing to contribute to the manifest: its stored
    # entry is still accurate.  Running the header/content pass anyway would
    # cost a full libclang parse per TU AND stamp current header hashes onto
    # a TU that was never re-parsed — erasing the only signal that its
    # headers are stale.  A preliminary or pre-flags_hash entry does not
    # qualify: those must be regenerated with real hashes.
    #
    # "reuse" is excluded — that path migrates a TU to a new config_hash and
    # the new manifest still needs its entry.
    manifest_entry = manifest_lookup.get(tu_key) if manifest_lookup else None
    manifest_entry_usable = bool(
        manifest_entry
        and manifest_entry.get("source_hash")
        and manifest_entry.get("flags_hash")
    )
    skip_header_pass = (
        check_status == "unchanged" and manifest_entry_usable and not content_backfill_needed
    )
    rec = existing_files.get(file_path_str)
    file_id = rec.file_id if rec else None
    try:
        current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
    except OSError:
        current_mtime = 0.0

    content_filled = 0
    headers = None

    # Serialise via write_lock with 120 s timeout.
    # 120 s allows the slowest TU to finish its Phase 2 write before
    # this TU acquires the lock.  Shorter timeouts risk false
    # failures on CI machines with contended I/O.
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
            if not skip_header_pass:
                # Fill ifdef-filtered file content via tokenization
                fc, hdrs = _build_filtered_file_content(
                    conn, unit, config_hash, project_root, build_dir_patterns=build_dir_patterns,
                    skip_files=skip_files,
                )
                content_filled = fc
                if hdrs:
                    headers = {tu_key: hdrs}

    return {
        "file_id": file_id,
        "headers": headers,
        "status": "unchanged (content)" if hashes is not None else "unchanged",
        "content_filled": content_filled,
    }
