"""``fw-context export``, ``analyze``, ``version`` — data export and analysis."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import VerboseFormatter


def cmd_export(args: argparse.Namespace) -> int:
    """Export the symbol index as portable JSON (``_format: "fw-context-export/1"``).

    Writes all symbols and optionally cross-references to a JSON file
    or stdout. The format includes project metadata, all symbol records,
    and reference counts.
    """
    import json

    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer.build import detect_build_system
    from ..indexer.db import count_refs, get_active_config, open_db
    from ..utils import resolve_project_root

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print(f"No index found for {project_root}. Run 'fw-context index' first.", file=sys.stderr)
        return 1

    conn = open_db(db_path)
    try:
        build_cfg = get_active_config(conn, project_id)
        if not build_cfg:
            print(f"No build config indexed for {project_root}.", file=sys.stderr)
            return 1
        config_hash = build_cfg["config_hash"]

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
    in config. Re-generates per-symbol summaries, inputs/outputs analysis
    and method override relationships. Existing analysis rows are skipped
    (idempotent) — only unanalyzed symbols are processed.
    """
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer._llm_analysis import _build_llm_analysis
    from ..indexer._postprocess import _build_overrides
    from ..indexer.db import get_active_config, open_db, upsert_build_config
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

    if not cfg.llm.enabled:
        print("error: [llm] enabled = false in config. Enable Ollama first.", file=sys.stderr)
        return 1

    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print(f"No index found for {project_root}. Run 'fw-context index' first.", file=sys.stderr)
        return 1

    conn = open_db(db_path)
    try:
        build_cfg = get_active_config(conn, project_id)
        if not build_cfg:
            print("No build config indexed.", file=sys.stderr)
            return 1
        config_hash = build_cfg["config_hash"]
    finally:
        conn.close()

    # Resolve analyze_vendor: CLI flag takes precedence, config falls back.
    analyze_vendor = (
        False
        if getattr(args, "no_analyze_vendor", False)
        else getattr(args, "analyze_vendor", False) or cfg.llm.analyze_vendor
    )

    # Re-open connection for the analysis (uses its own transactions)
    conn = open_db(db_path)
    try:
        # Create CacheClient from config if available
        cc = None
        if cfg.cache_server and cfg.cache_server.url:
            try:
                from ..cache_client import CacheClient

                cc = CacheClient(
                    url=cfg.cache_server.url,
                    token=cfg.cache_server.token,
                    force=cfg.cache_server.force,
                    batch_size=cfg.cache_server.batch_size,
                )
            except (ValueError, TypeError, AttributeError) as e:
                logging.getLogger(__name__).warning("Failed to create CacheClient: %s", e)

        _build_llm_analysis(
            conn,
            config_hash,
            cfg.llm,
            db_path.parent,
            project_root=project_root,
            project_only=not analyze_vendor,
            cache_client=cc,
            retry_unparseable=True,
        )
        if cc:
            cc.close()
        _build_overrides(conn, config_hash, db_path.parent)
        conn.commit()

        # Update stored analyze_vendor so get_active_build reports
        # consistent state.  Only manifest_verification and description
        # are preserved from the existing row (not overwritten).
        upsert_build_config(
            conn, config_hash, project_id, build_cfg["compile_commands_path"],
            description=build_cfg["description"] if "description" in build_cfg.keys() else "",
            manifest_verification=build_cfg["manifest_verification"] if "manifest_verification" in build_cfg.keys() else "none",
            analyze_vendor=int(analyze_vendor),
        )
    finally:
        conn.close()

    print(f"LLM analysis complete for {project_root}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    """Print version and exit."""
    from .. import __version__

    print(f"fw-context-mcp {__version__}")
    return 0
