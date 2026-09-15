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


def resolve_build(
    conn: sqlite3.Connection,
    project_id: str,
    cfg: Config,
    variant: str = "",
    image: str = "",
) -> tuple[str | None, str | None]:
    """Resolve ``(variant, image)`` → ``(config_hash, error)``.

    The answer is ONE build, thus the answer is one ``config_hash``.  Three
    outcomes, and the two fields tell them apart:

    * ``(hash, None)`` — this build answers the query.
    * ``(None, error)`` — fail-closed refusal, in words for the caller to
      pass on.  See the module docstring for the five refusals.
    * ``(None, None)`` — the project has no indexed build at all.  That is
      not a refusal: the caller decides between an empty answer and an
      error, and the two callers differ.

    This returned a list of scopes and a ``multi`` flag until the merging
    of builds went away.  Neither survived the removal: the list could hold
    one element, which invited a second pass and kept an unreachable merge
    branch alive, and ``multi`` was read nowhere — ``list_variants`` and
    ``get_active_build`` each compute that flag from the config themselves.
    """
    from ...indexer.db import get_active_config, get_builds_for_scope

    build_cfg = cfg.build
    declared = [v.name for v in build_cfg.variants]

    # WHY the index decides and not the config alone: the config DECLARES an
    # intention and ``build_configs`` records the FACT.  An index run with
    # ``--variant`` writes the name into that table, and a config that no
    # longer declares it — edited since, or an index made elsewhere — used
    # to send the query down the single-project path.  There the newest
    # build wins in silence, and the variant and image of the caller are
    # not even read: measured on a seeded index holding an application and
    # a bootloader, a query for the application got the bootloader.
    indexed_variants = sorted(
        {r["variant"] for r in get_builds_for_scope(conn, project_id) if r["variant"]}
    )
    # The two are UNITED, and it is not one or the other.  ``declared or
    # indexed`` read the index only when the config declared NOTHING, thus
    # one declared variant hid every indexed one beside it: that name came
    # back as unknown and its build was out of reach, with no command to
    # reach it — the operator would have to edit config.toml to query a
    # build that already exists.  The reason the index takes part in this
    # decision holds for every name in it, not only for the first.
    #
    # The declared order leads, because it is the order the operator wrote
    # and the order the refusal below reads out.
    names = declared + [v for v in indexed_variants if v not in declared]

    if not names:
        row = get_active_config(conn, project_id)
        return (row["config_hash"] if row is not None else None), None

    # ── Multi-build: fail-closed on variant ──
    if not variant:
        if build_cfg.default_variant:
            variant = build_cfg.default_variant
        else:
            return None, (
                f"Project has {len(names)} build variant(s). Specify ``variant`` "
                f"(one of: {', '.join(names)}) or set [build] default_variant in "
                f"config.toml. Call get_active_build() for the variants/images table."
            )

    if variant == "*":
        return None, (
            "A query about code answers for ONE build. A project can hold a "
            "bootloader, a first-stage loader and the application, and they "
            "are separate programs. Name one variant, and one image of it. "
            "To learn whether a symbol is in two of them, ask twice. Call "
            "get_active_build() for the variants/images table."
        )

    if variant not in set(names):
        return None, f"Unknown variant '{variant}'. Available: {', '.join(sorted(names))}."

    rows = get_builds_for_scope(conn, project_id, variant, image or "")
    if not rows:
        if image:
            # A name of its own: ``names`` above holds the VARIANTS, and a
            # second meaning for one name inside one function reads as the
            # first one until a reader checks.
            known = get_builds_for_scope(conn, project_id, variant)
            known_images = sorted({r["image"] or "" for r in known if r["image"]})
            if known_images:
                return None, (
                    f"Unknown image '{image}' of variant '{variant}'. "
                    f"Available: {', '.join(known_images)}."
                )
        return None, (
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
        return None, (
            f"Variant '{variant}' holds {len(images)} images, and each is a "
            f"separate program: {', '.join(images)}. Specify ``image``. "
            f"Call get_active_build() for the variants/images table."
        )

    # One build answers.  The rows arrive newest first, thus an older build
    # left behind by an interrupted index run cannot win.
    return rows[0]["config_hash"], None


def run_scoped_query(
    root,
    db_path,
    query_fn,
    variant: str = "",
    image: str = "",
) -> list[dict]:
    """Resolve ``(variant, image)`` and run ``query_fn(conn, config_hash)``.

    ``resolve_build`` answers with one build, thus the result is the output
    of that one build with no annotation.  Fail-closed: a resolution error is
    returned as a single ``{"error": …}`` dict, which names what the caller
    must choose.  A project with no indexed build gives an empty list.
    """
    from ...config import derive_project_id
    from ...config import load as load_config
    from .context import _quick_open_readonly
    from .stale import _with_stale_recovery

    project_id = derive_project_id(root)
    cfg = load_config(root)
    conn = _quick_open_readonly(db_path)
    try:
        config_hash, err = resolve_build(conn, project_id, cfg, variant or "", image or "")
    finally:
        conn.close()
    if err:
        return [{"error": err}]
    if config_hash is None:
        return []
    return _with_stale_recovery(root, db_path, query_fn, config_hash=config_hash)
