"""``fw-context index`` — build or rebuild the symbol index."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

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
    from ..indexer.build import check_completeness, generate_compile_commands

    cc = cfg.index.compile_commands
    if not cc.is_absolute():
        cc = (project_root / cc).resolve()
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


# NOTE: PID file access could benefit from fcntl.flock() for multi-process
# safety. Currently uses os.kill(pid, 0) for liveness checks which has
# a TOCTOU race with PID reuse (low risk — PIDs on Linux wrap at 4M, collision extremely unlikely).  flock would eliminate this entirely.
def _manage_bg_reindex(db_path: Path) -> None:
    """Kill any running background reindex and write pause/pid files."""
    pause_file = db_path.parent / "reindex.pause"
    reindex_pid_file = db_path.parent / "reindex.pid"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if reindex_pid_file.exists():
        try:
            bg_pid = int(reindex_pid_file.read_text(encoding="utf-8").strip())
            if bg_pid != os.getpid():
                os.kill(bg_pid, signal.SIGTERM)
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    try:
                        os.kill(bg_pid, 0)
                    except OSError:
                        break
                    time.sleep(0.1)
                else:
                    try:
                        os.kill(bg_pid, signal.SIGKILL)
                    except OSError:
                        log.debug("SIGKILL on bg reindex process %d failed", bg_pid)
                reindex_pid_file.unlink(missing_ok=True)
        except (OSError, ValueError):
            reindex_pid_file.unlink(missing_ok=True)

    try:
        pause_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        log.debug("Failed to write pause marker %s", pause_file)
    reindex_pid_file.write_text(str(os.getpid()), encoding="utf-8")


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


def cmd_index(args: argparse.Namespace) -> int:
    """Build or rebuild the symbol index from compile_commands.json.

    By default, reuses an existing ``compile_commands.json`` for fast
    incremental indexing.  When the file is missing, a clean build is
    triggered automatically (auto-detecting Mbed OS / Zephyr / PlatformIO).

    Pass ``--build`` to force a fresh build and full re-index.
    """
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer.build import detect_build_system
    from ..indexer.runner import run
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

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    bg = getattr(args, "background", False)
    detected_system = detect_build_system(project_root)

    if detected_system:
        print(f"Project: {project_root.name}  path={project_root}  build={detected_system}")
    elif bg:
        cc_fallback = project_root / "compile_commands.json"
        if cc_fallback.exists():
            print(f"Project: {project_root.name}  path={project_root}  build=unknown (bg, reusing cc)")
        else:
            print(f"error: No build system detected and no compile_commands.json for {project_root}.", file=sys.stderr)
            print("  Run 'fw-context index --build' first.", file=sys.stderr)
            return 1
    else:
        print(f"Project: {project_root.name}  path={project_root}  build=unknown")

    # ── Resolve compile_commands.json ──
    cc_result = _resolve_compile_commands(args, project_root, cfg, detected_system, bg)
    if cc_result[0] is None:
        return 1
    compile_commands, explicit_cc = cc_result

    # ── Validate build artifacts ──
    compile_commands, build_dir_patterns, ok = _validate_and_fix_artifacts(
        compile_commands, project_root, detected_system, cfg, bg, explicit_cc, args
    )
    if not ok:
        return 1

    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    vendor_paths = list(getattr(args, "vendor_paths", None) or cfg.index.vendor_paths)
    project_paths = list(getattr(args, "project_paths", None) or cfg.index.project_paths)

    cs_config = cfg.cache_server
    if cs_config is not None and getattr(args, "force", False):
        from dataclasses import replace
        cs_config = replace(cs_config, force=True)

    # ── Manage background reindex ──
    _manage_bg_reindex(db_path)
    reindex_pid_file = db_path.parent / "reindex.pid"
    pause_file = db_path.parent / "reindex.pause"

    try:
        config_hash = run(
            compile_commands=compile_commands,
            db_path=db_path,
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
            build_dir_patterns=build_dir_patterns,
            analyze_vendor=(
                False
                if getattr(args, "no_analyze_vendor", False)
                else getattr(args, "analyze_vendor", False) or cfg.llm.analyze_vendor
            ),
        )
        print(f"Indexed. config_hash={config_hash[:16]}…  db={db_path}")

        _post_index_optimize(db_path, project_root, project_id, detected_system, args)
        return 0
    finally:
        reindex_pid_file.unlink(missing_ok=True)
        try:
            if pause_file.exists():
                content = pause_file.read_text(encoding="utf-8").strip()
                if content == str(os.getpid()):
                    pause_file.unlink(missing_ok=True)
        except OSError:
            log.debug("Failed to unlink pause file %s", pause_file)
