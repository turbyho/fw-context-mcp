"""Manifest update helpers extracted from runner.py.

Updates ``manifest.json`` after indexing: header mtime refresh and
incremental/full manifest rebuild.

WHAT is manifest.json: a JSON file that stores per-TU (Translation Unit)
metadata — the source file, compiler arguments, a content hash of the
source, and a list of included headers with their content hashes.  This
enables fast staleness detection without re-parsing all headers.

WHY incremental manifest update: after a full index of 500+ TUs,
regenerating the manifest from scratch (re-tokenizing every header) takes
minutes.  The incremental path reuses pre-collected header hashes from the
main indexing loop, only re-tokenizing new or changed TUs.

WHY header mtime refresh: ``git checkout`` / ``git merge`` changes header
file mtimes without changing content.  Without the refresh pass, the
stored mtimes would fall behind disk state, causing phantom modifications
that trigger unnecessary background reindexes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..utils import compute_source_hash
from .config_hash import compute_flags_hash

log = logging.getLogger(__name__)


def _mtime_bump_is_safe(
    resolved: Path,
    header: dict,
    project_root: Path,
    vendor_patterns: list[str],
    hash_cache: dict[str, str] | None,
) -> bool:
    # *header* is a manifest ``headers`` record: {hash, generated}.  The path
    # is passed separately as *resolved* because the record is keyed by it.
    """Return True when the stored mtime may be moved forward for this header.

    Only content-identical headers qualify.  Bumping the mtime of a header
    whose content actually changed would hide that change from
    ``_count_modified_files`` and from the next index run — the index would
    keep serving symbols parsed from the old text while reporting itself as
    up to date.

    Vendor, generated, and out-of-tree headers keep the unconditional
    behaviour: their stored hashes are trusted everywhere else in the
    pipeline, so to verify them here would be meaningless.

    Cost is not the reason for these rules.  To hash every header in the
    manifest takes 45 ms on the measured projects (1 037 headers on
    zbox-ecb-fw, 541 on a Zephyr build, 640 on an ESP32 build), against
    an index run of 42 minutes.  That is 0.002 %.
    """
    from .manifest import _hash_with_cache
    from .sdk_detect import _path_matches

    if header.get("generated"):
        return True
    try:
        rel_path = str(resolved.relative_to(project_root))
    except ValueError:
        return True  # outside project_root — hash is trusted from the manifest
    if any(_path_matches(rel_path, pat) for pat in vendor_patterns):
        return True
    return _hash_with_cache(resolved, hash_cache) == header.get("hash", "")


def _refresh_header_mtimes_from_manifest(
    conn,
    config_hash: str,
    project_root: Path,
    manifest: dict | None,
    *,
    vendor_patterns: list[str] | None = None,
    hash_cache: dict[str, str] | None = None,
) -> int:
    """Refresh stored mtimes for headers touched by VCS operations.

    After ``git checkout`` / ``git merge``, header files get new mtimes
    even when their content hasn't changed.  The stored ``files.mtime``
    values fall behind, causing ``_count_modified_files`` to report phantom
    modifications and spawn unnecessary background reindexes.

    This function scans the manifest's header entries and updates the
    stored mtime whenever the on-disk mtime is newer but the stored mtime
    is stale — fixing the drift without a full Tier-3 reparse.

    WHY only update when on-disk mtime is newer: we use ``UPDATE ... SET
    mtime=? WHERE mtime < ?`` — this is a one-way forward correction.
    If the on-disk mtime is OLDER than the stored mtime (e.g., after a
    ``git checkout`` of an older commit), we don't roll back the stored
    mtime because that would mark the file as modified and trigger a
    reindex.  The content hash check in the staleness detection will
    still catch actual content changes regardless of mtime.

    WHY the hash check (see :func:`_mtime_bump_is_safe`): a project header
    whose content really changed must keep its stale mtime.  Bumping it
    would erase the last signal that the index is behind — the header is
    not a TU of its own, so nothing else would ever notice.

    Called once after the main TU loop, before the manifest update phase.
    Returns the number of refreshed header records.
    """
    if manifest is None:
        return 0
    vendor_patterns = vendor_patterns or []
    refreshed = 0
    # The manifest's headers map already holds each path once, so there is no
    # per-TU loop and no dedup set to maintain.
    for header_path, record in (manifest.get("headers") or {}).items():
        # Resolve absolute path for stat() — the stored path may be relative
        p = Path(header_path)
        if not p.is_absolute():
            p_resolved = (project_root / p).resolve()
        else:
            p_resolved = p.resolve()
        try:
            cur_mtime = p_resolved.stat().st_mtime
        except OSError:
            continue
        if not _mtime_bump_is_safe(p_resolved, record, project_root, vendor_patterns, hash_cache):
            continue  # content really changed — the stale signal must survive
        # Use the manifest path directly — it already matches files.path format
        cur_obj = conn.execute(
            "UPDATE files SET mtime=? WHERE config_hash=? AND path=? AND mtime < ?",
            (cur_mtime, config_hash, header_path, cur_mtime),
        )
        if cur_obj.rowcount:
            refreshed += 1
    if refreshed:
        log.info("header mtimes refreshed from manifest: %d", refreshed)
    return refreshed


# REMOVED: _is_excluded — no files are excluded from indexing; all are indexed



def _update_manifest_after_index(
    *,
    manifest: dict | None,
    units: list,
    project_root: Path,
    db_dir: Path,
    compile_commands: Path,
    updated_count: int,
    tu_headers: dict[str, list[dict]] | None = None,
    build_dir_patterns: list[str] | None = None,
    vendor_patterns: list[str] | None = None,
    config_hash: str = "",
    scope: list[str] | None = None,
    reparsed_tus: set[str] | None = None,
) -> dict | None:
    """Update ``manifest.json`` after an indexing run.

    Strategy (ordered by cost):
    - No existing manifest → build from scratch (tokenize all TUs).
    - Manifest exists, nothing changed → skip (manifest is still valid).
    - Manifest exists, TUs changed, *tu_headers* provided → incremental:
      reuse stored entries for unchanged TUs, update only changed ones.
      This is the fast path — no extra libclang parsing needed.
    - Manifest exists, TUs changed, no *tu_headers* → fallback to full
      rebuild (re-tokenize all TUs).  This is the slow path.

    WHY incremental update: after indexing 500+ TUs, re-tokenizing every
    header takes minutes.  The incremental path reuses pre-collected header
    hashes from the main indexing loop (``tu_headers``), avoiding a second
    libclang parse for unchanged TUs.

    *reparsed_tus* names the TUs that were actually re-parsed in this run.
    Only those may contribute fresh header hashes: an entry rewritten for a
    TU that kept its previous symbols would claim the index is current while
    it still holds data parsed from the old header text.  Every other TU
    keeps its stored entry verbatim.  ``None`` disables the filter (used by
    callers with no run bookkeeping, e.g. a first index).

    *vendor_patterns* is the EFFECTIVE set this run used — what the builder
    derived plus what the config added.  It is stored so the query layer can
    read it instead of deriving its own: the strongest source for Zephyr is
    ``-fmacro-prefix-map`` in the compiler flags, and only the indexer has
    those.  A consumer that derives its own set answers with a different one,
    and the staleness check then re-hashes headers the indexer trusted.

    Returns the updated manifest dict, or ``None`` when no update needed.
    """
    from .manifest import MANIFEST_FORMAT, _collect_headers_from_tokens, _intern_arguments, save

    # No TU was re-parsed — keep the existing manifest as-is, provided:
    #   - the TU list hasn't changed (same number of entries), and
    #   - manifest entries have real source_hash data (not a preliminary
    #     manifest written by build_preliminary with empty hashes).
    # A different TU count means files were added/removed from
    # compile_commands.json.  A preliminary manifest means the on-disk
    # file was overwritten by build_preliminary and needs regeneration.
    #
    # WHY collected header hashes must NOT force a rewrite: *tu_headers* also
    # carries hashes gathered for TUs that were only bookkept, not re-parsed.
    # Writing those in would declare the manifest current while the index
    # still holds symbols parsed from the previous header text — the staleness
    # signal would be gone and only ``--force`` could recover.
    if manifest is not None and updated_count == 0:
        # Check for degraded (preliminary) manifest — entries with empty
        # source_hash mean build_preliminary overwrote the real manifest.
        # A preliminary manifest is written during the build phase before
        # any TU is parsed — it has the correct TU list but empty hashes.
        # We MUST regenerate with real hashes from the just-completed parse
        # to ensure accurate staleness detection on the next index run.
        entries = manifest.get("entries", [])
        if entries and not entries[0].get("source_hash"):
            log.info("Manifest has preliminary entries — regenerating with full hashes")
            # Fall through to full regeneration below (don't return early)
        else:
            old_count = len(entries)
            if old_count == len(units):
                return manifest
            log.info("Rebuilding manifest.json (TU count changed: %d → %d)", old_count, len(units))

    # ── Build/update manifest entries ──
    # Priority: 1) tu_headers (pre-collected during main loop — no extra I/O),
    # 2) old manifest entries (unchanged), 3) libclang tokenization (slow fallback).
    from .manifest import fold_headers, tu_arguments
    from .manifest import generate as generate_manifest
    from .manifest import load as reload_manifest

    # The manifest's two shared tables.  The header table is seeded from the
    # previous manifest so an entry carried over unchanged keeps the records
    # its path list points at; save() prunes whatever ends up unreferenced.
    header_table: dict[str, dict] = dict(manifest.get("headers") or {}) if manifest else {}
    arg_sets: list[list[str]] = []

    def carry_over(old_entry: dict) -> dict:
        """Re-point a reused entry at THIS manifest's arg_sets table.

        ``arg_set`` is an index into the table of the manifest it was written
        for.  Copying the entry without re-interning would leave the index
        pointing at a different argument list, or past the end of the new
        table — the reason this is a function and not an append.
        """
        carried = dict(old_entry)
        carried["arg_set"] = _intern_arguments(
            tu_arguments(manifest or {}, old_entry), arg_sets
        )
        return carried

    if tu_headers is not None:
        # Use pre-collected header hashes from _build_filtered_file_content.
        # Avoids a second libclang parse — tu_headers was populated during
        # the main TU loop for every unchanged/updated TU.
        log.info("Building manifest.json from %d pre-collected TU headers...", len(tu_headers))
        old_entries: dict[str, dict] = {}
        if manifest is not None:
            old_entries = {e.get("file", ""): e for e in manifest.get("entries", [])}
        entries = []
        reused = 0
        updated = 0

        for unit in units:
            try:
                tu_rel = str(unit.file.resolve().relative_to(project_root))
            except ValueError:
                tu_rel = str(unit.file.resolve())

            # Fresh hashes only from TUs that were really re-parsed —
            # see *reparsed_tus* in the docstring.
            if tu_rel in tu_headers and (reparsed_tus is None or tu_rel in reparsed_tus):
                source_hash = compute_source_hash(unit.file.resolve())
                entries.append(
                    {
                        "file": tu_rel,
                        "directory": str(unit.directory) if unit.directory else str(project_root),
                        "arg_set": _intern_arguments(unit.clang_args, arg_sets),
                        "source_hash": source_hash,
                        "headers": fold_headers(tu_headers[tu_rel], header_table),
                        "flags_hash": compute_flags_hash(unit.raw_entry) if unit.raw_entry else "",
                    }
                )
                updated += 1
            elif tu_rel in old_entries:
                entries.append(carry_over(old_entries[tu_rel]))
                reused += 1
            else:
                headers = _collect_headers_from_tokens(
                    unit, project_root, build_dir_patterns, header_table
                )
                source_hash = compute_source_hash(unit.file.resolve())
                entries.append(
                    {
                        "file": tu_rel,
                        "directory": str(unit.directory) if unit.directory else str(project_root),
                        "arg_set": _intern_arguments(unit.clang_args, arg_sets),
                        "source_hash": source_hash,
                        "headers": headers,
                        "flags_hash": compute_flags_hash(unit.raw_entry) if unit.raw_entry else "",
                    }
                )
                updated += 1

        log.info("manifest.json: %d updated (from tu_headers), %d reused", updated, reused)
    elif manifest is None:
        # No manifest and no tu_headers — full rebuild via libclang (slow)
        log.info("Generating manifest.json from %d TUs...", len(units))
        gen_hash = generate_manifest(
            compile_commands, db_dir, project_root, units,
            build_dir_patterns=build_dir_patterns, scope=scope,
            vendor_patterns=vendor_patterns,
        )
        return reload_manifest(db_dir, gen_hash)
    else:
        # ── Incremental update (tu_headers=None, manifest exists) ──
        old_entries = {e.get("file", ""): e for e in manifest.get("entries", [])}
        entries = []
        reused = 0
        updated = 0

        for unit in units:
            try:
                tu_rel = str(unit.file.resolve().relative_to(project_root))
            except ValueError:
                tu_rel = str(unit.file.resolve())

            if tu_rel in old_entries:
                entries.append(carry_over(old_entries[tu_rel]))
                reused += 1
            else:
                headers = _collect_headers_from_tokens(
                    unit, project_root, build_dir_patterns, header_table
                )
                source_hash = compute_source_hash(unit.file.resolve())
                entries.append(
                    {
                        "file": tu_rel,
                        "directory": str(unit.directory) if unit.directory else str(project_root),
                        "arg_set": _intern_arguments(unit.clang_args, arg_sets),
                        "source_hash": source_hash,
                        "headers": headers,
                        "flags_hash": compute_flags_hash(unit.raw_entry) if unit.raw_entry else "",
                    }
                )
                updated += 1

        log.info("manifest.json incremental: %d updated, %d reused", updated, reused)

    manifest_data = {
        "_format": MANIFEST_FORMAT,
        "compile_commands_path": str(compile_commands),
        "project_root": str(project_root),
        "arg_sets": arg_sets,
        "headers": header_table,
        "entries": entries,
    }
    # Preserve build_dir_patterns across incremental updates
    if build_dir_patterns:
        manifest_data["build_dir_patterns"] = build_dir_patterns
    elif manifest and manifest.get("build_dir_patterns"):
        manifest_data["build_dir_patterns"] = manifest["build_dir_patterns"]
    # Same inheritance for the vendor set: an incremental run that gets no
    # patterns must keep the ones the build was indexed with, or the next
    # staleness check reads a manifest that describes a different boundary.
    if vendor_patterns:
        manifest_data["vendor_patterns"] = vendor_patterns
    elif manifest and manifest.get("vendor_patterns"):
        manifest_data["vendor_patterns"] = manifest["vendor_patterns"]
    # Preserve macros from old manifest
    if manifest and manifest.get("macros"):
        manifest_data["macros"] = manifest["macros"]
    if not config_hash:
        from fw_context_mcp.config.settings import derive_project_id as _derive_id

        from .manifest import compute_config_hash as _compute_cc_hash

        config_hash = _compute_cc_hash(units, project_root, _derive_id(project_root), build_dir_patterns, scope=scope, db_dir=db_dir)
    config_hash = save(manifest_data, db_dir, config_hash)
    log.info(
        "manifest.json saved: %d TUs, %d distinct headers (%d references), config_hash=%s",
        len(entries),
        len(manifest_data.get("headers") or {}),
        sum(len(e.get("headers", [])) for e in entries),
        config_hash[:12],
    )
    return manifest_data



# ═══════════════════════════════════════════════════════════════
# SECTION: Core per-TU indexing
# ═══════════════════════════════════════════════════════════════

# INVARIANT: symbols.usr has UNIQUE NOT NULL, refs has no UNIQUE constraint.
# UPDATE + DELETE pattern prevents duplicates via NOT IN subquery on symbols.usr.
