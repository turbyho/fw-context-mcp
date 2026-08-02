"""fw-context CLI — index and query firmware code intelligence."""

# ruff: noqa: I001 — lazy imports in functions must stay near use sites

from __future__ import annotations

import argparse
import logging
import sys

from .. import __version__


log = logging.getLogger(__name__)


class VerboseFormatter(logging.Formatter):
    """Structured output with phase headers for ``--verbose`` mode.

    Phase headers are emitted via ``log.info("", extra={"phase": "name"})``
    and rendered as framed separators.  Body messages are indented.  Phase
    results (single-line summaries) use ``extra={"result": True}`` to align
    timing info right after the phase header on the same line.
    """

    WIDTH: int = 60

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()

        # Phase header
        phase = getattr(record, "phase", None)
        if phase:
            header = f"── {phase} "
            padding = max(2, self.WIDTH - len(header))
            return "\n" + header + ("─" * padding)

        # Phase result (same-line summary)
        if getattr(record, "result", False):
            return f"  {msg}"

        # Regular message within a phase
        return f"  {msg}"


def main() -> None:
    """Entry point for the ``fw-context`` CLI — dispatches subcommands.

    Subcommands: index, search, list, status, init, export, cache, db,
    watch, finetune, analyze, version. Parses arguments via argparse and calls the
    corresponding ``cmd_*`` handler.
    """
    parser = argparse.ArgumentParser(prog="fw-context", description="Firmware code intelligence")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--version",
        action="version",
        version=f"fw-context-mcp {__version__}",
        help="Show version and exit",
    )
    sub = parser.add_subparsers(dest="cmd")

    from ._index import cmd_index  # noqa: I001
    p_index = sub.add_parser(
        "index",
        help="Build the symbol index from compile_commands.json (reuses existing compile_commands.json, builds only if missing)",
    )
    p_index.add_argument("-v", "--verbose", action="store_true")
    p_index.add_argument(
        "compile_commands",
        nargs="?",
        default=None,
        metavar="compile_commands.json",
        help="Use an explicit compile_commands.json (skips build)",
    )
    p_index.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_index.add_argument(
        "--build", action="store_true", help="Force a clean build and regenerate compile_commands.json"
    )
    p_index.add_argument(
        "--no-clean",
        action="store_true",
        help="With --build: skip clean, do incremental build (may produce incomplete compile_commands.json)",
    )
    p_index.add_argument("--vendor-paths", nargs="*", default=None,
                         help="Additional vendor/SDK directories (additive to auto-detection)")
    p_index.add_argument("--project-paths", nargs="*", default=None,
                         help="Manual project directories (overrides auto-detection)")
    p_index.add_argument("--name", metavar="NAME", help="Project name override")
    p_index.add_argument("--no-refs", action="store_true", help="Skip cross-reference indexing (on by default)")
    p_index.add_argument("--no-embeddings", action="store_true", dest="no_embeddings", help="Skip embedding generation")
    p_index.add_argument(
        "--embeddings",
        action="store_true",
        dest="embeddings",
        default=None,
        help="Generate symbol embeddings (default)",
    )
    p_index.add_argument(
        "--analyze",
        action="store_true",
        dest="analyze",
        default=False,
        help="Generate LLM-based symbol analysis (summary, inputs, outputs)",
    )
    p_index.add_argument("--no-analyze", action="store_true", dest="no_analyze", help="Skip LLM analysis generation")
    p_index.add_argument(
        "--analyze-vendor", action="store_true", dest="analyze_vendor", default=False,
        help="Also analyze vendor/SDK code (mbed-os, Zephyr, etc.)",
    )
    p_index.add_argument(
        "--no-analyze-vendor", action="store_true", dest="no_analyze_vendor",
        help="Skip vendor/SDK analysis (overrides config)",
    )
    p_index.add_argument(
        "--force",
        action="store_true",
        help="Force re-index of all files, embeddings, LLM analysis, overrides, and caches (skip mtime/checksum checks)",
    )
    p_index.add_argument(
        "--background",
        action="store_true",
        dest="background",
        default=False,
        help="Background reindex mode — skip build, validation, and dep-tracking fixes (safe for automated runs)",
    )
    p_index.set_defaults(func=cmd_index)

    from ._search import cmd_list, cmd_search, cmd_status  # noqa: I001
    p_search = sub.add_parser("search", help="Search indexed symbols")
    p_search.add_argument("-v", "--verbose", action="store_true")
    p_search.add_argument("query")
    p_search.add_argument("--project", metavar="DIR")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    from ._init import cmd_init  # noqa: I001
    p_init = sub.add_parser("init", help="Register fw-context with AI assistants and inject instructions")
    p_init.add_argument(
        "--tool", metavar="ID", help="Set up a specific tool (claude-code, opencode, kilocode, codex, cursor)"
    )
    p_init.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    p_init.add_argument("--force", action="store_true", help="Overwrite even when collisions are detected")
    p_init.add_argument(
        "--instructions-only", action="store_true", help="Only inject instructions, skip MCP registration"
    )
    p_init.add_argument(
        "--scope",
        choices=["all", "global", "project"],
        default="project",
        help="Which scope to inject (default: project — only the current project)",
    )
    p_init.add_argument("--project", metavar="DIR", help="Project root (for project-scoped targets)")
    p_init.add_argument(
        "--list-tools", action="store_true", help="List supported AI assistants and their detection status"
    )
    p_init.set_defaults(func=cmd_init, tool=None, dry_run=False, force=False, instructions_only=False, list_tools=False)

    p_list = sub.add_parser("list", help="List all indexed projects")
    p_list.add_argument("-v", "--verbose", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="Show index status for the current project")
    p_status.add_argument("--project", metavar="DIR")
    p_status.set_defaults(func=cmd_status)

    from ._export import cmd_analyze, cmd_export  # noqa: I001
    p_export = sub.add_parser("export", help="Export the symbol index as JSON")
    p_export.add_argument("--project", metavar="DIR")
    p_export.add_argument("-o", "--output", metavar="PATH", help="Output file (default: stdout)")
    p_export.add_argument("--no-refs", action="store_true", help="Omit cross-references")
    p_export.set_defaults(func=cmd_export)

    p_analyze = sub.add_parser("analyze", help="Re-run LLM symbol analysis on existing index (idempotent)")
    p_analyze.add_argument("-v", "--verbose", action="store_true")
    p_analyze.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_analyze.add_argument(
        "--analyze-vendor", action="store_true", dest="analyze_vendor", default=False,
        help="Also analyze vendor/SDK code (mbed-os, Zephyr, etc.)",
    )
    p_analyze.add_argument(
        "--no-analyze-vendor", action="store_true", dest="no_analyze_vendor",
        help="Skip vendor/SDK analysis (overrides config)",
    )
    p_analyze.set_defaults(func=cmd_analyze)

    # Cache management subcommands
    from ._cache import cmd_cache_clear, cmd_cache_push, cmd_cache_remote_init, cmd_cache_stats  # noqa: I001
    p_cache = sub.add_parser("cache", help="Manage LLM analysis cache (local + remote)")
    p_cache_sub = p_cache.add_subparsers(dest="cache_command")

    p_cache_stats = p_cache_sub.add_parser("stats", help="Show cache statistics")
    p_cache_stats.add_argument("--project", metavar="DIR", help="Project root for remote config (default: cwd)")
    p_cache_stats.add_argument("--remote", action="store_true", help="Show only remote server cache (Tier 2)")
    p_cache_stats.set_defaults(func=cmd_cache_stats)

    p_cache_clear = p_cache_sub.add_parser("clear", help="Delete cache entries")
    p_cache_clear.add_argument("--project", metavar="DIR", help="Project root for remote config (default: cwd)")
    p_cache_clear.add_argument(
        "--remote", action="store_true", help="Clear remote server cache for this project (Tier 2)"
    )
    p_cache_clear.add_argument("--all", action="store_true", help="Clear both local and remote")
    p_cache_clear.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_cache_clear.set_defaults(func=cmd_cache_clear)

    p_cache_push = p_cache_sub.add_parser("push", help="Push all local cache entries to remote server")
    p_cache_push.add_argument("--project", metavar="DIR", help="Project root for remote config (default: cwd)")
    p_cache_push.add_argument("--batch", type=int, metavar="N", help="Batch size (default: from config, 100)")
    p_cache_push.set_defaults(func=cmd_cache_push)

    p_cache_remote = p_cache_sub.add_parser(
        "remote-init", help="Interactive setup: configure remote cache URL and token"
    )
    p_cache_remote.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_cache_remote.set_defaults(func=cmd_cache_remote_init)

    # ── db management subcommands ──
    from ._db import cmd_db, cmd_db_cleanup, cmd_db_delete, cmd_db_list, cmd_db_stats  # noqa: I001
    p_db = sub.add_parser("db", help="Manage the index database (builds, stats, cleanup)")
    p_db_sub = p_db.add_subparsers(dest="db_command")

    p_db_list = p_db_sub.add_parser("list", help="List all builds for a project")
    p_db_list.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_db_list.add_argument("-v", "--verbose", action="store_true", help="Show full config_hash (64 chars)")
    p_db_list.set_defaults(func=cmd_db_list)

    p_db_stats = p_db_sub.add_parser("stats", help="Show detailed statistics for a build")
    p_db_stats.add_argument("config_hash", help="Build config hash (first 12+ chars)")
    p_db_stats.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_db_stats.set_defaults(func=cmd_db_stats)

    p_db_delete = p_db_sub.add_parser("delete", help="Delete a specific build or all builds (--all)")
    p_db_delete.add_argument("config_hash", nargs="?", default=None, help="Build config hash to delete")
    p_db_delete.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_db_delete.add_argument("--force", action="store_true", help="Allow deleting the active build")
    p_db_delete.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_db_delete.add_argument("--all", action="store_true", help="Delete the entire index (all builds)")
    p_db_delete.set_defaults(func=cmd_db_delete)

    p_db_cleanup = p_db_sub.add_parser("cleanup", help="Remove orphaned compile_commands artifacts")
    p_db_cleanup.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_db_cleanup.set_defaults(func=cmd_db_cleanup)

    # Without a subcommand, show help
    p_db.set_defaults(func=cmd_db)

    from ._watch import cmd_watch, cmd_watch_restart, cmd_watch_status  # noqa: I001
    p_watch = sub.add_parser("watch", help="Manage the background watcher daemon")
    p_watch_sub = p_watch.add_subparsers(dest="watch_command")
    p_watch_status = p_watch_sub.add_parser("status", help="Show daemon status")
    p_watch_status.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_watch_status.set_defaults(func=cmd_watch_status)
    p_watch_restart = p_watch_sub.add_parser("restart", help="Restart the daemon")
    p_watch_restart.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_watch_restart.set_defaults(func=cmd_watch_restart)
    # Without a subcommand, show help
    p_watch.set_defaults(func=cmd_watch)

    from ._export import cmd_version  # noqa: I001
    p_version = sub.add_parser("version", help="Show version information")
    p_version.set_defaults(func=cmd_version)

    from ._finetune import cmd_finetune  # noqa: I001
    p_finetune = sub.add_parser("finetune", help="Self-supervised fine-tune the embedding model on project code")
    p_finetune.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_finetune.add_argument("--sample-limit", type=int, default=2000, metavar="N",
                            help="Max synthetic queries to mine (default: 2000)")
    p_finetune.add_argument("--epochs", type=int, default=3, metavar="N",
                            help="Training epochs (default: 3)")
    p_finetune.add_argument("--batch-size", type=int, default=16, metavar="N",
                            help="Training batch size (default: 16)")
    p_finetune.set_defaults(func=cmd_finetune)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)
    try:
        sys.exit(args.func(args))
    except BaseException as exc:
        from ..utils import is_fatal
        if is_fatal(exc):
            raise
        # ProjectNotInitializedError — lazy import to avoid circular deps
        from ..config.settings import ProjectNotInitializedError

        if isinstance(exc, ProjectNotInitializedError):
            print(f"error: {exc}", file=sys.stderr)
        else:
            print(f"fw-context: error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
