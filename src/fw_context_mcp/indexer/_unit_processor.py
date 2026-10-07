"""Unit-level processing for the incremental indexing pipeline.

Position in the pipeline
------------------------
1. The **runner** (:mod:`runner`) gives all translation units (TUs) to
   :func:`decide_units` before the loop: hashes only, no database writes.
   It says for each TU if it is unchanged.
2. The runner then iterates the TUs, one at a time.  A changed TU goes
   to :func:`_parse_unit` (libclang, no lock), and an unchanged one to
   :func:`_handle_unchanged`.
3. :func:`_handle_unchanged` and :func:`_process_unit` serialise via
   ``write_lock`` and persist the results.

Design principles
-----------------
* **Decide by content, never by time** — a TU is unchanged only when
  the manifest holds an entry for this listing with the same flags hash,
  and the source file and every header that the parse read still have
  the hash of that entry.  A file time cannot answer: git checkout, touch
  and a build move it without a change, and a copy with an older time
  (``cp -p``, a restore from a backup) brings a change with no new time.
  Measured: the hashes of every unit of a project, all builds, take 0.1 to
  2.1 s.  One libclang parse takes seconds.
* **No cross-build import** — a TU is either up to date under the
  current ``config_hash`` or it is re-parsed.  Rows are never copied
  from another build: ``config_hash`` identifies the compilation
  dialect, so another build's rows were produced by different macros
  and say nothing about this one.  Adding or removing a source file no
  longer changes ``config_hash``, so the case this used to optimise
  does not arise.
* **Parse outside the lock, serialise writes** — libclang parsing takes
  seconds per TU and runs without any lock.  Only database writes take
  the lock, thus a manual operation can interleave between two TUs.
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

import logging
import os
import sqlite3
import time
from collections.abc import Collection
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path

try:
    from clang.cindex import TranslationUnitLoadError
except ImportError:
    TranslationUnitLoadError = RuntimeError  # clang not available — use fallback

from ..utils import SAFE_EXCEPT, compute_source_hash, is_fatal
from .config_hash import compute_flags_hash
from .db import (
    open_db,
    transaction,
    write_lock,
)
from .ops import _build_filtered_file_content, _normalize_file_path, store_symbols_for_unit

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnitDecision:
    """What :func:`decide_units` says about one listing of compile_commands.json.

    *hashes* is ``(source_hash, flags_hash)`` of the listing, for its
    ``files`` row.  *mtime* is the time of the source file, read BEFORE its
    hash, thus the two describe the same text or the time is older (0.0
    when the file is gone).  *unchanged* is True when the index holds what a
    parse would give, and no parse is necessary.
    """

    hashes: tuple[str, str]
    mtime: float
    unchanged: bool


def decide_units(
    units: list,
    project_root: Path,
    existing_files,
    manifest: dict[str, list[dict]] | None,
    header_stale_tus: frozenset[str] = frozenset(),
    *,
    force: bool,
    hash_cache: dict[str, str] | None,
    header_table: dict[str, dict] | None,
    transient_defines: Collection[str],
) -> list[UnitDecision]:
    """Decide for each unit, in order, if it must be parsed again.

    A listing is unchanged when all of these are true:

    * No ``force`` and no ``FW_CONTEXT_FORCE_REFINDEX=1``.
    * The file is not in *header_stale_tus*.
    * The index holds a row for the file.
    * *manifest* holds an entry for the file with the flags hash of THIS
      listing (see :func:`_unchanged_entry`), and that entry is not stale:
      the source file and each header have the hash of the entry, and the
      entry has no ``needs_reparse`` mark.
    * No other listing of the same file must be parsed.

    No file time takes part.  Before, the first test was "the source file
    is not newer than the stored time", which did not see a copy with an
    older time, nor a change of the flags or of a header (the header pass
    covered the last one).

    The last rule is why all units are decided before the first parse.
    compile_commands.json can list one file more than once with other
    flags, and the index holds ONE set of rows for a file: the rows of the
    listing that the run parsed last.  A run that parsed only the listing
    whose flags changed left rows that depend on the order of the listings,
    and a full run gave other rows.  Thus each listing of the file is parsed.

    Args:
        manifest: ``{file: [entry, ...]}`` from ``manifest.load()``, one
            entry for each listing of the file in compile_commands.json.
            ``None`` (no manifest) parses every unit.
        header_stale_tus: Normalized paths of TUs that include a header
            whose content changed, or whose entry carries ``needs_reparse``
            (from ``runner._tus_to_requeue``).
        hash_cache: Shared ``{resolved path: sha256}`` memo for header
            hashes, so a header included by many TUs is read once per index
            run.
        header_table: The ``headers`` map of the manifest.  An entry holds
            header paths only, and their hashes are in this map.
        transient_defines: ``[index] transient_defines``, for the flags hash
            of the listing.  The manifest entries hold hashes of the same list.
    """
    force_all = force or os.environ.get("FW_CONTEXT_FORCE_REFINDEX") == "1"
    paths: list[str] = []
    hashes: list[tuple[str, str]] = []
    mtimes: list[float] = []
    own: list[bool] = []
    for unit in units:
        resolved_tu = unit.file.resolve()
        file_path = _normalize_file_path(str(resolved_tu), project_root)
        # The time first, then the hash: a save between the two leaves the
        # row with an older time than the text, and the staleness check of
        # the query side then hashes the file again.
        try:
            mtime = unit.file.stat().st_mtime
        except OSError:
            mtime = 0.0
        # compute_source_hash gives "" for a file it cannot read (a source
        # that the build lists and the disk lost): no entry holds "".
        source_hash = compute_source_hash(unit.file)
        flags_hash = (
            compute_flags_hash(unit.raw_entry, transient_defines=transient_defines)
            if unit.raw_entry is not None else ""
        )
        unchanged = False
        if (
            not force_all
            and file_path not in header_stale_tus
            and file_path in existing_files
            and manifest is not None
        ):
            try:
                tu_rel = str(resolved_tu.relative_to(project_root))
            except ValueError:
                tu_rel = str(resolved_tu)
            unchanged = _unchanged_entry(
                manifest.get(tu_rel, ()), source_hash, flags_hash, project_root,
                hash_cache=hash_cache, header_table=header_table,
            ) is not None
        paths.append(file_path)
        hashes.append((source_hash, flags_hash))
        mtimes.append(mtime)
        own.append(unchanged)

    behind = {path for path, unchanged in zip(paths, own, strict=True) if not unchanged}
    decisions = [
        UnitDecision(hashes=h, mtime=mtime, unchanged=unchanged and path not in behind)
        for path, h, mtime, unchanged in zip(paths, hashes, mtimes, own, strict=True)
    ]
    siblings = sum(1 for unchanged, d in zip(own, decisions, strict=True) if unchanged and not d.unchanged)
    if siblings:
        log.info("%d unit(s) parsed because another listing of the same file changed", siblings)
    return decisions


def _parse_unit(unit, index_refs, skip_files: set[str] | None, hashes: tuple[str, str]):
    """Parse a unit that :func:`decide_units` did not find unchanged.

    Does NOT write to the database — the caller is responsible for
    acquiring ``write_lock`` and calling ``_process_unit(pre_parsed=...)``
    to persist the result.

    Returns:
        * ``("skipped", None, None, None)`` — parse failed.
        * ``("updated", parsed, (t_start, t_end), hashes)`` — parsed
          successfully, ready for ``_process_unit(pre_parsed=parsed)``.
    """
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
    # Measured on the Mbed project: a branch switch removed two generated zcbor
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


def _unchanged_entry(
    entries,
    source_hash: str,
    flags_hash: str,
    project_root: Path,
    *,
    hash_cache: dict[str, str] | None,
    header_table: dict[str, dict] | None,
) -> dict | None:
    """Return the manifest entry that proves a listing unchanged, or None.

    *entries* are the entries of one file, one for each listing in
    compile_commands.json.  The entry of this listing is the one with
    *flags_hash*: Zephyr compiles ``misc/empty_file.c`` three times in one
    image with two sets of flags, and the entry of one set says nothing
    about the other.  Two listings with the same flags have entries that
    say the same, thus the first one answers.

    *source_hash* is the hash of the source file that the caller read, so
    that check_tu_staleness does not read the file a second time.

    An empty *flags_hash* (a unit without a compile_commands entry) or an
    entry without a source hash (a preliminary manifest) proves nothing,
    and the unit is parsed.
    """
    from .manifest import check_tu_staleness, resolve_headers

    if not flags_hash:
        return None
    for entry in entries:
        if entry.get("flags_hash") != flags_hash or not entry.get("source_hash"):
            continue
        stale, _ = check_tu_staleness(
            entry, project_root,
            hash_cache=hash_cache, headers=resolve_headers(entry, header_table),
            source_hash=source_hash,
        )
        return None if stale else entry
    return None


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
    *,
    pre_parsed,
    parse_timing=(0.0, 0.0),
    hashes: tuple[str, str] | None = None,
    build_dir_patterns=None,
    skip_files: frozenset[str] | None = None,
):
    """Store one translation unit that :func:`_parse_unit` parsed.

    Opens its own DB connection when *conn* is ``None``, otherwise reuses
    the caller-supplied connection (persistent per-worker connection).

    Serializes DB writes via *lock* when supplied (``threading.Lock`` for
    intra-process synchronization).  When *lock* is ``None``, the caller
    is responsible for serialisation (sequential path with fcntl wrap).

    The caller decided and parsed (*pre_parsed*), thus the lock is only
    held for the DB write.  *parse_timing* provides the ``(t_start,
    t_end)`` values for the summary statistics.  This function had its own
    decision and parse for a call without *pre_parsed*, by the file time;
    only tests used it.

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
        existing_files: ``{path: FileHashRecord}`` of the build, for the
            ``file_id`` of each row that the store replaces.
        lock: Optional ``threading.Lock`` used as a context manager to
            serialise DB writes between workers (intra-process).
        conn: Optional persistent SQLite connection — when provided, the
            caller manages its lifecycle (open once per worker thread,
            close after all TUs).  When ``None``, a connection is opened
            and closed for this call.
        pre_parsed: The result of ``extract_all()`` for this unit.
        parse_timing: ``(t_start, t_end)`` tuple from the caller's
            ``time.monotonic()`` measurements around the parse step.
        hashes: ``(source_hash, flags_hash)`` of the listing, for its
            ``files`` row.

    Returns:
        A tuple ``(status, symbols_added, refs_added, timing, headers)`` where
        *status* is ``"updated"`` (new or modified symbols stored) or
        ``"skipped"`` (the store failed), and
        *headers* is a list of ``{path, hash, generated}`` dicts for included
        header files (empty list for skipped).
    """

    parsed = pre_parsed
    t_parse_start, t_parse_end = parse_timing

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
    hashes: tuple[str, str],
    mtime: float,
    conn: sqlite3.Connection,
    config_hash: str,
    project_root: Path,
    build_dir_patterns: list[str] | None,
    db_path: Path,
    existing_files: dict,
    skip_files: frozenset[str] | None = None,
    content_backfill_needed: bool = True,
) -> dict:
    """Handle a TU that needs no re-parse — file-record and content bookkeeping.

    Updates the TU's file record and fills ifdef-filtered content.  The caller
    is responsible for applying the returned counters to its own state.

    Args:
        unit: The compilation unit being processed.
        hashes: ``(source_hash, flags_hash)`` of the listing, from
            :func:`decide_units`.
        mtime: The time of the source file that :func:`decide_units` read
            with the hash.  The decision runs before the loop, hours before
            this call on a large project: the time of the file NOW can
            belong to a text saved after the hash.  The query side decides
            by the hash; the time is the one that a row without a hash would
            give, and it must not claim a newer text than the hash.
        conn: Open SQLite connection to the index database.
        config_hash: Active build config hash.
        project_root: Project root directory.
        build_dir_patterns: Build directory exclusion patterns.
        db_path: Path to the index database file.
        existing_files: Dict mapping file paths to ``FileHashRecord``.
        skip_files: Headers already processed by earlier TUs in this run.
        content_backfill_needed: True when some file in the index still has
            an empty ``content`` column.  Computed once per run by the
            caller; when True the header/content pass runs even for
            unchanged TUs so the backfill can complete.

    Returns:
        dict with keys:
        - ``file_id`` (int | None): File record ID for use in Phase 2.
        - ``headers`` (dict | None): Collected headers for manifest update.
        - ``status`` (str): ``"unchanged"``.
        - ``content_filled`` (int): 1 if ifdef content was filled, 0 otherwise.
    """
    file_path_str = _normalize_file_path(str(unit.file.resolve()), project_root)
    try:
        tu_key = str(unit.file.resolve().relative_to(project_root))
    except ValueError:
        tu_key = str(unit.file.resolve())

    # An unchanged TU has nothing to contribute to the manifest: its stored
    # entry is still accurate, and decide_units found it, with real
    # hashes.  Running the header/content pass anyway would cost a full
    # libclang parse per TU AND stamp current header hashes onto a TU that
    # was never re-parsed — erasing the only signal that its headers are
    # stale.  Only the content backfill needs the pass.
    skip_header_pass = not content_backfill_needed
    # The decision found a row: an unchanged unit always has one.
    file_id = existing_files[file_path_str].file_id

    content_filled = 0
    headers = None

    # Serialise via write_lock with 120 s timeout.
    # 120 s allows the slowest TU to finish its Phase 2 write before
    # this TU acquires the lock.  Shorter timeouts risk false
    # failures on CI machines with contended I/O.
    with write_lock(db_path.parent, timeout=120.0):
        with transaction(conn, checkpoint=False):
            # The row gets the hashes that the decision read.  They are the
            # hashes that the index holds (the decision compared them), and
            # the staleness checks of mcp/shared/stale.py read source_hash.
            # The time is the one of the decision, read with the hash.
            source_hash, flags_hash = hashes
            conn.execute(
                "UPDATE files SET mtime=?, source_hash=?, flags_hash=? WHERE id=?",
                (mtime, source_hash, flags_hash, file_id),
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
        "status": "unchanged",
        "content_filled": content_filled,
    }
