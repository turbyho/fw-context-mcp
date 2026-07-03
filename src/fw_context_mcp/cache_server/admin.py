"""``fw-cache-admin`` — manage projects, tokens, and the cache database.

All admin commands connect directly to PostgreSQL (not via HTTP).  They
require ``FW_CACHE_DB_URL`` in the environment and an admin token in
``FW_CACHE_ADMIN_TOKEN`` (validated against the first project created
by ``fw-cache-server init``, which has ``can_overwrite=true``).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import os
import sys
from collections.abc import Callable
from typing import Any


def _check_env() -> tuple[str, str]:
    db_url = os.environ.get("FW_CACHE_DB_URL", "")
    admin_token = os.environ.get("FW_CACHE_ADMIN_TOKEN", "")
    if not db_url:
        print("Error: FW_CACHE_DB_URL environment variable is required", file=sys.stderr)
        sys.exit(1)
    if not admin_token:
        print("Error: FW_CACHE_ADMIN_TOKEN environment variable is required", file=sys.stderr)
        sys.exit(1)
    return db_url, admin_token




def admin_command(fn: Callable[[Any, argparse.Namespace], Any]) -> Callable[[argparse.Namespace], int]:
    """Decorator that wraps async admin command handlers.

    Handles the repeated boilerplate: DB connection, admin auth, and
    ``asyncio.run``.  The decorated function receives ``(backend, args)``
    and returns an exit code.  Also attaches ``admin_project_id`` to args
    so command handlers know which project the admin token is scoped to.
    """

    @functools.wraps(fn)
    def wrapper(args: argparse.Namespace) -> int:
        from .backend import CacheBackend

        db_url, admin_token = _check_env()

        async def _run() -> int:
            backend = CacheBackend(db_url)
            try:
                await backend.connect()
                perms = await backend.validate_token(admin_token)
                if perms is None or not perms.get("can_overwrite", False):
                    print("Error: admin token is invalid or lacks can_overwrite permission", file=sys.stderr)
                    return 1
                args._admin_project_id = perms.get("project_id")
                return await fn(backend, args)
            finally:
                await backend.close()

        return asyncio.run(_run())

    return wrapper


@admin_command
async def cmd_project_create(backend, args: argparse.Namespace) -> int:
    token = await backend.create_project(args.project_id, args.description or "")
    if token is None:
        print(f"Error: project '{args.project_id}' already exists", file=sys.stderr)
        return 1
    print(f"Project '{args.project_id}' created")
    print(f"  Admin token: {token}")
    return 0


@admin_command
async def cmd_project_remove(backend, args: argparse.Namespace) -> int:
    admin_proj = getattr(args, "_admin_project_id", None)
    if admin_proj == args.project_id:
        print(f"Error: cannot remove project '{args.project_id}' — your admin token is scoped to it", file=sys.stderr)
        return 1
    if not args.confirm:
        print(f"Dry-run: would delete project '{args.project_id}' (all tokens cascade-deleted, cache stays intact)")
        print("  Add --confirm to execute.")
        return 0
    ok = await backend.remove_project(args.project_id)
    if not ok:
        print(f"Error: project '{args.project_id}' not found", file=sys.stderr)
        return 1
    print(f"Project '{args.project_id}' removed (tokens deleted, cache preserved)")
    return 0


@admin_command
async def cmd_project_list(backend, args: argparse.Namespace) -> int:
    projects = await backend.list_projects()
    if not projects:
        print("No projects found")
        return 0
    for p in projects:
        print(f"  {p['id']}  ({p.get('description', '')})  created {p['created_at']}")
    return 0


@admin_command
async def cmd_token_create(backend, args: argparse.Namespace) -> int:
    can_write = args.write or args.overwrite
    can_overwrite = args.overwrite

    token = await backend.create_token(
        args.project_id,
        can_read=True,
        can_write=can_write,
        can_overwrite=can_overwrite,
        description=args.description or "",
    )
    kind = "read-only"
    if can_overwrite:
        kind = "write+overwrite"
    elif can_write:
        kind = "write"
    print(f"Token created for '{args.project_id}' ({kind})")
    print(f"  Token: {token}")
    return 0


@admin_command
async def cmd_token_revoke(backend, args: argparse.Namespace) -> int:
    ok = await backend.revoke_token(args.token)
    if not ok:
        print("Error: token not found or already revoked", file=sys.stderr)
        return 1
    print("Token revoked")
    return 0


@admin_command
async def cmd_token_list(backend, args: argparse.Namespace) -> int:
    tokens = await backend.list_tokens(args.project_id)
    if not tokens:
        print(f"No tokens for project '{args.project_id}'")
        return 0
    for t in tokens:
        status = "active" if t["revoked_at"] is None else f"revoked {t['revoked_at']}"
        perms = []
        if t["can_read"]:
            perms.append("R")
        if t["can_write"]:
            perms.append("W")
        if t["can_overwrite"]:
            perms.append("O")
        desc = t.get("description", "") or ""
        print(f"  id={t['id']}  hash={t['token_hash'][:8]}...  perms={','.join(perms)}  "
              f"{desc}  [{status}]")
    return 0


@admin_command
async def cmd_cache_stats(backend, args: argparse.Namespace) -> int:
    stats = await backend.cache_stats()
    print(f"Total entries:  {stats['total_entries']}")
    print(f"Newest entry:   {stats['newest_entry'] or 'n/a'}")
    print(f"Oldest entry:   {stats['oldest_entry'] or 'n/a'}")
    if stats["models"]:
        print("By model:")
        for model, cnt in sorted(stats["models"].items()):
            print(f"  {model}: {cnt}")
    return 0


@admin_command
async def cmd_cache_purge(backend, args: argparse.Namespace) -> int:
    days = parse_days(args.older_than)
    print(f"Purging entries older than {days} days...")
    deleted = await backend.cache_purge_older_than(days)
    print(f"Deleted {deleted} entries")
    return 0


def parse_days(s: str) -> int:
    """Parse a duration string like ``90d`` or ``30`` into days."""
    s = s.strip().lower()
    if s.endswith("d"):
        s = s[:-1]
    try:
        days = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid duration: {s!r} — expected positive integer, e.g. 90 or 90d"
        ) from None
    if days < 1:
        raise argparse.ArgumentTypeError(f"duration must be at least 1 day, got {days}")
    return days


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fw-cache-admin",
        description="Manage fw-context cache server projects, tokens, and cache",
    )
    sub = parser.add_subparsers(dest="command")

    # -- project --
    p_proj = sub.add_parser("project", help="Manage projects")
    p_proj_sub = p_proj.add_subparsers(dest="project_command")

    p_create = p_proj_sub.add_parser("create", help="Create a new project")
    p_create.add_argument("project_id", help="Project ID (e.g. my-firmware)")
    p_create.add_argument("--description", help="Optional description")
    p_create.set_defaults(func=cmd_project_create)

    p_remove = p_proj_sub.add_parser("remove", help="Remove a project (cache stays)")
    p_remove.add_argument("project_id", help="Project ID")
    p_remove.add_argument("--confirm", action="store_true", help="Confirm deletion")
    p_remove.set_defaults(func=cmd_project_remove)

    p_list = p_proj_sub.add_parser("list", help="List all projects")
    p_list.set_defaults(func=cmd_project_list)

    # -- token --
    p_tok = sub.add_parser("token", help="Manage tokens")
    p_tok_sub = p_tok.add_subparsers(dest="token_command")

    p_t_create = p_tok_sub.add_parser("create", help="Create a token for a project")
    p_t_create.add_argument("project_id", help="Project ID")
    p_t_create.add_argument("--write", action="store_true", help="Write token (no overwrite)")
    p_t_create.add_argument("--overwrite", action="store_true", help="Write token with overwrite permission")
    p_t_create.add_argument("--description", help="Optional description (e.g. developer name)")
    p_t_create.set_defaults(func=cmd_token_create)

    p_t_revoke = p_tok_sub.add_parser("revoke", help="Revoke a token")
    p_t_revoke.add_argument("token", help="Token to revoke")
    p_t_revoke.set_defaults(func=cmd_token_revoke)

    p_t_list = p_tok_sub.add_parser("list", help="List tokens for a project")
    p_t_list.add_argument("project_id", help="Project ID")
    p_t_list.set_defaults(func=cmd_token_list)

    # -- cache --
    p_cache = sub.add_parser("cache", help="Cache management")
    p_cache_sub = p_cache.add_subparsers(dest="cache_command")

    p_stats = p_cache_sub.add_parser("stats", help="Show cache statistics")
    p_stats.set_defaults(func=cmd_cache_stats)

    p_purge = p_cache_sub.add_parser("purge", help="Purge old cache entries")
    p_purge.add_argument("--older-than", required=True, help="Delete entries older than N days (e.g. 90d)")
    p_purge.set_defaults(func=cmd_cache_purge)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)
    if not hasattr(args, "func"):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction) and action.dest == "command":
                subparser = action.choices.get(args.command)
                if subparser:
                    subparser.print_help()
                    sys.exit(1)
        parser.print_help()
        sys.exit(1)

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
