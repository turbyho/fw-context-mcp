"""Variant/image scoping for MCP query tools.

Resolves the ``(variant, image)`` selection carried by query-tool parameters
into a list of concrete build scopes (``config_hash`` + identity), with the
fail-closed selection semantics from §5.7:

- single-project → one scope ``(variant='', image='')``, no error.
- multi-project, variant omitted → error unless ``[build] default_variant``.
- ``variant="*"`` → every build.
- ``image`` omitted → all images of the selected variant.
- unknown variant → error listing known names.
- declared-but-unindexed variant → error pointing at ``fw-context index``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from ...config.settings import Config


def resolve_scopes(
    conn: sqlite3.Connection,
    project_id: str,
    cfg: Config,
    variant: str = "",
    image: str = "",
) -> tuple[list[dict], bool, str | None]:
    """Resolve ``(variant, image)`` → ``(scopes, multi, error)``.

    Each scope is ``{"config_hash", "variant", "image"}``.  ``multi`` is True
    for multi-variant projects (even when a single scope resolves).  ``error``
    is a human-readable string on failure (fail-closed).
    """
    from ...indexer.db import get_active_config, get_builds_for_scope

    build_cfg = cfg.build
    multi = bool(build_cfg.variants)

    if not multi:
        row = get_active_config(conn, project_id)
        scopes = (
            [{"config_hash": row["config_hash"], "variant": "", "image": ""}]
            if row is not None
            else []
        )
        return scopes, False, None

    # ── Multi-variant: fail-closed on variant ──
    if not variant:
        if build_cfg.default_variant:
            variant = build_cfg.default_variant
        else:
            name_list = [v.name for v in build_cfg.variants]
            return [], True, (
                f"Project has {len(name_list)} build variant(s). Specify ``variant`` "
                f"(one of: {', '.join(name_list)}) or set [build] default_variant in "
                f"config.toml. Call get_active_build() for the variants/images table."
            )

    if variant == "*":
        rows = get_builds_for_scope(conn, project_id, "*")
        return [
            {"config_hash": r["config_hash"], "variant": r["variant"] or "", "image": r["image"] or ""}
            for r in rows
        ], True, None

    name_set = {v.name for v in build_cfg.variants}
    if variant not in name_set:
        return [], True, f"Unknown variant '{variant}'. Available: {', '.join(sorted(name_set))}."

    rows = get_builds_for_scope(conn, project_id, variant, image or "")
    if not rows:
        return [], True, (
            f"Variant '{variant}' is declared in config but not indexed. "
            f"Run 'fw-context index --build --variant {variant}'."
        )
    return [
        {"config_hash": r["config_hash"], "variant": r["variant"] or "", "image": r["image"] or ""}
        for r in rows
    ], True, None


def run_scoped_query(
    root,
    db_path,
    query_fn,
    variant: str = "",
    image: str = "",
) -> list[dict]:
    """Resolve ``(variant, image)`` and run ``query_fn(conn, config_hash)`` per scope.

    Single-scope results are bit-identical to today's single-build output
    (no annotation).  Multi-scope results merge per-build outputs, annotating
    each record with its resolved ``variant``/``image``.  Fail-closed: a
    resolution error is returned as a single ``{"error": …}`` dict.
    """
    from ...config import derive_project_id
    from ...config import load as load_config
    from .context import _quick_open_readonly
    from .stale import _with_stale_recovery

    project_id = derive_project_id(root)
    cfg = load_config(root)
    conn = _quick_open_readonly(db_path)
    try:
        scopes, _multi, err = resolve_scopes(conn, project_id, cfg, variant or "", image or "")
    finally:
        conn.close()
    if err:
        return [{"error": err}]
    if len(scopes) == 1:
        return _with_stale_recovery(root, db_path, query_fn, config_hash=scopes[0]["config_hash"])

    merged: list[dict] = []
    for scope in scopes:
        part = _with_stale_recovery(root, db_path, query_fn, config_hash=scope["config_hash"])
        for r in part:
            if isinstance(r, dict) and "error" not in r and "warning" not in r:
                r = dict(r)
                r["variant"] = scope["variant"]
                r["image"] = scope["image"]
            merged.append(r)
    return merged
