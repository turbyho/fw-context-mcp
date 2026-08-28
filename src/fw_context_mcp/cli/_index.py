"""``fw-context index`` — build or rebuild the libclang symbol index.

This command parses ``compile_commands.json`` with libclang and stores
function/class/enum metadata, cross-references, embeddings, and optional
LLM analysis in an SQLite database.

Indexing phases run in order:
1. **Build** (if ``--build`` or missing cc.json) — generate
   compile_commands.json from the detected build system.
2. **Validate** — check cc.json completeness, detect stale entries,
   fix path separators and other common issues.
3. **Parse** — libclang extraction of symbols from every translation unit.
4. **Refs** — cross-reference indexing: callers, callees, indirect edges.
5. **Embeddings** — generate vector embeddings for semantic search.
6. **Analyze** (optional) — LLM-generated summaries for each symbol.

WHY: The index is the core data structure of fw-context.  Without a
complete and accurate index, every search, call-graph query, and
semantic search returns wrong or empty results.  This command must
handle incremental updates, stale detection, background conflict
resolution, and graceful degradation when optional phases fail.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..indexer import autobuild
from ..mcp.shared.pid_file import PidFile
from ..utils import CC_OUTPUT_REL, autobuild_dir
from . import VerboseFormatter

if TYPE_CHECKING:
    from ..indexer.build import BuildConfig

log = logging.getLogger(__name__)


# ── Automatic build for a source file the build system never saw ──
# A new .c file has no translation unit: it is absent from
# compile_commands.json, and only a build puts it there.  A plain reindex
# skips it without a word, thus fw-context runs the build itself.
#
# The markers and the state machine live in indexer/autobuild.py, because
# mcp/handlers/maintenance.py needs the same answers to tell the caller what
# will happen, and it cannot import from cli/.


def _plan_auto_build(
    project_root: Path,
    db_path: Path,
    cfg,
    detected_system: str | None,
) -> tuple[list[str], BuildConfig | None]:
    """Return ``(sources that need a build, the config to build them with)``.

    An empty list means "do not build", and the config is then None.  The
    list is non-empty only when all of these hold:

    1. An index exists.  Without one there is nothing to compare against.
    2. Source files sit on disk that compile_commands.json does not cover.
    3. The backend may build in the background — it isolates its output, or
       it compiles nothing.  See ``builders.background_build_safe``.
    4. No recent automatic build failed for the same files.

    Condition 3 is the important one.  The build runs while the user works,
    possibly while an IDE builds the same project, and fw-context cannot
    lock the build of the IDE.

    WHY the config comes back instead of being set on *cfg*: the contract of
    ``background_build_safe`` says the answer MAY depend on
    ``cfg.isolated_build_dir`` (protocol.py), thus the question has to be
    asked with the value the build would really use.  Setting it on *cfg*
    before asking would leave it behind on every path that then answers "do
    not build", so the candidate is built with ``replace()`` and the caller
    applies it only when there is something to do.
    """
    if not db_path.exists():
        return [], None

    from dataclasses import replace

    from ..indexer.builders import background_build_safe, registry
    from ..mcp.shared.stale import find_unindexed_sources

    system = cfg.build.system or detected_system
    builder_cls = registry.get(system) if system else None
    if builder_cls is None:
        return [], None
    candidate = replace(cfg.build, isolated_build_dir=autobuild_dir())
    if not background_build_safe(builder_cls(), candidate):
        return [], None

    from ..config import derive_project_id
    from ..indexer.db import get_active_config, open_db

    conn = open_db(db_path)
    try:
        active = get_active_config(conn, derive_project_id(project_root))
        if not active or not active["compile_commands_path"]:
            return [], None
        new_sources = find_unindexed_sources(
            conn,
            active["config_hash"],
            project_root,
            Path(active["compile_commands_path"]),
        )
    finally:
        conn.close()

    if not new_sources or autobuild.blocked(db_path.parent, new_sources):
        return [], None
    return new_sources, candidate


def _resolve_compile_commands(
    args: argparse.Namespace,
    project_root: Path,
    cfg,
    detected_system: str | None,
    bg: bool,
) -> tuple[Path | None, bool]:
    """Resolve the compile_commands.json path.

    Returns (path, is_explicit) or (None, False) on fatal error.
    Caller checks for None → return 1.

    Three resolution strategies, tried in order:
    1. ``--build`` — force a fresh build, ignoring any existing file.
    2. Explicit path (positional arg) — use the provided file.
    3. Default — reuse existing cc.json from config; build if missing.

    WHY: Users should not need to know where their build system stores
    compile_commands.json.  The default path is discovered from the
    detected build system and stored in config.  The explicit path
    option exists for edge cases (CI pipelines, unsupported build
    systems) where the user manages cc.json themselves.
    """
    explicit_cc = bool(args.compile_commands)

    if args.build:
        # `--background` means "skip the build" for a run that a user did not
        # ask for, because a build competes for the CPU and for the output
        # directory.  One case earns an exception: fw-context turned the
        # build on itself, after it found a source file that
        # compile_commands.json does not cover, and only a build can repair
        # that.  It is allowed only with an isolated output directory, which
        # `_plan_auto_build` grants to a backend that keeps its artifacts
        # apart from the ones of the build of the user.
        if bg and not cfg.build.isolated_build_dir:
            print("error: --build and --background are mutually exclusive", file=sys.stderr)
            return None, False
        from ..indexer.build import generate_compile_commands

        build_cfg = cfg.build
        if args.no_clean:
            build_cfg.clean = False
        try:
            return generate_compile_commands(project_root, build_cfg), False
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return None, False

    if explicit_cc:
        cc = Path(args.compile_commands)
        if not cc.is_absolute():
            cc = (project_root / cc).resolve()
        if not cc.exists():
            print(f"error: {cc} not found", file=sys.stderr)
            print("  Run 'fw-context index --build' to build and index automatically.", file=sys.stderr)
            return None, False
        from ..indexer.build import check_completeness
        for warning in check_completeness(cc, project_root):
            print(f"warning: {warning}", file=sys.stderr)
        return cc, True

    # Default: reuse existing, build only if missing
    from ..indexer.build import (
        check_completeness,
        generate_compile_commands,
        resolve_reuse_compile_commands,
    )

    cc = resolve_reuse_compile_commands(project_root, cfg.index.compile_commands)
    if not cc.exists():
        if bg:
            print(f"error: compile_commands.json not found at {cc}", file=sys.stderr)
            print("  Background reindex requires an existing compile_commands.json.", file=sys.stderr)
            print("  Run 'fw-context index --build' first to set up the project.", file=sys.stderr)
            return None, False
        print("compile_commands.json not found, running build...", file=sys.stderr)
        try:
            return generate_compile_commands(project_root, cfg.build), False
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return None, False

    for warning in check_completeness(cc, project_root):
        print(f"warning: {warning}", file=sys.stderr)
    return cc, False


def _validate_and_fix_artifacts(
    compile_commands: Path,
    project_root: Path,
    detected_system: str | None,
    cfg,
    bg: bool,
    explicit_cc: bool,
    args: argparse.Namespace,
) -> tuple[Path | None, list[str] | None, bool]:
    """Validate build artifacts and optionally auto-fix.

    Returns (compile_commands, build_dir_patterns, ok).
    compile_commands may be updated if a rebuild was triggered.
    Caller returns 1 when ok is False.

    Checks performed:
    * Staleness — cc.json older than project sources.
    * Completeness — all source files present in cc.json entries.
    * Path correctness — Windows backslashes, mixed separators.

    WHY: A stale compile_commands.json produces an index that silently
    misses new or renamed files.  Auto-detection and auto-rebuild
    prevent the user from debugging "symbol not found" errors caused
    by an outdated cc.json rather than actual code issues.
    """
    if not detected_system:
        return compile_commands, None, True

    from ..indexer.build import generate_compile_commands
    from ..indexer.builders import registry as builder_registry
    from ..indexer.validator import is_compile_commands_stale, validate_and_fix

    builder_cls = builder_registry.get(detected_system)
    if builder_cls is None:
        return compile_commands, None, True

    builder_instance = builder_cls()
    build_dir_patterns = builder_instance.get_build_dir_patterns(project_root)

    if not bg:
        stale, stale_reasons = is_compile_commands_stale(compile_commands, project_root)
        if stale:
            if explicit_cc:
                print(f"warning: explicit compile_commands.json is stale ({'; '.join(stale_reasons)})", file=sys.stderr)
            else:
                print(f"compile_commands.json is stale ({'; '.join(stale_reasons)}), rebuilding...")
                try:
                    compile_commands_new = generate_compile_commands(project_root, cfg.build)
                    print(f"Generated: {compile_commands_new}")
                    compile_commands = compile_commands_new
                except RuntimeError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    print("  Run 'fw-context index --build' to retry.", file=sys.stderr)
                    return compile_commands, None, False

    issues = validate_and_fix(compile_commands, project_root, builder_instance, cfg.build, fix=not bg)
    errors = [i for i in issues if i.severity == "error"]
    for w in [i for i in issues if i.severity == "warning"]:
        print(f"warning: {w.message}", file=sys.stderr)

    if errors:
        for e in errors:
            print(f"error: {e.message}", file=sys.stderr)
        if not bg and not explicit_cc and not args.build:
            print("Rebuilding to fix issues...")
            try:
                compile_commands_new = generate_compile_commands(project_root, cfg.build)
                print(f"Generated: {compile_commands_new}")
                issues = validate_and_fix(compile_commands_new, project_root, builder_instance, cfg.build, fix=True)
                errors = [i for i in issues if i.severity == "error"]
                for i in issues:
                    if i.severity == "warning":
                        print(f"warning: {i.message}", file=sys.stderr)
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return compile_commands, None, False
        if errors:
            for e in errors:
                print(f"error: {e.message}", file=sys.stderr)
            if bg:
                print("Run 'fw-context index' to resolve issues.", file=sys.stderr)
            else:
                print("Run 'fw-context index --build' to rebuild and fix issues.", file=sys.stderr)
            return compile_commands, None, False

    return compile_commands, build_dir_patterns, True


def _pid_is_fw_context_reindexer(pid: int) -> bool:
    """Verify that *pid* is a BACKGROUND fw-context index run.

    WHY the identity check at all: Linux recycles PIDs.  A stale
    ``reindex.pid`` may name a process that has nothing to do with
    fw-context, and signalling it would take out an unrelated service.

    WHY the cmdline and not the process name: this used to compare
    ``/proc/<pid>/comm`` against ``("fw-context", "python", "python3")``.  A
    virtualenv interpreter is named for its version — ``python3.14`` here —
    so the comparison never matched and the function always returned False.
    The takeover it guards therefore never happened, and two index runs could
    write the same database side by side.  Observed exactly that: a run
    started at 16:42 was still going when a second one started at 17:04.

    Only ``--background`` runs are killable.  The daemon spawns those and
    will start another when it needs one; a foreground run belongs to a
    person and is excluded through :func:`index_run_lock` instead of being
    terminated under them.
    """
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return False
    # /proc joins argv with NULs.
    argv = [arg for arg in cmdline.split("\0") if arg]
    if not any(arg.endswith(("fw-context", "fw_context_mcp.cli", "fw_context_mcp")) for arg in argv):
        return False
    if "index" not in argv:
        return False
    return "--background" in argv

def _kill_bg_reindex(db_path: Path) -> None:
    """Terminate a running BACKGROUND index run so that its lock drops.

    Reads reindex.pid, sends SIGTERM, waits up to 5 seconds and falls back
    to SIGKILL.  Two concurrent writers on one SQLite database corrupt it,
    and SQLITE_BUSY retries are not mutual exclusion on a long write.

    Writes nothing.  To claim the index is a separate step, and only the
    process that won index_run_lock may do it — see _claim_index().  A run
    that writes the pause marker and then LOSES the lock leaves a marker
    that names a live process, and the run that WON reads that marker as
    "somebody took the index over" and abandons itself.

    Deliberately does NOT clean up the pause marker of the run it just
    killed.  PidFile.is_active answers False for a dead PID and
    raise_if_superseded ignores such a marker — its own docstring says so.
    To unlink a marker that belongs to another process is the same defect
    this function exists to remove, seen from the other side.
    """
    reindex_pid_file = db_path.parent / "reindex.pid"

    # Kill any running background reindex, with PID reuse safety.
    old_pid = PidFile.read_pid(reindex_pid_file)
    if old_pid is not None and old_pid != os.getpid():
        if _pid_is_fw_context_reindexer(old_pid):
            os.kill(old_pid, signal.SIGTERM)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if not PidFile._pid_exists(old_pid):
                    break
                time.sleep(0.1)
            else:
                try:
                    os.kill(old_pid, signal.SIGKILL)
                except OSError:
                    log.debug("SIGKILL on bg reindex process %d failed", old_pid)
        else:
            log.debug("PID %d from reindex.pid is not an fw-context reindexer — ignoring", old_pid)


def _claim_index(db_dir: Path) -> None:
    """Write the pause marker and the pid marker.

    Call this function ONLY while the caller holds index_run_lock.  The pause
    marker stops the file-watcher daemon from starting a background run, and
    the pid marker tells every other process which run owns the index.  Both
    are claims, and a process that lost the lock owns nothing.

    Creates no directory: index_run_lock already does that before it takes
    the lock.
    """
    PidFile(db_dir / "reindex.pause").write()
    PidFile(db_dir / "reindex.pid").write()


def _release_index(db_dir: Path) -> None:
    """Drop the markers this process wrote, and only those.

    unlink_if_ours checks the PID, so a marker another run owns survives.
    """
    PidFile(db_dir / "reindex.pid").unlink_if_ours()
    PidFile(db_dir / "reindex.pause").unlink_if_ours()


def _post_index_optimize(
    db_path: Path,
    project_root: Path,
    project_id: str,
    detected_system: str | None,
    args: argparse.Namespace,
) -> None:
    """Run PRAGMA optimize and update the global project registry."""
    try:
        import sqlite3 as _sqlite3

        _opt_conn = _sqlite3.connect(str(db_path))
        _opt_conn.execute("PRAGMA optimize")
        _opt_conn.close()
    except _sqlite3.Error:
        log.debug("PRAGMA optimize failed for %s", db_path, exc_info=True)

    from ..config.global_db import open_global_db, upsert_project_registry

    _ptype = detected_system or "unknown"
    _gconn = open_global_db()
    try:
        upsert_project_registry(
            _gconn, project_id, getattr(args, "name", None) or project_root.name, _ptype, str(project_root)
        )
    finally:
        _gconn.close()


def _select_variants(variants: list, args: argparse.Namespace) -> list:
    """Return the variants selected by --variant/--variants CLI filters."""
    if not variants:
        return []
    requested: set[str] | None = None
    if getattr(args, "variant", None):
        requested = {args.variant}
    elif getattr(args, "variants", None):
        requested = {v.strip() for v in args.variants.split(",") if v.strip()}
    if requested is None:
        return list(variants)
    selected = [v for v in variants if v.name in requested]
    missing = requested - {v.name for v in selected}
    for m in sorted(missing):
        print(f"error: variant '{m}' not found in [[build.variants]]", file=sys.stderr)
    return selected


def _find_variant(variants: list, name: str):
    for v in variants:
        if v.name == name:
            return v
    return None


def _effective_board(build_cfg, variant, image: str) -> str:
    """Resolve the concrete board per-(variant, image).

    Per-image override wins (FLPR → cpuflpr), else the variant default board,
    else the shared ``[build] board``.
    """
    for img in variant.images:
        if img.name == image and img.board:
            return img.board
    return variant.board or build_cfg.board or ""


def _variant_build_dir(variant, build_cfg) -> str:
    """Return the per-variant build output directory (relative)."""
    return variant.build_dir or build_cfg.build_dir or f"build/{variant.name}"


def _filter_images(cc_list: list, args: argparse.Namespace) -> list:
    """Apply --image (inclusive) and --exclude-image (exclusive) index filters."""
    include = getattr(args, "image", None)
    exclude = set(getattr(args, "exclude_image", None) or [])
    out = []
    for item in cc_list:
        _, image, _, _ = item
        if image and image in exclude:
            continue
        if include and image != include:
            continue
        out.append(item)
    return out


def _discover_existing_cc(project_root, variants: list, build_cfg) -> list:
    """Discover existing ``compile_commands.<variant>[.<image>].json`` copies."""
    found: list = []
    for variant in variants:
        build_dir = _variant_build_dir(variant, build_cfg)
        bd = project_root / build_dir
        # per-image layout: <build_dir>/<image>/compile_commands.json (NCS 3.2.3)
        if bd.is_dir():
            for sub in sorted(bd.iterdir()):
                cc = sub / "compile_commands.json"
                if cc.exists():
                    found.append((variant.name, sub.name, cc, _effective_board(build_cfg, variant, sub.name)))
        # non-sysbuild single-image layout: compile_commands.<variant>.json
        single = (project_root / CC_OUTPUT_REL).with_name(f"compile_commands.{variant.name}.json")
        if single.exists():
            found.append((variant.name, "", single, _effective_board(build_cfg, variant, "")))
        if not bd.is_dir() and not single.exists():
            print(f"error: no build artifacts for variant '{variant.name}' — run 'fw-context index --build'", file=sys.stderr)
    return found


def _run_multi(
    args,
    cfg,
    project_root,
    project_id,
    db_path,
    detected_system,
    run_kwargs,
) -> int:
    """Orchestrate indexing of all (variant, image) builds.

    Builds each variant (sysbuild via ``build_multi``, else per-variant
    ``build()``), indexes each (variant, image) with deferred FTS/cleanup,
    then runs the FTS rebuild and per-(variant, image) retention once at the
    end (§5.8).  A failure in one build does not abort the others.
    """
    from ..exit_codes import EXIT_SUPERSEDED
    from ..indexer._postprocess import cleanup_old_builds_multi
    from ..indexer.build import build_variant_config, generate_compile_commands
    from ..indexer.builders import registry as builder_registry
    from ..indexer.db import open_db, rebuild_files_fts, rebuild_fts, rebuild_macros_fts
    from ..indexer.runner import IndexSuperseded, run

    build_cfg = cfg.build
    variants = _select_variants(build_cfg.variants, args)
    if not variants:
        print("error: no variants selected", file=sys.stderr)
        return 1

    system = build_cfg.system or detected_system
    builder_cls = builder_registry.get(system) if system else None
    builder = builder_cls() if builder_cls else None

    cc_list: list[tuple[str, str, Path, str]] = []

    if args.build:
        if builder is not None and hasattr(builder, "build_multi") and build_cfg.sysbuild:
            for variant, image, path in builder.build_multi(project_root, build_cfg):
                v = _find_variant(variants, variant)
                board = _effective_board(build_cfg, v, image) if v else ""
                cc_list.append((variant, image, path, board))
        else:
            for variant in variants:
                vcfg = build_variant_config(build_cfg, variant)
                if build_cfg.isolated_build_dir:
                    # One directory per variant.  Every variant has its own
                    # output directory, thus a shared one would make the
                    # variants overwrite each other.
                    vcfg.isolated_build_dir = autobuild_dir(variant.name)
                try:
                    path = generate_compile_commands(project_root, vcfg)
                except RuntimeError as exc:
                    print(f"error: variant '{variant.name}': {exc}", file=sys.stderr)
                    continue
                cc_list.append((variant.name, "", path, _effective_board(build_cfg, variant, "")))
    else:
        cc_list = _discover_existing_cc(project_root, variants, build_cfg)

    cc_list = _filter_images(cc_list, args)

    if args.no_index:
        print(f"Built {len(cc_list)} build(s) — skipping indexing (--no-index)")
        return 0

    if not cc_list:
        print("error: no builds to index", file=sys.stderr)
        return 1

    touched_pairs: list[tuple[str, str]] = []
    for variant_name, image, cc_path, board in cc_list:
        v = _find_variant(variants, variant_name)
        env = dict(build_cfg.env)
        if v is not None:
            env.update(v.env)
        build_dir_patterns = [f"{_variant_build_dir(v, build_cfg)}/"] if v is not None else None
        # The path layers are summed per build, not once for the command:
        # two variants may vendor different in-tree trees.  run_kwargs holds
        # the [index] layer (or the CLI flag, which replaces it).
        per_build = dict(run_kwargs)
        idx = v.index_overrides if v is not None else {}
        for key in ("vendor_paths", "project_paths"):
            extra = list(idx.get(key) or [])
            per_build[key] = _layered_paths(list(run_kwargs[key]), extra)
            if extra:
                print(
                    f"  {variant_name}: {key} = "
                    f"{len(run_kwargs[key])} from [index] + {len(extra)} from variant"
                )
        try:
            ch = run(
                compile_commands=cc_path,
                db_path=db_path,
                variant=variant_name,
                image=image,
                board=board,
                build_env=env,
                build_dir_patterns=build_dir_patterns,
                defer_fts=True,
                defer_cleanup=True,
                **per_build,
            )
        except IndexSuperseded as exc:
            # Another process owns the index now.  The remaining builds would
            # abort on the same marker, so stop and let the whole command be
            # retried — a partial sweep would leave some builds indexed
            # against a database the other process is changing.
            print(f"Superseded: {exc}", file=sys.stderr)
            return EXIT_SUPERSEDED
        except Exception as exc:  # noqa: BLE001 — best-effort per-build
            print(f"error: indexing variant={variant_name} image={image}: {exc}", file=sys.stderr)
            continue
        touched_pairs.append((variant_name, image))
        print(f"Indexed variant={variant_name} image={image} config_hash={ch[:16]}…")

    # Final: rebuild FTS once and run per-(variant, image) retention once.
    conn = open_db(db_path)
    fts_problems: list[str] = []
    try:
        rebuild_fts(conn)
        rebuild_files_fts(conn)
        rebuild_macros_fts(conn)
        # Each build skipped its own FTS check because the rebuild was
        # deferred to here — see _step_verify_integrity.  This is the one
        # place that can run it, and it has to: the FTS index backs
        # search_code, search_content and the macro search, and an index that
        # disagrees with its content table serves rows that are not there.
        from ..indexer._postprocess import fts_inconsistencies

        fts_problems = fts_inconsistencies(conn)
        deleted = cleanup_old_builds_multi(conn, project_id, db_path.parent, touched_pairs)
        if deleted:
            print(f"Cleaned up {deleted} stale build(s)")
    finally:
        conn.close()

    if fts_problems:
        for problem in fts_problems:
            print(f"error: {problem}", file=sys.stderr)
        print(
            "error: the full-text index disagrees with its content after a "
            "rebuild — reindex with --force",
            file=sys.stderr,
        )
        return 1

    _post_index_optimize(db_path, project_root, project_id, system, args)
    return 0


def _layered_paths(base: list[str], variant_extra: list[str]) -> list[str]:
    """Return the ``[index]`` layer PLUS this variant's, in a stable order.

    Adds, never replaces.  A user with a value in ``[index]`` and in the
    variant would otherwise lose the shared entries without a word, which is
    the same class of silent wrongness this series of changes is about.  The
    precedent is already in the repo: ``_DICT_FIELDS`` in build.py merges
    ``env`` per key, and says so.

    The cost, and it is real: a variant cannot NARROW the set.  The channels
    for that are ``project_paths``, which wins over every vendor pattern, or
    a fix to the detection.  No escape hatch is added.

    ``dict.fromkeys`` and not ``set``: the order shows up in the LIKE filters,
    in the log and in the manifest, and a stable order diffs cleanly.
    """
    return list(dict.fromkeys([*base, *variant_extra]))


def _build_run_kwargs(
    args, cfg, project_root, project_id, vendor_paths, project_paths, cs_config,
) -> dict:
    """Build the shared ``runner.run()`` kwargs from config + CLI flags."""
    from ..indexer.build import detect_build_system

    return dict(
        vendor_paths=vendor_paths,
        project_paths=project_paths,
        project_name=args.name or cfg.project.name,
        index_refs=False if args.no_refs else cfg.index.index_refs,
        index_embeddings=(
            False
            if getattr(args, "no_embeddings", False)
            else getattr(args, "embeddings", None) or cfg.index.index_embeddings
        ),
        analyze_symbols=(
            False
            if getattr(args, "no_analyze", False)
            else getattr(args, "analyze", False) or cfg.llm.analyze_symbols
        ),
        analyze_overrides=True,
        project_root=project_root,
        project_id=project_id,
        llm_config=cfg.llm,
        cache_server_config=cs_config,
        config_header=cfg.index.config_header,
        force=args.force,
        analyze_vendor=(
            False
            if getattr(args, "no_analyze_vendor", False)
            else getattr(args, "analyze_vendor", False) or cfg.llm.analyze_vendor
        ),
        purge_max_missing_percent=cfg.index.purge_max_missing_percent,
        build_system=cfg.build.system or detect_build_system(project_root),
    )


def _ensure_watcher_after_index(project_root: Path) -> None:
    """Start the watcher daemon after an index run that was successful.

    WHY: an MCP server spawns the daemon only at startup, and only when
    ``index.db`` already exists.  A server that starts before the first
    index takes an early return and spawns no daemon.  The daemon is the
    only component that starts a background reindex, thus that project
    gets no automatic reindex.

    This function closes that window.  After the index run, ``index.db``
    exists, which is the only precondition of ``_ensure_daemon_running``.
    The call does nothing when a daemon already runs.
    """
    try:
        from ..mcp.background import _ensure_daemon_running

        _ensure_daemon_running(project_root)
    except (RuntimeError, OSError):
        # Not fatal — the index is complete and usable.  Only the
        # automatic reindex of changed files is not available.  The
        # command `fw-context watch restart` corrects this.
        log.debug("Could not start the watcher daemon for %s", project_root, exc_info=True)


def cmd_index(args: argparse.Namespace) -> int:
    """Build or rebuild the symbol index from compile_commands.json.

    By default, reuses an existing ``compile_commands.json`` for fast
    incremental indexing.  When the file is missing, a clean build is
    triggered automatically (auto-detecting Mbed OS / Zephyr / PlatformIO).

    Pass ``--build`` to force a fresh build and full re-index.

    WHY: Most invocations are incremental (user edits a file and runs
    ``fw-context index``).  Forcing a build on every run would be
    wasteful — Mbed/zephyr builds can take minutes.  The auto-detect
    logic only triggers a build when necessary.
    """
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..exit_codes import EXIT_ALREADY_RUNNING, EXIT_SUPERSEDED
    from ..indexer.build import detect_build_system
    from ..indexer.db._locking import IndexRunLocked, index_run_lock
    from ..indexer.runner import IndexSuperseded, run
    from ..utils import resolve_project_root

    if args.verbose:
        handler = logging.StreamHandler()
        handler.setFormatter(VerboseFormatter())
        logging.basicConfig(level=logging.DEBUG, handlers=[handler])
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )

    # Suppress httpx INFO logs (one per HTTP request — extremely noisy during
    # embedding generation, LLM analysis, and cache server communication).
    logging.getLogger("httpx").setLevel(logging.WARNING)

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    bg = getattr(args, "background", False)
    # The config wins over the markers, the same form _run_multi already
    # uses.  The two can disagree: a freestanding NCS application has
    # CMakeLists.txt and no west.yml, so a marker scan calls it a CMake
    # project.  This value also decides which builder generates and
    # validates compile_commands.json, not only the vendor patterns.
    detected_system = cfg.build.system or detect_build_system(project_root)

    if detected_system:
        print(f"Project: {project_root.name}  path={project_root}  build={detected_system}")
    elif bg:
        cc_fallback = project_root / CC_OUTPUT_REL
        if cc_fallback.exists():
            print(f"Project: {project_root.name}  path={project_root}  build=unknown (bg, reusing cc)")
        else:
            print(f"error: No build system detected and no compile_commands.json for {project_root}.", file=sys.stderr)
            print("  Run 'fw-context index --build' first.", file=sys.stderr)
            return 1
    else:
        print(f"Project: {project_root.name}  path={project_root}  build=unknown")

    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    # ── Automatic build for a source file that the build system never saw ──
    # Such a file has no translation unit, thus a plain reindex skips it and
    # reports success.  Only a build writes it into compile_commands.json.
    # `_plan_auto_build` returns a non-empty list only when the backend can
    # build without touching the output of the build of the user.
    auto_build_sources: list[str] = []
    if not getattr(args, "build", False):
        auto_build_sources, auto_build_cfg = _plan_auto_build(
            project_root, db_path, cfg, detected_system
        )
        # The two always arrive together; the second test is what lets the
        # type checker see that, and it costs nothing.
        if auto_build_sources and auto_build_cfg is not None:
            args.build = True
            # The candidate that _plan_auto_build asked the backend about.
            # Applying it here, and nowhere else, keeps every "do not build"
            # path from leaving an isolated directory behind on cfg.
            cfg.build = auto_build_cfg
            listed = ", ".join(auto_build_sources[:3])
            more = f" and {len(auto_build_sources) - 3} more" if len(auto_build_sources) > 3 else ""
            log.info(
                "%d source file(s) are missing from compile_commands.json (%s%s) — "
                "running a build into %s",
                len(auto_build_sources), listed, more, cfg.build.isolated_build_dir,
            )

    # The CLI flag REPLACES the [index] layer.  A variant's own [index] keys
    # are added on top of whichever of the two won — see _layered_paths().
    vendor_paths = list(getattr(args, "vendor_paths", None) or cfg.index.vendor_paths)
    project_paths = list(getattr(args, "project_paths", None) or cfg.index.project_paths)

    cs_config = cfg.cache_server
    if cs_config is not None and getattr(args, "force", False):
        from dataclasses import replace
        cs_config = replace(cs_config, force=True)

    # ── Multi-variant: dispatch BEFORE single-build resolution ──
    # WHY: [[build.variants]] replaces the single [build] board/source flow.
    # build_multi() builds each variant; the single-build path below
    # (generate_compile_commands → builder.build()) would fail on a project
    # whose board lives per-variant (Zephyr "requires a board name").
    if cfg.build.variants:
        # Takes over from a background run by killing it, which also drops
        # its index lock; the lock below then only refuses another
        # FOREGROUND run.  The markers are claimed AFTER the lock — see
        # _kill_bg_reindex().
        _kill_bg_reindex(db_path)
        run_kwargs = _build_run_kwargs(
            args, cfg, project_root, project_id, vendor_paths, project_paths, cs_config,
        )
        try:
            # Held across every (variant, image) build, not per build: a
            # second run slipping in between two builds would index against a
            # database this one is still changing.
            with index_run_lock(db_path.parent):
                _claim_index(db_path.parent)
                try:
                    exit_code = _run_multi(
                        args, cfg, project_root, project_id, db_path,
                        detected_system, run_kwargs,
                    )
                finally:
                    _release_index(db_path.parent)
        except IndexRunLocked as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ALREADY_RUNNING

        # Outside the index lock, and only after a run that was
        # successful — see the same call at the end of this function.
        if exit_code == 0:
            if auto_build_sources:
                autobuild.clear_failure(db_path.parent)
            _ensure_watcher_after_index(project_root)
        elif auto_build_sources:
            autobuild.record_failure(db_path.parent, auto_build_sources)
        return exit_code

    # ── Resolve compile_commands.json ──
    cc_result = _resolve_compile_commands(args, project_root, cfg, detected_system, bg)
    if cc_result[0] is None:
        if auto_build_sources:
            # The build that fw-context started failed.  Remember it, or the
            # next daemon cycle repeats it: a failed build leaves
            # compile_commands.json untouched, thus the trigger stays armed.
            autobuild.record_failure(db_path.parent, auto_build_sources)
        return 1
    compile_commands, explicit_cc = cc_result
    assert compile_commands is not None  # checked above via cc_result[0]

    # ── Validate build artifacts ──
    compile_commands, build_dir_patterns, ok = _validate_and_fix_artifacts(
        compile_commands, project_root, detected_system, cfg, bg, explicit_cc, args
    )
    if not ok:
        return 1
    assert compile_commands is not None  # _validate_and_fix_artifacts returns Path|None, but ok=True → non-None

    # ── Manage background reindex ──
    # Takes over from a background run by killing it, which also drops its
    # index lock; the lock below then only refuses another FOREGROUND run.
    # The markers are claimed AFTER the lock — see _kill_bg_reindex().
    _kill_bg_reindex(db_path)

    run_kwargs = _build_run_kwargs(
        args, cfg, project_root, project_id, vendor_paths, project_paths, cs_config,
    )

    try:
        with index_run_lock(db_path.parent):
            _claim_index(db_path.parent)
            try:
                config_hash = run(
                    compile_commands=compile_commands,
                    db_path=db_path,
                    build_dir_patterns=build_dir_patterns,
                    **run_kwargs,
                )
                print(f"Indexed. config_hash={config_hash[:16]}…  db={db_path}")

                _post_index_optimize(
                    db_path, project_root, project_id, detected_system, args
                )
            except IndexSuperseded as exc:
                # Not a failure — see IndexSuperseded.  The distinct exit code
                # lets the daemon retry instead of treating it as a broken run.
                print(f"Superseded: {exc}", file=sys.stderr)
                return EXIT_SUPERSEDED
            finally:
                _release_index(db_path.parent)
    except IndexRunLocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ALREADY_RUNNING

    if auto_build_sources:
        # The build worked, thus the backoff marker of an earlier failure is
        # obsolete.
        autobuild.clear_failure(db_path.parent)

    # Outside the index lock.  The daemon does a staleness check when it
    # starts, thus a daemon that starts inside the lock could start a
    # second index run against the database that this run still changes.
    _ensure_watcher_after_index(project_root)
    return 0
