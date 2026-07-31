"""``fw-context cache`` — LLM analysis cache management (local + remote)."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import httpx


def cmd_cache_stats(args: argparse.Namespace) -> int:
    """Show cache statistics for one or both tiers."""
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..utils import resolve_project_root
    from ..cache_client import local_cache_stats, CacheClient

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
            if not remote_stats:
                print(f"Remote cache (Tier 2): {cs.url}")
                print("  Server unreachable — check connectivity")
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
    """Push all local cache entries to the remote cache server.

    Uses ``--force`` by default (X-Cache-Overwrite) so newer local entries
    replace older remote ones.  Progress is reported in batches.
    """
    from ..config import load as load_config
    from ..utils import resolve_project_root
    from ..cache_client import get_local_cache_db, CacheClient

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

        batch_size = args.batch or cs.batch_size
        cc = CacheClient(url=cs.url, token=cs.token, force=True, batch_size=batch_size)
        try:
            pushed = 0
            for i in range(0, total, batch_size):
                chunk = rows[i : i + batch_size]
                entries = [
                    {"hash": r[0], "summary": r[1], "inputs": r[2], "outputs": r[3], "model": r[4]} for r in chunk
                ]
                n = cc.batch_put(entries)
                pushed += n
                print(f"  [{i + len(chunk)}/{total}] pushed {n} entries")
            print(f"Done: {pushed}/{total} entries pushed to {cs.url}")
        finally:
            cc.close()
    finally:
        local_db.close()

    return 0


def cmd_cache_remote_init(args: argparse.Namespace) -> int:
    """Interactive wizard: configure remote cache server connection.

    Prompts for URL and token, verifies the connection, and writes
    [cache_server] to the global config (~/.fw-context/config.toml).
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
            if auth_resp.status_code != 200:
                print(f"error: server returned {auth_resp.status_code}", file=sys.stderr)
                return 1

            stats = auth_resp.json()
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

    cache_section = f"""
[cache_server]
url = "{url}"
"""

    if "[cache_server]" in existing:
        # Replace existing section
        new_content = re.sub(
            r"\[cache_server\].*?(?=\[|$)",
            cache_section.strip(),
            existing,
            flags=re.DOTALL,
        )
    else:
        # Append
        new_content = existing.rstrip("\n") + "\n" + cache_section.strip() + "\n"

    config_path.write_text(new_content, encoding="utf-8")
    print(f"\nRemote cache configured: {url}")
    print(f"Config written to: {config_path}")
    print("Run 'fw-context cache stats --remote' to verify.")

    return 0


def cmd_cache_clear(args: argparse.Namespace) -> int:
    """Delete cache entries for one or both tiers."""
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..utils import resolve_project_root
    from ..cache_client import local_cache_clear, CacheClient

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
