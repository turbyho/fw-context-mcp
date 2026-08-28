"""Stale detection and recovery — file mtime checks, auto-reindex, query staleness wrapping.

WHY stale detection cannot be a simple "compare index timestamp to latest file
mtime": compile_commands.json changes may require a full reindex even if no source
file changed (new defines, changed include paths, different compiler).  Conversely,
source files may change without modifying compile_commands.json.  Both triggers are
independent and must be checked separately.

WHY there are two staleness helpers (``_stale_files`` vs ``_count_modified_files``):
- ``_stale_files`` operates on a small set of result files — it checks "are THESE
  specific files stale?"  Used after every search/lookup to warn the assistant.
- ``_count_modified_files`` scans ALL files in the database — it answers "how many
  files are stale overall?"  Used by ``get_active_build`` for the dashboard count
  and by the daemon's startup check.

WHY mtime checks are cached: on NFS/CIFS mounts, each ``os.path.getmtime()`` may
take 50-200 ms.  With 10K indexed files, a full scan would take seconds.  The
``_modified_cache`` with 30-second TTL bounds this to one scan per evaluation
interval.  The ``MAX(mtime)`` verification before serving cached values catches
external modifications by the daemon's background reindex (which is a separate
OS process and cannot call ``_invalidate_modified_cache``).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from ...config import derive_project_id
from ...indexer.db import get_active_config
from ...utils import (
    MTIME_TOLERANCE_S,
    TU_EXTENSIONS,
    abs_path,
    build_dir_patterns_with_fw_context,
    compute_source_hash,
)
from .context import _quick_open_readonly, get_executor

log = logging.getLogger(__name__)

# How close two timestamps must be to count as the same one.  NOT a
# tolerance for clock skew — that is MTIME_TOLERANCE_S, and it is a whole
# second.  This is only wide enough to absorb the float round trip through
# SQLite REAL and a filesystem that does not return a stat bit-exactly.
#
# It must stay small.  The rule it serves reads "an exact match is the file
# the index hashed"; widen it and every file inside the band reads as
# unchanged again, which is the behaviour this replaced.
_MTIME_MATCH_EPS_S: float = 0.001

# ── Mtime cache ──
# Cache for _count_modified_files results with a 30-second TTL.
# Invalidation is done explicitly by _invalidate_modified_cache after
# write operations (reindex_file_impl, file watcher).  The background
# reindex is a separate OS process and cannot invalidate this cache;
# the TTL ensures staleness is bounded to at most 30 seconds.
#
# Cache entries are (timestamp, count, max_stored_mtime) — the last
# field is the MAX(mtime) from the files table at cache time.  Before
# serving a cached value we verify this hasn't changed, catching
# external DB modifications by bg-reindex / manual fw-context index.
_modified_cache: dict[str, tuple[float, int, float]] = {}
_CACHE_TTL_S: float = 30.0  # shared TTL for mtime cache and header staleness cache


def _invalidate_modified_cache(config_hash: str | None = None) -> None:
    """Invalidate the mtime cache.  If *config_hash* is None, clear all entries.

    NOTE on who calls this: reindex_file_impl and reset_index, both in
    mcp/handlers/maintenance.py.  The file watcher does NOT and cannot — it
    reindexes in a subprocess, as the module docstring says — so the TTL is
    the only bound on an edit the daemon picked up.
    """
    if config_hash:
        _modified_cache.pop(config_hash, None)
    else:
        _modified_cache.clear()


def _path_matches_patterns(path: str, patterns: list[str]) -> bool:
    """Return True if *path* contains any of the build-directory *patterns*."""
    if not patterns:
        return False
    return any(pat in path for pat in patterns)


# ── Structural staleness ──────────────────────────────────────────────────
# Shared by background._fast_staleness_check and daemon._staleness_check.


def check_structural_staleness(
    conn,
    config_hash: str,
    cfg: dict,
    root: Path,
) -> list[str]:
    """Check structural staleness — compile_commands.json, schema, refs.

    Returns a list of human-readable reasons the index needs a reindex.
    These are the checks that both the background reindex trigger and the
    daemon startup perform — file-level mtime checks are added separately
    by callers that need them.

    All imports are lazy to avoid circular dependencies at module level.
    """
    from ...config import load as load_config
    from ...indexer.db import CURRENT_SCHEMA_VERSION, get_db_schema_version
    from .context import _is_stale

    reasons: list[str] = []

    # 1. compile_commands.json changed? (one stat call)
    cc_path = cfg["compile_commands_path"]
    if _is_stale(cfg, cc_path)[0]:
        reasons.append("compile_commands.json changed")

    # 2. Schema version mismatch?
    schema_ver = get_db_schema_version(conn)
    if schema_ver < CURRENT_SCHEMA_VERSION:
        reasons.append(f"schema {schema_ver} < {CURRENT_SCHEMA_VERSION}")

    # 3. Missing refs (and indirect call sites when refs are missing)?
    proj_cfg = load_config(root)
    if proj_cfg.index.index_refs:
        ref_count = conn.execute(
            "SELECT COUNT(*) FROM refs WHERE config_hash=?",
            (config_hash,),
        ).fetchone()[0]
        if ref_count == 0:
            reasons.append("refs missing")
            ics_count = conn.execute(
                "SELECT COUNT(*) FROM indirect_call_sites WHERE config_hash=?",
                (config_hash,),
            ).fetchone()[0]
            if ics_count == 0:
                reasons.append("indirect call sites missing")

    return reasons


def _stale_files(conn, config_hash: str, file_paths: list[str], root: Path) -> list[str]:
    """Return the subset of *file_paths* whose on-disk mtime is newer than the index."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from ...indexer.manifest import load_build_dir_patterns
    from ...indexer.ops import _normalize_file_path

    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    # This runs on every query routed through _with_stale_recovery, and the
    # patterns are all it needs — see load_build_dir_patterns for why parsing
    # the whole manifest here was the most expensive thing on that path.
    build_patterns = load_build_dir_patterns(db_path.parent, config_hash)

    # Build work items: batch-fetch stored mtimes in one query (was N+1).
    normalized: list[tuple[str, str]] = []  # (abs_path, db_key)
    seen: set[str] = set()
    for path in dict.fromkeys(file_paths):
        if path in seen:
            continue
        seen.add(path)
        if _path_matches_patterns(path, build_patterns):
            continue
        db_key = _normalize_file_path(path, root)
        normalized.append((path, db_key))

    if not normalized:
        return []

    # Batch lookup: one SELECT for all keys
    keys = [db_key for _, db_key in normalized]
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT path, mtime, source_hash FROM files WHERE config_hash = ? AND path IN ({placeholders})",  # noqa: S608 — placeholders only
        (config_hash, *keys),
    ).fetchall()
    stored_map: dict[str, tuple[float, str]] = {
        r["path"]: (r["mtime"], r["source_hash"] or "") for r in rows
    }

    # Build work items with the stored mtime and hash
    work_items: list[tuple[str, float, str]] = []  # (abs_path, mtime, hash)
    for file_path, db_key in normalized:
        stored = stored_map.get(db_key)
        if stored is not None:
            work_items.append((file_path, stored[0], stored[1]))

    if not work_items:
        return []

    # This path checks the files that a result names, and a result names at
    # most `limit` of them — 50 by default, 200 at the ceiling.  Hashing all
    # of them costs 0.67 ms at 50 files, thus the timestamp is not consulted
    # at all: it is wrong after a git checkout, and it misses a write that
    # kept the old stamp.  The project-wide counter cannot afford the same
    # and filters by mtime first.
    stale: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, len(work_items))) as ex:
        futures = {
            ex.submit(_file_differs, path, mtime, digest): path
            for path, mtime, digest in work_items
        }
        for f in as_completed(futures):
            try:
                if f.result():
                    stale.append(futures[f])
            except OSError:
                pass
    return stale


def _file_differs(path: str, stored_mtime: float, stored_hash: str) -> bool:
    """Answer by content when a hash exists, by timestamp when it does not.

    THE ONE TO USE for a caller that holds a handful of files: a search
    result, a symbol body, a file being read.  It asks the content first,
    with no timestamp gate in front, which removes both failure modes of the
    timestamp at once — the stamp git rewrote without changing a byte, and
    the write that kept the old stamp.

    ``_check_file_stale`` is the other half and is NOT interchangeable: it
    gates on the timestamp and reads the content only for a file the stamp
    calls suspect.  That trade belongs to the caller that walks every
    indexed file and cannot afford a hash per row.  Reaching for it on a
    single file is what left read_file warning about a file git had only
    touched.
    """
    differs = _content_differs(path, stored_hash)
    if differs is not None:
        return differs
    return _check_file_stale(path, stored_mtime)


def _in_racy_window(file_mtime: float, stored_mtime: float) -> bool:
    """Tell whether the timestamp of a file is too close to trust.

    A write that lands within ``MTIME_TOLERANCE_S`` of the index run keeps a
    stamp that the plain comparison accepts, thus the change disappears.  The
    same happens with ``touch -r``, with rsync ``--times``, and with any
    editor that restores the original time.

    Git calls such an entry "racily clean" and rehashes it instead of
    trusting the stat cache.  This function marks the same set: a file whose
    stamp sits inside the tolerance band around the stored one, in either
    direction.

    ONE caller, ``_check_file_stale``.  It used to gate the project-wide
    scan as well, and there it was worse than useless: the band is symmetric
    around the STORED stamp, and an unchanged file carries exactly that
    stamp, so every unchanged file fell inside it.  See
    ``_count_modified_files`` for the rule that replaced it.
    """
    return abs(file_mtime - stored_mtime) <= MTIME_TOLERANCE_S


def _content_differs(path: str, stored_hash: str) -> bool | None:
    """Compare the file at *path* against *stored_hash*.

    Returns True when the bytes differ, False when they match, and None when
    the question cannot be answered — no stored hash, or the file cannot be
    read.  The caller then falls back to the timestamp.

    This is the only signal that survives git.  ``checkout``, ``pull`` and
    ``stash pop`` rewrite the mtime of every file they touch, without regard
    to content, thus a checkout back to the original branch restores the same
    bytes with a fresh time.
    """
    if not stored_hash:
        return None
    current = compute_source_hash(Path(path))
    if not current:
        return None  # unreadable — compute_source_hash swallows the OSError
    return current != stored_hash


def _check_file_stale(path: str, stored_mtime: float, stored_hash: str = "") -> bool:
    """Return True when *path* no longer matches what the index holds.

    The timestamp alone answers wrongly in both directions:

    * Too often — git rewrites the mtime of a file it did not change.
    * Not often enough — a write within ``MTIME_TOLERANCE_S`` of the index
      run keeps the old stamp, and so do ``touch -r`` and rsync ``--times``.

    With *stored_hash* the timestamp only raises the question and the content
    answers it, which is what git does: its stat cache filters, and the
    object hash decides.  Without a stored hash the behaviour is the old one,
    thus an index written before this change still works.

    FOR THE BATCH PATH ONLY.  The gate exists to keep a scan of every
    indexed file affordable; a caller holding one file should use
    ``_file_differs``, which skips the gate and asks the content directly.

    A missing file is stale — it was deleted since indexing.
    """
    try:
        file_mtime = os.path.getmtime(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False

    newer = file_mtime > stored_mtime + MTIME_TOLERANCE_S
    # A stamp inside the tolerance band is not evidence of anything: the
    # write may have landed in the same second as the index run.  Git treats
    # such an entry as "racily clean" and rehashes it, and so does this.
    if not newer and not _in_racy_window(file_mtime, stored_mtime):
        return False

    differs = _content_differs(path, stored_hash)
    if differs is not None:
        return differs
    # No stored hash: the timestamp is all there is, and inside the band it
    # says nothing, thus the file counts as unchanged — the old behaviour.
    return newer


def _count_modified_files(
    conn,
    config_hash: str,
    root: Path,
    *,
    use_cache: bool = False,
) -> int:
    """Count files whose on-disk mtime is newer than the stored mtime.

    Files with mtime=0 (pre-migration databases) are skipped — their
    mtime is unknown, not "modified".

    Deduplicates by resolved absolute path, keeping the maximum stored
    mtime.  This prevents false positives from duplicate ``files`` rows
    (e.g. one with an absolute path whose mtime was updated and one with
    a relative path whose mtime is stale).

    When *use_cache* is True and a cached value is less than
    ``_CACHE_TTL_S`` seconds old AND the DB hasn't been modified
    externally (verified via ``MAX(mtime)`` from the files table),
    the cached value is returned.
    Call ``_invalidate_modified_cache(config_hash)`` after any write
    operation that changes file mtimes.
    """
    if use_cache:
        cached = _modified_cache.get(config_hash)
        if cached is not None:
            ts, count, max_stored = cached
            if time.monotonic() - ts < _CACHE_TTL_S:
                # Verify DB hasn't been modified externally (bg reindex,
                # manual fw-context index from another process, etc.)
                try:
                    row = conn.execute(
                        "SELECT MAX(mtime) FROM files WHERE config_hash=? AND mtime > 0",
                        (config_hash,),
                    ).fetchone()
                    current_max = row[0] if row else 0.0
                    if current_max == max_stored:
                        return count
                except sqlite3.Error:
                    pass  # query failed — fall through to recompute
            # Cache stale — remove and recompute
            _modified_cache.pop(config_hash, None)

    # Deduplicate by resolved absolute path, keeping the newest stored mtime.
    # Duplicate rows arise when the same file was indexed under different
    # path formats (absolute vs relative) — e.g. after a reindex with a
    # different working directory or compile_commands.json format.
    best_mtime: dict[str, tuple[float, str]] = {}
    rows = conn.execute(
        "SELECT path, mtime, source_hash FROM files WHERE config_hash=?", (config_hash,)
    ).fetchall()
    for r in rows:
        path = r["path"]
        stored = r["mtime"]
        if not stored:
            continue
        p = Path(path)
        if not p.is_absolute():
            p = (root / path).resolve()
        key = str(p)
        if key not in best_mtime or stored > best_mtime[key][0]:
            best_mtime[key] = (stored, r["source_hash"] or "")

    # Load build_dir_patterns from manifest to skip build-generated files
    from ...indexer.manifest import load_build_dir_patterns

    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    build_patterns = load_build_dir_patterns(db_path.parent, config_hash)

    # NOTE: TOCTOU — file may change between DB read and stat() below.
    # Not a security issue: worst case is missed detection until next query.
    modified = 0
    for key, (stored_mtime, stored_hash) in best_mtime.items():
        if build_patterns and _path_matches_patterns(key, build_patterns):
            continue  # build-generated file — skip
        try:
            # NOTE: individual stat() calls — O(n) for n files. On modern NVMe
            # filesystems this is <10ms for 10K files. If slow, consider
            # os.scandir() batch processing or caching more aggressively.
            #
            # The index stores the mtime it read together with the hash it
            # computed, thus an exact match means "this is the file we
            # hashed" and nothing needs reading.  Anything else is suspect
            # in BOTH directions: newer is the ordinary edit, older means
            # the file was replaced by an older copy — a restore from a
            # backup, or a tar that kept its times.
            #
            # The previous rule was "newer, or inside the racy window", and
            # that window is symmetric around the stored stamp.  An
            # unchanged file carries exactly that stamp, so it fell inside
            # and reached the hash: measured on zbox-ecb-fw, 1910 of 1911
            # rows did.  The gate filtered nothing while get_active_build —
            # the mandatory first call — hashed the whole project every
            # time.  The one set it did exclude was the backwards stamp,
            # which is the one case that needed reading.
            #
            # What this cannot see: a write that restores the stamp to the
            # exact float the index holds.  _stale_files has no gate at
            # all, so a query about that file still reports it.
            file_mtime = os.path.getmtime(key)
            if abs(file_mtime - stored_mtime) <= _MTIME_MATCH_EPS_S:
                continue
            differs = _content_differs(key, stored_hash)
            if differs is None:
                # No stored hash: the timestamp is all there is, and only
                # one direction is readable from it.
                if file_mtime > stored_mtime + MTIME_TOLERANCE_S:
                    modified += 1
            elif differs:
                modified += 1
        except FileNotFoundError:
            modified += 1
        except OSError:
            pass

    if use_cache:
        max_stored = max((mtime for mtime, _ in best_mtime.values()), default=0.0)
        _modified_cache[config_hash] = (time.monotonic(), modified, max_stored)
    return modified


# ── New source files ──
# A file that the build system never saw has no translation unit, thus a
# reindex cannot pick it up: it is absent from compile_commands.json, and
# only a build puts it there.  Detection therefore needs the file tree, not
# the mtimes of the rows the index already holds.

# Only translation-unit candidates — the set lives in utils.TU_EXTENSIONS,
# shared with compile_commands.py and builders/manual.py.  A new header is
# deliberately out of scope: one that nothing includes stays out of the
# index even after a build, thus reporting it would give a warning with no
# cure, and one that some unit includes reaches the index on its own.

# Cap on the number of reported paths.  The count is what matters to the
# caller; a full list of a hundred paths would only crowd the message.
_UNINDEXED_REPORT_LIMIT = 20


def _project_scan_roots(scan_roots: set[str], build_patterns: list[str]) -> set[str]:
    """Drop the build-output directories from the scan roots.

    ``_indexed_paths`` collects the roots from the index, because the layouts
    differ: zbox-ecb-fw keeps code in ``lib``, ``src`` and ``targets_custom``,
    birdie1-v2-fw-v3 adds ``mbed_target_stm`` and ``components``.  A fixed
    list of ``src``/``lib``/``include`` would miss those.

    A build directory can hold a project file — a generated config header —
    and thus reach the set.  Walking it would cost a lot and find only build
    output, so it goes out here.

    ``"."`` means that the project holds source files in its root directory,
    and it never matches a build pattern.

    The trailing separator is necessary.  ``load_build_dir_patterns`` gives
    directory patterns with one — ``"BUILD/"``, ``".pio/build/"`` — and
    ``_path_matches_patterns`` does a substring test, thus a bare ``"BUILD"``
    matched nothing and the walk descended into the build output.
    """
    return {
        root
        for root in scan_roots
        if root == "." or not _path_matches_patterns(f"{root}/", build_patterns)
    }


# How long git may take to list the files of the project.  It is a local
# read of the index file; a project big enough to need longer than this has
# something else wrong, and the walk below is the answer either way.
_GIT_LS_TIMEOUT_S: float = 30.0


def _git_tu_candidates(root: Path) -> list[str] | None:
    """Return the source files of the project, or None when git cannot say.

    ``git ls-files -co --exclude-standard`` gives the tracked files plus the
    untracked ones that .gitignore does not cover — which is the code of the
    user and nothing else.  A gitignored vendor tree drops out on its own:
    measured, that is ``ncs/`` on zbox-ecb-fw-v5, where the directory walk
    found 109,687 candidates in 4.0 s and this finds 12 in 1 ms, and
    ``.pio/`` on HA_Boiler.

    It also answers what the walk could not.  ``scan_roots`` comes from
    files the index already holds, so a whole new top-level directory named
    no root, and the walk from "." does not descend — a new module arriving
    as ``tests/`` or ``components/`` was reported by nothing at all.  An
    untracked file in a new directory is in this listing.

    Returns None when git cannot answer: no repository, no git binary, a
    non-zero exit, a timeout.  The caller then walks, which is what a
    project without a repository needs.

    A vendored SDK that IS committed stays in the listing — mbed-os/ on
    zbox-ecb-fw is 6,215 source files in git — so the caller still applies
    the vendor roots it takes from the index.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=_GIT_LS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [
        rel for rel in result.stdout.splitlines()
        if Path(rel).suffix.lower() in TU_EXTENSIONS
    ]


def _vendor_roots(conn, config_hash: str) -> set[str]:
    """Return the top-level directories that hold vendor code only.

    Taken from ``files.is_project``: a root that carries non-project rows
    and no project row is the SDK.  The manifest's own vendor patterns are
    not enough — measured, zbox-ecb-fw-v5 has none at all — and this
    question has an answer in the index for every project that indexed the
    vendor tree.

    Only relative paths count.  An absolute one names a file outside the
    project, which has no top-level directory inside it.
    """
    project: set[str] = set()
    vendor: set[str] = set()
    for row in conn.execute(
        "SELECT path, is_project FROM files WHERE config_hash=?", (config_hash,)
    ):
        stored = row["path"]
        if os.path.isabs(stored):
            continue
        parts = Path(stored).parts
        first = parts[0] if len(parts) > 1 else "."
        (project if row["is_project"] else vendor).add(first)
    return vendor - project


def find_unindexed_sources(
    conn,
    config_hash: str,
    root: Path,
    cc_path: Path,
    *,
    limit: int | None = _UNINDEXED_REPORT_LIMIT,
    apply_exclusions: bool = True,
) -> list[str]:
    """Return source files that ``compile_commands.json`` does not cover.

    A file qualifies when BOTH conditions hold:

    1. The index holds no row for it.
    2. Its mtime is newer than ``compile_commands.json``.

    Both are necessary.  Condition 1 alone is useless: a project keeps many
    source files out of the build on purpose — library headers that nothing
    includes, tests outside the build, ``secrets.example.h``.  On
    birdie1-v2-fw-v3 that is 726 files, and every one of them is correct.

    Condition 2 must compare against ``compile_commands.json``, never against
    the index timestamp.  A reindex moves the index timestamp even when it
    ignores the new file, thus the file would look old on the next run and
    stay invisible forever.  Only a build writes compile_commands.json.

    The same property stops a loop: after a successful build the file is
    older than the regenerated compile_commands.json, thus a file that the
    build system never accepts is reported once, not forever.

    Args:
        conn: Open connection to the index database.
        config_hash: Active build configuration.
        root: Project root.
        cc_path: Path of the compile_commands.json of this build.
        limit: Maximum number of paths to return, or None for all of them.
            The cap serves the caller that REPORTS the count — a list of a
            hundred paths only crowds the message.  The caller that RECORDS
            the set must pass None: record_excluded replaces the marker
            wholesale, thus a truncated list silently drops every entry past
            the cap and those files are reported again on every edit.
        apply_exclusions: When True (default), leave out the files that a
            build already ran for and still did not cover — see
            ``indexer.autobuild.load_excluded``.  The caller that writes
            that marker passes False, or it would filter out the very files
            it is about to record.

    Returns:
        list[str]: Paths relative to *root*, sorted, at most *limit* long.
        Empty when compile_commands.json is absent — without a reference
        point every file would look new, and a warning on every query is
        worse than none.
    """
    from ...indexer.autobuild import load_excluded
    from ...indexer.manifest import load_build_dir_patterns
    from ...indexer.ops import _normalize_file_path

    try:
        cc_mtime = os.path.getmtime(cc_path)
    except OSError:
        return []

    known, scan_roots = _indexed_paths(conn, config_hash)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    # The directory fw-context writes into is not a place to look for the
    # source files of the user: it holds the generated compile_commands.json
    # and the output of the automatic build.  The shared helper adds it,
    # because the same gap bites _is_generated_header — see its docstring
    # for the measurement.
    build_patterns = build_dir_patterns_with_fw_context(
        load_build_dir_patterns(db_path.parent, config_hash)
    )
    # Read once, before the walk.  The caller that WRITES the marker passes
    # apply_exclusions=False, or it would filter out the very files it is
    # about to record.
    excluded = load_excluded(db_path.parent) if apply_exclusions else {}

    found: list[str] = []
    for candidate in _tu_candidates(conn, config_hash, root, scan_roots, build_patterns):
        # _normalize_file_path decides the one spelling that every
        # path-keyed lookup uses, thus the comparison must go through it.
        # It runs on the candidates only — resolving every indexed path
        # instead cost one syscall per row and made the scan 7x slower.
        key = _normalize_file_path(candidate, root)
        if key in known:
            continue
        try:
            if os.path.getmtime(candidate) <= cc_mtime:
                continue
        except OSError:
            continue  # vanished between the listing and the stat
        # A build already ran for this file and the build system still
        # did not take it.  Reporting it again only tells the caller to
        # run a command that changes nothing, and it would hold
        # get_active_build on "reindex_needed" for ever.  The hash is
        # what expires the record: an edit means the user may have just
        # added the file to the build, thus it earns one more report.
        recorded = excluded.get(key)
        if recorded and recorded == compute_source_hash(Path(candidate)):
            continue
        found.append(key)

    return sorted(found) if limit is None else sorted(found)[:limit]


def _tu_candidates(
    conn, config_hash: str, root: Path, scan_roots: set[str], build_patterns: list[str]
):
    """Yield the absolute path of every source file that could need a build.

    Git first, the directory walk as the fallback.  See _git_tu_candidates
    for why git is the better question and _walk_tu_candidates for what the
    walk cannot reach.
    """
    listed = _git_tu_candidates(root)
    if listed is not None:
        vendor = _vendor_roots(conn, config_hash)
        for rel in listed:
            top = rel.split("/")[0] if "/" in rel else "."
            if top in vendor:
                continue  # a vendored SDK that is committed to the repository
            if _path_matches_patterns(rel, build_patterns):
                continue
            yield str((root / rel).resolve())
        return

    # No repository, or git could not answer.  The walk sees only the roots
    # the index already knows, thus a new top-level directory stays
    # invisible here — that is the cost of having no git to ask.
    for scan_root in sorted(_project_scan_roots(scan_roots, build_patterns)):
        start = root if scan_root == "." else root / scan_root
        yield from _walk_tu_candidates(start, scan_root == ".", build_patterns)


def _indexed_paths(conn, config_hash: str) -> tuple[set[str], set[str]]:
    """Return what the index knows, as ``(paths, scan_roots)``.

    *paths* holds the ``files.path`` column verbatim.  The indexer writes it
    through ``_normalize_file_path``, thus a caller that normalises its own
    candidate the same way can compare the two directly, with no syscall per
    row.

    *scan_roots* holds the first path component of every project file, which
    seeds the directory walk.  A vendor file adds nothing: a new file lands
    beside the code of the team, not inside the SDK.
    """
    paths: set[str] = set()
    scan_roots: set[str] = set()
    for row in conn.execute(
        "SELECT path, is_project FROM files WHERE config_hash=?", (config_hash,)
    ):
        stored = row["path"]
        paths.add(stored)
        if row["is_project"] and not os.path.isabs(stored):
            parts = Path(stored).parts
            scan_roots.add(parts[0] if len(parts) > 1 else ".")
    return paths, scan_roots


def _walk_tu_candidates(start: Path, root_only: bool, build_patterns: list[str]):
    """Yield the absolute path of every translation-unit candidate under *start*.

    With *root_only* the walk covers the directory itself and no child: the
    named scan roots already cover the subtrees, and a recursive walk from
    the project root would cross the vendor tree.
    """
    if not start.is_dir():
        return

    if root_only:
        for entry in start.iterdir():
            if entry.is_file() and entry.suffix.lower() in TU_EXTENSIONS:
                yield str(entry.resolve())
        return

    for dirpath, dirnames, filenames in os.walk(start):
        if build_patterns and _path_matches_patterns(dirpath, build_patterns):
            dirnames[:] = []  # build output — do not descend
            continue
        for filename in filenames:
            if Path(filename).suffix.lower() in TU_EXTENSIONS:
                yield str(Path(dirpath, filename).resolve())




# ── Header dependency staleness ──
# Cache for _check_header_staleness results.  Keyed as "headers:{config_hash}".
# Uses the same TTL as _modified_cache (30 seconds).
_header_staleness_cache: dict[str, tuple[float, int]] = {}


def _check_header_staleness(
    conn,
    config_hash: str,
    root: Path,
    *,
    max_files: int = 200,
    use_cache: bool = True,
) -> tuple[int, list[str]]:
    """Count TUs whose header dependencies have changed since indexing.

    Loads ``manifest.json`` from the index directory, compares stored
    header hashes against current on-disk content for project headers.

    Returns ``(count, affected_files)`` where *count* is the number of
    TUs with stale headers and *affected_files* is the list of source
    file paths.

    Performance is bounded by *max_files* (default 200).  When
    *use_cache* is True (default), results are cached for 30 seconds.
    Callers that need fresh results (e.g. ``get_active_build``) should
    pass ``use_cache=False``.
    """
    if use_cache:
        cache_key = f"headers:{config_hash}"
        cached = _header_staleness_cache.get(cache_key)
        if cached is not None:
            ts, count = cached
            if time.monotonic() - ts < _CACHE_TTL_S:
                return count, []

    from ...indexer.manifest import check_tu_staleness, resolve_headers
    from ...indexer.manifest import load as load_manifest

    # Find the DB dir from the conn's path
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    # Load the manifest OF THIS BUILD.  Without the config_hash, load() falls
    # back to the most recently written manifest.*.json — on a project with
    # build variants that is whichever variant was indexed last, so the count
    # returned for variant A could describe variant B's translation units and
    # header hashes, and get cached under A's key.  A missing manifest for
    # this build means there is no header-hash data for it, which is exactly
    # "no header staleness known" rather than someone else's answer.
    manifest = load_manifest(db_path.parent, config_hash)
    if not manifest:
        return 0, []

    project_root = Path(manifest.get("project_root", str(root)))
    # No vendor set is needed here any more: header_is_trusted() is the one
    # rule, and it reads `generated` alone.  This layer used to derive its
    # own set, which differed from the indexer's — it has no compiler flags —
    # and the narrower answer made this check re-hash headers the indexer had
    # trusted.  maintenance.py turned that into "stale": true and asked for a
    # reindex that could not clear it.

    stale_count = 0
    affected: list[str] = []

    # Resolved per entry rather than for the whole manifest: only the first
    # *max_files* entries are examined, and on a large project that is a small
    # fraction of them.
    header_table = manifest.get("headers")
    for entry in manifest.get("entries", [])[:max_files]:
        stale, _ = check_tu_staleness(
            entry, project_root,
            headers=resolve_headers(entry, header_table),
        )
        if stale:
            stale_count += 1
            affected.append(entry["file"])

    if use_cache:
        _header_staleness_cache[cache_key] = (time.monotonic(), stale_count)
    return stale_count, affected


def _with_stale_recovery(
    root: Path,
    db_path: Path,
    query_fn,
    *,
    stale_msg: str = "",
    config_hash: str | None = None,
) -> list[dict]:
    """Execute *query_fn(conn, config_hash)* on the executor with stale-recovery.

    When the index or result files are stale, kicks off a background
    ``fw-context index`` and returns a warning.  The original (possibly
    stale) results are always returned — the background reindex handles
    the actual fix asynchronously.

    The ENTIRE body (config read + ``query_fn`` + ``_stale_files``) runs
    inside ``executor.execute_sync``: the connection is needed for the
    whole search query, not just the staleness check.  A shorter variant
    that closed or released the connection after the config read would
    break every search tool — do NOT "optimise" this by splitting the
    connection usage.

    ``config_hash`` is read fresh per request via a short-lived
    read-only connection (no write transaction), then passed per call
    to the executor — a reindex with a changed build config can never
    leave queries filtering by a stale hash.
    """
    from ..background import _ensure_daemon_running  # lazy — avoids circular import

    # ── Read config_hash via a separate short-lived read-only connection ──
    # WHY: config_hash must be read BEFORE entering the executor lock.
    # The executor's single shared connection is a scarce resource —
    # using it for a 2-line config lookup would block all concurrent
    # search queries.  _quick_open_readonly opens a separate WAL reader
    # that coexists with the executor's connection.
    # Fresh config_hash per request: a reindex with a changed build
    # config can never leave queries filtering by a stale hash.
    if config_hash is None:
        conn = _quick_open_readonly(db_path)
        try:
            project_id = derive_project_id(root)
            cfg = get_active_config(conn, project_id)
            if not cfg:
                return [{"error": "No build config indexed."}]
            config_hash = cfg["config_hash"]
        finally:
            conn.close()

    executor = get_executor(db_path)

    def _query(db_conn, cfg_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        result_rows = query_fn(db_conn, cfg_hash)
        # Ensure plain dicts — rows must be materialised before returning.
        safe_rows: list[dict] = [dict(r) for r in result_rows]
        paths = collect_result_paths(safe_rows, root)
        if paths:
            return safe_rows, _stale_files(db_conn, cfg_hash, paths, root), 0, []
        # An empty search result is the case the caller most easily reads as
        # "does not exist".
        dirty, new_sources = diagnose_empty_result(db_conn, cfg_hash, root)
        return safe_rows, [], dirty, new_sources

    safe_rows, stale_f, dirty, new_sources = executor.execute_sync(_query, config_hash)

    if stale_f or dirty:
        # Kept here, not in annotate_stale: this path knows that the project
        # has a database, thus _db_path cannot raise.  A new source file does
        # not start the daemon: only a build repairs that, and the daemon
        # does not build.
        _ensure_daemon_running(root)
    return annotate_stale(
        safe_rows, stale_f, empty_dirty_count=dirty, empty_new_sources=new_sources
    )


# ── shared stale annotation ──
# _with_stale_recovery above serves the search handlers, which always give a
# list of records.  The call-graph, inheritance, and source handlers give
# either a list or a single dict, thus they need a shape-tolerant form.  Both
# use the same detection: compare the mtime of every file that the result
# names against the mtime that the index holds.

STALE_RESULT_MESSAGE = (
    "Results may be stale — {count} file(s) changed. Background reindex in "
    "progress. Run 'fw-context index' to force full update."
)

# An empty result names no file, thus the per-record check has nothing to
# compare.  Without this second message the caller reads "nothing found" as
# "does not exist", and the empty-result playbook makes that worse: the model
# tries four or five tools, gets nothing every time, and the repetition reads
# as proof.
EMPTY_RESULT_STALE_MESSAGE = (
    "No result, and {count} indexed file(s) changed after the last index run. "
    "What you look for can be in one of them, thus an empty result is not "
    "proof that it does not exist. Run 'fw-context index'."
)

# A file that compile_commands.json does not cover is a stronger case than a
# changed file, and it needs a different command: only a build writes that
# file, thus a plain reindex leaves the caller in a circle.
EMPTY_RESULT_NEW_SOURCE_MESSAGE = (
    "No result, and {count} source file(s) are not in compile_commands.json "
    "(first: {first}). A plain reindex cannot see them, because they have no "
    "translation unit. Run `fw-context index --build`."
)

# The handlers do not agree on one name for the path of a record, thus a
# check for a single key silently skips whole tools.  The names below were
# collected from the actual output of every call-graph, inheritance, and
# source tool against an indexed project.
#
# Aliases for the one file that a record is about — the first one present
# wins, because a record that holds two of them names the same file twice.
_PRIMARY_PATH_KEYS = ("file", "file_path", "source_file")
# Keys of records that name more than one file.  ``find_indirect_targets``
# gives the assignment site and the call site, and they are different files,
# thus both must be checked.
_EXTRA_PATH_KEYS = ("assign_file", "call_file")


def collect_result_paths(result, root: Path, *, _nested: bool = False) -> list[str]:
    """Give the absolute path of every file that *result* names.

    Accepts both result shapes that the handlers use: a list of records, and
    a single record.  A record with no path key adds nothing, thus an error
    dict or a summary dict passes through without a stat() call.

    Which tool uses which key:

    * ``file`` — ``find_callers``, ``find_references``, the search tools,
      ``get_file_map``, ``read_file``, and the ``methods`` of
      ``find_wrapper_callers``
    * ``file_path`` — ``find_dead_code``, ``find_hotspots``,
      ``find_all_callers_recursive``, ``find_callees_recursive``
    * ``source_file`` — ``trace_data_flow``
    * ``assign_file`` + ``call_file`` — ``find_indirect_targets``

    Args:
        result: A record, or a list of records.
        root: Project root, for the relative paths that the index stores.
        _nested: Internal.  True while the function reads a nested list, and
            it stops a second descent.

    Returns:
        list[str]: The absolute paths, with duplicates.  ``_stale_files``
        removes them before it calls stat().
    """
    records = result if isinstance(result, list) else [result]
    paths: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in _PRIMARY_PATH_KEYS:
            value = record.get(key)
            if value:
                paths.append(abs_path(root, value))
                break
        for key in _EXTRA_PATH_KEYS:
            value = record.get(key)
            if value:
                paths.append(abs_path(root, value))
        if _nested:
            continue
        # One level down, no more.  ``find_wrapper_callers`` holds its paths
        # in ``methods``, thus a check of the top level alone covers none of
        # its files.  The depth stops at one because no handler nests deeper,
        # and an open recursion would walk large results for nothing.
        for value in record.values():
            if isinstance(value, list) and any(isinstance(v, dict) for v in value):
                paths.extend(collect_result_paths(value, root, _nested=True))
    return paths


def annotate_stale(
    result,
    stale_files: list[str],
    *,
    empty_dirty_count: int = 0,
    empty_new_sources: list[str] | None = None,
):
    """Add a stale warning to *result*.

    Three conditions produce a warning, in this order of precedence:

    * *stale_files* — the result names files, and some of them changed.
    * *empty_new_sources* — the result names no file, and these source files
      are absent from compile_commands.json.  Named first among the empty
      cases, because it is the only one that needs ``--build``.
    * *empty_dirty_count* — the result names no file, and this many indexed
      files changed.

    The last two exist because an empty result gives per-record detection
    nothing to compare, and the caller reads silence as absence.

    A list gets the warning as its first record, which matches the shape that
    ``_with_stale_recovery`` gives.  A dict gets ``stale_warning`` and
    ``stale`` keys instead — a leading record would break the dict contract
    of the source handlers.

    Returns *result* unchanged when no condition holds.

    WHY no daemon start here: ``server.main`` starts the watcher daemon and
    its ping loop keeps it alive, thus a query does not have to.  A side
    effect in this function would also break every caller whose project has
    no database — ``_db_path`` raises there, and a warning must never fail
    the query it describes.
    """
    if stale_files:
        message = STALE_RESULT_MESSAGE.format(count=len(stale_files))
    elif empty_new_sources:
        message = EMPTY_RESULT_NEW_SOURCE_MESSAGE.format(
            count=len(empty_new_sources), first=empty_new_sources[0]
        )
    elif empty_dirty_count:
        message = EMPTY_RESULT_STALE_MESSAGE.format(count=empty_dirty_count)
    else:
        return result

    if isinstance(result, list):
        return [{"warning": message}, *result]
    if isinstance(result, dict):
        annotated = dict(result)
        annotated["stale_warning"] = message
        annotated["stale"] = True
        return annotated
    return result


def diagnose_empty_result(conn, config_hash: str, root: Path) -> tuple[int, list[str]]:
    """Explain an empty result: ``(changed_file_count, unindexed_sources)``.

    Runs only when the result names no file, thus its cost falls on the
    queries that returned nothing anyway.

    ``use_cache=False`` on purpose: the cache of ``_count_modified_files``
    validates itself against ``MAX(mtime)`` of the files table, which only a
    reindex changes.  An edit on disk leaves that cache valid, thus a cached
    answer would miss the very edit this check exists for.

    NOT cached either, for the same reason.  A cache on this answer was
    tried and dropped: after the mtime gate was fixed the scan costs 23 ms
    on the largest test project, and a cached diagnosis outlives the edit
    it describes for as long as its TTL.
    """
    dirty = _count_modified_files(conn, config_hash, root, use_cache=False)
    row = conn.execute(
        "SELECT compile_commands_path FROM build_configs WHERE config_hash=?",
        (config_hash,),
    ).fetchone()
    if row is None or not row["compile_commands_path"]:
        return dirty, []
    return dirty, find_unindexed_sources(
        conn, config_hash, root, Path(row["compile_commands_path"])
    )


def with_stale_annotation(
    root: Path, executor, query_fn, config_hash: str, *, diagnose_empty: bool = True
):
    """Run *query_fn* on *executor* and annotate a stale result.

    The staleness check must share the connection with the query, thus it
    runs inside the same ``execute_sync`` call.  A separate call would take
    the executor lock twice and could see a different index state.

    A result that names files is checked file by file.  A result that names
    none goes to ``diagnose_empty_result``: an empty answer is the case where
    the caller most needs to know that the index is behind, and it is the one
    case where per-record detection has nothing to work with.

    *diagnose_empty* is for the multi-scope caller.  The diagnosis describes
    the project, not one build of it, and every scope would reach the same
    conclusion — ``execute_scoped`` then throws the duplicates away.  Nine
    configs on zbox-ecb-fw-v5 meant nine scans of the whole index for one
    message.
    """

    def _query(conn, cfg_hash):
        result = query_fn(conn, cfg_hash)
        paths = collect_result_paths(result, root)
        if paths:
            return result, _stale_files(conn, cfg_hash, paths, root), 0, []
        if not diagnose_empty:
            return result, [], 0, []
        dirty, new_sources = diagnose_empty_result(conn, cfg_hash, root)
        return result, [], dirty, new_sources

    result, stale, dirty, new_sources = executor.execute_sync(_query, config_hash)
    return annotate_stale(
        result, stale, empty_dirty_count=dirty, empty_new_sources=new_sources
    )
