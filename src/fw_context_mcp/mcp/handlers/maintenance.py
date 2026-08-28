"""Maintenance MCP tools — index health checks, LLM configuration,
project listing, index reset, and single-file reindexing.

These are diagnostic and administrative tools distinct from the
search and code-navigation tools in other handler modules.  They
do not query the symbol index for code-analysis purposes; instead
they inspect index metadata, configure runtime behavior, and
manage the index lifecycle.

Public MCP handlers in this module:
    get_active_build         — mandatory first-call index health report
    get_environment_status   — aggregate deps/build/index/llm in one call
    check_ollama             — LLM backend availability and model status
    check_dependencies       — read-only dependency audit (structured results)
    configure_llm            — write per-developer LLM settings to local.toml
    reindex_file             — re-parse a single source file into the index
    reindex_file_impl        — same as above with an ``--analysis`` equivalent
    reset_index              — delete the symbol database
    list_projects            — enumerate all indexed projects
    get_project_info         — look up a project by UUID4 in the global registry

Each handler is read-only except where noted in its docstring.
The ``Field(description=…)`` strings on public signatures are tool
registration metadata — they must not be modified casually."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...config import derive_project_id
from ...config import load as load_config
from ...config.settings import Config
from ...indexer.compile_commands import parse as parse_cc
from ...indexer.db import (
    CURRENT_SCHEMA_VERSION,
    DatabaseCorruptionError,
    WriteLockTimeout,
    compute_analysis_coverage,
    count_refs,
    get_active_config,
    get_all_builds_for_project,
    get_all_projects,
    get_db_schema_version,
    make_analysis_summary,
    open_db,
    transaction,
)
from ...llm.ollama import check_setup
from ...utils import resolve_project_root
from ..background import _is_bg_reindex_running
from ..shared.context import (
    _db_path,
    _detect_build_system,
    _is_stale,
    _quick_open_readonly,
    _resolve_context,
    get_executor,
    invalidate_executor,
)
from ..shared.stale import (
    _check_header_staleness,
    _count_modified_files,
    find_unindexed_sources,
)

log = logging.getLogger(__name__)


def _expand_dir_placeholder(dir_str: str, root: Path) -> str:
    """Expand the ``${NCS}`` placeholder in an image source dir (best-effort).

    ``dir`` for an SDK image may point outside project_root (``${NCS}/…``) —
    a machine-specific path that must not be committed literally.  The
    placeholder is expanded to the NCS root detected from standard signals;
    on failure the raw string is returned (the LLM still sees the placeholder).
    """
    if "${NCS}" not in dir_str:
        return dir_str
    try:
        from ...indexer.builders.zephyr import ZephyrBuildSystem

        ncs = ZephyrBuildSystem._detect_ncs(root)
        if ncs is not None:
            _, version, ncs_root = ncs
            return dir_str.replace("${NCS}", f"{ncs_root}/{version}")
    except Exception:  # nosec B110 — best-effort display expansion
        pass
    return dir_str


def _build_variant_discovery(cfg: Config, builds: list, root: Path) -> dict:
    """Build discovery data (variants/images/variant_images) for the LLM.

    WHY discovery: the LLM must know what the project contains (parts = images)
    and for which MCU variants indexes exist — without guessing names.  Source
    of truth is config ``[[build.variants]]`` first; when config ``images`` is
    empty (auto-detect), the actually-indexed ``build_configs`` rows fill in.
    """
    build_cfg = cfg.build
    variants_cfg = build_cfg.variants
    multi = bool(variants_cfg)

    variants = [
        {
            "name": v.name,
            "description": v.description,
            "board": v.board or build_cfg.board or "",
        }
        for v in variants_cfg
    ]

    images: list[dict] = []
    variant_images: dict[str, list[str]] = {}
    if any(v.images for v in variants_cfg):
        seen: set[str] = set()
        for v in variants_cfg:
            variant_images[v.name] = [img.name for img in v.images]
            for img in v.images:
                if img.name in seen:
                    continue
                seen.add(img.name)
                entry = {
                    "name": img.name,
                    "description": img.description,
                    "dir": _expand_dir_placeholder(img.dir, root) if img.dir else "",
                    "type": img.type,
                }
                if img.board:
                    entry["board"] = img.board
                images.append(entry)
    else:
        # Auto-detect from build_configs (no explicit images in config).
        seen_images: set[str] = set()
        for b in builds:
            variant_name = b["variant"] or ""
            image_name = b["image"] or ""
            if variant_name and image_name:
                variant_images.setdefault(variant_name, [])
                if image_name not in variant_images[variant_name]:
                    variant_images[variant_name].append(image_name)
            if image_name and image_name not in seen_images:
                seen_images.add(image_name)
                images.append({"name": image_name, "description": "", "dir": "", "type": "project"})

    return {
        "multi": multi,
        "variants": variants,
        "images": images,
        "variant_images": variant_images,
        "active_variant": build_cfg.default_variant,
        "active_image": build_cfg.default_image,
    }


def list_variants(
    project_root: Annotated[
        str | None, Field(description="Project root directory. Auto-detected from CWD if omitted.")
    ] = None,
) -> dict:
    """List every indexed build with its (variant, image, board) identity.

    Read-only diagnostic — shows what is actually indexed, not what the config
    declares.  Each row is one ``(variant, image)`` build with its own
    ``config_hash`` and symbol count.  For single-project indexes this returns
    one row with ``variant``/``image`` empty.

    Use ``get_active_build`` for the mandatory first-call health check and the
    human-readable ``variants``/``images`` discovery; use this tool to see the
    per-build ``config_hash`` and symbol counts (authoritative per-build state).

    Args:
        project_root: Project root directory. Auto-detected from CWD if
            omitted.

    Returns:
        dict: {builds (list[dict]), multi (bool — True when the config
        declares variants or a build has a non-empty variant name)}.

        Each build dict holds: variant (str — empty for a single-project
        index), image (str — empty for a single-project index), board (str),
        config_hash (str), symbol_count (int), file_count (int),
        manifest_verification (str — "full" or "none").

        When the project is not initialized, or has no index, the result is
        {builds: [], multi: False, error (str)}.
    """
    root = resolve_project_root(project_root)
    cfg = load_config(root)
    project_id = cfg.project.id
    if not project_id:
        return {"builds": [], "multi": False, "error": f"Project at {root} is not initialized."}
    db_path = cfg.index.db_dir / project_id / "index.db"
    if not db_path.exists():
        return {"builds": [], "multi": False, "error": f"No index found for {root}."}

    conn = _quick_open_readonly(db_path)
    try:
        builds = get_all_builds_for_project(conn, project_id)
    finally:
        conn.close()

    result_builds: list[dict] = []
    for b in builds:
        result_builds.append(
            {
                "variant": b["variant"] or "",
                "image": b["image"] or "",
                "board": b["board"] or "",
                "config_hash": b["config_hash"],
                "symbol_count": b["symbol_count"],
                "file_count": b["file_count"],
                "manifest_verification": b["manifest_verification"],
            }
        )

    multi = bool(cfg.build.variants) or any(r["variant"] for r in result_builds)
    return {"builds": result_builds, "multi": multi}


# ── moved from server.py ──
def get_active_build(
    project_root: Annotated[
        str | None, Field(description="Project root directory. Auto-detected from CWD if omitted.")
    ] = None,
    fast: Annotated[
        bool, Field(description="When True (default), reuse the cached manifest "
        "hashes for the header check. Both modes count modified files. Pass "
        "False to recompute the header hashes, which is far slower.")
    ] = True,
) -> dict:
    """MANDATORY FIRST CALL for C/C++ projects. Return metadata about the
    most recently indexed build configuration — check index health before
    using any other fw-context tools.

    Read-only, and it spawns no subprocess — the startup daemon thread and
    the file watcher own the background reindex.

    Act on ``status``:

    * ``"ready"`` — up to date. Continue.
    * ``"reindexing"`` — background reindex running; queries stay accurate.
      Continue. ``reindex_progress`` holds its last log line.
    * ``"reindex_needed"`` — schema mismatch, changed compile_commands.json,
      or a source file that compile_commands.json does not cover. Queries
      still work on existing data. Read ``reindex_reasons``: a missing
      source file needs ``fw-context index --build``, the other two need
      only ``fw-context index``.
    * ``"no_index"`` — initialized, never indexed. Run ``fw-context index``.
    * ``"not_initialized"`` — run ``fw-context init``.
    * ``"error"`` — DB corruption or access error. Use other tools.

    Three conditions set ``reindex_needed``: an outdated schema, a changed
    compile_commands.json, and a source file that is on disk but absent from
    compile_commands.json.  The third one needs a build, because only the
    build system writes that file — a plain reindex has no translation unit
    for the file and skips it without a word.  Modified source files are
    something else: they are handled per-query, and never set it.

    ``indexed_at`` and ``first_indexed_at`` are UTC; file mtimes are local
    time.  Never compare the two directly — in UTC+2 a correctly indexed
    file looks 2 hours newer than ``indexed_at``.  Call with ``fast=False``
    to find modified files.

    ``analysis`` splits the LLM-analysis coverage into project and vendor
    symbols:

    * ``model`` — the model of the analysis, or None. One model only, even
      when several were used.
    * ``analyze_vendor`` — the value at index time, not the current config.
    * ``project`` / ``vendor`` — ``{analyzed, skipped, total}``.
      ``skipped`` = tried, but not analyzable (body larger than the model
      context, an unparseable answer, or a body that was not readable).
    * ``complete`` — no work left: every project symbol is analyzed or
      skipped.  True exactly when ``reindex_reasons`` holds no "unanalyzed
      symbols" entry.  Vendor symbols excluded by ``analyze_vendor=False``
      never block it, thus ``vendor.total`` large with ``vendor.analyzed=0``
      is expected, not a defect.

    Args:
        project_root: Project root directory. Auto-detected from CWD if
            omitted.
        fast: When True (default), the header check reuses the cached
            manifest hashes.  Both modes run the per-file scan, thus
            ``modified_files_count`` is accurate either way — a tool that
            reported "ready" while the search tools warned about the same
            file gave the caller two readings and no way to choose.
            False recomputes the header hashes and costs several times more.

    Returns:
        dict: {config_hash, project_id, project_root, build_system,
        compile_commands, indexed_at (str — "YYYY-MM-DD HH:MM:SS" in UTC,
        the completion time of the last full index), symbol_count, file_count,
        reference_count, modified_files_count (int — files whose content no
        longer matches the index; counted in both modes),
        header_affected_tus (int — number of TUs with stale header
        dependencies), manifest_verification (str —
        "full" when manifest.json exists, "none" otherwise),
        analysis (dict — LLM-analysis coverage split by project/vendor:
        {model, analyze_vendor, project: {analyzed, skipped, total},
        vendor: {analyzed, skipped, total}, complete}),
        description (str), first_indexed_at (str — UTC, same format as
        indexed_at),
        vendor_paths (list[str] — config index.vendor_paths),
        project_paths (list[str] — config index.project_paths),
        bg_reindex_running (bool),
        reindex_progress (str or None — last log line when reindex is running),
        schema_version (int — DB schema version),
        current_schema (int — code expects), status (str — "ready"|"reindexing"|
        "reindex_needed"|"no_index"|"not_initialized"|"error"), reindex_needed (bool —
        structural mismatch requiring a full reindex),
        reindex_reasons (list[str] — why reindex is needed, empty when False),
        stale (bool — True when reindex_needed or header_affected_tus > 0),
        _warning (str, optional — when manifest verification is not "full"),
        vec_available (bool), vec_error (str, optional),
        index_message (str — human-readable summary of index state),
        multi (bool — True for a multi-variant project),
        variants (list[dict] — {name, description, board}),
        images (list[dict] — {name, description, dir, type}),
        variant_images (dict — variant name to its image names),
        active_variant (str or None — [build] default_variant),
        active_image (str or None — [build] default_image)}

        For a project that is not initialized, the result holds only
        ``status``, ``project_root``, and ``index_message``.  When no index
        exists, the result adds ``project_id``.
    """
    root = resolve_project_root(project_root)
    # Resolve project ID directly — bypass _db_path/derive_project_id so
    # get_active_build can report not_initialized/no_index gracefully
    # instead of raising.  Other tools still use derive_project_id
    # (via _resolve_context) and raise ProjectNotInitializedError when
    # the project is not initialized — fail-fast for operational tools,
    # graceful degradation for this diagnostic tool.
    fw_cfg = load_config(root)
    project_id = fw_cfg.project.id
    if not project_id:
        # The project directory exists but was never initialized.
        # Return a human-readable status so the LLM can guide the user.
        return {
            "status": "not_initialized",
            "project_root": str(root),
            "index_message": (
                f"Project at {root} is not initialized. "
                "Run `fw-context init` via bash to create a project ID and config, "
                "then call get_active_build() again."
            ),
        }
    db_path = fw_cfg.index.db_dir / project_id / "index.db"
    if not db_path.exists():
        # Project is initialized but no index was ever built.
        # The LLM uses this to prompt the user for indexing.
        return {
            "status": "no_index",
            "project_root": str(root),
            "project_id": project_id,
            "index_message": (
                f"No symbol index found for {root}. "
                "Run `fw-context index <path/to/compile_commands.json>` via bash "
                "to build the index, then call get_active_build() again."
            ),
        }

    # Check bg reindex status BEFORE querying the DB — when a bg reindex
    # is running, the _modified_cache may contain stale counts from before
    # the reindex started.  Skipping the cache ensures accurate counts.
    bg_running = _is_bg_reindex_running(root)

    # Open a short-lived read-only connection to fetch the active config
    # hash.  We close it immediately — the actual heavy queries run
    # through the executor on its own connection.
    conn = _quick_open_readonly(db_path)
    try:
        project_id = derive_project_id(root)
        cfg = get_active_config(conn, project_id)
        if not cfg:
            return {"error": f"No build config indexed for project at {root}."}
        config_hash = cfg["config_hash"]
    finally:
        conn.close()

    # The executor serializes all DB access through a single shared
    # connection.  This prevents SQLITE_BUSY errors when the MCP server
    # handles concurrent requests and a background reindex is running.
    executor = get_executor(db_path)

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        sym_count = conn.execute("SELECT COUNT(*) FROM symbols WHERE config_hash=?", (config_hash,)).fetchone()[0]
        file_count = conn.execute("SELECT COUNT(*) FROM files WHERE config_hash=?", (config_hash,)).fetchone()[0]
        ref_count = count_refs(conn, config_hash)
        manifest_verification = cfg.get("manifest_verification", "none")
        if fast:
            # The scan runs in fast mode too.  Without it this tool answered
            # "ready" while search_code on the same index warned about the
            # same file, and the caller had no way to tell which reading
            # held.  It costs about 41 ms on a project of 1900 files,
            # against the 749 ms that the header check below already spends.
            #
            # use_cache=False on purpose: the cache validates itself against
            # MAX(mtime) of the files table, which only a reindex moves, thus
            # a cached answer would miss the edit that just happened.
            modified_count = _count_modified_files(conn, config_hash, root, use_cache=False)
            # NOTE: the header check is NOT cheap — it hashes every header of
            # every translation unit, which is 22,693 files and 749 ms on
            # zbox-ecb-fw.  `fast` only gives it a cache; it does not skip it.
            if manifest_verification == "full":
                header_affected_tus, _ = _check_header_staleness(
                    conn, config_hash, root, use_cache=True,
                )
            else:
                header_affected_tus = 0
        else:
            # Full stat scan — walks every indexed file on disk to detect
            # mtime changes.  Slow (~100 ms for large projects) but accurate.
            modified_count = _count_modified_files(conn, config_hash, root, use_cache=False)
            # Check header dependencies separately (different metric: TUs, not files)
            if manifest_verification == "full":
                header_affected_tus, _ = _check_header_staleness(
                    conn,
                    config_hash,
                    root,
                    use_cache=False,
                )
            else:
                header_affected_tus = 0
        db_schema_ver = get_db_schema_version(conn)

        # LLM analysis coverage — split by project vs vendor so the LLM can
        # tell intentionally-skipped vendor symbols apart from project
        # symbols that genuinely still need analysis.
        coverage = compute_analysis_coverage(conn, config_hash)
        # Exclude the skip:* sentinels (skip:toolarge, skip:unparseable) —
        # they are not model names and must never surface as analysis.model.
        analysis_model_row = conn.execute(
            """SELECT a.model FROM llm_analysis a
               JOIN symbols s ON s.id = a.symbol_id
               WHERE s.config_hash = ?
                 AND a.model NOT LIKE 'skip:%' LIMIT 1""",
            (config_hash,),
        ).fetchone()
        model = analysis_model_row["model"] if analysis_model_row else None

        # Read stored analyze_vendor — what was actually indexed.
        # Fall back to False for DBs that predate the column.
        try:
            stored_analyze_vendor = bool(
                conn.execute(
                    "SELECT analyze_vendor FROM build_configs WHERE config_hash = ?",
                    (config_hash,),
                ).fetchone()["analyze_vendor"]
            )
        except sqlite3.OperationalError:
            stored_analyze_vendor = False

        # Load project config for vendor_paths/project_paths in the response.
        proj_cfg = load_config(project_root=root)

        analysis = make_analysis_summary(coverage, stored_analyze_vendor, model)

        cc_changed, stale_reason = _is_stale(cfg, cfg["compile_commands_path"])
        schema_old = db_schema_ver < CURRENT_SCHEMA_VERSION

        # A source file that the build system never saw has no translation
        # unit, thus a plain reindex cannot pick it up: it is absent from
        # compile_commands.json, and only a build puts it there.  Without
        # this check the tool reported "ready" over a file it knew nothing
        # about, and it kept reporting it after a reindex.
        new_sources = find_unindexed_sources(
            conn, config_hash, root, Path(cfg["compile_commands_path"])
        )

        # Three conditions force a reindex:
        #   1. Schema version in DB is older than current code expects.
        #   2. compile_commands.json was modified since indexing
        #      (detected via mtime comparison in _is_stale).
        #   3. A source file is missing from compile_commands.json.
        # Modified source files are handled per-query via auto-reindex
        # and do NOT cause reindex_needed=True.
        needs_reindex = cc_changed or schema_old or bool(new_sources)

        # Build reindex_reasons — only when reindex is actually needed
        reindex_reasons: list[str] = []
        if schema_old:
            reindex_reasons.append(f"schema_mismatch: {db_schema_ver} < {CURRENT_SCHEMA_VERSION}")
        if cc_changed:
            reindex_reasons.append(stale_reason or "compile_commands_changed")
        if new_sources:
            listed = ", ".join(new_sources[:3])
            more = f" and {len(new_sources) - 3} more" if len(new_sources) > 3 else ""
            reindex_reasons.append(
                f"{len(new_sources)} source file(s) not in compile_commands.json "
                f"({listed}{more}) — needs `fw-context index --build`"
            )

        # Determine status — single value that drives LLM decision-making.
        # Priority: reindex_needed > reindexing > ready.
        if needs_reindex:
            status = "reindex_needed"
        elif bg_running:
            status = "reindexing"
        else:
            status = "ready"

        # Build human-readable index message
        if status == "ready" and modified_count:
            # The index is structurally sound, thus the status stays "ready"
            # and every query keeps working.  But files on disk have moved
            # past it, and the per-query warnings will say so — this message
            # must not contradict them by claiming full freshness.
            index_message = (
                f"Index is usable ({sym_count} symbols), but {modified_count} file(s) "
                f"changed since the last index run. Results about those files can be "
                f"out of date — run `fw-context index`."
            )
        elif status == "ready":
            index_message = f"Index is fully up to date ({sym_count} symbols)"
        elif status == "reindexing":
            if modified_count:
                index_message = (
                    f"Index is usable — {modified_count} file(s) being reindexed "
                    f"in background. All queries return accurate results."
                )
            else:
                index_message = (
                    "Index is usable — background reindex in progress. All queries return accurate results."
                )
        elif new_sources:
            # Checked before the other reindex reasons: `--build` is the only
            # command that repairs this one, and a message that says plain
            # `index` would send the caller in a circle.
            index_message = (
                f"{len(new_sources)} source file(s) are not in compile_commands.json "
                f"(first: {new_sources[0]}). A plain reindex cannot see them — "
                f"run `fw-context index --build`. Queries still work on existing data."
            )
        elif schema_old and cc_changed:
            index_message = (
                f"Schema version mismatch ({db_schema_ver} < {CURRENT_SCHEMA_VERSION}) "
                f"and compile_commands.json changed. Run fw-context index. "
                f"Queries still work on existing data."
            )
        elif schema_old:
            index_message = (
                f"Schema version mismatch ({db_schema_ver} < {CURRENT_SCHEMA_VERSION}). "
                f"Run fw-context index. Queries still work on existing data."
            )
        else:
            index_message = "Compile commands changed — run fw-context index. Queries still work on existing data."

        # When manifest verification is not full, warn the LLM.
        _warning = None
        if manifest_verification != "full":
            if bg_running:
                _warning = (
                    "Index was built without full header dependency "
                    "tracking (manifest verification: "
                    f"{manifest_verification}). A reindex is currently "
                    "running — the index will be updated once it "
                    "completes. You may continue analysis, but note "
                    "that header changes since the last completed "
                    "index may not be reflected."
                )
                index_message += (
                    f" — manifest verification: {manifest_verification} (reindex in progress — wait for completion)"
                )
            else:
                _warning = (
                    "⚠️ INDEX DEGRADED — STOP ALL C/C++ ANALYSIS "
                    "IMMEDIATELY.\n\n"
                    "manifest.json is NOT available "
                    "for this index (manifest verification: "
                    f"{manifest_verification}). The index may contain "
                    "STALE data — header changes cannot be detected "
                    "without manifest.json. Continuing analysis with a "
                    "stale index WILL produce incorrect results.\n\n"
                    "REQUIRED: Tell the user to run 'fw-context index' "
                    "to rebuild with full dependency tracking. Do NOT "
                    "continue any C/C++ analysis until the user "
                    "confirms the index has been rebuilt."
                )
                index_message += (
                    f" — manifest verification: {manifest_verification} (run 'fw-context index' for full tracking)"
                )

        if header_affected_tus:
            # A TU that failed to parse keeps its needs_reparse mark, so it
            # counts here run after run.  Without the second half of this
            # sentence the user is told to run 'fw-context index', and the
            # index does not clear it.
            index_message += (
                f" | {header_affected_tus} TU(s) have stale header dependencies "
                "— header changes since last index, or a TU that failed to "
                "parse (see the index log)"
            )

        # Append LLM-analysis coverage, split by project/vendor, so the LLM
        # reads unanalyzed vendor symbols as expected (analyze_vendor=false),
        # not as a defect.
        index_message += _analysis_message(analysis, stored_analyze_vendor)

        result: dict = {
            "config_hash": config_hash,
            "project_id": project_id,
            "project_root": str(root),
            "build_system": proj_cfg.build.system or _detect_build_system(root),
            "compile_commands": cfg["compile_commands_path"],
            "indexed_at": cfg["created_at"],
            "symbol_count": sym_count,
            "file_count": file_count,
            "reference_count": ref_count,
            "modified_files_count": modified_count,
            "header_affected_tus": header_affected_tus,
            "schema_version": db_schema_ver,
            "current_schema": CURRENT_SCHEMA_VERSION,
            "analysis": analysis,
            "manifest_verification": manifest_verification,
            "description": cfg["description"] if "description" in cfg.keys() else "",
            "first_indexed_at": cfg["first_indexed_at"] if "first_indexed_at" in cfg.keys() else "",
            "vendor_paths": proj_cfg.index.vendor_paths,
            "project_paths": proj_cfg.index.project_paths,
            # What the INDEX RUN applied, which is not always what the config
            # says today: the builder derives part of it, a variant adds to
            # it, and the config can have changed since.  Three layers with no
            # way to read the result are worse than two.
            "effective_vendor_patterns": _effective_vendor_patterns(
                db_path.parent, config_hash, root
            ),
            "status": status,
            "reindex_needed": needs_reindex,
            "reindex_reasons": reindex_reasons,
            "stale": needs_reindex or header_affected_tus > 0,
            "index_message": index_message,
        }
        if _warning is not None:
            result["_warning"] = _warning

        # ── Multi-variant discovery (§5.6.A) ──
        # Tell the LLM what the project contains (parts = images) and for which
        # variants indexes exist — before it guesses names.  Sources: config
        # [[build.variants]] first, build_configs fallback.
        builds = get_all_builds_for_project(conn, project_id)
        discovery = _build_variant_discovery(proj_cfg, builds, root)
        result["multi"] = discovery["multi"]
        result["variants"] = discovery["variants"]
        result["images"] = discovery["images"]
        result["variant_images"] = discovery["variant_images"]
        result["active_variant"] = discovery["active_variant"]
        result["active_image"] = discovery["active_image"]

        if discovery["multi"]:
            # Aggregate health fields across builds (shared common/ code is
            # counted N times — sums are "total symbol records", not unique).
            result["symbol_count"] = sum(b["symbol_count"] for b in builds)
            result["file_count"] = sum(b["file_count"] for b in builds)
            result["reference_count"] = sum(b["reference_count"] for b in builds)
            # config_hash/compile_commands reflect the default build only; empty
            # without [build] default_variant (the LLM must pick a variant).
            default_build = None
            if discovery["active_variant"]:
                default_build = get_active_config(
                    conn, project_id, discovery["active_variant"], discovery["active_image"] or ""
                )
            result["config_hash"] = default_build["config_hash"] if default_build else ""
            result["compile_commands"] = (
                default_build["compile_commands_path"] if default_build else ""
            )

        return result

    result = executor.execute_sync(_query, config_hash)
    if "error" in result:
        return result
    # Background reindex is managed by the startup daemon thread and the
    # file watcher — get_active_build() is a read-only tool and should
    # not spawn subprocesses.
    result["bg_reindex_running"] = bg_running
    if bg_running:
        result["reindex_progress"] = _read_reindex_progress(db_path)

    # sqlite-vec is an optional C extension for vector embeddings.
    # Report its availability so semantic_search can degrade gracefully.
    from ...deps import _vec_available
    vec_ok, vec_err = _vec_available()
    result["vec_available"] = vec_ok
    if vec_err is not None:
        result["vec_error"] = vec_err

    return result
def _read_reindex_progress(db_path: Path) -> str | None:
    """Read the last log line from the background reindex process.

    Opens ``reindex.log`` in the same directory as the index database.
    Reads only the last 4 KiB of the file — sufficient for a single
    log line without reading a multi-megabyte log file into memory.
    Returns ``None`` when the log file is missing or empty.
    """
    log_file = db_path.parent / "reindex.log"
    try:
        with open(log_file, encoding="utf-8") as fh:
            fh.seek(0, 2)
            file_size = fh.tell()
            if file_size == 0:
                return None
            fh.seek(max(0, file_size - 4096))
            last_chunk = fh.read()
            lines = last_chunk.splitlines()
            return lines[-1].strip() if lines else None
    except (OSError, IndexError):
        return None


# ── moved from server.py ──
def _analysis_message(analysis: dict, analyze_vendor: bool) -> str:
    """Return the LLM-analysis part of ``index_message``.

    Takes the ``analysis`` dict from ``make_analysis_summary`` and gives one
    line for a human reader.  The line always names the skipped symbols,
    because the pipeline cannot analyze them — the gap between ``analyzed``
    and ``total`` is not a defect.

    With ``analyze_vendor=False`` the line reports the project counts, and
    says that fw-context skipped the vendor symbols by design.  With
    ``analyze_vendor=True`` it reports both sides.

    Args:
        analysis: The ``analysis`` dict — {project, vendor} each with
            ``analyzed``, ``skipped``, and ``total``.
        analyze_vendor: The stored value, thus the message describes the
            build that fw-context indexed, not the current config.

    Returns:
        str: the message part, with a leading ``" | "`` separator.
    """
    p = analysis["project"]
    v = analysis["vendor"]
    if analyze_vendor:
        skipped_note = ""
        if p["skipped"] or v["skipped"]:
            skipped_note = (
                f" ({p['skipped']} project, {v['skipped']} vendor symbols skipped)"
            )
        return (
            f" | LLM analysis: project {p['analyzed']}/{p['total']}, "
            f"vendor {v['analyzed']}/{v['total']}{skipped_note}"
        )
    skipped_note = f", {p['skipped']} project symbols skipped" if p["skipped"] else ""
    return (
        f" | LLM analysis: project {p['analyzed']}/{p['total']}{skipped_note} "
        "(vendor skipped — analyze_vendor=false)"
    )


def _list_status(db_schema_ver: int, cc_stale: bool) -> str:
    """Map schema version and compile-commands staleness to a status label.

    Returns ``"reindex_needed"`` when either the DB schema is outdated
    or compile_commands.json has changed.  Returns ``"ready"`` otherwise.
    Used by ``list_projects`` to summarize each indexed project.
    """
    needs = (db_schema_ver < CURRENT_SCHEMA_VERSION) or cc_stale
    return "reindex_needed" if needs else "ready"


def list_projects(
    project_root: Annotated[
        str | None,
        Field(description="Project root. Auto-detected if omitted. Pass to distinguish multiple indexed projects."),
    ] = None,
) -> list[dict]:
    """List all indexed firmware projects with their statistics.

    Read-only. No side effects. Use at session start to discover available
    projects; use ``get_active_build`` for details on the currently active project.

    ``indexed_at`` and ``first_indexed_at`` are UTC, in ``"YYYY-MM-DD
    HH:MM:SS"`` format — the same format that ``get_active_build`` returns.

    ``analysis`` holds the ``project`` and ``vendor`` counts only.  For the
    ``model``, ``analyze_vendor``, and ``complete`` fields, call
    ``get_active_build`` for that project.

    Args:
        project_root: Project root. Auto-detected if omitted. Pass to
            distinguish multiple indexed projects.

    Returns:
        list of dicts, each with: project_id, name, root_path, build_system,
        symbol_count, file_count, indexed_at (str — UTC), description (str),
        first_indexed_at (str — UTC), schema_version, current_schema,
        reindex_needed (bool), status (str — "ready" or "reindex_needed"),
        db (path to SQLite database file),
        variant_count (int — number of build variants),
        image_count (int — number of sysbuild images),
        analysis (dict — LLM-analysis coverage
        {project: {analyzed, skipped, total},
        vendor: {analyzed, skipped, total}}, or None when no build is
        indexed).

        When no project has an index, the result is a single dict with an
        ``info`` key.  When fw-context cannot read a database, the result
        holds a dict with ``db`` and ``error`` keys for that file.
    """
    cfg = load_config(project_root=Path(project_root).resolve() if project_root else None)
    index_dir = cfg.index.db_dir
    # Glob for subdirectories containing index.db — each subdirectory
    # is named after a project_id UUID4.
    db_files = list(index_dir.glob("*/index.db")) if index_dir.exists() else []
    if not db_files:
        return [{"info": f"No indexed projects found under {index_dir}."}]
    results: list[dict] = []
    for db_path in sorted(db_files):
        try:
            executor = get_executor(db_path)

            def _query(conn, _config_hash):
                # Runs under the executor lock on the single shared
                # connection; must not open its own connection.
                rows = get_all_projects(conn)
                schema_ver = get_db_schema_version(conn)
                counts = {}
                for row in conn.execute(
                    "SELECT project_id, "
                    "COUNT(DISTINCT CASE WHEN variant != '' THEN variant END) AS vc, "
                    "COUNT(DISTINCT CASE WHEN image != '' THEN image END) AS ic "
                    "FROM build_configs GROUP BY project_id"
                ).fetchall():
                    counts[row["project_id"]] = (row["vc"] or 0, row["ic"] or 0)
                coverage_map = {
                    row["config_hash"]: compute_analysis_coverage(conn, row["config_hash"])
                    for row in rows
                    if row["config_hash"]
                }
                return rows, schema_ver, counts, coverage_map

            rows, db_schema_ver, variant_counts, coverage_map = executor.execute_sync(_query, "")
            for r in rows:
                # Staleness check: compare stored creation time with
                # compile_commands.json mtime.  Does not require
                # _wrap_tool — list_projects is informational only.
                cc_stale = (
                    _is_stale(
                        {"created_at": r["created_at"]},
                        r["compile_commands_path"],
                    )[0]
                    if r["compile_commands_path"]
                    else False
                )
                root = Path(r["root_path"]) if r["root_path"] else None
                # Report the CONFIGURED build system first — projects without
                # markers (e.g. an NCS workspace) resolve system from config,
                # not from marker detection, so _detect_build_system would
                # wrongly report "unknown".
                try:
                    _pc = load_config(project_root=root) if root else None
                    _bs = (_pc.build.system if _pc else None) or (_detect_build_system(root) if root else "unknown")
                except Exception:
                    _bs = _detect_build_system(root) if root else "unknown"
                cov = coverage_map.get(r["config_hash"]) if r["config_hash"] else None
                results.append(
                    {
                        "project_id": r["project_id"],
                        "name": r["name"],
                        "root_path": r["root_path"],
                        "build_system": _bs,
                        "symbol_count": r["symbol_count"],
                        "file_count": r["file_count"],
                        "indexed_at": r["created_at"],
                        "description": r["description"] if "description" in r.keys() else "",
                        "first_indexed_at": r["first_indexed_at"] if "first_indexed_at" in r.keys() else "",
                         "schema_version": db_schema_ver,
                         "current_schema": CURRENT_SCHEMA_VERSION,
                         "reindex_needed": cc_stale or db_schema_ver < CURRENT_SCHEMA_VERSION,
                         "status": _list_status(db_schema_ver, cc_stale),
                         "db": str(db_path),
                         "variant_count": variant_counts.get(r["project_id"], (0, 0))[0],
                         "image_count": variant_counts.get(r["project_id"], (0, 0))[1],
                         "analysis": (
                             {"project": cov["project"], "vendor": cov["vendor"]}
                             if cov else None
                         ),
                    }
                )
        except (sqlite3.Error, OSError) as e:
            results.append({"db": str(db_path), "error": str(e)})
    return results


# ── moved from server.py ──
def reset_index(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    confirm: Annotated[
        bool, Field(description="Must be True to execute. Call without confirm first as dry-run.")
    ] = False,
) -> dict:
    """Delete the entire symbol index for a project.

    Not read-only — permanently deletes the SQLite database and WAL files.
    Call with ``confirm=False`` first (dry-run) to see what would be deleted.
    Re-index with ``fw-context index`` afterwards.

    Handles corrupt databases gracefully — you can delete a corrupt index
    without needing to open it first.

    Args:
        project_root: Project root directory. Auto-detected if omitted.
        confirm: Must be True to execute. Call without first as dry-run.

    Returns:
        dict: {project_root, db, project_id, action: "dry_run"|"deleted",
        message, symbol_count, indexed_at (dry-run)}.

        A ``warning`` key means that the database is corrupt — the
        integrity check failed, thus the counts can be incomplete.

        On failure the dict holds only ``error`` with the reason.
    """
    root = resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Nothing to reset."}
    project_id = derive_project_id(root)
    cfg_data = None
    sym_count = 0
    corrupt = False
    conn = None
    try:
        conn = open_db(db_path)
        # DatabaseCorruptionError is raised during open when the integrity
        # check (PRAGMA integrity_check) fails.  A corrupt DB can still
        # be deleted — we track the flag and skip symbol counting.
    except DatabaseCorruptionError:
        corrupt = True
    else:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if cfg_data:
                sym_count = conn.execute(
                    "SELECT COUNT(*) FROM symbols WHERE config_hash=?",
                    (cfg_data["config_hash"],),
                ).fetchone()[0]
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    info: dict[str, object] = {
        "project_root": str(root),
        "db": str(db_path),
        "project_id": project_id,
    }
    if cfg_data:
        info["symbol_count"] = sym_count
        info["indexed_at"] = cfg_data["created_at"]
    elif corrupt:
        info["warning"] = "Database is corrupt — integrity check failed."
    if not confirm:
        info["action"] = "dry_run"
        if corrupt:
            info["message"] = (
                f"Database at {db_path} is corrupt. "
                "Call reset_index(confirm=True) to delete it anyway, "
                "then run 'fw-context index' to rebuild."
            )
        else:
            info["message"] = f"Would delete {db_path}. Call reset_index(confirm=True) to proceed."
        return info
    from ..background import bg_reindex_pause

    with bg_reindex_pause(root):
        # Mandatory ordering:
        # 1. The dry-run connection above is already closed (finally block).
        # 2. Invalidate the executor BEFORE deleting the DB file —
        #    otherwise it keeps a connection to an unlinked file and
        #    every subsequent query silently reads stale data forever.
        # 3. Only then delete the files.
        invalidate_executor(str(db_path.resolve()))
        db_path.unlink()
        # WAL and SHM files are sidecar files managed by SQLite in WAL
        # journal mode.  They must be deleted alongside the main DB file
        # to prevent a zombie WAL from being replayed into a fresh index.
        for suffix in ("-wal", "-shm", "-journal"):
            p = db_path.with_name(db_path.name + suffix)
            p.unlink(missing_ok=True)
        from ...mcp.shared.stale import _invalidate_modified_cache
        _invalidate_modified_cache()  # clear all entries — DB is gone
        info["action"] = "deleted"
        info["message"] = f"Index deleted. Run 'fw-context index' in {root} to rebuild."
    return info


# ── moved from server.py ──
def _reindex_cleanup_deleted_file(
    conn: sqlite3.Connection,
    cfg_data: sqlite3.Row,
    target: Path,
    root: Path,
    db_path: Path,
) -> dict:
    """Remove index records for a file that no longer exists on disk.

    Called when ``reindex_file`` is invoked for a path that the user
    deleted after indexing.  Fetches the file's old ID from the index,
    counts deleted symbols for reporting, and purges all records
    (symbols, references, analysis) via ``purge_file_records``.

    Pauses background reindex to avoid a race: the bg thread might
    also detect the deletion and attempt its own cleanup.
    """
    config_hash = cfg_data["config_hash"]
    from ...indexer.db import get_file_mtimes, purge_file_records
    from ...indexer.db._locking import WriteLockTimeout
    from ...indexer.ops import _normalize_file_path
    from ..background import bg_reindex_pause

    known = get_file_mtimes(conn, config_hash)
    file_path_str = _normalize_file_path(str(target), root)
    if file_path_str not in known:
        return {"error": f"File not found on disk or in index: {target}"}

    file_id_old, _ = known[file_path_str]
    symbol_count = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE file_id = ?", (file_id_old,)
    ).fetchone()[0]

    from ...mcp.shared.stale import _invalidate_modified_cache
    _invalidate_modified_cache(config_hash)

    with bg_reindex_pause(root):
        try:
            # write_lock_held=False, transaction_held=False — this caller
            # does NOT hold either; purge_file_records acquires its own.
            purge_file_records(
                conn, config_hash, file_id_old, file_path_str,
                db_dir=db_path.parent,
            )
            return {
                "file": str(target),
                "symbols_removed": symbol_count,
                "action": "deleted",
            }
        except WriteLockTimeout as e:
            return {"error": f"Could not acquire write lock for cleanup of {target}: {e}", "action": "timeout"}


def _reindex_parse_and_store(
    conn: sqlite3.Connection,
    matching: list,
    cfg_data: sqlite3.Row,
    cfg_refs: bool,
    root: Path,
    db_path: Path,
    target: Path,
    vendor_patterns: list[str],
    project_patterns_list: list[str],
) -> tuple[int, dict]:
    """Parse one or more translation units with libclang and store their symbols.

    Steps:
    1. Call ``extract_all`` for each TU — this invokes libclang to parse
       the file with the compiler flags from compile_commands.json.
    2. Within a single write-lock transaction, call ``store_symbols_for_unit``
       for each successfully parsed TU to upsert symbols into the database.
    3. Run macro resolution (best-effort — silently skipped on failure).
    4. Delete orphan file records (files that disappeared from the index).
    5. Update the manifest.json header-dependency tracking file.

    Returns ``(total_symbols, result_dict)``.  The result dict includes
    ``symbols_updated``, ``elapsed_s``, and optionally ``skipped_tus``.
    """
    from ...indexer.compile_commands import CompilationUnit as TU
    from ...indexer.db import write_lock as db_write_lock
    from ...indexer.ops import store_symbols_for_unit
    from ...indexer.symbols import (
        ExtractionResult,
        extract_all,
    )
    from ..background import bg_reindex_pause

    config_hash = cfg_data["config_hash"]

    # Phase 1: parse all matching TUs before acquiring the write lock.
    # Parsing is CPU-bound and does not touch the database — doing it
    # outside the lock minimises contention.
    parsed_units: list[tuple[TU, ExtractionResult]] = []
    skipped_tus: list[str] = []
    for unit in matching:
        try:
            parsed = extract_all(unit, with_refs=cfg_refs)
            parsed_units.append((unit, parsed))
        except sqlite3.Error as exc:
            return 0, {"error": f"DB error during parse of {unit.file.name}: {exc}"}
        except RuntimeError as exc:
            # Clang parse failures (missing headers, syntax errors) are
            # non-fatal — skip the TU and continue with others.
            log.warning("skip TU %s during reindex: %s", unit.file.name, exc)
            skipped_tus.append(str(unit.file.name))

    from ...mcp.shared.stale import _invalidate_modified_cache
    _invalidate_modified_cache(config_hash)

    with bg_reindex_pause(root):
        t0 = time.monotonic()
        total_symbols = 0

        try:
            # Phase 2: acquire the write lock and store all parsed symbols
            # in a single transaction per TU.  Each TU gets its own
            # transaction so partial failures don't roll back all work.
            with db_write_lock(db_path.parent, timeout=60.0):
                for unit, parsed in parsed_units:
                    with transaction(conn):
                        syms_added, _, _headers = store_symbols_for_unit(
                            conn, unit, config_hash, root,
                            vendor_patterns=vendor_patterns,
                            project_patterns=project_patterns_list,
                            index_refs=cfg_refs,
                            pre_parsed=parsed,
                        )
                        total_symbols += syms_added

                # Phase 3: resolve macro definitions after all TUs are stored.
                # Macro resolution needs the clang flags from one TU (they
                # are identical for TUs of the same file) and runs a
                # best-effort pass — failure is silently ignored.
                if parsed_units:
                    try:
                        from ...indexer.macros import resolve_and_update
                        first_unit = parsed_units[0][0]
                        resolve_and_update(conn, config_hash, first_unit.clang_args, first_unit.file.resolve(), cwd=root)
                    except Exception:  # nosec B110 — macro expansion is best-effort
                        pass

                # Phase 4: clean up orphan files — files that existed in
                # the previous index but are now absent from the TU list.
                from ...indexer.db import delete_orphan_files
                deleted_orphans = delete_orphan_files(conn, config_hash, root)
                if deleted_orphans:
                    log.debug("Orphan files cleaned up: %d", deleted_orphans)

                _update_manifest_after_reindex(parsed_units, root, db_path.parent, config_hash)

                elapsed = round(time.monotonic() - t0, 2)
                result = {
                    "file": str(target),
                    "translation_units": len(matching),
                    "symbols_updated": total_symbols,
                    "elapsed_s": elapsed,
                }
                if skipped_tus:
                    result["skipped_tus"] = skipped_tus
                    result["skipped_count"] = len(skipped_tus)
                if not parsed_units and skipped_tus:
                    return 0, {"error": f"All {len(matching)} translation unit(s) failed to parse", "skipped_tus": skipped_tus}
            return total_symbols, result
        except (sqlite3.Error, WriteLockTimeout) as exc:
            return 0, {"error": f"DB error during reindex: {exc}"}

def _update_manifest_after_reindex(
    parsed_units: list,
    root: Path,
    db_dir: Path,
    config_hash: str,
) -> None:
    """Update manifest.json entries for reindexed translation units.

    The manifest tracks each TU's source hash and its set of included
    headers.  After re-parsing a file, the manifest entry must be
    refreshed so header-staleness detection (used by ``get_active_build``)
    reflects the file's current dependencies.

    When the manifest does not exist (first index, or manifest not
    enabled), this is a no-op — the ``load_manifest`` guard returns
    early.
    """
    try:
        from fw_context_mcp.utils import compute_source_hash

        from ...indexer.manifest import (
            _collect_headers_from_tokens,
            _intern_arguments,
            header_is_trusted,
            mark_entries_behind,
            merge_header_records,
            update_entry,
        )
        from ...indexer.manifest import (
            load as load_manifest,
        )
        from ...indexer.manifest import (
            save as save_manifest,
        )
        manifest_data = load_manifest(db_dir, config_hash)
        if manifest_data is not None:
            # The patterns the INDEX RUN used, read from the manifest it
            # wrote.  With None here _is_generated_header() answers False for
            # every header, so every record this function writes claimed
            # "generated": False.  After the end of vendor trust `generated`
            # is the only trust rule left, so that turned every build of a
            # re-indexed file into a full reparse.  Measured on zbox-ecb-fw-v5
            # variant nrf52840-dev: 27 generated headers dropped to 0.
            bdp = manifest_data.get("build_dir_patterns")
            if not bdp:
                bdp, _ = _reindex_build_patterns_for_generated(root)
            # Snapshot the shared header table BEFORE the loop.  A per-unit
            # difference would make the second unit read what the first one
            # just wrote as somebody else's change.
            table_before = {
                path: record.get("hash", "")
                for path, record in (manifest_data.get("headers") or {}).items()
            }
            reparsed_rel: set[str] = set()
            for unit, _parsed in parsed_units:
                # Records go into a per-unit table and are merged by
                # merge_header_records, so a header this re-parse is the first
                # to see still contributes its hash to the manifest's shared
                # map — without a downgrade of `generated` on the way.
                header_records: dict[str, dict] = {}
                headers = _collect_headers_from_tokens(
                    unit, root, bdp, header_records
                )
                source_hash = compute_source_hash(unit.file.resolve())
                try:
                    tu_rel = str(unit.file.resolve().relative_to(root))
                except ValueError:
                    tu_rel = str(unit.file.resolve())
                reparsed_rel.add(tu_rel)
                for idx, entry in enumerate(manifest_data.get("entries", [])):
                    if entry.get("file") == tu_rel:
                        update_entry(
                            manifest_data, idx, source_hash, headers, header_records
                        )
                        break
                else:
                    # Same merge as update_entry uses.  This branch bypasses
                    # update_entry completely, so a plain .update() here is a
                    # second place where a False overwrites a True.
                    merge_header_records(manifest_data, header_records)
                    manifest_data.setdefault("entries", []).append({
                        "file": tu_rel,
                        "directory": str(unit.directory) if unit.directory else str(root),
                        "arg_set": _intern_arguments(
                            unit.clang_args,
                            manifest_data.setdefault("arg_sets", []),
                        ),
                        "source_hash": source_hash,
                        "headers": headers,
                    })
            # This function refreshed the SHARED header hashes, so the next
            # full index run sees no difference for the OTHER units that
            # include those headers — their rows still come from the old
            # text.  Flag them, or reindex_file hides the change from every
            # later run.
            changed = {
                path
                for path, record in (manifest_data.get("headers") or {}).items()
                if not header_is_trusted(record)
                and record.get("hash", "") != table_before.get(path, "")
            }
            behind = mark_entries_behind(manifest_data, changed, reparsed_rel)
            if behind:
                log.info(
                    "reindex_file: %d entr(ies) flagged needs_reparse after "
                    "%d refreshed header(s)", behind, len(changed),
                )
            save_manifest(manifest_data, db_dir, config_hash)
    except OSError:
        log.debug("manifest.json update skipped during reindex_file", exc_info=True)


def _reindex_llm_analysis(
    conn: sqlite3.Connection,
    config_hash: str,
    cfg: Config,
    db_dir: Path,
    result: dict,
    *,
    project_root: Path,
) -> None:
    """Regenerate LLM symbol analysis for symbols affected by a reindex.

    Only runs when both ``cfg.llm.enabled`` and ``cfg.llm.analyze_symbols``
    are True.  Reads the stored ``analyze_vendor`` flag from the build
    config to determine whether vendor symbols should be re-analysed.

    Uses ``CacheClient`` to avoid redundant LLM calls for symbols whose
    source has not changed.  Failures are non-fatal — the result dict
    gets an ``analysis_warning`` key instead of a hard error.
    """
    if not (cfg.llm.enabled and cfg.llm.analyze_symbols):
        return
    try:
        from ...cache_client import CacheClient
        from ...indexer._llm_analysis import _build_llm_analysis
        try:
            stored_av = bool(
                conn.execute(
                    "SELECT analyze_vendor FROM build_configs WHERE config_hash = ?",
                    (config_hash,),
                ).fetchone()["analyze_vendor"]
            )
        except sqlite3.OperationalError:
            stored_av = False
        cc = CacheClient.from_config(cfg)
        try:
            _build_llm_analysis(
                conn, config_hash, cfg.llm, db_dir,
                write_lock_held=False, retry_unparseable=True,
                cache_client=cc, project_only=not stored_av,
                project_root=project_root,
            )
            conn.commit()
        finally:
            if cc:
                cc.close()
        analyzed_count = conn.execute(
            "SELECT COUNT(*) FROM llm_analysis a JOIN symbols s ON s.id = a.symbol_id WHERE s.config_hash = ?",
            (config_hash,),
        ).fetchone()[0]
        result["analysis_updated"] = analyzed_count
    except (sqlite3.Error, RuntimeError, OSError) as exc:
        result["analysis_warning"] = f"LLM analysis skipped: {exc}"


def _reindex_overrides(
    conn: sqlite3.Connection,
    config_hash: str,
    cfg: Config,
    db_dir: Path,
    result: dict,
) -> None:
    """Rebuild virtual method override relationships after reindex.

    Only runs when ``cfg.index.index_refs`` is True (reference indexing
    must be enabled).  The override table maps virtual methods to their
    overrides in derived classes and is used by ``get_method_overrides``.

    Failures are non-fatal — an ``overrides_warning`` key is added to
    the result dict.
    """
    if not cfg.index.index_refs:
        return
    try:
        from ...indexer.runner import _build_overrides
        _build_overrides(conn, config_hash, db_dir, write_lock_held=False, force=True)
        conn.commit()
    except (sqlite3.Error, RuntimeError) as exc:
        result["overrides_warning"] = f"Override analysis skipped: {exc}"


def _reindex_pagerank(
    conn: sqlite3.Connection,
    config_hash: str,
    cfg: Config,
    result: dict,
) -> None:
    """Rebuild PageRank scores and the hotspot cache after reindex.

    PageRank runs on the call graph to rank functions by architectural
    importance.  The hotspot cache pre-computes the top-N most-called
    functions for ``find_hotspots``.  Both depend on the reference index
    (``cfg.index.index_refs`` must be True).

    Failures are non-fatal.
    """
    if not cfg.index.index_refs:
        return
    try:
        from ...indexer.runner import _build_hotspot_cache, _build_pagerank
        _build_pagerank(conn, config_hash, write_lock_held=False, force=True)
        _build_hotspot_cache(conn, config_hash, force=True)
        conn.commit()
    except (sqlite3.Error, RuntimeError) as exc:
        result["pagerank_warning"] = f"PageRank/hotspot recompute skipped: {exc}"


def _reindex_embeddings(
    conn: sqlite3.Connection,
    config_hash: str,
    cfg: Config,
    db_dir: Path,
    target: Path,
    root: Path,
    result: dict,
) -> None:
    """Regenerate vector embeddings for symbols in the reindexed file.

    Only runs when ``cfg.llm.enabled`` is True (the embedding model is
    configured through the LLM subsystem).  Queries for all symbol IDs
    belonging to the file path, then calls ``_build_embeddings`` with
    the filtered ID set to avoid recomputing embeddings for the entire
    index.

    When no symbols are found for the file path (possible for symlink
    or out-of-root files), the warning is logged but no error is raised.
    """
    if not cfg.llm.enabled:
        return
    from ...indexer.ops import _normalize_file_path
    try:
        from ...indexer.runner import _build_embeddings as _reembed
        file_symbol_ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM symbols WHERE config_hash = ? AND file_path = ?",
                (config_hash, _normalize_file_path(str(target), root)),
            ).fetchall()
        ]
        if file_symbol_ids:
            _reembed(conn, config_hash, cfg.llm, db_dir, symbol_ids=file_symbol_ids)
        else:
            log.warning(
                "No symbols found for path %s in DB — skipping embedding regeneration "
                "(this may happen for files outside the project root or after a symlink change)",
                target,
            )
        conn.commit()
        reembedded = conn.execute(
            "SELECT COUNT(DISTINCT symbol_id) FROM embeddings e "
            "JOIN symbols s ON s.id = e.symbol_id WHERE s.config_hash = ?",
            (config_hash,),
        ).fetchone()[0]
        result["embeddings_updated"] = reembedded
    except (sqlite3.Error, RuntimeError, OSError) as exc:
        result["embedding_warning"] = f"Embedding regeneration skipped: {exc}"


def _reindex_post_write_phases(
    conn: sqlite3.Connection,
    config_hash: str,
    cfg: Config,
    total_symbols: int,
    db_dir: Path,
    target: Path,
    result: dict,
    root: Path,
) -> dict:
    """Run post-write enrichment phases after reindex_file.

    Each phase is independent — failure in one does not prevent others
    from running.  The phases are:

    1. LLM symbol analysis (function descriptions)
    2. Virtual method override relationships
    3. PageRank scores and hotspot cache
    4. Vector embeddings for semantic search

    When ``total_symbols <= 0``, all phases are skipped (nothing was
    added or updated).

    The coverage warning for a header is NOT set here.  It used to be, and
    that put it behind two gates it has nothing to do with: this function
    returns early when ``total_symbols <= 0``, and the caller skips it
    entirely when ``with_analysis=False`` — the mode the background
    auto-reindex uses.  A caller that re-parsed a header through one unit
    has to hear about it whether or not the enrichment phases ran, thus
    _header_coverage_warning() is applied by the caller.
    """
    if total_symbols <= 0:
        return result

    _reindex_llm_analysis(conn, config_hash, cfg, db_dir, result, project_root=root)
    _reindex_overrides(conn, config_hash, cfg, db_dir, result)
    _reindex_pagerank(conn, config_hash, cfg, result)

    _reindex_embeddings(conn, config_hash, cfg, db_dir, target, root, result)
    return result


def _header_coverage_warning(
    target: Path, matching: list, via_header: bool
) -> str | None:
    """Say that a header was re-parsed through one compilation context.

    Another unit can include the same header under a different set of
    ``#define`` values and still hold symbols from before the edit.  Only a
    full index covers every context, and the caller has to know that the
    answer is partial.

    *via_header* covers the manifest fallback.  The suffix test covers a
    header that compile_commands.json does list, which some build systems
    emit.  The suffix test alone is not enough: it names ``.h`` and
    ``.hpp``, while an ``#include`` reaches ``.hh``, ``.hxx``, ``.inc`` and
    extension-less headers just as well, and the fallback resolves those
    too.
    """
    if not (via_header or (len(matching) == 1 and target.suffix.lower() in {".h", ".hpp"})):
        return None
    return (
        "Header re-indexed via one TU. Other TUs including this header "
        "may still have stale symbols — run 'fw-context index' for full accuracy."
    )


# ── moved from server.py ──
def reindex_file_impl(
    file_path: Annotated[
        str,
        Field(
            description="Absolute or project-relative path to the file to re-parse. A source file must have an entry in compile_commands.json. A header is re-parsed through one translation unit that includes it, and the result then carries a warning that other units can still hold stale symbols."
        ),
    ],
    project_root: Annotated[
        str | None, Field(description="Project root directory. Auto-detected from cwd if omitted.")
    ] = None,
    *,
    with_analysis: Annotated[
        bool,
        Field(
            description="When True (default), also regenerates LLM symbol analysis and method override relationships — slower but produces a fully up-to-date index. Set False for a fast symbol-only update (used by background auto-reindex)."
        ),
    ] = True,
) -> dict:
    """Re-parse a single source file with libclang and update its symbols in the index.

    Not read-only — uses the exact compiler flags from ``compile_commands.json``.
    Use after editing a file to keep the index current without a full rebuild.

    A source file must be listed in ``compile_commands.json``.  A header is
    not listed there — compile_commands.json names translation units — so it
    is re-parsed through one unit that includes it, taken from the manifest.
    That answer describes a single compilation context, thus the result
    carries a ``warning``: another unit can see the header under a different
    set of ``#define`` values and still hold stale symbols.  Only a full
    ``fw-context index`` covers every context.  One unit and not all of them
    is a cost decision — an application header reaches a median of 3 units
    but as many as 266 on a real project, at tens of seconds each.

    Also regenerates LLM analysis and method override relationships for
    affected symbols when ``with_analysis=True``.  The analysis is
    content-addressed, thus an unchanged symbol is never re-analysed.

    Args:
        file_path: Path to the file to re-parse.  A source file must be in
            compile_commands.json; a header goes through one including unit.
        project_root: Project root directory. Auto-detected if omitted.
        with_analysis: When True (default), also regenerates LLM symbol analysis,
            method override relationships, PageRank, and embeddings. Set False
            for a fast symbol-only update (used by background auto-reindex).

    Returns:
        dict: {file, translation_units, symbols_updated, elapsed_s,
        analysis_updated (if LLM enabled with analysis), or error}.

        On failure the dict holds only ``error`` with the reason.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root, skip_ready_check=True)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}

    # Config read via a short-lived read-only connection.
    ro_conn = _quick_open_readonly(db_path)
    try:
        cfg_data = get_active_config(ro_conn, project_id)
        if not cfg_data:
            return {"error": "No build config indexed."}
        config_hash = cfg_data["config_hash"]
    finally:
        ro_conn.close()

    executor = get_executor(db_path)

    def _query(conn, config_hash):
        # WRITE path: the executor is not read-only — writes are
        # serialized with reads by the same lock.  A collision with a
        # concurrently running CLI indexer is absorbed by the executor's
        # 120 s busy_timeout (the CLI indexer keeps its own 10 s
        # fail-fast).  Timeout is enforced by _wrap_tool, not here.
        target, error = _reindex_resolve_target(file_path, root)
        if error:
            return error
        if not target.exists():
            return _reindex_cleanup_deleted_file(conn, cfg_data, target, root, db_path)

        # The manifest of THIS build carries the vendor set the index run
        # applied.  reindex_file must write is_project with that same
        # boundary, or one file ends up on the other side of it.
        #
        # Loaded before the match because it answers a second question:
        # compile_commands.json lists no headers, so a header target finds
        # its translation unit only through the manifest.  One load serves
        # both — it costs 14 ms on a 9.3 MB manifest.
        from ...indexer.manifest import load as load_manifest

        build_manifest = load_manifest(db_path.parent, config_hash)

        matching, via_header, error = _reindex_match_tus(
            target, cfg_data, manifest=build_manifest, project_root=root,
        )
        if error:
            return error

        vendor_patterns, project_patterns_list = _reindex_build_patterns(
            cfg, root, build_manifest
        )
        total_symbols, result = _reindex_parse_and_store(
            conn, matching, cfg_data, cfg.index.index_refs,
            root, db_path, target, vendor_patterns, project_patterns_list,
        )
        if "error" in result:
            return result

        # Set before the with_analysis gate: the warning describes how much
        # of the header the re-parse covered, which does not depend on
        # whether the enrichment phases run.
        coverage_warning = _header_coverage_warning(target, matching, via_header)
        if coverage_warning:
            result["warning"] = coverage_warning

        if not with_analysis:
            return result

        return _reindex_post_write_phases(
            conn, config_hash, cfg, total_symbols, db_path.parent,
            target, result, root,
        )

    try:
        return executor.execute_sync(_query, config_hash)
    finally:
        # Best-effort statistics refresh after the write.  Runs through
        # the executor so it serializes with other queries.
        def _optimize(conn, _ch):
            conn.execute("PRAGMA optimize")

        try:
            executor.execute_sync(_optimize, config_hash)
        except sqlite3.Error:
            pass


def _reindex_resolve_target(
    file_path: str, root: Path
) -> tuple[Path, dict | None]:
    """Resolve *file_path* to an absolute path relative to *root*.

    When the path is relative, it is joined with *root* and resolved.
    When absolute, it is resolved directly.  Resolution follows symlinks
    so the path matches the canonical form in compile_commands.json.

    Returns ``(target, None)`` on success or ``(target, error_dict)``
    when the path cannot be resolved.  The error_dict arm is reserved
    for future path-safety checks; currently this function never fails.
    """
    target = Path(file_path)
    if not target.is_absolute():
        target = (root / target).resolve()
    else:
        target = target.resolve()
    return target, None


def _tu_for_header(
    target: Path, manifest: dict | None, project_root: Path
) -> Path | None:
    """Return one translation unit that includes *target*, or None.

    A header is never its own entry in compile_commands.json, thus the
    direct match cannot find it.  The manifest records each TU with the
    header paths it pulled in, which answers the reverse question.

    ONE unit, not all of them.  The fan-out is large: measured on
    zbox-ecb-fw, an application header reaches a median of 3 translation
    units but ``sdk_config.h`` reaches 266, and one re-parse costs about
    42 s on that project.  Re-parsing every including unit would turn a
    single tool call into hours, thus the caller warns that the answer
    comes from one compilation context and points at a full index.

    The choice is the lowest path in sort order, not the first entry.
    Manifest order follows compile_commands.json, which a rebuild can
    reshuffle, and a tool that picked a different unit on every call
    would be hard to reason about.

    Paths are compared as strings.  The manifest already stores resolved
    forms — relative to the project root when inside it, absolute
    otherwise (see ops.py) — and *target* arrives resolved from
    _reindex_resolve_target, thus another resolve() per header would only
    add a syscall for each of the tens of thousands of header references
    a large manifest holds.
    """
    if not manifest:
        return None
    root = project_root.resolve()
    target_str = str(target)
    best: str | None = None
    for entry in manifest.get("entries") or ():
        tu_file = entry.get("file")
        if not tu_file:
            continue
        for header in entry.get("headers") or ():
            header_abs = header if Path(header).is_absolute() else str(root / header)
            if header_abs != target_str:
                continue
            tu_abs = tu_file if Path(tu_file).is_absolute() else str(root / tu_file)
            if best is None or tu_abs < best:
                best = tu_abs
            break
    return Path(best) if best is not None else None


def _reindex_match_tus(
    target: Path,
    cfg_data: sqlite3.Row,
    *,
    manifest: dict | None = None,
    project_root: Path | None = None,
) -> tuple[list, bool, dict | None]:
    """Find compilation units in compile_commands.json that build *target*.

    Parses the compile_commands.json file and matches by absolute,
    resolved file path.  A single source file may appear in more than one
    unit — the same .cpp built with two flag sets — and every match is
    returned so symbols from each context are updated.

    A header matches nothing this way, because compile_commands.json
    lists translation units and a header is not one.  For that case
    *manifest* supplies one unit that includes the header; see
    _tu_for_header for why one and which one.

    Returns ``(matching_tus, via_header, None)`` on success, where
    *via_header* says the units came from the manifest fallback and the
    result therefore describes a single compilation context.  On failure
    it returns an empty list with an ``error_dict`` — the
    compile_commands.json is missing, or nothing builds the target.
    """
    cc_path = Path(cfg_data["compile_commands_path"])
    if not cc_path.exists():
        return [], False, {"error": f"compile_commands.json not found: {cc_path}"}
    # list(), not the iterator parse_cc returns: the header fallback below
    # walks the units a second time, and a generator is empty by then.
    units = list(parse_cc(cc_path))
    matching = [u for u in units if Path(u.file).resolve() == target]
    if matching:
        return matching, False, None

    tu_path = _tu_for_header(target, manifest, project_root or target.parent)
    if tu_path is not None:
        # The manifest can name a unit that a later build dropped from
        # compile_commands.json.  An empty result here falls through to
        # the error below, which is the same answer as before.
        via = [u for u in units if Path(u.file).resolve() == tu_path]
        if via:
            return via[:1], True, None

    return [], False, {"error": f"{target.name} not found in compile_commands.json — it may be a header-only file."}


def _effective_vendor_patterns(
    db_dir: Path, config_hash: str, root: Path
) -> list[str]:
    """Return the vendor set stored with THIS build, or [] when unknown.

    Read-only and best-effort: this feeds a status report, so a manifest that
    cannot be read must not make the report fail.
    """
    from ...indexer.manifest import load as load_manifest

    try:
        manifest = load_manifest(db_dir, config_hash)
    except OSError:
        log.debug("manifest unreadable for the status report", exc_info=True)
        return []
    if manifest is None:
        return []
    return list(manifest.get("vendor_patterns") or [])


def _reindex_build_patterns_for_generated(root: Path) -> tuple[list[str], None]:
    """Return build-output patterns for a manifest that stores none.

    Only for an index written before the manifest carried the key.  The
    manifest is the better source: it holds the value the index run applied,
    while this one re-detects and can answer differently after the build
    system changes.
    """
    from ...indexer.build import detect_build_system
    from ...indexer.builders import registry

    system = detect_build_system(root)
    builder_cls = registry.get(system) if system else None
    if builder_cls is None:
        return [], None
    return list(builder_cls().get_build_dir_patterns(root)), None


def _reindex_build_patterns(
    cfg, root: Path, manifest: dict | None = None
) -> tuple[list[str], list[str]]:
    """Collect vendor-exclude and project-include file path patterns.

    Returns two lists of SQL LIKE patterns:

    *vendor_patterns* — paths that the SDK detector and the user config
    mark as vendor/SDK code.  These patterns exclude symbols from the
    ``is_project`` flag and from ``project_only`` queries.

    *project_patterns_list* — paths explicitly listed as project code
    in the user config.  These override the auto-detected vendor
    boundary when the detector misclassifies a path.

    Pass *manifest* whenever the caller has it.  The set stored there is the
    one the index run applied, and to derive a second one here would give
    this build two different boundaries.
    """
    from ...indexer.sdk_detect import _normalize_patterns, vendor_patterns_for_build

    vendor_patterns = vendor_patterns_for_build(
        manifest, root, build_system=cfg.build.system
    )
    if cfg.index.vendor_paths:
        vendor_patterns.extend(_normalize_patterns(cfg.index.vendor_paths))
    project_patterns_list = (
        _normalize_patterns(list(cfg.index.project_paths))
        if cfg.index.project_paths
        else []
    )
    return vendor_patterns, project_patterns_list


# ── moved from server.py ──
def reindex_file(
    file_path: Annotated[str, Field(description="Path to the file to re-parse. A source file must be in compile_commands.json. A header goes through one translation unit that includes it, and the result then carries a warning about the other units.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
) -> dict:
    """Re-parse a single source file with libclang and update its symbols in the index.

    Not read-only — uses the exact compiler flags from ``compile_commands.json``.
    Use after editing a file to keep the index current without a full rebuild.

    A source file must be listed in ``compile_commands.json``.  A header is
    not listed there, thus it is re-parsed through one unit that includes
    it.  That answer covers a single compilation context, thus the result
    carries a ``warning`` — only a full ``fw-context index`` covers every
    unit that includes the header.

    Also regenerates LLM analysis and method override relationships for
    affected symbols when those features are enabled in config.  An
    unchanged symbol keeps its stored analysis.

    Args:
        file_path: Path to the file to re-parse.  A source file must be in
            compile_commands.json; a header goes through one including unit.
        project_root: Project root directory. Auto-detected if omitted.

    Returns:
        dict: {file, translation_units, symbols_updated, elapsed_s,
        analysis_updated (if LLM enabled), or error}.
    """
    return reindex_file_impl(file_path, project_root, with_analysis=True)


# ── moved from server.py ──
def check_ollama(
    project_root: Annotated[
        str | None, Field(description="Project root. Auto-detected if omitted. "
        "Used to locate LLM config. Falls back to auto-detection when omitted.")
    ] = None,
) -> dict:
    """Check whether the LLM backend is running and the configured embedding/chat model is installed.

    Read-only: yes. No side effects. Call before smart_search,
    semantic_search, or explain_symbol (when on-demand fallback is
    expected — pre-computed analysis returns instantly without the LLM
    backend).

    Args:
        project_root: Project root. Auto-detected if omitted. Used to
            locate the project's LLM configuration.

    Returns:
        dict: {ollama_enabled (bool), status (str — "ok"|"disabled"|
        "not_configured"|"model_missing"|"embedding_unavailable"|"error"),
        ollama_running (bool), ollama_url (str), configured_model (str),
        num_ctx (int), installed_models (list[str]),
        configured_embed_model (str), embedding_installed (bool),
        message (str, on error/disabled), model_details (list[dict], when
        Ollama running), suggest_cloud (bool), vec_available (bool),
        vec_error (str, optional), debug_log (str, optional — only when
        debug logging is enabled)}
    """
    try:
        _, cfg, _, _ = _resolve_context(project_root)
    except RuntimeError:
        # Project has no index — irrelevant for check_ollama (only needs
        # LLM config).  Fall back to loading config directly without
        # requiring an active index.
        from pathlib import Path

        from fw_context_mcp.config.settings import load

        cfg = load(Path(project_root) if project_root else None)
    if not cfg.llm.enabled:
        # LLM is explicitly disabled in config.  Return immediately
        # with a message that explains the implications for tools that
        # depend on the LLM backend.
        return {
            "status": "disabled",
            "ollama_enabled": False,
            "ollama_running": False,
            "configured_model": cfg.llm.model,
            "num_ctx": cfg.llm.num_ctx,
            "message": (
                "LLM backend is disabled in config ([llm] enabled = false). "
                "explain_symbol with pre-computed analysis (default) returns instantly. "
                "Without analysis, it returns source + explain_prompt for the agent to answer. "
                "smart_search will use raw text queries."
            ),
        }
    # Delegates to check_setup which probes the Ollama server / chat API,
    # lists installed models, and determines the status string.
    result = check_setup(cfg.llm)
    result["ollama_enabled"] = True

    # sqlite-vec is needed for semantic_search embeddings.
    # Report availability so tools can degrade gracefully.
    from ...deps import _vec_available
    vec_ok, vec_err = _vec_available()
    result["vec_available"] = vec_ok
    if vec_err is not None:
        result["vec_error"] = vec_err

    return result


def _dep_to_schema(r) -> dict:
    """Map one ``DepCheckResult`` to the ``{name, status, message, action}`` schema.

    The optional ``action`` carries a human-readable ``message`` (what is
    wrong or what to do) and an exact shell ``command`` (``fix_cmd``).
    When ``instructions`` merely repeats ``fix_cmd`` (the common pip/apt
    one-liner case), the status ``message`` is used as the human part so
    ``action.message`` is not a bare duplicate of ``action.command``.
    ``status="skipped"`` passes through unchanged — it signals a missing
    prerequisite, not a failure.
    """
    d: dict = {"name": r.name, "status": r.status, "message": r.message}
    if r.fix_cmd or r.instructions:
        human = r.instructions or r.message
        if r.fix_cmd and human.strip() == r.fix_cmd.strip():
            human = r.message
        d["action"] = {"message": human, "command": r.fix_cmd}
    return d


def _llm_to_schema(result: dict) -> dict:
    """Map ``check_ollama()`` output to the documented ``llm`` sub-schema.

    ``check_ollama`` returns ``ollama_enabled``/``configured_model``/
    ``configured_embed_model``/``embedding_installed`` — not the ``llm``
    sub-schema.  This adapter maps those keys.  The disabled path returns
    only a subset of keys, so every read uses ``.get(key, default)``.
    """
    llm: dict = {
        "enabled": bool(result.get("ollama_enabled")),
        "ollama_running": bool(result.get("ollama_running")),
        "chat_model": result.get("configured_model"),
        "embed_model": result.get("configured_embed_model"),
    }
    status = result.get("status")
    action: dict | None = None
    if status == "model_missing":
        model = (
            result.get("configured_embed_model")
            if not result.get("embedding_installed")
            else result.get("configured_model")
        )
        action = {"message": result.get("message", ""), "command": f"ollama pull {model}" if model else None}
    elif status in ("not_configured", "disabled"):
        action = {"message": result.get("message", ""), "command": "fw-context init"}
    elif status in ("embedding_unavailable", "error"):
        action = {"message": result.get("message", "")}
    if action is not None:
        llm["action"] = action
    return llm


def _cc_entry_count(cc: Path) -> int | None:
    """Count entries in a compile_commands.json (None on parse error).

    ``None`` signals "count unknown" to the LLM — a corrupt file must not
    crash this read-only status tool.  ``0`` is reserved for an
    empty-but-valid compile DB, so a parse failure is kept distinct.
    """
    import json

    try:
        return len(json.loads(cc.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return None


def get_environment_status(
    project_root: Annotated[
        str | None,
        Field(description="Project root. Auto-detected if omitted. Pass explicitly when the project is not the server cwd."),
    ] = None,
) -> dict:
    """Return the complete project environment status in one call.

    Read-only. Aggregates five domains into a single call so the LLM can
    see everything at session start without extra round-trips:

    - ``deps`` — dependency audit (``run_full_check``), each entry with an
      optional ``action`` (``message`` + shell ``command``).  ``status="skipped"``
      means a prerequisite is missing (e.g. ``libclang-so`` skipped because
      ``libclang-python`` is absent) — not a failure.
    - ``build_system`` — detected build system, ``None`` when unknown.
    - ``compile_db`` — whether compile_commands.json exists and its entry count.
      Reported as ``{"exists": false, ...}`` before init (no config to resolve
      the path from, and loading one would create empty config files).
    - ``index`` — the FULL ``get_active_build()`` result, unchanged (its action
      lives in ``index_message``).
    - ``llm`` — LLM backend status with an optional ``action``.

    When the project is not initialized (``index.status == "not_initialized"``),
    only the config-independent dependency subset runs (checks that do not need
    a project config) — Ollama/model/db/build checks are skipped.

    Args:
        project_root: Project root directory. Auto-detected from CWD if omitted.

    Returns:
        dict: {init_status (str — "initialized" or "not_initialized"),
        deps (list[dict] — name, status, message, and an optional action),
        build_system (str or None),
        compile_db (dict — {exists (bool), path (str or None),
        entry_count (int or None — None before init, and when fw-context
        cannot read the file)}),
        index (dict — the full ``get_active_build`` result),
        llm (dict — {enabled, ollama_running, chat_model, embed_model}, plus
        ``ollama_enabled`` when the LLM check ran, plus an optional
        ``action``)}.
    """
    from ...deps import run_full_check

    root = resolve_project_root(project_root)

    index = get_active_build(project_root=str(root))
    init_status = "initialized" if index.get("status") != "not_initialized" else "not_initialized"

    if index.get("status") == "not_initialized":
        # libclang-so is project-config-independent (only needs libclang-python),
        # so it belongs in the pre-init subset — the "missing libclang .so →
        # apt install libclang-18-dev" signal must work before the first init.
        pip_subset = {"pysqlite3", "sqlite-vec", "libclang-python", "libclang-so", "watchfiles", "tomli-w"}
        dep_results = run_full_check(project_root=str(root), subset=pip_subset)
        # No [index] compile_commands path exists pre-init, and load_config()
        # would create empty .fw-context/config.toml + local.toml.  Skip it so
        # this read-only tool has no file-creation side effect before init.
        compile_db: dict = {"exists": False, "path": None, "entry_count": None}
    else:
        dep_results = run_full_check(project_root=str(root))
        cfg = load_config(root)
        from ...indexer.build import resolve_reuse_compile_commands

        cc = resolve_reuse_compile_commands(root, cfg.index.compile_commands)
        compile_db = {"exists": cc.exists(), "path": str(cc) if cc.exists() else None, "entry_count": None}
        if cc.exists():
            compile_db["entry_count"] = _cc_entry_count(cc)
    deps = [_dep_to_schema(r) for r in dep_results]

    build_system_raw = index.get("build_system") or _detect_build_system(root)
    build_system = None if build_system_raw == "unknown" else build_system_raw

    if index.get("status") == "not_initialized":
        # No config exists yet — probe the LLM backend from defaults so this
        # read-only tool does NOT create .fw-context config files.  check_ollama
        # routes through load() (via _resolve_context), which has that
        # file-creation side effect.  Resolve the embed model first so the
        # report mirrors the initialized path (concrete embed_model, no pull).
        from ...config.settings import LLMConfig
        from ...llm.auto_model import resolve_embed_model

        default_cfg = LLMConfig()
        resolve_embed_model(default_cfg, skip_pull=True)
        llm_result = check_setup(default_cfg)
        llm_result["ollama_enabled"] = True
        llm = _llm_to_schema(llm_result)
    else:
        llm = _llm_to_schema(check_ollama(project_root=str(root)))

    return {
        "init_status": init_status,
        "deps": deps,
        "build_system": build_system,
        "compile_db": compile_db,
        "index": index,
        "llm": llm,
    }


def check_dependencies(
    project_root: Annotated[
        str | None,
        Field(description="Project root. Auto-detected if omitted. Pass explicitly when the project is not the server cwd."),
    ] = None,
) -> list[dict]:
    """Run the full dependency audit. Read-only. Returns structured results.

    Returns the raw per-check dicts (``name``, ``status``, ``message``,
    ``fix_cmd``, ``instructions``, ``critical``) — NOT the formatted
    ``doctor`` table.  Read ``status``/``fix_cmd``/``instructions`` per
    issue; ``status="skipped"`` means a prerequisite is missing.

    Args:
        project_root: Project root directory. Auto-detected from CWD if omitted.

    Returns:
        list[dict]: one dict per check, ``DepCheckResult`` fields via ``asdict``.
    """
    from dataclasses import asdict

    from ...deps import run_full_check

    return [asdict(r) for r in run_full_check(project_root=project_root)]


def get_project_info(
    project_id: Annotated[str, Field(description="Project ID (UUID4 hex) to look up.")] = "",
) -> dict:
    """Return project metadata (name, type, root_path) for a project ID.

    Looks up the global project registry at ``~/.fw-context/projects.db``.
    Use this to identify a project from its UUID4 — find out what build
    system it uses, its name, and where it was last indexed.

    Read-only. No side effects.

    Args:
        project_id: Project ID (UUID4 hex) to look up.

    Returns:
        dict: {project_id, name, project_type, root_path, created_at, updated_at}
        or {"error": "..."} when the project_id is not registered.

        On failure the dict holds only ``error`` with the reason.
    """
    from ...config.global_db import get_project_by_id

    if not project_id:
        # Empty string is the default — the LLM must be told that a
        # project_id is required before this tool can return anything.
        return {"error": "project_id is required — provide a UUID4 hex string from fw-context init."}
    result = get_project_by_id(project_id)
    if result is None:
        # The UUID was not found in the global registry (~/.fw-context/projects.db).
        # This can happen when the project was deleted or the registry was reset.
        return {"error": f"Project '{project_id}' not found in the global registry."}
    return result


def configure_llm(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    chat_api_base: Annotated[
        str | None,
        Field(
            description=(
                "Chat API URL. None = use local Ollama for chat. "
                "Auto-detects format: :11434 or /api/generate -> Ollama, "
                "/v1 or bare host -> OpenAI-compatible. "
                "Examples: 'https://api.deepseek.com/v1' (DeepSeek), "
                "'http://localhost:4000' (LiteLLM), "
                "'http://localhost:8080/v1' (llama.cpp). "
                "WARNING: external URLs send source code to that host."
            )
        ),
    ] = None,
    chat_api_key: Annotated[
        str | None,
        Field(description="API key for cloud/proxy APIs. None for local/no-auth."),
    ] = None,
    chat_api_format: Annotated[
        str,
        Field(description="Format override: 'auto' (default), 'ollama', or 'openai'."),
    ] = "auto",
    model: Annotated[
        str | None,
        Field(description="Chat model name. None = keep current."),
    ] = None,
    embed_model: Annotated[
        str | None,
        Field(description="Embedding model name (Ollama only). None = keep current."),
    ] = None,
    auto_pull: Annotated[
        bool,
        Field(description="Auto-pull models on 404 (Ollama only). False for intranet."),
    ] = False,
    stream: Annotated[
        bool | None,
        Field(
            description=(
                "Stream chat responses via SSE (both OpenAI-compatible and Ollama-native). "
                "True = send stream:true, consume SSE chunks — avoids reverse-proxy idle "
                "timeouts (nginx 60s, Cloudflare 100s). None = keep current setting."
            )
        ),
    ] = None,
) -> dict:
    """Configure LLM settings for the current project.

    Writes to ``<project>/.fw-context/local.toml`` ONLY (gitignored,
    per-developer). Does NOT modify the global config or the shared
    project ``config.toml``. After writing, tests the configuration
    by making a simple API call (skipped when LLM is disabled).

    IMPORTANT: When ``chat_api_base`` points to an external host, source
    code snippets in chat prompts will be sent to that endpoint. Ensure
    this complies with your organization's data security policies.
    Consider using local Ollama or an internal API proxy first.

    Args:
        project_root: Project root directory. Auto-detected if omitted.
        chat_api_base: Chat API URL (see description for format details).
        chat_api_key: Bearer token for cloud/proxy APIs.
        chat_api_format: Override auto-detection: "auto", "ollama", "openai".
        model: Chat model name.
        embed_model: Embedding model name (Ollama only).
        auto_pull: Whether to auto-pull models on 404.
        stream: Stream chat responses via SSE. True avoids reverse-proxy idle timeouts.

    Returns:
        dict: {status ("ok"|"error"), chat_api (dict — configured, endpoint,
        format, model), model (str), auto_pull (bool), stream (bool),
        test_latency_s (float, on success), test_response (str, on success),
        compliance_warning (str, when chat_api_base is external),
        message (str)}
    """
    from ...config.settings import ProjectNotInitializedError

    try:
        _, cfg, _, root = _resolve_context(project_root, skip_ready_check=True)
    except ProjectNotInitializedError as e:
        # configure_llm writes local.toml — the project must be initialized
        # so the config directory (.fw-context/) exists.
        return {
            "status": "error",
            "message": (
                f"Project is not initialized. Run 'fw-context init' in the "
                f"project root first. ({e.root})"
            ),
        }

    # Collect only non-default/non-None values to avoid overwriting
    # existing settings with defaults.  The auto_pull comparison uses
    # the current config value (not a constant) to distinguish "user
    # explicitly set False" from "user passed the default arg False".
    updates: dict[str, object] = {}
    if chat_api_base is not None:
        updates["chat_api_base"] = chat_api_base
    if chat_api_key is not None:
        updates["chat_api_key"] = chat_api_key
    if chat_api_format != "auto":
        updates["chat_api_format"] = chat_api_format
    if model is not None:
        updates["model"] = model
    if embed_model is not None:
        updates["embed_model"] = embed_model
    if auto_pull != cfg.llm.auto_pull:
        updates["auto_pull"] = auto_pull
    if stream is not None:
        updates["stream"] = stream

    if not updates:
        return {
            "status": "error",
            "message": "No configuration changes to write — all parameters are None or default.",
        }

    # Delegate to the shared write+test implementation (llm/configure.py).
    # It writes only local.toml — never global or committed config —
    # reloads the config, and tests the endpoint.
    from ...llm.configure import configure_llm_core

    return configure_llm_core(Path(root), updates, chat_api_base=chat_api_base)
