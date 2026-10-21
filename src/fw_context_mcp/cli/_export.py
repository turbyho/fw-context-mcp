"""``fw-context export``, ``analyze``, ``version`` — data export and analysis.

Export: serializes the symbol index as portable JSON (format version
``fw-context-export/1``) — enables data exchange, backup, and offline
analysis.  Analyzes: re-runs LLM symbol analysis on an existing index
without recompiling.  Version: prints the installed package version.

WHY export format versioning: downstream consumers (scripts, visualizers,
other tools) need a stable contract.  The ``_format`` field enables
forward-compatible parsing.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import VerboseFormatter


def cmd_export(args: argparse.Namespace) -> int:
    """Export the symbol index as portable JSON (``_format: "fw-context-export/1"``).

    Writes all symbols and optionally cross-references to a JSON file or
    stdout.  The format includes project metadata, all symbol records,
    and reference counts.

    WHY JSON (not SQLite dump): the index database uses SQLite-specific
    features (FTS5, vec0).  A JSON export is portable across machines,
    database versions, and consumers.  The format is documented and
    versioned so downstream tools can rely on it.
    """
    import json

    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer.build import detect_build_system
    from ..indexer.db import count_refs
    from ..utils import resolve_project_root
    from ._builds import NO_BUILD, open_for_reading, selected_build

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print(f"No index found for {project_root}. Run 'fw-context index' first.", file=sys.stderr)
        return 1

    conn = open_for_reading(db_path)
    if conn is None:
        return 1
    try:
        build_cfg, error = selected_build(conn, project_id, cfg, args)
        if build_cfg is None:
            print(f"No build config indexed for {project_root}." if error == NO_BUILD else error, file=sys.stderr)
            return 1
        config_hash = build_cfg["config_hash"]

        # variant, image and board name the build of the export.  They are
        # new keys, and no key changes, thus the format stays
        # fw-context-export/1.
        output: dict = {
            "_format": "fw-context-export/1",
            "project": {
                "id": project_id,
                "root": str(project_root),
                "build_system": detect_build_system(project_root),
                "compile_commands": build_cfg["compile_commands_path"],
                "indexed_at": build_cfg["created_at"],
            },
            "config_hash": config_hash,
            "variant": build_cfg["variant"] or "",
            "image": build_cfg["image"] or "",
            "board": build_cfg["board"] or "",
        }

        # Symbols
        symbols = conn.execute(
            """SELECT name, qualified_name, kind, file_path, line, col, end_line,
                      is_definition, signature, docstring
               FROM symbols WHERE config_hash=? ORDER BY kind, name""",
            (config_hash,),
        ).fetchall()
        output["symbols"] = [dict(r) for r in symbols]
        output["symbol_count"] = len(symbols)

        # References (optional)
        if not args.no_refs:
            ref_count = count_refs(conn, config_hash)
            if ref_count > 0:
                refs = conn.execute(
                    """SELECT r.to_usr, r.from_file, r.from_line, r.ref_kind,
                              caller.name AS caller_name,
                              callee.name AS callee_name
                       FROM refs r
                       LEFT JOIN symbols caller ON caller.usr = r.from_usr AND caller.config_hash = r.config_hash
                       LEFT JOIN symbols callee ON callee.usr = r.to_usr AND callee.config_hash = r.config_hash
                       WHERE r.config_hash = ?
                       ORDER BY r.from_file, r.from_line""",
                    (config_hash,),
                ).fetchall()
                output["references"] = [dict(r) for r in refs]
                output["reference_count"] = ref_count
            else:
                output["reference_count"] = 0
    finally:
        conn.close()

    json_text = json.dumps(output, indent=2, ensure_ascii=False, default=str)

    if args.output:
        Path(args.output).write_text(json_text, encoding="utf-8")
        print(
            f"Exported {output['symbol_count']} symbols"
            f"{' + ' + str(output.get('reference_count', 0)) + ' references' if 'reference_count' in output else ''}"
            f" → {args.output}"
        )
    else:
        print(json_text)

    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Re-run LLM symbol analysis on an existing index (idempotent).

    Requires Ollama (or LM Studio) running and ``[llm] enabled = true``
    in config.  Re-generates per-symbol summaries, inputs/outputs analysis,
    and method override relationships.

    WHY idempotent: analysis is keyed by content hash — re-running on an
    already-analyzed index skips all cached symbols.  This means users can
    safely call ``fw-context analyze`` after editing a few files without
    re-analyzing unchanged symbols.

    WHY suppress httpx INFO logs: each HTTP request to Ollama produces an
    INFO-level log line; during analysis of thousands of symbols this
    floods the output.  WARNING level still shows errors.

    ``--force-llm`` makes each analysis again, as ``fw-context index
    --force-llm`` does, and keeps the same marker
    (``indexer._force_llm_marker``): a command that stops, or does not reach
    each symbol, is continued by the next ``--force-llm`` of either command.
    """
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..utils import resolve_project_root
    from ._builds import selected_builds

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

    if not cfg.llm.enabled:
        print("error: [llm] enabled = false in config. Enable Ollama first.", file=sys.stderr)
        return 1

    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print(f"No index found for {project_root}. Run 'fw-context index' first.", file=sys.stderr)
        return 1

    # A selector that names no build fails at once, also while the index is
    # held; _analyze_builds reads the builds again under the lock.  Read
    # only: an index of an older schema skips this check, and the read
    # under the lock migrates it.
    from ..indexer.db import open_db_readonly
    from ._builds import reading_problem

    # Resolve analyze_vendor: CLI flag takes precedence, config falls back.
    analyze_vendor = (
        False
        if getattr(args, "no_analyze_vendor", False)
        else getattr(args, "analyze_vendor", False) or cfg.llm.analyze_vendor
    )

    from ._llm_choice import RunProfileError, analysis_count_line, llm_for_run, may_ask

    conn = open_db_readonly(db_path)
    count = ""
    try:
        error = None
        if not reading_problem(conn):
            builds, error = selected_builds(conn, project_id, cfg, args)
            if error is None and not getattr(args, "llm", None) and may_ask(False):
                # For the question only: a fact of the index, read before the lock.
                count = analysis_count_line(conn, builds, project_only=not analyze_vendor,
                                            force=bool(getattr(args, "force_llm", False)))
    finally:
        conn.close()
    if error:
        print(error, file=sys.stderr)
        return 1
    try:
        cfg = llm_for_run(args, cfg, background=False, analyses=True, count=count)
    except RunProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from ..indexer.db._locking import IndexRunLocked
    from ..indexer.runner import IndexStopped
    from ._index import _is_analyze_run, _owned_index

    # The index lock of `fw-context index`: two operations on the index of
    # one project must not write it at the same time.  Before, `analyze`
    # took none, thus it wrote analyses while an index run replaced the
    # symbols, and a `--force-llm` of each command removed the marker of
    # the other.
    #
    # `analyze` never takes the index over, also not from a background run:
    # it would analyse an index that the stopped run did not complete, and
    # the symbols that the run then changes would go to the model again.
    # It says which run holds the index and waits for its end.  The lock
    # also pauses the background reindex while `analyze` holds it.
    waiting = False
    while True:
        try:
            with _owned_index(db_path.parent, _NEVER_TAKE_OVER, pid_marker=False):
                return _analyze_builds(args, cfg, project_root, project_id, db_path, analyze_vendor)
        except IndexRunLocked as exc:
            if not waiting:
                pid = exc.holder_pid
                who = "another 'fw-context analyze'" if pid and _is_analyze_run(pid) else "an index run"
                holder = f"pid {pid}" if pid else "another process"
                print(f"{who[0].upper()}{who[1:]} of this project holds the index ({holder}); "
                      f"fw-context analyze waits for its end", file=sys.stderr)
                waiting = True
            time.sleep(_INDEX_WAIT_SECONDS)
        except IndexStopped as exc:
            print(f"{exc.label}: {exc}", file=sys.stderr)
            return exc.exit_code


# `_owned_index` takes the index over from a background run unless the
# arguments say `background`: a background run never takes anything over.
_NEVER_TAKE_OVER = argparse.Namespace(background=True, takeover=False)

# How often a waiting `analyze` asks for the index lock.  An index run takes
# minutes to hours, thus a few seconds of delay after its end cost nothing.
_INDEX_WAIT_SECONDS = 5.0


def _analyze_builds(
    args: argparse.Namespace, cfg, project_root: Path, project_id: str, db_path: Path, analyze_vendor: bool,
) -> int:
    """Analyse each selected build and return the exit code; the caller holds the index lock (see :func:`cmd_analyze`).

    The builds are read again under the lock: an index run that `analyze`
    waited for can have replaced a build.
    """
    from ..indexer import _force_llm_marker as force_llm_marker
    from ..indexer._llm_analysis import _build_llm_analysis
    from ..indexer._postprocess import _build_overrides
    from ..indexer.db import open_db, set_analyze_vendor
    from ._builds import label, selected_builds

    conn = open_db(db_path)
    try:
        builds, error = selected_builds(conn, project_id, cfg, args)
    finally:
        conn.close()
    if error:
        print(error, file=sys.stderr)
        return 1

    force_llm = bool(getattr(args, "force_llm", False))
    force_since = None
    if force_llm:
        force_since, continues = force_llm_marker.since_for(db_path.parent, cfg.llm.model)
        if continues:
            print(f"--force-llm continues the command of {force_since} UTC", file=sys.stderr)

    # Re-open connection for the analysis (uses its own transactions)
    conn = open_db(db_path)
    cc = None
    try:
        # Create CacheClient from config if available
        if cfg.cache_server and cfg.cache_server.url:
            try:
                from ..cache_client import CacheClient

                # The cache server holds the LLM analysis only: a new
                # analysis replaces its entries (can_overwrite).
                cc = CacheClient(
                    url=cfg.cache_server.url,
                    token=cfg.cache_server.token,
                    force=cfg.cache_server.force or force_llm,
                    batch_size=cfg.cache_server.batch_size,
                )
            except (ValueError, TypeError, AttributeError) as e:
                logging.getLogger(__name__).warning("Failed to create CacheClient: %s", e)

        # Each build without options, as a reindex does: the analysis is
        # content-addressed, thus a symbol that one build already
        # analysed is not sent to the model again.
        for build in builds:
            config_hash = build["config_hash"]
            finished = _build_llm_analysis(
                conn,
                config_hash,
                cfg.llm,
                db_path.parent,
                project_root=project_root,
                project_only=not analyze_vendor,
                cache_client=cc,
                retry_unparseable=True,
                force_since=force_since,
            )
            if force_llm and not finished:
                force_llm_marker.mark_unfinished(db_path.parent)
            _build_overrides(conn, config_hash, db_path.parent)
            conn.commit()

            # Update stored analyze_vendor so get_project_status reports
            # consistent state.  The time of the index run stays: no symbol
            # changed (see set_analyze_vendor).
            set_analyze_vendor(conn, config_hash, analyze_vendor)
            conn.commit()
            if finished:
                print(f"LLM analysis complete for {project_root}, build {label(build)}")
            else:
                print(f"LLM analysis not complete for {project_root}, build {label(build)}: a symbol failed, "
                      "or the run stopped (see the log). A later run continues")
    finally:
        if cc:
            cc.close()
        conn.close()
    if force_llm and not force_llm_marker.finish(db_path.parent):
        print("warning: --force-llm did not reach each symbol (see the log); run it again to continue",
              file=sys.stderr)
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    """Print the version, and a newer release when the MCP server found one.

    The command reads the state file of the update check and sends no
    request: the MCP server asks PyPI in the background (see
    ``version_check``), and a version command must give its answer at once
    on a machine without network access too.
    """
    from .. import __version__
    from ..config.settings import load_update_settings
    from ..version_check import newer_release

    print(f"fw-context-mcp {__version__}")
    found = newer_release(load_update_settings().check)
    if found is not None:
        print(f"fw-context-mcp {found[0]} is available on PyPI. Upgrade it with the tool that installed it.")
    return 0
