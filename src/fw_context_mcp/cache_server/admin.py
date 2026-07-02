"""``fw-cache-admin`` — manage projects, tokens, and the cache database.

All admin commands connect directly to PostgreSQL (not via HTTP).  They
require ``FW_CACHE_DB_URL`` in the environment and an admin token in
``FW_CACHE_ADMIN_TOKEN`` (validated against the first project created
by ``fw-cache-server init``, which has ``can_overwrite=true``).
"""

from __future__ import annotations

import argparse
import os
import sys


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


async def _verify_admin(backend: object, admin_token: str) -> bool:
    from .backend import CacheBackend
    b: CacheBackend = backend  # type: ignore[assignment]
    perms = await b.validate_token(admin_token)
    if perms is None or not perms.get("can_overwrite", False):
        print("Error: admin token is invalid or lacks can_overwrite permission", file=sys.stderr)
        return False
    return True


def cmd_project_create(args: argparse.Namespace) -> int:
    import asyncio

    from .backend import CacheBackend

    db_url, admin_token = _check_env()

    async def _run() -> int:
        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            if not await _verify_admin(backend, admin_token):
                return 1
            token = await backend.create_project(args.project_id, args.description or "")
            if token is None:
                print(f"Error: project '{args.project_id}' already exists", file=sys.stderr)
                return 1
            print(f"Project '{args.project_id}' created")
            print(f"  Admin token: {token}")
        finally:
            await backend.close()
        return 0

    return asyncio.run(_run())


def cmd_project_remove(args: argparse.Namespace) -> int:
    import asyncio

    from .backend import CacheBackend

    db_url, admin_token = _check_env()

    async def _run() -> int:
        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            if not await _verify_admin(backend, admin_token):
                return 1
            if not args.confirm:
                print(f"Dry-run: would delete project '{args.project_id}' (cache stays intact)")
                return 0
            ok = await backend.remove_project(args.project_id)
            if not ok:
                print(f"Error: project '{args.project_id}' not found", file=sys.stderr)
                return 1
            print(f"Project '{args.project_id}' removed")
        finally:
            await backend.close()
        return 0

    return asyncio.run(_run())


def cmd_project_list(args: argparse.Namespace) -> int:
    import asyncio

    from .backend import CacheBackend

    db_url, admin_token = _check_env()

    async def _run() -> int:
        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            if not await _verify_admin(backend, admin_token):
                return 1
            projects = await backend.list_projects()
            if not projects:
                print("No projects found")
                return 0
            for p in projects:
                print(f"  {p['id']}  ({p.get('description', '')})  created {p['created_at']}")
        finally:
            await backend.close()
        return 0

    return asyncio.run(_run())


def cmd_token_create(args: argparse.Namespace) -> int:
    import asyncio

    from .backend import CacheBackend

    db_url, admin_token = _check_env()
    can_write = args.write or args.overwrite
    can_overwrite = args.overwrite

    async def _run() -> int:
        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            if not await _verify_admin(backend, admin_token):
                return 1
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
        finally:
            await backend.close()
        return 0

    return asyncio.run(_run())


def cmd_token_revoke(args: argparse.Namespace) -> int:
    import asyncio

    from .backend import CacheBackend

    db_url, admin_token = _check_env()

    async def _run() -> int:
        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            if not await _verify_admin(backend, admin_token):
                return 1
            ok = await backend.revoke_token(args.token)
            if not ok:
                print("Error: token not found or already revoked", file=sys.stderr)
                return 1
            print("Token revoked")
        finally:
            await backend.close()
        return 0

    return asyncio.run(_run())


def cmd_token_list(args: argparse.Namespace) -> int:
    import asyncio

    from .backend import CacheBackend

    db_url, admin_token = _check_env()

    async def _run() -> int:
        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            if not await _verify_admin(backend, admin_token):
                return 1
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
        finally:
            await backend.close()
        return 0

    return asyncio.run(_run())


def cmd_cache_stats(args: argparse.Namespace) -> int:
    import asyncio

    from .backend import CacheBackend

    db_url, admin_token = _check_env()

    async def _run() -> int:
        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            if not await _verify_admin(backend, admin_token):
                return 1
            stats = await backend.cache_stats()
            print(f"Total entries:  {stats['total_entries']}")
            print(f"Newest entry:   {stats['newest_entry'] or 'n/a'}")
            print(f"Oldest entry:   {stats['oldest_entry'] or 'n/a'}")
            if stats["models"]:
                print("By model:")
                for model, cnt in sorted(stats["models"].items()):
                    print(f"  {model}: {cnt}")
        finally:
            await backend.close()
        return 0

    return asyncio.run(_run())


def cmd_cache_purge(args: argparse.Namespace) -> int:
    import asyncio

    from .backend import CacheBackend

    db_url, admin_token = _check_env()

    async def _run() -> int:
        backend = CacheBackend(db_url)
        try:
            await backend.connect()
            if not await _verify_admin(backend, admin_token):
                return 1
            days = parse_days(args.older_than)
            print(f"Purging entries older than {days} days...")
            deleted = await backend.cache_purge_older_than(days)
            print(f"Deleted {deleted} entries")
        finally:
            await backend.close()
        return 0

    return asyncio.run(_run())


def parse_days(s: str) -> int:
    """Parse a duration string like ``90d`` or ``30`` into days."""
    s = s.strip().lower()
    if s.endswith("d"):
        s = s[:-1]
    return int(s)


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
    p_t_create.add_argument("--readonly", action="store_true", default=True, help="Read-only token (default)")
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

    sys.exit(args.func(args))
