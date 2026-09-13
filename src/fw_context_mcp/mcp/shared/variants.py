"""Variant/image scoping for MCP query tools.

Resolves the ``(variant, image)`` selection carried by query-tool parameters
into ONE concrete build scope (``config_hash`` + identity).

── One question about code, one build ──

A project can hold several builds, and they are not variations of one answer.
Measured on one Zephyr project, nine builds sit on two axes: two boards
(``nrf52840-dev``, ``nrf54lm20a-dev``) and, inside each, several images —
``app``, ``mcuboot``, ``stage0``, ``app_slot1_variant``.  An image is a
separate firmware binary: a bootloader is not the application.

Nobody reasons about code across two programs or two boards at once, thus a
merged answer serves no question and carries two hazards.  It blends symbols
of different binaries, and where two builds ARE near-identical it duplicates
almost every row: ``app`` and ``app_slot1_variant`` of that project share
10585 of about 11000 names.

Both selectors are therefore fail-closed:

- single-project → one scope ``(variant='', image='')``, no error.
- multi-build, variant omitted → error unless ``[build] default_variant``.
- multi-build, image omitted while the variant holds several → error that
  lists the images.
- unknown variant or image → error listing the known names.
- declared-but-unindexed variant → error pointing at ``fw-context index``.

A caller that wants to know whether a symbol lives in the bootloader as well
asks twice, once per image.  Two plain answers beat one blended answer.
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

    *scopes* holds AT MOST ONE scope ``{"config_hash", "variant", "image"}``.
    ``multi`` is True for a project that declares build variants, even when
    one scope resolves.  ``error`` is a human-readable string on failure,
    because both selectors are fail-closed — see the module docstring.
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

    # ── Multi-build: fail-closed on variant ──
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
        return [], True, (
            "A query about code answers for ONE build. A project can hold a "
            "bootloader, a first-stage loader and the application, and they "
            "are separate programs. Name one variant, and one image of it. "
            "To learn whether a symbol is in two of them, ask twice. Call "
            "get_active_build() for the variants/images table."
        )

    name_set = {v.name for v in build_cfg.variants}
    if variant not in name_set:
        return [], True, f"Unknown variant '{variant}'. Available: {', '.join(sorted(name_set))}."

    rows = get_builds_for_scope(conn, project_id, variant, image or "")
    if not rows:
        if image:
            known = get_builds_for_scope(conn, project_id, variant)
            names = sorted({r["image"] or "" for r in known if r["image"]})
            if names:
                return [], True, (
                    f"Unknown image '{image}' of variant '{variant}'. "
                    f"Available: {', '.join(names)}."
                )
        return [], True, (
            f"Variant '{variant}' is declared in config but not indexed. "
            f"Run 'fw-context index --build --variant {variant}'."
        )

    # ── Fail-closed on image ──
    # Several images of one variant are several PROGRAMS, thus a query that
    # names none of them has not said what it asks about.  Answering for all
    # of them would blend a bootloader with an application, and two builds of
    # one application would duplicate nearly every row.
    images = sorted({r["image"] or "" for r in rows if r["image"]})
    if not image and len(images) > 1:
        return [], True, (
            f"Variant '{variant}' holds {len(images)} images, and each is a "
            f"separate program: {', '.join(images)}. Specify ``image``. "
            f"Call get_active_build() for the variants/images table."
        )

    # One build answers.  The rows arrive newest first, thus an older build
    # left behind by an interrupted index run cannot win.
    row = rows[0]
    return [
        {
            "config_hash": row["config_hash"],
            "variant": row["variant"] or "",
            "image": row["image"] or "",
        }
    ], True, None


def run_scoped_query(
    root,
    db_path,
    query_fn,
    variant: str = "",
    image: str = "",
) -> list[dict]:
    """Resolve ``(variant, image)`` and run ``query_fn(conn, config_hash)``.

    ``resolve_scopes`` answers with one scope, thus the result is the output
    of that one build with no annotation.  Fail-closed: a resolution error is
    returned as a single ``{"error": …}`` dict, which names what the caller
    must choose.
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
    if not scopes:
        return []
    return _with_stale_recovery(root, db_path, query_fn, config_hash=scopes[0]["config_hash"])
