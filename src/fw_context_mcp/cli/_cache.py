"""``fw-context cache`` — LLM analysis cache management (local + remote).

Two-tier caching architecture: local global cache (Tier 1) on disk for fast
lookup, remote cache server (Tier 2) for team-wide sharing.  Each tier
stores pre-computed LLM symbol analysis keyed by content hash, so identical
symbol bodies produce identical analysis across all projects and machines.

WHY two tiers: local cache avoids network latency for repeated analysis
runs; remote cache enables team members to benefit from each other's
analyzed symbols without re-running expensive LLM calls.  The content hash
makes cache entries deterministic — same source = same analysis.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ..cache_client import CacheClient


def _stats_failure(cc: CacheClient) -> str:
    """Say why ``cc.stats()`` gave None, as the end of a sentence about the server.

    ``stats()`` gives None for four causes, and each needs a different
    action from the user.  "not reachable" for all four sent the user to
    look for a network fault when the server ran.

    The server gives 429 only to a request whose token failed (its
    middleware limits failed authentication attempts), thus the message for
    429 names the token first.
    """
    if cc.auth_rejected:
        return "rejected the token (401/403) — check [cache_server] token"
    if cc.rate_limited:
        return (
            "rejected the token and limits the failed attempts (429) — "
            "check [cache_server] token, then wait and try again"
        )
    if cc.invalid_response:
        return (
            "answered with a body that is not a JSON object — a proxy can be "
            "in front of the server, see the log for the content type"
        )
    return "is not reachable — check the URL and the network"


def cmd_cache_stats(args: argparse.Namespace) -> int:
    """Print cache statistics: entry counts, models, per-project coverage.

    Reports Tier 1 (local) always, Tier 2 (remote) only when ``--remote``
    is given.  For the remote tier, also shows how many of THIS project's
    analysis entries are already cached on the server.
    """
    from ..cache_client import CacheClient, local_cache_stats
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..utils import resolve_project_root

    show_local = not args.remote
    project_root = resolve_project_root(args.project) if hasattr(args, "project") and args.project else Path.cwd()

    # Tier 1: local global cache
    if show_local:
        local_stats = local_cache_stats()
        print(f"Local cache (Tier 1): {local_stats['total_entries']} entries  ({local_stats['path']})")

    # Tier 2: remote cache
    if args.remote:
        cfg = load_config(project_root=project_root)
        cs = cfg.cache_server
        if not cs or not cs.url:
            print("Remote cache (Tier 2): not configured (set [cache_server] in config)")
            return 0

        cc = CacheClient(url=cs.url, token=cs.token)
        try:
            remote_stats = cc.stats()
            if remote_stats is None:
                # Exit 0 as before: this command shows a status, and a
                # server that does not answer is one.  The text must be true.
                print(f"Remote cache (Tier 2): {cs.url}")
                print(f"  The server {_stats_failure(cc)}")
                return 0

            print(f"Remote cache (Tier 2): {cs.url}")
            total = remote_stats.get("total_entries", 0)
            newest = remote_stats.get("newest_entry", "")
            models = remote_stats.get("models", {})

            print(f"  Total entries: {total}")
            if newest:
                print(f"  Newest entry:  {newest}")
            if models:
                print("  Models:")
                for model, cnt in sorted(models.items()):
                    pct = f" ({cnt / total * 100:.0f}%)" if total else ""
                    print(f"    {model}: {cnt}{pct}")

            # Per-project breakdown: batch-lookup all sym's hash in this project
            project_id = derive_project_id(project_root)
            db_path = cfg.index.db_dir / project_id / "index.db"
            if db_path.exists():
                from ..indexer.db import open_db

                conn = open_db(db_path)
                hashes: list[str] = []
                try:
                    hashes = [
                        r[0]
                        for r in conn.execute(
                            "SELECT DISTINCT content_hash FROM llm_analysis WHERE content_hash != ''"
                        ).fetchall()
                    ]
                finally:
                    conn.close()
                if hashes:
                    hits = cc.batch_get(hashes)
                    cached_count = sum(1 for v in hits.values() if v is not None)
                    print(f"  Project cache: {cached_count}/{len(hashes)} cached ({project_id})")
        finally:
            cc.close()

    return 0


def cmd_cache_push(args: argparse.Namespace) -> int:
    """Upload local Tier 1 entries to the remote Tier 2 cache server.

    By default only the entries that the server does not have are stored:
    the server keeps an existing entry (first write wins, see
    ``cache_server/app.py``), so every local entry is sent and the server
    reports how many it inserted.  ``--overwrite`` sends the
    ``X-Cache-Overwrite`` header and replaces the server's entries.

    WHY not overwrite by default: this used to overwrite always, on the
    grounds that a content hash guarantees an identical analysis.  It does
    not.  The model's output varies from run to run, and the hash covers the
    body, name, signature and docstring — not the prompt.  Observed: 16
    entries with the same hash and model held a different text locally and
    on the server.  A push must not replace a team's entries without asking.

    WHY the capability check first: with the header, a token without
    ``can_overwrite`` gets 403, the client marks the token read-only and
    skips every later chunk — and the command used to report
    ``Done: 0/N`` with exit 0.  A refused write is now an error.

    WHY batch push: pushing entries one-by-one would incur HTTP overhead
    per entry; batching by ``batch_size`` (from config or ``--batch``)
    amortizes the connection cost across multiple entries.

    WHY progress reporting per batch: users need feedback during large
    pushes (thousands of entries) to know the operation is still alive.
    """
    from ..cache_client import CacheClient, get_local_cache_db
    from ..config import load as load_config
    from ..utils import resolve_project_root

    project_root = resolve_project_root(args.project) if hasattr(args, "project") else None
    if not project_root:
        print("error: --project required for remote cache push", file=sys.stderr)
        return 1

    cfg = load_config(project_root=project_root)
    cs = cfg.cache_server
    if not cs or not cs.url:
        print("error: [cache_server] not configured", file=sys.stderr)
        return 1

    local_db = get_local_cache_db(readonly=True)
    try:
        rows = local_db.execute(
            "SELECT content_hash, summary, inputs, outputs, model FROM llm_analysis_cache"
        ).fetchall()
        total = len(rows)
        if total == 0:
            print("Local cache is empty — nothing to push.")
            return 0

        overwrite = bool(getattr(args, "overwrite", False))
        batch_size = args.batch or cs.batch_size
        # argparse refuses a --batch below 1, but the config value comes here
        # as it is: the config loader keeps a bad value and does not fail,
        # thus a bad [cache_server] section does not stop other commands.  A
        # negative step gave an empty loop and a false "Done", and 0 gave a
        # ValueError from range().
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            print(
                f"error: [cache_server] batch_size must be a positive integer, not {batch_size!r}",
                file=sys.stderr,
            )
            return 1
        cc = CacheClient(url=cs.url, token=cs.token, force=overwrite, batch_size=batch_size)
        try:
            caps = cc.stats()
            if caps is None:
                print(f"error: remote cache {cs.url} {_stats_failure(cc)}", file=sys.stderr)
                return 1
            if not caps.get("can_write"):
                print("error: the cache token is read-only (can_write=false)", file=sys.stderr)
                return 1
            if overwrite and not caps.get("can_overwrite"):
                print(
                    "error: --overwrite needs a token with can_overwrite; "
                    "run without it to upload only the missing entries",
                    file=sys.stderr,
                )
                return 1

            verb = "written" if overwrite else "inserted"
            written = 0
            # The step is the size that the client uses, not the requested
            # one: the client limits it to what the server takes.  With a
            # larger step one batch_put sent more than one request, and the
            # progress and error counts no longer agreed with the requests.
            step = cc.batch_size
            for i in range(0, total, step):
                chunk = rows[i : i + step]
                entries = [
                    {"hash": r[0], "summary": r[1], "inputs": r[2], "outputs": r[3], "model": r[4]} for r in chunk
                ]
                n = cc.batch_put(entries)
                if cc.put_failures:
                    print(
                        f"error: the server did not accept the write after {i}/{total} entries "
                        f"({written} {verb}) — see the log for the HTTP status",
                        file=sys.stderr,
                    )
                    return 1
                written += n
                print(f"  [{i + len(chunk)}/{total}] {verb} {n}")
            if overwrite:
                print(f"Done: {written}/{total} entries written to {cs.url}")
            else:
                print(f"Done: {written} inserted, {total - written} already on {cs.url}")
        finally:
            cc.close()
    finally:
        local_db.close()

    return 0


def cmd_cache_remote_init(args: argparse.Namespace) -> int:
    """Interactive wizard: configure remote cache server connection.

    Prompts for URL and token, verifies the connection, and writes the
    [cache_server] section to the global config (~/.fw-context/config.toml).

    WHY interactive wizard: the remote cache server URL and token are
    deployment-specific — they differ per organization and per user role.
    An interactive prompt is the least error-prone way to collect them,
    especially the token which should never appear in shell history.

    WHY token in separate file (not config.toml): config.toml is shared
    via version control; the token is a secret.  The token is stored in
    ``~/.fw-context/.cache_token`` with ``0o600`` permissions, created via
    atomic write (temp file + rename) to prevent window-of-vulnerability
    where the file exists with world-readable permissions.
    """
    from ..config.settings import _ensure_global_config

    # Resolve global config
    config_path = _ensure_global_config()

    # Read existing config
    existing = config_path.read_text(encoding="utf-8")

    # Show current config if any
    current_url = ""
    url_match = re.search(r'\[cache_server\].*?\nurl\s*=\s*"([^"]*)"', existing, re.DOTALL)
    if url_match:
        current_url = url_match.group(1)

    if current_url:
        print(f"Current remote cache: {current_url}")
    else:
        print("No remote cache configured.")

    # --- Step 1: URL ---
    print()
    url_default = current_url or "https://fw-cache.example.com"
    url_input = input(f"Cache server URL [{url_default}]: ").strip()
    url = url_input if url_input else url_default

    # --- Step 2: Token ---
    print()
    token_input = input("Token (paste your read or read+write token): ").strip()
    if not token_input:
        print("error: token is required", file=sys.stderr)
        return 1
    token = token_input

    # --- Step 3: Verify connection ---
    print(f"\nVerifying connection to {url} ...")
    try:
        with httpx.Client(base_url=url, timeout=10.0) as client:
            # Check health first
            health_resp = client.get("/health")
            if health_resp.status_code != 200:
                print(f"error: server returned {health_resp.status_code}", file=sys.stderr)
                return 1

            # Check auth
            auth_resp = client.get(
                "/cache/stats",
                headers={"Authorization": f"Bearer {token}"},
            )
            if auth_resp.status_code == 401:
                print("error: authentication failed (401) — check your token", file=sys.stderr)
                return 1
            if auth_resp.status_code == 403:
                print("error: access denied (403) — token may lack permissions", file=sys.stderr)
                return 1
            if auth_resp.status_code == 429:
                # The middleware gives 429 only after failed authentication
                # attempts from this address, thus the token is the cause.
                print(
                    "error: the server limits the failed authentication attempts (429) — "
                    "check your token, then wait and try again",
                    file=sys.stderr,
                )
                return 1
            if auth_resp.status_code != 200:
                print(f"error: server returned {auth_resp.status_code}", file=sys.stderr)
                return 1

            # A proxy can answer 200 with an HTML page, and json() then
            # raises a ValueError that the handlers below do not catch.
            try:
                stats = auth_resp.json()
            except ValueError:
                stats = None
            if not isinstance(stats, dict):
                content_type = auth_resp.headers.get("content-type", "")
                print(
                    f"error: {url} answered with a body that is not a JSON object "
                    f"(content-type {content_type!r}) — a proxy can be in front of the server",
                    file=sys.stderr,
                )
                return 1
            total = stats.get("total_entries", 0)
            can_read = stats.get("can_read", False)
            can_write = stats.get("can_write", False)
            can_overwrite = stats.get("can_overwrite", False)

            print(f"  Connected. Server has {total} cached entries.")
            perms = []
            if can_read:
                perms.append("read")
            if can_write:
                perms.append("write")
            if can_overwrite:
                perms.append("overwrite")
            perm_str = ", ".join(perms) if perms else "none"
            print(f"  Token permissions: {perm_str}")

            if not can_write:
                print()
                print("  NOTE: Your token is read-only. Remote cache push and clear")
                print("  will be skipped. To write, use a read+write token instead.")
    except httpx.ConnectError:
        print(f"error: cannot connect to {url} — check the URL and network", file=sys.stderr)
        return 1
    except httpx.TimeoutException:
        print(f"error: connection to {url} timed out", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # --- Step 4: Write config ---
    # Store the token atomically with restricted permissions (0600)
    # so it never appears in plaintext inside the shared config.toml
    # and never exists with world-readable permissions, even for a moment.
    token_dir = Path.home() / ".fw-context"
    token_dir.mkdir(parents=True, exist_ok=True)
    token_file = token_dir / ".cache_token"
    tmp_file = token_file.with_suffix(token_file.suffix + ".tmp")
    fd = os.open(str(tmp_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp_file, token_file)

    from fw_context_mcp.config._toml_editor import set_key
    set_key(config_path, "cache_server", "url", url)

    print(f"\nRemote cache configured: {url}")
    print(f"Config written to: {config_path}")
    print("Run 'fw-context cache stats --remote' to verify.")

    return 0


def cmd_cache_clear(args: argparse.Namespace) -> int:
    """Delete cache entries for one or both tiers (``--all``, ``--remote``).

    Requires interactive confirmation unless ``--yes`` is passed, because
    clearing the cache means re-running LLM analysis on the next index,
    which is expensive (CPU/GPU time, possibly cloud API cost).

    WHY per-project remote clear: clearing ALL remote entries would affect
    other team members; the remote clear only removes THIS project's entries
    (resolved via content hashes from the local index DB).
    """
    from ..cache_client import CacheClient, local_cache_clear
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..utils import resolve_project_root

    project_root = resolve_project_root(args.project) if hasattr(args, "project") else None

    # Determine which tiers to clear
    clear_local = args.all or not args.remote
    clear_remote = args.all or args.remote

    if not args.yes:
        tiers = []
        if clear_local:
            tiers.append("local (Tier 1)")
        if clear_remote:
            tiers.append("remote server (Tier 2)")
        answer = input(f"Delete cache for: {', '.join(tiers)}? This is safe — cache will be rebuilt. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    # Tier 1: local global cache (single DB shared by all projects)
    if clear_local:
        result = local_cache_clear()
        if result == 0:
            print("Local cache (Tier 1): deleted")
        else:
            print("Local cache (Tier 1): not found")

    # Tier 2: remote cache — clears only this project's entries
    if clear_remote and project_root:
        cfg = load_config(project_root=project_root)
        cs = cfg.cache_server
        if cs and cs.url:
            project_id = derive_project_id(project_root)
            db_path = cfg.index.db_dir / project_id / "index.db"
            hashes = []
            if db_path.exists():
                from ..indexer.db import open_db

                conn = open_db(db_path)
                try:
                    hashes = [
                        r[0]
                        for r in conn.execute(
                            "SELECT DISTINCT content_hash FROM llm_analysis WHERE content_hash != ''"
                        ).fetchall()
                    ]
                finally:
                    conn.close()
            if hashes:
                cc = CacheClient(url=cs.url, token=cs.token)
                try:
                    n = cc.clear_remote(hashes)
                    print(f"Remote cache (Tier 2): cleared {n}/{len(hashes)} entries")
                finally:
                    cc.close()
            else:
                print("Remote cache (Tier 2): no project cache entries to clear")
        else:
            print("Remote cache (Tier 2): not configured (set [cache_server] in .fw-context/local.toml)")
    elif clear_remote:
        print("Remote cache (Tier 2): no project resolved")

    return 0
