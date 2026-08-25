"""Index runner: parse compile_commands.json, extract symbols, store to SQLite.

WHY a separate runner: the indexing pipeline has four distinct phases
(preparation, parsing, post-processing, optional analysis) that are
orchestrated in sequence.  Keeping them in one module allows the
write-lock and heartbeat mechanisms to span the entire run.

Uses ``indexer/ops.py`` for the shared "parse TU → store symbols" loop so
that runner, reindex_file, and auto-reindex all use the same code path.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from ..config.settings import derive_project_id
from ..mcp.shared.pid_file import PidFile
from ._embedding import (
    _build_embeddings,
    _chunk_body,
    _cleanup_orphaned_cc_artifacts,
    _fmt_dur,
    _truncate_body,
)
from ._llm_analysis import _fetch_callees, _fetch_referencers
from ._postprocess import (
    _build_hotspot_cache,
    _build_overrides,
    _build_pagerank,
    _run_postprocess,
)
from ._unit_processor import (
    _check_and_parse_unit,
    _handle_unchanged,
    _process_unit,
)
from .compile_commands import _SOURCE_EXTS, validate_include_files
from .compile_commands import parse as parse_compile_commands
from .db import (
    drop_fts_triggers,
    get_file_hashes,
    open_db,
    rebuild_fts,
    transaction,
    upsert_build_config,
    upsert_project,
    write_lock,
)

log = logging.getLogger(__name__)

__all__ = [
    "_build_embeddings",
    "_build_hotspot_cache",
    "_build_overrides",
    "_build_pagerank",
    "_chunk_body",
    "_cleanup_orphaned_cc_artifacts",
    "_fmt_dur",
    "_fetch_callees",
    "_fetch_referencers",
    "_run_postprocess",
    "_truncate_body",
    "run",
]



# ═══════════════════════════════════════════════════════════════
# SECTION: Embedding building (→ llm_analysis.py)
# ═══════════════════════════════════════════════════════════════


# Process exit status for a run that stopped because another process took the
# index over.  Distinct from 0 (done) and 1 (failed) so the daemon can tell
# the three apart: this one means the work still has to happen, from the
# start.  75 is EX_TEMPFAIL from sysexits.h — a temporary failure, try again.
#
# Lives beside the exception rather than in the CLI: the producer (the CLI)
# and the consumer (the daemon) are in different layers, and both already
# depend on the indexer.
EXIT_SUPERSEDED = 75

# Process exit status for a run that refused to start because another
# indexing run already owns the index.  Unlike EXIT_SUPERSEDED there is
# nothing to retry: the run that holds the lock is doing this work.
# 69 is EX_UNAVAILABLE from sysexits.h — the service is busy.
EXIT_ALREADY_RUNNING = 69


class IndexSuperseded(Exception):
    """Another process took over the index, so this run gave up.

    Not a failure: nothing is wrong with the index, and nothing was written
    that has to be undone.  The run simply stopped because continuing would
    have applied decisions made from a snapshot the other process has since
    invalidated.

    Deliberately outside :data:`SAFE_EXCEPT` — a handler that swallowed this
    as "a step failed, carry on" would resume exactly the run this exists to
    stop.  Callers report it as superseded and retry the work from the start.
    """


def raise_if_superseded(db_dir: Path) -> None:
    """Raise :class:`IndexSuperseded` when another process owns the index.

    The MCP server writes ``<pid>`` to ``reindex.pause`` before a manual
    ``reindex_file`` or ``reset_index``.  An indexing run used to block here
    until the marker cleared and then carry on from the same translation unit
    — which is not safe.  Everything the loop decides with was captured
    before the pause: ``existing_files`` (the mtime and hash snapshot), the
    manifest lookup, the stale-header set, and the header ownership built up
    TU by TU.  After a manual operation those all describe an index that no
    longer exists.

    The damage is concrete.  ``reset_index`` empties the database; a resumed
    run then reads its pre-pause ``existing_files``, takes the mtime
    fast-path for translation units whose rows are gone, and finishes by
    stamping a manifest over a half-built index.  A ``reindex_file`` is
    subtler but the same shape: the manual parse re-assigned header
    ownership, and the resumed run's ``replace_file_data`` deletes rows it
    just wrote.

    So the run gives up instead.  The caller reports it as superseded rather
    than as a failure, and the work is retried from the start — with a fresh
    snapshot — once the marker clears.

    A marker holding our own PID is ignored: a foreground index writes it to
    stop the background reindex, not to stop itself.  A marker whose process
    is gone is ignored too — nothing owns the index any more.
    """
    pause_file = db_dir / "reindex.pause"
    if not PidFile.is_active(pause_file):
        return
    requester_pid = PidFile.read_pid(pause_file)
    if requester_pid is None or requester_pid == os.getpid():
        return
    raise IndexSuperseded(
        f"another process (pid {requester_pid}) took over the index; "
        f"abandoning this run so it can be redone from a fresh snapshot"
    )





def _tus_to_requeue(
    manifest: dict,
    stale_headers: set[str],
    project_root: Path,
) -> frozenset[str]:
    """Return the normalized TU paths that must skip the cheap staleness tiers.

    Two sources, and the union of them:

    - a TU that includes a header whose content changed, and
    - a TU whose entry carries ``needs_reparse``.

    The second source must be read even when *stale_headers* is empty.  That
    is the whole point of the mark: another unit's re-parse already wrote the
    current hash into the shared header table, so the difference the header
    pass looks for is gone.  Built inside an ``if stale_headers:`` the set
    stays empty and the mark reaches nothing.
    """
    from .manifest import tus_affected_by_headers
    from .ops import _normalize_file_path

    flagged = {
        e["file"] for e in manifest.get("entries", []) if e.get("needs_reparse")
    }
    from_headers = tus_affected_by_headers(manifest, stale_headers)
    affected = from_headers | flagged
    if not affected:
        return frozenset()

    log.info(
        "%d changed header(s) -> %d TU(s) queued for reparse, "
        "%d more queued by needs_reparse",
        len(stale_headers), len(from_headers), len(flagged - from_headers),
    )
    return frozenset(
        _normalize_file_path(str((project_root / tu_rel).resolve()), project_root)
        for tu_rel in affected
    )


def run(
    compile_commands: Path,
    db_path: Path,
    vendor_paths: list[str] | None = None,
    project_paths: list[str] | None = None,
    project_name: str | None = None,
    index_refs: bool = False,
    index_embeddings: bool = False,
    analyze_symbols: bool = False,
    analyze_overrides: bool = True,
    project_root: Path | None = None,
    project_id: str | None = None,
    llm_config=None,
    cache_server_config=None,
    force: bool = False,
    index_macros_expanded: bool = True,
    config_header: str = "",
    build_dir_patterns: list[str] | None = None,
    build_system: str | None = None,
    analyze_vendor: bool = False,
    purge_max_missing_percent: int = 20,
    variant: str = "",
    image: str = "",
    board: str = "",
    build_env: dict[str, str] | None = None,
    defer_fts: bool = False,
    defer_cleanup: bool = False,
) -> str:
    """Index a project: parse translation units, extract symbols, and store to SQLite.

    This is the main entry point for indexing a firmware project.  It reads
    ``compile_commands.json``, parses every translation unit with libclang,
    deduplicates symbols across files, and persists the result to the SQLite
    database at ``db_path``.  Optionally builds embeddings, LLM-based symbol
    analysis, file-level summaries, and the method override graph.

    All files from all included headers are indexed unconditionally.
    ``is_project`` is computed per-symbol from *vendor_paths* (auto-detected
    SDK dirs + user-configured) and *project_paths* (user-configured overrides).

    Args:
        compile_commands: Path to ``compile_commands.json`` (generated by
            ``bear``, ``compiledb``, or ``CMAKE_EXPORT_COMPILE_COMMANDS``).
        db_path: Path to the SQLite database file that will store the index.
        vendor_paths: Additional vendor/SDK directory patterns (additive to
            auto-detection).  Paths matching these get ``is_project=0``.
        project_paths: Manual project directory patterns that override
            auto-detection.  Paths matching these get ``is_project=1``.
            Use absolute paths for directories outside the project root.
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
        build_system: The ``[build] system`` config key of this project.  It
            decides WHICH builder answers for the vendor patterns.  Pass it
            whenever the config has it: the config and the markers can
            disagree, and a freestanding NCS application reads as a CMake
            project by its markers alone.  With None the markers decide.
        project_id: Unique project identifier (auto-derived from
            ``project_root`` when not provided).
        llm_config: Configuration dataclass for Ollama connection (URL,
            model names, enabled flag).  Required when any ``index_*`` or
            ``analyze_*`` option is enabled.

    Returns:
        The ``config_hash`` string — a content-addressable fingerprint of the
        ``compile_commands.json`` used for staleness detection.
    """
    if project_root is None:
        project_root = compile_commands.parent.resolve()
    else:
        project_root = project_root.resolve()

    # Normalize patterns without % wildcard to match subdirectories.  The
    # vendor set waits for the translation units — see below.
    from .sdk_detect import _build_sdk_excludes, _normalize_patterns

    project_patterns_list = _normalize_patterns(list(project_paths)) if project_paths else []

    if project_id is None:
        project_id = derive_project_id(project_root)
    name = project_name or project_root.name
    # Collect git context for build description (branch + last tag).
    from .git_context import get_git_description

    git_description = get_git_description(project_root)
    # Clean up compile_commands.<hash>.json artifacts left by a previous
    # interrupted run before computing a new config_hash.
    _cleanup_orphaned_cc_artifacts(db_path, project_id)
    # Parse compile_commands.json to discover translation units.  Must
    # happen before config_hash computation so the manifest can be built
    # from the actual TU list.
    units = list(parse_compile_commands(compile_commands))
    units = [u for u in units if u.file.suffix.lower() in _SOURCE_EXTS]
    log.info("TUs to index: %d", len(units))

    # ── The effective vendor set, computed HERE and not earlier ──
    # A builder may read the compiler flags: Zephyr takes ZEPHYR_BASE and
    # WEST_TOPDIR from -fmacro-prefix-map, which names the roots of the build
    # that is indexed rather than the shell that runs fw-context.  The units
    # carry those flags, so the set cannot be built before they are parsed.
    # Its first use is the header staleness pre-pass further down.
    vendor_patterns = list(
        _build_sdk_excludes(project_root, build_system, units=units)
    )
    if vendor_paths:
        vendor_patterns.extend(_normalize_patterns(vendor_paths))
    log.info(
        "vendor patterns for %s: %s",
        variant or project_root.name,
        ", ".join(vendor_patterns) or "none",
    )

    # Determine config_hash from manifest.json.  The manifest captures the
    # full structural build identity (files, directories, compiler flags) —
    # more comprehensive than hashing compile_commands.json alone.
    #
    # When a manifest exists, compute the expected structural hash from the
    # current units and compare.  If they match, reuse the stored hash so
    # _update_manifest_after_index() can do an incremental header update.
    # If they differ (compile_commands.json changed), rebuild the preliminary
    # manifest.  When no manifest exists yet (first index), build one.
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

    from .manifest import build_preliminary, build_scope, compute_structural_hash
    from .manifest import load as load_manifest

    scope = build_scope(variant, image, build_env)

    expected_hash = compute_structural_hash(
        compile_commands,
        project_root,
        units,
        build_dir_patterns,
        project_id=project_id,
        scope=scope,
    )
    manifest = load_manifest(db_path.parent, expected_hash)
    if manifest is not None:
        config_hash = manifest.get("config_hash", expected_hash)
    else:
        config_hash = build_preliminary(
            compile_commands,
            db_path.parent,
            project_root,
            units,
            build_dir_patterns,
            project_id=project_id,
            scope=scope,
        )
        # Reload manifest from disk — build_preliminary may have written a
        # preliminary (empty source_hash) manifest.  The in-memory manifest
        # must reflect what _update_manifest_after_index will find on disk
        # (degraded or fresh), so the early-return guard can detect
        # preliminary entries and fall through to regeneration.
        manifest = load_manifest(db_path.parent, config_hash)

    # WHY heartbeat: the index subprocess can deadlock (libclang hang, disk
    # full, NFS stall).  Without a heartbeat, the watchdog has no way to
    # distinguish "slow but alive" from "deadlocked."  This daemon thread
    # writes a timestamp to a known file every 30s — if the file stops
    # updating, the watchdog kills and restarts the process.
    _hb_log = os.environ.get("FW_CONTEXT_HEARTBEAT_LOG")
    if _hb_log:
        _hb_stop = threading.Event()

        def _heartbeat() -> None:
            while not _hb_stop.wait(30.0):
                try:
                    with open(_hb_log, "a") as f:
                        f.write(f"{time.strftime('%H:%M:%S')} heartbeat\n")
                except (OSError, ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error):
                    pass  # non-fatal — continue

        _hb_thread = threading.Thread(target=_heartbeat, daemon=True)
        _hb_thread.start()

    log.info("", extra={"phase": f"Indexing {name} ({config_hash[:12]})"})
    log.info("project=%s project_id=%s config_hash=%s", name, project_id, config_hash[:12])

    conn = open_db(db_path)
    # Determine initial manifest verification for this config.
    # New config_hash → 'indexing' (hidden from MCP queries until complete).
    # Existing config → preserve the previous value (in-place update).
    old_row = conn.execute(
        "SELECT manifest_verification FROM build_configs WHERE config_hash=?",
        (config_hash,),
    ).fetchone()
    if old_row is None:
        initial_manifest_verification = "indexing"
    else:
        initial_manifest_verification = old_row["manifest_verification"]

    # Resolve analyze_vendor: CLI flag takes precedence, config falls back.
    # Only meaningful when analyze_symbols is True — otherwise analysis doesn't
    # run and the flag is irrelevant (storing True would mislead get_active_build).
    if analyze_symbols:
        _analyze_vendor = analyze_vendor or (llm_config is not None and llm_config.analyze_vendor)
    else:
        _analyze_vendor = False

    # Serialize with any concurrent writer (background reindex, daemon)
    with write_lock(db_path.parent, timeout=120.0):
        with transaction(conn):
            upsert_project(conn, project_id, name, str(project_root))
            upsert_build_config(
                conn,
                config_hash,
                project_id,
                str(compile_commands),
                description=git_description,
                manifest_verification=initial_manifest_verification,
                analyze_vendor=int(_analyze_vendor),
                variant=variant,
                image=image,
                board=board,
            )

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
    # The entries hold header paths; their hashes live in this shared map.
    # Both travel together into the staleness checks.
    manifest_header_table: dict[str, dict] = (
        dict(manifest.get("headers") or {}) if manifest is not None else {}
    )

    # ── Header staleness pre-pass ──
    # A header is not a translation unit, so editing one leaves the mtime and
    # source hash of every dependent TU untouched — the per-TU mtime fast-path
    # would report them all as unchanged and the header's symbols would stay
    # frozen at their first-index state.  Hash the project headers once here
    # and mark every TU that includes a changed one for re-parsing.
    #
    # ALL dependent TUs are marked, not a subset: symbols from a shared header
    # are claimed by whichever TU stores them first, and a TU that keeps its
    # old rows would block the fresh ones via the ON CONFLICT(config_hash,
    # usr) guard in insert_symbols_batch.
    #
    # header_hash_cache is shared with the per-TU staleness checks below, so
    # each project header is read and hashed exactly once per run.
    header_hash_cache: dict[str, str] = {}
    header_stale_tus: frozenset[str] = frozenset()
    if manifest is not None:
        from .manifest import collect_stale_headers

        stale_headers = collect_stale_headers(
            manifest, project_root, hash_cache=header_hash_cache
        )
        header_stale_tus = _tus_to_requeue(manifest, stale_headers, project_root)

    # Drop FTS5 content-sync triggers before bulk indexing — each symbol
    # INSERT/DELETE/UPDATE would otherwise pay per-row FTS index overhead
    # (~2× write I/O).  The FTS table is rebuilt from scratch in one pass
    # after all TUs are stored.
    # If a previous run crashed after drop_fts_triggers() but before
    # rebuild_fts(), the triggers are missing — repair now so FTS stays
    # consistent during this run.
    missing_triggers = False
    for trigger_name in ("symbols_ai", "files_ai", "macros_ai"):
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger_name,)
        )
        if cur.fetchone() is None:
            missing_triggers = True
    if missing_triggers and not defer_fts:
        log.info("FTS5 triggers missing (possibly from a crashed run) — rebuilding FTS first")
        rebuild_fts(conn)
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
    # TUs actually re-parsed in this run.  The manifest may only take fresh
    # header hashes from these — an entry refreshed for a TU that kept its
    # previous symbols would erase the evidence that the index is behind.
    reparsed_tus: set[str] = set()
    t0 = time.monotonic()

    # Is any PROJECT file still missing its ifdef-filtered content?  One query
    # per run instead of one per TU.  While a backfill is pending, unchanged
    # TUs keep running the content pass so it can complete; once every project
    # file has content they skip it, along with the libclang parse behind it.
    #
    # WHY project files only (relative path = inside project_root, see
    # _normalize_file_path): out-of-tree system headers routinely end up with
    # an empty content column — e.g. libstdc++ headers whose cursors were all
    # claimed by an earlier TU.  Counting those would pin the flag to True
    # forever on any C++ project and the pass would never be skipped.
    content_backfill_needed = (
        conn.execute(
            "SELECT 1 FROM files WHERE config_hash = ? AND content = '' "
            "AND path NOT LIKE '/%' LIMIT 1",
            (config_hash,),
        ).fetchone()
        is not None
    )

    # skip_files accumulates resolved paths of headers already processed
    # by earlier TUs in this run.  Starts empty each run — never loaded
    # from manifest, never persisted.  This avoids stale-header bugs
    # where a changed header would be skipped because it was in a
    # persist-stored skip set from a previous index run.
    skip_files: set[str] = set()

    log.info("", extra={"phase": f"Parsing ({len(units)} TUs)"})

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
        raise_if_superseded(db_path.parent)  # Give up if another process took over
        fname = unit.file.name
        processed = i + 1

        # ── Phase 1: staleness check + libclang parse (no lock) ──
        # WHY outside lock: libclang parsing can take seconds to minutes per
        # TU on large codebases (mbed-os, Zephyr).  Holding the write lock
        # during parsing starves other indexers (bg reindex, concurrent
        # ``fw-context index --force``) and causes WriteLockTimeout errors.
        check_status, parsed_data, parse_timing, hashes = _check_and_parse_unit(
            unit,
            config_hash,
            project_root,
            index_refs,
            existing_files,
            force=force,
            manifest=manifest_lookup,
            skip_files=skip_files,
            header_stale_tus=header_stale_tus,
            hash_cache=header_hash_cache,
            header_table=manifest_header_table,
        )

        # Snapshot the skip set BEFORE folding in this TU's own files.
        # Content fill must skip only headers already processed by EARLIER
        # TUs.  This TU's headers are filled now — their active lines come
        # from the AST walk, because get_tokens() returns tokens only for
        # the main file.  A post-update skip set would skip this TU's
        # headers entirely and leave files.content empty.
        skip_before = frozenset(skip_files) if skip_files else None

        if parsed_data is not None and hasattr(parsed_data, 'newly_seen_files'):
            skip_files.update(parsed_data.newly_seen_files)

        if check_status == "unchanged":
            result = _handle_unchanged(
                unit, check_status, hashes, conn, config_hash, project_root,
                build_dir_patterns, db_path, existing_files,
                skip_files=skip_before,
                manifest_lookup=manifest_lookup,
                content_backfill_needed=content_backfill_needed,
            )
            unchanged += 1
            content_filled += result["content_filled"]
            if result["headers"]:
                tu_headers.update(result["headers"])
            log.info("[%d/%d] %s: %s", processed, len(units), fname, result["status"])
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
                vendor_patterns,
                project_patterns_list,
                index_refs,
                db_path,
                existing_files,
                conn=conn,
                force=force,
                pre_parsed=parsed_data,
                parse_timing=parse_timing,
                hashes=hashes,
                build_dir_patterns=build_dir_patterns,
                skip_files=skip_before,
            )
            if status == "updated":
                updated += 1
                total_syms += syms
                total_refs += refs
                acc_parse += timing[0]
                acc_lock += timing[1]
                acc_write += timing[2]
                try:
                    tu_key = str(unit.file.resolve().relative_to(project_root))
                except ValueError:
                    tu_key = str(unit.file.resolve())
                # Only a re-parsed TU may refresh its manifest entry — its
                # symbols now match the headers it just read.
                reparsed_tus.add(tu_key)
                if tu_headers_list:
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



    # ── Post-processing ──
    _run_postprocess(
        conn=conn,
        config_hash=config_hash,
        project_root=project_root,
        db_dir=db_path.parent,
        units=units,
        tu_headers=tu_headers,
        manifest=manifest,
        compile_commands=compile_commands,
        updated=updated,
        build_dir_patterns=build_dir_patterns,
        vendor_patterns=vendor_patterns,
        project_patterns_list=project_patterns_list,
        project_id=project_id,
        git_description=git_description,
        index_refs=index_refs,
        index_embeddings=index_embeddings,
        index_macros_expanded=index_macros_expanded,
        analyze_symbols=analyze_symbols,
        analyze_overrides=analyze_overrides,
        analyze_vendor=_analyze_vendor,
        llm_config=llm_config,
        cache_server_config=cache_server_config,
        force=force,
        purge_max_missing_percent=purge_max_missing_percent,
        variant=variant,
        image=image,
        board=board,
        scope=scope,
        defer_fts=defer_fts,
        defer_cleanup=defer_cleanup,
        header_hash_cache=header_hash_cache,
        reparsed_tus=reparsed_tus,
    )

    elapsed = time.monotonic() - t0
    log.info("", extra={"phase": f"Done — {total_syms} symbols, {total_refs} refs, {_fmt_dur(elapsed)}"})
    log.info("%d updated, %d unchanged, %d skipped  config_hash=%s", updated, unchanged, skipped, config_hash[:12])
    return config_hash
