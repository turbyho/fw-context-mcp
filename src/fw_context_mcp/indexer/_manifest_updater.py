"""Manifest update helpers extracted from runner.py.

Updates ``manifest.json`` after indexing: header mtime refresh and
incremental/full manifest rebuild.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..utils import compute_source_hash
from .config_hash import compute_flags_hash

log = logging.getLogger(__name__)


def _refresh_header_mtimes_from_manifest(
    conn,
    config_hash: str,
    project_root: Path,
    manifest: dict | None,
) -> int:
    """Refresh stored mtimes for headers touched by VCS operations.

    After ``git checkout`` / ``git merge``, header files get new mtimes
    even when their content hasn't changed.  The stored ``files.mtime``
    values fall behind, causing ``_count_modified_files`` to report phantom
    modifications and spawn unnecessary background reindexes.

    This function scans the manifest's header entries and updates the
    stored mtime whenever the on-disk mtime is newer but the stored mtime
    is stale — fixing the drift without a full Tier-3 reparse.

    Called once after the main TU loop, before the manifest update phase.
    Returns the number of refreshed header records.
    """
    if manifest is None:
        return 0
    refreshed = 0
    for entry in manifest.get("entries", []):
        for h in entry.get("headers", []):
            # Resolve absolute path for stat() — h["path"] may be relative
            p = Path(h["path"])
            if not p.is_absolute():
                p_resolved = (project_root / p).resolve()
            else:
                p_resolved = p.resolve()
            try:
                cur_mtime = p_resolved.stat().st_mtime
            except OSError:
                continue
            # Use the manifest path directly — it already matches files.path format
            cur_obj = conn.execute(
                "UPDATE files SET mtime=? WHERE config_hash=? AND path=? AND mtime < ?",
                (cur_mtime, config_hash, h["path"], cur_mtime),
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
    config_hash: str = "",
) -> dict | None:
    """Update ``manifest.json`` after an indexing run.

    Strategy:
    - No existing manifest → build from scratch (tokenize all TUs).
    - Manifest exists, nothing changed → skip (manifest is still valid).
    - Manifest exists, TUs changed, *tu_headers* provided → incremental:
      reuse stored entries for unchanged TUs, update only changed ones.
    - Manifest exists, TUs changed, no *tu_headers* → fallback to full
      rebuild (re-tokenize all TUs).

    Returns the updated manifest dict, or ``None`` when no update needed.
    """
    from .manifest import MANIFEST_FORMAT, _collect_headers_from_tokens, save

    # Nothing changed — keep existing manifest as-is, but only when all
    # of these hold:
    #   - TU list hasn't changed (same number of entries)
    #   - No stale header hashes were collected (tu_headers empty)
    #   - Manifest entries have real source_hash data (not a preliminary
    #     manifest written by build_preliminary with empty hashes)
    # A different TU count means files were added/removed from
    # compile_commands.json.  A preliminary manifest means the on-disk
    # file was overwritten by build_preliminary and needs regeneration.
    if manifest is not None and updated_count == 0 and not tu_headers:
        # Check for degraded (preliminary) manifest — entries with empty
        # source_hash mean build_preliminary overwrote the real manifest.
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
    from .manifest import generate as generate_manifest
    from .manifest import load as reload_manifest

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

            if tu_rel in tu_headers:
                source_hash = compute_source_hash(unit.file.resolve())
                entries.append(
                    {
                        "file": tu_rel,
                        "directory": str(unit.directory) if unit.directory else str(project_root),
                        "arguments": unit.clang_args,
                        "source_hash": source_hash,
                        "headers": tu_headers[tu_rel],
                        "flags_hash": compute_flags_hash(unit.raw_entry) if unit.raw_entry else "",
                    }
                )
                updated += 1
            elif tu_rel in old_entries:
                entries.append(old_entries[tu_rel])
                reused += 1
            else:
                headers = _collect_headers_from_tokens(unit, project_root, build_dir_patterns)
                source_hash = compute_source_hash(unit.file.resolve())
                entries.append(
                    {
                        "file": tu_rel,
                        "directory": str(unit.directory) if unit.directory else str(project_root),
                        "arguments": unit.clang_args,
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
        generate_manifest(compile_commands, db_dir, project_root, units, build_dir_patterns=build_dir_patterns)
        return reload_manifest(db_dir)
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
                entries.append(old_entries[tu_rel])
                reused += 1
            else:
                headers = _collect_headers_from_tokens(unit, project_root, build_dir_patterns)
                source_hash = compute_source_hash(unit.file.resolve())
                entries.append(
                    {
                        "file": tu_rel,
                        "directory": str(unit.directory) if unit.directory else str(project_root),
                        "arguments": unit.clang_args,
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
        "entries": entries,
    }
    # Preserve build_dir_patterns across incremental updates
    if build_dir_patterns:
        manifest_data["build_dir_patterns"] = build_dir_patterns
    elif manifest and manifest.get("build_dir_patterns"):
        manifest_data["build_dir_patterns"] = manifest["build_dir_patterns"]
    # Preserve macros from old manifest
    if manifest and manifest.get("macros"):
        manifest_data["macros"] = manifest["macros"]
    if not config_hash:
        from fw_context_mcp.config.settings import derive_project_id as _derive_id

        from .manifest import compute_config_hash as _compute_cc_hash

        config_hash = _compute_cc_hash(units, project_root, _derive_id(project_root), build_dir_patterns)
    config_hash = save(manifest_data, db_dir, config_hash)
    header_count = sum(len(e.get("headers", [])) for e in entries)
    log.info("manifest.json saved: %d TUs, %d headers, config_hash=%s", len(entries), header_count, config_hash[:12])
    return manifest_data



# ═══════════════════════════════════════════════════════════════
# SECTION: Core per-TU indexing
# ═══════════════════════════════════════════════════════════════

# INVARIANT: symbols.usr has UNIQUE NOT NULL, refs has no UNIQUE constraint.
# UPDATE + DELETE pattern prevents duplicates via NOT IN subquery on symbols.usr.
