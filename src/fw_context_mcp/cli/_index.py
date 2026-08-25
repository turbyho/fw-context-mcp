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

from ..mcp.shared.pid_file import PidFile
from ..utils import CC_OUTPUT_REL
from . import VerboseFormatter

log = logging.getLogger(__name__)


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
        if bg:
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

def _manage_bg_reindex(db_path: Path) -> None:
    """Kill any running background reindex and write pause/pid files.

    A foreground ``fw-context index`` must not race with a background
    reindex writing to the same database.  This function:
    1. Reads the PID file to find the background process.
    2. Sends SIGTERM, waits up to 5 seconds, falls back to SIGKILL.
    3. Writes a pause file to prevent the file-watcher daemon from
       starting a new background reindex while the foreground one runs.
    4. Writes a PID file so other processes know this index is active.

    WHY: Two concurrent writers on an SQLite database cause corruption
    (SQLITE_BUSY retries are not a substitute for mutual exclusion on
    long-running writes).  The pause file is the coordination mechanism
    between the CLI and the daemon.
    """
    pause_file = db_path.parent / "reindex.pause"
    reindex_pid_file = db_path.parent / "reindex.pid"
    db_path.parent.mkdir(parents=True, exist_ok=True)

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

    PidFile(pause_file).write()
    PidFile(reindex_pid_file).write()


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
    from ..indexer._postprocess import cleanup_old_builds_multi
    from ..indexer.build import build_variant_config, generate_compile_commands
    from ..indexer.builders import registry as builder_registry
    from ..indexer.db import open_db, rebuild_files_fts, rebuild_fts, rebuild_macros_fts
    from ..indexer.runner import EXIT_SUPERSEDED, IndexSuperseded, run

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
                **run_kwargs,
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
    from ..indexer.build import detect_build_system
    from ..indexer.db._locking import IndexRunLocked, index_run_lock
    from ..indexer.runner import (
        EXIT_ALREADY_RUNNING,
        EXIT_SUPERSEDED,
        IndexSuperseded,
        run,
    )
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
        # FOREGROUND run.
        _manage_bg_reindex(db_path)
        run_kwargs = _build_run_kwargs(
            args, cfg, project_root, project_id, vendor_paths, project_paths, cs_config,
        )
        try:
            # Held across every (variant, image) build, not per build: a
            # second run slipping in between two builds would index against a
            # database this one is still changing.
            with index_run_lock(db_path.parent):
                try:
                    return _run_multi(
                        args, cfg, project_root, project_id, db_path,
                        detected_system, run_kwargs,
                    )
                finally:
                    PidFile(db_path.parent / "reindex.pid").unlink_if_ours()
                    PidFile(db_path.parent / "reindex.pause").unlink_if_ours()
        except IndexRunLocked as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ALREADY_RUNNING

    # ── Resolve compile_commands.json ──
    cc_result = _resolve_compile_commands(args, project_root, cfg, detected_system, bg)
    if cc_result[0] is None:
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
    _manage_bg_reindex(db_path)

    run_kwargs = _build_run_kwargs(
        args, cfg, project_root, project_id, vendor_paths, project_paths, cs_config,
    )

    try:
        with index_run_lock(db_path.parent):
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
                return 0
            except IndexSuperseded as exc:
                # Not a failure — see IndexSuperseded.  The distinct exit code
                # lets the daemon retry instead of treating it as a broken run.
                print(f"Superseded: {exc}", file=sys.stderr)
                return EXIT_SUPERSEDED
            finally:
                PidFile(db_path.parent / "reindex.pid").unlink_if_ours()
                PidFile(db_path.parent / "reindex.pause").unlink_if_ours()
    except IndexRunLocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
