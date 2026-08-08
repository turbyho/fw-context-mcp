"""``fw-cache-admin`` — manage projects, tokens, and the cache database.

All admin commands connect directly to PostgreSQL (not via HTTP).  They
require ``FW_CACHE_DB_URL`` in the environment and an admin token in
``FW_CACHE_ADMIN_TOKEN`` (validated against the first project created
by ``fw-cache-server init``, which has ``can_overwrite=true``).

Why direct PG access (not HTTP)?
--------------------------------
Admin operations (create/delete projects, revoke tokens, purge cache)
must work *even when the server is down*.  Direct database access also
avoids exposing these privileged operations over HTTP — only server
processes and the admin tool touch the meta database directly.

Token permission model
----------------------
* ``can_read``   — can call ``POST /cache/batch`` (batch lookup)
* ``can_write``  — can call ``PUT /cache/batch`` (batch write)
* ``can_overwrite`` — can send ``X-Cache-Overwrite: true`` (re-analyze symbols)
* ``is_admin``    — admin token (not scoped to a project, ``project_id IS NULL``)
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
    """Return ``(db_url, admin_token)`` from environment, exiting on failure.

    Both env vars are required for all admin commands — the tool cannot
    function without database access and an auth token.
    """
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
    # NOTE: wrapper drops the first arg (backend) — functools.wraps copies __name__/__doc__ but the signature differs intentionally
    """Decorator that wraps async admin command handlers.

    Handles the repeated boilerplate: DB connection, admin auth, and
    ``asyncio.run``.  The decorated function receives ``(backend, args)``
    and returns an exit code.  Also attaches ``admin_project_id`` to args
    so command handlers know which project the admin token is scoped to.

    Why a decorator?
    ----------------
    Every admin command needs the same setup (connect → validate →
    execute → close). Without this decorator, each of the 7 command
    handlers would duplicate ~15 lines of boilerplate.
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
                if perms is None or not perms.get("is_admin", False):
                    print("Error: admin token is invalid or lacks admin permission", file=sys.stderr)
                    return 1
                args._admin_project_id = perms.get("project_id")
                return await fn(backend, args)
            finally:
                # Always close — even on auth failure, the pool must be
                # released before the process exits.
                await backend.close()

        return asyncio.run(_run())

    return wrapper


@admin_command
async def cmd_project_create(backend, args: argparse.Namespace) -> int:
    """Create a new project and generate its first write+overwrite token.

    Args:
        backend: Connected CacheBackend instance.
        args: Namespace with ``project_id`` (UUID4 hex) and optional ``description``.

    Why validate UUID4 hex format?
    ------------------------------
    Project IDs come from ``fw-context init`` which generates UUID4.
    Rejecting non-32-hex strings early prevents silent corruption —
    a typo'd project ID would create an orphan project nobody can match.
    """
    pid = args.project_id
    if len(pid) != 32 or not all(c in "0123456789abcdef" for c in pid):
        print(f"Error: project ID must be 32 hex characters (UUID4), got: {pid}", file=sys.stderr)
        print("  Run 'fw-context init' to generate a valid project ID.", file=sys.stderr)
        return 1
    token = await backend.create_project(pid, args.description or "")
    if token is None:
        print(f"Error: project '{pid}' already exists", file=sys.stderr)
        return 1
    print(f"Project '{pid}' created")
    print(f"  Admin token: {token}")
    return 0


@admin_command
async def cmd_project_remove(backend, args: argparse.Namespace) -> int:
    """Remove a project and cascade-delete its tokens. Cache entries stay intact.

    Args:
        backend: Connected CacheBackend instance.
        args: Namespace with ``project_id`` and optional ``--confirm``.

    Why protect self-deletion?
    --------------------------
    Removing the project that your own admin token belongs to would
    leave you without any admin access — the token's project_id is
    NULL for admin tokens, but scoped admin tokens (unusual) would
    lose their project reference.
    """
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
    """List all registered projects with creation timestamps.

    Args:
        backend: Connected CacheBackend instance.
        args: Unused (satisfies decorator contract).
    """
    projects = await backend.list_projects()
    if not projects:
        print("No projects found")
        return 0
    for p in projects:
        print(f"  {p['id']}  ({p.get('description', '')})  created {p['created_at']}")
    return 0


@admin_command
async def cmd_token_create(backend, args: argparse.Namespace) -> int:
    """Create an access token for a project with specified permissions.

    Args:
        backend: Connected CacheBackend instance.
        args: Namespace with ``project_id``, ``--write``, ``--overwrite``,
              and optional ``--description``.

    Why separate read/write/overwrite tokens?
    -----------------------------------------
    Most developers only need read access (they consume cached analyses).
    Write tokens go to CI runners and the project maintainer who runs
    ``fw-context index --analyze``.  Overwrite tokens are rare — only
    for re-indexing after SDK upgrades.  This separation of concerns
    limits blast radius: a leaked read-only token cannot corrupt the
    shared cache.
    """
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
    """Revoke an access token by its plain-text value.

    Args:
        backend: Connected CacheBackend instance.
        args: Namespace with ``token`` (the plain-text token to revoke).

    Why use plain-text token (not hash ID)?
    ---------------------------------------
    Tokens are created as plain-text and shown once.  The admin tool
    accepts the plain-text token to avoid requiring the admin to store
    or look up hash IDs.  The plain-text is SHA‑256 hashed server-side
    before lookup — no token is ever stored in plain text.
    """
    ok = await backend.revoke_token(args.token)
    if not ok:
        print("Error: token not found or already revoked", file=sys.stderr)
        return 1
    print("Token revoked")
    return 0


@admin_command
async def cmd_token_list(backend, args: argparse.Namespace) -> int:
    """List all tokens for a project.

    Args:
        backend: Connected CacheBackend instance.
        args: Namespace with ``project_id``.

    Why show only first 8 hex chars of the hash?
    ---------------------------------------------
    Token hashes are 64‑byte SHA‑256 digests stored as BYTEA.  Showing
    the full hash is unnecessary noise — the first 8 hex chars are
    enough to identify a token in logs and audits.  The plain-text
    token is NEVER shown after creation.
    """
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
    """Show cache statistics: total entries, timestamp range, model breakdown.

    Args:
        backend: Connected CacheBackend instance.
        args: Unused (satisfies decorator contract).

    Why show model breakdown?
    -------------------------
    Different LLM models produce different quality analyses.  Knowing
    the model distribution helps decide when to re-analyze after
    upgrading to a better model — entries from an older model can be
    purged or overwritten.
    """
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
    """Delete cache entries older than a given number of days.

    Args:
        backend: Connected CacheBackend instance.
        args: Namespace with ``--older-than`` (e.g. ``90d`` or ``90``).

    Why purge instead of TTL-based expiration?
    ------------------------------------------
    PostgreSQL does not have built-in row TTL like Redis.  Periodic
    purging via cron is simpler to reason about than trigger-based
    expiration — the admin runs ``fw-cache-admin cache purge --older-than 180d``
    monthly via cron or systemd timer.
    """
    days = parse_days(args.older_than)
    print(f"Purging entries older than {days} days...")
    deleted = await backend.cache_purge_older_than(days)
    print(f"Deleted {deleted} entries")
    return 0


def parse_days(s: str) -> int:
    """Parse a duration string like ``90d`` or ``30`` into days.

    Accepts both ``90d`` (explicit) and ``90`` (implicit days) for
    compatibility with typical CLI conventions.  The ``d`` suffix is
    optional — plain integers are treated as days.
    """
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
    """Entry point for ``fw-cache-admin`` CLI.

    Why argparse sub-sub-commands?
    ------------------------------
    The tool has three top-level groups (project, token, cache) each with
    sub-commands (create, remove, list, etc.).  Two-level subparsers
    provide discoverable help: ``fw-cache-admin project --help`` shows
    project sub-commands before any run.
    """
    parser = argparse.ArgumentParser(
        prog="fw-cache-admin",
        description="Manage fw-context cache server projects, tokens, and cache",
    )
    sub = parser.add_subparsers(dest="command")

    # -- project --
    p_proj = sub.add_parser("project", help="Manage projects")
    p_proj_sub = p_proj.add_subparsers(dest="project_command")

    p_create = p_proj_sub.add_parser("create", help="Create a new project")
    p_create.add_argument("project_id", help="Project ID (UUID4 hex from fw-context init)")
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
        # Subcommand was given but no sub-subcommand — print the sub-help
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
